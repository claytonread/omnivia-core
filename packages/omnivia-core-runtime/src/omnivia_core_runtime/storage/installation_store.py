"""Exclusive, fenced persistence for installation-scoped authority.

One :class:`InstallationStore` owns one lifetime file lock and one SQLite
connection.  The lock excludes another installation service; the persisted
generation makes a predecessor permanently stale even if it resumes with an old
Python object.  No writable connection escapes this module.

Two kinds of installation authority live here.  The workspace allocation family
records which workspaces this installation created and under whose authority.
The installed-MCP family below it records which host has a dedicated principal,
bound to which workspace, holding exactly which rights, verified against which
salted credential digest -- durable state that every MCP call is resolved against
afresh, so a rotation or a revocation lands on the next call rather than when
some cached session happens to expire.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from omnivia_core_runtime.ownership.locks import FileLock, LockRole, create_lock
from omnivia_core_runtime.storage.backup import InstallationLayout
from omnivia_core_runtime.storage.connection import (
    DEFAULT_BUSY_TIMEOUT_MS,
    StorageError,
    fingerprint_schema,
)
from omnivia_core_runtime.storage.installation_migrations import (
    INSTALLATION_FORMAT_VERSION,
    INSTALLATION_WRITER_FUNCTION,
    apply_initial_installation_schema,
    apply_pending_installation_migrations,
    installation_schema_present,
    verify_installation_schema,
)


class InstallationStoreError(StorageError):
    """The installation catalogue could not safely complete an operation."""


class InstallationBusy(InstallationStoreError):
    """Another installation owner holds the catalogue lifetime lock."""


class InstallationAuthorityError(InstallationStoreError):
    """A caller supplied an absent, stale or contradictory installation authority."""


class InstallationIdempotencyConflict(InstallationStoreError):
    """An installation idempotency scope is already bound to other request bytes."""


class AllocationState(str, Enum):
    """Durable two-phase workspace allocation lifecycle."""

    PREPARING = "preparing"
    ACTIVE = "active"
    FAILED_RECOVERABLE = "failed_recoverable"


@dataclass(frozen=True)
class InstallationAuthority:
    """The exact installation owner tuple every state change must prove."""

    installation_id: str
    owner_instance_id: str
    fencing_generation: int


@dataclass(frozen=True)
class NewInstallationAllocation:
    """Server-minted identities for a first, and only a first, claim."""

    audit_ref: str
    claim_id: str
    allocation_id: str
    target_workspace_id: str
    target_path: Path


@dataclass(frozen=True)
class InstallationAllocation:
    """One durable workspace target and its recovery state."""

    allocation_id: str
    target_workspace_id: str
    target_path: Path
    principal_id: str
    operation: str
    purpose: str
    claim_id: str
    audit_ref: str
    state: AllocationState
    state_detail: str | None
    fencing_generation: int


@dataclass(frozen=True)
class InstallationOutcome:
    """The exact terminal answer stored for an installation idempotency claim."""

    outcome_id: str
    claim_id: str
    outcome_branch: str
    error_code: str | None
    outcome_json: str | None
    outcome_reference: str | None
    outcome_digest: str
    audit_ref: str


@dataclass(frozen=True)
class AllocationClaim:
    """Result of claiming a scope: new work, a resumable target, or a replay."""

    created: bool
    allocation: InstallationAllocation
    outcome: InstallationOutcome | None


class McpHost(str, Enum):
    """A supported MCP host, spelled as the installed command names it."""

    CLAUDE_CODE = "claude-code"
    CODEX = "codex"


class McpProfile(str, Enum):
    """The exposure profile a setup was configured for."""

    RESTRICTED = "restricted"
    AUTHORING = "authoring"


class McpSetupStatus(str, Enum):
    """Whether a configured host's authority is live."""

    ACTIVE = "active"
    REVOKED = "revoked"


class McpGrantKind(str, Enum):
    """Which kind of right one grant row states.

    `ROLE` is the one kind that is not derivable from the frozen operation
    catalogue: R004 section 9.1 requires an authoring setup to hold "workspace
    contributor authority sufficient for `memory:write`", and the mutation
    coordinator asks for that as a role rather than as a scope. Storing it as a
    grant row is what makes it durable least-privilege state the service derives
    -- revoked with every other right when the generation advances -- rather than
    something a configuration file or a caller could assert about itself.
    """

    OPERATION = "operation"
    SCOPE = "scope"
    PURPOSE = "purpose"
    CAPABILITY = "capability"
    ROLE = "role"


@dataclass(frozen=True, order=True)
class McpGrant:
    """One exact right: never a pattern, never a set, never a wildcard.

    `version` is the capability's minimum contract version and belongs to a
    capability alone. Ordered so a stored policy and a requested one can be
    compared as sorted tuples rather than by whatever order either was built in.
    """

    kind: McpGrantKind
    value: str
    version: str | None = None


@dataclass(frozen=True)
class NewMcpSetup:
    """Server-minted identity and verification material for one provisioning.

    Minted by the caller, but only from inside the catalogue write transaction and
    only once the store has established that a write is actually needed -- so an
    idempotent reconfigure does not even generate a credential it would discard.

    No secret is here. `credential_digest` is `sha256:<salt || secret>` and
    `credential_reference` is an opaque public name; neither can produce the bearer
    that satisfies them.
    """

    audit_ref: str
    setup_id: str
    principal_id: str
    credential_reference: str
    credential_salt: str
    credential_digest: str
    grants: tuple[McpGrant, ...]


@dataclass(frozen=True)
class InstalledMcpSetup:
    """One durable MCP setup, as a value that is safe to print.

    Deliberately missing the salt and the digest as well as the secret. This is
    what `omnivia mcp status` reports and what a refusal may name, and a field that
    is not on the value cannot reach a log line, a `repr` or an error message by
    somebody's oversight.
    """

    setup_id: str
    host: McpHost
    workspace_id: str
    principal_id: str
    profile: McpProfile
    authoring_intent: bool
    credential_reference: str
    status: McpSetupStatus
    setup_generation: int
    created_at_us: int
    updated_at_us: int
    revoked_at_us: int | None


