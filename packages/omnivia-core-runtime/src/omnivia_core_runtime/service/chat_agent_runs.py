"""Durable delegated Chat Agent Runs and mailbox continuation (T-0665)."""

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
    "AgentRunConflict",
    "AgentRunMailboxMessage",
    "ChatAgentRun",
    "agent_run_projection",
    "deliver_mailbox_message",
    "enqueue_mailbox_message",
    "heartbeat_agent_run",
    "mark_agent_run_terminal",
    "open_agent_run",
]


class AgentRunConflict(Exception):
    """A delegated run or mailbox request disagrees with durable state."""


@dataclass(frozen=True, slots=True)
class ChatAgentRun:
    workspace_id: str
    conversation_id: str
    agent_run_id: str
    parent_generation_job_id: str | None
    parent_turn_id: str | None
    runtime_name: str
    runtime_version: str
    display_name: str
    mode: str
    state: str
    configuration_digest: str
    created_by_actor_id: str
    heartbeat_at_us: int | None
    version: int
    created_at_us: int
    updated_at_us: int
    finished_at_us: int | None


@dataclass(frozen=True, slots=True)
class AgentRunMailboxMessage:
    workspace_id: str
    agent_run_id: str
    mailbox_message_id: str
    direction: str
    idempotency_key: str
    sender_actor_id: str
    payload: Mapping[str, Any]
    payload_digest: str
    delivery_state: str
    created_at_us: int
    delivered_at_us: int | None


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
        raise StorageError("chat agent run JSON is not representable") from error


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _safe_object(value: Mapping[str, Any], label: str) -> dict[str, Any]:
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


def _run_from_row(workspace_id: str, row: sqlite3.Row) -> ChatAgentRun:
    return ChatAgentRun(
        workspace_id=workspace_id,
        conversation_id=str(row["conversation_id"]),
        agent_run_id=str(row["agent_run_id"]),
        parent_generation_job_id=(
            None if row["parent_generation_job_id"] is None else str(row["parent_generation_job_id"])
        ),
        parent_turn_id=None if row["parent_turn_id"] is None else str(row["parent_turn_id"]),
        runtime_name=str(row["runtime_name"]),
        runtime_version=str(row["runtime_version"]),
        display_name=str(row["display_name"]),
        mode=str(row["mode"]),
        state=str(row["state"]),
        configuration_digest=str(row["configuration_digest"]),
        created_by_actor_id=str(row["created_by_actor_id"]),
        heartbeat_at_us=None if row["heartbeat_at_us"] is None else int(row["heartbeat_at_us"]),
        version=int(row["version"]),
        created_at_us=int(row["created_at_us"]),
        updated_at_us=int(row["updated_at_us"]),
        finished_at_us=None if row["finished_at_us"] is None else int(row["finished_at_us"]),
    )


def _message_from_row(workspace_id: str, row: sqlite3.Row) -> AgentRunMailboxMessage:
    return AgentRunMailboxMessage(
        workspace_id=workspace_id,
        agent_run_id=str(row["agent_run_id"]),
        mailbox_message_id=str(row["mailbox_message_id"]),
        direction=str(row["direction"]),
        idempotency_key=str(row["idempotency_key"]),
        sender_actor_id=str(row["sender_actor_id"]),
        payload=json.loads(str(row["payload_json"])),
        payload_digest=str(row["payload_digest"]),
        delivery_state=str(row["delivery_state"]),
        created_at_us=int(row["created_at_us"]),
        delivered_at_us=None if row["delivered_at_us"] is None else int(row["delivered_at_us"]),
    )


def read_agent_run(
    connection: sqlite3.Connection, *, workspace_id: str, agent_run_id: str
) -> ChatAgentRun | None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM omnivia_chat_agent_runs "
            "WHERE workspace_id = ? AND agent_run_id = ?",
            (workspace_id, agent_run_id),
        ).fetchone()
        return None if row is None else _run_from_row(workspace_id, row)
    finally:
        connection.row_factory = previous


