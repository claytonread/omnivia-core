"""Exclusive, fenced persistence for installation-scoped authority.

One :class:`InstallationStore` owns one lifetime file lock and one SQLite
connection.  The lock excludes another installation service; the persisted
generation makes a predecessor permanently stale even if it resumes with an old
Python object.  No writable connection escapes this module.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Self

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
            verify_installation_schema(connection)
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
    "NewInstallationAllocation",
    "open_installation_store",
]