@dataclass(frozen=True)
class McpSetupOutcome:
    """A configure result: the durable setup, and whether it was rotated.

    `rotated` is false exactly when the requested state was already the live one,
    which is the only case in which the previous credential keeps working.
    """

    rotated: bool
    setup: InstalledMcpSetup


@dataclass(frozen=True)
class ResolvedMcpSetup:
    """An authenticated setup and the exact policy its current generation holds."""

    setup: InstalledMcpSetup
    grants: tuple[McpGrant, ...]


def mcp_credential_digest(salt: str, secret: str) -> str:
    """The stored verifier for one bearer secret: `sha256:<salt || secret>`.

    Salted per setup so two hosts that were somehow issued the same secret do not
    share a digest, and so a stored digest is useless against any other catalogue.

    A single SHA-256 rather than a password hash on purpose: the input is 256 bits
    of `secrets` randomness, not something a human chose, so there is no dictionary
    to slow down and no work factor that would buy anything against a search space
    nothing can enumerate. The cost that matters here is on verification, which
    happens on every call.
    """
    return "sha256:" + hashlib.sha256(f"{salt}{secret}".encode()).hexdigest()


#: The audit vocabulary the installed-MCP lifecycle records itself under, in the
#: installation's own append-only audit table.
_MCP_ADMINISTRATION_PURPOSE = "installed_mcp_administration"
_MCP_CONFIGURE_OPERATION = "mcp.setup.configure"
_MCP_REVOKE_OPERATION = "mcp.setup.revoke"

#: Every redaction-safe setup column, in :class:`InstalledMcpSetup` field order.
#: Named once so a query cannot select the salt or the digest by accident and a
#: reader cannot map a column to the wrong field.
_MCP_SETUP_COLUMNS = (
    "setup_id, host, workspace_id, principal_id, profile, authoring_intent, "
    "credential_reference, status, setup_generation, created_at_us, updated_at_us, "
    "revoked_at_us"
)


def _mcp_setup_from_row(row: Sequence[Any]) -> InstalledMcpSetup:
    """One setup row as its redacted value. Trailing columns are ignored.

    Ignoring them is what lets the authentication query select the salt and the
    digest it has to compare without those ever reaching the value it returns.
    """
    return InstalledMcpSetup(
        setup_id=str(row[0]),
        host=McpHost(str(row[1])),
        workspace_id=str(row[2]),
        principal_id=str(row[3]),
        profile=McpProfile(str(row[4])),
        authoring_intent=bool(int(row[5])),
        credential_reference=str(row[6]),
        status=McpSetupStatus(str(row[7])),
        setup_generation=int(row[8]),
        created_at_us=int(row[9]),
        updated_at_us=int(row[10]),
        revoked_at_us=None if row[11] is None else int(row[11]),
    )


def _checked_grants(grants: Sequence[McpGrant]) -> tuple[McpGrant, ...]:
    """The requested rights, proved exact, in canonical order.

    The schema refuses a wildcard too. This refuses it first, with a message that
    names what was wrong rather than an integrity error from three layers down --
    and it refuses the two things a CHECK constraint cannot see: an empty policy,
    which is not a least-privilege grant but the absence of one, and a right stated
    twice, which would make "the exact rights" a multiset nothing could compare
    against the profile it is supposed to be.
    """
    checked: list[McpGrant] = []
    for grant in grants:
        if not isinstance(grant, McpGrant):
            raise TypeError("installed MCP rights must be McpGrant values")
        if not 1 <= len(grant.value) <= 128 or _wildcard(grant.value):
            raise InstallationStoreError(
                "an installed MCP right must be an exact bounded value, never a "
                "wildcard"
            )
        if (grant.kind is McpGrantKind.CAPABILITY) != (grant.version is not None):
            raise InstallationStoreError(
                "a capability right states a minimum version and no other right may"
            )
        if grant.version is not None and (
            not 1 <= len(grant.version) <= 32 or _wildcard(grant.version)
        ):
            raise InstallationStoreError(
                "a capability right must state an exact bounded minimum version"
            )
        checked.append(grant)
    if not checked:
        raise InstallationStoreError(
            "an installed MCP setup must grant at least one exact right"
        )
    ordered = tuple(sorted(set(checked)))
    if len(ordered) != len(checked):
        raise InstallationStoreError("an installed MCP right was granted twice")
    return ordered


def _wildcard(value: str) -> bool:
    return "*" in value or "?" in value


def _wall_clock_us() -> int:
    return time.time_ns() // 1_000


def _installation_id() -> str:
    return f"inst-{uuid.uuid4()}"