def open_agent_run(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    conversation_id: str,
    agent_run_id: str,
    runtime_name: str,
    runtime_version: str,
    display_name: str,
    mode: str,
    configuration: Mapping[str, Any],
    created_by_actor_id: str,
    now_us: int,
    parent_generation_job_id: str | None = None,
    parent_turn_id: str | None = None,
) -> ChatAgentRun:
    """Open or replay a delegated run descriptor by exact id, not display name."""
    if mode not in {"one_shot", "continuable"}:
        raise AgentRunConflict("agent run mode is invalid")
    if not agent_run_id or not runtime_name or not runtime_version or not display_name:
        raise AgentRunConflict("agent run identity fields are required")
    conversation = chat.read_conversation(
        connection, workspace_id=workspace_id, conversation_id=conversation_id
    )
    if conversation is None:
        raise AgentRunConflict("agent run must belong to an existing conversation")
    config_digest = _digest(_safe_object(configuration, "agent run configuration"))
    existing = read_agent_run(connection, workspace_id=workspace_id, agent_run_id=agent_run_id)
    if existing is not None:
        if (
            existing.conversation_id == conversation_id
            and existing.parent_generation_job_id == parent_generation_job_id
            and existing.parent_turn_id == parent_turn_id
            and existing.runtime_name == runtime_name
            and existing.runtime_version == runtime_version
            and existing.display_name == display_name
            and existing.mode == mode
            and existing.configuration_digest == config_digest
            and existing.created_by_actor_id == created_by_actor_id
        ):
            return existing
        raise AgentRunConflict("agent run id already names another run")
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.connection.execute(
            "INSERT INTO omnivia_chat_agent_runs "
            "(workspace_id, conversation_id, agent_run_id, parent_generation_job_id, "
            "parent_turn_id, runtime_name, runtime_version, display_name, mode, state, "
            "configuration_digest, created_by_actor_id, heartbeat_at_us, version, "
            "schema_version, created_at_us, updated_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, 1, 1, ?, ?)",
            (
                workspace_id,
                conversation_id,
                agent_run_id,
                parent_generation_job_id,
                parent_turn_id,
                runtime_name,
                runtime_version,
                display_name,
                mode,
                config_digest,
                created_by_actor_id,
                now_us,
                now_us,
                now_us,
            ),
        )
    opened = read_agent_run(connection, workspace_id=workspace_id, agent_run_id=agent_run_id)
    if opened is None:  # pragma: no cover
        raise StorageError(f"agent run {agent_run_id!r} did not settle")
    return opened


def _mailbox_by_key(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    agent_run_id: str,
    idempotency_key: str,
) -> AgentRunMailboxMessage | None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM omnivia_chat_agent_run_mailbox "
            "WHERE workspace_id = ? AND agent_run_id = ? AND idempotency_key = ?",
            (workspace_id, agent_run_id, idempotency_key),
        ).fetchone()
        return None if row is None else _message_from_row(workspace_id, row)
    finally:
        connection.row_factory = previous


def enqueue_mailbox_message(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    agent_run_id: str,
    mailbox_message_id: str,
    direction: str,
    idempotency_key: str,
    sender_actor_id: str,
    payload: Mapping[str, Any],
    now_us: int,
) -> AgentRunMailboxMessage:
    """Persist a mailbox message before acknowledging submission to the caller."""
    if direction not in {"to_agent", "from_agent"}:
        raise AgentRunConflict("mailbox direction is invalid")
    run = read_agent_run(connection, workspace_id=workspace_id, agent_run_id=agent_run_id)
    if run is None:
        raise AgentRunConflict("mailbox message must name an existing agent run")
    if run.state in {"succeeded", "failed", "cancelled"}:
        raise AgentRunConflict("terminal agent runs cannot accept mailbox messages")
    if run.mode == "one_shot" and direction == "to_agent" and _mailbox_by_key(
        connection,
        workspace_id=workspace_id,
        agent_run_id=agent_run_id,
        idempotency_key=idempotency_key,
    ) is None:
        existing_to_agent = connection.execute(
            "SELECT 1 FROM omnivia_chat_agent_run_mailbox "
            "WHERE workspace_id = ? AND agent_run_id = ? AND direction = 'to_agent' LIMIT 1",
            (workspace_id, agent_run_id),
        ).fetchone()
        if existing_to_agent is not None:
            raise AgentRunConflict("one-shot agent runs accept only one input message")

    safe_payload = _safe_object(payload, "agent run mailbox payload")
    payload_digest = _digest(safe_payload)
    existing = _mailbox_by_key(
        connection,
        workspace_id=workspace_id,
        agent_run_id=agent_run_id,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        if (
            existing.mailbox_message_id == mailbox_message_id
            and existing.direction == direction
            and existing.sender_actor_id == sender_actor_id
            and existing.payload_digest == payload_digest
        ):
            return existing
        raise AgentRunConflict("mailbox idempotency key already names another message")

    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.connection.execute(
            "INSERT INTO omnivia_chat_agent_run_mailbox "
            "(workspace_id, agent_run_id, mailbox_message_id, direction, idempotency_key, "
            "sender_actor_id, payload_json, payload_digest, delivery_state, created_at_us, "
            "schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, 1)",
            (
                workspace_id,
                agent_run_id,
                mailbox_message_id,
                direction,
                idempotency_key,
                sender_actor_id,
                _canonical_json(safe_payload),
                payload_digest,
                now_us,
            ),
        )
    stored = _mailbox_by_key(
        connection,
        workspace_id=workspace_id,
        agent_run_id=agent_run_id,
        idempotency_key=idempotency_key,
    )
    if stored is None:  # pragma: no cover
        raise StorageError(f"mailbox message {mailbox_message_id!r} did not settle")
    return stored


def deliver_mailbox_message(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    mailbox_message_id: str,
    now_us: int,
) -> AgentRunMailboxMessage:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM omnivia_chat_agent_run_mailbox "
            "WHERE workspace_id = ? AND mailbox_message_id = ?",
            (workspace_id, mailbox_message_id),
        ).fetchone()
    finally:
        connection.row_factory = previous
    if row is None:
        raise AgentRunConflict("mailbox message is unknown")
    message = _message_from_row(workspace_id, row)
    if message.delivery_state == "delivered":
        return message
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        writer.connection.execute(
            "UPDATE omnivia_chat_agent_run_mailbox "
            "SET delivery_state = 'delivered', delivered_at_us = ? "
            "WHERE workspace_id = ? AND mailbox_message_id = ? AND delivery_state = 'queued'",
            (now_us, workspace_id, mailbox_message_id),
        )
    delivered = _mailbox_by_key(
        connection,
        workspace_id=workspace_id,
        agent_run_id=message.agent_run_id,
        idempotency_key=message.idempotency_key,
    )
    if delivered is None:  # pragma: no cover
        raise StorageError(f"mailbox message {mailbox_message_id!r} vanished")
    return delivered


