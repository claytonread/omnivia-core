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
from omnivia_core_runtime.service.chat_generation import (
    GenerationLifecycleError,
    append_provider_generation_event,
    claim_queued_generation,
)
from omnivia_core_runtime.storage import chat

__all__ = [
    "ChatGenerationExecutor",
    "GenerationExecutorConfig",
    "GenerationExecutorError",
    "ProviderStream",
]

ProviderStream = Callable[[ProviderInvocationRequest], Iterable[Mapping[str, Any]]]
_MAX_TEXT_BYTES: Final = 262_144
_TERMINAL_PROVIDER_EVENTS: Final = frozenset({"finish", "error"})


class GenerationExecutorError(Exception):
    """A sanitized executor refusal; provider content is never included."""


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


def _provider_messages(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    trigger_message_id: str,
) -> tuple[Mapping[str, Any], ...]:
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

    result: list[Mapping[str, Any]] = []
    for message in reversed(lineage):
        if message.visibility != "standard" or message.completion_status != "complete":
            continue
        parts: list[Mapping[str, Any]] = []
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
            parts.append({"kind": "text", "text": text})
        if parts:
            result.append({"role": message.role, "parts": parts})
    if not result:
        raise GenerationExecutorError("the conversation contains no provider-visible messages")
    return tuple(result)


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


@dataclass(frozen=True, slots=True)
class ChatGenerationExecutor:
    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    fencing_generation: int
    workspace_id: str
    clock: Clock
    invoke: ProviderStream
    config: GenerationExecutorConfig

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
        )
        invocation_id = _stable_id("provider-inv", generation_job_id)
        requested_at = self.clock.wall_time()
        try:
            request = ProviderInvocationRequest(
                invocation_id=invocation_id,
                workspace_id=self.workspace_id,
                conversation_id=claimed.job.conversation_id,
                job_id=generation_job_id,
                attempt_id=attempt_id,
                connection_id=self.config.connection_id,
                model_id=self.config.model_id,
                operation="language.stream",
                messages=_provider_messages(
                    self.connection,
                    workspace_id=self.workspace_id,
                    conversation_id=claimed.job.conversation_id,
                    trigger_message_id=trigger_message_id,
                ),
                response_format={"kind": "text"},
                policy_ref=self.config.policy_ref,
                classification_ref=self.config.classification_ref,
                residency_ref=self.config.residency_ref,
                idempotency_key=claimed.job.idempotency_key,
                correlation_id=_stable_id("correlation", generation_job_id),
                deadline_at=_timestamp(
                    requested_at + timedelta(seconds=self.config.deadline_seconds)
                ),
                requested_at=_timestamp(requested_at),
                causation_id=trigger_message_id,
            )
        except Exception:  # noqa: BLE001 - boundary failures are terminalized, never exposed
            self._fail_generation(generation_job_id, attempt_id)
            return

        text_chunks: list[str] = []
        expected_ordinal = 0
        saw_start = False
        saw_terminal = False
        durable_started = any(
            event.event_type == "chat.generation.started"
            for event in chat.read_generation_events(
                self.connection,
                workspace_id=self.workspace_id,
                generation_job_id=generation_job_id,
            )
        )
        try:
            stream = self.invoke(request)
            for event in stream:
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
                    text_chunks.append(delta)
                    if len("".join(text_chunks).encode("utf-8")) > _MAX_TEXT_BYTES:
                        raise GenerationExecutorError("the provider response exceeds message bounds")
                if event_type == "finish" and event.get("finishReason") not in {
                    "error",
                    "cancelled",
                }:
                    if not saw_start or not text_chunks:
                        raise GenerationExecutorError("a successful provider stream is incomplete")
                    result_message_id = self._materialize_assistant(
                        claimed=claimed, text="".join(text_chunks)
                    )
                    append_provider_generation_event(
                        self.connection,
                        self.identity,
                        workspace_id=self.workspace_id,
                        fencing_generation=self.fencing_generation,
                        generation_job_id=generation_job_id,
                        generation_attempt_id=attempt_id,
                        provider_event=event,
                        result_message_id=result_message_id,
                        now_us=_now_us(self.clock),
                    )
                elif event_type != "stream-start" or not durable_started:
                    append_provider_generation_event(
                        self.connection,
                        self.identity,
                        workspace_id=self.workspace_id,
                        fencing_generation=self.fencing_generation,
                        generation_job_id=generation_job_id,
                        generation_attempt_id=attempt_id,
                        provider_event=event,
                        now_us=_now_us(self.clock),
                    )
                if event_type in _TERMINAL_PROVIDER_EVENTS:
                    saw_terminal = True
                    break
            if not saw_terminal:
                raise GenerationExecutorError("the provider stream ended without a terminal event")
        except GenerationLifecycleError:
            raise
        except Exception:  # noqa: BLE001 - provider/iterator failures cross this boundary
            self._fail_generation(generation_job_id, attempt_id)
            return

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

    def _fail_generation(self, generation_job_id: str, attempt_id: str) -> None:
        job = chat.read_generation_job(
            self.connection,
            workspace_id=self.workspace_id,
            generation_job_id=generation_job_id,
        )
        if job is None or job.state != "running":
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
                "errorCode": "malformed-response",
                "retryable": False,
                "statusClass": "unknown",
                "safeMessage": "the provider stream could not be completed safely",
            },
            now_us=_now_us(self.clock),
        )

    def _materialize_assistant(self, *, claimed: Any, text: str) -> str:
        job = claimed.job
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
