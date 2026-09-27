"""The engineering continuity vertical, over the real substrate (plan PR-B).

Register → append → close → handoff, driven through the real handlers with the
real twelve-check authorisation seam, the real guard triggers of migration 0048
and the real mutation coordinator: a replayed key returns the original receipt,
a competing successor loses as a precondition failure, an append to a closed
session is a conflict, an oversized payload is refused whole, and the durable
receipt survives a service-restart-shaped reopen of the same database.

Two contributors sharing the workspace, driven through the production
application surface, each see and change only their own sessions and
checkpoints: in the continuity operations, the `working_context` search and
the `resume` pack. Another principal's are indistinguishable from missing ones.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_engineering_source_coverage as sc
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.service.application import (
    authorize_application_request,
    engineering_family_session,
)
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
)
from omnivia_core_runtime.service.handlers.continuity import ContinuityHandlers
from omnivia_core_runtime.service.operations import (
    OperationContext,
    OperationError,
)
from omnivia_core_runtime.service.pagination import PROCESS_CONTINUATION_TOKENS

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INVALID_REQUEST,
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


# --- principal isolation, through the production application surface ---------------

#: Two authenticated contributors in one workspace. There is no sharing grant.
OWNER = engineering_family_session(
    principal_id=sc.PRINCIPAL,
    installation_id=s0.INSTALLATION_ID,
    workspace_id=sc.WORKSPACE_ID,
)
OTHER = engineering_family_session(
    principal_id="intruder",
    installation_id=s0.INSTALLATION_ID,
    workspace_id=sc.WORKSPACE_ID,
)

#: Everything a continuity mutation settles; a refusal must leave all of it alone.
_SETTLED_TABLES = (
    "omnivia_engineering_sessions",
    "omnivia_engineering_checkpoints",
    "omnivia_idempotency_claims",
    "omnivia_application_audit_events",
    "omnivia_mutation_executions",
)


@pytest.fixture
def workspace(tmp_path: Any) -> Any:
    opened = sc.Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


def _stated(version: str) -> dict[str, Any]:
    return {"mutation_precondition": MutationPrecondition(record_version=version)}


def _settled(workspace: Any) -> list[list[Any]]:
    connection = workspace.holder.connection
    return [
        connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
        for table in _SETTLED_TABLES
    ]


def _session_with(
    workspace: Any, session: AuthenticatedSession, objectives: list[str]
) -> str:
    """Register a session as `session` and append one checkpoint per objective."""
    session_id = str(
        workspace.ok("continuity.session.register", _register_input(), session=session)[
            "session"
        ]["session_id"]
    )
    for head, objective in enumerate(objectives):
        workspace.ok(
            "continuity.checkpoint.append",
            _append_input(
                session_id, payload={"objective": objective, "checkpoint_kind": "periodic"}
            ),
            session=session,
            **_stated(f"seq-{head}"),
        )
    return session_id


def test_another_principal_cannot_append_to_or_close_a_session(workspace: Any) -> None:
    """The intruder names the owner's exact session id, states its true version
    and a stale one, and repeats itself. Every append and close is refused exactly
    as for a nonexistent session, before any stated version is compared and after
    the owner closes it, and settles nothing. The owner's own stated versions,
    replay and close are unchanged."""
    owner = _session_with(workspace, OWNER, ["Investigate the restore failure"])
    before = _settled(workspace)

    def attempts(session_id: str) -> list[tuple[str, str, str]]:
        return [
            workspace.refused(operation, payload, session=OTHER, **_stated(version))
            # The owner's true head, then a stale version.
            for version in ("seq-1", "seq-0")
            for operation, payload in (
                ("continuity.checkpoint.append", _append_input(session_id)),
                ("continuity.session.close", _close_input(session_id, expected_sequence=1)),
            )
        ]

    denied = attempts(owner)
    assert denied == attempts("esess-nowhere")
    assert {refusal[0] for refusal in denied} == {ERROR_CODE_NOT_FOUND}
    # A denial records no claim: one key repeats the same refusal, even for a
    # different body, rather than replaying or conflicting.
    for payload in (_append_input(owner), _append_input(owner, parent_checkpoint_id="eck-x")):
        assert workspace.refused(
            "continuity.checkpoint.append",
            payload,
            session=OTHER,
            key="idem-intruder",
            **_stated("seq-1"),
        ) == denied[0]
    assert _settled(workspace) == before

    stale = workspace.refused(
        "continuity.checkpoint.append", _append_input(owner), session=OWNER, **_stated("seq-0")
    )
    assert stale[0] == ERROR_CODE_MUTATION_PRECONDITION_FAILED
    appended = workspace.ok(
        "continuity.checkpoint.append",
        _append_input(owner),
        session=OWNER,
        key="idem-owner",
        **_stated("seq-1"),
    )
    assert appended["receipt"]["sequence"] == 2
    assert workspace.ok(
        "continuity.checkpoint.append",
        _append_input(owner),
        session=OWNER,
        key="idem-owner",
        **_stated("seq-1"),
    ) == appended
    closed = workspace.ok(
        "continuity.session.close",
        _close_input(owner, expected_sequence=2),
        session=OWNER,
        **_stated("seq-2"),
    )
    assert (closed["state"], closed["receipt"]["sequence"]) == ("closed", 3)

    # Closed, the session is still `not_found` to the intruder, never a conflict.
    assert attempts(owner) == denied
    assert workspace.refused(
        "continuity.checkpoint.append", _append_input(owner), session=OWNER, **_stated("seq-3")
    )[0] == ERROR_CODE_CONFLICT


def test_the_fenced_write_refuses_another_principal_whatever_the_precondition_saw(
    workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the precondition read blinded, append and close still resolve the
    session as the effective principal inside the fenced write."""
    from omnivia_core_runtime.service.handlers import continuity as handlers

    owner = _session_with(workspace, OWNER, ["Investigate the restore failure"])
    before = _settled(workspace)
    monkeypatch.setattr(handlers, "_session_version", lambda *_: "seq-1")
    for operation, payload in (
        ("continuity.checkpoint.append", _append_input(owner)),
        ("continuity.session.close", _close_input(owner, expected_sequence=1)),
    ):
        refusal = workspace.refused(operation, payload, session=OTHER, **_stated("seq-1"))
        assert refusal[0] == ERROR_CODE_NOT_FOUND
    assert _settled(workspace) == before


