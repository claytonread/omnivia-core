"""Core-owned execution of one durable Chat generation queue item.

The executor is deliberately provider-neutral.  Its injected ``invoke`` boundary
accepts the governed F2a :class:`ProviderInvocationRequest` and yields normalized
F2a event mappings; credentials, SDK objects and endpoints remain outside Core.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from omnivia_core.chat_contract.v1 import (
    ProviderInvocationRequest,
    codec,
    to_canonical_json,
)
from omnivia_core.chat_contract.v1.generated import (
    DIRECTION_CONTENT_MAX_UTF8_BYTES,
    F2A_PROVIDER_EVENT_TYPES,
)
from omnivia_core_runtime.ownership.identity import Clock, ServiceInstanceIdentity
from omnivia_core_runtime.service.chat_attachment_authority import (
    ATTACHMENT_PART_TYPE,
    AttachmentRefused,
    attachment_reference_of_part,
    revalidate_attachments,
)
from omnivia_core_runtime.service.chat_compaction import derive_model_visible_context
from omnivia_core_runtime.service.chat_generation import (
    DEFAULT_LEASE_US,
    MAX_GENERATION_ATTEMPTS,
    MAX_REASONING_SUMMARY_BYTES,
    MAX_RESPONSE_TEXT_BYTES,
    BoundaryUnavailable,
    ContinuationIdentities,
    DirectionNotPending,
    GenerationConflict,
    GenerationHeartbeat,
    GenerationLifecycleError,
    GenerationNotFound,
    GenerationTerminal,
    StaleGenerationVersion,
    UnsupportedProviderEvent,
    append_provider_generation_event,
    append_provider_reasoning_summary,
    append_provider_structured_output,
    append_provider_text_delta,
    apply_direction_continuation,
    claim_fork_generation,
    claim_queued_generation,
    close_unapplied_direction,
    continuation_identities,
    generated_text_digest,
    provider_finish_id,
    stage_direction_continuation,
)
from omnivia_core_runtime.service.chat_part_registry import (
    ACTIVITY_SERIES_PART_TYPE,
    ACTIVITY_SERIES_RESPONSE_SCHEMA_REF,
    CHART_PART_TYPE,
    CHART_RESPONSE_SCHEMA_REF,
    DATA_TABLE_RESPONSE_SCHEMA_REF,
    DIAGRAM_FLOW_PART_TYPE,
    DIAGRAM_FLOW_RESPONSE_SCHEMA_REF,
    GUARDRAIL_NOTICE_PART_TYPE,
    GUARDRAIL_NOTICE_REASON,
    GUARDRAIL_NOTICE_SAFE_MESSAGE,
    METRIC_NUMBER_PART_TYPE,
    NUMBER_TICKER_RESPONSE_SCHEMA_REF,
    PRESENTATION_ONLY_PART_TYPES,
    REASONING_SUMMARY_PART_TYPE,
    RESPONSE_FORMAT_PART_TYPE,
    RICH_PROSE_PART_TYPE,
    TOOL_CALL_PART_TYPE,
    WEB_SEARCH_PART_TYPE,
    PartPayloadInvalid,
    validate_registered_payload,
)
from omnivia_core_runtime.service.chat_tool_lifecycle import (
    PROVIDER_REPORTED_REGISTRY_REF,
    PROVIDER_REPORTED_TOOL_VERSION,
    deny_tool_call,
    open_chat_turn,
    record_provider_tool_result,
    record_tool_proposal,
)
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.connection import StorageError

__all__ = [
    "AttachmentUnavailable",
    "ChatGenerationExecutor",
    "GenerationExecutorConfig",
    "GenerationExecutorError",
    "ProviderRouteUnavailable",
    "ProviderStream",
]

ProviderStream = Callable[[ProviderInvocationRequest], Iterable[Mapping[str, Any]]]
#: Head-room between an invocation's deadline and the lease that must outlive it.
#: A lease shorter than the deadline expires while the executor is still legitimately
#: waiting, so the claim is sized from the deadline rather than left at the default.
_LEASE_MARGIN_SECONDS: Final = 30
#: Sanitized, caller-safe text per terminal outcome. Never provider content.
_SAFE_MESSAGES: Final = {
    "provider-unavailable": "no provider route was reachable for this generation",
    "malformed-response": "the provider stream could not be completed safely",
    # The closed F2a vocabulary has no attachment code; `invalid-request` is the one that
    # says the request was refused before it was sent, for a reason on the caller's side.
    "invalid-request": "an attached file is no longer available, so no request was sent",
}
_TERMINAL_PROVIDER_EVENTS: Final = frozenset({"finish", "error"})
#: `common.schema.json#/$defs/ProviderAttemptId` is a WorkspaceScopedId used for one
#: provider transport attempt.
_TRANSPORT_ATTEMPT_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
#: `ProviderPartId` and `ProviderStepId` are both `WorkspaceScopedId` in the contract.
_PROVIDER_ID: Final = _TRANSPORT_ATTEMPT_ID
_PROMPT_BUNDLE: Final = {
    "id": "core.chat.default-model-visible-context",
    "version": 1,
    "kind": "message-lineage",
}
#: A continuation assembles its context differently (the output so far and the Direction
#: are model-visible too), so its manifest names a different bundle rather than passing
#: itself off as the default one.
_CONTINUATION_PROMPT_BUNDLE: Final = {
    "id": "core.chat.direction-continuation-context",
    "version": 1,
    "kind": "message-lineage-continuation",
}
#: `provider.schema.json` bounds one request to 4096 messages.
_MAX_REQUEST_MESSAGES: Final = 4096
#: `common.schema.json#/$defs/DirectionTextPart` and `generation.schema.json#/$defs/Direction`.
_DIRECTION_MAX_PARTS: Final = 16
_DIRECTION_TEXT_MAX_CHARS: Final = 4000
#: How often one boundary re-reads the pending Direction after it changed under the
#: transition (a supersession landing between the read and the commit). Each try is
#: fully re-proved; the bound only stops a writer who keeps replacing it from holding the
#: boundary open for ever.
_MAX_DIRECTION_TRIES: Final = 3
#: How a Direction still pending ends once its Generation has, by the status the
#: Generation ended in: a stopped Generation cancels it as `generation_stopped`, one that
#: ended any other way as `job_terminal`, and one paused for a retry has lost the Attempt
#: the Direction was aimed at.
_UNAPPLIED_OF_JOB_STATE: Final = {
    "cancelled": ("cancelled", "generation_stopped"),
    "succeeded": ("cancelled", "job_terminal"),
    "failed": ("cancelled", "job_terminal"),
    "retryable": ("rejected", "target_attempt_ended"),
}


class GenerationExecutorError(Exception):
    """A sanitized executor refusal; provider content is never included."""


class AttachmentUnavailable(GenerationExecutorError):
    """An accepted attachment that may no longer be sent; nothing was sent for it.

    Revoked, removed, changed or physically gone between admission and this attempt. It
    ends the Generation before any provider call, as a non-retryable `invalid-request`:
    retrying cannot bring the file back, and the actor sends again without it.
    """


class ProviderRouteUnavailable(GenerationExecutorError):
    """No provider route could be resolved, or the adapter cannot reach one.

    Distinct from every other executor failure ON PURPOSE. "I never reached a
    provider" and "a provider answered badly" are different facts about the same
    attempt, and collapsing them loses the one a reader needs: whether the failure
    is a routing or configuration problem to fix or a response to investigate. The frozen F2a
    vocabulary already separates them -- `provider-unavailable` against
    `malformed-response` -- so the distinction costs nothing to keep.

    Raised by route resolution here, and available to an injected adapter that
    discovers at call time that its connection is unreachable.
    """


@dataclass(frozen=True, slots=True)
class GenerationExecutorConfig:
    connection_id: str
    model_id: str
    policy_ref: str
    classification_ref: str
    residency_ref: str
    service_actor_id: str
    deadline_seconds: int = 120


def _stable_id(kind: str, source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return f"{kind}-{digest[:40]}"


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _now_us(clock: Clock) -> int:
    return int(clock.wall_time().timestamp() * 1_000_000)


def _next_outbox_cursor(connection: sqlite3.Connection, workspace_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(outbox_cursor), 0) + 1 "
        "FROM omnivia_chat_transactional_outbox WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    return int(row[0])


@dataclass(frozen=True, slots=True)
class _ProviderContext:
    messages: tuple[Mapping[str, Any], ...]
    manifest_message_refs: tuple[Mapping[str, Any], ...]
    #: The attachment references the request sends, pinned, in the order it sends them.
    attachments: tuple[Mapping[str, str], ...] = ()


def _data_table_provider_text(payload: Mapping[str, Any]) -> str:
    """Deterministic model-visible text for one committed data table.

    The table remains a typed transcript part for rendering. F2a provider messages
    have no table part, so later turns receive the exact validated columns and rows
    as bounded canonical JSON rather than silently losing the prior answer.
    """
    try:
        validate_registered_payload(RICH_PROSE_PART_TYPE, payload)
    except PartPayloadInvalid as error:
        raise GenerationExecutorError(
            "the conversation contains a malformed rich-prose table"
        ) from error
    block = payload["blocks"][0]
    return "Data table:\n" + to_canonical_json(
        {"cols": block["cols"], "rows": block["rows"]}
    )


def _metric_number_provider_text(payload: Mapping[str, Any]) -> str:
    """Deterministic model-visible text for one committed numeric metric."""
    try:
        validate_registered_payload(METRIC_NUMBER_PART_TYPE, payload)
    except PartPayloadInvalid as error:
        raise GenerationExecutorError(
            "the conversation contains a malformed numeric metric"
        ) from error
    return "Metric:\n" + to_canonical_json(dict(payload))


def _chart_provider_text(payload: Mapping[str, Any]) -> str:
    """Deterministic model-visible text for one committed chart answer."""
    try:
        validate_registered_payload(CHART_PART_TYPE, payload)
    except PartPayloadInvalid as error:
        raise GenerationExecutorError(
            "the conversation contains a malformed chart"
        ) from error
    return "Chart:\n" + to_canonical_json(dict(payload))


def _diagram_flow_provider_text(payload: Mapping[str, Any]) -> str:
    """Deterministic model-visible text for one committed directed graph."""
    try:
        validate_registered_payload(DIAGRAM_FLOW_PART_TYPE, payload)
    except PartPayloadInvalid as error:
        raise GenerationExecutorError(
            "the conversation contains a malformed diagram flow"
        ) from error
    return "Diagram:\n" + to_canonical_json(dict(payload))


def _activity_series_provider_text(payload: Mapping[str, Any]) -> str:
    """Deterministic model-visible text for one committed activity series."""
    try:
        validate_registered_payload(ACTIVITY_SERIES_PART_TYPE, payload)
    except PartPayloadInvalid as error:
        raise GenerationExecutorError(
            "the conversation contains a malformed activity series"
        ) from error
    return "Activity:\n" + to_canonical_json(dict(payload))


def _trigger_response_format(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    trigger_message_id: str,
) -> Mapping[str, Any]:
    """Resolve the one internal response-format directive on a trigger Message."""
    directives = tuple(
        part
        for part in chat.read_message_parts(
            connection, workspace_id=workspace_id, message_id=trigger_message_id
        )
        if part.part_type == RESPONSE_FORMAT_PART_TYPE
    )
    if not directives:
        return {"kind": "text"}
    if len(directives) != 1 or directives[0].visibility != "internal":
        raise GenerationExecutorError("the generation response-format directive is invalid")
    directive = directives[0]
    try:
        validate_registered_payload(RESPONSE_FORMAT_PART_TYPE, directive.payload)
    except PartPayloadInvalid as error:
        raise GenerationExecutorError(
            "the generation response-format directive is invalid"
        ) from error
    return {
        "kind": "structured",
        "schemaRef": directive.payload["schemaRef"],
    }


def _provider_context(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    branch_id: str,
    trigger_message_id: str,
    blobs_root: Path | None,
) -> _ProviderContext:
    """Build the request projection and the matching hash/reference manifest.

    The request still needs text because the provider boundary needs it. The
    manifest does not copy that text; it records the exact ordered Message/Part
    identities and content hashes from which the request is reconstructable.
    """
    compacted = derive_model_visible_context(
        connection,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        branch_id=branch_id,
        trigger_message_id=trigger_message_id,
    )
    if compacted is not None:
        return _ProviderContext(
            compacted.messages,
            compacted.manifest_message_refs,
        )

    messages = chat.read_messages_by_conversation_sequence(
        connection, workspace_id=workspace_id, conversation_id=conversation_id
    )
    by_id = {message.message_id: message for message in messages}
    lineage: list[chat.Message] = []
    seen: set[str] = set()
    current_id: str | None = trigger_message_id
    while current_id is not None:
        if current_id in seen:
            raise GenerationExecutorError("the conversation message ancestry contains a cycle")
        seen.add(current_id)
        message = by_id.get(current_id)
        if message is None:
            raise GenerationExecutorError("the generation trigger ancestry is incomplete")
        lineage.append(message)
        current_id = message.parent_message_id

    provider_messages: list[Mapping[str, Any]] = []
    manifest_refs: list[Mapping[str, Any]] = []
    attachments: list[Mapping[str, str]] = []
    for message in reversed(lineage):
        if message.visibility != "standard" or message.completion_status != "complete":
            continue
        provider_parts: list[Mapping[str, Any]] = []
        part_refs: list[Mapping[str, Any]] = []
        for part in chat.read_message_parts(
            connection, workspace_id=workspace_id, message_id=message.message_id
        ):
            if part.visibility != "standard":
                continue
            if part.part_type == ATTACHMENT_PART_TYPE:
                provider_part = _attachment_file_part(
                    connection,
                    message=message,
                    payload=part.payload,
                    current=message.message_id == trigger_message_id,
                    blobs_root=blobs_root,
                )
                attachments.append(provider_part["attachmentReference"])
                provider_parts.append(provider_part)
            elif part.part_type in PRESENTATION_ONLY_PART_TYPES:
                # These exact Core-authored families belong to the transcript UI.
                # They are not provider prompt content and therefore have no entry
                # in either the F2a message or its model-visible part references.
                continue
            elif part.part_type == RICH_PROSE_PART_TYPE:
                provider_parts.append(
                    {"kind": "text", "text": _data_table_provider_text(part.payload)}
                )
            elif part.part_type == METRIC_NUMBER_PART_TYPE:
                provider_parts.append(
                    {"kind": "text", "text": _metric_number_provider_text(part.payload)}
                )
            elif part.part_type == CHART_PART_TYPE:
                provider_parts.append(
                    {"kind": "text", "text": _chart_provider_text(part.payload)}
                )
            elif part.part_type == DIAGRAM_FLOW_PART_TYPE:
                provider_parts.append(
                    {"kind": "text", "text": _diagram_flow_provider_text(part.payload)}
                )
            elif part.part_type == ACTIVITY_SERIES_PART_TYPE:
                provider_parts.append(
                    {"kind": "text", "text": _activity_series_provider_text(part.payload)}
                )
            else:
                text = part.payload.get("text") if part.part_type == "text" else None
                if not isinstance(text, str):
                    raise GenerationExecutorError(
                        "the conversation contains a message part this executor cannot project to F2a"
                    )
                provider_parts.append({"kind": "text", "text": text})
            part_refs.append(
                {
                    "partId": part.part_id,
                    "partIndex": part.part_index,
                    "partType": part.part_type,
                    "schemaVersion": part.schema_version,
                    "contentHash": part.content_hash,
                }
            )
        if provider_parts:
            provider_messages.append({"role": message.role, "parts": provider_parts})
            manifest_refs.append(
                {
                    "messageId": message.message_id,
                    "role": message.role,
                    "conversationSequence": message.conversation_sequence,
                    "schemaVersion": message.schema_version,
                    "contentHash": message.content_hash,
                    "parts": part_refs,
                }
            )
    if not provider_messages:
        raise GenerationExecutorError("the conversation contains no provider-visible messages")
    return _ProviderContext(tuple(provider_messages), tuple(manifest_refs), tuple(attachments))


def _attachment_file_part(
    connection: sqlite3.Connection,
    *,
    message: chat.Message,
    payload: Mapping[str, Any],
    current: bool,
    blobs_root: Path | None,
) -> dict[str, Any]:
    """One accepted `attachment` part as the provider contract's `file` part, revalidated.

    The part carries the opaque `AttachmentReference` and nothing else: no path, URL or
    byte. The trigger Message's attachment (`current`) is re-proved as admission proved it,
    staged row and a full re-hash included; an earlier turn's is re-proved against the
    evidence alone, so the composer removing a long-sent file does not break every later
    turn of the branch.
    """
    reference = attachment_reference_of_part(payload)
    if reference is None:
        raise GenerationExecutorError("the conversation contains a malformed attachment part")
    if message.author_id is None:
        # No author means no staged authority to prove the file against.
        raise AttachmentUnavailable("an accepted attachment has no author to prove it against")
    try:
        (pinned,) = revalidate_attachments(
            connection,
            workspace_id=message.workspace_id,
            conversation_id=message.conversation_id,
            actor_id=message.author_id,
            references=(reference,),
            blobs_root=blobs_root,
            require_staged=current,
            rehash=current,
        )
    except AttachmentRefused:
        raise AttachmentUnavailable("an accepted attachment is no longer available") from None
    # `.reference` and never the PinnedAttachment itself: the provider contract's
    # `file` part carries the opaque reference, and the display facts the part payload
    # also holds are for a renderer, not for a model.
    return {"kind": "file", "attachmentReference": dict(pinned.reference)}


def _digest_json(value: Mapping[str, Any] | tuple[Mapping[str, Any], ...]) -> str:
    document = to_canonical_json({"value": _plain_json(value)})
    return "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest()


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(nested) for nested in value]
    if isinstance(value, list):
        return [_plain_json(nested) for nested in value]
    return value


@dataclass(frozen=True, slots=True)
class _Hop:
    """One Direction taken in at an iteration boundary: what a later request adds to its context.

    A durable hop is read from a continuation row and its applied Direction. The hop about
    to be written is planned from the pending Direction and the identities derived for it.
    One builder composes the request and the manifest from either, so the request a
    continuation is first invoked with and the one a replay rebuilds are the same.
    """

    continuation_id: str
    prior_attempt_id: str
    next_attempt_id: str
    direction_id: str
    direction_digest: str
    direction_texts: tuple[str, ...]
    provider_finish_id: str
    prior_output_digest: str
    prior_text: str


class _DirectionRefusedError(Exception):
    """A pending Direction that cannot be taken in here, with the closed pair saying why.

    A signal inside this module, never raised past it: the boundary that sees it closes the
    Direction with that pair and settles the Generation without it.
    """

    def __init__(self, state: str, code: str) -> None:
        super().__init__(code)
        self.state = state
        self.code = code


def _direction_texts(direction: chat.Direction) -> tuple[str, ...] | None:
    """A Direction's authored text parts if its stored content is exactly the contract's.

    `{"editableParts": [...]}` and nothing else, one to sixteen closed text parts of one to
    4000 characters, and the canonical object within its byte bound. Anything else is
    `None`: a Direction this build cannot read as the contract defines it is not text to
    put in front of a model.
    """
    content = direction.content
    parts = content.get("editableParts")
    if (
        set(content) != {"editableParts"}
        or not isinstance(parts, list)
        or not 1 <= len(parts) <= _DIRECTION_MAX_PARTS
    ):
        return None
    texts: list[str] = []
    for part in parts:
        payload = part.get("payload") if isinstance(part, Mapping) else None
        text = payload.get("text") if isinstance(payload, Mapping) else None
        if (
            not isinstance(part, Mapping)
            or not isinstance(payload, Mapping)
            or set(part) != {"payload", "type"}
            or part["type"] != "text"
            or set(payload) != {"text"}
            or not isinstance(text, str)
            or not 1 <= len(text) <= _DIRECTION_TEXT_MAX_CHARS
        ):
            return None
        texts.append(text)
    if codec.direction_content_byte_size(parts) > DIRECTION_CONTENT_MAX_UTF8_BYTES:
        return None
    return tuple(texts)


def _hop_messages(hops: tuple[_Hop, ...]) -> tuple[Mapping[str, Any], ...]:
    """What each hop adds to the model-visible context: the output so far, then the Direction.

    The output is the model's own earlier turn and the Direction is the actor speaking
    again, so they go in as an `assistant` and a `user` message. Neither is a Message in
    the conversation graph, and neither is ever written back to it.
    """
    messages: list[Mapping[str, Any]] = []
    for hop in hops:
        messages.append({"role": "assistant", "parts": [{"kind": "text", "text": hop.prior_text}]})
        messages.append(
            {"role": "user", "parts": [{"kind": "text", "text": text} for text in hop.direction_texts]}
        )
    return tuple(messages)


def _hops_manifest(hops: tuple[_Hop, ...]) -> Mapping[str, Any]:
    """The continuation lineage as identifiers and digests, never as text."""
    return {
        "iteration": len(hops),
        "hops": [
            {
                "continuationId": hop.continuation_id,
                "priorAttemptId": hop.prior_attempt_id,
                "nextAttemptId": hop.next_attempt_id,
                "directionId": hop.direction_id,
                "directionContentDigest": hop.direction_digest,
                "providerFinishId": hop.provider_finish_id,
                "priorOutputDigest": hop.prior_output_digest,
            }
            for hop in hops
        ],
    }


def _request_manifest_body(
    *,
    request: ProviderInvocationRequest,
    job: chat.GenerationJob,
    manifest_message_refs: tuple[Mapping[str, Any], ...],
    attachments: tuple[Mapping[str, str], ...] = (),
    hops: tuple[_Hop, ...] = (),
) -> Mapping[str, Any]:
    response_format_digest = _digest_json(request.response_format)
    generation_options_digest = (
        None if request.generation_options is None else _digest_json(request.generation_options)
    )
    provider_options_digest = (
        None
        if request.provider_options_by_namespace is None
        else _digest_json(request.provider_options_by_namespace)
    )
    prompt_bundle = _CONTINUATION_PROMPT_BUNDLE if hops else _PROMPT_BUNDLE
    prompt_bundle_digest = _digest_json(prompt_bundle)
    body: dict[str, Any] = {
        "schemaVersion": 1,
        "requestManifestId": _stable_id(
            "request-manifest", f"{job.generation_job_id}:{request.attempt_id}"
        ),
        "workspaceId": request.workspace_id,
        "conversationId": request.conversation_id,
        "branchId": job.branch_id,
        "generationJobId": request.job_id,
        "generationAttemptId": request.attempt_id,
        "triggerMessageId": job.trigger_message_id,
        "providerInvocationId": request.invocation_id,
        "idempotencyKey": request.idempotency_key,
        "modelVisible": {
            "messages": list(manifest_message_refs),
            "renderedPromptContent": "not_stored",
            "contentPolicy": "identifiers_and_hashes_only",
        },
        "promptBundle": {
            **prompt_bundle,
            "digest": prompt_bundle_digest,
        },
        "toolDefinitions": [],
        "route": {
            "connectionId": request.connection_id,
            "modelId": request.model_id,
            "operation": request.operation,
        },
        "requestOptions": {
            "responseFormatDigest": response_format_digest,
            "generationOptionsDigest": generation_options_digest,
            "providerOptionsByNamespaceDigest": provider_options_digest,
            "deadlineAt": request.deadline_at,
            "requestedAt": request.requested_at,
            "causationId": request.causation_id,
            "correlationId": request.correlation_id,
        },
        "policy": {
            "policyRef": request.policy_ref,
            "classificationRef": request.classification_ref,
            "residencyRef": request.residency_ref,
        },
        "authorizedReferences": {
            "context": [],
            "attachments": [dict(reference) for reference in attachments],
            "evidence": [],
        },
        "retention": {
            "class": "chat_request_manifest_v1",
            "export": "metadata_and_hashes",
        },
    }
    if hops:
        # 0069's exact binding: three keys, and `directionVersion` is the pending version
        # this manifest was built from -- always 1, whatever the Direction becomes by it.
        body["direction"] = {
            "contentDigest": hops[-1].direction_digest,
            "directionId": hops[-1].direction_id,
            "directionVersion": 1,
        }
        body["continuation"] = _hops_manifest(hops)
    return body


def _existing_manifest_timestamps(
    connection: sqlite3.Connection, *, workspace_id: str, generation_attempt_id: str
) -> tuple[str, str] | None:
    existing = chat.read_request_manifest(
        connection, workspace_id=workspace_id, generation_attempt_id=generation_attempt_id
    )
    if existing is None:
        return None
    options = existing.manifest_body.get("requestOptions")
    if not isinstance(options, Mapping):
        raise GenerationExecutorError("the stored request manifest is malformed")
    requested_at = options.get("requestedAt")
    deadline_at = options.get("deadlineAt")
    if not isinstance(requested_at, str) or not isinstance(deadline_at, str):
        raise GenerationExecutorError("the stored request manifest is missing request timestamps")
    return requested_at, deadline_at


#: Why a provider-proposed tool call without a result is denied at the terminal boundary.
NO_EXECUTION_PATH: Final = "no_governed_execution_path"
MAX_PROVIDER_TOOL_RESULT_BYTES: Final = 64 * 1024
PROVIDER_WEB_SEARCH_TOOL_NAME: Final = "codex.web_search"


def tool_call_part_payload(call: chat.ChatToolCall) -> dict[str, Any]:
    """The `tool.call` part payload for one ledger row.

    NO ARGUMENTS, IN ANY FORM -- not the values, not the digest. The ledger
    records them; a Message part publishes to every reader of the conversation,
    and C8 settled that this bundle discloses no tool input outbound, refusing
    to project it even to the reviewer deciding on the call.
    `chat_part_registry` closes this shape so the absence is enforced rather
    than remembered.

    `state` is the row's own state, verbatim, not translated into a consumer's
    display vocabulary. `detail` is fixed Core prose: provider output and call
    arguments never become transcript content.
    """
    payload: dict[str, Any] = {"name": call.tool_name, "id": call.tool_call_id, "state": call.state}
    if call.registry_ref == PROVIDER_REPORTED_REGISTRY_REF:
        if call.state == "succeeded":
            payload["detail"] = "The connected provider reported this tool call completed."
        elif call.state == "failed":
            payload["detail"] = "The connected provider reported this tool call failed."
        else:
            payload["detail"] = (
                "Proposed by the model. Not run by OmniVia: no governed tool execution path was available."
            )
    return payload


def web_search_part_payload(call: chat.ChatToolCall) -> dict[str, Any]:
    """Publish only the query of one exact, completed provider search.

    The provider tool result contains no result documents, URLs or snippets,
    so none are inferred from assistant prose. The query is the sole public
    display fact and remains bounded by the registered part validator.
    """
    arguments = call.proposed_arguments
    if (
        call.registry_ref != PROVIDER_REPORTED_REGISTRY_REF
        or call.tool_version != PROVIDER_REPORTED_TOOL_VERSION
        or call.tool_name != PROVIDER_WEB_SEARCH_TOOL_NAME
        or call.state != "succeeded"
        or not isinstance(arguments, Mapping)
        or set(arguments) != {"query"}
    ):
        raise GenerationExecutorError("a provider web search is malformed")
    payload: dict[str, Any] = {"query": arguments.get("query"), "state": "complete"}
    try:
        validate_registered_payload(WEB_SEARCH_PART_TYPE, payload)
    except PartPayloadInvalid as error:
        raise GenerationExecutorError("a provider web search is malformed") from error
    return payload


def _tool_call_message_part(call: chat.ChatToolCall) -> tuple[str, dict[str, Any]]:
    if (
        call.registry_ref == PROVIDER_REPORTED_REGISTRY_REF
        and getattr(call, "tool_version", None) == PROVIDER_REPORTED_TOOL_VERSION
        and call.tool_name == PROVIDER_WEB_SEARCH_TOOL_NAME
        and call.state == "succeeded"
    ):
        return WEB_SEARCH_PART_TYPE, web_search_part_payload(call)
    return TOOL_CALL_PART_TYPE, tool_call_part_payload(call)


def _part_document(part_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"payload": dict(payload), "type": part_type, "visibility": "standard"}


def reasoning_summary_part_payload(
    summaries: tuple[chat.GenerationReasoningSummary, ...],
) -> dict[str, Any]:
    """Project explicit provider summaries without reading any reasoning content."""
    return {
        "title": "Reasoning summary",
        "detail": "\n\n".join(item.summary_text for item in summaries),
    }


def guardrail_notice_part_payload() -> dict[str, Any]:
    """The one fixed transcript notice for a provider content-filter finish."""
    return {
        "reasonCode": GUARDRAIL_NOTICE_REASON,
        "safeMessage": GUARDRAIL_NOTICE_SAFE_MESSAGE,
    }


def structured_output_message_part(
    output: chat.GenerationStructuredOutput,
) -> tuple[str, dict[str, Any]]:
    if output.schema_ref == ACTIVITY_SERIES_RESPONSE_SCHEMA_REF:
        part_type = ACTIVITY_SERIES_PART_TYPE
        payload = dict(output.output)
    elif output.schema_ref == DATA_TABLE_RESPONSE_SCHEMA_REF:
        part_type = RICH_PROSE_PART_TYPE
        payload = {"blocks": [{**dict(output.output), "kind": "table"}]}
    elif output.schema_ref == NUMBER_TICKER_RESPONSE_SCHEMA_REF:
        part_type = METRIC_NUMBER_PART_TYPE
        payload = dict(output.output)
    elif output.schema_ref == CHART_RESPONSE_SCHEMA_REF:
        part_type = CHART_PART_TYPE
        payload = dict(output.output)
    elif output.schema_ref == DIAGRAM_FLOW_RESPONSE_SCHEMA_REF:
        part_type = DIAGRAM_FLOW_PART_TYPE
        payload = dict(output.output)
    else:
        raise GenerationExecutorError("the structured output schema is unsupported")
    try:
        validate_registered_payload(part_type, payload)
    except PartPayloadInvalid as error:
        raise GenerationExecutorError("the structured output is malformed") from error
    return part_type, payload


def _message_parts(
    text: str,
    tool_calls: tuple[chat.ChatToolCall, ...] = (),
    reasoning_summaries: tuple[chat.GenerationReasoningSummary, ...] = (),
    guardrail_notice: bool = False,
    structured_outputs: tuple[chat.GenerationStructuredOutput, ...] = (),
) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Ordered parts: provider summary, answer text, guardrail, then tools.

    No text with typed non-text content means no text part, never an empty one: a
    tool-call-only or fully filtered response commits only its typed parts rather than
    inventing empty prose. Hash, part index and the rows written all come from this one
    list.
    """
    parts: list[tuple[str, dict[str, Any]]] = []
    if reasoning_summaries:
        parts.append(
            (REASONING_SUMMARY_PART_TYPE, reasoning_summary_part_payload(reasoning_summaries))
        )
    if text or (not tool_calls and not structured_outputs and not guardrail_notice):
        # A text-only commit keeps its text part whatever it holds, so no pre-C9
        # message changes meaning; only typed non-text content can omit it.
        parts.append(("text", {"text": text}))
    parts.extend(structured_output_message_part(output) for output in structured_outputs)
    if guardrail_notice:
        parts.append((GUARDRAIL_NOTICE_PART_TYPE, guardrail_notice_part_payload()))
    parts.extend(_tool_call_message_part(call) for call in tool_calls)
    return tuple(parts)


