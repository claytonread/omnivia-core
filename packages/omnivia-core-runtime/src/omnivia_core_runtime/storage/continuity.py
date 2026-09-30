"""Engineering continuity records (SPEC-CORE-ENGMEM-001, plan PR-B; spec §7, §9).

Every write here runs inside the fenced mutation transaction the service's
mutation coordinator opens, exactly as the decision family's storage does: this
module holds no connection, no lease and no clock of its own. The guard triggers
migration 0048 carries make an unguarded write impossible, and the workspace
writer generation is enforced there — the session's own binding generation is
recorded on the row and enforced by the callers of this module.

Migration 0048 owns `omnivia_engineering_sessions` and the immutable checkpoint
chain.  Migration 0060 adds append-only lifecycle history plus one current
authority pointer for each trusted adapter association.  The pointer is the
server-side fence that makes an older registration response historical after a
renewal or reconnect.  This module is the only writer; handlers never spell SQL
against these tables.

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

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core_runtime.storage.decisions import canonical_document, content_digest
from omnivia_core_runtime.storage.payload_budget import (
    PayloadLengthMismatch,
    PayloadReadBudget,
)

_SESSIONS_TABLE: Final = "omnivia_engineering_sessions"
_CHECKPOINTS_TABLE: Final = "omnivia_engineering_checkpoints"
_LIFECYCLE_TABLE: Final = "omnivia_engineering_session_lifecycle"
_AUTHORITY_TABLE: Final = "omnivia_engineering_session_authority"

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
#: has expired against the mutation's own settlement instant.  Service-owned
#: renew/revoke/expiry primitives below rotate or terminate the trusted binding.
SESSION_LEASE_SECONDS: Final = 24 * 60 * 60

_ASSOCIATION_REF_PREFIX: Final = "core-association.v1:"
_ASSOCIATION_GENERATION_FLOOR: Final = 2
_MAX_SQLITE_INTEGER: Final = 9_223_372_036_854_775_807


class SessionNotFound(LookupError):
    """No such continuity session in this workspace for this principal."""


class SessionNotActive(RuntimeError):
    """The continuity session exists but is not writable: not `active`, or its
    lease expired at or before this mutation's settlement instant."""


class SessionBindingMismatch(RuntimeError):
    """The authenticated continuity generation no longer names this binding."""


class SequencePreconditionFailed(RuntimeError):
    """The stated expected predecessor sequence is not the session's last."""


class ParentCheckpointMismatch(RuntimeError):
    """The named parent checkpoint is not the session's current head."""


class PayloadTooLarge(RuntimeError):
    """The canonical checkpoint payload exceeds the 256 KiB cap."""


@dataclass(frozen=True)
class AssociationRegistrationPrecondition:
    """The complete settled association state a reconnect observed.

    Registration carries this server-read token into the fenced transaction.  A
    renewal, termination, reconnect, or legacy-row insertion changes at least one
    field, so an older request cannot arrive later and rotate newer authority.
    """

    latest_generation: int
    current_session_id: str | None
    authority_generation: int | None
    authority_state: str | None
    authority_lease_expires_at_us: int | None
    authority_updated_at_us: int | None
    authority_audit_ref: str | None