def _connect_catalogue(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    uri = path.resolve().as_uri().replace("file://", "file:", 1)
    connection = sqlite3.connect(
        f"{uri}?mode=rwc",
        uri=True,
        check_same_thread=False,
        isolation_level=None,
    )
    try:
        connection.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA locking_mode = EXCLUSIVE")
        journal = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        if journal is not None and str(journal[0]).lower() not in ("wal", "memory"):
            raise InstallationStoreError(
                f"installation catalogue could not enable WAL ({journal[0]!r})"
            )
        connection.execute("PRAGMA foreign_keys = ON")
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()
        if enabled is None or int(enabled[0]) != 1:
            raise InstallationStoreError(
                "installation catalogue could not enable foreign keys"
            )
        connection.create_function(
            INSTALLATION_WRITER_FUNCTION, 0, lambda: 1, deterministic=True
        )
        return connection
    except BaseException:
        connection.close()
        raise


class InstallationStore:
    """The sole write-capable installation catalogue owner."""

    def __init__(
        self,
        *,
        layout: InstallationLayout,
        lock: FileLock,
        connection: sqlite3.Connection,
        authority: InstallationAuthority,
        clock_us: Callable[[], int],
    ) -> None:
        self._layout = layout
        self._lock = lock
        self._connection: sqlite3.Connection | None = connection
        self._authority = authority
        self._clock_us = clock_us
        self._mutex = threading.RLock()

    @property
    def authority(self) -> InstallationAuthority:
        return self._authority

    @property
    def database_path(self) -> Path:
        return self._layout.installation_database

    @property
    def installation_root(self) -> Path:
        """The catalogue root whose lifetime lock this store owns."""
        return self._layout.root.resolve()

    @property
    def closed(self) -> bool:
        return self._connection is None

    def __enter__(self) -> Self:
        self._require_connection()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release the connection before the lifetime lock, in that order."""
        with self._mutex:
            connection = self._connection
            self._connection = None
            try:
                if connection is not None:
                    connection.close()
            finally:
                self._lock.release()

    def claim_allocation(
        self,
        authority: InstallationAuthority,
        *,
        principal_id: str,
        operation: str,
        purpose: str,
        idempotency_key: str,
        request_digest: str,
        identity_factory: Callable[[], NewInstallationAllocation],
    ) -> AllocationClaim:
        """Claim a request once; equivalent retries reuse its existing target.

        The identity factory is invoked only after the scope is proven absent while
        holding the catalogue write transaction.  A retry therefore does not even
        mint a discarded second workspace id or path.
        """
        with self._transaction(authority) as connection:
            existing = self._find_scope(
                connection,
                principal_id=principal_id,
                operation=operation,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                existing_digest, allocation = existing
                if existing_digest != request_digest:
                    raise InstallationIdempotencyConflict(
                        "installation idempotency scope is bound to a different "
                        "canonical request"
                    )
                return AllocationClaim(
                    created=False,
                    allocation=allocation,
                    outcome=self._outcome_for_claim(connection, allocation.claim_id),
                )

            minted = identity_factory()
            now_us = self._now_us()
            target_path = str(minted.target_path)
            if not minted.target_path.is_absolute():
                raise InstallationStoreError(
                    "server-derived installation target path must be absolute"
                )
            values = (
                self._authority.installation_id,
                self._authority.fencing_generation,
            )
            try:
                connection.execute(
                    "INSERT INTO omnivia_installation_audit_events "
                    "(audit_ref, installation_id, principal_id, operation, purpose, "
                    "outcome_class, error_code, fencing_generation, recorded_at_us) "
                    "VALUES (?, ?, ?, ?, ?, 'accepted', NULL, ?, ?)",
                    (
                        minted.audit_ref,
                        values[0],
                        principal_id,
                        operation,
                        purpose,
                        values[1],
                        now_us,
                    ),
                )
                connection.execute(
                    "INSERT INTO omnivia_installation_idempotency_claims "
                    "(claim_id, installation_id, principal_id, operation, "
                    "idempotency_key, request_digest, audit_ref, "
                    "fencing_generation, claimed_at_us) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        minted.claim_id,
                        values[0],
                        principal_id,
                        operation,
                        idempotency_key,
                        request_digest,
                        minted.audit_ref,
                        values[1],
                        now_us,
                    ),
                )
                connection.execute(
                    "INSERT INTO omnivia_installation_allocations "
                    "(allocation_id, installation_id, target_workspace_id, "
                    "target_path, principal_id, operation, purpose, claim_id, "
                    "audit_ref, state, state_detail, fencing_generation, "
                    "created_at_us, updated_at_us) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', NULL, ?, ?, ?)",
                    (
                        minted.allocation_id,
                        values[0],
                        minted.target_workspace_id,
                        target_path,
                        principal_id,
                        operation,
                        purpose,
                        minted.claim_id,
                        minted.audit_ref,
                        values[1],
                        now_us,
                        now_us,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise InstallationStoreError(
                    "installation allocation claim violated durable authority"
                ) from error
            persisted = self._allocation_by_id(connection, minted.allocation_id)
            if persisted is None:  # pragma: no cover - same-transaction invariant
                raise InstallationStoreError(
                    "installation allocation was not persisted"
                )
            return AllocationClaim(created=True, allocation=persisted, outcome=None)

    def resume_allocation(
        self, authority: InstallationAuthority, *, allocation_id: str
    ) -> InstallationAllocation:
        """Move one recoverable allocation back to preparing under this owner."""
        with self._transaction(authority) as connection:
            allocation = self._required_allocation(connection, allocation_id)
            if allocation.state is AllocationState.PREPARING:
                return allocation
            if allocation.state is AllocationState.ACTIVE:
                return allocation
            now_us = self._now_us()
            connection.execute(
                "UPDATE omnivia_installation_allocations "
                "SET state = 'preparing', state_detail = NULL, "
                "fencing_generation = ?, updated_at_us = ? WHERE allocation_id = ?",
                (self._authority.fencing_generation, now_us, allocation_id),
            )
            return self._required_allocation(connection, allocation_id)

    def fail_allocation(
        self,
        authority: InstallationAuthority,
        *,
        allocation_id: str,
        detail: str,
        execution_id: str,
        grant_id: str,
        required_role: str,
        settlement_guard: Callable[[], None],
    ) -> InstallationAllocation:
        """Record a recoverable filesystem failure and consume its grant."""
        with self._transaction(authority) as connection:
            allocation = self._required_allocation(connection, allocation_id)
            if allocation.state is AllocationState.ACTIVE:
                raise InstallationStoreError("an active allocation cannot fail")
            settlement_guard()
            now_us = self._now_us()
            connection.execute(
                "UPDATE omnivia_installation_allocations "
                "SET state = 'failed_recoverable', state_detail = ?, "
                "fencing_generation = ?, updated_at_us = ? WHERE allocation_id = ?",
                (
                    detail,
                    self._authority.fencing_generation,
                    now_us,
                    allocation_id,
                ),
            )
            self._insert_grant_use(
                connection,
                allocation=allocation,
                execution_id=execution_id,
                grant_id=grant_id,
                required_role=required_role,
                execution_kind="executed",
                recorded_at_us=now_us,
            )
            settlement_guard()
            return self._required_allocation(connection, allocation_id)

    def settle_allocation_success(
        self,
        authority: InstallationAuthority,
        *,
        allocation_id: str,
        workspace_label: str | None,
        outcome_id: str,
        outcome_json: str,
        outcome_digest: str,
        execution_id: str,
        grant_id: str,
        required_role: str,
        settlement_guard: Callable[[], None],
    ) -> InstallationOutcome:
        """Activate the exact target and atomically store result and grant use."""
        with self._transaction(authority) as connection:
            allocation = self._required_allocation(connection, allocation_id)
            if allocation.state is AllocationState.ACTIVE:
                raise InstallationStoreError("allocation is already active")
            settlement_guard()
            now_us = self._now_us()
            generation = self._authority.fencing_generation
            connection.execute(
                "UPDATE omnivia_installation_allocations "
                "SET state = 'active', state_detail = NULL, fencing_generation = ?, "
                "updated_at_us = ? WHERE allocation_id = ?",
                (generation, now_us, allocation_id),
            )
            active = self._required_allocation(connection, allocation_id)
            try:
                connection.execute(
                    "INSERT INTO omnivia_installation_workspaces "
                    "(workspace_id, installation_id, workspace_path, workspace_label, "
                    "allocation_id, fencing_generation, registered_at_us) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        active.target_workspace_id,
                        self._authority.installation_id,
                        str(active.target_path),
                        workspace_label,
                        active.allocation_id,
                        generation,
                        now_us,
                    ),
                )
                connection.execute(
                    "INSERT INTO omnivia_installation_idempotency_outcomes "
                    "(outcome_id, claim_id, installation_id, outcome_branch, "
                    "error_code, outcome_json, outcome_reference, outcome_digest, "
                    "audit_ref, fencing_generation, settled_at_us) "
                    "VALUES (?, ?, ?, 'success', NULL, ?, NULL, ?, ?, ?, ?)",
                    (
                        outcome_id,
                        active.claim_id,
                        self._authority.installation_id,
                        outcome_json,
                        outcome_digest,
                        active.audit_ref,
                        generation,
                        now_us,
                    ),
                )
                self._insert_grant_use(
                    connection,
                    allocation=active,
                    execution_id=execution_id,
                    grant_id=grant_id,
                    required_role=required_role,
                    execution_kind="executed",
                    recorded_at_us=now_us,
                )
            except sqlite3.IntegrityError as error:
                raise InstallationStoreError(
                    "installation settlement violated durable authority"
                ) from error
            outcome = self._outcome_for_claim(connection, active.claim_id)
            if outcome is None:  # pragma: no cover - same-transaction invariant
                raise InstallationStoreError("installation outcome was not persisted")
            settlement_guard()
            return outcome

    def record_replay_grant(
        self,
        authority: InstallationAuthority,
        *,
        allocation_id: str,
        execution_id: str,
        grant_id: str,
        required_role: str,
        settlement_guard: Callable[[], None],
    ) -> InstallationOutcome:
        """Consume a fresh grant before serving an already settled answer."""
        with self._transaction(authority) as connection:
            allocation = self._required_allocation(connection, allocation_id)
            outcome = self._outcome_for_claim(connection, allocation.claim_id)
            if allocation.state is not AllocationState.ACTIVE or outcome is None:
                raise InstallationStoreError(
                    "only an active allocation with a terminal outcome can replay"
                )
            settlement_guard()
            try:
                self._insert_grant_use(
                    connection,
                    allocation=allocation,
                    execution_id=execution_id,
                    grant_id=grant_id,
                    required_role=required_role,
                    execution_kind="replayed",
                    recorded_at_us=self._now_us(),
                )
            except sqlite3.IntegrityError as error:
                raise InstallationStoreError(
                    "installation replay grant was already used or contradicted authority"
                ) from error
            settlement_guard()
            return outcome

    def get_allocation(self, allocation_id: str) -> InstallationAllocation | None:
        with self._mutex:
            return self._allocation_by_id(self._require_connection(), allocation_id)

    def get_outcome(self, claim_id: str) -> InstallationOutcome | None:
        with self._mutex:
            return self._outcome_for_claim(self._require_connection(), claim_id)

    def list_workspace_ids(self) -> tuple[str, ...]:
        with self._mutex:
            rows = (
                self._require_connection()
                .execute(
                    "SELECT workspace_id FROM omnivia_installation_workspaces "
                    "WHERE installation_id = ? ORDER BY workspace_id",
                    (self._authority.installation_id,),
                )
                .fetchall()
            )
            return tuple(str(row[0]) for row in rows)

    def list_workspace_outcomes(
        self,
    ) -> tuple[tuple[str, InstallationOutcome], ...]:
        """Return the canonical outcomes for this installation's active inventory.

        The catalogue is the enumeration authority.  The join starts at the
        installation workspace inventory and remains constrained by the owned
        installation id, so a caller cannot discover a sibling catalogue by naming
        a path or a workspace identifier.  A registered workspace without its
        atomic allocation/outcome chain is corruption and is refused rather than
        silently omitted from a list that claims to be complete.
        """
        with self._mutex:
            connection = self._require_connection()
            rows = connection.execute(
                "SELECT w.workspace_id, a.claim_id "
                "FROM omnivia_installation_workspaces w "
                "JOIN omnivia_installation_allocations a "
                "ON a.installation_id = w.installation_id "
                "AND a.allocation_id = w.allocation_id "
                "WHERE w.installation_id = ? AND a.state = 'active' "
                "ORDER BY w.workspace_id",
                (self._authority.installation_id,),
            ).fetchall()
            results: list[tuple[str, InstallationOutcome]] = []
            for workspace_id, claim_id in rows:
                outcome = self._outcome_for_claim(connection, str(claim_id))
                if outcome is None or outcome.outcome_branch != "success":
                    raise InstallationStoreError(
                        "registered installation workspace has no successful outcome"
                    )
                results.append((str(workspace_id), outcome))
            return tuple(results)

    # --- dedicated installed-MCP authority (Gate B) ---------------------------

    def configure_mcp_setup(
        self,
        authority: InstallationAuthority,
        *,
        host: McpHost,
        workspace_id: str,
        profile: McpProfile,
        authoring_intent: bool,
        grants: Sequence[McpGrant],
        identity_factory: Callable[[], NewMcpSetup],
    ) -> McpSetupOutcome:
        """Provision, or re-provision, the one MCP setup for `host`.

        The requested state is the whole of what a caller may ask for: a host, a
        workspace this installation already authorised, a profile, an explicit
        authoring intent and the exact rights that profile implies. Every identity
        and every piece of credential material is minted by the factory *inside*
        this transaction, after the store has decided a write is needed, so a
        caller cannot choose a principal, a reference or a verifier and an
        idempotent reconfigure never generates a secret it would throw away.

        Requesting exactly the live state is answered from it, unrotated -- a
        repeated `configure` is not a reason to invalidate a working credential.
        Anything else is a rotation: a new generation, a new principal, a new
        reference and a new verifier, in one statement with the old one, so there
        is no instant at which both the previous and the next credential work.

        The workspace is proved to be in this installation's authorised inventory
        before anything is written. The composite foreign key proves it a second
        time, but an unknown workspace is a thing the caller got wrong and deserves
        to be told so rather than an integrity error from underneath.
        """
        requested = _checked_grants(grants)
        if (profile is McpProfile.AUTHORING) != bool(authoring_intent):
            raise InstallationStoreError(
                "the authoring profile requires recorded authoring intent, and "
                "authoring intent requires the authoring profile"
            )
        with self._transaction(authority) as connection:
            self._require_installation_workspace(connection, workspace_id)
            existing = self._mcp_setup_for_host(connection, host)
            if (
                existing is not None
                and existing.status is McpSetupStatus.ACTIVE
                and existing.workspace_id == workspace_id
                and existing.profile is profile
                and existing.authoring_intent == bool(authoring_intent)
                and self._mcp_grants(
                    connection, existing.setup_id, existing.setup_generation
                )
                == requested
            ):
                return McpSetupOutcome(rotated=False, setup=existing)

            minted = identity_factory()
            now_us = self._now_us()
            generation = 1 if existing is None else existing.setup_generation + 1
            setup_id = minted.setup_id if existing is None else existing.setup_id
            try:
                self._insert_mcp_audit_event(
                    connection,
                    audit_ref=minted.audit_ref,
                    principal_id=minted.principal_id,
                    operation=_MCP_CONFIGURE_OPERATION,
                    now_us=now_us,
                )
                if existing is None:
                    connection.execute(
                        "INSERT INTO omnivia_installation_mcp_setups "
                        "(setup_id, installation_id, host, workspace_id, "
                        "principal_id, profile, authoring_intent, "
                        "credential_reference, credential_salt, credential_digest, "
                        "status, setup_generation, fencing_generation, "
                        "created_at_us, updated_at_us, revoked_at_us) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?, "
                        "NULL)",
                        (
                            setup_id,
                            self._authority.installation_id,
                            host.value,
                            workspace_id,
                            minted.principal_id,
                            profile.value,
                            int(bool(authoring_intent)),
                            minted.credential_reference,
                            minted.credential_salt,
                            minted.credential_digest,
                            self._authority.fencing_generation,
                            now_us,
                            now_us,
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE omnivia_installation_mcp_setups "
                        "SET workspace_id = ?, principal_id = ?, profile = ?, "
                        "authoring_intent = ?, credential_reference = ?, "
                        "credential_salt = ?, credential_digest = ?, "
                        "status = 'active', setup_generation = ?, "
                        "fencing_generation = ?, updated_at_us = ?, "
                        "revoked_at_us = NULL "
                        "WHERE installation_id = ? AND setup_id = ?",
                        (
                            workspace_id,
                            minted.principal_id,
                            profile.value,
                            int(bool(authoring_intent)),
                            minted.credential_reference,
                            minted.credential_salt,
                            minted.credential_digest,
                            generation,
                            self._authority.fencing_generation,
                            now_us,
                            self._authority.installation_id,
                            setup_id,
                        ),
                    )
                for index, grant in enumerate(requested):
                    connection.execute(
                        "INSERT INTO omnivia_installation_mcp_grants "
                        "(grant_row_id, installation_id, setup_id, setup_generation, "
                        "grant_kind, grant_value, grant_version, fencing_generation, "
                        "granted_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            f"{setup_id}-{generation}-{index}",
                            self._authority.installation_id,
                            setup_id,
                            generation,
                            grant.kind.value,
                            grant.value,
                            grant.version,
                            self._authority.fencing_generation,
                            now_us,
                        ),
                    )
            except sqlite3.IntegrityError as error:
                raise InstallationStoreError(
                    "installed MCP configuration violated durable authority"
                ) from error
            settled = self._mcp_setup_for_host(connection, host)
            if settled is None:  # pragma: no cover - same-transaction invariant
                raise InstallationStoreError("installed MCP setup was not persisted")
            return McpSetupOutcome(rotated=True, setup=settled)

    def revoke_mcp_setup(
        self,
        authority: InstallationAuthority,
        *,
        host: McpHost,
        audit_ref: str,
        credential_salt: str,
        credential_digest: str,
    ) -> InstalledMcpSetup | None:
        """Revoke `host`'s authority, idempotently. `None` if it was never configured.

        The verifier is overwritten with material that answers to nothing, rather
        than merely being marked unusable. `status` alone would already refuse
        every resolution, and this is the second lock: a future reader that forgot
        the status check still cannot authenticate the revoked bearer.

        The rights go with it. A revocation advances the generation, and a policy
        read is scoped to the setup's current generation, so the previous
        generation's grant rows stop being anybody's authority in the same
        statement -- while staying on disk as evidence of what was granted.

        Revoking an already revoked host changes nothing and returns the durable
        row, which is what makes a repeated `omnivia mcp revoke` free rather than a
        second revocation with a second timestamp.
        """
        with self._transaction(authority) as connection:
            existing = self._mcp_setup_for_host(connection, host)
            if existing is None or existing.status is McpSetupStatus.REVOKED:
                return existing
            now_us = self._now_us()
            try:
                self._insert_mcp_audit_event(
                    connection,
                    audit_ref=audit_ref,
                    principal_id=existing.principal_id,
                    operation=_MCP_REVOKE_OPERATION,
                    now_us=now_us,
                )
                connection.execute(
                    "UPDATE omnivia_installation_mcp_setups "
                    "SET status = 'revoked', credential_salt = ?, "
                    "credential_digest = ?, setup_generation = ?, "
                    "fencing_generation = ?, updated_at_us = ?, revoked_at_us = ? "
                    "WHERE installation_id = ? AND setup_id = ?",
                    (
                        credential_salt,
                        credential_digest,
                        existing.setup_generation + 1,
                        self._authority.fencing_generation,
                        now_us,
                        now_us,
                        self._authority.installation_id,
                        existing.setup_id,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise InstallationStoreError(
                    "installed MCP revocation violated durable authority"
                ) from error
            settled = self._mcp_setup_for_host(connection, host)
            if settled is None:  # pragma: no cover - same-transaction invariant
                raise InstallationStoreError("installed MCP setup was not persisted")
            return settled

    def mcp_setup(self, host: McpHost) -> InstalledMcpSetup | None:
        """The durable setup for one host, redacted, or `None`."""
        with self._mutex:
            return self._mcp_setup_for_host(self._require_connection(), host)

    def mcp_setups(self) -> tuple[InstalledMcpSetup, ...]:
        """Every configured host's setup, redacted, in host order."""
        with self._mutex:
            connection = self._require_connection()
            rows = connection.execute(
                f"SELECT {_MCP_SETUP_COLUMNS} FROM omnivia_installation_mcp_setups "
                "WHERE installation_id = ? ORDER BY host",
                (self._authority.installation_id,),
            ).fetchall()
            return tuple(_mcp_setup_from_row(row) for row in rows)

    def mcp_grants(self, setup_id: str, setup_generation: int) -> tuple[McpGrant, ...]:
        """The exact rights one setup generation holds, in canonical order."""
        with self._mutex:
            return self._mcp_grants(
                self._require_connection(), setup_id, setup_generation
            )

    def resolve_mcp_credential(self, secret: str) -> ResolvedMcpSetup | None:
        """The active setup one bearer secret authenticates, read fresh, or `None`.

        Every call re-reads durable state, and nothing here is cached: a rotation
        or a revocation therefore takes effect on the next call and on the next
        replay, which is the property a cached session would destroy.

        Only active setups are candidates, and each is compared with
        :func:`hmac.compare_digest` against its own salted verifier. Every
        candidate is compared even once one has matched, so the work this does is
        the same whichever host was configured first.

        `None` rather than a refusal: which of "no setup", "wrong secret" and
        "revoked" happened is not something this can tell a caller apart from the
        others without saying more than a failed authentication may say.
        """
        if not isinstance(secret, str) or not secret:
            return None
        with self._mutex:
            connection = self._require_connection()
            rows = connection.execute(
                f"SELECT {_MCP_SETUP_COLUMNS}, credential_salt, credential_digest "
                "FROM omnivia_installation_mcp_setups "
                "WHERE installation_id = ? AND status = 'active' ORDER BY host",
                (self._authority.installation_id,),
            ).fetchall()
            matched: InstalledMcpSetup | None = None
            for row in rows:
                digest = mcp_credential_digest(str(row[-2]), secret)
                if hmac.compare_digest(digest, str(row[-1])) and matched is None:
                    matched = _mcp_setup_from_row(row)
            if matched is None:
                return None
            grants = self._mcp_grants(
                connection, matched.setup_id, matched.setup_generation
            )
            if not grants:
                raise InstallationStoreError(
                    "an active installed MCP setup holds no durable rights"
                )
            return ResolvedMcpSetup(setup=matched, grants=grants)

    def _require_installation_workspace(
        self, connection: sqlite3.Connection, workspace_id: str
    ) -> None:
        row = connection.execute(
            "SELECT 1 FROM omnivia_installation_workspaces "
            "WHERE installation_id = ? AND workspace_id = ?",
            (self._authority.installation_id, workspace_id),
        ).fetchone()
        if row is None:
            raise InstallationStoreError(
                "the named workspace is not in this installation's authorised inventory"
            )

    def _mcp_setup_for_host(
        self, connection: sqlite3.Connection, host: McpHost
    ) -> InstalledMcpSetup | None:
        row = connection.execute(
            f"SELECT {_MCP_SETUP_COLUMNS} FROM omnivia_installation_mcp_setups "
            "WHERE installation_id = ? AND host = ?",
            (self._authority.installation_id, host.value),
        ).fetchone()
        return None if row is None else _mcp_setup_from_row(row)

    def _mcp_grants(
        self, connection: sqlite3.Connection, setup_id: str, setup_generation: int
    ) -> tuple[McpGrant, ...]:
        rows = connection.execute(
            "SELECT grant_kind, grant_value, grant_version "
            "FROM omnivia_installation_mcp_grants "
            "WHERE installation_id = ? AND setup_id = ? AND setup_generation = ? "
            "ORDER BY grant_kind, grant_value",
            (self._authority.installation_id, setup_id, setup_generation),
        ).fetchall()
        return tuple(
            McpGrant(
                kind=McpGrantKind(str(row[0])),
                value=str(row[1]),
                version=None if row[2] is None else str(row[2]),
            )
            for row in rows
        )

    def _insert_mcp_audit_event(
        self,
        connection: sqlite3.Connection,
        *,
        audit_ref: str,
        principal_id: str,
        operation: str,
        now_us: int,
    ) -> None:
        """Append the lifecycle evidence for one administration decision.

        The installation's own append-only audit table, reused rather than
        duplicated: it already records "which principal, which operation, which
        purpose, decided how, under which generation" and already refuses UPDATE
        and DELETE outright. `principal_id` is the dedicated MCP principal the
        decision was about, which is the subject a later reader needs; no
        credential, reference, salt, digest or content has a column here.
        """
        connection.execute(
            "INSERT INTO omnivia_installation_audit_events "
            "(audit_ref, installation_id, principal_id, operation, purpose, "
            "outcome_class, error_code, fencing_generation, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, 'succeeded', NULL, ?, ?)",
            (
                audit_ref,
                self._authority.installation_id,
                principal_id,
                operation,
                _MCP_ADMINISTRATION_PURPOSE,
                self._authority.fencing_generation,
                now_us,
            ),
        )

    @contextmanager
    def _transaction(
        self, authority: InstallationAuthority
    ) -> Iterator[sqlite3.Connection]:
        with self._mutex:
            connection = self._require_connection()
            begun = False
            try:
                connection.execute("BEGIN IMMEDIATE")
                begun = True
                self._assert_authority(connection, authority)
                yield connection
                self._assert_authority(connection, authority)
                connection.execute("COMMIT")
            except BaseException:
                if begun and connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _assert_authority(
        self,
        connection: sqlite3.Connection,
        authority: InstallationAuthority,
    ) -> None:
        if authority != self._authority or not self._lock.held:
            raise InstallationAuthorityError(
                "installation authority is absent, stale or belongs to another owner"
            )
        row = connection.execute(
            "SELECT installation_id, owner_instance_id, fencing_generation "
            "FROM omnivia_installation_state WHERE singleton = 1"
        ).fetchone()
        actual = None if row is None else (str(row[0]), str(row[1]), int(row[2]))
        expected = (
            authority.installation_id,
            authority.owner_instance_id,
            authority.fencing_generation,
        )
        if actual != expected:
            raise InstallationAuthorityError(
                "installation owner tuple no longer matches durable fencing state"
            )

    def _find_scope(
        self,
        connection: sqlite3.Connection,
        *,
        principal_id: str,
        operation: str,
        idempotency_key: str,
    ) -> tuple[str, InstallationAllocation] | None:
        row = connection.execute(
            "SELECT c.request_digest, a.allocation_id "
            "FROM omnivia_installation_idempotency_claims c "
            "JOIN omnivia_installation_allocations a ON a.claim_id = c.claim_id "
            "AND a.installation_id = c.installation_id "
            "WHERE c.installation_id = ? AND c.principal_id = ? "
            "AND c.operation = ? AND c.idempotency_key = ?",
            (
                self._authority.installation_id,
                principal_id,
                operation,
                idempotency_key,
            ),
        ).fetchone()
        if row is None:
            return None
        allocation = self._allocation_by_id(connection, str(row[1]))
        if allocation is None:
            raise InstallationStoreError(
                "installation claim exists without its atomic allocation"
            )
        return str(row[0]), allocation

    def _required_allocation(
        self, connection: sqlite3.Connection, allocation_id: str
    ) -> InstallationAllocation:
        allocation = self._allocation_by_id(connection, allocation_id)
        if allocation is None:
            raise InstallationStoreError(
                f"unknown installation allocation {allocation_id}"
            )
        return allocation

    def _allocation_by_id(
        self, connection: sqlite3.Connection, allocation_id: str
    ) -> InstallationAllocation | None:
        row = connection.execute(
            "SELECT allocation_id, target_workspace_id, target_path, principal_id, "
            "operation, purpose, claim_id, audit_ref, state, state_detail, "
            "fencing_generation FROM omnivia_installation_allocations "
            "WHERE installation_id = ? AND allocation_id = ?",
            (self._authority.installation_id, allocation_id),
        ).fetchone()
        if row is None:
            return None
        return InstallationAllocation(
            allocation_id=str(row[0]),
            target_workspace_id=str(row[1]),
            target_path=Path(str(row[2])),
            principal_id=str(row[3]),
            operation=str(row[4]),
            purpose=str(row[5]),
            claim_id=str(row[6]),
            audit_ref=str(row[7]),
            state=AllocationState(str(row[8])),
            state_detail=None if row[9] is None else str(row[9]),
            fencing_generation=int(row[10]),
        )

    def _outcome_for_claim(
        self, connection: sqlite3.Connection, claim_id: str
    ) -> InstallationOutcome | None:
        row = connection.execute(
            "SELECT outcome_id, claim_id, outcome_branch, error_code, outcome_json, "
            "outcome_reference, outcome_digest, audit_ref "
            "FROM omnivia_installation_idempotency_outcomes "
            "WHERE installation_id = ? AND claim_id = ?",
            (self._authority.installation_id, claim_id),
        ).fetchone()
        if row is None:
            return None
        return InstallationOutcome(
            outcome_id=str(row[0]),
            claim_id=str(row[1]),
            outcome_branch=str(row[2]),
            error_code=None if row[3] is None else str(row[3]),
            outcome_json=None if row[4] is None else str(row[4]),
            outcome_reference=None if row[5] is None else str(row[5]),
            outcome_digest=str(row[6]),
            audit_ref=str(row[7]),
        )

    def _insert_grant_use(
        self,
        connection: sqlite3.Connection,
        *,
        allocation: InstallationAllocation,
        execution_id: str,
        grant_id: str,
        required_role: str,
        execution_kind: str,
        recorded_at_us: int,
    ) -> None:
        connection.execute(
            "INSERT INTO omnivia_installation_grant_uses "
            "(execution_id, installation_id, allocation_id, target_workspace_id, "
            "principal_id, operation, purpose, grant_id, required_role, "
            "execution_kind, claim_id, audit_ref, fencing_generation, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                execution_id,
                self._authority.installation_id,
                allocation.allocation_id,
                allocation.target_workspace_id,
                allocation.principal_id,
                allocation.operation,
                allocation.purpose,
                grant_id,
                required_role,
                execution_kind,
                allocation.claim_id,
                allocation.audit_ref,
                self._authority.fencing_generation,
                recorded_at_us,
            ),
        )

    def _now_us(self) -> int:
        value = self._clock_us()
        if value <= 0:
            raise InstallationStoreError(
                "installation clock returned a non-positive time"
            )
        return value

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise InstallationStoreError("installation store is closed")
        return self._connection