def _message_hash(
    text: str,
    tool_calls: tuple[chat.ChatToolCall, ...] = (),
    reasoning_summaries: tuple[chat.GenerationReasoningSummary, ...] = (),
    guardrail_notice: bool = False,
    structured_outputs: tuple[chat.GenerationStructuredOutput, ...] = (),
) -> tuple[str, tuple[str, ...]]:
    """The Message `content_hash` and one part hash per part, in part order.

    The message hash is computed over the WHOLE part list, which is what makes
    it the message's content hash rather than its text's. C9 adds parts after
    the text, so they have to be in it: a hash over the text alone would call
    two different messages identical, and the idempotency check in
    :meth:`_materialize_assistant` compares exactly this value against the
    durable one to decide whether a replay is the same commit.

    That check is also why the order has to be deterministic.
    `read_chat_tool_calls_for_generation_job` orders by provider position
    (attempt, step ordinal) for this reason, and the tool calls are durable
    before the commit, so a replay of the same generation hashes the same way.
    """
    parts = _message_parts(
        text,
        tool_calls,
        reasoning_summaries,
        guardrail_notice,
        structured_outputs,
    )
    documents = [_part_document(part_type, payload) for part_type, payload in parts]
    part_hashes = tuple(
        "sha256:" + hashlib.sha256(to_canonical_json(payload).encode("utf-8")).hexdigest()
        for _, payload in parts
    )
    message_document = json.dumps(
        documents,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    message_hash = "sha256:" + hashlib.sha256(message_document.encode("utf-8")).hexdigest()
    return message_hash, part_hashes


@dataclass(frozen=True, slots=True)
class ChatGenerationExecutor:
    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    fencing_generation: int
    workspace_id: str
    clock: Clock
    invoke: ProviderStream
    config: GenerationExecutorConfig
    heartbeat_factory: Callable[..., GenerationHeartbeat] = GenerationHeartbeat
    #: The workspace blob root attachments are proved present under. Without one, any
    #: attachment fails closed as unavailable rather than being sent unchecked.
    blobs_root: Path | None = None

    def execute(self, **fields: Any) -> None:
        """Run either a fresh queued submission or an already-opened retry attempt."""
        if fields.get("queued_submission_id") is not None:
            self.execute_submission(
                queued_submission_id=str(fields["queued_submission_id"]),
                generation_job_id=str(fields["generation_job_id"]),
                trigger_message_id=str(fields["trigger_message_id"]),
            )
            return
        if fields.get("generation_attempt_id") is not None:
            self.execute_generation_attempt(
                generation_job_id=str(fields["generation_job_id"]),
                generation_attempt_id=str(fields["generation_attempt_id"]),
            )
            return
        if fields.get("generation_job_id") is not None and fields.get("trigger_message_id") is not None:
            self.execute_fork_generation(
                generation_job_id=str(fields["generation_job_id"]),
                trigger_message_id=str(fields["trigger_message_id"]),
            )
            return
        raise GenerationExecutorError("no executable chat generation target was supplied")

    def execute_next(self) -> str | None:
        """Execute the next executable queued item, or return ``None`` when idle.

        Queued submissions first, then fork-created jobs
        (:func:`chat.read_next_executable_fork_generation_job`), which have no queue row
        and would otherwise never be found after a restart.

        Reads only rows with a matching generation job already committed
        (:func:`chat.read_next_executable_queued_submission`): an `EnqueueMessage`
        row is `queued` from the moment it is written, well before any
        `SubmitMessage` drains it into a Message and opens the job that makes it
        executable (W5 GB-05). Such a pre-submit row is therefore invisible to this
        read rather than returned and failed -- it waits for its drain, and a later,
        already-executable row is picked up in the meantime.
        """
        submission = chat.read_next_executable_queued_submission(
            self.connection, workspace_id=self.workspace_id
        )
        if submission is None:
            fork = chat.read_next_executable_fork_generation_job(
                self.connection, workspace_id=self.workspace_id
            )
            if fork is None:
                return None
            self.execute_fork_generation(
                generation_job_id=fork.generation_job_id,
                trigger_message_id=fork.trigger_message_id,
            )
            return fork.generation_job_id
        # The join above already proved this job exists; SubmitMessage creates it
        # in the same transaction as the row itself (directly, or by drain).
        existing = self.connection.execute(
            "SELECT generation_job_id, trigger_message_id FROM omnivia_chat_generation_jobs "
            "WHERE workspace_id = ? AND conversation_id = ? AND idempotency_key = ?",
            (self.workspace_id, submission.conversation_id, submission.idempotency_key),
        ).fetchone()
        if existing is None:
            raise GenerationExecutorError("a queued submission has no generation job")
        job_id, trigger_message_id = str(existing[0]), str(existing[1])
        self.execute_submission(
            queued_submission_id=submission.queued_submission_id,
            generation_job_id=job_id,
            trigger_message_id=trigger_message_id,
        )
        return job_id

    def execute_submission(
        self,
        *,
        queued_submission_id: str,
        generation_job_id: str,
        trigger_message_id: str,
    ) -> None:
        """Claim, invoke and durably settle one submitted message's generation."""
        existing_job = chat.read_generation_job(
            self.connection,
            workspace_id=self.workspace_id,
            generation_job_id=generation_job_id,
        )
        if existing_job is not None and existing_job.state in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            self._close_unapplied_direction(generation_job_id)
            return
        attempt_id = _stable_id("genatt", generation_job_id)
        resumed = self._continued_attempt(existing_job, attempt_id)
        if existing_job is not None and resumed is not None:
            self._execute_attempt(
                job=existing_job,
                attempt_id=resumed,
                trigger_message_id=trigger_message_id,
                renew_lease=True,
            )
            return
        claimed = claim_queued_generation(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
            queued_submission_id=queued_submission_id,
            generation_job_id=generation_job_id,
            generation_attempt_id=attempt_id,
            trigger_message_id=trigger_message_id,
            lease_owner=self.identity.service_instance_id,
            now_us=_now_us(self.clock),
            lease_duration_us=self._lease_duration_us(),
        )
        self._execute_attempt(
            job=claimed.job,
            attempt_id=attempt_id,
            trigger_message_id=trigger_message_id,
            renew_lease=True,
        )

    def execute_fork_generation(
        self, *, generation_job_id: str, trigger_message_id: str
    ) -> None:
        """Claim, invoke and durably settle a fork-created job (no queued submission row).

        A job already running is left to its lease and never invoked again, and a
        finished one only has its unapplied Direction closed, so a repeated or late wake
        is harmless. Anything else that is not an unclaimed fork job raises before a write.
        """
        existing_job = chat.read_generation_job(
            self.connection,
            workspace_id=self.workspace_id,
            generation_job_id=generation_job_id,
        )
        if existing_job is not None and existing_job.state != "queued":
            if existing_job.state in {"succeeded", "failed", "cancelled"}:
                self._close_unapplied_direction(generation_job_id)
            return
        attempt_id = _stable_id("genatt", generation_job_id)
        claimed = claim_fork_generation(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
            generation_job_id=generation_job_id,
            generation_attempt_id=attempt_id,
            trigger_message_id=trigger_message_id,
            lease_owner=self.identity.service_instance_id,
            now_us=_now_us(self.clock),
            lease_duration_us=self._lease_duration_us(),
        )
        self._execute_attempt(
            job=claimed,
            attempt_id=attempt_id,
            trigger_message_id=trigger_message_id,
            renew_lease=True,
        )

    def execute_generation_attempt(
        self, *, generation_job_id: str, generation_attempt_id: str
    ) -> None:
        """Invoke the provider for an already-opened retry or continuation attempt.

        A continuation attempt is rebuilt from the durable rows that opened it, so
        calling this for one whose provider was never invoked (or was interrupted) sends
        the very request it would have sent the first time.
        """
        job = chat.read_generation_job(
            self.connection,
            workspace_id=self.workspace_id,
            generation_job_id=generation_job_id,
        )
        status = chat.read_generation_job_status_projection(
            self.connection,
            workspace_id=self.workspace_id,
            generation_job_id=generation_job_id,
        )
        if (
            job is None
            or status is None
            or status.state != "running"
            or status.current_attempt_id != generation_attempt_id
        ):
            self._close_unapplied_direction(generation_job_id)
            return
        self._execute_attempt(
            job=job,
            attempt_id=generation_attempt_id,
            trigger_message_id=job.trigger_message_id,
            renew_lease=job.state not in {"succeeded", "failed", "cancelled"},
        )

    def _execute_attempt(
        self,
        *,
        job: chat.GenerationJob,
        attempt_id: str,
        trigger_message_id: str,
        renew_lease: bool,
    ) -> None:
        """Run an attempt, and then each continuation attempt a Direction opens after it.

        Each pass is one attempt's provider invocation. It returns the successor attempt
        when its iteration ended at a boundary where a Direction was taken in, and `None`
        when the Generation ended there. A loop rather than a recursion, because a
        Generation may be steered as often as it has attempts left.
        """
        next_attempt_id: str | None = attempt_id
        while next_attempt_id is not None:
            next_attempt_id = self._run_attempt(
                job=job,
                attempt_id=next_attempt_id,
                trigger_message_id=trigger_message_id,
                renew_lease=renew_lease,
            )

    def _compose_request(
        self,
        *,
        job: chat.GenerationJob,
        attempt_id: str,
        trigger_message_id: str,
        hops: tuple[_Hop, ...],
        requested_at_text: str,
        deadline_at_text: str,
    ) -> tuple[ProviderInvocationRequest, Mapping[str, Any]]:
        """The provider request for one attempt and the manifest that accounts for it.

        With no hops this is an ordinary attempt's, as it always was. With hops it
        extends the same model-visible context by each hop's output and Direction, under
        a derived idempotency key (the provider must not mistake it for the request it
        continues) and caused by the Direction rather than by the trigger Message.
        """
        request_context = _provider_context(
            self.connection,
            workspace_id=self.workspace_id,
            conversation_id=job.conversation_id,
            branch_id=job.branch_id,
            trigger_message_id=trigger_message_id,
            blobs_root=self.blobs_root,
        )
        messages = (*request_context.messages, *_hop_messages(hops))
        if len(messages) > _MAX_REQUEST_MESSAGES:
            raise _DirectionRefusedError("rejected", "boundary_unavailable")
        connection_id, model_id = self._effective_route(job)
        response_format = _trigger_response_format(
            self.connection,
            workspace_id=self.workspace_id,
            trigger_message_id=trigger_message_id,
        )
        if hops and response_format.get("kind") == "structured":
            raise _DirectionRefusedError("rejected", "boundary_unavailable")
        request = ProviderInvocationRequest(
            invocation_id=_stable_id("provider-inv", f"{job.generation_job_id}:{attempt_id}"),
            workspace_id=self.workspace_id,
            conversation_id=job.conversation_id,
            job_id=job.generation_job_id,
            attempt_id=attempt_id,
            connection_id=connection_id,
            model_id=model_id,
            operation="language.stream",
            messages=messages,
            response_format=response_format,
            policy_ref=self.config.policy_ref,
            classification_ref=self.config.classification_ref,
            residency_ref=self.config.residency_ref,
            idempotency_key=(
                _stable_id("continuation-idem", f"{job.idempotency_key}:{attempt_id}")
                if hops
                else job.idempotency_key
            ),
            correlation_id=_stable_id("correlation", job.generation_job_id),
            deadline_at=deadline_at_text,
            requested_at=requested_at_text,
            causation_id=hops[-1].direction_id if hops else trigger_message_id,
        )
        manifest_body = _request_manifest_body(
            request=request,
            job=job,
            manifest_message_refs=request_context.manifest_message_refs,
            attachments=request_context.attachments,
            hops=hops,
        )
        return request, manifest_body

    def _run_attempt(
        self,
        *,
        job: chat.GenerationJob,
        attempt_id: str,
        trigger_message_id: str,
        renew_lease: bool,
    ) -> str | None:
        lease_expires_at_us = _now_us(self.clock) + self._lease_duration_us()
        requested_at = self.clock.wall_time()
        try:
            self._resolve_route(job)
            manifest_timestamps = _existing_manifest_timestamps(
                self.connection,
                workspace_id=self.workspace_id,
                generation_attempt_id=attempt_id,
            )
            hops = self._read_hops(job=job, attempt_id=attempt_id)
            if manifest_timestamps is None:
                requested_at_text = _timestamp(requested_at)
                deadline_at_text = _timestamp(
                    requested_at + timedelta(seconds=self.config.deadline_seconds)
                )
            else:
                requested_at_text, deadline_at_text = manifest_timestamps
            request, manifest_body = self._compose_request(
                job=job,
                attempt_id=attempt_id,
                trigger_message_id=trigger_message_id,
                hops=hops,
                requested_at_text=requested_at_text,
                deadline_at_text=deadline_at_text,
            )
        except AttachmentUnavailable:
            # Refused before any request existed: nothing was sent, and the lifecycle is
            # still a real terminal outcome rather than a submission that vanished.
            self._fail_generation(job.generation_job_id, attempt_id, error_code="invalid-request")
            return None
        except ProviderRouteUnavailable:
            # Never reached a provider at all. The lifecycle is still real -- claimed,
            # attempted, terminal -- which is what makes this a durable outcome rather
            # than a submission that vanished.
            self._fail_generation(
                job.generation_job_id, attempt_id, error_code="provider-unavailable"
            )
            return None
        except Exception:  # noqa: BLE001 - boundary failures are terminalized, never exposed
            self._fail_generation(
                job.generation_job_id, attempt_id, error_code="malformed-response"
            )
            return None

        invocation_id = request.invocation_id
        structured_response = request.response_format.get("kind") == "structured"
        try:
            chat.append_request_manifest_once(
                self.connection,
                self.identity,
                workspace_id=self.workspace_id,
                fencing_generation=self.fencing_generation,
                conversation_id=job.conversation_id,
                branch_id=job.branch_id,
                generation_job_id=job.generation_job_id,
                generation_attempt_id=attempt_id,
                trigger_message_id=trigger_message_id,
                provider_invocation_id=invocation_id,
                request_manifest_id=str(manifest_body["requestManifestId"]),
                idempotency_key=request.idempotency_key,
                manifest_body=manifest_body,
                created_at_us=_now_us(self.clock),
            )
        except chat.RequestManifestConflict as error:
            raise GenerationExecutorError("the provider request manifest conflicts") from error

        # What the earlier attempts of this Generation already wrote of its response: the
        # response bound is the Generation's, not each attempt's.
        prior_bytes = sum(len(hop.prior_text.encode("utf-8")) for hop in hops)
        continued_as: str | None = None
        text_chunks: list[str] = []
        reasoning_summaries: list[str] = []
        structured_outputs: list[chat.GenerationStructuredOutput] = []
        # C9: the step ordinal of the next tool call in THIS attempt. Per
        # attempt, not per job: `open_chat_turn` opens one turn per attempt, and
        # a step ordinal is a position within its own turn.
        tool_call_ordinal = 1
        expected_ordinal = 0
        transport_attempt_id: str | None = None
        saw_start = False
        saw_terminal = False
        heartbeat = self.heartbeat_factory(
            connection=self.connection,
            identity=self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
            generation_job_id=job.generation_job_id,
            generation_attempt_id=attempt_id,
            lease_owner=self.identity.service_instance_id,
            clock=self.clock,
            lease_duration_us=self._lease_duration_us(),
        )
        if renew_lease:
            heartbeat.start(lease_expires_at_us)
        durable_started = any(
            event.event_type == "chat.generation.started"
            and event.generation_attempt_id == attempt_id
            for event in chat.read_generation_events(
                self.connection,
                workspace_id=self.workspace_id,
                generation_job_id=job.generation_job_id,
            )
        )
        try:
            stream = self.invoke(request)
            for event in stream:
                # Before the event is durable, not after: the append itself requires an
                # unexpired lease, so a stream that paused past the renewal point would
                # otherwise fail on the very write that proves it is still alive.
                if renew_lease:
                    lease_expires_at_us = heartbeat.tick() or lease_expires_at_us
                event_type, expected_ordinal, transport_attempt_id = self._validate_event(
                    event,
                    invocation_id=invocation_id,
                    attempt_id=attempt_id,
                    transport_attempt_id=transport_attempt_id,
                    expected_ordinal=expected_ordinal,
                )
                if event_type == "stream-start":
                    if saw_start or expected_ordinal != 1:
                        raise GenerationExecutorError("the provider stream start is misplaced")
                    saw_start = True
                elif not saw_start:
                    raise GenerationExecutorError("the provider stream has no leading start event")
                if event_type in _TERMINAL_PROVIDER_EVENTS:
                    self._deny_unsettled_provider_calls(job.generation_job_id, attempt_id)
                if event_type == "text-delta":
                    if structured_response:
                        raise GenerationExecutorError(
                            "a structured provider response emitted text"
                        )
                    delta = event.get("delta")
                    if not isinstance(delta, str):
                        raise GenerationExecutorError("a provider text delta is malformed")
                    # Checked here as well as in the service so an oversized stream is a
                    # terminalized `malformed-response` rather than a lifecycle refusal
                    # that would leave this job durably running.
                    candidate = "".join([*text_chunks, delta])
                    if prior_bytes + len(candidate.encode("utf-8")) > MAX_RESPONSE_TEXT_BYTES:
                        raise GenerationExecutorError("the provider response exceeds message bounds")
                    # The chunk, its ordering event and the job's sequence commit
                    # together: a crash mid-delta leaves none of them rather than a
                    # chunk no event orders.
                    append_provider_text_delta(
                        self.connection,
                        self.identity,
                        workspace_id=self.workspace_id,
                        fencing_generation=self.fencing_generation,
                        generation_job_id=job.generation_job_id,
                        generation_attempt_id=attempt_id,
                        provider_event=event,
                        chunk_ordinal=len(text_chunks),
                        now_us=_now_us(self.clock),
                        prior_response_bytes=prior_bytes,
                    )
                    text_chunks.append(delta)
                elif event_type == "reasoning-summary":
                    summary = event.get("summary")
                    part_id = event.get("partId")
                    step_id = event.get("stepId")
                    summary_bytes: bytes | None = None
                    if isinstance(summary, str):
                        try:
                            summary_bytes = summary.encode("utf-8")
                        except UnicodeEncodeError:
                            pass
                    if (
                        not isinstance(summary, str)
                        or not summary
                        or "\x00" in summary
                        or len(summary) > 65_536
                        or summary_bytes is None
                        or len(summary_bytes) > MAX_REASONING_SUMMARY_BYTES
                        or not isinstance(part_id, str)
                        or _PROVIDER_ID.fullmatch(part_id) is None
                        or not isinstance(step_id, str)
                        or _PROVIDER_ID.fullmatch(step_id) is None
                    ):
                        raise GenerationExecutorError("a provider reasoning summary is malformed")
                    append_provider_reasoning_summary(
                        self.connection,
                        self.identity,
                        workspace_id=self.workspace_id,
                        fencing_generation=self.fencing_generation,
                        generation_job_id=job.generation_job_id,
                        generation_attempt_id=attempt_id,
                        provider_event=event,
                        summary_ordinal=len(reasoning_summaries),
                        now_us=_now_us(self.clock),
                        lease_duration_us=self._lease_duration_us(),
                    )
                    reasoning_summaries.append(summary)
                elif event_type == "structured-output" and structured_response:
                    # `structured-output` is also part of the existing open F2a
                    # event vocabulary for ordinary text/tool streams. Those
                    # events retain their pre-table behaviour and fall through
                    # to the identity-only generation-event projection below.
                    # Only an explicitly requested, allow-listed structured
                    # response enters the governed structured-output ledger.
                    if structured_outputs:
                        raise GenerationExecutorError(
                            "a structured provider response emitted more than one output"
                        )
                    try:
                        structured_outputs.append(
                            append_provider_structured_output(
                                self.connection,
                                self.identity,
                                workspace_id=self.workspace_id,
                                fencing_generation=self.fencing_generation,
                                generation_job_id=job.generation_job_id,
                                generation_attempt_id=attempt_id,
                                provider_event=event,
                                output_ordinal=0,
                                now_us=_now_us(self.clock),
                                lease_duration_us=self._lease_duration_us(),
                            )
                        )
                    except UnsupportedProviderEvent as error:
                        raise GenerationExecutorError(
                            "a provider structured output is malformed"
                        ) from error
                elif event_type == "tool-call":
                    # C9: the one non-text family with a durable home and a
                    # built consumer. Recorded BEFORE the stream goes on, so a
                    # crash mid-stream keeps what the provider already asked
                    # for -- the same guarantee `append_provider_text_delta`
                    # gives text, and the reason this is not assembled from
                    # memory at commit. A Direction continuation commits text
                    # read back from durable chunks across attempts, so an
                    # in-memory tool call would be lost while its own text
                    # survived.
                    self._record_tool_proposal(
                        job=job,
                        attempt_id=attempt_id,
                        event=event,
                        step_ordinal=tool_call_ordinal,
                    )
                    tool_call_ordinal += 1
                elif event_type == "tool-result":
                    self._record_provider_tool_result(
                        job=job,
                        attempt_id=attempt_id,
                        event=event,
                    )
                elif event_type == "finish" and event.get("finishReason") not in {
                    "error",
                    "cancelled",
                }:
                    content_filtered = event.get("finishReason") == GUARDRAIL_NOTICE_REASON
                    durable_chunks = chat.read_generation_text_chunks(
                        self.connection,
                        workspace_id=self.workspace_id,
                        generation_job_id=job.generation_job_id,
                        generation_attempt_id=attempt_id,
                    )
                    durable_outputs = chat.read_generation_structured_outputs(
                        self.connection,
                        workspace_id=self.workspace_id,
                        generation_job_id=job.generation_job_id,
                        generation_attempt_id=attempt_id,
                    )
                    # A successful stream must have produced SOMETHING this attempt:
                    # text, a durable tool call, or the provider's exact closed
                    # content-filter finish. The latter itself produces the fixed
                    # guardrail part; every other empty success remains malformed.
                    has_required_response = (
                        len(durable_outputs) == 1
                        if structured_response and not content_filtered
                        else bool(
                            durable_chunks
                            or self._attempt_tool_calls(job.generation_job_id, attempt_id)
                            or content_filtered
                        )
                    )
                    if (
                        not saw_start
                        or not has_required_response
                        or (structured_response and durable_chunks)
                        or (not structured_response and durable_outputs)
                        or len(durable_outputs) > 1
                    ):
                        raise GenerationExecutorError("a successful provider stream is incomplete")
                    # The one earliest-safe boundary: a provider iteration that completed,
                    # never a delta inside one. A Direction pending here is taken in and
                    # this Generation goes on as its successor attempt; otherwise the
                    # Generation settles, as it always did.
                    # A filtered boundary is final for this attempt: continuing it
                    # with a Direction would ask a second request to evade the exact
                    # provider decision the committed notice reports.
                    if not content_filtered and not structured_response:
                        continued_as = self._continue_with_direction(
                            job=job,
                            attempt_id=attempt_id,
                            hops=hops,
                            finish_event=event,
                            durable_chunks=durable_chunks,
                        )
                    if continued_as is None:
                        result_message_id = self._materialize_assistant(
                            job=job,
                            attempt_id=attempt_id,
                            text="".join(hop.prior_text for hop in hops)
                            + "".join(chunk.text_content for chunk in durable_chunks),
                            guardrail_notice=content_filtered,
                        )
                        append_provider_generation_event(
                            self.connection,
                            self.identity,
                            workspace_id=self.workspace_id,
                            fencing_generation=self.fencing_generation,
                            generation_job_id=job.generation_job_id,
                            generation_attempt_id=attempt_id,
                            provider_event=self._settling_finish(
                                job=job, attempt_id=attempt_id, finish_event=event
                            ),
                            result_message_id=result_message_id,
                            now_us=_now_us(self.clock),
                            lease_owner=self.identity.service_instance_id,
                        )
                elif event_type != "stream-start" or not durable_started:
                    append_provider_generation_event(
                        self.connection,
                        self.identity,
                        workspace_id=self.workspace_id,
                        fencing_generation=self.fencing_generation,
                        generation_job_id=job.generation_job_id,
                        generation_attempt_id=attempt_id,
                        provider_event=event,
                        now_us=_now_us(self.clock),
                        lease_owner=self.identity.service_instance_id,
                    )
                if event_type in _TERMINAL_PROVIDER_EVENTS:
                    saw_terminal = True
                    break
            if not saw_terminal:
                raise GenerationExecutorError("the provider stream ended without a terminal event")
        except GenerationTerminal:
            # The Generation ended under this stream (a stop, or another terminal write):
            # a Direction still aimed at it can never be applied, so it is closed before
            # the refusal goes on to the caller.
            self._close_unapplied_direction(job.generation_job_id)
            raise
        except GenerationLifecycleError:
            # Includes losing the lease to another instance. Terminalizing here would
            # write over a job this executor no longer owns.
            raise
        except AttachmentUnavailable:
            # A file withdrawn while the Generation was running, found when a Direction
            # continuation re-proved the request: no further request is sent for it.
            self._fail_generation(job.generation_job_id, attempt_id, error_code="invalid-request")
            return None
        except ProviderRouteUnavailable:
            self._fail_generation(
                job.generation_job_id, attempt_id, error_code="provider-unavailable"
            )
            return None
        except Exception:  # noqa: BLE001 - provider/iterator failures cross this boundary
            # A stream that started and then failed, or ended without a terminal event,
            # is a bad RESPONSE -- the provider was reached. `malformed-response`, never
            # `provider-unavailable`.
            self._fail_generation(
                job.generation_job_id, attempt_id, error_code="malformed-response"
            )
            return None
        finally:
            heartbeat.stop()
        if continued_as is None:
            # This Generation ended here, so a Direction that arrived after the boundary
            # was last read was never going to be applied.
            self._close_unapplied_direction(job.generation_job_id)
        return continued_as

    @staticmethod
    def _validate_event(
        event: Mapping[str, Any],
        *,
        invocation_id: str,
        attempt_id: str,
        transport_attempt_id: str | None,
        expected_ordinal: int,
    ) -> tuple[str, int, str]:
        """The event's type, the next ordinal, and the transport Attempt of this stream.

        An event's `attemptId` is the provider's transport Attempt (`ProviderAttemptId`),
        not the Chat Generation Attempt (`attempt_id`) the request named. It is an id of its
        own, never that one, and the same on every event of one stream: the first event
        fixes it (`transport_attempt_id` is `None` until then). Another stream, such as a
        same-route retry, may carry another.
        """
        if not isinstance(event, Mapping):
            raise GenerationExecutorError("a provider event is not a JSON object")
        event_type = event.get("eventType")
        if event_type not in F2A_PROVIDER_EVENT_TYPES:
            raise GenerationExecutorError("a provider event type is unsupported")
        transport = event.get("attemptId")
        if (
            not isinstance(transport, str)
            or not _TRANSPORT_ATTEMPT_ID.fullmatch(transport)
            or transport == attempt_id
            or (transport_attempt_id is not None and transport != transport_attempt_id)
        ):
            raise GenerationExecutorError("the provider event transport attempt is not this stream's")
        ordinal = event.get("ordinal")
        if (
            event.get("invocationId") != invocation_id
            or not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal != expected_ordinal
        ):
            raise GenerationExecutorError("the provider event stream is not contiguous")
        return str(event_type), expected_ordinal + 1, transport

    def _lease_duration_us(self) -> int:
        """A lease that outlives the invocation it is protecting."""
        deadline_us = (self.config.deadline_seconds + _LEASE_MARGIN_SECONDS) * 1_000_000
        return max(DEFAULT_LEASE_US, deadline_us)

    def _effective_route(self, job: chat.GenerationJob) -> tuple[str, str]:
        """The route this job runs on: the one pinned on the job (0074), else the
        executor's startup configuration for a legacy job that pinned none. Every
        attempt and continuation of the job resolves the same pair, because it is read
        from the job and the job's route is immutable.
        """
        if job.provider_connection_id is not None and job.provider_model_id is not None:
            return job.provider_connection_id, job.provider_model_id
        return self.config.connection_id, self.config.model_id

    def _resolve_route(self, job: chat.GenerationJob) -> None:
        """Refuse before invoking when the job's effective route cannot name a provider.

        Deliberately AFTER the claim, not before it. The owner's distinction is that a
        workspace with an executor installed but no reachable route must still produce
        a real lifecycle -- claimed, attempted, terminal -- rather than silently
        swallowing the submission. Refusing before mutation is the separate case where
        no executor boundary is installed at all, and that belongs to the handler.
        """
        if not all(self._effective_route(job)):
            raise ProviderRouteUnavailable("no provider route is configured")

    def _fail_generation(
        self, generation_job_id: str, attempt_id: str, *, error_code: str
    ) -> None:
        job = chat.read_generation_job(
            self.connection,
            workspace_id=self.workspace_id,
            generation_job_id=generation_job_id,
        )
        status = chat.read_generation_job_status_projection(
            self.connection,
            workspace_id=self.workspace_id,
            generation_job_id=generation_job_id,
        )
        if job is None:
            return
        if job.state != "running" and not (
            status is not None
            and status.state == "running"
            and status.current_attempt_id == attempt_id
        ):
            return
        self._deny_unsettled_provider_calls(generation_job_id, attempt_id)
        append_provider_generation_event(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
            generation_job_id=generation_job_id,
            generation_attempt_id=attempt_id,
            provider_event={
                "eventType": "error",
                "errorCode": error_code,
                # An unreachable route is worth retrying; a response this build cannot
                # read is not, because retrying reproduces it.
                "retryable": error_code == "provider-unavailable",
                "statusClass": "client_error" if error_code == "invalid-request" else "unknown",
                "safeMessage": _SAFE_MESSAGES[error_code],
            },
            now_us=_now_us(self.clock),
            lease_owner=self.identity.service_instance_id,
        )
        self._close_unapplied_direction(generation_job_id)

    # --- C6: a Direction taken in at an iteration boundary --------------------------

    def _attempt_text(self, *, job: chat.GenerationJob, attempt_id: str) -> str:
        return "".join(
            chunk.text_content
            for chunk in chat.read_generation_text_chunks(
                self.connection,
                workspace_id=self.workspace_id,
                generation_job_id=job.generation_job_id,
                generation_attempt_id=attempt_id,
            )
        )

    def _read_hops(self, *, job: chat.GenerationJob, attempt_id: str) -> tuple[_Hop, ...]:
        """The Directions already taken in on the way to `attempt_id`, oldest first.

        Empty for an attempt no Direction opened. Read from the durable rows alone, so a
        continuation attempt rebuilt after a crash is the one that was opened. Each hop is
        held to what was recorded about it: the continuation names this attempt and the
        Direction's target, and the prior output still hashes to the digest it recorded.
        """
        directions = chat.read_directions_for_generation_job(
            self.connection, workspace_id=self.workspace_id, generation_job_id=job.generation_job_id
        )
        opened_by = {
            direction.applied_attempt_id: direction
            for direction in directions
            if direction.state == "applied" and direction.applied_attempt_id is not None
        }
        chain: list[_Hop] = []
        current = attempt_id
        while current in opened_by:
            direction = opened_by[current]
            continuation = chat.read_generation_continuation_for_direction(
                self.connection, workspace_id=self.workspace_id, direction_id=direction.direction_id
            )
            texts = _direction_texts(direction)
            if (
                continuation is None
                or texts is None
                or continuation.next_attempt_id != current
                or continuation.prior_attempt_id != direction.target_attempt_id
                or len(chain) >= MAX_GENERATION_ATTEMPTS
            ):
                raise GenerationExecutorError("the continuation of this attempt cannot be read")
            prior_text = self._attempt_text(job=job, attempt_id=continuation.prior_attempt_id)
            if generated_text_digest(prior_text) != continuation.prior_output_digest:
                raise GenerationExecutorError("the output a continuation followed has changed")
            chain.append(
                _Hop(
                    continuation_id=continuation.continuation_id,
                    prior_attempt_id=continuation.prior_attempt_id,
                    next_attempt_id=current,
                    direction_id=direction.direction_id,
                    direction_digest=direction.content_digest,
                    direction_texts=texts,
                    provider_finish_id=continuation.provider_finish_id,
                    prior_output_digest=continuation.prior_output_digest,
                    prior_text=prior_text,
                )
            )
            current = direction.target_attempt_id
        chain.reverse()
        return tuple(chain)

    def _continued_attempt(
        self, job: chat.GenerationJob | None, first_attempt_id: str
    ) -> str | None:
        """The attempt a Generation has since continued on to, if its first one was continued.

        The claim of a queue row already submitted is a replay that insists on the first
        attempt, but a Generation steered since then is running on a successor. Only a
        running job whose current attempt is reached from the first one through applied
        Directions counts; anything else is the ordinary replay.
        """
        if job is None or job.state != "running":
            return None
        status = chat.read_generation_job_status_projection(
            self.connection, workspace_id=self.workspace_id, generation_job_id=job.generation_job_id
        )
        if (
            status is None
            or status.state != "running"
            or status.current_attempt_id in (None, first_attempt_id)
        ):
            return None
        hops = self._read_hops(job=job, attempt_id=str(status.current_attempt_id))
        if hops and hops[0].prior_attempt_id == first_attempt_id:
            return status.current_attempt_id
        return None

    def _trigger_actor(self, job: chat.GenerationJob) -> str | None:
        """The human who sent the Message this Generation answers: the only actor who steers it."""
        for message in chat.read_messages_by_conversation_sequence(
            self.connection, workspace_id=self.workspace_id, conversation_id=job.conversation_id
        ):
            if message.message_id == job.trigger_message_id:
                return message.author_id if message.author_type == "human" else None
        return None

    def _close_direction(
        self, *, job: chat.GenerationJob, direction: chat.Direction, state: str, code: str
    ) -> None:
        """End a pending Direction as never applied. A Direction that moved first stays as it moved."""
        with contextlib.suppress(DirectionNotPending):
            close_unapplied_direction(
                self.connection,
                self.identity,
                workspace_id=self.workspace_id,
                fencing_generation=self.fencing_generation,
                generation_job_id=job.generation_job_id,
                direction_id=direction.direction_id,
                state=state,
                code=code,
                now_us=_now_us(self.clock),
                lease_owner=self.identity.service_instance_id,
                lease_duration_us=self._lease_duration_us(),
            )

    def _close_unapplied_direction(self, generation_job_id: str) -> None:
        """Close a Direction still pending on a Generation that is no longer running.

        The Generation can only have ended or paused for a retry, and either way the
        Direction can never be applied. It is closed with the pair that says which.
        Nothing happens while the Generation runs. A Direction that cannot be read safely
        is left exactly as it is: it is never guessed at, and it is not this path's place
        to turn a finished Generation into an error.
        """
        status = chat.read_generation_job_status_projection(
            self.connection, workspace_id=self.workspace_id, generation_job_id=generation_job_id
        )
        closing = None if status is None else _UNAPPLIED_OF_JOB_STATE.get(status.state)
        if closing is None:
            return
        with contextlib.suppress(StorageError, DirectionNotPending):
            pending = chat.read_pending_direction(
                self.connection, workspace_id=self.workspace_id, generation_job_id=generation_job_id
            )
            if pending is not None:
                close_unapplied_direction(
                    self.connection,
                    self.identity,
                    workspace_id=self.workspace_id,
                    fencing_generation=self.fencing_generation,
                    generation_job_id=generation_job_id,
                    direction_id=pending.direction_id,
                    state=closing[0],
                    code=closing[1],
                    now_us=_now_us(self.clock),
                    lease_owner=self.identity.service_instance_id,
                    lease_duration_us=self._lease_duration_us(),
                )

    def _boundary_direction(
        self, *, job: chat.GenerationJob, attempt_id: str
    ) -> chat.Direction | None:
        """The Direction to take in at this attempt's iteration boundary, re-proved, or `None`.

        A Direction already applied at this boundary is returned first, so a replay resumes
        the continuation it wrote rather than looking for a new one. Otherwise the job's one
        pending Direction, and only if everything about it still holds: this attempt is the
        one running, it is aimed at this attempt, its actor is the one the Generation
        answers, and its stored content is the contract's. One that is not applicable is
        closed with the reason it is not and not applied; the Generation settles without it.
        """
        directions = chat.read_directions_for_generation_job(
            self.connection, workspace_id=self.workspace_id, generation_job_id=job.generation_job_id
        )
        for direction in directions:
            if direction.state == "applied" and direction.target_attempt_id == attempt_id:
                return direction
        pending = next((d for d in directions if d.state == "pending"), None)
        if pending is None:
            return None
        status = chat.read_generation_job_status_projection(
            self.connection, workspace_id=self.workspace_id, generation_job_id=job.generation_job_id
        )
        if status is not None and status.state in {"succeeded", "failed", "cancelled"}:
            # Stopped (or ended) under a Direction waiting for this boundary: there is
            # nothing to settle, so no Message is written for a Generation that is over.
            raise GenerationTerminal(f"generation job {job.generation_job_id!r} already ended")
        if status is None or status.state != "running" or status.current_attempt_id != attempt_id:
            # This attempt no longer owns the Generation. Settling it is refused on its own
            # terms; whatever the Direction becomes is decided by whoever does own it.
            return None
        if (
            pending.generation_job_id != job.generation_job_id
            or pending.conversation_id != job.conversation_id
            or pending.branch_id != job.branch_id
            or pending.requested_boundary != "earliest_safe"
        ):
            raise GenerationConflict(
                f"direction {pending.direction_id!r} does not belong to this generation"
            )
        refusal: tuple[str, str] | None = None
        if pending.target_attempt_id != attempt_id:
            refusal = ("rejected", "target_attempt_ended")
        elif pending.actor_id != self._trigger_actor(job) or _direction_texts(pending) is None:
            refusal = ("rejected", "policy_refused")
        if refusal is not None:
            self._close_direction(job=job, direction=pending, state=refusal[0], code=refusal[1])
            return None
        return pending

    def _plan_continuation(
        self,
        *,
        job: chat.GenerationJob,
        attempt_id: str,
        hops: tuple[_Hop, ...],
        direction: chat.Direction,
        prior_text: str,
        prior_digest: str,
        finish_id: str,
    ) -> tuple[ContinuationIdentities, Mapping[str, Any]]:
        """The identities and request manifest of the continuation that takes `direction` in.

        Pure of writes. Raises :class:`_DirectionRefusedError` for a boundary that cannot be
        continued from: no room left in the response the Generation may produce, or a
        request the provider contract would refuse. Whether an Attempt or event position is
        left to open the successor in is the service's to decide, with the rows it reads.
        """
        written = sum(len(hop.prior_text.encode("utf-8")) for hop in hops)
        written += len(prior_text.encode("utf-8"))
        if written >= MAX_RESPONSE_TEXT_BYTES:
            raise _DirectionRefusedError("rejected", "boundary_unavailable")
        texts = _direction_texts(direction)
        if texts is None:  # re-proved by the caller; this guards against a row that changed
            raise _DirectionRefusedError("rejected", "policy_refused")
        # This attempt completed the iteration at its depth in the chain; the Direction
        # takes effect at the next one.
        identities = continuation_identities(
            generation_job_id=job.generation_job_id,
            direction_id=direction.direction_id,
            iteration=len(hops) + 1,
        )
        if direction.state == "applied" and direction.applied_iteration != len(hops) + 1:
            raise GenerationConflict(
                f"direction {direction.direction_id!r} was applied at another iteration"
            )
        hop = _Hop(
            continuation_id=identities.continuation_id,
            prior_attempt_id=attempt_id,
            next_attempt_id=identities.generation_attempt_id,
            direction_id=direction.direction_id,
            direction_digest=direction.content_digest,
            direction_texts=texts,
            provider_finish_id=finish_id,
            prior_output_digest=prior_digest,
            prior_text=prior_text,
        )
        timestamps = _existing_manifest_timestamps(
            self.connection,
            workspace_id=self.workspace_id,
            generation_attempt_id=identities.generation_attempt_id,
        )
        if timestamps is None:
            requested_at = self.clock.wall_time()
            timestamps = (
                _timestamp(requested_at),
                _timestamp(requested_at + timedelta(seconds=self.config.deadline_seconds)),
            )
        _, manifest_body = self._compose_request(
            job=job,
            attempt_id=identities.generation_attempt_id,
            trigger_message_id=job.trigger_message_id,
            hops=(*hops, hop),
            requested_at_text=timestamps[0],
            deadline_at_text=timestamps[1],
        )
        return identities, manifest_body

    def _continue_with_direction(
        self,
        *,
        job: chat.GenerationJob,
        attempt_id: str,
        hops: tuple[_Hop, ...],
        finish_event: Mapping[str, Any],
        durable_chunks: tuple[chat.GenerationTextChunk, ...],
    ) -> str | None:
        """Take the Direction pending at this completed iteration in, or return `None`.

        Two commits, in order, and the provider is invoked for the successor only after the
        second. Phase A closes the iteration and stages the successor
        (:func:`stage_direction_continuation`); phase B applies the Direction to it and moves
        the Generation onto it (:func:`apply_direction_continuation`). The successor
        attempt's id is returned for the caller to run. `None` means no Direction applies
        here and the Generation settles: none pending, or the one pending was closed
        unapplied. Both phases are idempotent on the durable rows, so a replay of this
        boundary (after a crash between them, or by a second worker) writes nothing twice and
        returns the successor already opened. A Direction that changed under either phase
        (replaced between the read and the commit) is read again, a bounded number of times,
        each fully re-proved.
        """
        for _ in range(_MAX_DIRECTION_TRIES):
            direction = self._boundary_direction(job=job, attempt_id=attempt_id)
            if direction is None:
                return None
            prior_text = "".join(chunk.text_content for chunk in durable_chunks)
            prior_digest = generated_text_digest(prior_text)
            finish_id = provider_finish_id(finish_event)
            refused: tuple[str, str] | None = None
            try:
                identities, manifest_body = self._plan_continuation(
                    job=job,
                    attempt_id=attempt_id,
                    hops=hops,
                    direction=direction,
                    prior_text=prior_text,
                    prior_digest=prior_digest,
                    finish_id=finish_id,
                )
                stage_direction_continuation(
                    self.connection,
                    self.identity,
                    workspace_id=self.workspace_id,
                    fencing_generation=self.fencing_generation,
                    generation_job_id=job.generation_job_id,
                    prior_attempt_id=attempt_id,
                    direction_id=direction.direction_id,
                    actor_id=direction.actor_id,
                    identities=identities,
                    manifest_body=manifest_body,
                    provider_event=finish_event,
                    prior_output_digest=prior_digest,
                    lease_owner=self.identity.service_instance_id,
                    now_us=_now_us(self.clock),
                    lease_duration_us=self._lease_duration_us(),
                )
                status = chat.read_generation_job_status_projection(
                    self.connection,
                    workspace_id=self.workspace_id,
                    generation_job_id=job.generation_job_id,
                )
                if status is None:
                    raise GenerationNotFound(
                        f"generation job {job.generation_job_id!r} is not in this workspace"
                    )
                applied = apply_direction_continuation(
                    self.connection,
                    self.identity,
                    workspace_id=self.workspace_id,
                    fencing_generation=self.fencing_generation,
                    generation_job_id=job.generation_job_id,
                    prior_attempt_id=attempt_id,
                    direction_id=direction.direction_id,
                    actor_id=direction.actor_id,
                    identities=identities,
                    manifest_body=manifest_body,
                    expected_status_version=status.version,
                    lease_owner=self.identity.service_instance_id,
                    now_us=_now_us(self.clock),
                    lease_duration_us=self._lease_duration_us(),
                )
            except _DirectionRefusedError as error:
                refused = (error.state, error.code)
            except BoundaryUnavailable:
                refused = ("rejected", "boundary_unavailable")
            except (DirectionNotPending, StaleGenerationVersion):
                continue
            if refused is not None:
                self._close_direction(
                    job=job, direction=direction, state=refused[0], code=refused[1]
                )
                return None
            return applied.attempt.generation_attempt_id
        raise GenerationConflict(
            f"the direction pending on generation job {job.generation_job_id!r} kept changing"
        )

    def _settling_finish(
        self, *, job: chat.GenerationJob, attempt_id: str, finish_event: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """The finish event as the Generation settles on it.

        An iteration closed on this very finish and then not continued (the Direction it
        was closed for ended before it could be applied) already holds the finish's
        provider event id, and a durable provider event id belongs to one event. The
        settling event is then the same finish without it, so the Generation ends rather
        than waiting on an id it can no longer use.
        """
        closings = [
            event
            for event in chat.read_generation_events(
                self.connection,
                workspace_id=self.workspace_id,
                generation_job_id=job.generation_job_id,
            )
            if event.generation_attempt_id == attempt_id
            and event.event_type == "chat.generation.iteration_completed"
        ]
        if not closings:
            return finish_event
        finish_id = provider_finish_id(finish_event)
        if all(event.provider_event_id != finish_id for event in closings):
            return finish_event
        return {key: value for key, value in finish_event.items() if key != "providerEventId"}

    def _attempt_tool_calls(
        self, generation_job_id: str, attempt_id: str
    ) -> tuple[chat.ChatToolCall, ...]:
        """The durable tool calls THIS attempt recorded, not the whole job's."""
        return tuple(
            call
            for call in chat.read_chat_tool_calls_for_generation_job(
                self.connection,
                workspace_id=self.workspace_id,
                generation_job_id=generation_job_id,
            )
            if call.generation_attempt_id == attempt_id
        )

    def _record_tool_proposal(
        self,
        *,
        job: chat.GenerationJob,
        attempt_id: str,
        event: Mapping[str, Any],
        step_ordinal: int,
    ) -> None:
        """Make one provider `tool-call` event durable pending a result or terminal.

        A canonical F2a `tool-result` may follow when the provider itself ran
        the operation. Until then this is only a proposal. The terminal boundary
        denies any call still unresolved, preserving the old no-execution truth
        for OpenAI-style function calls that expect the application to run them.

        These provider-reported calls write no review-lifecycle row, so they do
        not enter C8 Review or open a wait. A crash leaves the proposal durable;
        replaying the same stream either settles the same result or denies it at
        the terminal boundary.

        THE ARGUMENTS ARE RECORDED, NOT PUBLISHED. `proposed_arguments` is what
        the ledger is for and it is digested there; the `tool.call` message part
        carries no arguments at all (see `chat_part_registry`), so nothing
        outbound discloses them.

        Idempotent through stable ids: a redelivered provider stream recomputes
        the same turn, step and tool-call ids, `record_tool_proposal` returns the
        existing row for an identical proposal, and `deny_tool_call` returns the
        existing denial. A replay therefore converges instead of conflicting. The
        same `(attempt, stepId, toolCallId)` with other arguments is a conflict
        and fails the generation; the durable row is never rewritten.
        """
        provider_call_id = event.get("toolCallId")
        provider_step_id = event.get("stepId")
        tool_name = event.get("toolName")
        arguments = event.get("input")
        if (
            not isinstance(provider_call_id, str)
            or not _PROVIDER_ID.fullmatch(provider_call_id)
            or not isinstance(provider_step_id, str)
            or not _PROVIDER_ID.fullmatch(provider_step_id)
            or not isinstance(tool_name, str)
            or not tool_name
            or not isinstance(arguments, Mapping)
        ):
            raise GenerationExecutorError("a provider tool call is malformed")
        now_us = _now_us(self.clock)
        # One turn per generation ATTEMPT, which is what `open_chat_turn` itself
        # states and keys on; one step per CALL, because the turn-step table is
        # keyed by step and two calls sharing a provider step would collide.
        turn = open_chat_turn(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
            generation_job_id=job.generation_job_id,
            generation_attempt_id=attempt_id,
            turn_id=_stable_id("turn", attempt_id),
            now_us=now_us,
        )
        # The F2a identity is the whole tuple: a provider may reuse one
        # `toolCallId` on distinct steps, and keying on the call id alone would
        # make the second a conflicting proposal. JSON, not a delimiter join,
        # so no id can be crafted to collide with another tuple.
        tool_call_id = _stable_id(
            "toolcall", json.dumps([attempt_id, provider_step_id, provider_call_id])
        )
        record_tool_proposal(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
            turn_id=turn.turn_id,
            step_id=_stable_id("step", tool_call_id),
            step_ordinal=step_ordinal,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_version=PROVIDER_REPORTED_TOOL_VERSION,
            registry_ref=PROVIDER_REPORTED_REGISTRY_REF,
            proposed_arguments=dict(arguments),
            now_us=now_us,
        )

    def _record_provider_tool_result(
        self,
        *,
        job: chat.GenerationJob,
        attempt_id: str,
        event: Mapping[str, Any],
    ) -> None:
        """Settle only the exact call identity named by a bounded F2a result."""
        provider_call_id = event.get("toolCallId")
        provider_step_id = event.get("stepId")
        output = event.get("output")
        is_error = event.get("isError", False)
        if (
            not isinstance(provider_call_id, str)
            or not _PROVIDER_ID.fullmatch(provider_call_id)
            or not isinstance(provider_step_id, str)
            or not _PROVIDER_ID.fullmatch(provider_step_id)
            or not isinstance(output, Mapping)
            or not isinstance(is_error, bool)
        ):
            raise GenerationExecutorError("a provider tool result is malformed")
        try:
            output_document = to_canonical_json(dict(output)).encode("utf-8")
        except Exception as error:
            raise GenerationExecutorError("a provider tool result is malformed") from error
        if len(output_document) > MAX_PROVIDER_TOOL_RESULT_BYTES:
            raise GenerationExecutorError("a provider tool result exceeds message bounds")
        tool_call_id = _stable_id(
            "toolcall", json.dumps([attempt_id, provider_step_id, provider_call_id])
        )
        try:
            record_provider_tool_result(
                self.connection,
                self.identity,
                workspace_id=self.workspace_id,
                fencing_generation=self.fencing_generation,
                tool_call_id=tool_call_id,
                result_payload=dict(output),
                is_error=is_error,
                now_us=_now_us(self.clock),
            )
        except Exception as error:
            raise GenerationExecutorError("a provider tool result is unmatched or unsafe") from error

    def _deny_unsettled_provider_calls(self, generation_job_id: str, attempt_id: str) -> None:
        """Close every call that reached a terminal stream without a result."""
        for call in self._attempt_tool_calls(generation_job_id, attempt_id):
            if call.state == "proposed":
                deny_tool_call(
                    self.connection,
                    self.identity,
                    workspace_id=self.workspace_id,
                    fencing_generation=self.fencing_generation,
                    tool_call_id=call.tool_call_id,
                    failure_code=NO_EXECUTION_PATH,
                    now_us=_now_us(self.clock),
                )
            elif call.state not in {"succeeded", "failed", "denied", "cancelled"}:
                raise GenerationExecutorError("a provider tool lifecycle ended without a result")

    def _materialize_assistant(
        self,
        *,
        job: chat.GenerationJob,
        attempt_id: str,
        text: str,
        guardrail_notice: bool = False,
    ) -> str:
        message_id = _stable_id("assistant", job.generation_job_id)
        # C9: read BEFORE the hash, because the hash is over the whole part
        # list. Read by job, not attempt: `text` above is assembled across
        # every hop of this Generation, so the parts beside it must span the
        # same attempts or the Message would state only its last hop's calls.
        tool_calls = chat.read_chat_tool_calls_for_generation_job(
            self.connection,
            workspace_id=self.workspace_id,
            generation_job_id=job.generation_job_id,
        )
        reasoning_summaries = chat.read_generation_reasoning_summaries(
            self.connection,
            workspace_id=self.workspace_id,
            generation_job_id=job.generation_job_id,
        )
        structured_outputs = chat.read_generation_structured_outputs(
            self.connection,
            workspace_id=self.workspace_id,
            generation_job_id=job.generation_job_id,
            generation_attempt_id=attempt_id,
        )
        message_hash, part_hashes = _message_hash(
            text,
            tool_calls,
            reasoning_summaries,
            guardrail_notice,
            structured_outputs,
        )
        existing = self.connection.execute(
            "SELECT content_hash FROM omnivia_chat_messages "
            "WHERE workspace_id = ? AND message_id = ?",
            (self.workspace_id, message_id),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != message_hash:
                raise GenerationExecutorError("the generation result conflicts with durable content")
            return message_id

        conversation = chat.read_conversation(
            self.connection,
            workspace_id=self.workspace_id,
            conversation_id=job.conversation_id,
        )
        branch = chat.read_branch(
            self.connection, workspace_id=self.workspace_id, branch_id=job.branch_id
        )
        if conversation is None or branch is None:
            raise GenerationExecutorError("the generation conversation is unavailable")
        if branch.current_head_message_id != job.trigger_message_id:
            raise GenerationExecutorError("the generation branch head changed before settlement")
        now_us = _now_us(self.clock)
        sequence = conversation.latest_conversation_sequence + 1
        head_version = branch.head_version + 1
        command_id = _stable_id("gencommand", job.generation_job_id)
        with chat.chat_writer(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ) as writer:
            writer.update_conversation(
                conversation_id=job.conversation_id,
                expected_graph_revision=conversation.graph_revision,
                graph_revision=conversation.graph_revision,
                latest_conversation_sequence=sequence,
                state=conversation.state,
                title=conversation.title,
                title_source=conversation.title_source,
                default_branch_id=conversation.default_branch_id,
                updated_at_us=now_us,
                archived_at_us=conversation.archived_at_us,
                tombstoned_at_us=conversation.tombstoned_at_us,
            )
            writer.append_message(
                message_id=message_id,
                conversation_id=job.conversation_id,
                parent_message_id=job.trigger_message_id,
                role="assistant",
                author_type="provider",
                author_id=self.config.service_actor_id,
                conversation_sequence=sequence,
                schema_version=1,
                content_hash=message_hash,
                completion_status="complete",
                visibility="standard",
                created_on_branch_id=job.branch_id,
                generation_job_id=job.generation_job_id,
                created_at_us=now_us,
                committed_at_us=now_us,
            )
            # One loop over the ordered part list the hash was computed from.
            # An explicit reasoning summary (if any) precedes answer text, the
            # Core-authored guardrail notice, and tool parts in ledger order. Every
            # typed payload is validated against the registry before it is written,
            # so a shape a family does not state -- the tool's arguments above all --
            # cannot reach a committed Message even by mistake.
            tool_calls_by_index = iter(tool_calls)
            for index, (part_type, payload) in enumerate(
                _message_parts(
                    text,
                    tool_calls,
                    reasoning_summaries,
                    guardrail_notice,
                    structured_outputs,
                )
            ):
                if part_type in {TOOL_CALL_PART_TYPE, WEB_SEARCH_PART_TYPE}:
                    validate_registered_payload(part_type, payload)
                    part_id = _stable_id("assistant-tool-part", next(tool_calls_by_index).tool_call_id)
                elif part_type == REASONING_SUMMARY_PART_TYPE:
                    validate_registered_payload(part_type, payload)
                    part_id = _stable_id("assistant-reasoning-part", job.generation_job_id)
                elif part_type == GUARDRAIL_NOTICE_PART_TYPE:
                    validate_registered_payload(part_type, payload)
                    part_id = _stable_id("assistant-guardrail-part", job.generation_job_id)
                elif part_type == RICH_PROSE_PART_TYPE:
                    validate_registered_payload(part_type, payload)
                    part_id = _stable_id("assistant-table-part", job.generation_job_id)
                elif part_type == METRIC_NUMBER_PART_TYPE:
                    validate_registered_payload(part_type, payload)
                    part_id = _stable_id("assistant-metric-part", job.generation_job_id)
                else:
                    part_id = _stable_id("assistant-part", job.generation_job_id)
                writer.append_message_part(
                    part_id=part_id,
                    message_id=message_id,
                    conversation_id=job.conversation_id,
                    part_index=index,
                    part_type=part_type,
                    schema_version=1,
                    visibility="standard",
                    payload=payload,
                    provenance="ai",
                    content_hash=part_hashes[index],
                    created_at_us=now_us,
                )
            # C10 RegenerateResponse: the `regeneration` relation, written
            # here because this is the first moment both of its ends exist.
            # The command that opened this job could not write it -- the
            # derived message did not exist yet -- so it recorded the
            # provenance on the branch and this reads it back.
            #
            # KEYED TO THIS JOB, NOT THE BRANCH'S LIFETIME. `origin_kind` and
            # `fork_source_message_id` are immutable and stay true forever, so
            # testing them alone made EVERY generation the branch ever ran
            # write another `(original -> message, regeneration)` row. Continue
            # the conversation normally on a regenerated branch -- the expected
            # thing to do with it -- and turn two, three and four each append a
            # false edge claiming to be a regeneration of the original. The
            # derivations primary key does not catch it because the derived id
            # differs every time, and the table forbids UPDATE and DELETE, so
            # the corruption is silent and permanent.
            #
            # The regeneration's own job is the one whose trigger is the branch
            # head the fork started from: `_regenerate_response` opens it with
            # `trigger_message_id` and `initial_head_message_id` both set to the
            # shared user parent. A later send on the same branch triggers off
            # its own new user message, so it fails this test. A RETRY of the
            # regeneration reuses the job id and never reaches here at all.
            if (
                branch is not None
                and branch.origin_kind == "assistant_regeneration"
                and branch.fork_source_message_id is not None
                and job.trigger_message_id == branch.initial_head_message_id
            ):
                writer.append_message_derivation(
                    conversation_id=job.conversation_id,
                    source_message_id=branch.fork_source_message_id,
                    derived_message_id=message_id,
                    derivation_kind="regeneration",
                    created_by_actor_id=self.config.service_actor_id,
                    created_at_us=now_us,
                )
            writer.append_branch_head_event(
                event_id=_stable_id("assistant-head", job.generation_job_id),
                conversation_id=job.conversation_id,
                branch_id=job.branch_id,
                head_version=head_version,
                previous_head_message_id=job.trigger_message_id,
                new_head_message_id=message_id,
                cause="assistant_message_materialised",
                command_id=command_id,
                graph_revision=conversation.graph_revision,
                conversation_sequence=sequence,
                actor_id=self.config.service_actor_id,
                occurred_at_us=now_us,
                schema_version=1,
            )
            writer.update_branch_head(
                branch_id=job.branch_id,
                expected_head_version=branch.head_version,
                head_version=head_version,
                current_head_message_id=message_id,
                state=branch.state,
                archived_at_us=branch.archived_at_us,
                tombstoned_at_us=branch.tombstoned_at_us,
            )
            writer.append_outbox_entry(
                outbox_cursor=_next_outbox_cursor(self.connection, self.workspace_id),
                domain_event_id=_stable_id("assistant-event", job.generation_job_id),
                event_kind="chat.message.committed",
                conversation_id=job.conversation_id,
                generation_job_id=job.generation_job_id,
                payload={
                    "conversationId": job.conversation_id,
                    "branchId": job.branch_id,
                    "messageId": message_id,
                    "parentMessageId": job.trigger_message_id,
                    "conversationSequence": sequence,
                    "graphRevision": conversation.graph_revision,
                    "headVersion": head_version,
                    "role": "assistant",
                    "generationJobId": job.generation_job_id,
                },
                created_at_us=now_us,
            )
        return message_id
