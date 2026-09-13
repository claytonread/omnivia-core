"""`--init` registers the canonical managed-local workspace into the installation
catalogue, closing the gap where `configure_mcp_setup` (and any other
installation-authorised operation) refused it as "not in this installation's
authorised inventory".
"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from omnivia_core_runtime.service import workspace_init as workspace_init_module
from omnivia_core_runtime.service.authorization import AuthenticatedSession
from omnivia_core_runtime.service.installation_bootstrap import (
    BOOTSTRAP_PRINCIPAL,
    BOOTSTRAP_REGISTER_OPERATION,
    BOOTSTRAP_REQUIRED_ROLE,
    initialise_and_register_managed_local_workspace,
)
from omnivia_core_runtime.service.installed_mcp import InstalledMcpAuthority
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
from omnivia_core_runtime.storage import installation_store as installation_store_module
from omnivia_core_runtime.storage.installation_store import (
    InstallationStore,
    InstallationStoreError,
    McpHost,
    McpProfile,
    NewInstallationAllocation,
    open_installation_store,
)


def _init(tmp_path: Path, *, name: str = "workspace") -> tuple[Path, Path]:
    return tmp_path / name, tmp_path / "installation-state"


def _seed_active_registration(
    store: InstallationStore,
    *,
    workspace_id: str,
    target_path: Path,
    operation: str = "workspace.create",
    idempotency_key: str = "seeded-key",
) -> None:
    """Durably register `workspace_id` at `target_path` under some *other*
    operation and idempotency key entirely -- simulating an active
    `omnivia_installation_workspaces` row this bootstrap registration did not
    itself create, the way `workspace.create`'s own settlement would.
    """
    authority = store.authority
    claim = store.claim_allocation(
        authority,
        principal_id="other-origin",
        operation=operation,
        purpose=WORKSPACE_ADMINISTRATION_PURPOSE,
        idempotency_key=idempotency_key,
        request_digest="sha256:" + "1" * 64,
        identity_factory=lambda: NewInstallationAllocation(
            audit_ref=f"audit-{idempotency_key}",
            claim_id=f"claim-{idempotency_key}",
            allocation_id=f"allocation-{idempotency_key}",
            target_workspace_id=workspace_id,
            target_path=target_path,
        ),
    )
    store.settle_allocation_success(
        authority,
        allocation_id=claim.allocation.allocation_id,
        workspace_label="seeded elsewhere",
        outcome_id=f"outcome-{idempotency_key}",
        outcome_json='{"seeded":true}',
        outcome_digest="sha256:" + "2" * 64,
        execution_id=f"execution-{idempotency_key}",
        grant_id=f"grant-{idempotency_key}",
        required_role="other_role",
        settlement_guard=lambda: None,
    )


def test_fresh_init_registers_the_canonical_workspace_in_the_catalogue(
    tmp_path: Path,
) -> None:
    workspace_root, installation_root = _init(tmp_path)

    result = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )

    assert result.status is WorkspaceInitStatus.INITIALISED
    assert result.workspace_id is not None
    store = open_installation_store(installation_root, owner_instance_id="checker")
    try:
        assert result.workspace_id in store.list_workspace_ids()
    finally:
        store.close()


def test_repeat_init_is_idempotent_and_does_not_duplicate_the_registration(
    tmp_path: Path,
) -> None:
    workspace_root, installation_root = _init(tmp_path)

    first = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )
    second = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )

    assert first.status is WorkspaceInitStatus.INITIALISED
    assert second.status is WorkspaceInitStatus.ALREADY_INITIALISED
    assert second.workspace_id == first.workspace_id
    store = open_installation_store(installation_root, owner_instance_id="checker")
    try:
        assert store.list_workspace_ids() == (first.workspace_id,)
    finally:
        store.close()


def test_registering_the_bootstrapped_workspace_fixes_configure_mcp_setup(
    tmp_path: Path,
) -> None:
    """The exact regression this repair closes.

    Before registration, `configure_mcp_setup` (through `InstalledMcpAuthority`,
    the same seam `omnivia mcp configure` calls) refuses this workspace with
    `InstallationStoreError` -- the CLI-visible `LocalControlError.REFUSED` /
    "the installed MCP authority refused the requested change". After
    `--init` registers it, the identical call succeeds.
    """
    workspace_root, installation_root = _init(tmp_path)
    result = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )
    assert result.status is WorkspaceInitStatus.INITIALISED
    assert result.workspace_id is not None

    store = open_installation_store(installation_root, owner_instance_id="mcp-owner")
    try:
        authority = InstalledMcpAuthority(store)
        session = AuthenticatedSession(
            principal_id="local-owner",
            roles=frozenset({INSTALLATION_ADMINISTRATOR_ROLE}),
            installations=frozenset({store.authority.installation_id}),
        )
        provisioning = authority.configure(
            session,
            host=McpHost.CLAUDE_CODE,
            workspace_id=result.workspace_id,
            profile=McpProfile.RESTRICTED,
            authoring_intent=False,
        )
        assert provisioning.setup.workspace_id == result.workspace_id
    finally:
        store.close()


def test_a_workspace_registered_at_a_different_path_is_refused_as_a_conflict(
    tmp_path: Path,
) -> None:
    """The exact fact the refusal names: a workspace id genuinely *active* in the
    catalogue at one path, encountered again at another.

    Both workspaces are real, disk-verified targets rather than a hand-seeded
    claim: the second is built through `initialise_allocated_workspace` --
    the same entry point `workspace.create` itself bootstraps through -- given
    the first workspace's own id, so its manifest and database genuinely carry
    that identity. The registration attempt against it must therefore be
    refused by the real settled `omnivia_installation_workspaces` row for that
    id, not merely by a `PREPARING` claim (see
    `test_an_uncommitted_registration_claim_with_a_different_digest_is_refused_as_a_conflict`
    for that separate, narrower case).
    """
    workspace_root, installation_root = _init(tmp_path)
    registered = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )
    assert registered.status is WorkspaceInitStatus.INITIALISED
    workspace_id = registered.workspace_id
    assert workspace_id is not None

    elsewhere = tmp_path / "elsewhere"
    allocated = initialise_allocated_workspace(
        workspace_root=elsewhere,
        installation_root=installation_root,
        target_workspace_id=workspace_id,
        display_name="a workspace copied elsewhere",
    )
    assert allocated.status is WorkspaceInitStatus.INITIALISED
    assert allocated.workspace_id == workspace_id

    result = initialise_and_register_managed_local_workspace(
        workspace_root=elsewhere, installation_root=installation_root
    )

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.WORKSPACE_REGISTRATION_CONFLICT
    assert result.workspace_id == workspace_id
    # The genuinely active registration at the first path is untouched, and no
    # second row for the same id was ever admitted at the conflicting path.
    store = open_installation_store(installation_root, owner_instance_id="checker")
    try:
        assert store.list_workspace_ids() == (workspace_id,)
    finally:
        store.close()


def test_a_workspace_registered_under_a_different_operation_at_a_different_path_is_refused_as_a_conflict(
    tmp_path: Path,
) -> None:
    """The active registration this refusal exists to catch need not be this
    bootstrap's own. `workspace.create` (or any other durable registration)
    can settle the very workspace id `--init` later computes, under its own
    operation and idempotency key -- one this bootstrap's own idempotency
    scope (`bootstrap:{workspace_id}`) would never find, since scope lookup is
    keyed by operation and key together. The conflict must still be caught,
    by inspecting the registration itself rather than this bootstrap's own
    claim scope, and caught *before* a doomed claim is ever minted under this
    operation for it.
    """
    workspace_root, installation_root = _init(tmp_path)
    bootstrapped = initialise_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )
    assert bootstrapped.status is WorkspaceInitStatus.INITIALISED
    workspace_id = bootstrapped.workspace_id
    assert workspace_id is not None

    store = open_installation_store(installation_root, owner_instance_id="seeder")
    try:
        _seed_active_registration(
            store,
            workspace_id=workspace_id,
            target_path=(tmp_path / "elsewhere").resolve(),
        )
    finally:
        store.close()

    result = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.WORKSPACE_REGISTRATION_CONFLICT
    assert result.workspace_id == workspace_id

    database = installation_root / "catalogue" / "installation.sqlite"
    connection = sqlite3.connect(database)
    try:
        # No bootstrap ledger chain was ever minted for this doomed attempt --
        # the pre-check refused before `claim_allocation` was even called.
        claim_row = connection.execute(
            "SELECT 1 FROM omnivia_installation_idempotency_claims "
            "WHERE operation = ? AND idempotency_key = ?",
            (BOOTSTRAP_REGISTER_OPERATION, f"bootstrap:{workspace_id}"),
        ).fetchone()
        assert claim_row is None
        allocation_row = connection.execute(
            "SELECT 1 FROM omnivia_installation_allocations WHERE operation = ?",
            (BOOTSTRAP_REGISTER_OPERATION,),
        ).fetchone()
        assert allocation_row is None
    finally:
        connection.close()

    store = open_installation_store(installation_root, owner_instance_id="checker")
    try:
        # The seeded registration -- the only one -- is untouched.
        assert store.list_workspace_ids() == (workspace_id,)
    finally:
        store.close()


def test_a_workspace_registered_under_a_different_operation_at_the_same_path_replays_successfully(
    tmp_path: Path,
) -> None:
    """The other half of the same fact: an active registration for this exact
    workspace id at this exact path, settled under a wholly different
    operation, must be treated as already registered rather than raced
    against with a second claim under this bootstrap's own operation -- one
    that could only ever collide with it on the same unique target index in
    `omnivia_installation_workspaces` and surface as an opaque `WRITE_FAILURE`
    even though the path genuinely matches.
    """
    workspace_root, installation_root = _init(tmp_path)
    bootstrapped = initialise_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )
    assert bootstrapped.status is WorkspaceInitStatus.INITIALISED
    workspace_id = bootstrapped.workspace_id
    assert workspace_id is not None

    store = open_installation_store(installation_root, owner_instance_id="seeder")
    try:
        _seed_active_registration(
            store,
            workspace_id=workspace_id,
            target_path=workspace_root.resolve(),
        )
    finally:
        store.close()

    result = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )

    assert result.status is not WorkspaceInitStatus.REFUSED
    assert result.workspace_id == workspace_id
    store = open_installation_store(installation_root, owner_instance_id="checker")
    try:
        assert store.list_workspace_ids() == (workspace_id,)
    finally:
        store.close()

    database = installation_root / "catalogue" / "installation.sqlite"
    connection = sqlite3.connect(database)
    try:
        # The pre-check found the seeded registration's path already matched,
        # so no claim was ever minted under this bootstrap's own operation.
        claim_row = connection.execute(
            "SELECT 1 FROM omnivia_installation_idempotency_claims "
            "WHERE operation = ? AND idempotency_key = ?",
            (BOOTSTRAP_REGISTER_OPERATION, f"bootstrap:{workspace_id}"),
        ).fetchone()
        assert claim_row is None
    finally:
        connection.close()


def test_a_same_id_same_path_active_registration_replays_successfully(
    tmp_path: Path,
) -> None:
    """The other half of the conflict rule: identical target, no conflict.

    `test_repeat_init_is_idempotent_and_does_not_duplicate_the_registration`
    proves this at the `WorkspaceInitResult` level; this proves it at the
    catalogue's own allocation identity, so the conflict test above and this
    one cannot both pass by accident of a check that ignores the path.
    """
    workspace_root, installation_root = _init(tmp_path)
    first = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )
    assert first.status is WorkspaceInitStatus.INITIALISED
    workspace_id = first.workspace_id
    assert workspace_id is not None

    second = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )

    assert second.status is WorkspaceInitStatus.ALREADY_INITIALISED
    assert second.workspace_id == workspace_id
    store = open_installation_store(installation_root, owner_instance_id="checker")
    try:
        assert store.list_workspace_ids() == (workspace_id,)
    finally:
        store.close()


def test_an_uncommitted_registration_claim_with_a_different_digest_is_refused_as_a_conflict(
    tmp_path: Path,
) -> None:
    """A narrower case than the active-registration conflict above: an
    idempotency claim was made for this workspace id under a different
    canonical request (a different target path) but never settled -- still
    `PREPARING`, with no active `omnivia_installation_workspaces` row at all.

    `claim_allocation` refuses on the digest mismatch before it ever looks at
    the claim's allocation state, so this and the active-registration case
    above are deliberately proven separately rather than one standing in for
    the other.
    """
    workspace_root, installation_root = _init(tmp_path)
    bootstrapped = initialise_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )
    assert bootstrapped.status is WorkspaceInitStatus.INITIALISED
    workspace_id = bootstrapped.workspace_id
    assert workspace_id is not None

    # Pre-seed an uncommitted (PREPARING) claim for the same workspace id bound
    # to a different canonical request -- no active registration exists yet.
    store = open_installation_store(installation_root, owner_instance_id="seeder")
    try:
        store.claim_allocation(
            store.authority,
            principal_id=BOOTSTRAP_PRINCIPAL,
            operation=BOOTSTRAP_REGISTER_OPERATION,
            purpose=WORKSPACE_ADMINISTRATION_PURPOSE,
            idempotency_key=f"bootstrap:{workspace_id}",
            request_digest="sha256:" + "0" * 64,
            identity_factory=lambda: NewInstallationAllocation(
                audit_ref="audit-conflict",
                claim_id="claim-conflict",
                allocation_id="allocation-conflict",
                target_workspace_id=workspace_id,
                target_path=(tmp_path / "elsewhere").resolve(),
            ),
        )
    finally:
        store.close()

    result = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.WORKSPACE_REGISTRATION_CONFLICT
    # The real workspace on disk is unaffected -- only the catalogue write refused.
    store = open_installation_store(installation_root, owner_instance_id="checker")
    try:
        assert store.list_workspace_ids() == ()
    finally:
        store.close()


def test_an_existing_unrelated_refusal_never_touches_the_installation_catalogue(
    tmp_path: Path,
) -> None:
    workspace_root, installation_root = _init(tmp_path)
    workspace_root.mkdir(parents=True)
    (workspace_root / "somebody-elses-file.txt").write_text("mine", encoding="utf-8")

    result = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.UNRELATED_DIRECTORY
    # Nothing about the installation catalogue was ever opened or created.
    assert not installation_root.exists()


def test_the_installation_catalogue_being_busy_refuses_registration_without_crashing(
    tmp_path: Path,
) -> None:
    workspace_root, installation_root = _init(tmp_path)
    holder = open_installation_store(
        installation_root, owner_instance_id="live-service"
    )
    try:
        result = initialise_and_register_managed_local_workspace(
            workspace_root=workspace_root, installation_root=installation_root
        )
    finally:
        holder.close()

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.WORKSPACE_BUSY
    # The workspace itself was still bootstrapped -- only registration was busy.
    assert result.workspace_id is not None


def test_windows_catalogue_busy_refusal_secures_workspace_but_not_catalogue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite parents are secured only after their own lifetime lock is held."""
    workspace_root, installation_root = _init(tmp_path)
    prepared = initialise_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )
    assert prepared.status is WorkspaceInitStatus.INITIALISED

    holder = open_installation_store(
        installation_root, owner_instance_id="live-service"
    )
    seen: list[tuple[Path, bool]] = []
    monkeypatch.setattr(workspace_init_module, "_WINDOWS_OWNER_CONTROL", True)
    monkeypatch.setattr(
        workspace_init_module,
        "restrict_to_owner",
        lambda path, *, directory: seen.append((path, directory)),
    )
    try:
        result = initialise_and_register_managed_local_workspace(
            workspace_root=workspace_root,
            installation_root=installation_root,
        )
    finally:
        holder.close()

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.WORKSPACE_BUSY
    assert seen == [
        (workspace_root, True),
        (workspace_root / "workspace.sqlite", False),
        (workspace_root / "workspace.json", False),
        (workspace_root / "locks", True),
    ]


