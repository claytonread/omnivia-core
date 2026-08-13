"""Installation-bound authority and recoverable ``workspace.create`` coordination.

The installation service is not a workspace service with a missing workspace id.  It
owns the machine-local catalogue, derives every target from server configuration, and
uses a durable claim before touching the filesystem.  A retry therefore resumes one
target; it never selects or mints another.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    DEFAULT_RETRY_CLASSIFICATION,
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_BOOTSTRAP_IN_PROGRESS,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    RETRY_CLASS_NON_RETRYABLE,
    CapabilityRef,
    ContractDecodeError,
    ContractSemanticError,
    GrantedAuthority,
    IdempotencyEquivalence,
    PageMetadata,
    Purpose,
    RequestEnvelope,
    RequestMetadata,
    Scope,
    WorkspaceCompatibility,
    WorkspaceCreateInput,
    WorkspaceCreateResult,
    WorkspaceDescriptor,
    WorkspaceListInput,
    WorkspaceListResult,
    classify_version_compatibility,
    idempotency_equivalence,
    to_canonical_json,
    validate_page_limit,
)
from omnivia_core.contracts.v1.canonical_json import canonicalize, parse_json_document
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.ownership.locks import LockRole, create_lock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    AuthorizedApplicationContext,
    ServiceBinding,
)
from omnivia_core_runtime.service.handlers.workspace import (
    WORKSPACE_STATUS_ACTIVE,
    canonical_timestamp,
)
from omnivia_core_runtime.service.mutation import (
    DEFAULT_GRANT_LIFETIME_US,
    INSTALLATION_ADMINISTRATOR_ROLE,
    WORKSPACE_ADMINISTRATION_PURPOSE,
)
from omnivia_core_runtime.service.operations import OperationError
from omnivia_core_runtime.service.versions import (
    SUPPORTED_WORKSPACE_ORDINALS,
    build_version_window,
    workspace_contract_version,
)
from omnivia_core_runtime.service.workspace_init import (
    WORKSPACE_FORMAT_VERSION,
    WorkspaceInitRefusal,
    WorkspaceInitResult,
    WorkspaceInitStatus,
    initialise_allocated_workspace,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    StorageError,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.installation_store import (
    AllocationClaim,
    AllocationState,
    InstallationAllocation,
    InstallationAuthority,
    InstallationIdempotencyConflict,
    InstallationOutcome,
    InstallationStore,
    InstallationStoreError,
    NewInstallationAllocation,
)
from omnivia_core_runtime.storage.migrations import (
    BASELINE_PRISTINE,
    applied_migrations,
    canonical_schema_fingerprint,
    load_migrations,
    read_workspace_state,
)
from omnivia_core_runtime.workspace.layout import WorkspaceLayout
from omnivia_core_runtime.workspace.manifest_store import (
    ManifestStoreError,
    read_manifest,
)

WORKSPACE_CREATE_OPERATION: Final = "workspace.create"
WORKSPACE_LIST_OPERATION: Final = "workspace.list"
WORKSPACE_LIST_PURPOSE: Final = "workspace_inspection"

_MESSAGE_NO_AUTHORITY: Final = (
    "the installation mutation grant does not cover this request and target"
)
_MESSAGE_EXPIRED: Final = "the installation mutation grant is no longer current"
_MESSAGE_CONFLICT: Final = (
    "the idempotency key is already bound to a different canonical request"
)
_MESSAGE_BOOTSTRAP_BUSY: Final = "the accepted workspace target is still being prepared"
_MESSAGE_BOOTSTRAP_FAILED: Final = (
    "the accepted workspace target could not be prepared by this attempt"
)
_MESSAGE_STORED_OUTCOME: Final = (
    "the stored installation outcome is not an exact canonical workspace result"
)
_MESSAGE_INVALID_LIST_INPUT: Final = (
    "the workspace list input is not valid for this operation"
)
_MESSAGE_LIST_AUTHORITY: Final = (
    "the installation read grant does not cover this inventory"
)

_SUPPORTED_WORKSPACE_VERSIONS: Final = build_version_window(
    workspace_contract_version(min(SUPPORTED_WORKSPACE_ORDINALS)),
    workspace_contract_version(max(SUPPORTED_WORKSPACE_ORDINALS)),
)


def _monotonic_us(clock: Clock) -> int:
    return int(clock.monotonic() * 1_000_000)


def _wall_us(clock: Clock) -> int:
    return int(clock.wall_time().timestamp() * 1_000_000)


class InstallationMutationDenied(OperationError):
    def __init__(self, message: str) -> None:
        super().__init__(
            ERROR_CODE_AUTHORIZATION_DENIED,
            message,
            retry_class=RETRY_CLASS_NON_RETRYABLE,
        )


class InstallationIdempotencyDenied(OperationError):
    def __init__(self) -> None:
        super().__init__(
            ERROR_CODE_IDEMPOTENCY_CONFLICT,
            _MESSAGE_CONFLICT,
            retry_class=DEFAULT_RETRY_CLASSIFICATION[ERROR_CODE_IDEMPOTENCY_CONFLICT],
        )


class InstallationBootstrapInProgress(OperationError):
    def __init__(self) -> None:
        super().__init__(
            ERROR_CODE_BOOTSTRAP_IN_PROGRESS,
            _MESSAGE_BOOTSTRAP_BUSY,
            retry_class=DEFAULT_RETRY_CLASSIFICATION[ERROR_CODE_BOOTSTRAP_IN_PROGRESS],
        )


class InstallationSeamFault(OperationError):
    def __init__(self, message: str = _MESSAGE_BOOTSTRAP_FAILED) -> None:
        super().__init__(
            ERROR_CODE_INTERNAL_NON_RECOVERABLE,
            message,
            retry_class=RETRY_CLASS_NON_RETRYABLE,
        )


_INSTALLATION_ISSUER_MARK: Final = object()


@dataclass(frozen=True, init=False)
class InstallationMutationGrant:
    """One process-local, server-issued permission for one claimed allocation."""

    _issuer_mark: object = field(repr=False, compare=False)
    grant_id: str
    installation_id: str
    fencing_generation: int
    allocation_id: str
    target_workspace_id: str
    target_path: Path
    principal_id: str
    required_role: str
    scopes: frozenset[str]
    capabilities: tuple[CapabilityRef, ...]
    operation: str
    purpose: str
    idempotency_key: str
    request_fingerprint: str
    issued_at_us: int
    issued_monotonic_us: int
    expires_monotonic_us: int

    def __init__(self, issuer_mark: object | None = None) -> None:
        if issuer_mark is not _INSTALLATION_ISSUER_MARK:
            raise TypeError("InstallationMutationGrant values are issued by the server")
        object.__setattr__(self, "_issuer_mark", issuer_mark)

    @property
    def server_issued(self) -> bool:
        return getattr(self, "_issuer_mark", None) is _INSTALLATION_ISSUER_MARK

    def is_current(self, now_monotonic_us: int) -> bool:
        return self.issued_monotonic_us <= now_monotonic_us < self.expires_monotonic_us


@dataclass(frozen=True)
class PreparedWorkspaceCreate:
    """The durable target and grant produced before filesystem work begins."""

    input: WorkspaceCreateInput
    context: AuthorizedApplicationContext
    equivalence: IdempotencyEquivalence
    claim: AllocationClaim
    grant: InstallationMutationGrant


@dataclass(frozen=True)
class WorkspaceCreateExecution:
    result: Mapping[str, Any]
    replayed: bool
    allocation_id: str
    target_workspace_id: str


WorkspaceBootstrapper = Callable[..., WorkspaceInitResult]


@dataclass(frozen=True)
class InstallationOperationContext:
    """The exact server-authorized facts visible to an installation handler.

    Installation operations have no selected workspace. Keeping this context beside
    the installation service prevents either inventing a workspace id or weakening
    the existing workspace handler contract. Session and binding stay in the
    server-owned handler composition, so a request cannot replace them.
    """

    request: RequestEnvelope
    principal: str
    installation_id: str
    granted_operations: frozenset[str]
    authorization: AuthorizedApplicationContext
    service: Any = None
    authority: GrantedAuthority | None = None
    scopes: tuple[Scope, ...] | None = None
    purpose: Purpose | None = None


def _same_allocation_identity(
    durable: InstallationAllocation, prepared: InstallationAllocation
) -> bool:
    """Compare the immutable allocation facts while allowing lifecycle progress."""
    return (
        durable.allocation_id == prepared.allocation_id
        and durable.target_workspace_id == prepared.target_workspace_id
        and durable.target_path == prepared.target_path
        and durable.principal_id == prepared.principal_id
        and durable.operation == prepared.operation
        and durable.purpose == prepared.purpose
        and durable.claim_id == prepared.claim_id
        and durable.audit_ref == prepared.audit_ref
    )


class InstallationApplicationService:
    """One installation-bound application service and allocation coordinator."""

    def __init__(
        self,
        *,
        store: InstallationStore,
        installation_root: Path,
        workspace_storage_root: Path,
        core_version: str,
        clock: Clock,
        bootstrapper: WorkspaceBootstrapper = initialise_allocated_workspace,
    ) -> None:
        if not workspace_storage_root.is_absolute():
            raise ValueError("workspace storage root must be an absolute server path")
        self._store = store
        self._installation_root = installation_root.resolve()
        if self._installation_root != store.installation_root:
            raise ValueError(
                "installation service root must match the owned catalogue root"
            )
        self._workspace_storage_root = workspace_storage_root.resolve()
        self._core_version = core_version
        self._clock = clock
        self._bootstrapper = bootstrapper

    @property
    def authority(self) -> InstallationAuthority:
        return self._store.authority

    def prepare_workspace_create(
        self,
        context: AuthorizedApplicationContext,
        *,
        session: AuthenticatedSession,
        binding: ServiceBinding,
        input_: WorkspaceCreateInput,
        equivalence: IdempotencyEquivalence,
        lifetime_us: int = DEFAULT_GRANT_LIFETIME_US,
    ) -> PreparedWorkspaceCreate:
        """Claim the scope and issue a grant bound to its server-minted target."""
        self._validate_request_authority(
            context,
            session=session,
            binding=binding,
            input_=input_,
            equivalence=equivalence,
        )
        try:
            claim = self._store.claim_allocation(
                self.authority,
                principal_id=context.principal_id,
                operation=WORKSPACE_CREATE_OPERATION,
                purpose=WORKSPACE_ADMINISTRATION_PURPOSE,
                idempotency_key=equivalence.scope.idempotency_key,
                request_digest=equivalence.fingerprint,
                identity_factory=self._mint_allocation,
            )
        except InstallationIdempotencyConflict as error:
            raise InstallationIdempotencyDenied() from error
        except InstallationStoreError as error:
            raise InstallationSeamFault() from error

        grant = self._issue_grant(
            context,
            session=session,
            equivalence=equivalence,
            allocation=claim.allocation,
            lifetime_us=lifetime_us,
        )
        return PreparedWorkspaceCreate(
            input=input_,
            context=context,
            equivalence=equivalence,
            claim=claim,
            grant=grant,
        )

    def execute_workspace_create(
        self, prepared: PreparedWorkspaceCreate
    ) -> WorkspaceCreateExecution:
        """Replay or bootstrap and settle one previously prepared allocation."""
        self._require_grant(prepared)
        claim = prepared.claim
        grant = prepared.grant
        allocation = self._store.get_allocation(claim.allocation.allocation_id)
        if allocation is None or not _same_allocation_identity(
            allocation, claim.allocation
        ):
            raise InstallationSeamFault()
        outcome = self._store.get_outcome(allocation.claim_id)
        if outcome is not None:
            return self._replay(prepared, allocation, outcome)
        if allocation.state is AllocationState.ACTIVE:
            raise InstallationSeamFault()
        if allocation.state is AllocationState.FAILED_RECOVERABLE:
            try:
                allocation = self._store.resume_allocation(
                    self.authority, allocation_id=allocation.allocation_id
                )
            except InstallationStoreError as error:
                raise InstallationSeamFault() from error

        try:
            init = self._bootstrapper(
                workspace_root=allocation.target_path,
                installation_root=self._installation_root,
                target_workspace_id=allocation.target_workspace_id,
                display_name=prepared.input.display_name,
                core_version=self._core_version,
            )
        except Exception as error:
            self._record_recoverable_failure(prepared, allocation, "bootstrap_failed")
            raise InstallationSeamFault() from error

        if init.status is WorkspaceInitStatus.REFUSED:
            if init.refusal is WorkspaceInitRefusal.WORKSPACE_BUSY:
                # Another coordinator owns the exact server-minted target.  It may
                # still settle this allocation, so this contender must neither mint a
                # replacement nor mislabel the shared allocation as failed.
                raise InstallationBootstrapInProgress()
            detail = (
                "bootstrap_refused"
                if init.refusal is None
                else f"bootstrap_refused:{init.refusal.value}"
            )
            self._record_recoverable_failure(prepared, allocation, detail)
            raise InstallationSeamFault()

        # Reacquire the same lifetime lock used by init and the service, then keep it
        # through target verification and catalogue settlement.  A successful
        # bootstrap result is only a claim; the exact durable target is the proof.
        self._require_grant(prepared)
        target_lock = create_lock(
            WorkspaceLayout(root=allocation.target_path).locks_path / "storage.lock",
            LockRole.LIFETIME_STORAGE,
            {"holder": "installation-settlement"},
        )
        try:
            held = target_lock.acquire()
        except OSError as error:
            target_lock.release()
            self._record_recoverable_failure(
                prepared, allocation, "result_verification_failed"
            )
            raise InstallationSeamFault() from error
        if not held:
            raise InstallationBootstrapInProgress()
        try:
            try:
                result = self._workspace_result(
                    allocation, expected_display_name=prepared.input.display_name
                )
            except InstallationSeamFault:
                self._record_recoverable_failure(
                    prepared, allocation, "result_verification_failed"
                )
                raise
            canonical = to_canonical_json(result)
            outcome_digest = (
                "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            )
            # Verification can take time.  Recheck monotonic expiry at the last
            # service instruction before the atomic catalogue settlement.
            self._require_grant(prepared)
            try:
                self._store.settle_allocation_success(
                    self.authority,
                    allocation_id=allocation.allocation_id,
                    workspace_label=prepared.input.display_name,
                    outcome_id=f"iout-{uuid.uuid4()}",
                    outcome_json=canonical,
                    outcome_digest=outcome_digest,
                    execution_id=f"iex-{uuid.uuid4()}",
                    grant_id=grant.grant_id,
                    required_role=grant.required_role,
                    settlement_guard=lambda: self._require_grant(prepared),
                )
            except InstallationStoreError as error:
                # A concurrent equivalent execution can settle after this
                # execution's durable refresh but before it acquired the target.
                settled = self._store.get_outcome(allocation.claim_id)
                if settled is not None:
                    return self._replay(prepared, allocation, settled)
                raise InstallationSeamFault() from error
            return WorkspaceCreateExecution(
                result=result,
                replayed=False,
                allocation_id=allocation.allocation_id,
                target_workspace_id=allocation.target_workspace_id,
            )
        finally:
            target_lock.release()

    def create_workspace(
        self,
        context: AuthorizedApplicationContext,
        *,
        session: AuthenticatedSession,
        binding: ServiceBinding,
        input_: WorkspaceCreateInput,
        equivalence: IdempotencyEquivalence,
        lifetime_us: int = DEFAULT_GRANT_LIFETIME_US,
    ) -> WorkspaceCreateExecution:
        prepared = self.prepare_workspace_create(
            context,
            session=session,
            binding=binding,
            input_=input_,
            equivalence=equivalence,
            lifetime_us=lifetime_us,
        )
        return self.execute_workspace_create(prepared)

    def create_workspace_from_authorized_request(
        self,
        context: AuthorizedApplicationContext,
        *,
        session: AuthenticatedSession,
        binding: ServiceBinding,
        input_: WorkspaceCreateInput,
        lifetime_us: int = DEFAULT_GRANT_LIFETIME_US,
    ) -> WorkspaceCreateExecution:
        """Issue S0 authority for, execute, and settle one admitted request.

        The equivalence is recomputed here from the exact authorized context rather
        than accepted from a handler.  This is the production entry: a handler can
        supply the decoded domain input, but cannot choose the bytes or scope an
        idempotency key becomes bound to.
        """
        equivalence = self._expected_equivalence(context, input_)
        return self.create_workspace(
            context,
            session=session,
            binding=binding,
            input_=input_,
            equivalence=equivalence,
            lifetime_us=lifetime_us,
        )

    def list_workspaces(
        self,
        context: AuthorizedApplicationContext,
        *,
        session: AuthenticatedSession,
        binding: ServiceBinding,
        input_: WorkspaceListInput,
    ) -> Mapping[str, Any]:
        """List only the active inventory of the authorized installation.

        Enumeration starts at the catalogue owned by this service; neither the
        request nor a continuation token contributes a filesystem path or an
        installation identifier.  Tokens are bound to this installation owner,
        principal and page size, and expire when the catalogue fencing generation
        moves.
        """
        self._validate_list_authority(
            context, session=session, binding=binding, input_=input_
        )
        valid_limit = True
        try:
            validate_page_limit(input_.limit)
        except ContractSemanticError:
            valid_limit = False
        if not valid_limit:
            raise OperationError(
                ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_LIST_INPUT
            )

        limit = 1000 if input_.limit is None else input_.limit
        requested_token = (
            None if input_.page is None else input_.page.continuation_token
        )
        if input_.page is not None and (
            requested_token is None or not 1 <= len(requested_token) <= 512
        ):
            raise OperationError(
                ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_LIST_INPUT
            )

        try:
            inventory = self._store.list_workspace_outcomes()
        except InstallationStoreError as error:
            raise InstallationSeamFault() from error

        start = 0
        if requested_token is not None:
            found: int | None = None
            for index, (workspace_id, _outcome) in enumerate(inventory):
                if self._list_cursor(
                    principal_id=context.principal_id,
                    limit=limit,
                    workspace_id=workspace_id,
                ) == requested_token:
                    found = index + 1
                    break
            if found is None:
                raise OperationError(
                    ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_LIST_INPUT
                )
            start = found

        page_entries = inventory[start : start + limit]
        descriptors: list[WorkspaceDescriptor] = []
        for workspace_id, outcome in page_entries:
            result = WorkspaceCreateResult.from_wire(_decode_outcome(outcome))
            if result.workspace.workspace_id != workspace_id:
                raise InstallationSeamFault(_MESSAGE_STORED_OUTCOME)
            descriptors.append(result.workspace)

        end = start + len(page_entries)
        next_token = None
        if end < len(inventory) and page_entries:
            next_token = self._list_cursor(
                principal_id=context.principal_id,
                limit=limit,
                workspace_id=page_entries[-1][0],
            )
        return WorkspaceListResult(
            workspaces=tuple(descriptors),
            page=PageMetadata(continuation_token=next_token),
        ).to_wire()

    def _validate_list_authority(
        self,
        context: AuthorizedApplicationContext,
        *,
        session: AuthenticatedSession,
        binding: ServiceBinding,
        input_: WorkspaceListInput,
    ) -> None:
        authority = self.authority
        if (
            not isinstance(input_, WorkspaceListInput)
            or context.operation != WORKSPACE_LIST_OPERATION
            or context.workspace_id is not None
            or context.installation_id != authority.installation_id
            or binding.installation_id != authority.installation_id
            or binding.workspace_id is not None
            or context.principal_id != session.principal_id
            or authority.installation_id not in session.installations
            or WORKSPACE_LIST_OPERATION not in session.operations
            or context.purpose != WORKSPACE_LIST_PURPOSE
            or not frozenset(context.scopes) <= session.scopes
        ):
            raise InstallationMutationDenied(_MESSAGE_LIST_AUTHORITY)

    def _list_cursor(
        self, *, principal_id: str, limit: int, workspace_id: str
    ) -> str:
        bound = "\0".join(
            (
                "workspace-list-v1",
                self.authority.installation_id,
                str(self.authority.fencing_generation),
                principal_id,
                str(limit),
                workspace_id,
            )
        ).encode("utf-8")
        return "wsl1-" + hashlib.sha256(bound).hexdigest()

    def _validate_request_authority(
        self,
        context: AuthorizedApplicationContext,
        *,
        session: AuthenticatedSession,
        binding: ServiceBinding,
        input_: WorkspaceCreateInput,
        equivalence: IdempotencyEquivalence,
    ) -> None:
        authority = self.authority
        if not isinstance(input_, WorkspaceCreateInput):
            raise InstallationMutationDenied(_MESSAGE_NO_AUTHORITY)
        if not 1 <= len(input_.display_name) <= 256:
            raise InstallationMutationDenied(_MESSAGE_NO_AUTHORITY)
        if (
            context.operation != WORKSPACE_CREATE_OPERATION
            or context.workspace_id is not None
            or context.installation_id != authority.installation_id
            or binding.installation_id != authority.installation_id
            or binding.workspace_id is not None
            or context.principal_id != session.principal_id
            or authority.installation_id not in session.installations
            or WORKSPACE_CREATE_OPERATION not in session.operations
            or context.purpose != WORKSPACE_ADMINISTRATION_PURPOSE
            or INSTALLATION_ADMINISTRATOR_ROLE not in session.roles
            or INSTALLATION_ADMINISTRATOR_ROLE not in context.roles
            or not frozenset(context.scopes) <= session.scopes
        ):
            raise InstallationMutationDenied(_MESSAGE_NO_AUTHORITY)
        key = context.idempotency_key
        scope = equivalence.scope
        expected = self._expected_equivalence(context, input_)
        if (
            key is None
            or scope.principal_id != context.principal_id
            or scope.workspace_id is not None
            or scope.operation != WORKSPACE_CREATE_OPERATION
            or scope.idempotency_key != key
            or not equivalence.fingerprint.startswith("sha256:")
            or equivalence != expected
        ):
            raise InstallationMutationDenied(_MESSAGE_NO_AUTHORITY)

    def _expected_equivalence(
        self,
        context: AuthorizedApplicationContext,
        input_: WorkspaceCreateInput,
    ) -> IdempotencyEquivalence:
        key = context.idempotency_key
        if key is None:
            raise InstallationMutationDenied(_MESSAGE_NO_AUTHORITY)
        metadata = RequestMetadata(
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            api_version=context.api_version,
            client=context.client,
            workspace_id=context.workspace_id,
            scopes=context.scopes,
            purpose=context.purpose,
            deadline_ms=context.deadline_ms,
            idempotency_key=key,
            mutation_precondition=context.mutation_precondition,
            required_capabilities=context.required_capabilities,
        )
        try:
            return idempotency_equivalence(
                context.operation,
                metadata,
                input_.to_wire(),
                principal_id=context.principal_id,
                workspace_id=context.workspace_id,
            )
        except (ContractSemanticError, TypeError, ValueError) as error:
            raise InstallationMutationDenied(_MESSAGE_NO_AUTHORITY) from error

    def _mint_allocation(self) -> NewInstallationAllocation:
        workspace_id = f"ws-{uuid.uuid4()}"
        target = (self._workspace_storage_root / workspace_id).resolve()
        if target.parent != self._workspace_storage_root:
            raise InstallationSeamFault()
        return NewInstallationAllocation(
            audit_ref=f"iaud-{uuid.uuid4()}",
            claim_id=f"iclaim-{uuid.uuid4()}",
            allocation_id=f"ialloc-{uuid.uuid4()}",
            target_workspace_id=workspace_id,
            target_path=target,
        )

    def _issue_grant(
        self,
        context: AuthorizedApplicationContext,
        *,
        session: AuthenticatedSession,
        equivalence: IdempotencyEquivalence,
        allocation: InstallationAllocation,
        lifetime_us: int,
    ) -> InstallationMutationGrant:
        issued_wall_us = _wall_us(self._clock)
        issued_monotonic_us = _monotonic_us(self._clock)
        if lifetime_us <= 0 or issued_wall_us <= 0:
            raise InstallationMutationDenied(_MESSAGE_EXPIRED)
        grant = InstallationMutationGrant(_INSTALLATION_ISSUER_MARK)
        fields: Mapping[str, object] = {
            "grant_id": f"imgr-{uuid.uuid4()}",
            "installation_id": self.authority.installation_id,
            "fencing_generation": self.authority.fencing_generation,
            "allocation_id": allocation.allocation_id,
            "target_workspace_id": allocation.target_workspace_id,
            "target_path": allocation.target_path,
            "principal_id": session.principal_id,
            "required_role": INSTALLATION_ADMINISTRATOR_ROLE,
            "scopes": frozenset(context.scopes) & session.scopes,
            "capabilities": context.capabilities,
            "operation": WORKSPACE_CREATE_OPERATION,
            "purpose": WORKSPACE_ADMINISTRATION_PURPOSE,
            "idempotency_key": equivalence.scope.idempotency_key,
            "request_fingerprint": equivalence.fingerprint,
            "issued_at_us": issued_wall_us,
            "issued_monotonic_us": issued_monotonic_us,
            "expires_monotonic_us": issued_monotonic_us + lifetime_us,
        }
        for name, value in fields.items():
            object.__setattr__(grant, name, value)
        return grant

    def _require_grant(self, prepared: PreparedWorkspaceCreate) -> None:
        grant = prepared.grant
        allocation = prepared.claim.allocation
        context = prepared.context
        equivalence = prepared.equivalence
        expected = self._expected_equivalence(context, prepared.input)
        if (
            not grant.server_issued
            or grant.installation_id != self.authority.installation_id
            or grant.fencing_generation != self.authority.fencing_generation
            or grant.allocation_id != allocation.allocation_id
            or grant.target_workspace_id != allocation.target_workspace_id
            or grant.target_path != allocation.target_path
            or grant.principal_id != context.principal_id
            or grant.required_role not in context.roles
            or grant.scopes != frozenset(context.scopes)
            or grant.capabilities != context.capabilities
            or grant.operation != context.operation
            or grant.purpose != context.purpose
            or grant.idempotency_key != equivalence.scope.idempotency_key
            or grant.request_fingerprint != equivalence.fingerprint
            or equivalence != expected
        ):
            raise InstallationMutationDenied(_MESSAGE_NO_AUTHORITY)
        if not grant.is_current(_monotonic_us(self._clock)):
            raise InstallationMutationDenied(_MESSAGE_EXPIRED)

    def _replay(
        self,
        prepared: PreparedWorkspaceCreate,
        allocation: InstallationAllocation,
        outcome: InstallationOutcome,
    ) -> WorkspaceCreateExecution:
        result = _decode_outcome(outcome)
        self._require_grant(prepared)
        grant = prepared.grant
        try:
            self._store.record_replay_grant(
                self.authority,
                allocation_id=allocation.allocation_id,
                execution_id=f"iex-{uuid.uuid4()}",
                grant_id=grant.grant_id,
                required_role=grant.required_role,
                settlement_guard=lambda: self._require_grant(prepared),
            )
        except InstallationStoreError as error:
            raise InstallationSeamFault() from error
        return WorkspaceCreateExecution(
            result=result,
            replayed=True,
            allocation_id=allocation.allocation_id,
            target_workspace_id=allocation.target_workspace_id,
        )

    def _record_recoverable_failure(
        self,
        prepared: PreparedWorkspaceCreate,
        allocation: InstallationAllocation,
        detail: str,
    ) -> None:
        # Failure is a durable state transition and consumes the grant just as
        # success does.  It must not be recorded under authority that expired while
        # the bootstrapper was running.
        self._require_grant(prepared)
        grant = prepared.grant
        try:
            self._store.fail_allocation(
                self.authority,
                allocation_id=allocation.allocation_id,
                detail=detail,
                execution_id=f"iex-{uuid.uuid4()}",
                grant_id=grant.grant_id,
                required_role=grant.required_role,
                settlement_guard=lambda: self._require_grant(prepared),
            )
        except InstallationStoreError as error:
            raise InstallationSeamFault() from error

    def _workspace_result(
        self,
        allocation: InstallationAllocation,
        *,
        expected_display_name: str,
    ) -> Mapping[str, Any]:
        try:
            layout = WorkspaceLayout(root=allocation.target_path)
            problems = layout.validate(require_database=True)
            if problems:
                raise ValueError("allocated workspace layout is not complete")
            manifest = read_manifest(layout)
            if manifest.workspace_id != allocation.target_workspace_id:
                raise ValueError("workspace manifest identity differs from allocation")
            if manifest.name != expected_display_name:
                raise ValueError("workspace manifest label differs from request")
            if (
                manifest.integrity is None
                or not manifest.integrity_matches()
                or manifest.compatibility.workspace_format_version
                != WORKSPACE_FORMAT_VERSION
            ):
                raise ValueError("workspace manifest is not an exact current target")

            connection = open_database(
                layout.database_path,
                OpenMode.EXCLUSIVE_MAINTENANCE,
                enable_wal=False,
            )
            try:
                state = read_workspace_state(connection)
                migrations = load_migrations()
                expected_ledger = {
                    migration.version: migration.checksum for migration in migrations
                }
                user_version = connection.execute("PRAGMA user_version").fetchone()
                started = connection.execute(
                    "SELECT COUNT(*) FROM omnivia_migration_attempts "
                    "WHERE outcome = 'started'"
                ).fetchone()
                if (
                    state is None
                    or state.workspace_id != allocation.target_workspace_id
                    or state.workspace_format_version != WORKSPACE_FORMAT_VERSION
                    or state.baseline_state != BASELINE_PRISTINE
                    or applied_migrations(connection) != expected_ledger
                    or user_version is None
                    or not migrations
                    or int(user_version[0]) != migrations[-1].version
                    or started is None
                    or int(started[0]) != 0
                    or not fingerprint_schema(connection).matches(
                        canonical_schema_fingerprint()
                    )
                    or integrity_check(connection)
                    or foreign_key_check(connection)
                ):
                    raise ValueError(
                        "workspace database is not the exact settled target"
                    )
            finally:
                connection.close()

            version = workspace_contract_version(
                manifest.compatibility.workspace_format_version
            )
            descriptor = WorkspaceDescriptor(
                workspace_id=allocation.target_workspace_id,
                display_name=manifest.name or allocation.target_workspace_id,
                status=WORKSPACE_STATUS_ACTIVE,
                compatibility=WorkspaceCompatibility(
                    workspace_format_version=version,
                    supported_workspace_versions=_SUPPORTED_WORKSPACE_VERSIONS,
                    status=classify_version_compatibility(
                        version, _SUPPORTED_WORKSPACE_VERSIONS
                    ),
                ),
                created_at=canonical_timestamp(manifest.created_at),
            )
            result = WorkspaceCreateResult(workspace=descriptor).to_wire()
            WorkspaceCreateResult.from_wire(result)
            return result
        except (
            OSError,
            ValueError,
            ContractDecodeError,
            ManifestStoreError,
            sqlite3.Error,
            StorageError,
        ) as error:
            raise InstallationSeamFault() from error


def _decode_outcome(outcome: InstallationOutcome) -> Mapping[str, Any]:
    stored = outcome.outcome_json
    if stored is None or outcome.outcome_branch != "success":
        raise InstallationSeamFault(_MESSAGE_STORED_OUTCOME)
    expected = "sha256:" + hashlib.sha256(stored.encode("utf-8")).hexdigest()
    if outcome.outcome_digest != expected:
        raise InstallationSeamFault(_MESSAGE_STORED_OUTCOME)
    try:
        decoded = parse_json_document(stored)
        if canonicalize(decoded) != stored or not isinstance(decoded, dict):
            raise ValueError("stored result is not a canonical object")
        result = WorkspaceCreateResult.from_wire(decoded)
        wire = result.to_wire()
        if canonicalize(wire) != stored:
            raise ValueError("stored result does not round-trip exactly")
        return wire
    except (ContractDecodeError, TypeError, ValueError) as error:
        raise InstallationSeamFault(_MESSAGE_STORED_OUTCOME) from error


__all__ = [
    "WORKSPACE_CREATE_OPERATION",
    "WORKSPACE_LIST_OPERATION",
    "WORKSPACE_LIST_PURPOSE",
    "InstallationApplicationService",
    "InstallationBootstrapInProgress",
    "InstallationIdempotencyDenied",
    "InstallationMutationDenied",
    "InstallationMutationGrant",
    "InstallationOperationContext",
    "InstallationSeamFault",
    "PreparedWorkspaceCreate",
    "WorkspaceCreateExecution",
]