def _association_prefix(association_key: str) -> str:
    digest = association_key.removeprefix("sha256:")
    if (
        not association_key.startswith("sha256:")
        or len(association_key) != len("sha256:") + 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("continuity association key is not a sha256 digest")
    return f"{_ASSOCIATION_REF_PREFIX}{association_key}:"


def _association_key_from_ref(host_session_ref: str | None) -> str | None:
    """Recover only the one-way server association key from stored correlation.

    The opaque host correlation remains unreadable.  Rows written before trusted
    association support, including values that merely resemble the prefix, do not
    become authority unless the complete bounded digest form is present.
    """
    if host_session_ref is None or not host_session_ref.startswith(
        _ASSOCIATION_REF_PREFIX
    ):
        return None
    tail = host_session_ref[len(_ASSOCIATION_REF_PREFIX) :]
    key, separator, correlation = tail.partition(":sha256:")
    if separator:
        # The association key itself contains the first colon.  Partitioning at
        # the correlation marker leaves ``sha256:<hex>`` in ``key``.
        candidate = key
        if len(correlation) != 64 or any(
            character not in "0123456789abcdef" for character in correlation
        ):
            return None
    else:
        key, separator, correlation = tail.partition(":none")
        candidate = key
        if correlation:
            return None
    try:
        if not separator or not host_session_ref.startswith(
            _association_prefix(candidate)
        ):
            return None
    except ValueError:
        return None
    return candidate


def associated_host_session_ref(
    association_key: str,
    host_session_ref: str | None,
) -> str:
    """Encode trusted association identity plus a safe host-correlation digest.

    The caller's opaque host reference remains correlation only: its digest is
    retained, while the equality key comes solely from server-established
    association state.
    """
    correlation = (
        "none"
        if host_session_ref is None
        else "sha256:" + hashlib.sha256(host_session_ref.encode("utf-8")).hexdigest()
    )
    return _association_prefix(association_key) + correlation


def read_association_registration_precondition(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    principal_id: str,
    association_key: str,
) -> AssociationRegistrationPrecondition:
    """Read the state a later fenced registration must compare exactly.

    Generation one is reserved for legacy/unassociated registrations.  This is
    the no-migration discriminator that prevents a historical caller-chosen host
    reference from being reinterpreted as authenticated association state.
    """
    prefix = _association_prefix(association_key)
    row = connection.execute(
        f"SELECT MAX(binding_generation) FROM {_SESSIONS_TABLE} "
        "WHERE workspace_id = ? AND principal_id = ? "
        "AND binding_generation >= ? "
        "AND substr(host_session_ref, 1, ?) = ?",
        (
            workspace_id,
            principal_id,
            _ASSOCIATION_GENERATION_FLOOR,
            len(prefix),
            prefix,
        ),
    ).fetchone()
    latest = None if row is None else row[0]
    latest_generation = (
        _ASSOCIATION_GENERATION_FLOOR - 1 if latest is None else int(latest)
    )
    authority = _read_association_authority(
        connection,
        workspace_id=workspace_id,
        principal_id=principal_id,
        association_key=association_key,
    )
    return AssociationRegistrationPrecondition(
        latest_generation=latest_generation,
        current_session_id=(
            None if authority is None else str(authority["session_id"])
        ),
        authority_generation=(
            None if authority is None else int(authority["binding_generation"])
        ),
        authority_state=None if authority is None else str(authority["state"]),
        authority_lease_expires_at_us=(
            None if authority is None else int(authority["lease_expires_at_us"])
        ),
        authority_updated_at_us=(
            None if authority is None else int(authority["updated_at_us"])
        ),
        authority_audit_ref=(
            None if authority is None else str(authority["audit_ref"])
        ),
    )


def _read_association_authority(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    principal_id: str,
    association_key: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        f"SELECT current_session_id, binding_generation, state, "
        "lease_expires_at_us, updated_at_us, audit_ref "
        f"FROM {_AUTHORITY_TABLE} WHERE workspace_id = ? "
        "AND principal_id = ? AND association_key = ?",
        (workspace_id, principal_id, association_key),
    ).fetchone()
    if row is None:
        return None
    return {
        "session_id": row[0],
        "binding_generation": int(row[1]),
        "state": row[2],
        "lease_expires_at_us": int(row[3]),
        "updated_at_us": int(row[4]),
        "audit_ref": row[5],
    }


def _next_lifecycle_sequence(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    session_id: str,
) -> int:
    row = connection.execute(
        f"SELECT MAX(event_sequence) FROM {_LIFECYCLE_TABLE} "
        "WHERE workspace_id = ? AND session_id = ?",
        (workspace_id, session_id),
    ).fetchone()
    latest = None if row is None else row[0]
    return 1 if latest is None else int(latest) + 1


def _append_lifecycle_event(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    session_id: str,
    event_type: str,
    association_key: str | None,
    binding_generation: int,
    state: str,
    lease_expires_at_us: int,
    prior_session_id: str | None = None,
) -> None:
    connection.execute(
        f"INSERT INTO {_LIFECYCLE_TABLE} "
        "(workspace_id, session_id, event_sequence, event_type, association_key, "
        "binding_generation, state, lease_expires_at_us, settled_at_us, "
        "prior_session_id, audit_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            session_id,
            _next_lifecycle_sequence(
                connection, workspace_id=workspace_id, session_id=session_id
            ),
            event_type,
            association_key,
            int(binding_generation),
            state,
            int(lease_expires_at_us),
            int(settlement.settled_at_us),
            prior_session_id,
            settlement.audit_ref,
        ),
    )


