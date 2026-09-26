"""Engineering continuity records (SPEC-CORE-ENGMEM-001, plan PR-B; spec §7, §9).

Every write here runs inside the fenced mutation transaction the service's
mutation coordinator opens, exactly as the decision family's storage does: this
module holds no connection, no lease and no clock of its own. The guard triggers
migration 0048 carries make an unguarded write impossible, and the workspace
writer generation is enforced there — the session's own binding generation is
recorded on the row and enforced by the callers of this module.

The record families are migrations 0048 only: `omnivia_engineering_sessions`
(operational binding bookkeeping) and `omnivia_engineering_checkpoints`
(immutable L0 evidence, strictly append-only, contiguous per-session sequence).
This module is the only writer; handlers never spell SQL against these tables.

A stored checkpoint is evidence, not accepted knowledge: nothing here writes
governed records, governance state, or anything the retrieval surfaces would
serve as accepted.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any, Final

from omnivia_core_runtime.storage.decisions import canonical_document, content_digest

_SESSIONS_TABLE: Final = "omnivia_engineering_sessions"
_CHECKPOINTS_TABLE: Final = "omnivia_engineering_checkpoints"

#: The default checkpoint payload cap: 256 KiB of canonical UTF-8 (spec §9.2).
CHECKPOINT_PAYLOAD_CAP_BYTES: Final = 262144

#: The default session lease: 24 hours, refreshed by re-registration through the
#: trusted adapter. Enforcement of expiry lands with the binding-refresh slice.
SESSION_LEASE_SECONDS: Final = 24 * 60 * 60


class SessionNotFound(LookupError):
    """The named continuity session does not exist in this workspace."""


class SessionNotActive(RuntimeError):
    """The continuity session exists but is not `active`."""


class SequencePreconditionFailed(RuntimeError):
    """The stated expected predecessor sequence is not the session's last."""


class ParentCheckpointMismatch(RuntimeError):
    """The named parent checkpoint is not the session's current head."""


class PayloadTooLarge(RuntimeError):
    """The canonical checkpoint payload exceeds the 256 KiB cap."""