def open_installation_store(
    installation_root: Path,
    *,
    owner_instance_id: str,
    clock_us: Callable[[], int] = _wall_clock_us,
    installation_id_factory: Callable[[], str] = _installation_id,
) -> InstallationStore:
    """Acquire installation ownership and return its sole write-capable store."""
    if not 1 <= len(owner_instance_id) <= 128:
        raise InstallationStoreError(
            "owner instance id must contain 1 to 128 characters"
        )
    layout = InstallationLayout(root=installation_root)
    lock = create_lock(
        layout.installation_lock,
        LockRole.LIFETIME_STORAGE,
        {"holder": owner_instance_id, "scope": "installation"},
    )
    try:
        held = lock.acquire()
    except OSError as error:
        raise InstallationStoreError(
            "installation lifetime lock could not be opened"
        ) from error
    if not held:
        raise InstallationBusy("another process owns the installation catalogue")

    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_catalogue(layout.installation_database)
        now_us = clock_us()
        if now_us <= 0:
            raise InstallationStoreError(
                "installation clock returned a non-positive time"
            )

        if not installation_schema_present(connection):
            if fingerprint_schema(connection).tables:
                raise InstallationStoreError(
                    "installation catalogue contains an unrecognised schema"
                )
            installation_id = installation_id_factory()
            if not 1 <= len(installation_id) <= 128:
                raise InstallationStoreError(
                    "minted installation id must contain 1 to 128 characters"
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                apply_initial_installation_schema(
                    connection,
                    installation_id=installation_id,
                    owner_instance_id=owner_instance_id,
                    now_us=now_us,
                )
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            generation = 1
        else:
            # An existing catalogue is read for its identity *before* it is verified
            # against the pinned head, because it is legitimately allowed to be
            # behind that head: this build must be able to tell a catalogue that
            # needs migrating from one that has drifted. Everything that would have
            # been checked here is still checked -- the ledger, `user_version` and
            # the schema fingerprint are held to the prefix the ledger claims before
            # a single statement is applied, and to the head immediately after.
            row = connection.execute(
                "SELECT installation_id, installation_format_version, "
                "fencing_generation FROM omnivia_installation_state WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise InstallationStoreError("installation identity row is absent")
            installation_id = str(row[0])
            if str(row[1]) != INSTALLATION_FORMAT_VERSION:
                raise InstallationStoreError(
                    "installation format version is not supported by this Core build"
                )
            generation = int(row[2]) + 1
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE omnivia_installation_state SET fencing_generation = ?, "
                    "owner_instance_id = ?, owner_acquired_at_us = ?, updated_at_us = ? "
                    "WHERE singleton = 1",
                    (generation, owner_instance_id, now_us, now_us),
                )
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            # After the generation advance, never before: every ledger row this
            # applies must name the owner that is current now, which is the rule the
            # schema's own INSERT trigger enforces from inside the transaction.
            apply_pending_installation_migrations(
                connection,
                installation_id=installation_id,
                owner_instance_id=owner_instance_id,
                fencing_generation=generation,
                now_us=now_us,
            )

        verify_installation_schema(connection)
        authority = InstallationAuthority(
            installation_id=installation_id,
            owner_instance_id=owner_instance_id,
            fencing_generation=generation,
        )
        durable = connection.execute(
            "SELECT installation_id, owner_instance_id, fencing_generation "
            "FROM omnivia_installation_state WHERE singleton = 1"
        ).fetchone()
        if durable != (
            authority.installation_id,
            authority.owner_instance_id,
            authority.fencing_generation,
        ):
            raise InstallationAuthorityError(
                "installation acquisition did not settle its exact owner tuple"
            )
        store = InstallationStore(
            layout=layout,
            lock=lock,
            connection=connection,
            authority=authority,
            clock_us=clock_us,
        )
        connection = None
        return store
    except BaseException:
        if connection is not None:
            connection.close()
        lock.release()
        raise


__all__ = [
    "AllocationClaim",
    "AllocationState",
    "InstallationAllocation",
    "InstallationAuthority",
    "InstallationAuthorityError",
    "InstallationBusy",
    "InstallationIdempotencyConflict",
    "InstallationOutcome",
    "InstallationStore",
    "InstallationStoreError",
    "InstalledMcpSetup",
    "McpGrant",
    "McpGrantKind",
    "McpHost",
    "McpProfile",
    "McpSetupOutcome",
    "McpSetupStatus",
    "NewInstallationAllocation",
    "NewMcpSetup",
    "ResolvedMcpSetup",
    "mcp_credential_digest",
    "open_installation_store",
]