def read_associated_session(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    principal_id: str,
    association_key: str,
) -> dict[str, Any] | None:
    """Resolve the single settled authority for an authenticated adapter.

    No selector from the operation payload participates.  The authority table's
    primary key makes one association resolve to at most one current session.
    """
    _association_prefix(association_key)
    row = connection.execute(
        f"SELECT s.session_id, s.principal_id, s.state, s.binding_generation, "
        "s.lease_expires_at_us, s.host_session_ref, s.checkout_hint, "
        "s.repository_target_json, s.registered_at_us, s.closed_at_us, "
        "s.last_checkpoint_sequence, s.last_checkpoint_id "
        f"FROM {_AUTHORITY_TABLE} a JOIN {_SESSIONS_TABLE} s "
        "ON s.workspace_id = a.workspace_id "
        "AND s.session_id = a.current_session_id "
        "WHERE a.workspace_id = ? AND a.principal_id = ? "
        "AND a.association_key = ? "
        "AND s.principal_id = a.principal_id "
        "AND s.binding_generation = a.binding_generation "
        "AND s.state = a.state "
        "AND s.lease_expires_at_us = a.lease_expires_at_us",
        (workspace_id, principal_id, association_key),
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
    association_key: str | None = None,
    association_precondition: AssociationRegistrationPrecondition | None = None,
    host_session_ref: str | None,
    checkout_hint: str | None,
    repository_target: Mapping[str, Any] | None,
    registered_at_us: int,
) -> int:
    """Insert one active binding and settle its lifecycle authority atomically.

    Unassociated generation-one registrations retain the accepted v1 behavior.
    A trusted association ignores a caller-computed generation.  It compares the
    complete server-read association precondition, derives the next value under
    the fenced write, supersedes every older active association row, and moves
    the single current pointer only after the new row and history exist.
    """
    prior_session_id: str | None = None
    authority: dict[str, Any] | None = None
    if association_key is None:
        if association_precondition is not None:
            raise SessionBindingMismatch(
                "an unassociated registration cannot carry association authority"
            )
        if int(binding_generation) != 1:
            raise SessionBindingMismatch(
                "an unassociated continuity binding must use generation one"
            )
        settled_generation = 1
    else:
        prefix = _association_prefix(association_key)
        if association_precondition is None:
            raise SessionBindingMismatch(
                "continuity registration requires a settled association precondition"
            )
        observed_precondition = read_association_registration_precondition(
            connection,
            workspace_id=workspace_id,
            principal_id=principal_id,
            association_key=association_key,
        )
        if observed_precondition != association_precondition:
            raise SessionBindingMismatch(
                "continuity association advanced before registration settled"
            )
        if observed_precondition.latest_generation >= _MAX_SQLITE_INTEGER:
            raise SessionBindingMismatch("continuity binding generation is exhausted")
        settled_generation = observed_precondition.latest_generation + 1
        authority = _read_association_authority(
            connection,
            workspace_id=workspace_id,
            principal_id=principal_id,
            association_key=association_key,
        )
        if authority is not None:
            prior_session_id = str(authority["session_id"])
            if settled_generation != int(authority["binding_generation"]) + 1:
                raise SessionBindingMismatch(
                    "continuity association history is not contiguous"
                )
        else:
            prior = connection.execute(
                f"SELECT session_id FROM {_SESSIONS_TABLE} "
                "WHERE workspace_id = ? AND principal_id = ? "
                "AND binding_generation >= ? "
                "AND substr(host_session_ref, 1, ?) = ? "
                "ORDER BY binding_generation DESC, session_id ASC LIMIT 1",
                (
                    workspace_id,
                    principal_id,
                    _ASSOCIATION_GENERATION_FLOOR,
                    len(prefix),
                    prefix,
                ),
            ).fetchone()
            prior_session_id = None if prior is None else str(prior[0])

        # Pre-0060 databases can contain several active association rows and no
        # trusted current pointer.  Fence every one before the replacement row
        # is installed; their history remains readable and explicitly superseded.
        active_rows = connection.execute(
            f"SELECT session_id, binding_generation, lease_expires_at_us "
            f"FROM {_SESSIONS_TABLE} WHERE workspace_id = ? AND principal_id = ? "
            "AND state = 'active' AND binding_generation >= ? "
            "AND substr(host_session_ref, 1, ?) = ? "
            "ORDER BY binding_generation, session_id",
            (
                workspace_id,
                principal_id,
                _ASSOCIATION_GENERATION_FLOOR,
                len(prefix),
                prefix,
            ),
        ).fetchall()
        for old_session_id, old_generation, old_lease in active_rows:
            _append_lifecycle_event(
                connection,
                settlement,
                workspace_id=workspace_id,
                session_id=str(old_session_id),
                event_type="superseded",
                association_key=association_key,
                binding_generation=int(old_generation),
                state="revoked",
                lease_expires_at_us=int(old_lease),
                prior_session_id=None,
            )
            changed = connection.execute(
                f"UPDATE {_SESSIONS_TABLE} SET state = 'revoked' "
                "WHERE workspace_id = ? AND session_id = ? AND principal_id = ? "
                "AND binding_generation = ? AND state = 'active'",
                (
                    workspace_id,
                    old_session_id,
                    principal_id,
                    old_generation,
                ),
            )
            if changed.rowcount != 1:
                raise SessionBindingMismatch(
                    "continuity association changed during registration"
                )

    lease_expires_at_us = int(registered_at_us) + SESSION_LEASE_SECONDS * 1_000_000
    if association_key is not None and authority is not None:
        lease_expires_at_us = max(
            lease_expires_at_us,
            int(authority["lease_expires_at_us"]) + 1,
        )
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
            settled_generation,
            lease_expires_at_us,
            host_session_ref,
            checkout_hint,
            None if repository_target is None else canonical_document(dict(repository_target)),
            int(registered_at_us),
            settlement.audit_ref,
        ),
    )
    # Migration 0060's AFTER INSERT trigger writes the registered history row.
    # Keeping that coupling in SQLite means every guarded session insertion has
    # lifecycle history, including a future service writer that bypasses this
    # helper.  ``prior_session_id`` is derived there from the still-current
    # authority pointer; retain this assertion so code and trigger agree.
    history = connection.execute(
        f"SELECT prior_session_id FROM {_LIFECYCLE_TABLE} "
        "WHERE workspace_id = ? AND session_id = ? AND event_sequence = 1 "
        "AND event_type = 'registered'",
        (workspace_id, session_id),
    ).fetchone()
    if history is None or history[0] != prior_session_id:
        raise SessionBindingMismatch(
            "continuity registration history did not settle atomically"
        )
    if association_key is not None:
        authority = _read_association_authority(
            connection,
            workspace_id=workspace_id,
            principal_id=principal_id,
            association_key=association_key,
        )
        if authority is None:
            connection.execute(
                f"INSERT INTO {_AUTHORITY_TABLE} "
                "(workspace_id, principal_id, association_key, current_session_id, "
                "binding_generation, state, lease_expires_at_us, updated_at_us, "
                "audit_ref) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (
                    workspace_id,
                    principal_id,
                    association_key,
                    session_id,
                    settled_generation,
                    lease_expires_at_us,
                    int(settlement.settled_at_us),
                    settlement.audit_ref,
                ),
            )
        else:
            changed = connection.execute(
                f"UPDATE {_AUTHORITY_TABLE} SET current_session_id = ?, "
                "binding_generation = ?, state = 'active', lease_expires_at_us = ?, "
                "updated_at_us = ?, audit_ref = ? WHERE workspace_id = ? "
                "AND principal_id = ? AND association_key = ? "
                "AND current_session_id = ? AND binding_generation = ?",
                (
                    session_id,
                    settled_generation,
                    lease_expires_at_us,
                    int(settlement.settled_at_us),
                    settlement.audit_ref,
                    workspace_id,
                    principal_id,
                    association_key,
                    authority["session_id"],
                    authority["binding_generation"],
                ),
            )
            if changed.rowcount != 1:
                raise SessionBindingMismatch(
                    "continuity association changed during registration"
                )
    return settled_generation


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


