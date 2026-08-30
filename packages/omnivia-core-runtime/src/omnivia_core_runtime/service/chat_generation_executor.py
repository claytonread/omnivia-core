"""Core-owned execution of one durable Chat generation queue item.

The executor is deliberately provider-neutral.  Its injected ``invoke`` boundary
accepts the governed F2a :class:`ProviderInvocationRequest` and yields normalized
F2a event mappings; credentials, SDK objects and endpoints remain outside Core.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from omnivia_core.chat_contract.v1 import ProviderInvocationRequest, to_canonical_json
from omnivia_core.chat_contract.v1.generated import F2A_PROVIDER_EVENT_TYPES
from omnivia_core_runtime.ownership.identity import Clock, ServiceInstanceIdentity
from omnivia_core_runtime.service.chat_compaction import derive_model_visible_context
from omnivia_core_runtime.service.chat_generation import (
    DEFAULT_LEASE_US,
    GenerationHeartbeat,
    GenerationLifecycleError,
    append_provider_generation_event,
    claim_queued_generation,
)
from omnivia_core_runtime.storage import chat

__all__ = [
    "ChatGenerationExecutor",
    "GenerationExecutorConfig",
    "GenerationExecutorError",
    "ProviderRouteUnavailable",
    "ProviderStream",
]

ProviderStream = Callable[[ProviderInvocationRequest], Iterable[Mapping[str, Any]]]
_MAX_TEXT_BYTES: Final = 262_144
#: Head-room between an invocation's deadline and the lease that must outlive it.
#: A lease shorter than the deadline expires while the executor is still legitimately
#: waiting, so the claim is sized from the deadline rather than left at the default.
_LEASE_MARGIN_SECONDS: Final = 30
#: Sanitized, caller-safe text per terminal outcome. Never provider content.
_SAFE_MESSAGES: Final = {
    "provider-unavailable": "no provider route was reachable for this generation",
    "malformed-response": "the provider stream could not be completed safely",
}
_TERMINAL_PROVIDER_EVENTS: Final = frozenset({"finish", "error"})
_PROMPT_BUNDLE: Final = {
    "id": "core.chat.default-model-visible-context",
    "version": 1,
    "kind": "message-lineage",
}


class GenerationExecutorError(Exception):
    """A sanitized executor refusal; provider content is never included."""


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


def _provider_context(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    branch_id: str,
    trigger_message_id: str,
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
    return _ProviderContext(tuple(provider_messages), tuple(manifest_refs))


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


def _request_manifest_body(
    *,
    request: ProviderInvocationRequest,
    job: chat.GenerationJob,
    manifest_message_refs: tuple[Mapping[str, Any], ...],
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
    prompt_bundle_digest = _digest_json(_PROMPT_BUNDLE)
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
            **_PROMPT_BUNDLE,
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
            "attachments": [],
            "evidence": [],
        },
        "retention": {
            "class": "chat_request_manifest_v1",
            "export": "metadata_and_hashes",
        },
    }
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


def _message_hash(text: str) -> tuple[str, str]:
    payload_document = to_canonical_json({"text": text})
    part_hash = "sha256:" + hashlib.sha256(payload_document.encode("utf-8")).hexdigest()
    message_document = json.dumps(
        [{"payload": {"text": text}, "type": "text", "visibility": "standard"}],
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    message_hash = "sha256:" + hashlib.sha256(message_document.encode("utf-8")).hexdigest()
    return message_hash, part_hash


def _text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


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
        raise GenerationExecutorError("no executable chat generation target was supplied")

    def execute_next(self) -> str | None:
        """Execute the next queued workspace item, or return ``None`` when idle."""
        submission = chat.read_next_queued_submission(
            self.connection, workspace_id=self.workspace_id
        )
        if submission is None:
            return None
        # SubmitMessage creates the queued job in the same transaction as this row.
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
            return
        attempt_id = _stable_id("genatt", generation_job_id)
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

    def execute_generation_attempt(
        self, *, generation_job_id: str, generation_attempt_id: str
    ) -> None:
        """Invoke the provider for an already-opened retry attempt."""
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
        lease_expires_at_us = _now_us(self.clock) + self._lease_duration_us()
        invocation_id = _stable_id(
            "provider-inv", f"{job.generation_job_id}:{attempt_id}"
        )
        requested_at = self.clock.wall_time()
        try:
            self._resolve_route()
            manifest_timestamps = _existing_manifest_timestamps(
                self.connection,
                workspace_id=self.workspace_id,
                generation_attempt_id=attempt_id,
            )
            request_context = _provider_context(
                self.connection,
                workspace_id=self.workspace_id,
                conversation_id=job.conversation_id,
                branch_id=job.branch_id,
                trigger_message_id=trigger_message_id,
            )
            if manifest_timestamps is None:
                requested_at_text = _timestamp(requested_at)
                deadline_at_text = _timestamp(
                    requested_at + timedelta(seconds=self.config.deadline_seconds)
                )
            else:
                requested_at_text, deadline_at_text = manifest_timestamps
            request = ProviderInvocationRequest(
                invocation_id=invocation_id,
                workspace_id=self.workspace_id,
                conversation_id=job.conversation_id,
                job_id=job.generation_job_id,
                attempt_id=attempt_id,
                connection_id=self.config.connection_id,
                model_id=self.config.model_id,
                operation="language.stream",
                messages=request_context.messages,
                response_format={"kind": "text"},
                policy_ref=self.config.policy_ref,
                classification_ref=self.config.classification_ref,
                residency_ref=self.config.residency_ref,
                idempotency_key=job.idempotency_key,
                correlation_id=_stable_id("correlation", job.generation_job_id),
                deadline_at=deadline_at_text,
                requested_at=requested_at_text,
                causation_id=trigger_message_id,
            )
            manifest_body = _request_manifest_body(
                request=request,
                job=job,
                manifest_message_refs=request_context.manifest_message_refs,
            )
        except ProviderRouteUnavailable:
            # Never reached a provider at all. The lifecycle is still real -- claimed,
            # attempted, terminal -- which is what makes this a durable outcome rather
            # than a submission that vanished.
            self._fail_generation(
                job.generation_job_id, attempt_id, error_code="provider-unavailable"
            )
            return
        except Exception:  # noqa: BLE001 - boundary failures are terminalized, never exposed
            self._fail_generation(
                job.generation_job_id, attempt_id, error_code="malformed-response"
            )
            return

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
                idempotency_key=job.idempotency_key,
                manifest_body=manifest_body,
                created_at_us=_now_us(self.clock),
            )
        except chat.RequestManifestConflict as error:
            raise GenerationExecutorError("the provider request manifest conflicts") from error

        text_chunks: list[str] = []
        expected_ordinal = 0
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
                event_type, expected_ordinal = self._validate_event(
                    event,
                    invocation_id=invocation_id,
                    attempt_id=attempt_id,
                    expected_ordinal=expected_ordinal,
                )
                if event_type == "stream-start":
                    if saw_start or expected_ordinal != 1:
                        raise GenerationExecutorError("the provider stream start is misplaced")
                    saw_start = True
                elif not saw_start:
                    raise GenerationExecutorError("the provider stream has no leading start event")
                if event_type == "text-delta":
                    delta = event.get("delta")
                    if not isinstance(delta, str):
                        raise GenerationExecutorError("a provider text delta is malformed")
                    candidate = "".join([*text_chunks, delta])
                    if len(candidate.encode("utf-8")) > _MAX_TEXT_BYTES:
                        raise GenerationExecutorError("the provider response exceeds message bounds")
                    with chat.chat_writer(
                        self.connection,
                        self.identity,
                        workspace_id=self.workspace_id,
                        fencing_generation=self.fencing_generation,
                    ) as writer:
                        writer.append_generation_text_chunk(
                            conversation_id=job.conversation_id,
                            generation_job_id=job.generation_job_id,
                            generation_attempt_id=attempt_id,
                            chunk_ordinal=len(text_chunks),
                            provider_event_id=(
                                str(event["providerEventId"])
                                if isinstance(event.get("providerEventId"), str)
                                else None
                            ),
                            text_content=delta,
                            content_hash=_text_hash(delta),
                            occurred_at_us=_now_us(self.clock),
                        )
                    text_chunks.append(delta)
                if event_type == "finish" and event.get("finishReason") not in {
                    "error",
                    "cancelled",
                }:
                    durable_chunks = chat.read_generation_text_chunks(
                        self.connection,
                        workspace_id=self.workspace_id,
                        generation_job_id=job.generation_job_id,
                        generation_attempt_id=attempt_id,
                    )
                    if not saw_start or not durable_chunks:
                        raise GenerationExecutorError("a successful provider stream is incomplete")
                    result_message_id = self._materialize_assistant(
                        job=job,
                        text="".join(chunk.text_content for chunk in durable_chunks),
                    )
                    append_provider_generation_event(
                        self.connection,
                        self.identity,
                        workspace_id=self.workspace_id,
                        fencing_generation=self.fencing_generation,
                        generation_job_id=job.generation_job_id,
                        generation_attempt_id=attempt_id,
                        provider_event=event,
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
        except GenerationLifecycleError:
            # Includes losing the lease to another instance. Terminalizing here would
            # write over a job this executor no longer owns.
            raise
        except ProviderRouteUnavailable:
            self._fail_generation(
                job.generation_job_id, attempt_id, error_code="provider-unavailable"
            )
            return
        except Exception:  # noqa: BLE001 - provider/iterator failures cross this boundary
            # A stream that started and then failed, or ended without a terminal event,
            # is a bad RESPONSE -- the provider was reached. `malformed-response`, never
            # `provider-unavailable`.
            self._fail_generation(
                job.generation_job_id, attempt_id, error_code="malformed-response"
            )
            return
        finally:
            heartbeat.stop()

    @staticmethod
    def _validate_event(
        event: Mapping[str, Any],
        *,
        invocation_id: str,
        attempt_id: str,
        expected_ordinal: int,
    ) -> tuple[str, int]:
        if not isinstance(event, Mapping):
            raise GenerationExecutorError("a provider event is not a JSON object")
        event_type = event.get("eventType")
        if event_type not in F2A_PROVIDER_EVENT_TYPES:
            raise GenerationExecutorError("a provider event type is unsupported")
        ordinal = event.get("ordinal")
        if (
            event.get("invocationId") != invocation_id
            or event.get("attemptId") != attempt_id
            or not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal != expected_ordinal
        ):
            raise GenerationExecutorError("the provider event stream is not contiguous")
        return str(event_type), expected_ordinal + 1

    def _lease_duration_us(self) -> int:
        """A lease that outlives the invocation it is protecting."""
        deadline_us = (self.config.deadline_seconds + _LEASE_MARGIN_SECONDS) * 1_000_000
        return max(DEFAULT_LEASE_US, deadline_us)

    def _resolve_route(self) -> None:
        """Refuse before invoking when the configured route cannot name a provider.

        Deliberately AFTER the claim, not before it. The owner's distinction is that a
        workspace with an executor installed but no reachable route must still produce
        a real lifecycle -- claimed, attempted, terminal -- rather than silently
        swallowing the submission. Refusing before mutation is the separate case where
        no executor boundary is installed at all, and that belongs to the handler.
        """
        if not self.config.connection_id or not self.config.model_id:
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
                "statusClass": "unknown",
                "safeMessage": _SAFE_MESSAGES[error_code],
            },
            now_us=_now_us(self.clock),
            lease_owner=self.identity.service_instance_id,
        )

    def _materialize_assistant(self, *, job: chat.GenerationJob, text: str) -> str:
        message_id = _stable_id("assistant", job.generation_job_id)
        message_hash, part_hash = _message_hash(text)
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
            writer.append_message_part(
                part_id=_stable_id("assistant-part", job.generation_job_id),
                message_id=message_id,
                conversation_id=job.conversation_id,
                part_index=0,
                part_type="text",
                schema_version=1,
                visibility="standard",
                payload={"text": text},
                provenance="ai",
                content_hash=part_hash,
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
