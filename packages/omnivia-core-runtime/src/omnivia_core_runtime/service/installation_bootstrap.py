"""Durable installation-catalogue registration for the `--init`-bootstrapped workspace.

`omnivia-core-service --init` (R004-10, `workspace_init.py`) creates the one
managed-local workspace at the caller-named root directly on the filesystem: no
session, no request envelope, no authenticated caller stands behind it, because the
trusted local operator invoking the CLI *is* the authority for that call.
`initialise_workspace` on its own only ever touches that workspace's own database
and manifest -- it never opens the installation catalogue -- so the workspace it
creates has no row in `omnivia_installation_workspaces`, and every
installation-authorised operation against it (`configure_mcp_setup` among them)
refuses with "not in this installation's authorised inventory".

This module closes that gap the same way `workspace.create` earns a place in the
inventory for a server-minted target (`installation.py`): one claimed allocation,
verified against the real workspace on disk, settled as active through the same
durable claim/allocation/outcome/audit chain -- never a bypass of the
`omnivia_installation_workspaces` foreign key, and never an unconditional adoption
of whatever a caller says is on disk. The one difference is the identity:
`workspace.create` mints a fresh workspace id and path from server configuration;
`initialise_and_register_managed_local_workspace` registers the *existing* id and
path `initialise_workspace` already committed to on this exact call, and only
after that bootstrap succeeded. A second call for the same workspace id resumes or
replays exactly like a repeated `workspace.create`; a workspace id already
registered at a *different* path is a conflict and is refused
(`WORKSPACE_REGISTRATION_CONFLICT`) rather than silently re-pointing an existing
authorisation.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Final

from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core_runtime.ownership.locks import LockRole, create_lock
from omnivia_core_runtime.service.installation import (
    InstallationSeamFault,
    verify_workspace_result,
)
from omnivia_core_runtime.service.mutation import WORKSPACE_ADMINISTRATION_PURPOSE
from omnivia_core_runtime.service.workspace_init import (
    WorkspaceInitRefusal,
    WorkspaceInitResult,
    WorkspaceInitStatus,
    initialise_workspace,
)
from omnivia_core_runtime.storage.installation_store import (
    AllocationState,
    InstallationBusy,
    InstallationIdempotencyConflict,
    InstallationStore,
    InstallationStoreError,
    NewInstallationAllocation,
    open_installation_store,
)
from omnivia_core_runtime.workspace.layout import WorkspaceLayout
from omnivia_core_runtime.workspace.manifest_store import (
    ManifestStoreError,
    read_manifest,
)

#: The audit vocabulary this registration records itself under. Distinct from
#: `workspace.create`'s `WORKSPACE_CREATE_OPERATION`: this is not that request, it
#: is the trusted local bootstrap admitting a workspace it did not mint.
BOOTSTRAP_REGISTER_OPERATION: Final = "installation.bootstrap_register_workspace"

#: Recorded as the acting principal and the catalogue's lock holder. Diagnostic
#: only, like `workspace_init.LOCK_HOLDER` -- no session names this call.
BOOTSTRAP_PRINCIPAL: Final = "omnivia-core-service --init"

#: The role recorded on the one-shot grant-use ledger entry for this registration.
BOOTSTRAP_REQUIRED_ROLE: Final = "installation_bootstrap"

#: Fixed sentence for every unexpected internal-fault refusal below. No exception
#: text or path is interpolated into it: an internal storage fault can carry
#: whatever a driver or the filesystem said, which is exactly the free-form
#: server diagnostic this trusted local result document must never repeat.
_INTERNAL_FAULT: Final = (
    "the installation catalogue could not durably register this workspace"
)


def initialise_and_register_managed_local_workspace(
    *,
    workspace_root: Path,
    installation_root: Path,
    core_version: str = "0.1.0",
) -> WorkspaceInitResult:
    """Bootstrap the canonical managed-local workspace and admit it to the catalogue.

    Every filesystem decision and refusal is `initialise_workspace`'s alone: a
    refusal there is returned exactly as it stands, before the installation
    catalogue is even opened, so none of its refusals gain a catalogue side
    effect. Only a successful bootstrap -- fresh or already there -- goes on to
    register, and registration is itself idempotent and replay-safe.
    """
    result = initialise_workspace(
        workspace_root=workspace_root,
        installation_root=installation_root,
        core_version=core_version,
    )
    if result.status is WorkspaceInitStatus.REFUSED:
        return result
    if result.workspace_id is None or result.workspace_root is None:
        raise InstallationStoreError(
            "a successful workspace bootstrap did not carry a workspace identity"
        )

    refused = _register(
        workspace_id=result.workspace_id,
        workspace_root=result.workspace_root.resolve(),
        installation_root=installation_root,
    )
    if refused is None:
        return result
    refusal, reason = refused
    return WorkspaceInitResult(
        status=WorkspaceInitStatus.REFUSED,
        refusal=refusal,
        reason=reason,
        workspace_id=result.workspace_id,
        workspace_root=result.workspace_root,
        installation_root=installation_root,
        workspace_format_version=result.workspace_format_version,
    )


def _register(
    *, workspace_id: str, workspace_root: Path, installation_root: Path
) -> tuple[WorkspaceInitRefusal, str] | None:
    """`None` on success (including "already registered"); a refusal otherwise."""
    try:
        store = open_installation_store(
            installation_root, owner_instance_id=BOOTSTRAP_PRINCIPAL
        )
    except InstallationBusy:
        return (
            WorkspaceInitRefusal.WORKSPACE_BUSY,
            (
                "another process owns the installation catalogue for this "
                "workspace; stop the service that owns it and try again"
            ),
        )
    except InstallationStoreError:
        return (WorkspaceInitRefusal.WRITE_FAILURE, _INTERNAL_FAULT)

    try:
        return _claim_and_settle(
            store, workspace_id=workspace_id, workspace_root=workspace_root
        )
    finally:
        store.close()


def _claim_and_settle(
    store: InstallationStore, *, workspace_id: str, workspace_root: Path
) -> tuple[WorkspaceInitRefusal, str] | None:
    authority = store.authority

    # Checked first, against the catalogue's own registration rather than this
    # bootstrap's idempotency scope: an active registration for this exact
    # workspace id can have been settled under a wholly different operation and
    # idempotency key -- `workspace.create`'s own, or another claim entirely --
    # which this bootstrap's own scope would never find. Deciding from the
    # registration itself, before any claim is minted, is what makes a matching
    # path replay and a conflicting one refuse `WORKSPACE_REGISTRATION_CONFLICT`
    # regardless of which operation registered it first -- rather than the
    # matching case reaching the same `omnivia_installation_workspaces` unique
    # target index as the conflicting one and both surfacing as an opaque
    # `WRITE_FAILURE`. Reading here is already race-safe: this store holds the
    # catalogue's one exclusive lifetime lock for as long as it stays open, so
    # no other owner can settle a competing registration between this read and
    # the claim below.
    try:
        existing_path = store.get_registered_workspace_path(workspace_id)
    except InstallationStoreError:
        return (WorkspaceInitRefusal.WRITE_FAILURE, _INTERNAL_FAULT)
    if existing_path is not None:
        if existing_path == workspace_root:
            return None
        return (
            WorkspaceInitRefusal.WORKSPACE_REGISTRATION_CONFLICT,
            (
                f"{workspace_id} is already registered in this installation's "
                "catalogue at a different path; nothing was written"
            ),
        )

    request_digest = "sha256:" + hashlib.sha256(
        to_canonical_json(
            {"workspace_id": workspace_id, "workspace_root": str(workspace_root)}
        ).encode("utf-8")
    ).hexdigest()

    def _mint() -> NewInstallationAllocation:
        return NewInstallationAllocation(
            audit_ref=f"iaud-{uuid.uuid4()}",
            claim_id=f"iclaim-{uuid.uuid4()}",
            allocation_id=f"ialloc-{uuid.uuid4()}",
            target_workspace_id=workspace_id,
            target_path=workspace_root,
        )

    try:
        claim = store.claim_allocation(
            authority,
            principal_id=BOOTSTRAP_PRINCIPAL,
            operation=BOOTSTRAP_REGISTER_OPERATION,
            purpose=WORKSPACE_ADMINISTRATION_PURPOSE,
            idempotency_key=f"bootstrap:{workspace_id}",
            request_digest=request_digest,
            identity_factory=_mint,
        )
    except InstallationIdempotencyConflict:
        return (
            WorkspaceInitRefusal.WORKSPACE_REGISTRATION_CONFLICT,
            (
                f"{workspace_id} is already registered in this installation's "
                "catalogue at a different path; nothing was written"
            ),
        )
    except InstallationStoreError:
        return (WorkspaceInitRefusal.WRITE_FAILURE, _INTERNAL_FAULT)

    if claim.outcome is not None:
        return None

    allocation = claim.allocation
    if allocation.state is AllocationState.ACTIVE:
        # Corruption: an active allocation with no outcome cannot arise from this
        # store's own transactional writes. Fail closed rather than guess.
        return (WorkspaceInitRefusal.WRITE_FAILURE, _INTERNAL_FAULT)
    if allocation.state is AllocationState.FAILED_RECOVERABLE:
        try:
            allocation = store.resume_allocation(
                authority, allocation_id=allocation.allocation_id
            )
        except InstallationStoreError:
            return (WorkspaceInitRefusal.WRITE_FAILURE, _INTERNAL_FAULT)

    # Held across verification and settlement so nothing else can mutate the
    # workspace between reading it and recording what was read, mirroring the
    # same lock `workspace.create` takes for the same reason.
    target_lock = create_lock(
        WorkspaceLayout(root=workspace_root).locks_path / "storage.lock",
        LockRole.LIFETIME_STORAGE,
        {"holder": "installation-bootstrap-registration"},
    )
    try:
        held = target_lock.acquire()
    except OSError:
        return (WorkspaceInitRefusal.WRITE_FAILURE, _INTERNAL_FAULT)
    if not held:
        return (
            WorkspaceInitRefusal.WORKSPACE_BUSY,
            (
                "another process holds the storage lock for this workspace; "
                "stop the service that owns it and try again"
            ),
        )
    try:
        try:
            manifest = read_manifest(WorkspaceLayout(root=workspace_root))
            display_name = manifest.name or workspace_id
            result = verify_workspace_result(
                allocation, expected_display_name=display_name
            )
        except (ManifestStoreError, InstallationSeamFault):
            return (WorkspaceInitRefusal.WRITE_FAILURE, _INTERNAL_FAULT)

        canonical = to_canonical_json(result)
        outcome_digest = "sha256:" + hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()
        try:
            store.settle_allocation_success(
                authority,
                allocation_id=allocation.allocation_id,
                workspace_label=manifest.name,
                outcome_id=f"iout-{uuid.uuid4()}",
                outcome_json=canonical,
                outcome_digest=outcome_digest,
                execution_id=f"iex-{uuid.uuid4()}",
                grant_id=f"ibgr-{uuid.uuid4()}",
                required_role=BOOTSTRAP_REQUIRED_ROLE,
                settlement_guard=lambda: None,
            )
        except InstallationStoreError:
            # A concurrent registration may have settled this exact allocation
            # between this call's claim and its own settlement. That recovery
            # read can itself fail the same way the settlement just did; either
            # way this closes on WRITE_FAILURE rather than letting the second
            # fault escape.
            try:
                settled = store.get_outcome(allocation.claim_id) is not None
            except InstallationStoreError:
                settled = False
            if settled:
                return None
            return (WorkspaceInitRefusal.WRITE_FAILURE, _INTERNAL_FAULT)
        return None
    finally:
        target_lock.release()


__all__ = [
    "BOOTSTRAP_PRINCIPAL",
    "BOOTSTRAP_REGISTER_OPERATION",
    "BOOTSTRAP_REQUIRED_ROLE",
    "initialise_and_register_managed_local_workspace",
]
