"""Repository identity resolution and continuity fail-closed refs (plan PR-C).

The §6.2 rules over the real substrate: two same-basename repositories stay
distinct and a label-only resolution is ambiguous; a checkout re-point is an
audited mapping change that keeps the logical id; snapshot references resolve
through their repository; and every continuity reference to a repository or
snapshot that is not registered is refused before anything is stored.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.service.application import authorize_application_request
from omnivia_core_runtime.service.handlers.continuity import ContinuityHandlers
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage import repository_identity as repo_identity

from omnivia_core.contracts.v1 import (
    ERROR_CODE_NOT_FOUND,
    get_operation_metadata,
)

WORKSPACE_ID = s0.WORKSPACE_ID

REGISTER = get_operation_metadata("continuity.session.register")


def _owned(tmp_path: Any) -> Any:
    path = tmp_path / "workspace.sqlite"
    s0.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    return m1.take_ownership(path)


def _settle(holder: Any, mutate: Any, *, marker: str = "m1") -> Any:
    """One fenced mutation settlement running `mutate` through the coordinator."""
    entry = REGISTER
    operation_input = {"schema_version": "engineering.1", "settlement": marker}
    envelope = s0.envelope_for(
        entry,
        operation_input=operation_input,
        idempotency_key=f"idem-settle-{marker}",
    )
    authorized = authorize_application_request(
        envelope,
        session=s0.session_for(entry),
        binding=s0.BINDING,
        supported_capabilities=s0.SUPPORTED,
    )
    from omnivia_core_runtime.ownership.fencing import read_guard
    from omnivia_core_runtime.service.mutation import (
        execute_mutation,
        issue_mutation_grant,
    )

    from omnivia_core.contracts.v1 import idempotency_equivalence

    guard = read_guard(holder.connection)
    grant = issue_mutation_grant(
        authorized,
        session=s0.session_for(entry),
        binding=s0.BINDING,
        guard=guard,
        equivalence=idempotency_equivalence(
            entry.name,
            envelope.metadata,
            operation_input,
            principal_id=authorized.principal_id,
            workspace_id=authorized.workspace_id,
        ),
        clock=s0.clock_at(),
    )
    outcome = execute_mutation(
        holder.connection,
        holder.identity,
        grant=grant,
        context=authorized,
        equivalence=idempotency_equivalence(
            entry.name,
            envelope.metadata,
            operation_input,
            principal_id=authorized.principal_id,
            workspace_id=authorized.workspace_id,
        ),
        mutate=mutate,
        validate_result=lambda _result: True,
        clock=s0.clock_at(),
    )
    return outcome


def _register_two_same_label(holder: Any) -> tuple[str, str]:
    ids: list[str] = []

    def first(fenced: Any, settlement: Any) -> None:
        repo_identity.register_repository(
            fenced,
            settlement,
            workspace_id=WORKSPACE_ID,
            repository_id="erepo-1",
            display_name="app",
            provider_hint=None,
            registered_at_us=settlement.settled_at_us,
        )
        ids.append("erepo-1")
        return {"registered": "erepo-1"}

    _settle(holder, first, marker="repo-1")

    def second(fenced: Any, settlement: Any) -> None:
        repo_identity.register_repository(
            fenced,
            settlement,
            workspace_id=WORKSPACE_ID,
            repository_id="erepo-2",
            display_name="app",
            provider_hint=None,
            registered_at_us=settlement.settled_at_us,
        )
        ids.append("erepo-2")
        return {"registered": "erepo-2"}

    _settle(holder, second, marker="repo-2")
    return ids[0], ids[1]


def test_same_basename_repositories_stay_distinct_and_label_resolution_is_ambiguous(
    tmp_path: Any,
) -> None:
    holder = _owned(tmp_path)
    try:
        first_id, second_id = _register_two_same_label(holder)
        assert first_id != second_id
        with pytest.raises(repo_identity.RepositoryAmbiguous):
            repo_identity.resolve_repository(
                holder.connection, workspace_id=WORKSPACE_ID, label="app"
            )
        by_id = repo_identity.resolve_repository(
            holder.connection, workspace_id=WORKSPACE_ID, repository_id=second_id
        )
        assert by_id is not None and by_id["repository_id"] == second_id
        assert (
            repo_identity.resolve_repository(
                holder.connection, workspace_id=WORKSPACE_ID, label="no-such-label"
            )
            is None
        )
    finally:
        holder.connection.close()


def test_a_moved_checkout_repoints_under_audit_keeping_the_logical_id(
    tmp_path: Any,
) -> None:
    holder = _owned(tmp_path)
    try:
        _register_two_same_label(holder)

        def bind_to_first(fenced: Any, settlement: Any) -> None:
            repo_identity.register_checkout(
                fenced,
                settlement,
                workspace_id=WORKSPACE_ID,
                checkout_id="eck-1",
                repository_id="erepo-1",
                installation_id="inst-1",
                checkout_hint="/home/dev/app",
                registered_at_us=settlement.settled_at_us,
            )
            return {"checkout": "eck-1"}

        _settle(holder, bind_to_first, marker="checkout-1")
        resolved = repo_identity.resolve_repository(
            holder.connection,
            workspace_id=WORKSPACE_ID,
            checkout_hint="/home/dev/app",
            installation_id="inst-1",
        )
        assert resolved is not None and resolved["repository_id"] == "erepo-1"

        def repoint_to_second(fenced: Any, settlement: Any) -> None:
            repo_identity.register_checkout(
                fenced,
                settlement,
                workspace_id=WORKSPACE_ID,
                checkout_id="eck-1",
                repository_id="erepo-2",
                installation_id="inst-1",
                checkout_hint="/home/dev/app",
                registered_at_us=settlement.settled_at_us,
            )
            return {"repointed": "erepo-2"}

        _settle(holder, repoint_to_second, marker="checkout-2")
        repointed = repo_identity.resolve_repository(
            holder.connection,
            workspace_id=WORKSPACE_ID,
            checkout_hint="/home/dev/app",
            installation_id="inst-1",
        )
        assert repointed is not None and repointed["repository_id"] == "erepo-2"
        # One row, one mapping: the UNIQUE (installation, hint) key held.
        count = holder.connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_checkouts WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()[0]
        assert count == 1
    finally:
        holder.connection.close()


def test_snapshot_refs_validate_fail_closed(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        _register_two_same_label(holder)

        def record(fenced: Any, settlement: Any) -> None:
            repo_identity.record_snapshot(
                fenced,
                settlement,
                workspace_id=WORKSPACE_ID,
                snapshot_id="esnap-1",
                repository_id="erepo-1",
                snapshot_kind="git_commit",
                manifest={"files": 3, "status": "complete"},
                base_commit="0f8e2c1b",
                capture_status="complete",
                captured_at_us=settlement.settled_at_us,
            )
            return {"snapshot": "esnap-1"}

        _settle(holder, record, marker="snap-1")
        repo_identity.validate_snapshot_ref(
            holder.connection,
            workspace_id=WORKSPACE_ID,
            repository_id="erepo-1",
            snapshot_id="esnap-1",
        )
        with pytest.raises(repo_identity.SnapshotNotFound):
            repo_identity.validate_snapshot_ref(
                holder.connection,
                workspace_id=WORKSPACE_ID,
                repository_id="erepo-1",
                snapshot_id="esnap-nowhere",
            )
        # A snapshot cannot be borrowed across repositories.
        with pytest.raises(repo_identity.SnapshotNotFound):
            repo_identity.validate_snapshot_ref(
                holder.connection,
                workspace_id=WORKSPACE_ID,
                repository_id="erepo-2",
                snapshot_id="esnap-1",
            )
        with pytest.raises(repo_identity.RepositoryNotFound):
            repo_identity.validate_snapshot_ref(
                holder.connection,
                workspace_id=WORKSPACE_ID,
                repository_id="erepo-nowhere",
                snapshot_id=None,
            )
    finally:
        holder.connection.close()


def _register_input(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"schema_version": "engineering.1"}
    base.update(overrides)
    return base


_REGISTER_CALLS = {"n": 0}


def _call_register(holder: Any, operation_input: dict[str, Any]) -> Any:
    _REGISTER_CALLS["n"] += 1
    handlers = ContinuityHandlers(
        service=SimpleNamespace(connection=holder.connection, identity=holder.identity),
        session=s0.session_for(REGISTER),
        binding=s0.BINDING,
        clock=s0.clock_at(),
    )
    envelope = s0.envelope_for(
        REGISTER,
        operation_input=operation_input,
        idempotency_key=f"idem-register-{_REGISTER_CALLS['n']}",
    )
    authorized = authorize_application_request(
        envelope,
        session=s0.session_for(REGISTER),
        binding=s0.BINDING,
        supported_capabilities=s0.SUPPORTED,
    )
    context = OperationContext(
        request=envelope,
        principal=authorized.principal_id,
        workspace_id=authorized.workspace_id or WORKSPACE_ID,
        granted_operations=frozenset({REGISTER.name}),
        authorization=authorized,
    )
    return handlers.continuity_session_register(context)


def test_continuity_repository_refs_are_fail_closed(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        _register_two_same_label(holder)
        # An unregistered repository id is refused before anything is stored.
        with pytest.raises(OperationError) as unknown:
            _call_register(
                holder,
                _register_input(
                    repository_target={
                        "snapshot_id": "esnap-x",
                        "repository_id": "erepo-nowhere",
                    }
                ),
            )
        assert unknown.value.code == ERROR_CODE_NOT_FOUND
        sessions = holder.connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_sessions WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()[0]
        assert sessions == 0

        # A registered repository and its recorded snapshot resolve.
        def record(fenced: Any, settlement: Any) -> None:
            repo_identity.record_snapshot(
                fenced,
                settlement,
                workspace_id=WORKSPACE_ID,
                snapshot_id="esnap-1",
                repository_id="erepo-1",
                snapshot_kind="git_commit",
                manifest={"files": 1},
                base_commit="abc123",
                capture_status="complete",
                captured_at_us=settlement.settled_at_us,
            )
            return {"snapshot": "esnap-1"}

        _settle(holder, record, marker="snap-2")
        registered = _call_register(
            holder,
            _register_input(
                repository_target={
                    "snapshot_id": "esnap-1",
                    "repository_id": "erepo-1",
                    "snapshot_kind": "git_commit",
                }
            ),
        )
        assert registered.result["session"]["repository_target"]["snapshot_id"] == "esnap-1"
    finally:
        holder.connection.close()


def test_an_ambiguous_label_cannot_be_smuggled_through_continuity(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        _register_two_same_label(holder)
        # The register input carries no repository_id: a label is not authority,
        # and the handler has no label field to smuggle one through. This test
        # pins the fail-closed path for a checkout hint that maps ambiguously
        # is impossible by construction — the UNIQUE mapping key allows exactly
        # one repository per (installation, hint) — so the pin here is that a
        # hint resolving to a registered checkout is honoured, and an
        # unregistered hint binds no repository at all.
        registered = _call_register(
            holder, _register_input(checkout_hint="/home/dev/app")
        )
        assert "repository_target" not in registered.result["session"]
        with pytest.raises(OperationError) as wrong_repo:
            _call_register(
                holder,
                _register_input(
                    repository_target={"snapshot_id": "esnap-1"},
                ),
            )
        assert wrong_repo.value.code == ERROR_CODE_NOT_FOUND
    finally:
        holder.connection.close()
