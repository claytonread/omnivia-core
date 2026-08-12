"""V06-5 CP-S0-I installation authority acceptance controls."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Any

import omnivia_core_runtime.service.installation as installation_module
import pytest
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    AuthorizedApplicationContext,
    ServiceBinding,
    authorize_application_request,
)
from omnivia_core_runtime.service.installation import (
    InstallationApplicationService,
    InstallationBootstrapInProgress,
    InstallationMutationDenied,
    InstallationMutationGrant,
    InstallationSeamFault,
)
from omnivia_core_runtime.service.mutation import (
    INSTALLATION_ADMINISTRATOR_ROLE,
    WORKSPACE_ADMINISTRATION_PURPOSE,
)
from omnivia_core_runtime.service.workspace_init import (
    WorkspaceInitRefusal,
    WorkspaceInitResult,
    WorkspaceInitStatus,
    initialise_allocated_workspace,
    initialise_workspace,
)
from omnivia_core_runtime.storage.installation_migrations import (
    PINNED_INSTALLATION_MIGRATIONS,
    load_installation_migrations,
)
from omnivia_core_runtime.storage.installation_store import (
    AllocationState,
    InstallationAuthorityError,
    InstallationBusy,
    InstallationIdempotencyConflict,
    InstallationStore,
    InstallationStoreError,
    NewInstallationAllocation,
    open_installation_store,
)
from omnivia_core_runtime.workspace.layout import WorkspaceLayout
from omnivia_core_runtime.workspace.manifest_store import read_manifest

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    CapabilityRef,
    CapabilityRequirement,
    ClientIdentity,
    IdempotencyEquivalence,
    RequestEnvelope,
    RequestMetadata,
    WorkspaceCreateInput,
    get_operation_metadata,
    idempotency_equivalence,
)

WORKSPACE_CREATE = get_operation_metadata("workspace.create")
INSTALLATION_PRINCIPAL = "principal-installation-administrator"
INSTALLATION_CLIENT = ClientIdentity(id="s0-install-client", version="0.1.0")


class TickClock:
    def __init__(self, start: int = 1_800_000_000_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1
        return self.value


def digest(document: str) -> str:
    return "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest()


def allocation(
    root: Path,
    suffix: str,
) -> NewInstallationAllocation:
    return NewInstallationAllocation(
        audit_ref=f"audit-{suffix}",
        claim_id=f"claim-{suffix}",
        allocation_id=f"allocation-{suffix}",
        target_workspace_id=f"ws-{suffix}",
        target_path=(root / "workspaces" / f"ws-{suffix}").resolve(),
    )


def workspace_create_authority(
    store: InstallationStore,
    *,
    input_: WorkspaceCreateInput,
    idempotency_key: str,
) -> tuple[
    AuthorizedApplicationContext,
    AuthenticatedSession,
    ServiceBinding,
    IdempotencyEquivalence,
]:
    """Build installation authority through the real twelve-check request seam."""
    required = WORKSPACE_CREATE.required_capability
    metadata = RequestMetadata(
        request_id=f"req-{idempotency_key}",
        correlation_id=f"cor-{idempotency_key}",
        trace_id=f"trc-{idempotency_key}",
        api_version=CONTRACT_VERSION,
        client=INSTALLATION_CLIENT,
        workspace_id=None,
        scopes=tuple(WORKSPACE_CREATE.scope.required_scopes),
        purpose=WORKSPACE_ADMINISTRATION_PURPOSE,
        required_capabilities=(
            CapabilityRequirement(
                id=required.id,
                minimum_version=required.minimum_version,
                required=True,
            ),
        ),
        idempotency_key=idempotency_key,
    )
    envelope = RequestEnvelope(
        operation=WORKSPACE_CREATE.name,
        metadata=metadata,
        input=input_.to_wire(),
    )
    session = AuthenticatedSession(
        principal_id=INSTALLATION_PRINCIPAL,
        roles=frozenset({INSTALLATION_ADMINISTRATOR_ROLE}),
        installations=frozenset({store.authority.installation_id}),
        operations=frozenset({WORKSPACE_CREATE.name}),
        scopes=frozenset(WORKSPACE_CREATE.scope.required_scopes),
        purposes=frozenset({WORKSPACE_ADMINISTRATION_PURPOSE}),
        capabilities=(CapabilityRef(id=required.id, version=required.minimum_version),),
    )
    binding = ServiceBinding(
        installation_id=store.authority.installation_id,
        workspace_id=None,
    )
    context = authorize_application_request(
        envelope,
        session=session,
        binding=binding,
        supported_capabilities=session.capabilities,
    )
    equivalence = idempotency_equivalence(
        envelope.operation,
        envelope.metadata,
        input_.to_wire(),
        principal_id=context.principal_id,
        workspace_id=None,
    )
    return context, session, binding, equivalence


def installation_service(
    store: InstallationStore,
    *,
    installation_root: Path,
    workspace_storage_root: Path,
    clock: FakeClock,
    bootstrapper: Any = initialise_allocated_workspace,
) -> InstallationApplicationService:
    return InstallationApplicationService(
        store=store,
        installation_root=installation_root,
        workspace_storage_root=workspace_storage_root,
        core_version="0.1.0",
        clock=clock,
        bootstrapper=bootstrapper,
    )


def mutate_guarded_table_without_schema_drift(
    database_path: Path,
    *,
    table: str,
    statement: str,
    parameters: tuple[object, ...] = (),
) -> None:
    """Test-only offline tampering that restores the exact trigger definitions."""
    connection = sqlite3.connect(database_path)
    try:
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = ? ORDER BY name",
            (table,),
        ).fetchall()
        for name, _sql in triggers:
            connection.execute(f'DROP TRIGGER "{name!s}"')
        connection.execute(statement, parameters)
        for _name, sql in triggers:
            assert isinstance(sql, str)
            connection.execute(sql)
        connection.commit()
    finally:
        connection.close()


def test_v06_5_s0_installation_identity_is_global_and_fenced(
    tmp_path: Path,
) -> None:
    installation_root = tmp_path / "installation"
    clock = TickClock()
    minted = 0

    def mint_installation_id() -> str:
        nonlocal minted
        minted += 1
        return "inst-global"

    first = open_installation_store(
        installation_root,
        owner_instance_id="installation-owner-a",
        clock_us=clock,
        installation_id_factory=mint_installation_id,
    )
    first_authority = first.authority
    assert first_authority.installation_id == "inst-global"
    assert first_authority.fencing_generation == 1
    assert first.database_path == (
        installation_root / "catalogue" / "installation.sqlite"
    )
    assert (installation_root / "catalogue" / "installation.lock").is_file()

    with pytest.raises(InstallationBusy):
        open_installation_store(
            installation_root,
            owner_instance_id="installation-owner-b",
            clock_us=clock,
        )

    wrong_owner = replace(first_authority, owner_instance_id="not-the-owner")
    called = False

    def must_not_mint() -> NewInstallationAllocation:
        nonlocal called
        called = True
        return allocation(tmp_path, "forged")

    with pytest.raises(InstallationAuthorityError):
        first.claim_allocation(
            wrong_owner,
            principal_id="principal-1",
            operation="workspace.create",
            purpose="workspace.create",
            idempotency_key="key-wrong-owner",
            request_digest=digest('{"display_name":"Wrong"}'),
            identity_factory=must_not_mint,
        )
    assert not called
    first.close()

    def must_not_remint_installation() -> str:
        raise AssertionError("reopening an installation must not mint a new identity")

    second = open_installation_store(
        installation_root,
        owner_instance_id="installation-owner-b",
        clock_us=clock,
        installation_id_factory=must_not_remint_installation,
    )
    try:
        assert second.authority.installation_id == first_authority.installation_id
        assert second.authority.fencing_generation == 2
        with pytest.raises(InstallationAuthorityError):
            second.claim_allocation(
                first_authority,
                principal_id="principal-1",
                operation="workspace.create",
                purpose="workspace.create",
                idempotency_key="key-stale-generation",
                request_digest=digest('{"display_name":"Stale"}'),
                identity_factory=must_not_mint,
            )
        assert not called
    finally:
        second.close()

    assert minted == 1
    connection = sqlite3.connect(
        installation_root / "catalogue" / "installation.sqlite"
    )
    try:
        state = connection.execute(
            "SELECT installation_id, owner_instance_id, fencing_generation "
            "FROM omnivia_installation_state WHERE singleton = 1"
        ).fetchone()
        assert state == ("inst-global", "installation-owner-b", 2)
        migrations = load_installation_migrations()
        ledger = connection.execute(
            "SELECT version, name, checksum FROM "
            "omnivia_installation_schema_migrations ORDER BY version"
        ).fetchall()
        assert ledger == [
            (migration.version, migration.name, migration.checksum)
            for migration in migrations
        ]
        assert (
            migrations[0].checksum == PINNED_INSTALLATION_MIGRATIONS[migrations[0].name]
        )
    finally:
        connection.close()


def test_v06_5_s0_service_root_is_bound_to_owned_catalogue(tmp_path: Path) -> None:
    owned_root = (tmp_path / "owned-installation").resolve()
    split_root = (tmp_path / "split-installation").resolve()
    store = open_installation_store(
        owned_root,
        owner_instance_id="installation-service",
        clock_us=TickClock(),
        installation_id_factory=lambda: "inst-root-binding",
    )
    try:
        with pytest.raises(ValueError, match="owned catalogue root"):
            installation_service(
                store,
                installation_root=split_root,
                workspace_storage_root=(tmp_path / "workspace-storage").resolve(),
                clock=FakeClock(),
            )
        assert not split_root.exists()
    finally:
        store.close()


def test_v06_5_s0_installation_audit_and_idempotency_are_durable(
    tmp_path: Path,
) -> None:
    installation_root = tmp_path / "installation"
    clock = TickClock()
    store = open_installation_store(
        installation_root,
        owner_instance_id="installation-owner",
        clock_us=clock,
        installation_id_factory=lambda: "inst-durable",
    )
    authority = store.authority
    request_json = '{"display_name":"Research"}'
    request_digest = digest(request_json)
    first_factory_calls = 0

    def mint_first() -> NewInstallationAllocation:
        nonlocal first_factory_calls
        first_factory_calls += 1
        return allocation(tmp_path, "one")

    first = store.claim_allocation(
        authority,
        principal_id="principal-1",
        operation="workspace.create",
        purpose="workspace.create",
        idempotency_key="key-one",
        request_digest=request_digest,
        identity_factory=mint_first,
    )
    assert first.created
    assert first.allocation.state is AllocationState.PREPARING
    assert first_factory_calls == 1

    def must_not_mint_retry() -> NewInstallationAllocation:
        raise AssertionError("an equivalent retry must reuse the accepted target")

    retry = store.claim_allocation(
        authority,
        principal_id="principal-1",
        operation="workspace.create",
        purpose="workspace.create",
        idempotency_key="key-one",
        request_digest=request_digest,
        identity_factory=must_not_mint_retry,
    )
    assert not retry.created
    assert retry.allocation == first.allocation
    assert retry.outcome is None

    with pytest.raises(InstallationIdempotencyConflict):
        store.claim_allocation(
            authority,
            principal_id="principal-1",
            operation="workspace.create",
            purpose="workspace.create",
            idempotency_key="key-one",
            request_digest=digest('{"display_name":"Different"}'),
            identity_factory=must_not_mint_retry,
        )

    outcome_json = '{"workspace_id":"ws-one"}'
    outcome = store.settle_allocation_success(
        authority,
        allocation_id=first.allocation.allocation_id,
        workspace_label="Research",
        outcome_id="outcome-one",
        outcome_json=outcome_json,
        outcome_digest=digest(outcome_json),
        execution_id="execution-one",
        grant_id="grant-one",
        required_role="installation_administrator",
        settlement_guard=lambda: None,
    )
    assert outcome.outcome_json == outcome_json
    assert store.list_workspace_ids() == ("ws-one",)

    replay = store.claim_allocation(
        authority,
        principal_id="principal-1",
        operation="workspace.create",
        purpose="workspace.create",
        idempotency_key="key-one",
        request_digest=request_digest,
        identity_factory=must_not_mint_retry,
    )
    assert replay.outcome == outcome
    assert (
        store.record_replay_grant(
            authority,
            allocation_id=first.allocation.allocation_id,
            execution_id="execution-replay",
            grant_id="grant-replay",
            required_role="installation_administrator",
            settlement_guard=lambda: None,
        )
        == outcome
    )
    with pytest.raises(InstallationStoreError):
        store.record_replay_grant(
            authority,
            allocation_id=first.allocation.allocation_id,
            execution_id="execution-replay-again",
            grant_id="grant-replay",
            required_role="installation_administrator",
            settlement_guard=lambda: None,
        )

    second = store.claim_allocation(
        authority,
        principal_id="principal-2",
        operation="workspace.create",
        purpose="workspace.create",
        idempotency_key="key-two",
        request_digest=digest('{"display_name":"Second"}'),
        identity_factory=lambda: allocation(tmp_path, "two"),
    )
    store.close()

    database = installation_root / "catalogue" / "installation.sqlite"
    connection = sqlite3.connect(database, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.create_function("omnivia_installation_writer", 0, lambda: 1)
    try:
        counts = {
            table: int(
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            )
            for table in (
                "omnivia_installation_audit_events",
                "omnivia_installation_idempotency_claims",
                "omnivia_installation_idempotency_outcomes",
                "omnivia_installation_grant_uses",
            )
        }
        assert counts == {
            "omnivia_installation_audit_events": 2,
            "omnivia_installation_idempotency_claims": 2,
            "omnivia_installation_idempotency_outcomes": 1,
            "omnivia_installation_grant_uses": 2,
        }
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE omnivia_installation_audit_events SET purpose = 'changed' "
                "WHERE audit_ref = 'audit-one'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM omnivia_installation_idempotency_claims "
                "WHERE claim_id = 'claim-one'"
            )

        # Every referenced parent exists, so foreign keys alone would accept this.
        # The relational trigger must reject cross-linking allocation one to claim/audit
        # two as an execution that never occurred.
        with pytest.raises(sqlite3.IntegrityError, match="unguarded INSERT"):
            connection.execute(
                "INSERT INTO omnivia_installation_grant_uses "
                "(execution_id, installation_id, allocation_id, target_workspace_id, "
                "principal_id, operation, purpose, grant_id, required_role, "
                "execution_kind, claim_id, audit_ref, fencing_generation, "
                "recorded_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "execution-cross-linked",
                    authority.installation_id,
                    first.allocation.allocation_id,
                    first.allocation.target_workspace_id,
                    first.allocation.principal_id,
                    first.allocation.operation,
                    first.allocation.purpose,
                    "grant-cross-linked",
                    "installation_administrator",
                    "executed",
                    second.allocation.claim_id,
                    second.allocation.audit_ref,
                    authority.fencing_generation,
                    clock(),
                ),
            )
    finally:
        connection.close()


@pytest.mark.parametrize("settlement", ("success", "failure", "replay"))
def test_v06_5_s0_expiry_is_rechecked_inside_store_transaction(
    tmp_path: Path,
    settlement: str,
) -> None:
    installation_root = (tmp_path / "installation").resolve()
    store = open_installation_store(
        installation_root,
        owner_instance_id="installation-service",
        clock_us=TickClock(),
        installation_id_factory=lambda: f"inst-contention-{settlement}",
    )
    authority = store.authority
    target = store.claim_allocation(
        authority,
        principal_id="principal-target",
        operation="workspace.create",
        purpose="workspace.create",
        idempotency_key=f"target-{settlement}",
        request_digest=digest('{"display_name":"Target"}'),
        identity_factory=lambda: allocation(tmp_path, "target"),
    ).allocation
    outcome_json = '{"workspace_id":"ws-target"}'
    if settlement == "replay":
        store.settle_allocation_success(
            authority,
            allocation_id=target.allocation_id,
            workspace_label="Target",
            outcome_id="outcome-target",
            outcome_json=outcome_json,
            outcome_digest=digest(outcome_json),
            execution_id="execution-initial",
            grant_id="grant-initial",
            required_role="installation_administrator",
            settlement_guard=lambda: None,
        )

    holder_entered = Event()
    release_holder = Event()
    worker_started = Event()
    holder_errors: list[Exception] = []
    worker_errors: list[Exception] = []

    def held_identity() -> NewInstallationAllocation:
        holder_entered.set()
        if not release_holder.wait(timeout=10):
            raise AssertionError("store contention holder was not released")
        return allocation(tmp_path, "holder")

    def hold_store_transaction() -> None:
        try:
            store.claim_allocation(
                authority,
                principal_id="principal-holder",
                operation="workspace.create",
                purpose="workspace.create",
                idempotency_key=f"holder-{settlement}",
                request_digest=digest('{"display_name":"Holder"}'),
                identity_factory=held_identity,
            )
        except Exception as error:  # noqa: BLE001 - report thread failure to parent
            holder_errors.append(error)

    clock = FakeClock()
    expires_at = clock.monotonic() + 1.0

    def require_current() -> None:
        if clock.monotonic() >= expires_at:
            raise InstallationMutationDenied("grant expired during store wait")

    def attempt_settlement() -> None:
        worker_started.set()
        try:
            if settlement == "success":
                store.settle_allocation_success(
                    authority,
                    allocation_id=target.allocation_id,
                    workspace_label="Target",
                    outcome_id="outcome-blocked",
                    outcome_json=outcome_json,
                    outcome_digest=digest(outcome_json),
                    execution_id="execution-blocked",
                    grant_id="grant-blocked",
                    required_role="installation_administrator",
                    settlement_guard=require_current,
                )
            elif settlement == "failure":
                store.fail_allocation(
                    authority,
                    allocation_id=target.allocation_id,
                    detail="blocked-failure",
                    execution_id="execution-blocked",
                    grant_id="grant-blocked",
                    required_role="installation_administrator",
                    settlement_guard=require_current,
                )
            else:
                store.record_replay_grant(
                    authority,
                    allocation_id=target.allocation_id,
                    execution_id="execution-blocked",
                    grant_id="grant-blocked",
                    required_role="installation_administrator",
                    settlement_guard=require_current,
                )
        except Exception as error:  # noqa: BLE001 - report thread failure to parent
            worker_errors.append(error)

    holder = Thread(target=hold_store_transaction)
    worker = Thread(target=attempt_settlement)
    try:
        holder.start()
        assert holder_entered.wait(timeout=10)
        worker.start()
        assert worker_started.wait(timeout=10)
        clock.advance_monotonic(2.0)
        release_holder.set()
        holder.join(timeout=10)
        worker.join(timeout=10)
        assert not holder.is_alive()
        assert not worker.is_alive()
        assert holder_errors == []
        assert len(worker_errors) == 1
        assert isinstance(worker_errors[0], InstallationMutationDenied)

        durable = store.get_allocation(target.allocation_id)
        assert durable is not None
        assert durable.state is (
            AllocationState.ACTIVE
            if settlement == "replay"
            else AllocationState.PREPARING
        )
    finally:
        release_holder.set()
        holder.join(timeout=10)
        worker.join(timeout=10)
        store.close()

    connection = sqlite3.connect(
        installation_root / "catalogue" / "installation.sqlite"
    )
    try:
        grant_uses = int(
            connection.execute(
                "SELECT COUNT(*) FROM omnivia_installation_grant_uses "
                "WHERE allocation_id = ?",
                (target.allocation_id,),
            ).fetchone()[0]
        )
        outcomes = int(
            connection.execute(
                "SELECT COUNT(*) FROM omnivia_installation_idempotency_outcomes "
                "WHERE claim_id = ?",
                (target.claim_id,),
            ).fetchone()[0]
        )
        expected = 1 if settlement == "replay" else 0
        assert grant_uses == expected
        assert outcomes == expected
    finally:
        connection.close()


def test_v06_5_s0_installation_grant_binds_server_minted_target(
    tmp_path: Path,
) -> None:
    installation_root = (tmp_path / "installation").resolve()
    workspace_storage_root = (tmp_path / "workspace-storage").resolve()
    store = open_installation_store(
        installation_root,
        owner_instance_id="installation-service",
        clock_us=TickClock(),
        installation_id_factory=lambda: "inst-grant-binding",
    )
    clock = FakeClock(
        monotonic=2_000.0,
        wall=datetime(2026, 8, 12, 1, 0, tzinfo=UTC),
    )
    service = installation_service(
        store,
        installation_root=installation_root,
        workspace_storage_root=workspace_storage_root,
        clock=clock,
    )
    input_ = WorkspaceCreateInput(display_name="Research")
    context, session, binding, equivalence = workspace_create_authority(
        store,
        input_=input_,
        idempotency_key="installation-grant-binding",
    )
    try:
        prepared = service.prepare_workspace_create(
            context,
            session=session,
            binding=binding,
            input_=input_,
            equivalence=equivalence,
        )
        allocation = prepared.claim.allocation
        grant = prepared.grant
        assert grant.server_issued
        assert grant.installation_id == store.authority.installation_id
        assert grant.fencing_generation == store.authority.fencing_generation
        assert grant.allocation_id == allocation.allocation_id
        assert grant.target_workspace_id == allocation.target_workspace_id
        assert grant.target_path == allocation.target_path
        assert grant.target_path.parent == workspace_storage_root
        assert grant.principal_id == context.principal_id
        assert grant.required_role == INSTALLATION_ADMINISTRATOR_ROLE
        assert grant.scopes == frozenset(context.scopes)
        assert grant.capabilities == context.capabilities
        assert grant.purpose == WORKSPACE_ADMINISTRATION_PURPOSE
        assert grant.idempotency_key == equivalence.scope.idempotency_key
        assert grant.request_fingerprint == equivalence.fingerprint

        with pytest.raises(TypeError, match="issued by the server"):
            InstallationMutationGrant()

        attacker_target = (tmp_path / "attacker-target").resolve()
        tampered = replace(
            prepared,
            claim=replace(
                prepared.claim,
                allocation=replace(allocation, target_path=attacker_target),
            ),
        )
        with pytest.raises(InstallationMutationDenied):
            service.execute_workspace_create(tampered)
        assert not attacker_target.exists()
        assert store.get_allocation(allocation.allocation_id) == allocation
    finally:
        store.close()


def test_v06_5_s0_installation_equivalence_cannot_be_rebound(
    tmp_path: Path,
) -> None:
    installation_root = (tmp_path / "installation").resolve()
    workspace_storage_root = (tmp_path / "workspace-storage").resolve()
    store = open_installation_store(
        installation_root,
        owner_instance_id="installation-service",
        clock_us=TickClock(),
        installation_id_factory=lambda: "inst-equivalence-binding",
    )
    service = installation_service(
        store,
        installation_root=installation_root,
        workspace_storage_root=workspace_storage_root,
        clock=FakeClock(),
    )
    authorised_input = WorkspaceCreateInput(display_name="Authorised A")
    executed_input = WorkspaceCreateInput(display_name="Executed B")
    context, session, binding, equivalence = workspace_create_authority(
        store,
        input_=authorised_input,
        idempotency_key="equivalence-binding",
    )
    try:
        with pytest.raises(InstallationMutationDenied):
            service.prepare_workspace_create(
                context,
                session=session,
                binding=binding,
                input_=executed_input,
                equivalence=equivalence,
            )
        assert store.list_workspace_ids() == ()
        assert not workspace_storage_root.exists()

        prepared = service.prepare_workspace_create(
            context,
            session=session,
            binding=binding,
            input_=authorised_input,
            equivalence=equivalence,
        )
        rebound = replace(prepared, input=executed_input)
        with pytest.raises(InstallationMutationDenied):
            service.execute_workspace_create(rebound)
        assert not workspace_storage_root.exists()
    finally:
        store.close()


def test_v06_5_s0_request_cannot_choose_workspace_or_path(tmp_path: Path) -> None:
    installation_root = (tmp_path / "installation").resolve()
    workspace_storage_root = (tmp_path / "workspace-storage").resolve()
    attacker_path = (tmp_path / "caller-selected-path").resolve()
    raw_input: Mapping[str, object] = {
        "display_name": "Caller cannot allocate",
        "workspace_id": "ws-caller-selected",
        "path": str(attacker_path),
        "generation": 999,
        "lease": "caller-lease",
    }
    input_ = WorkspaceCreateInput.from_wire(raw_input)
    assert input_.to_wire() == {"display_name": "Caller cannot allocate"}

    store = open_installation_store(
        installation_root,
        owner_instance_id="installation-service",
        clock_us=TickClock(),
        installation_id_factory=lambda: "inst-server-target",
    )
    service = installation_service(
        store,
        installation_root=installation_root,
        workspace_storage_root=workspace_storage_root,
        clock=FakeClock(),
    )
    context, session, binding, equivalence = workspace_create_authority(
        store,
        input_=input_,
        idempotency_key="server-target-only",
    )
    try:
        execution = service.create_workspace(
            context,
            session=session,
            binding=binding,
            input_=input_,
            equivalence=equivalence,
        )
        allocation = store.get_allocation(execution.allocation_id)
        assert allocation is not None
        assert allocation.target_workspace_id != "ws-caller-selected"
        assert allocation.target_path != attacker_path
        assert allocation.target_path.parent == workspace_storage_root
        assert allocation.target_path.name == allocation.target_workspace_id
        assert allocation.target_path.is_dir()
        assert not attacker_path.exists()
        assert store.list_workspace_ids() == (allocation.target_workspace_id,)
        workspace = execution.result["workspace"]
        assert isinstance(workspace, Mapping)
        assert workspace["workspace_id"] == allocation.target_workspace_id
    finally:
        store.close()


def test_v06_5_s0_manifest_identity_must_match_allocated_target(
    tmp_path: Path,
) -> None:
    installation_root = (tmp_path / "installation").resolve()
    workspace_storage_root = (tmp_path / "workspace-storage").resolve()
    store = open_installation_store(
        installation_root,
        owner_instance_id="installation-service",
        clock_us=TickClock(),
        installation_id_factory=lambda: "inst-target-verification",
    )

    def substitute_manifest_identity(**kwargs: Any) -> WorkspaceInitResult:
        kwargs["target_workspace_id"] = "ws-substituted"
        return initialise_allocated_workspace(**kwargs)

    service = installation_service(
        store,
        installation_root=installation_root,
        workspace_storage_root=workspace_storage_root,
        clock=FakeClock(),
        bootstrapper=substitute_manifest_identity,
    )
    input_ = WorkspaceCreateInput(display_name="Exact target")
    context, session, binding, equivalence = workspace_create_authority(
        store,
        input_=input_,
        idempotency_key="exact-target-verification",
    )
    try:
        prepared = service.prepare_workspace_create(
            context,
            session=session,
            binding=binding,
            input_=input_,
            equivalence=equivalence,
        )
        with pytest.raises(InstallationSeamFault):
            service.execute_workspace_create(prepared)

        durable = store.get_allocation(prepared.claim.allocation.allocation_id)
        assert durable is not None
        assert durable.state is AllocationState.FAILED_RECOVERABLE
        assert store.list_workspace_ids() == ()
        assert store.get_outcome(durable.claim_id) is None
    finally:
        store.close()


@pytest.mark.parametrize(
    "corruption",
    ("absent_database", "foreign_identity", "schema_drift", "migration_ledger"),
)
def test_v06_5_s0_database_must_be_exact_before_inventory_settlement(
    tmp_path: Path,
    corruption: str,
) -> None:
    installation_root = (tmp_path / "installation").resolve()
    workspace_storage_root = (tmp_path / "workspace-storage").resolve()
    store = open_installation_store(
        installation_root,
        owner_instance_id="installation-service",
        clock_us=TickClock(),
        installation_id_factory=lambda: f"inst-database-{corruption}",
    )

    def bootstrap_then_corrupt(**kwargs: Any) -> WorkspaceInitResult:
        result = initialise_allocated_workspace(**kwargs)
        layout = WorkspaceLayout(root=kwargs["workspace_root"])
        assert read_manifest(layout).workspace_id == kwargs["target_workspace_id"]
        if corruption == "absent_database":
            layout.database_path.unlink()
        elif corruption == "foreign_identity":
            mutate_guarded_table_without_schema_drift(
                layout.database_path,
                table="omnivia_workspace_state",
                statement=(
                    "UPDATE omnivia_workspace_state SET workspace_id = ? "
                    "WHERE singleton = 1"
                ),
                parameters=("ws-foreign",),
            )
        elif corruption == "schema_drift":
            connection = sqlite3.connect(layout.database_path)
            try:
                connection.execute("CREATE TABLE foreign_schema (id TEXT)")
                connection.commit()
            finally:
                connection.close()
        else:
            connection = sqlite3.connect(layout.database_path)
            try:
                latest = connection.execute(
                    "SELECT MAX(version) FROM omnivia_schema_migrations"
                ).fetchone()
                assert latest is not None
                latest_version = int(latest[0])
            finally:
                connection.close()
            mutate_guarded_table_without_schema_drift(
                layout.database_path,
                table="omnivia_schema_migrations",
                statement=("DELETE FROM omnivia_schema_migrations WHERE version = ?"),
                parameters=(latest_version,),
            )
        return result

    service = installation_service(
        store,
        installation_root=installation_root,
        workspace_storage_root=workspace_storage_root,
        clock=FakeClock(),
        bootstrapper=bootstrap_then_corrupt,
    )
    input_ = WorkspaceCreateInput(display_name="Verified database")
    context, session, binding, equivalence = workspace_create_authority(
        store,
        input_=input_,
        idempotency_key=f"database-{corruption}",
    )
    try:
        prepared = service.prepare_workspace_create(
            context,
            session=session,
            binding=binding,
            input_=input_,
            equivalence=equivalence,
        )
        with pytest.raises(InstallationSeamFault):
            service.execute_workspace_create(prepared)

        durable = store.get_allocation(prepared.claim.allocation.allocation_id)
        assert durable is not None
        assert durable.state is AllocationState.FAILED_RECOVERABLE
        assert store.list_workspace_ids() == ()
        assert store.get_outcome(durable.claim_id) is None
    finally:
        store.close()


def test_v06_5_s0_allocation_retry_reuses_target(tmp_path: Path) -> None:
    installation_root = (tmp_path / "installation").resolve()
    workspace_storage_root = (tmp_path / "workspace-storage").resolve()
    store = open_installation_store(
        installation_root,
        owner_instance_id="installation-service",
        clock_us=TickClock(),
        installation_id_factory=lambda: "inst-retry",
    )
    service = installation_service(
        store,
        installation_root=installation_root,
        workspace_storage_root=workspace_storage_root,
        clock=FakeClock(),
    )
    input_ = WorkspaceCreateInput(display_name="One durable target")
    context, session, binding, equivalence = workspace_create_authority(
        store,
        input_=input_,
        idempotency_key="retry-one-target",
    )
    try:
        first = service.prepare_workspace_create(
            context,
            session=session,
            binding=binding,
            input_=input_,
            equivalence=equivalence,
        )
        retry_before_work = service.prepare_workspace_create(
            context,
            session=session,
            binding=binding,
            input_=input_,
            equivalence=equivalence,
        )
        assert first.claim.created
        assert not retry_before_work.claim.created
        assert retry_before_work.claim.allocation == first.claim.allocation
        assert retry_before_work.grant.grant_id != first.grant.grant_id

        executed = service.execute_workspace_create(first)
        stale_replay = service.execute_workspace_create(retry_before_work)
        retry_after_settlement = service.prepare_workspace_create(
            context,
            session=session,
            binding=binding,
            input_=input_,
            equivalence=equivalence,
        )
        replayed = service.execute_workspace_create(retry_after_settlement)
        assert (
            executed.target_workspace_id == first.claim.allocation.target_workspace_id
        )
        assert replayed.target_workspace_id == executed.target_workspace_id
        assert replayed.result == executed.result
        assert stale_replay.target_workspace_id == executed.target_workspace_id
        assert stale_replay.result == executed.result
        assert not executed.replayed
        assert stale_replay.replayed
        assert replayed.replayed
        assert store.list_workspace_ids() == (executed.target_workspace_id,)
        assert [path.name for path in workspace_storage_root.iterdir()] == [
            executed.target_workspace_id
        ]
    finally:
        store.close()


def test_v06_5_s0_allocation_crash_is_recoverable(tmp_path: Path) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    installation_root = (tmp_path / "installation").resolve()
    workspace_storage_root = (tmp_path / "workspace-storage").resolve()
    store = open_installation_store(
        installation_root,
        owner_instance_id="installation-service",
        clock_us=TickClock(),
        installation_id_factory=lambda: "inst-crash-recovery",
    )
    clock = FakeClock()

    def bootstrap_then_crash(**kwargs: Any) -> WorkspaceInitResult:
        initialise_allocated_workspace(**kwargs)
        raise SimulatedProcessCrash()

    crashing_service = installation_service(
        store,
        installation_root=installation_root,
        workspace_storage_root=workspace_storage_root,
        clock=clock,
        bootstrapper=bootstrap_then_crash,
    )
    input_ = WorkspaceCreateInput(display_name="Recover after crash")
    context, session, binding, equivalence = workspace_create_authority(
        store,
        input_=input_,
        idempotency_key="crash-recovery",
    )
    try:
        first = crashing_service.prepare_workspace_create(
            context,
            session=session,
            binding=binding,
            input_=input_,
            equivalence=equivalence,
        )
        with pytest.raises(SimulatedProcessCrash):
            crashing_service.execute_workspace_create(first)

        after_crash = store.get_allocation(first.claim.allocation.allocation_id)
        assert after_crash is not None
        assert after_crash.state is AllocationState.PREPARING
        assert after_crash.target_path.is_dir()
        assert store.get_outcome(after_crash.claim_id) is None
        assert store.list_workspace_ids() == ()

        recovering_service = installation_service(
            store,
            installation_root=installation_root,
            workspace_storage_root=workspace_storage_root,
            clock=clock,
        )
        retry = recovering_service.prepare_workspace_create(
            context,
            session=session,
            binding=binding,
            input_=input_,
            equivalence=equivalence,
        )
        assert not retry.claim.created
        assert (
            retry.claim.allocation.target_workspace_id
            == after_crash.target_workspace_id
        )
        assert retry.claim.allocation.target_path == after_crash.target_path

        recovered = recovering_service.execute_workspace_create(retry)
        final = store.get_allocation(recovered.allocation_id)
        assert final is not None
        assert final.state is AllocationState.ACTIVE
        assert final.target_workspace_id == after_crash.target_workspace_id
        assert store.list_workspace_ids() == (final.target_workspace_id,)
        assert [path.name for path in workspace_storage_root.iterdir()] == [
            final.target_workspace_id
        ]
    finally:
        store.close()


def test_v06_5_s0_grant_cannot_expire_during_result_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation_root = (tmp_path / "installation").resolve()
    workspace_storage_root = (tmp_path / "workspace-storage").resolve()
    store = open_installation_store(
        installation_root,
        owner_instance_id="installation-service",
        clock_us=TickClock(),
        installation_id_factory=lambda: "inst-final-expiry",
    )
    clock = FakeClock()
    service = installation_service(
        store,
        installation_root=installation_root,
        workspace_storage_root=workspace_storage_root,
        clock=clock,
    )
    input_ = WorkspaceCreateInput(display_name="Expiry fence")
    context, session, binding, equivalence = workspace_create_authority(
        store,
        input_=input_,
        idempotency_key="final-expiry-fence",
    )
    read_manifest = installation_module.read_manifest

    def read_then_expire(layout: Any) -> Any:
        manifest = read_manifest(layout)
        clock.advance_monotonic(2.0)
        return manifest

    monkeypatch.setattr(installation_module, "read_manifest", read_then_expire)
    try:
        prepared = service.prepare_workspace_create(
            context,
            session=session,
            binding=binding,
            input_=input_,
            equivalence=equivalence,
            lifetime_us=1_000_000,
        )
        with pytest.raises(InstallationMutationDenied):
            service.execute_workspace_create(prepared)
        durable = store.get_allocation(prepared.claim.allocation.allocation_id)
        assert durable is not None
        assert durable.state is AllocationState.PREPARING
        assert store.list_workspace_ids() == ()
        assert store.get_outcome(durable.claim_id) is None
    finally:
        store.close()


def test_v06_5_s0_expired_grant_cannot_settle_failure(tmp_path: Path) -> None:
    installation_root = (tmp_path / "installation").resolve()
    workspace_storage_root = (tmp_path / "workspace-storage").resolve()
    store = open_installation_store(
        installation_root,
        owner_instance_id="installation-service",
        clock_us=TickClock(),
        installation_id_factory=lambda: "inst-failure-expiry",
    )
    clock = FakeClock()

    def expire_then_refuse(**_kwargs: Any) -> WorkspaceInitResult:
        clock.advance_monotonic(2.0)
        return WorkspaceInitResult(
            status=WorkspaceInitStatus.REFUSED,
            reason="simulated write refusal",
            refusal=WorkspaceInitRefusal.WRITE_FAILURE,
        )

    service = installation_service(
        store,
        installation_root=installation_root,
        workspace_storage_root=workspace_storage_root,
        clock=clock,
        bootstrapper=expire_then_refuse,
    )
    input_ = WorkspaceCreateInput(display_name="Failure expiry fence")
    context, session, binding, equivalence = workspace_create_authority(
        store,
        input_=input_,
        idempotency_key="failure-expiry-fence",
    )
    try:
        prepared = service.prepare_workspace_create(
            context,
            session=session,
            binding=binding,
            input_=input_,
            equivalence=equivalence,
            lifetime_us=1_000_000,
        )
        with pytest.raises(InstallationMutationDenied):
            service.execute_workspace_create(prepared)
        durable = store.get_allocation(prepared.claim.allocation.allocation_id)
        assert durable is not None
        assert durable.state is AllocationState.PREPARING
        assert store.list_workspace_ids() == ()
        assert store.get_outcome(durable.claim_id) is None
    finally:
        store.close()


def test_v06_5_s0_concurrent_allocation_reports_in_progress(tmp_path: Path) -> None:
    installation_root = (tmp_path / "installation").resolve()
    workspace_storage_root = (tmp_path / "workspace-storage").resolve()
    store = open_installation_store(
        installation_root,
        owner_instance_id="installation-service",
        clock_us=TickClock(),
        installation_id_factory=lambda: "inst-concurrent",
    )

    def target_owned_elsewhere(**_kwargs: Any) -> WorkspaceInitResult:
        return WorkspaceInitResult(
            status=WorkspaceInitStatus.REFUSED,
            reason="the accepted target is held by another coordinator",
            refusal=WorkspaceInitRefusal.WORKSPACE_BUSY,
        )

    service = installation_service(
        store,
        installation_root=installation_root,
        workspace_storage_root=workspace_storage_root,
        clock=FakeClock(),
        bootstrapper=target_owned_elsewhere,
    )
    input_ = WorkspaceCreateInput(display_name="Concurrent target")
    context, session, binding, equivalence = workspace_create_authority(
        store,
        input_=input_,
        idempotency_key="concurrent-target",
    )
    try:
        prepared = service.prepare_workspace_create(
            context,
            session=session,
            binding=binding,
            input_=input_,
            equivalence=equivalence,
        )
        with pytest.raises(InstallationBootstrapInProgress):
            service.execute_workspace_create(prepared)

        allocation = store.get_allocation(prepared.claim.allocation.allocation_id)
        assert allocation is not None
        assert allocation.state is AllocationState.PREPARING
        assert store.get_outcome(allocation.claim_id) is None
        assert store.list_workspace_ids() == ()
    finally:
        store.close()


def test_v06_5_s0_catalogue_name_does_not_hide_foreign_state(tmp_path: Path) -> None:
    installation_root = tmp_path / "installation"
    foreign_catalogue = installation_root / "catalogue"
    foreign_catalogue.mkdir(parents=True)
    foreign = foreign_catalogue / "private-notes.txt"
    foreign.write_text("not installation state", encoding="utf-8")
    workspace_root = tmp_path / "workspace"

    result = initialise_workspace(
        workspace_root=workspace_root,
        installation_root=installation_root,
    )

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.UNRECOGNISED_INSTALLATION_STATE
    assert foreign.read_text(encoding="utf-8") == "not installation state"
    assert not workspace_root.exists()