def read_bound_session(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    session_id: str,
    principal_id: str,
    binding_generation: int,
) -> dict[str, Any] | None:
    """The owned session only when its stored generation matches the binding.

    Ownership stays hidden first: another principal's row is still ``None``.
    Once ownership is established, a stale generation is a binding conflict,
    not a missing-record oracle.
    """
    session = read_session(
        connection,
        workspace_id=workspace_id,
        session_id=session_id,
        principal_id=principal_id,
    )
    if session is None:
        return None
    if session["binding_generation"] != binding_generation:
        raise SessionBindingMismatch("continuity binding generation is stale")
    association_key: str | None = None
    if binding_generation >= _ASSOCIATION_GENERATION_FLOOR:
        association_key = _association_key_from_ref(session["host_session_ref"])
        if association_key is None:
            raise SessionBindingMismatch(
                "continuity binding has no trusted association authority"
            )
        authority = _read_association_authority(
            connection,
            workspace_id=workspace_id,
            principal_id=principal_id,
            association_key=association_key,
        )
        if (
            authority is None
            or authority["session_id"] != session_id
            or authority["binding_generation"] != binding_generation
            or authority["state"] != session["state"]
            or authority["lease_expires_at_us"] != session["lease_expires_at_us"]
        ):
            raise SessionBindingMismatch("continuity binding is no longer current")
    session["association_key"] = association_key
    return session