def heartbeat_agent_run(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    agent_run_id: str,
    expected_version: int,
    now_us: int,
    state: str = "running",
) -> ChatAgentRun:
    if state not in {"running", "waiting", "stale"}:
        raise AgentRunConflict("heartbeat state is not open")
    run = read_agent_run(connection, workspace_id=workspace_id, agent_run_id=agent_run_id)
    if run is None or run.version != expected_version or run.state in {"succeeded", "failed", "cancelled"}:
        raise AgentRunConflict("agent run heartbeat lost the compare-and-set race")
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        cursor = writer.connection.execute(
            "UPDATE omnivia_chat_agent_runs "
            "SET state = ?, heartbeat_at_us = ?, version = version + 1, updated_at_us = ? "
            "WHERE workspace_id = ? AND agent_run_id = ? AND version = ?",
            (state, now_us, now_us, workspace_id, agent_run_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise AgentRunConflict("agent run heartbeat lost the compare-and-set race")
    updated = read_agent_run(connection, workspace_id=workspace_id, agent_run_id=agent_run_id)
    if updated is None:  # pragma: no cover
        raise StorageError(f"agent run {agent_run_id!r} vanished")
    return updated


def mark_agent_run_terminal(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    agent_run_id: str,
    expected_version: int,
    state: str,
    now_us: int,
) -> ChatAgentRun:
    if state not in {"succeeded", "failed", "cancelled"}:
        raise AgentRunConflict("terminal state is invalid")
    run = read_agent_run(connection, workspace_id=workspace_id, agent_run_id=agent_run_id)
    if run is None or run.version != expected_version:
        raise AgentRunConflict("agent run terminal update lost the compare-and-set race")
    if run.state in {"succeeded", "failed", "cancelled"}:
        if run.state == state:
            return run
        raise AgentRunConflict("agent run is already terminal")
    with chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    ) as writer:
        cursor = writer.connection.execute(
            "UPDATE omnivia_chat_agent_runs "
            "SET state = ?, version = version + 1, updated_at_us = ?, finished_at_us = ? "
            "WHERE workspace_id = ? AND agent_run_id = ? AND version = ?",
            (state, now_us, now_us, workspace_id, agent_run_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise AgentRunConflict("agent run terminal update lost the compare-and-set race")
    updated = read_agent_run(connection, workspace_id=workspace_id, agent_run_id=agent_run_id)
    if updated is None:  # pragma: no cover
        raise StorageError(f"agent run {agent_run_id!r} vanished")
    return updated


def agent_run_projection(
    connection: sqlite3.Connection, *, workspace_id: str, conversation_id: str
) -> tuple[Mapping[str, Any], ...]:
    """Bounded Chat/Activity projection: descriptors and counts, not event logs."""
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT r.*, "
            "SUM(CASE WHEN m.delivery_state = 'queued' THEN 1 ELSE 0 END) AS queued_count "
            "FROM omnivia_chat_agent_runs r "
            "LEFT JOIN omnivia_chat_agent_run_mailbox m "
            "ON m.workspace_id = r.workspace_id AND m.agent_run_id = r.agent_run_id "
            "WHERE r.workspace_id = ? AND r.conversation_id = ? "
            "GROUP BY r.workspace_id, r.agent_run_id "
            "ORDER BY r.updated_at_us DESC, r.agent_run_id",
            (workspace_id, conversation_id),
        ).fetchall()
    finally:
        connection.row_factory = previous
    return tuple(
        {
            "agentRunId": str(row["agent_run_id"]),
            "conversationId": str(row["conversation_id"]),
            "runtime": {
                "name": str(row["runtime_name"]),
                "version": str(row["runtime_version"]),
            },
            "displayName": str(row["display_name"]),
            "mode": str(row["mode"]),
            "state": str(row["state"]),
            "heartbeatAtUs": None if row["heartbeat_at_us"] is None else int(row["heartbeat_at_us"]),
            "queuedMailboxCount": int(row["queued_count"] or 0),
        }
        for row in rows
    )