def test_windows_registration_secures_catalogue_before_sqlite_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deletable sidecar namespace is private before the first catalogue open."""
    workspace_root, installation_root = _init(tmp_path)
    prepared = initialise_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )
    assert prepared.status is WorkspaceInitStatus.INITIALISED
    seeded = open_installation_store(
        installation_root, owner_instance_id="catalogue-seeder"
    )
    seeded.close()

    catalogue = installation_root / "catalogue"
    catalogue_database = catalogue / "installation.sqlite"
    seen: list[tuple[Path, bool]] = []
    real_connect = installation_store_module._connect_catalogue
    monkeypatch.setattr(workspace_init_module, "_WINDOWS_OWNER_CONTROL", True)
    monkeypatch.setattr(
        workspace_init_module,
        "restrict_to_owner",
        lambda path, *, directory: seen.append((path, directory)),
    )

    def _connect_after_security(path: Path) -> sqlite3.Connection:
        assert (catalogue, True) in seen
        assert (catalogue_database, False) in seen
        return real_connect(path)

    monkeypatch.setattr(
        installation_store_module, "_connect_catalogue", _connect_after_security
    )

    result = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root,
        installation_root=installation_root,
    )

    assert result.status is WorkspaceInitStatus.ALREADY_INITIALISED
    assert seen.index((workspace_root, True)) < seen.index((catalogue, True))


def test_a_failed_verification_fails_closed_and_a_later_retry_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnivia_core_runtime.service.installation_bootstrap as bootstrap_module

    workspace_root, installation_root = _init(tmp_path)
    real_verify = bootstrap_module.verify_workspace_result
    calls = {"count": 0}

    def _flaky(*args: object, **kwargs: object) -> object:
        calls["count"] += 1
        if calls["count"] == 1:
            raise bootstrap_module.InstallationSeamFault("simulated corruption")
        return real_verify(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(bootstrap_module, "verify_workspace_result", _flaky)

    failed = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )
    assert failed.status is WorkspaceInitStatus.REFUSED
    assert failed.refusal is WorkspaceInitRefusal.WRITE_FAILURE
    store = open_installation_store(installation_root, owner_instance_id="checker")
    try:
        assert store.list_workspace_ids() == ()
        allocation = store.get_allocation("ialloc-does-not-matter")  # sanity: no crash
        assert allocation is None
    finally:
        store.close()

    recovered = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )
    assert recovered.status is WorkspaceInitStatus.ALREADY_INITIALISED
    assert recovered.workspace_id == failed.workspace_id
    store = open_installation_store(installation_root, owner_instance_id="checker")
    try:
        assert store.list_workspace_ids() == (failed.workspace_id,)
    finally:
        store.close()


def test_a_contended_settlement_lock_is_refused_as_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnivia_core_runtime.service.installation_bootstrap as bootstrap_module

    class _NeverAcquired:
        def acquire(self) -> bool:
            return False

    monkeypatch.setattr(
        bootstrap_module, "create_lock", lambda *args, **kwargs: _NeverAcquired()
    )

    workspace_root, installation_root = _init(tmp_path)
    result = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.WORKSPACE_BUSY
    store = open_installation_store(installation_root, owner_instance_id="checker")
    try:
        assert store.list_workspace_ids() == ()
    finally:
        store.close()


def test_a_successful_registration_leaves_one_consistent_ledger_chain(
    tmp_path: Path,
) -> None:
    """Every durable row this registration writes carries the same current
    fencing generation and the same operation/principal bindings.

    Read directly off the catalogue's SQLite file rather than through the
    store's own accessors: those accessors are what the rest of this suite
    already exercises, so a bug that mis-recorded a column while still
    returning the right Python value would pass every other test here.
    """
    workspace_root, installation_root = _init(tmp_path)
    result = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )
    assert result.status is WorkspaceInitStatus.INITIALISED
    workspace_id = result.workspace_id
    assert workspace_id is not None

    database = installation_root / "catalogue" / "installation.sqlite"
    connection = sqlite3.connect(database)
    try:
        allocation_row = connection.execute(
            "SELECT allocation_id, target_path, principal_id, operation, purpose, "
            "claim_id, audit_ref, state, fencing_generation "
            "FROM omnivia_installation_allocations WHERE target_workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        assert allocation_row is not None
        (
            allocation_id,
            target_path,
            allocation_principal,
            allocation_operation,
            allocation_purpose,
            claim_id,
            audit_ref,
            state,
            generation,
        ) = allocation_row
        assert Path(target_path) == workspace_root.resolve()
        assert allocation_principal == BOOTSTRAP_PRINCIPAL
        assert allocation_operation == BOOTSTRAP_REGISTER_OPERATION
        assert allocation_purpose == WORKSPACE_ADMINISTRATION_PURPOSE
        assert state == "active"
        assert isinstance(generation, int)
        assert generation >= 1

        audit_row = connection.execute(
            "SELECT principal_id, operation, purpose, outcome_class, "
            "fencing_generation FROM omnivia_installation_audit_events "
            "WHERE audit_ref = ?",
            (audit_ref,),
        ).fetchone()
        assert audit_row == (
            BOOTSTRAP_PRINCIPAL,
            BOOTSTRAP_REGISTER_OPERATION,
            WORKSPACE_ADMINISTRATION_PURPOSE,
            "accepted",
            generation,
        )

        claim_row = connection.execute(
            "SELECT principal_id, operation, idempotency_key, audit_ref, "
            "fencing_generation FROM omnivia_installation_idempotency_claims "
            "WHERE claim_id = ?",
            (claim_id,),
        ).fetchone()
        assert claim_row == (
            BOOTSTRAP_PRINCIPAL,
            BOOTSTRAP_REGISTER_OPERATION,
            f"bootstrap:{workspace_id}",
            audit_ref,
            generation,
        )

        workspace_row = connection.execute(
            "SELECT allocation_id, workspace_path, fencing_generation "
            "FROM omnivia_installation_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        assert workspace_row is not None
        (workspace_allocation_id, workspace_path, workspace_generation) = workspace_row
        assert workspace_allocation_id == allocation_id
        assert Path(workspace_path) == workspace_root.resolve()
        assert workspace_generation == generation

        outcome_row = connection.execute(
            "SELECT outcome_branch, audit_ref, fencing_generation "
            "FROM omnivia_installation_idempotency_outcomes WHERE claim_id = ?",
            (claim_id,),
        ).fetchone()
        assert outcome_row == ("success", audit_ref, generation)

        grant_row = connection.execute(
            "SELECT principal_id, operation, purpose, required_role, "
            "execution_kind, claim_id, audit_ref, fencing_generation "
            "FROM omnivia_installation_grant_uses WHERE allocation_id = ?",
            (allocation_id,),
        ).fetchone()
        assert grant_row == (
            BOOTSTRAP_PRINCIPAL,
            BOOTSTRAP_REGISTER_OPERATION,
            WORKSPACE_ADMINISTRATION_PURPOSE,
            BOOTSTRAP_REQUIRED_ROLE,
            "executed",
            claim_id,
            audit_ref,
            generation,
        )
    finally:
        connection.close()


def test_a_manifest_read_failure_before_verification_fails_closed_without_leaking_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`read_manifest` runs before `verify_workspace_result`, under the same
    "any internal fault here closes on `WRITE_FAILURE`" contract the
    verification call itself already had. A `ManifestStoreError` from it must
    not escape `_claim_and_settle` -- and the fixed, non-interpolated
    `_INTERNAL_FAULT` sentence must still be what callers see, never the raw
    exception text.
    """
    import omnivia_core_runtime.service.installation_bootstrap as bootstrap_module

    workspace_root, installation_root = _init(tmp_path)

    def _boom_read_manifest(*args: object, **kwargs: object) -> object:
        raise bootstrap_module.ManifestStoreError("simulated manifest corruption")

    monkeypatch.setattr(bootstrap_module, "read_manifest", _boom_read_manifest)

    result = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.WRITE_FAILURE
    assert result.reason is not None
    assert "simulated" not in result.reason
    assert "corruption" not in result.reason


