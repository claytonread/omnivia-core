"""The engineering continuity vertical, over the real substrate (plan PR-B).

Register → append → close → handoff, driven through the real handlers with the
real twelve-check authorisation seam, the real guard triggers of migration 0048
and the real mutation coordinator: a replayed key returns the original receipt,
a competing successor loses as a precondition failure, an append to a closed
session is a conflict, an oversized payload is refused whole, and the durable
receipt survives a service-restart-shaped reopen of the same database.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.service.application import (
    authorize_application_request,
)
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
)
from omnivia_core_runtime.service.handlers.continuity import ContinuityHandlers
from omnivia_core_runtime.service.operations import (
    OperationContext,
    OperationError,
)

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_MUTATION_PRECONDITION_FAILED,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    MutationPrecondition,
    get_operation_metadata,
)

WORKSPACE_ID = s0.WORKSPACE_ID

REGISTER = get_operation_metadata("continuity.session.register")
APPEND = get_operation_metadata("continuity.checkpoint.append")
CLOSE = get_operation_metadata("continuity.session.close")
HANDOFF = get_operation_metadata("continuity.handoff.read")


def _owned(tmp_path: Any) -> Any:
    path = tmp_path / "workspace.sqlite"
    s0.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    return m1.take_ownership(path)


def _session(entry: Any) -> AuthenticatedSession:
    return s0.session_for(entry)


def _handlers(holder: Any, entry: Any) -> ContinuityHandlers:
    return ContinuityHandlers(
        service=SimpleNamespace(
            connection=holder.connection, identity=holder.identity
        ),
        session=_session(entry),
        binding=s0.BINDING,
        clock=s0.clock_at(),
    )


def _context(
    holder: Any,
    entry: Any,
    operation_input: dict[str, Any],
    *,
    stated_version: str | None = None,
    idempotency_key: str | None = None,
) -> OperationContext:
    overrides: dict[str, Any] = {}
    if stated_version is not None:
        overrides["mutation_precondition"] = MutationPrecondition(
            record_version=stated_version
        )
    if idempotency_key is not None:
        overrides["idempotency_key"] = idempotency_key
    envelope = s0.envelope_for(entry, operation_input=operation_input, **overrides)
    authorized = authorize_application_request(
        envelope,
        session=_session(entry),
        binding=s0.BINDING,
        supported_capabilities=s0.SUPPORTED,
    )
    return OperationContext(
        request=envelope,
        principal=authorized.principal_id,
        workspace_id=authorized.workspace_id or WORKSPACE_ID,
        granted_operations=frozenset({entry.name}),
        authorization=authorized,
    )


def _register_input(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "engineering.1",
        "checkout_hint": "/home/dev/app",
        "host_session_ref": "host-conv-77",
    }
    base.update(overrides)
    return base


def _append_input(session_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": session_id,
        "payload": {
            "objective": "Investigate the session-restoration failure",
            "checkpoint_kind": "periodic",
            "observations": [
                {"statement": "Retries did not change the failure.", "support": "claimed"}
            ],
            "unresolved_work": ["Why does restore fail after credential validation?"],
            "next_actions": ["Inspect the session-invalidation path"],
        },
    }
    base.update(overrides)
    return base


def _close_input(session_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": session_id,
        "expected_sequence": 1,
        "final_checkpoint": {
            "objective": "Wrap up the investigation",
            "checkpoint_kind": "session_close",
            "unresolved_work": ["Root cause still unconfirmed"],
        },
    }
    base.update(overrides)
    return base


def _call(
    holder: Any,
    entry: Any,
    handler_name: str,
    operation_input: dict[str, Any],
    *,
    stated_version: str | None = None,
    idempotency_key: str | None = None,
) -> Any:
    handlers = _handlers(holder, entry)
    context = _context(
        holder,
        entry,
        operation_input,
        stated_version=stated_version,
        idempotency_key=idempotency_key,
    )
    return getattr(handlers, handler_name)(context)


def test_register_append_close_handoff_is_one_durable_vertical(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        registered = _call(holder, REGISTER, "continuity_session_register", _register_input())
        session = registered.result["session"]
        assert session["state"] == "active"
        assert session["principal_id"] == registered.result["session"]["principal_id"]
        session_id = session["session_id"]

        appended = _call(
            holder,
            APPEND,
            "continuity_checkpoint_append",
            _append_input(session_id),
            stated_version="seq-0",
        )
        receipt = appended.result["receipt"]
        assert receipt["sequence"] == 1
        assert receipt["content_digest"].startswith("sha256:")
        assert receipt["audit_reference"] == appended.audit_reference

        closed = _call(
            holder,
            CLOSE,
            "continuity_session_close",
            _close_input(session_id, expected_sequence=1),
            stated_version="seq-1",
        )
        assert closed.result["checkpoint_recorded"] is True
        assert closed.result["state"] == "closed"
        assert closed.result["receipt"]["sequence"] == 2

        handoff = _call(
            holder,
            HANDOFF,
            "continuity_handoff_read",
            {"checkpoint_id": closed.result["receipt"]["checkpoint_id"]},
        )
        view = handoff["handoff"]
        assert view["format_version"] == "continuity_handoff.v1"
        assert view["redacted"] is False
        assert view["applicability"] == "not_evaluated"
        assert "Root cause still unconfirmed" in view["unresolved_work"]

        # The durable receipt survives a reopen of the same database: the
        # acknowledged sequence and digest are still there after the service
        # stops and starts again (AC-021's storage half).
        holder.connection.close()
        reopened = m1.take_ownership(holder.path)
        try:
            row = reopened.connection.execute(
                "SELECT sequence, content_digest FROM omnivia_engineering_checkpoints "
                "WHERE workspace_id = ? AND checkpoint_id = ?",
                (WORKSPACE_ID, closed.result["receipt"]["checkpoint_id"]),
            ).fetchone()
            assert row is not None
            assert row[0] == 2
            assert row[1] == closed.result["receipt"]["content_digest"]
        finally:
            reopened.connection.close()
    finally:
        try:
            holder.connection.close()
        except sqlite3.ProgrammingError:
            pass


def test_a_replayed_register_key_returns_the_same_binding(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        first = _call(holder, REGISTER, "continuity_session_register", _register_input())
        again = _call(holder, REGISTER, "continuity_session_register", _register_input())
        assert again.result["session"]["session_id"] == first.result["session"]["session_id"]
        assert again.audit_reference == first.audit_reference
    finally:
        holder.connection.close()


def test_a_reused_key_with_a_different_payload_is_an_idempotency_conflict(
    tmp_path: Any,
) -> None:
    holder = _owned(tmp_path)
    try:
        _call(holder, REGISTER, "continuity_session_register", _register_input())
        with pytest.raises(OperationError) as conflict:
            _call(
                holder,
                REGISTER,
                "continuity_session_register",
                _register_input(checkout_hint="/home/dev/other-app"),
            )
        assert conflict.value.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
    finally:
        holder.connection.close()


def test_a_competing_successor_loses_as_a_precondition_failure(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        session_id = _call(
            holder, REGISTER, "continuity_session_register", _register_input()
        ).result["session"]["session_id"]
        _call(
            holder,
            APPEND,
            "continuity_checkpoint_append",
            _append_input(session_id),
            stated_version="seq-0",
        )

        # The session's head is sequence 1; a caller that still expects the
        # session to be empty is refused rather than silently replacing it.
        with pytest.raises(OperationError) as stale:
            _call(
                holder,
                CLOSE,
                "continuity_session_close",
                _close_input(session_id, expected_sequence=0),
                stated_version="seq-0",
            )
        assert stale.value.code == ERROR_CODE_MUTATION_PRECONDITION_FAILED

        # The same refusal arrives through append's own expected predecessor.
        with pytest.raises(OperationError) as racing:
            _call(
                holder,
                APPEND,
                "continuity_checkpoint_append",
                _append_input(session_id, expected_parent_sequence=0),
                stated_version="seq-0",
                idempotency_key="idem-append-competing",
            )
        assert racing.value.code == ERROR_CODE_MUTATION_PRECONDITION_FAILED
    finally:
        holder.connection.close()


def test_an_append_to_a_closed_session_is_a_conflict(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        session_id = _call(
            holder, REGISTER, "continuity_session_register", _register_input()
        ).result["session"]["session_id"]
        closed = _call(
            holder,
            CLOSE,
            "continuity_session_close",
            {
                "session_id": session_id,
                "expected_sequence": 0,
            },
            stated_version="seq-0",
        )
        assert closed.result["checkpoint_recorded"] is False
        with pytest.raises(OperationError) as closed_error:
            _call(
                holder,
                APPEND,
                "continuity_checkpoint_append",
                _append_input(session_id),
                stated_version="seq-0",
            )
        assert closed_error.value.code == ERROR_CODE_CONFLICT
    finally:
        holder.connection.close()


def test_an_oversized_payload_is_refused_whole(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        session_id = _call(
            holder, REGISTER, "continuity_session_register", _register_input()
        ).result["session"]["session_id"]
        bloated = _append_input(
            session_id,
            payload={
                "objective": "x" * 2000,
                "checkpoint_kind": "periodic",
                "unresolved_work": ["y" * 2000] * 200,
            },
        )
        with pytest.raises(OperationError) as too_big:
            _call(
                holder,
                APPEND,
                "continuity_checkpoint_append",
                bloated,
                stated_version="seq-0",
            )
        assert too_big.value.code == ERROR_CODE_SIZE_LIMIT_EXCEEDED
        count = holder.connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_checkpoints WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()[0]
        assert count == 0
    finally:
        holder.connection.close()


def test_an_unknown_checkpoint_is_not_found_and_unguarded_writes_refuse(
    tmp_path: Any,
) -> None:
    holder = _owned(tmp_path)
    try:
        with pytest.raises(OperationError) as missing:
            _call(
                holder,
                HANDOFF,
                "continuity_handoff_read",
                {"checkpoint_id": "ck-nowhere"},
            )
        assert missing.value.code == ERROR_CODE_NOT_FOUND

        with pytest.raises(sqlite3.DatabaseError):
            holder.connection.execute(
                "INSERT INTO omnivia_engineering_sessions "
                "(workspace_id, session_id, principal_id, state, binding_generation, "
                "lease_expires_at_us, registered_at_us, audit_ref) "
                "VALUES (?, 'esess-forced', 'user-42', 'active', 1, 1, 1, 'audit-x')",
                (WORKSPACE_ID,),
            )
    finally:
        holder.connection.close()