def _require_writable_session(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    session_id: str,
    principal_id: str,
    binding_generation: int,
) -> dict[str, Any]:
    """The session, if it is owned, `active`, and its lease has not expired.

    The lease is compared against `settlement.settled_at_us`, the server's own
    settlement instant for this fenced mutation, never a caller-supplied time.
    An expired lease is refused the same way an inactive session is -- no new
    response code, and no `expired` state is persisted here; the row's `state`
    stays exactly what it was.
    """
    session = read_bound_session(
        connection,
        workspace_id=workspace_id,
        session_id=session_id,
        principal_id=principal_id,
        binding_generation=binding_generation,
    )
    if session is None:
        raise SessionNotFound(session_id)
    if session["state"] != "active":
        raise SessionNotActive(session["state"])
    if session["lease_expires_at_us"] <= settlement.settled_at_us:
        raise SessionNotActive("expired")
    return session


def renew_associated_session(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    principal_id: str,
    association_key: str,
    session_id: str,
    binding_generation: int,
    lease_seconds: int = SESSION_LEASE_SECONDS,
) -> dict[str, Any]:
    """Rotate one current live association to generation ``n + 1``.

    This is a service-owned primitive rather than a model-facing operation.  Its
    caller must already be inside the authoritative fenced mutation and supply
    the trusted association key.  The old generation, a terminal state, or the
    exact expiry boundary all fail before either summary row changes.
    """
    _association_prefix(association_key)
    if type(lease_seconds) is not int or lease_seconds <= 0:
        raise ValueError("continuity lease duration must be a positive integer")
    session = read_bound_session(
        connection,
        workspace_id=workspace_id,
        session_id=session_id,
        principal_id=principal_id,
        binding_generation=binding_generation,
    )
    if session is None:
        raise SessionNotFound(session_id)
    if session.get("association_key") != association_key:
        raise SessionBindingMismatch("continuity association does not match binding")
    if session["state"] != "active":
        raise SessionNotActive(session["state"])
    if session["lease_expires_at_us"] <= settlement.settled_at_us:
        raise SessionNotActive("expired")
    if binding_generation >= _MAX_SQLITE_INTEGER:
        raise SessionBindingMismatch("continuity binding generation is exhausted")
    next_generation = binding_generation + 1
    next_lease = max(
        int(settlement.settled_at_us) + lease_seconds * 1_000_000,
        int(session["lease_expires_at_us"]) + 1,
    )
    _append_lifecycle_event(
        connection,
        settlement,
        workspace_id=workspace_id,
        session_id=session_id,
        event_type="renewed",
        association_key=association_key,
        binding_generation=next_generation,
        state="active",
        lease_expires_at_us=next_lease,
    )
    changed = connection.execute(
        f"UPDATE {_SESSIONS_TABLE} SET binding_generation = ?, "
        "lease_expires_at_us = ? WHERE workspace_id = ? AND session_id = ? "
        "AND principal_id = ? AND binding_generation = ? AND state = 'active' "
        "AND lease_expires_at_us > ?",
        (
            next_generation,
            next_lease,
            workspace_id,
            session_id,
            principal_id,
            binding_generation,
            int(settlement.settled_at_us),
        ),
    )
    if changed.rowcount != 1:
        raise SessionBindingMismatch("continuity binding changed during renewal")
    pointer = connection.execute(
        f"UPDATE {_AUTHORITY_TABLE} SET binding_generation = ?, "
        "lease_expires_at_us = ?, updated_at_us = ?, audit_ref = ? "
        "WHERE workspace_id = ? AND principal_id = ? AND association_key = ? "
        "AND current_session_id = ? AND binding_generation = ? AND state = 'active'",
        (
            next_generation,
            next_lease,
            int(settlement.settled_at_us),
            settlement.audit_ref,
            workspace_id,
            principal_id,
            association_key,
            session_id,
            binding_generation,
        ),
    )
    if pointer.rowcount != 1:
        raise SessionBindingMismatch("continuity authority changed during renewal")
    renewed = read_session(
        connection,
        workspace_id=workspace_id,
        session_id=session_id,
        principal_id=principal_id,
    )
    if renewed is None:  # pragma: no cover - protected by the same transaction
        raise SessionNotFound(session_id)
    return renewed