def test_a_failed_recovery_read_after_a_failed_settlement_fails_closed_without_leaking_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery path for a failed `settle_allocation_success` reads
    `store.get_outcome` to tell a genuine concurrent settlement apart from a
    real fault. That recovery read can itself raise `InstallationStoreError`
    -- it is the same store, over the same connection, right after the
    settlement it is checking up on just failed -- and must not escape either;
    it must close on `WRITE_FAILURE` exactly like the settlement fault it was
    trying to explain away, with no injected diagnostic text in the reason.
    """
    workspace_root, installation_root = _init(tmp_path)

    def _boom_settle(self: InstallationStore, *args: object, **kwargs: object) -> None:
        raise InstallationStoreError("simulated settlement corruption")

    def _boom_get_outcome(self: InstallationStore, claim_id: str) -> None:
        raise InstallationStoreError("simulated recovery-read corruption")

    monkeypatch.setattr(InstallationStore, "settle_allocation_success", _boom_settle)
    monkeypatch.setattr(InstallationStore, "get_outcome", _boom_get_outcome)

    result = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )

    assert result.status is WorkspaceInitStatus.REFUSED
    assert result.refusal is WorkspaceInitRefusal.WRITE_FAILURE
    assert result.reason is not None
    assert "simulated" not in result.reason
    assert "corruption" not in result.reason


def test_concurrent_registration_attempts_converge_on_one_workspace(
    tmp_path: Path,
) -> None:
    """Several equivalent `--init` attempts against an already-registered
    workspace, coordinated to start together.

    Established once, sequentially, before the race: this isolates catalogue
    registration contention from fresh-workspace creation. Filesystem qualification
    uses a unique, exclusively created probe per contender, so that gate no longer
    introduces shared-filename contention of its own.

    A non-blocking file lock -- the workspace's own storage lock and the
    installation catalogue's lifetime lock, both taken along this path --
    admits exactly one holder at a time and refuses every other transiently
    rather than queueing it, so this is a real race rather than a mocked one.
    Nothing here asserts *which* contender wins or how many transiently lose:
    only the convergence facts that must hold regardless of scheduling. A
    A transient loser may still see `UNQUALIFIED_FILESYSTEM` if its independent
    native lock probe genuinely fails. What must never appear is
    `WORKSPACE_REGISTRATION_CONFLICT` or `WRITE_FAILURE`:
    either would mean two contenders' settlements collided for real, which is
    exactly what the claim/allocation fencing this test exercises exists to
    prevent.
    """
    workspace_root, installation_root = _init(tmp_path)
    established = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )
    assert established.status is WorkspaceInitStatus.INITIALISED
    workspace_id = established.workspace_id
    assert workspace_id is not None

    contenders = 6
    barrier = threading.Barrier(contenders)

    def _attempt() -> WorkspaceInitResult:
        barrier.wait()
        return initialise_and_register_managed_local_workspace(
            workspace_root=workspace_root, installation_root=installation_root
        )

    with ThreadPoolExecutor(max_workers=contenders) as pool:
        results = list(pool.map(lambda _: _attempt(), range(contenders)))

    assert len(results) == contenders
    settled = [r for r in results if r.status is not WorkspaceInitStatus.REFUSED]
    refused = [r for r in results if r.status is WorkspaceInitStatus.REFUSED]
    # A non-blocking lock starts free, so whichever contender reaches it first
    # -- in any interleaving -- is admitted; not every contender can lose.
    assert settled
    assert {r.refusal for r in refused} <= {
        WorkspaceInitRefusal.WORKSPACE_BUSY,
        WorkspaceInitRefusal.UNQUALIFIED_FILESYSTEM,
    }
    assert {r.workspace_id for r in settled} == {workspace_id}

    store = open_installation_store(installation_root, owner_instance_id="checker")
    try:
        assert store.list_workspace_ids() == (workspace_id,)
    finally:
        store.close()

    retry = initialise_and_register_managed_local_workspace(
        workspace_root=workspace_root, installation_root=installation_root
    )
    assert retry.status is WorkspaceInitStatus.ALREADY_INITIALISED
    assert retry.workspace_id == workspace_id
    store = open_installation_store(installation_root, owner_instance_id="checker")
    try:
        assert store.list_workspace_ids() == (workspace_id,)
    finally:
        store.close()
