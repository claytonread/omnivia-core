"""Replay-safe Chat model-input compaction (T-0663).

Compaction is a projection, not a rewrite.  The raw Conversation, Message, Part
and Generation rows remain the audit history; this module records a completed,
source-linked replacement projection for provider input and lets the generation
executor consume it when it covers an initial prefix of the selected branch
lineage.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from omnivia_core.chat_contract.v1 import ChatContractDecodeError, to_canonical_json
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.connection import StorageError

__all__ = [
    "ChatCompactionConflict",
    "CompactedModelVisibleContext",
    "UnsafeCompactionBoundary",
    "derive_model_visible_context",
    "record_completed_compaction",
]


class ChatCompactionConflict(Exception):
    """The requested compaction cannot be reconciled with durable Chat state."""


class UnsafeCompactionBoundary(ChatCompactionConflict):
    """A compaction range would split an atomic tool proposal/result pair."""


@dataclass(frozen=True, slots=True)
class CompactedModelVisibleContext:
    """Provider-visible messages plus manifest references derived from storage."""

    messages: tuple[Mapping[str, Any], ...]
    manifest_message_refs: tuple[Mapping[str, Any], ...]


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise StorageError("chat compaction JSON is not representable") from error


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _digest_object(value: Mapping[str, Any]) -> str:
    try:
        document = to_canonical_json(dict(value))
    except ChatContractDecodeError as error:
        raise StorageError("chat compaction payload is not canonicalisable") from error
    return "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest()


def _safe_json_object(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StorageError(f"{label} must be a JSON object")
    try:
        to_canonical_json(dict(value))
    except ChatContractDecodeError as error:
        raise StorageError(f"{label} is not canonicalisable") from error
    text = _canonical_json(value).lower()
    for marker in ("authorization", "bearer", "credential", "endpoint", "secret", "token"):
        if marker in text:
            raise StorageError(f"{label} contains protected text")
    return dict(value)


def _safe_json_array(value: Sequence[Mapping[str, Any]], label: str) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise StorageError(f"{label} must be a JSON array")
    return [_safe_json_object(item, label) for item in value]


def _message_text_parts(
    connection: sqlite3.Connection, *, workspace_id: str, message_id: str
) -> tuple[Mapping[str, Any], ...]:
    parts: list[Mapping[str, Any]] = []
    for part in chat.read_message_parts(
        connection, workspace_id=workspace_id, message_id=message_id
    ):
        if part.visibility != "standard":
            continue
        text = part.payload.get("text") if part.part_type == "text" else None
        if not isinstance(text, str):
            raise ChatCompactionConflict(
                "the selected chat lineage contains a non-text part this projection cannot send"
            )
        parts.append({"kind": "text", "text": text})
    return tuple(parts)


def _lineage(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    trigger_message_id: str,
) -> tuple[chat.Message, ...]:
    messages = chat.read_messages_by_conversation_sequence(
        connection, workspace_id=workspace_id, conversation_id=conversation_id
    )
    by_id = {message.message_id: message for message in messages}
    ordered: list[chat.Message] = []
    seen: set[str] = set()
    current_id: str | None = trigger_message_id
    while current_id is not None:
        if current_id in seen:
            raise ChatCompactionConflict("the selected chat lineage contains a cycle")
        seen.add(current_id)
        message = by_id.get(current_id)
        if message is None:
            raise ChatCompactionConflict("the selected chat lineage is incomplete")
        ordered.append(message)
        current_id = message.parent_message_id
    return tuple(reversed(ordered))


def _message_refs(messages: Sequence[chat.Message]) -> list[Mapping[str, Any]]:
    return [
        {
            "messageId": message.message_id,
            "role": message.role,
            "conversationSequence": message.conversation_sequence,
            "schemaVersion": message.schema_version,
            "contentHash": message.content_hash,
        }
        for message in messages
    ]


def _assert_range_is_safe(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    source_start_sequence: int,
    source_end_sequence: int,
) -> tuple[chat.Message, ...]:
    messages = tuple(
        message
        for message in chat.read_messages_by_conversation_sequence(
            connection, workspace_id=workspace_id, conversation_id=conversation_id
        )
        if source_start_sequence <= message.conversation_sequence <= source_end_sequence
    )
    if not messages:
        raise ChatCompactionConflict("the compaction source range contains no messages")

    covered_ids = {message.message_id for message in messages}
    all_messages = chat.read_messages_by_conversation_sequence(
        connection, workspace_id=workspace_id, conversation_id=conversation_id
    )
    proposed: dict[str, int] = {}
    terminal: dict[str, int] = {}
    for message in all_messages:
        for part in chat.read_message_parts(
            connection, workspace_id=workspace_id, message_id=message.message_id
        ):
            tool_id = part.payload.get("toolCallId") or part.payload.get("tool_call_id")
            if not isinstance(tool_id, str) or not tool_id:
                continue
            kind = str(part.part_type).replace("-", "_")
            if kind in {"tool_proposal", "tool_call"}:
                proposed[tool_id] = message.conversation_sequence
            if kind in {"tool_result", "tool_terminal"}:
                terminal[tool_id] = message.conversation_sequence

    for tool_id, start_sequence in proposed.items():
        end_sequence = terminal.get(tool_id)
        if end_sequence is None:
            continue
        proposal_covered = source_start_sequence <= start_sequence <= source_end_sequence
        terminal_covered = source_start_sequence <= end_sequence <= source_end_sequence
        if proposal_covered != terminal_covered:
            raise UnsafeCompactionBoundary(
                "the compaction range splits a tool proposal from its terminal result"
            )

    if covered_ids != {message.message_id for message in messages}:  # pragma: no cover - sanity
        raise ChatCompactionConflict("the compaction range is inconsistent")
    return messages


def record_completed_compaction(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    conversation_id: str,
    branch_id: str,
    compaction_id: str,
    projection_id: str,
    source_start_sequence: int,
    source_end_sequence: int,
    summary_message: Mapping[str, Any],
    model_input: Sequence[Mapping[str, Any]],
    policy_version: str,
    summarizer_version: str,
    now_us: int,
) -> CompactedModelVisibleContext:
    """Record the start/summary/completed brackets and completed projection."""
    if not policy_version or not summarizer_version:
        raise ChatCompactionConflict("compaction policy and summarizer versions are required")
    if source_start_sequence < 1 or source_end_sequence < source_start_sequence:
        raise ChatCompactionConflict("compaction source range is invalid")
    conversation = chat.read_conversation(
        connection, workspace_id=workspace_id, conversation_id=conversation_id
    )
    branch = chat.read_branch(connection, workspace_id=workspace_id, branch_id=branch_id)
    if conversation is None or branch is None or branch.conversation_id != conversation_id:
        raise ChatCompactionConflict("compaction must name an existing conversation branch")
    source_messages = _assert_range_is_safe(
        connection,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        source_start_sequence=source_start_sequence,
        source_end_sequence=source_end_sequence,
    )
    safe_summary = _safe_json_object(summary_message, "compaction summary")
    safe_model_input = _safe_json_array(model_input, "compaction model input")
    omitted_refs = _message_refs(source_messages)
    projection_body = {
        "projectionId": projection_id,
        "compactionId": compaction_id,
        "sourceRange": {
            "start": source_start_sequence,
            "end": source_end_sequence,
        },
        "policyVersion": policy_version,
        "summarizerVersion": summarizer_version,
        "omittedSourceRefs": omitted_refs,
        "modelInput": safe_model_input,
    }
    projection_digest = _digest(projection_body)
    existing = _existing_projection(
        connection,
        workspace_id=workspace_id,
        projection_id=projection_id,
        compaction_id=compaction_id,
    )
    if existing is not None:
        if (
            str(existing["conversation_id"]) == conversation_id
            and str(existing["branch_id"]) == branch_id
            and str(existing["projection_id"]) == projection_id
            and str(existing["compaction_id"]) == compaction_id
            and int(existing["source_start_sequence"]) == source_start_sequence
            and int(existing["source_end_sequence"]) == source_end_sequence
            and str(existing["policy_version"]) == policy_version
            and str(existing["summarizer_version"]) == summarizer_version
            and str(existing["omitted_source_refs_json"]) == _canonical_json(omitted_refs)
            and str(existing["model_input_json"]) == _canonical_json(safe_model_input)
            and str(existing["projection_digest"]) == projection_digest
            and str(existing["state"]) == "completed"
        ):
            return _projection_context(existing)
        raise ChatCompactionConflict("compaction id already names another projection")
    events: tuple[tuple[str, Mapping[str, Any]], ...] = (
        ("started", {"sourceRange": projection_body["sourceRange"]}),
        ("summary", {"summary": safe_summary}),
        ("completed", {"projectionDigest": projection_digest}),
    )
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        for index, (event_type, payload) in enumerate(events, start=1):
            writer.connection.execute(
                "INSERT INTO omnivia_chat_compaction_events "
                "(workspace_id, conversation_id, branch_id, compaction_id, event_id, "
                "event_sequence, event_type, source_start_sequence, source_end_sequence, "
                "policy_version, summarizer_version, payload_json, payload_digest, "
                "occurred_at_us, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    workspace_id,
                    conversation_id,
                    branch_id,
                    compaction_id,
                    f"{compaction_id}.e{index}",
                    index,
                    event_type,
                    source_start_sequence,
                    source_end_sequence,
                    policy_version,
                    summarizer_version,
                    _canonical_json(payload),
                    _digest_object(payload),
                    now_us + index - 1,
                ),
            )
        writer.connection.execute(
            "INSERT INTO omnivia_chat_model_input_projections "
            "(workspace_id, conversation_id, branch_id, projection_id, compaction_id, "
            "source_start_sequence, source_end_sequence, policy_version, summarizer_version, "
            "omitted_source_refs_json, model_input_json, projection_digest, state, "
            "created_at_us, completed_at_us, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, 1)",
            (
                workspace_id,
                conversation_id,
                branch_id,
                projection_id,
                compaction_id,
                source_start_sequence,
                source_end_sequence,
                policy_version,
                summarizer_version,
                _canonical_json(omitted_refs),
                _canonical_json(safe_model_input),
                projection_digest,
                now_us,
                now_us + len(events) - 1,
            ),
        )

    return CompactedModelVisibleContext(
        messages=tuple(safe_model_input),
        manifest_message_refs=(
            {
                "kind": "compaction",
                "projectionId": projection_id,
                "compactionId": compaction_id,
                "sourceRange": {"start": source_start_sequence, "end": source_end_sequence},
                "projectionDigest": projection_digest,
                "policyVersion": policy_version,
                "summarizerVersion": summarizer_version,
                "omittedSourceRefs": tuple(omitted_refs),
            },
        ),
    )


def _latest_projection(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    branch_id: str,
    max_source_end_sequence: int,
) -> sqlite3.Row | None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        row: sqlite3.Row | None = connection.execute(
            "SELECT * FROM omnivia_chat_model_input_projections "
            "WHERE workspace_id = ? AND conversation_id = ? AND branch_id = ? "
            "AND state = 'completed' AND source_end_sequence <= ? "
            "ORDER BY source_end_sequence DESC, completed_at_us DESC, projection_id DESC LIMIT 1",
            (workspace_id, conversation_id, branch_id, max_source_end_sequence),
        ).fetchone()
        return row
    finally:
        connection.row_factory = previous


def _existing_projection(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    projection_id: str,
    compaction_id: str,
) -> sqlite3.Row | None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        rows: list[sqlite3.Row] = list(
            connection.execute(
                "SELECT * FROM omnivia_chat_model_input_projections "
                "WHERE workspace_id = ? AND (projection_id = ? OR compaction_id = ?) "
                "ORDER BY projection_id",
                (workspace_id, projection_id, compaction_id),
            ).fetchall()
        )
    finally:
        connection.row_factory = previous
    if not rows:
        return None
    if len(rows) != 1:
        raise ChatCompactionConflict(
            "compaction ids conflict with more than one existing projection"
        )
    return rows[0]


def _projection_context(row: sqlite3.Row) -> CompactedModelVisibleContext:
    source_end = int(row["source_end_sequence"])
    omitted_refs = tuple(json.loads(str(row["omitted_source_refs_json"])))
    model_input = tuple(json.loads(str(row["model_input_json"])))
    return CompactedModelVisibleContext(
        messages=model_input,
        manifest_message_refs=(
            {
                "kind": "compaction",
                "projectionId": str(row["projection_id"]),
                "compactionId": str(row["compaction_id"]),
                "sourceRange": {
                    "start": int(row["source_start_sequence"]),
                    "end": source_end,
                },
                "projectionDigest": str(row["projection_digest"]),
                "policyVersion": str(row["policy_version"]),
                "summarizerVersion": str(row["summarizer_version"]),
                "omittedSourceRefs": omitted_refs,
            },
        ),
    )


def derive_model_visible_context(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    branch_id: str,
    trigger_message_id: str,
) -> CompactedModelVisibleContext | None:
    """Return compacted provider context when a completed prefix projection applies."""
    lineage = _lineage(
        connection,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        trigger_message_id=trigger_message_id,
    )
    if not lineage:
        return None
    earliest = min(message.conversation_sequence for message in lineage)
    latest = max(message.conversation_sequence for message in lineage)
    projection = _latest_projection(
        connection,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        branch_id=branch_id,
        max_source_end_sequence=latest - 1,
    )
    if projection is None:
        return None
    if int(projection["source_start_sequence"]) > earliest:
        return None

    source_end = int(projection["source_end_sequence"])
    context = _projection_context(projection)
    messages: list[Mapping[str, Any]] = [*context.messages]
    refs: list[Mapping[str, Any]] = [*context.manifest_message_refs]
    for message in lineage:
        if message.conversation_sequence <= source_end:
            continue
        if message.visibility != "standard" or message.completion_status != "complete":
            continue
        parts = _message_text_parts(connection, workspace_id=workspace_id, message_id=message.message_id)
        if not parts:
            continue
        messages.append({"role": message.role, "parts": parts})
        refs.append(
            {
                "messageId": message.message_id,
                "role": message.role,
                "conversationSequence": message.conversation_sequence,
                "schemaVersion": message.schema_version,
                "contentHash": message.content_hash,
                "parts": tuple(
                    {
                        "partId": part.part_id,
                        "partIndex": part.part_index,
                        "partType": part.part_type,
                        "schemaVersion": part.schema_version,
                        "contentHash": part.content_hash,
                    }
                    for part in chat.read_message_parts(
                        connection, workspace_id=workspace_id, message_id=message.message_id
                    )
                    if part.visibility == "standard"
                ),
            }
        )
    return CompactedModelVisibleContext(tuple(messages), tuple(refs))