def test_another_principal_cannot_read_a_handoff_by_either_key(workspace: Any) -> None:
    """A checkpoint id, or a session and sequence, of another principal's session
    is `not_found` exactly as a nonexistent one; each principal reads its own by
    both keys."""
    missing = [
        workspace.refused("continuity.handoff.read", key, session=OWNER)
        for key in ({"checkpoint_id": "eck-nowhere"}, {"session_id": "esess-nowhere", "sequence": 1})
    ]
    assert {refusal[0] for refusal in missing} == {ERROR_CODE_NOT_FOUND}

    keys: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for session in (OWNER, OTHER):
        objective = f"Objective of {session.principal_id}"
        by_sequence = {"session_id": _session_with(workspace, session, [objective]), "sequence": 1}
        view = workspace.ok("continuity.handoff.read", by_sequence, session=session)["handoff"]
        by_id = {"checkpoint_id": view["checkpoint_id"]}
        assert workspace.ok("continuity.handoff.read", by_id, session=session)["handoff"] == view
        assert view["objective"] == objective
        keys[session.principal_id] = (by_id, by_sequence)

    for reader, other in ((OWNER, OTHER), (OTHER, OWNER)):
        assert [
            workspace.refused("continuity.handoff.read", key, session=reader)
            for key in keys[other.principal_id]
        ] == missing