def _terminate_associated_session(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    principal_id: str,
    association_key: str,
    session_id: str,
    binding_generation: int,
    terminal_state: str,
) -> dict[str, Any]:
    if terminal_state not in {"expired", "revoked"}:
        raise ValueError("unsupported continuity terminal state")
    _association_prefix(association_key)
    session = read_session(
        connection,
        workspace_id=workspace_id,
        session_id=session_id,
        principal_id=principal_id,
    )
    if session is None:
        raise SessionNotFound(session_id)
    if session["binding_generation"] != binding_generation:
        raise SessionBindingMismatch("continuity binding generation is stale")
    if _association_key_from_ref(session["host_session_ref"]) != association_key:
        raise SessionBindingMismatch("continuity association does not match binding")
    authority = _read_association_authority(
        connection,
        workspace_id=workspace_id,
        principal_id=principal_id,
        association_key=association_key,
    )
    if (
        authority is None
        or authority["session_id"] != session_id
        or authority["binding_generation"] != binding_generation
    ):
        raise SessionBindingMismatch("continuity binding is no longer current")
    if session["state"] == terminal_state:
        return session
    if session["state"] != "active":
        raise SessionNotActive(session["state"])
    if terminal_state == "expired" and int(settlement.settled_at_us) < int(
        session["lease_expires_at_us"]
    ):
        raise SessionNotActive("lease_not_expired")
    _append_lifecycle_event(
        connection,
        settlement,
        workspace_id=workspace_id,
        session_id=session_id,
        event_type=terminal_state,
        association_key=association_key,
        binding_generation=binding_generation,
        state=terminal_state,
        lease_expires_at_us=int(session["lease_expires_at_us"]),
    )
    changed = connection.execute(
        f"UPDATE {_SESSIONS_TABLE} SET state = ? WHERE workspace_id = ? "
        "AND session_id = ? AND principal_id = ? AND binding_generation = ? "
        "AND state = 'active'",
        (
            terminal_state,
            workspace_id,
            session_id,
            principal_id,
            binding_generation,
        ),
    )
    if changed.rowcount != 1:
        raise SessionBindingMismatch("continuity binding changed during termination")
    pointer = connection.execute(
        f"UPDATE {_AUTHORITY_TABLE} SET state = ?, updated_at_us = ?, audit_ref = ? "
        "WHERE workspace_id = ? AND principal_id = ? AND association_key = ? "
        "AND current_session_id = ? AND binding_generation = ? AND state = 'active'",
        (
            terminal_state,
            int(settlement.settled_at_us),
            settlement.audit_ref,
            workspace_id,
            principal_id,
            association_key,
            session_id,
            binding_generation,
        ),
    )
    if pointer.rowcount != 1:
        raise SessionBindingMismatch("continuity authority changed during termination")
    terminated = read_session(
        connection,
        workspace_id=workspace_id,
        session_id=session_id,
        principal_id=principal_id,
    )
    if terminated is None:  # pragma: no cover - protected by the same transaction
        raise SessionNotFound(session_id)
    return terminated


def revoke_associated_session(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    principal_id: str,
    association_key: str,
    session_id: str,
    binding_generation: int,
) -> dict[str, Any]:
    """Atomically revoke the exact current trusted binding; repeated revoke is safe."""
    return _terminate_associated_session(
        connection,
        settlement,
        workspace_id=workspace_id,
        principal_id=principal_id,
        association_key=association_key,
        session_id=session_id,
        binding_generation=binding_generation,
        terminal_state="revoked",
    )


def expire_associated_session(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    principal_id: str,
    association_key: str,
    session_id: str,
    binding_generation: int,
) -> dict[str, Any]:
    """Materialize expiry only at or after the server-owned lease boundary."""
    return _terminate_associated_session(
        connection,
        settlement,
        workspace_id=workspace_id,
        principal_id=principal_id,
        association_key=association_key,
        session_id=session_id,
        binding_generation=binding_generation,
        terminal_state="expired",
    )


