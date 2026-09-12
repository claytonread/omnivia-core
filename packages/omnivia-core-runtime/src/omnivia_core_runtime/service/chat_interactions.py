"""Durable Chat waits, approvals and attention projection (T-0664)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from omnivia_core.chat_contract.v1 import ChatContractDecodeError, to_canonical_json
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.connection import StorageError

__all__ = [
    "ChatWaitConflict",
    "ChatWaitInteraction",
    "ChatWaitUnauthorized",
    "attention_projection",
    "decide_chat_wait",
    "expire_chat_wait",
    "open_chat_wait",
    "read_chat_wait",
]


class ChatWaitConflict(Exception):
    """A wait transition or replay disagrees with durable state."""


class ChatWaitUnauthorized(ChatWaitConflict):
    """The actor is not authorised to answer this wait."""


@dataclass(frozen=True, slots=True)
class ChatWaitInteraction:
    workspace_id: str
    conversation_id: str
    wait_id: str
    kind: str
    state: str
    requester_actor_id: str
    authorised_responder_policy: Mapping[str, Any]
    prompt: Mapping[str, Any]
    resume_token_digest: str
    generation_job_id: str | None
    turn_id: str | None
    tool_call_id: str | None
    agent_run_id: str | None
    decision: str | None
    decided_by_actor_id: str | None
    sensitive_answer_ciphertext_digest: str | None
    audit_ref: str | None
    version: int
    created_at_us: int
    updated_at_us: int
    expires_at_us: int | None
    decided_at_us: int | None


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
        raise StorageError("chat wait JSON is not representable") from error


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _safe_object(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StorageError(f"{label} must be a JSON object")
    try:
        to_canonical_json(dict(value))
    except ChatContractDecodeError as error:
        raise StorageError(f"{label} is not canonicalisable") from error
    return dict(value)


def _row_to_wait(workspace_id: str, row: sqlite3.Row) -> ChatWaitInteraction:
    return ChatWaitInteraction(
        workspace_id=workspace_id,
        conversation_id=str(row["conversation_id"]),
        wait_id=str(row["wait_id"]),
        kind=str(row["kind"]),
        state=str(row["state"]),
        requester_actor_id=str(row["requester_actor_id"]),
        authorised_responder_policy=json.loads(str(row["authorised_responder_policy_json"])),
        prompt=json.loads(str(row["prompt_json"])),
        resume_token_digest=str(row["resume_token_digest"]),
        generation_job_id=None if row["generation_job_id"] is None else str(row["generation_job_id"]),
        turn_id=None if row["turn_id"] is None else str(row["turn_id"]),
        tool_call_id=None if row["tool_call_id"] is None else str(row["tool_call_id"]),
        agent_run_id=None if row["agent_run_id"] is None else str(row["agent_run_id"]),
        decision=None if row["decision"] is None else str(row["decision"]),
        decided_by_actor_id=(
            None if row["decided_by_actor_id"] is None else str(row["decided_by_actor_id"])
        ),
        sensitive_answer_ciphertext_digest=(
            None
            if row["sensitive_answer_ciphertext_digest"] is None
            else str(row["sensitive_answer_ciphertext_digest"])
        ),
        audit_ref=None if row["audit_ref"] is None else str(row["audit_ref"]),
        version=int(row["version"]),
        created_at_us=int(row["created_at_us"]),
        updated_at_us=int(row["updated_at_us"]),
        expires_at_us=None if row["expires_at_us"] is None else int(row["expires_at_us"]),
        decided_at_us=None if row["decided_at_us"] is None else int(row["decided_at_us"]),
    )


def read_chat_wait(
    connection: sqlite3.Connection, *, workspace_id: str, wait_id: str
) -> ChatWaitInteraction | None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM omnivia_chat_wait_interactions "
            "WHERE workspace_id = ? AND wait_id = ?",
            (workspace_id, wait_id),
        ).fetchone()
        return None if row is None else _row_to_wait(workspace_id, row)
    finally:
        connection.row_factory = previous


def _same_open_wait(
    existing: ChatWaitInteraction,
    *,
    conversation_id: str,
    kind: str,
    requester_actor_id: str,
    policy: Mapping[str, Any],
    prompt: Mapping[str, Any],
    resume_token_digest: str,
    generation_job_id: str | None,
    turn_id: str | None,
    tool_call_id: str | None,
    agent_run_id: str | None,
    expires_at_us: int | None,
) -> bool:
    return (
        existing.conversation_id == conversation_id
        and existing.kind == kind
        and existing.requester_actor_id == requester_actor_id
        and dict(existing.authorised_responder_policy) == dict(policy)
        and dict(existing.prompt) == dict(prompt)
        and existing.resume_token_digest == resume_token_digest
        and existing.generation_job_id == generation_job_id
        and existing.turn_id == turn_id
        and existing.tool_call_id == tool_call_id
        and existing.agent_run_id == agent_run_id
        and existing.expires_at_us == expires_at_us
    )


def open_chat_wait(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    conversation_id: str,
    wait_id: str,
    kind: str,
    requester_actor_id: str,
    authorised_responder_policy: Mapping[str, Any],
    prompt: Mapping[str, Any],
    resume_token: str,
    now_us: int,
    generation_job_id: str | None = None,
    turn_id: str | None = None,
    tool_call_id: str | None = None,
    agent_run_id: str | None = None,
    expires_at_us: int | None = None,
) -> ChatWaitInteraction:
    """Open or replay one durable wait without foreground-route side effects."""
    if kind not in {"approval", "human_input", "policy_pause"}:
        raise ChatWaitConflict("unsupported chat wait kind")
    if not wait_id or not requester_actor_id or not resume_token:
        raise ChatWaitConflict("chat wait identity fields are required")
    conversation = chat.read_conversation(
        connection, workspace_id=workspace_id, conversation_id=conversation_id
    )
    if conversation is None:
        raise ChatWaitConflict("chat wait must belong to an existing conversation")
    policy = _safe_object(authorised_responder_policy, "chat wait responder policy")
    safe_prompt = _safe_object(prompt, "chat wait prompt")
    resume_digest = _digest({"resumeToken": resume_token})
    existing = read_chat_wait(connection, workspace_id=workspace_id, wait_id=wait_id)
    if existing is not None:
        if _same_open_wait(
            existing,
            conversation_id=conversation_id,
            kind=kind,
            requester_actor_id=requester_actor_id,
            policy=policy,
            prompt=safe_prompt,
            resume_token_digest=resume_digest,
            generation_job_id=generation_job_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            agent_run_id=agent_run_id,
            expires_at_us=expires_at_us,
        ):
            return existing
        raise ChatWaitConflict("chat wait id already names another wait")

    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.connection.execute(
            "INSERT INTO omnivia_chat_wait_interactions "
            "(workspace_id, conversation_id, wait_id, kind, state, requester_actor_id, "
            "authorised_responder_policy_json, prompt_json, resume_token_digest, "
            "generation_job_id, turn_id, tool_call_id, agent_run_id, version, schema_version, "
            "created_at_us, updated_at_us, expires_at_us) "
            "VALUES (?, ?, ?, ?, 'asked', ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)",
            (
                workspace_id,
                conversation_id,
                wait_id,
                kind,
                requester_actor_id,
                _canonical_json(policy),
                _canonical_json(safe_prompt),
                resume_digest,
                generation_job_id,
                turn_id,
                tool_call_id,
                agent_run_id,
                now_us,
                now_us,
                expires_at_us,
            ),
        )
    opened = read_chat_wait(connection, workspace_id=workspace_id, wait_id=wait_id)
    if opened is None:  # pragma: no cover - committed above
        raise StorageError(f"chat wait {wait_id!r} did not settle")
    return opened


def _authorised(policy: Mapping[str, Any], actor_id: str) -> bool:
    if policy.get("anyActor") is True:
        return True
    actor = policy.get("actorId")
    if actor == actor_id:
        return True
    actors = policy.get("actorIds")
    return isinstance(actors, list) and actor_id in actors


def decide_chat_wait(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    wait_id: str,
    expected_version: int,
    decided_by_actor_id: str,
    decision: str,
    now_us: int,
    sensitive_answer: Mapping[str, Any] | None = None,
    audit_ref: str | None = None,
) -> ChatWaitInteraction:
    """CAS-record the single decision for a wait; raw answer content is not stored."""
    wait = read_chat_wait(connection, workspace_id=workspace_id, wait_id=wait_id)
    if wait is None:
        raise ChatWaitConflict("chat wait is unknown")
    if wait.state != "asked":
        raise ChatWaitConflict("chat wait is no longer pending")
    if wait.version != expected_version:
        raise ChatWaitConflict("chat wait version is stale")
    if not _authorised(wait.authorised_responder_policy, decided_by_actor_id):
        raise ChatWaitUnauthorized("actor is not authorised to decide this chat wait")
    allowed = {"approval": {"approved", "denied"}, "human_input": {"answered"}, "policy_pause": {"approved", "denied"}}
    if decision not in allowed[wait.kind]:
        raise ChatWaitConflict("decision is not legal for this wait kind")
    answer_digest = None if sensitive_answer is None else _digest(_safe_object(sensitive_answer, "chat wait answer"))
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        cursor = writer.connection.execute(
            "UPDATE omnivia_chat_wait_interactions "
            "SET state = 'decided', decision = ?, decided_by_actor_id = ?, "
            "sensitive_answer_ciphertext_digest = ?, audit_ref = ?, version = version + 1, "
            "updated_at_us = ?, decided_at_us = ? "
            "WHERE workspace_id = ? AND wait_id = ? AND state = 'asked' AND version = ?",
            (
                decision,
                decided_by_actor_id,
                answer_digest,
                audit_ref,
                now_us,
                now_us,
                workspace_id,
                wait_id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise ChatWaitConflict("chat wait decision lost the compare-and-set race")
    decided = read_chat_wait(connection, workspace_id=workspace_id, wait_id=wait_id)
    if decided is None:  # pragma: no cover
        raise StorageError(f"chat wait {wait_id!r} vanished")
    return decided


def expire_chat_wait(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    wait_id: str,
    expected_version: int,
    now_us: int,
) -> ChatWaitInteraction:
    wait = read_chat_wait(connection, workspace_id=workspace_id, wait_id=wait_id)
    if wait is None:
        raise ChatWaitConflict("chat wait is unknown")
    if wait.state != "asked" or wait.version != expected_version:
        raise ChatWaitConflict("chat wait cannot expire from its current state")
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        cursor = writer.connection.execute(
            "UPDATE omnivia_chat_wait_interactions "
            "SET state = 'expired', version = version + 1, updated_at_us = ? "
            "WHERE workspace_id = ? AND wait_id = ? AND state = 'asked' AND version = ?",
            (now_us, workspace_id, wait_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise ChatWaitConflict("chat wait expiry lost the compare-and-set race")
    expired = read_chat_wait(connection, workspace_id=workspace_id, wait_id=wait_id)
    if expired is None:  # pragma: no cover
        raise StorageError(f"chat wait {wait_id!r} vanished")
    return expired


def attention_projection(
    connection: sqlite3.Connection, *, workspace_id: str, actor_id: str
) -> tuple[ChatWaitInteraction, ...]:
    """All unresolved waits this actor may answer, without changing focus/route."""
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM omnivia_chat_wait_interactions "
            "WHERE workspace_id = ? AND state = 'asked' ORDER BY created_at_us, wait_id",
            (workspace_id,),
        ).fetchall()
    finally:
        connection.row_factory = previous
    waits = tuple(_row_to_wait(workspace_id, row) for row in rows)
    return tuple(wait for wait in waits if _authorised(wait.authorised_responder_policy, actor_id))
