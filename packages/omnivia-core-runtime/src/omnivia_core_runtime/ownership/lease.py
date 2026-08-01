"""Workspace service lease, fencing generation and takeover (T-0629E, ADR-037).

Only Core Service holds the writable workspace lease. Every acquisition or
takeover atomically increments a monotonically increasing fencing generation, and
that generation is recorded in authoritative workspace state rather than held in
memory.

The rule this module exists to enforce is ADR-037 invariant 8: **lease expiry alone
does not prove the old writer is dead.** A paused, sleeping or debugger-suspended
process resumes believing it still owns the workspace. So an expired heartbeat is
only a signal that permits *investigation*; taking over additionally requires
positive evidence — the storage lock must be available, and the recorded process
must be demonstrably gone or demonstrably a different process.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum

from omnivia_core_runtime.ownership.identity import (
    Clock,
    ProcessEvidence,
    ProcessEvidenceSource,
    ServiceInstanceIdentity,
)
from omnivia_core_runtime.storage.connection import (
    StorageError,
    authorised,
    immediate_transaction,
)
from omnivia_core_runtime.storage.migrations import substrate_present

#: Heartbeat older than this is *suspicious*, never conclusive.
DEFAULT_LEASE_TTL_SECONDS = 30.0


class LeaseLifecycle(str, Enum):
    """Lifecycle of the lease record itself."""

    ACQUIRING = "acquiring"
    HELD = "held"
    DRAINING = "draining"
    RELEASED = "released"


class LeaseError(StorageError):
    """A lease could not be acquired, renewed or released."""


class LeaseHeld(LeaseError):
    """Another live service holds the lease and takeover is not permitted."""


@dataclass(frozen=True)
class LeaseRecord:
    """The current workspace service lease.

    Carries every field ADR-037 requires: workspace, service, installation and
    process identity; OS principal; fencing generation; lock mechanism; heartbeat;
    endpoint; lifecycle; takeover predecessor; and shutdown state.
    """

    workspace_id: str
    service_instance_id: str
    installation_id: str
    fencing_generation: int
    os_principal: str | None
    process_pid: int | None
    process_start: str | None
    boot_id: str | None
    endpoint: str | None
    lock_mechanism: str | None
    heartbeat_monotonic: float | None
    heartbeat_at: str | None
    acquired_at: str
    lifecycle: str
    takeover_predecessor: str | None
    shutdown_state: str | None

    @property
    def process(self) -> ProcessEvidence | None:
        if self.process_pid is None or self.process_start is None:
            return None
        return ProcessEvidence(
            pid=self.process_pid,
            start_time=self.process_start,
            boot_id=self.boot_id or "",
            os_principal=self.os_principal or "",
        )

    def is_expired(self, clock: Clock, ttl: float = DEFAULT_LEASE_TTL_SECONDS) -> bool:
        """Whether the heartbeat is stale.

        Deliberately named as a question about the *heartbeat*, not about the owner.
        A true result is a signal to investigate, never a licence to take over.
        """
        if self.heartbeat_monotonic is None:
            return True
        return (clock.monotonic() - self.heartbeat_monotonic) > ttl


class TakeoverRefusal(str, Enum):
    """Why a takeover was refused."""

    LEASE_IS_FRESH = "lease_is_fresh"
    OWNER_STILL_ALIVE = "owner_still_alive"
    STORAGE_LOCK_UNAVAILABLE = "storage_lock_unavailable"
    NO_LEASE_TO_TAKE = "no_lease_to_take"


@dataclass(frozen=True)
class TakeoverDecision:
    """Whether a takeover may proceed, and the evidence behind it."""

    permitted: bool
    refusal: TakeoverRefusal | None
    reason: str
    heartbeat_expired: bool
    storage_lock_available: bool
    owner_process_gone: bool


def read_lease(connection: sqlite3.Connection) -> LeaseRecord | None:
    """Read the single lease row, or None when no lease (or no substrate) exists."""
    if not substrate_present(connection):
        return None
    row = connection.execute(
        "SELECT workspace_id, service_instance_id, installation_id, fencing_generation, "
        "os_principal, process_pid, process_start, boot_id, endpoint, lock_mechanism, "
        "heartbeat_monotonic, heartbeat_at, acquired_at, lifecycle, "
        "takeover_predecessor, shutdown_state "
        "FROM omnivia_workspace_lease WHERE singleton = 1"
    ).fetchone()
    if row is None:
        return None
    return LeaseRecord(
        workspace_id=str(row[0]),
        service_instance_id=str(row[1]),
        installation_id=str(row[2]),
        fencing_generation=int(row[3]),
        os_principal=None if row[4] is None else str(row[4]),
        process_pid=None if row[5] is None else int(row[5]),
        process_start=None if row[6] is None else str(row[6]),
        boot_id=None if row[7] is None else str(row[7]),
        endpoint=None if row[8] is None else str(row[8]),
        lock_mechanism=None if row[9] is None else str(row[9]),
        heartbeat_monotonic=None if row[10] is None else float(row[10]),
        heartbeat_at=None if row[11] is None else str(row[11]),
        acquired_at=str(row[12]),
        lifecycle=str(row[13]),
        takeover_predecessor=None if row[14] is None else str(row[14]),
        shutdown_state=None if row[15] is None else str(row[15]),
    )


def evaluate_takeover(
    lease: LeaseRecord | None,
    *,
    clock: Clock,
    evidence: ProcessEvidenceSource,
    storage_lock_available: bool,
    ttl: float = DEFAULT_LEASE_TTL_SECONDS,
) -> TakeoverDecision:
    """Decide whether the recorded owner may be superseded.

    Three independent facts are required, and an expired heartbeat is only one of
    them. The storage lock must be available — a live owner still holds it, and
    flock releases on process death, so its availability is real evidence rather
    than a guess. The recorded process must also be gone or be a different process,
    which is why the record carries a start time and boot id and not just a PID.
    """
    if lease is None or lease.lifecycle == LeaseLifecycle.RELEASED.value:
        return TakeoverDecision(
            permitted=True,
            refusal=None,
            reason="no live lease exists",
            heartbeat_expired=True,
            storage_lock_available=storage_lock_available,
            owner_process_gone=True,
        )

    heartbeat_expired = lease.is_expired(clock, ttl)
    recorded = lease.process
    current = None if recorded is None else evidence.for_pid(recorded.pid)
    owner_process_gone = recorded is None or current is None or not recorded.is_same_process(current)

    if not heartbeat_expired:
        return TakeoverDecision(
            permitted=False,
            refusal=TakeoverRefusal.LEASE_IS_FRESH,
            reason="the lease heartbeat is current; the owner is alive",
            heartbeat_expired=False,
            storage_lock_available=storage_lock_available,
            owner_process_gone=owner_process_gone,
        )

    if not storage_lock_available:
        # The decisive case. A suspended process still holds its flock, so an
        # expired heartbeat plus a held lock means "paused", not "dead".
        return TakeoverDecision(
            permitted=False,
            refusal=TakeoverRefusal.STORAGE_LOCK_UNAVAILABLE,
            reason=(
                "the heartbeat expired but the owner still holds the storage lock; "
                "expiry alone does not prove the writer is dead"
            ),
            heartbeat_expired=True,
            storage_lock_available=False,
            owner_process_gone=owner_process_gone,
        )

    if not owner_process_gone:
        return TakeoverDecision(
            permitted=False,
            refusal=TakeoverRefusal.OWNER_STILL_ALIVE,
            reason="the recorded owner process is still the same live process",
            heartbeat_expired=True,
            storage_lock_available=True,
            owner_process_gone=False,
        )

    return TakeoverDecision(
        permitted=True,
        refusal=None,
        reason=(
            "heartbeat expired, storage lock available, and the recorded owner "
            "process is gone or replaced"
        ),
        heartbeat_expired=True,
        storage_lock_available=True,
        owner_process_gone=True,
    )


def acquire_lease(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    clock: Clock,
    workspace_id: str,
    holds_storage_lock: bool,
    lock_mechanism: str,
    endpoint: str | None = None,
    predecessor: str | None = None,
) -> LeaseRecord:
    """Atomically increment the fencing generation and take the lease.

    ADR-037 invariant 17: the lifetime storage lock and the sole exclusive SQLite
    connection are established *before* a new generation is committed. That is
    enforced here rather than documented, because a generation committed without the
    lock would let two services believe they were current.

    The increment reads and writes workspace state inside one `BEGIN IMMEDIATE`, so
    two racing acquisitions cannot both observe the same prior generation.
    """
    if not holds_storage_lock:
        raise LeaseError(
            "refusing to commit a fencing generation without the lifetime storage lock"
        )
    if not substrate_present(connection):
        # Checked before the transaction so a missing substrate surfaces as a typed
        # LeaseError rather than a raw "no such table" from inside the SELECT.
        raise LeaseError("cannot acquire a lease before the ownership substrate exists")

    process = identity.process
    # Widened for this transaction only, after the ownership check above. The
    # substrate is not writable outside the functions that check authority first --
    # a directly writable guard or lease row is authority without a check.
    with immediate_transaction(connection), authorised(connection, mutations=True):
        state = connection.execute(
            "SELECT workspace_id, fencing_generation FROM omnivia_workspace_state "
            "WHERE singleton = 1"
        ).fetchone()
        if state is None:
            raise LeaseError("cannot acquire a lease before the ownership substrate exists")
        if str(state[0]) != workspace_id:
            raise LeaseError(
                f"workspace mismatch: database holds {state[0]!r}, caller claims "
                f"{workspace_id!r}"
            )

        generation = int(state[1]) + 1
        moment = clock.wall_time().isoformat()

        connection.execute(
            "UPDATE omnivia_workspace_state SET fencing_generation = ?, updated_at = ? "
            "WHERE singleton = 1",
            (generation, moment),
        )
        connection.execute("DELETE FROM omnivia_workspace_lease WHERE singleton = 1")
        connection.execute(
            "INSERT INTO omnivia_workspace_lease (singleton, workspace_id, "
            "service_instance_id, installation_id, fencing_generation, os_principal, "
            "process_pid, process_start, boot_id, endpoint, lock_mechanism, "
            "heartbeat_monotonic, heartbeat_at, acquired_at, lifecycle, "
            "takeover_predecessor, shutdown_state) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                workspace_id,
                identity.service_instance_id,
                identity.installation_id,
                generation,
                process.os_principal,
                process.pid,
                process.start_time,
                process.boot_id,
                endpoint,
                lock_mechanism,
                clock.monotonic(),
                moment,
                moment,
                LeaseLifecycle.HELD.value,
                predecessor,
            ),
        )

    lease = read_lease(connection)
    if lease is None:  # pragma: no cover - the transaction committed
        raise LeaseError("lease acquisition committed but the record is unreadable")
    return lease


def heartbeat(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    clock: Clock,
) -> LeaseRecord:
    """Renew the lease heartbeat, refusing if this instance no longer owns it.

    Monotonic time only. A wall-clock adjustment must never make a live lease look
    expired, and must never extend a dead one.
    """
    # Widened for this transaction only, after the ownership check above. The
    # substrate is not writable outside the functions that check authority first --
    # a directly writable guard or lease row is authority without a check.
    with immediate_transaction(connection), authorised(connection, mutations=True):
        current = read_lease(connection)
        if current is None:
            raise LeaseError("no lease to renew")
        if current.service_instance_id != identity.service_instance_id:
            raise LeaseHeld(
                f"lease is held by {current.service_instance_id!r}, not "
                f"{identity.service_instance_id!r}"
            )
        previous = current.heartbeat_monotonic
        now = clock.monotonic()
        if previous is not None and now < previous:
            raise LeaseError("monotonic clock went backwards; refusing to renew")
        connection.execute(
            "UPDATE omnivia_workspace_lease SET heartbeat_monotonic = ?, heartbeat_at = ? "
            "WHERE singleton = 1",
            (now, clock.wall_time().isoformat()),
        )

    renewed = read_lease(connection)
    if renewed is None:  # pragma: no cover
        raise LeaseError("heartbeat committed but the record is unreadable")
    return renewed


def release_lease(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    clock: Clock,
    shutdown_state: str = "clean",
) -> LeaseRecord:
    """Graceful handover: mark the lease released and record how it ended.

    The generation is *not* decremented. A successor still increments from the
    recorded value, so generations remain monotonic across a clean handover and a
    resumed predecessor cannot find its own generation current again.
    """
    # Widened for this transaction only, after the ownership check above. The
    # substrate is not writable outside the functions that check authority first --
    # a directly writable guard or lease row is authority without a check.
    with immediate_transaction(connection), authorised(connection, mutations=True):
        current = read_lease(connection)
        if current is None:
            raise LeaseError("no lease to release")
        if current.service_instance_id != identity.service_instance_id:
            raise LeaseHeld(
                f"lease is held by {current.service_instance_id!r}; refusing to release "
                "another instance's lease"
            )
        connection.execute(
            "UPDATE omnivia_workspace_lease SET lifecycle = ?, shutdown_state = ?, "
            "heartbeat_at = ? WHERE singleton = 1",
            (LeaseLifecycle.RELEASED.value, shutdown_state, clock.wall_time().isoformat()),
        )

    released = read_lease(connection)
    if released is None:  # pragma: no cover
        raise LeaseError("release committed but the record is unreadable")
    return released


def assert_current_authority(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    workspace_id: str,
    fencing_generation: int,
) -> None:
    """Validate the full authority tuple against authoritative state.

    Checked against both the workspace-state generation and the lease row, because
    a stale instance can hold a plausible generation number while the lease has
    already moved to a successor.
    """
    state = connection.execute(
        "SELECT workspace_id, fencing_generation FROM omnivia_workspace_state "
        "WHERE singleton = 1"
    ).fetchone()
    if state is None:
        raise LeaseError("no workspace state; authority cannot be established")
    if str(state[0]) != workspace_id or int(state[1]) != fencing_generation:
        raise LeaseError(
            f"stale authority: state holds ({state[0]!r}, {state[1]}), caller holds "
            f"({workspace_id!r}, {fencing_generation})"
        )

    lease = read_lease(connection)
    if lease is None:
        raise LeaseError("no lease; authority cannot be established")
    if lease.service_instance_id != identity.service_instance_id:
        raise LeaseError(
            f"lease belongs to {lease.service_instance_id!r}, not "
            f"{identity.service_instance_id!r}"
        )
    if lease.fencing_generation != fencing_generation:
        raise LeaseError(
            f"lease generation is {lease.fencing_generation}, caller holds "
            f"{fencing_generation}"
        )


__all__ = [
    "DEFAULT_LEASE_TTL_SECONDS",
    "LeaseError",
    "LeaseHeld",
    "LeaseLifecycle",
    "LeaseRecord",
    "TakeoverDecision",
    "TakeoverRefusal",
    "acquire_lease",
    "assert_current_authority",
    "evaluate_takeover",
    "heartbeat",
    "read_lease",
    "release_lease",
]