def append_checkpoint(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    principal_id: str,
    checkpoint_id: str,
    session_id: str,
    binding_generation: int,
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
        binding_generation=binding_generation,
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
    updated = connection.execute(
        f"UPDATE {_SESSIONS_TABLE} SET last_checkpoint_sequence = ?, "
        "last_checkpoint_id = ? WHERE workspace_id = ? AND session_id = ? "
        "AND principal_id = ? AND binding_generation = ? AND state = 'active'",
        (
            sequence,
            checkpoint_id,
            workspace_id,
            session_id,
            principal_id,
            binding_generation,
        ),
    )
    if updated.rowcount != 1:
        raise SessionBindingMismatch("continuity binding changed during append")
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
    binding_generation: int,
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
        binding_generation=binding_generation,
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
            binding_generation=binding_generation,
            parent_checkpoint_id=session["last_checkpoint_id"],
            expected_parent_sequence=None,
            checkpoint_kind=str(final_checkpoint.get("checkpoint_kind", "session_close")),
            payload=final_checkpoint,
            recorded_at_us=closed_at_us,
        )
    association_key = session.get("association_key")
    _append_lifecycle_event(
        connection,
        settlement,
        workspace_id=workspace_id,
        session_id=session_id,
        event_type="closed",
        association_key=association_key,
        binding_generation=binding_generation,
        state="closed",
        lease_expires_at_us=int(session["lease_expires_at_us"]),
    )
    updated = connection.execute(
        f"UPDATE {_SESSIONS_TABLE} SET state = 'closed', closed_at_us = ? "
        "WHERE workspace_id = ? AND session_id = ? AND principal_id = ? "
        "AND binding_generation = ? AND state = 'active'",
        (
            int(closed_at_us),
            workspace_id,
            session_id,
            principal_id,
            binding_generation,
        ),
    )
    if updated.rowcount != 1:
        raise SessionBindingMismatch("continuity binding changed during close")
    if association_key is not None:
        pointer = connection.execute(
            f"UPDATE {_AUTHORITY_TABLE} SET state = 'closed', updated_at_us = ?, "
            "audit_ref = ? WHERE workspace_id = ? AND principal_id = ? "
            "AND association_key = ? AND current_session_id = ? "
            "AND binding_generation = ? AND state = 'active'",
            (
                int(settlement.settled_at_us),
                settlement.audit_ref,
                workspace_id,
                principal_id,
                association_key,
                session_id,
                binding_generation,
            ),
        )
        if pointer.rowcount != 1:
            raise SessionBindingMismatch("continuity authority changed during close")
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
    bound_session_id: str,
    binding_generation: int,
    checkpoint_id: str | None = None,
    session_id: str | None = None,
    sequence: int | None = None,
) -> dict[str, Any] | None:
    """One checkpoint under the exact authenticated continuity binding.

    Ownership, bound session identity and binding generation are decided by the
    same SQL that loads the payload.  Another principal's checkpoint, another
    session owned by the same principal, and a stale generation therefore load
    no payload and all read as ``None``.
    """
    values: tuple[Any, ...]
    if checkpoint_id is not None:
        key, values = "c.checkpoint_id = ?", (checkpoint_id,)
    else:
        key, values = "c.session_id = ? AND c.sequence = ?", (session_id, sequence)
    row = connection.execute(
        "SELECT c.checkpoint_id, c.session_id, c.sequence, c.parent_checkpoint_id, "
        "c.checkpoint_kind, c.payload_json, c.content_digest, c.recorded_at_us "
        f"{_OWNED_CHECKPOINTS} AND s.session_id = ? "
        "AND s.binding_generation = ? "
        "AND (s.binding_generation = 1 OR EXISTS ("
        f"SELECT 1 FROM {_AUTHORITY_TABLE} a "
        "WHERE a.workspace_id = s.workspace_id "
        "AND a.principal_id = s.principal_id "
        "AND a.current_session_id = s.session_id "
        "AND a.binding_generation = s.binding_generation "
        "AND a.state = s.state "
        "AND a.lease_expires_at_us = s.lease_expires_at_us)) AND " + key,
        (
            workspace_id,
            principal_id,
            bound_session_id,
            binding_generation,
            *values,
        ),
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
    payload_budget: PayloadReadBudget | None = None,
) -> list[Any]:
    """`principal_id`'s own checkpoints, newest first, as `(checkpoint_id,
    sequence, payload_json)` rows. Ownership is filtered in SQL before the order
    and `limit` apply (SQLite reads `LIMIT -1` as no limit)."""
    order_and_limit = (
        "ORDER BY c.recorded_at_us DESC, c.sequence DESC, c.checkpoint_id LIMIT ?"
    )
    if payload_budget is None:
        return connection.execute(
            f"SELECT c.checkpoint_id, c.sequence, c.payload_json {_OWNED_CHECKPOINTS} "
            + order_and_limit,
            (workspace_id, principal_id, limit),
        ).fetchall()
    length_sql = payload_budget.byte_length_sql(connection, "c.payload_json")
    metadata = connection.execute(
        f"SELECT c.checkpoint_id, c.sequence, {length_sql} {_OWNED_CHECKPOINTS} "
        + order_and_limit,
        (workspace_id, principal_id, limit),
    ).fetchall()
    expected = [(str(row[0]), int(row[1]), int(row[2])) for row in metadata]
    if any(not 2 <= row[2] <= CHECKPOINT_PAYLOAD_CAP_BYTES for row in expected):
        raise PayloadLengthMismatch("a checkpoint payload byte length is invalid")
    payload_budget.precheck([row[2] for row in expected])
    rows = connection.execute(
        f"SELECT c.checkpoint_id, c.sequence, c.payload_json {_OWNED_CHECKPOINTS} "
        + order_and_limit,
        (workspace_id, principal_id, limit),
    ).fetchall()
    if [(str(row[0]), int(row[1])) for row in rows] != [
        (row[0], row[1]) for row in expected
    ]:
        raise PayloadLengthMismatch(
            "checkpoint metadata changed inside the read snapshot"
        )
    for row, (_checkpoint_id, _sequence, expected_bytes) in zip(
        rows, expected, strict=True
    ):
        payload_budget.consume(str(row[2]), expected_bytes)
    return rows


