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

A session belongs to the principal that registered it. Every read here names
that principal in its SQL, and append and close re-read the session that way
inside their fenced transaction before writing. Another principal's session or
checkpoint is indistinguishable from a missing one: `SessionNotFound` or `None`,
never a payload, id, count or position. There is no sharing grant yet, so
continuity is same-principal only.

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

#: Checkpoints whose session belongs to one principal; binds (workspace, principal).
#: Every checkpoint read goes through it, so another principal's payload is never
#: loaded and never counted.
_OWNED_CHECKPOINTS: Final = (
    f"FROM {_CHECKPOINTS_TABLE} c JOIN {_SESSIONS_TABLE} s "
    "ON s.workspace_id = c.workspace_id AND s.session_id = c.session_id "
    "WHERE c.workspace_id = ? AND s.principal_id = ?"
)

#: The default checkpoint payload cap: 256 KiB of canonical UTF-8 (spec §9.2).
CHECKPOINT_PAYLOAD_CAP_BYTES: Final = 262144

#: The default session lease: 24 hours. Append and close refuse once the lease
#: has expired against the mutation's own settlement instant. Lease refresh and
#: revocation belong to a later lifecycle slice.
SESSION_LEASE_SECONDS: Final = 24 * 60 * 60


class SessionNotFound(LookupError):
    """No such continuity session in this workspace for this principal."""


class SessionNotActive(RuntimeError):
    """The continuity session exists but is not writable: not `active`, or its
    lease expired at or before this mutation's settlement instant."""


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
    principal_id: str,
) -> dict[str, Any] | None:
    """The session if `principal_id` owns it; another principal's reads as `None`."""
    row = connection.execute(
        f"SELECT session_id, principal_id, state, binding_generation, "
        "lease_expires_at_us, host_session_ref, checkout_hint, "
        "repository_target_json, registered_at_us, closed_at_us, "
        "last_checkpoint_sequence, last_checkpoint_id "
        f"FROM {_SESSIONS_TABLE} "
        "WHERE workspace_id = ? AND session_id = ? AND principal_id = ?",
        (workspace_id, session_id, principal_id),
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


def _require_writable_session(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    session_id: str,
    principal_id: str,
) -> dict[str, Any]:
    """The session, if it is owned, `active`, and its lease has not expired.

    The lease is compared against `settlement.settled_at_us`, the server's own
    settlement instant for this fenced mutation, never a caller-supplied time.
    An expired lease is refused the same way an inactive session is -- no new
    response code, and no `expired` state is persisted here; the row's `state`
    stays exactly what it was.
    """
    session = read_session(
        connection,
        workspace_id=workspace_id,
        session_id=session_id,
        principal_id=principal_id,
    )
    if session is None:
        raise SessionNotFound(session_id)
    if session["state"] != "active":
        raise SessionNotActive(session["state"])
    if session["lease_expires_at_us"] <= settlement.settled_at_us:
        raise SessionNotActive("expired")
    return session


def append_checkpoint(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    principal_id: str,
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
    or not at all: an oversized payload raises before anything is written. A
    session `principal_id` does not own is `SessionNotFound`, checked here under
    the fence whatever the caller checked before. A session whose lease expired
    at or before `settlement.settled_at_us` is `SessionNotActive`, checked before
    the sequence precondition and before any row is written.
    """
    session = _require_writable_session(
        connection,
        settlement,
        workspace_id=workspace_id,
        session_id=session_id,
        principal_id=principal_id,
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
    principal_id: str,
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
    Only `principal_id`'s own session closes; any other is `SessionNotFound`. A
    session whose lease expired at or before `settlement.settled_at_us` is
    `SessionNotActive`, checked before the sequence precondition, before the
    final checkpoint (if any) and before the session's state is written -- the
    close is refused whole, exactly as the checkpoint-only path is.
    """
    session = _require_writable_session(
        connection,
        settlement,
        workspace_id=workspace_id,
        session_id=session_id,
        principal_id=principal_id,
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
            principal_id=principal_id,
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
    principal_id: str,
    checkpoint_id: str | None = None,
    session_id: str | None = None,
    sequence: int | None = None,
) -> dict[str, Any] | None:
    """One checkpoint by id, or by session and sequence, if `principal_id` owns
    its session. Ownership is decided by the same SQL that loads the payload, so
    another principal's checkpoint is never loaded and reads as `None`."""
    values: tuple[Any, ...]
    if checkpoint_id is not None:
        key, values = "c.checkpoint_id = ?", (checkpoint_id,)
    else:
        key, values = "c.session_id = ? AND c.sequence = ?", (session_id, sequence)
    row = connection.execute(
        "SELECT c.checkpoint_id, c.session_id, c.sequence, c.parent_checkpoint_id, "
        "c.checkpoint_kind, c.payload_json, c.content_digest, c.recorded_at_us "
        f"{_OWNED_CHECKPOINTS} AND {key}",
        (workspace_id, principal_id, *values),
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


def read_checkpoints(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    principal_id: str,
    limit: int = -1,
) -> list[Any]:
    """`principal_id`'s own checkpoints, newest first, as `(checkpoint_id,
    sequence, payload_json)` rows. Ownership is filtered in SQL before the order
    and `limit` apply (SQLite reads `LIMIT -1` as no limit)."""
    return connection.execute(
        f"SELECT c.checkpoint_id, c.sequence, c.payload_json {_OWNED_CHECKPOINTS} "
        "ORDER BY c.recorded_at_us DESC, c.sequence DESC, c.checkpoint_id LIMIT ?",
        (workspace_id, principal_id, limit),
    ).fetchall()