def _plain(value: Any) -> Any:
    """Decode the contract's immutable containers into JSON-serialisable ones."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def register_session(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    session_id: str,
    principal_id: str,
    binding_generation: int = 1,
    host_session_ref: str | None,
    checkout_hint: str | None,
    repository_target: Mapping[str, Any] | None,
    registered_at_us: int,
) -> None:
    """Insert one `active` session binding. Identity is immutable once written."""
    connection.execute(
        f"INSERT INTO {_SESSIONS_TABLE} "
        "(workspace_id, session_id, principal_id, state, binding_generation, "
        "lease_expires_at_us, host_session_ref, checkout_hint, "
        "repository_target_json, registered_at_us, closed_at_us, "
        "last_checkpoint_sequence, last_checkpoint_id, audit_ref) "
        "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)",
        (
            workspace_id,
            session_id,
            principal_id,
            int(binding_generation),
            int(registered_at_us) + SESSION_LEASE_SECONDS * 1_000_000,
            host_session_ref,
            checkout_hint,
            None if repository_target is None else canonical_document(dict(repository_target)),
            int(registered_at_us),
            settlement.audit_ref,
        ),
    )


def read_session(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    session_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        f"SELECT session_id, principal_id, state, binding_generation, "
        "lease_expires_at_us, host_session_ref, checkout_hint, "
        "repository_target_json, registered_at_us, closed_at_us, "
        "last_checkpoint_sequence, last_checkpoint_id "
        f"FROM {_SESSIONS_TABLE} WHERE workspace_id = ? AND session_id = ?",
        (workspace_id, session_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "session_id": row[0],
        "principal_id": row[1],
        "state": row[2],
        "binding_generation": row[3],
        "lease_expires_at_us": row[4],
        "host_session_ref": row[5],
        "checkout_hint": row[6],
        "repository_target": None if row[7] is None else json.loads(row[7]),
        "registered_at_us": row[8],
        "closed_at_us": row[9],
        "last_checkpoint_sequence": row[10],
        "last_checkpoint_id": row[11],
    }


def _require_active_session(
    connection: sqlite3.Connection, *, workspace_id: str, session_id: str
) -> dict[str, Any]:
    session = read_session(
        connection, workspace_id=workspace_id, session_id=session_id
    )
    if session is None:
        raise SessionNotFound(session_id)
    if session["state"] != "active":
        raise SessionNotActive(session["state"])
    return session


def append_checkpoint(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    checkpoint_id: str,
    session_id: str,
    parent_checkpoint_id: str | None,
    expected_parent_sequence: int | None,
    checkpoint_kind: str,
    payload: Mapping[str, Any],
    recorded_at_us: int,
) -> dict[str, Any]:
    """Append one immutable checkpoint and advance the session's head pointer.

    One fenced transaction writes both the checkpoint row and the session's
    `last_checkpoint_*` pointer, so a competing successor loses here — either the
    expected parent still is the head and this caller wins, or the write is
    refused and the caller deliberately reconciles. The payload is stored whole
    or not at all: an oversized payload raises before anything is written.
    """
    session = _require_active_session(
        connection, workspace_id=workspace_id, session_id=session_id
    )
    last_sequence = session["last_checkpoint_sequence"]
    last_id = session["last_checkpoint_id"]
    if expected_parent_sequence is not None and int(expected_parent_sequence) != (
        last_sequence or 0
    ):
        raise SequencePreconditionFailed(
            f"expected parent sequence {expected_parent_sequence}, "
            f"session head is {last_sequence}"
        )
    if parent_checkpoint_id is not None and parent_checkpoint_id != last_id:
        raise ParentCheckpointMismatch(
            "the named parent checkpoint is not this session's current head"
        )
    payload_json = canonical_document(_plain(dict(payload)))
    if len(payload_json.encode("utf-8")) > CHECKPOINT_PAYLOAD_CAP_BYTES:
        raise PayloadTooLarge(
            f"canonical checkpoint payload exceeds {CHECKPOINT_PAYLOAD_CAP_BYTES} bytes"
        )
    sequence = (last_sequence or 0) + 1
    connection.execute(
        f"INSERT INTO {_CHECKPOINTS_TABLE} "
        "(workspace_id, checkpoint_id, session_id, sequence, parent_checkpoint_id, "
        "checkpoint_kind, payload_json, content_digest, recorded_at_us, audit_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            checkpoint_id,
            session_id,
            sequence,
            parent_checkpoint_id,
            checkpoint_kind,
            payload_json,
            content_digest(payload_json),
            int(recorded_at_us),
            settlement.audit_ref,
        ),
    )
    connection.execute(
        f"UPDATE {_SESSIONS_TABLE} SET last_checkpoint_sequence = ?, "
        "last_checkpoint_id = ? WHERE workspace_id = ? AND session_id = ?",
        (sequence, checkpoint_id, workspace_id, session_id),
    )
    return {
        "checkpoint_id": checkpoint_id,
        "session_id": session_id,
        "sequence": sequence,
        "content_digest": content_digest(payload_json),
        "recorded_at": recorded_at_us,
    }


def close_session(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    session_id: str,
    expected_sequence: int | None,
    final_checkpoint: Mapping[str, Any] | None,
    final_checkpoint_id: str,
    closed_at_us: int,
) -> dict[str, Any]:
    """Close one session, optionally committing its final checkpoint atomically.

    The expected sequence is a mutation precondition against the session's last
    acknowledged checkpoint: a caller that watched another writer advance the
    session re-reads and re-decides rather than replacing the newer checkpoint.
    """
    session = _require_active_session(
        connection, workspace_id=workspace_id, session_id=session_id
    )
    last_sequence = session["last_checkpoint_sequence"] or 0
    if expected_sequence is not None and int(expected_sequence) != last_sequence:
        raise SequencePreconditionFailed(
            f"expected sequence {expected_sequence}, session head is {last_sequence}"
        )
    receipt: dict[str, Any] | None = None
    if final_checkpoint is not None:
        receipt = append_checkpoint(
            connection,
            settlement,
            workspace_id=workspace_id,
            checkpoint_id=final_checkpoint_id,
            session_id=session_id,
            parent_checkpoint_id=session["last_checkpoint_id"],
            expected_parent_sequence=None,
            checkpoint_kind=str(final_checkpoint.get("checkpoint_kind", "session_close")),
            payload=final_checkpoint,
            recorded_at_us=closed_at_us,
        )
    connection.execute(
        f"UPDATE {_SESSIONS_TABLE} SET state = 'closed', closed_at_us = ? "
        "WHERE workspace_id = ? AND session_id = ?",
        (int(closed_at_us), workspace_id, session_id),
    )
    return {
        "session_id": session_id,
        "state": "closed",
        "checkpoint_recorded": receipt is not None,
        "receipt": receipt,
    }


def read_checkpoint(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    checkpoint_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        f"SELECT checkpoint_id, session_id, sequence, parent_checkpoint_id, "
        "checkpoint_kind, payload_json, content_digest, recorded_at_us "
        f"FROM {_CHECKPOINTS_TABLE} WHERE workspace_id = ? AND checkpoint_id = ?",
        (workspace_id, checkpoint_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "checkpoint_id": row[0],
        "session_id": row[1],
        "sequence": row[2],
        "parent_checkpoint_id": row[3],
        "checkpoint_kind": row[4],
        "payload": json.loads(row[5]),
        "content_digest": row[6],
        "recorded_at_us": row[7],
    }