@dataclass(frozen=True, slots=True)
class CheckpointMetadata:
    """One checkpoint's identity, sequence and exact stored UTF-8 payload byte
    length -- never its `payload_json`."""

    checkpoint_id: str
    sequence: int
    payload_byte_length: int


def list_checkpoint_metadata(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    principal_id: str,
    payload_budget: PayloadReadBudget,
    limit: int = -1,
) -> tuple[CheckpointMetadata, ...]:
    """`principal_id`'s own checkpoint metadata, newest first, without ever
    selecting `payload_json` -- the same stable order `read_checkpoints` applies
    (`recorded_at_us DESC, sequence DESC, checkpoint_id`), so a caller planning a
    budget from this list picks from the same ordering a body read would return.
    """
    if not connection.in_transaction:
        raise ValueError(
            "checkpoint payload planning requires the caller's active read snapshot"
        )
    order_and_limit = (
        "ORDER BY c.recorded_at_us DESC, c.sequence DESC, c.checkpoint_id LIMIT ?"
    )
    length_sql = payload_budget.byte_length_sql(connection, "c.payload_json")
    rows = connection.execute(
        f"SELECT c.checkpoint_id, c.sequence, {length_sql} {_OWNED_CHECKPOINTS} "
        + order_and_limit,
        (workspace_id, principal_id, limit),
    ).fetchall()
    metadata = tuple(
        CheckpointMetadata(str(row[0]), int(row[1]), int(row[2])) for row in rows
    )
    if any(
        not 2 <= entry.payload_byte_length <= CHECKPOINT_PAYLOAD_CAP_BYTES
        for entry in metadata
    ):
        raise PayloadLengthMismatch("a checkpoint payload byte length is invalid")
    return metadata


def read_selected_checkpoints(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    principal_id: str,
    selected: tuple[CheckpointMetadata, ...],
    payload_budget: PayloadReadBudget,
) -> list[Any]:
    """Fetch exactly `selected`'s checkpoints, in `selected`'s own order -- never
    a checkpoint this caller did not choose from `list_checkpoint_metadata`.

    Re-verifies identity, sequence and byte length against a fresh read of
    exactly these ids before a single payload is decoded, so metadata that
    changed since it was listed is caught here rather than silently absorbed
    into the byte precheck. `payload_budget.precheck` runs before the body
    SELECT and `consume` after it, exactly as `read_checkpoints` already
    sequences the two.
    """
    if not connection.in_transaction:
        raise ValueError(
            "selected checkpoint reading requires the caller's active read snapshot"
        )
    if not selected:
        return []
    checkpoint_ids = tuple(entry.checkpoint_id for entry in selected)
    placeholders = ", ".join("?" for _ in checkpoint_ids)
    length_sql = payload_budget.byte_length_sql(connection, "c.payload_json")
    metadata_rows = connection.execute(
        f"SELECT c.checkpoint_id, c.sequence, {length_sql} {_OWNED_CHECKPOINTS} "
        f"AND c.checkpoint_id IN ({placeholders})",
        (workspace_id, principal_id, *checkpoint_ids),
    ).fetchall()
    fresh = {str(row[0]): (int(row[1]), int(row[2])) for row in metadata_rows}
    if len(fresh) != len(checkpoint_ids) or any(
        fresh.get(entry.checkpoint_id) != (entry.sequence, entry.payload_byte_length)
        for entry in selected
    ):
        raise PayloadLengthMismatch(
            "checkpoint metadata changed inside the read snapshot"
        )
    payload_budget.precheck([entry.payload_byte_length for entry in selected])
    rows = connection.execute(
        f"SELECT c.checkpoint_id, c.sequence, c.payload_json {_OWNED_CHECKPOINTS} "
        f"AND c.checkpoint_id IN ({placeholders})",
        (workspace_id, principal_id, *checkpoint_ids),
    ).fetchall()
    by_id = {str(row[0]): row for row in rows}
    if set(by_id) != set(checkpoint_ids):
        raise PayloadLengthMismatch(
            "checkpoint metadata changed inside the read snapshot"
        )
    ordered = [by_id[checkpoint_id] for checkpoint_id in checkpoint_ids]
    for row, entry in zip(ordered, selected, strict=True):
        payload_budget.consume(str(row[2]), entry.payload_byte_length)
    return ordered