def test_working_context_search_reads_only_the_callers_checkpoints(workspace: Any) -> None:
    """Another principal's newer matching checkpoints change nothing the owner's
    search returns: not the matches, their order, the page count, nor the
    continuation's offset or snapshot. Each principal sees only its own, and a
    continuation is bound to the principal it was issued to."""
    _session_with(workspace, OWNER, [f"Restore session {n}" for n in ("alpha", "beta", "gamma")])

    def search(session: AuthenticatedSession, **extra: Any) -> dict[str, Any]:
        payload = {"query": "restore session", "view": "working_context", **extra}
        return workspace.ok("engineering.search", payload, session=session)

    def previews(result: dict[str, Any], field: str = "record_id") -> list[str]:
        return [preview[field] for preview in result["previews"]]

    everything = search(OWNER)
    first = search(OWNER, limit=2)
    token = first["page"]["continuation_token"]

    _session_with(workspace, OTHER, [f"Restore session XYZZY {n}" for n in range(4)])

    second = search(OWNER, limit=2, page={"continuation_token": token})
    assert previews(everything, "preview") == [
        "Restore session gamma",
        "Restore session beta",
        "Restore session alpha",
    ]
    assert previews(first) + previews(second) == previews(everything)
    assert second["page"] == {}
    assert search(OWNER) == everything
    # Seven checkpoints match in the workspace; the owner's three fit one page.
    assert search(OWNER, limit=3)["page"] == {}
    again = search(OWNER, limit=2)
    assert previews(again) == previews(first)
    issued, reissued = (
        PROCESS_CONTINUATION_TOKENS.decode(result["page"]["continuation_token"])
        for result in (first, again)
    )
    assert (reissued["o"], reissued["s"]) == (issued["o"], issued["s"])

    assert previews(search(OTHER), "preview") == [
        f"Restore session XYZZY {n}" for n in (3, 2, 1, 0)
    ]
    assert workspace.refused(
        "engineering.search",
        {
            "query": "restore session",
            "view": "working_context",
            "limit": 2,
            "page": {"continuation_token": token},
        },
        session=OTHER,
    )[0] == ERROR_CODE_INVALID_REQUEST


def test_the_resume_pack_reads_only_the_callers_checkpoints(workspace: Any) -> None:
    """The `resume` pack's working context is the caller's own five newest
    checkpoints. Another principal's newer ones take no slot, section, omission,
    budget or byte of the rendering, and the owner's never reach theirs."""
    _session_with(workspace, OWNER, [f"Owner step {n}" for n in range(1, 7)])
    build = {
        "query": "provider",
        "targets": [{"snapshot_id": "esnap-a", "snapshot_kind": "git_commit"}],
        "profile": "resume",
    }

    def pack(session: AuthenticatedSession) -> dict[str, Any]:
        return workspace.ok("engineering.context.build", build, session=session)["pack"]

    def objectives(built: dict[str, Any]) -> list[str]:
        return [
            section["content"].partition(" Unresolved:")[0]
            for section in built["sections"]
            if section["partition"] == "working_context"
        ]

    before = pack(OWNER)
    assert objectives(before) == [f"Owner step {n}" for n in (6, 5, 4, 3, 2)]
    assert len(before["sections"]) <= 24
    expected_checkpoint_bytes = sum(
        int(row[0])
        for row in workspace.holder.connection.execute(
            "SELECT length(CAST(c.payload_json AS BLOB)) "
            "FROM omnivia_engineering_checkpoints c "
            "JOIN omnivia_engineering_sessions s "
            "ON s.workspace_id = c.workspace_id AND s.session_id = c.session_id "
            "WHERE c.workspace_id = ? AND s.principal_id = ? "
            "ORDER BY c.recorded_at_us DESC, c.sequence DESC, c.checkpoint_id LIMIT 5",
            (sc.WORKSPACE_ID, OWNER.principal_id),
        )
    )
    assert before["budget"]["source_bytes_read"] == expected_checkpoint_bytes

    _session_with(workspace, OTHER, [f"XYZZY step {n}" for n in range(1, 6)])

    after = pack(OWNER)
    for field in ("sections", "citations", "omissions", "uncertainties", "rendering", "budget"):
        assert after[field] == before[field]
    assert "XYZZY" not in json.dumps(after)
    theirs = pack(OTHER)
    assert objectives(theirs) == [f"XYZZY step {n}" for n in (5, 4, 3, 2, 1)]
    assert "Owner step" not in json.dumps(theirs)


def test_resume_payload_budget_refuses_before_checkpoint_json_is_selected(
    workspace: Any,
) -> None:
    _session_with(workspace, OWNER, ["A checkpoint larger than one byte"])
    statements: list[str] = []
    workspace.holder.connection.set_trace_callback(statements.append)
    try:
        refusal = workspace.refused(
            "engineering.context.build",
            {
                "query": "provider",
                "targets": [],
                "profile": "resume",
                "budget": {"evidence_bytes": 1},
            },
            session=OWNER,
        )
    finally:
        workspace.holder.connection.set_trace_callback(None)
    assert refusal[0] == ERROR_CODE_SIZE_LIMIT_EXCEEDED
    assert not any(
        "SELECT c.checkpoint_id, c.sequence, c.payload_json" in statement
        for statement in statements
    )
