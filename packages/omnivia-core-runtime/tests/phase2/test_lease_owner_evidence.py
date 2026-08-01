"""T-0629E acceptance: lease, takeover and discovery (LE-01 … LE-22, BD-07 … BD-11)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from omnivia_core_runtime.ownership.discovery import (
    ReadinessState,
    ServiceDescriptor,
    compare_and_clean,
    descriptor_path,
    discover,
    is_compatible,
    publish,
)
from omnivia_core_runtime.ownership.identity import (
    FakeClock,
    FakeProcessEvidence,
    InstallationIdentity,
    ProcessEvidence,
    ServiceInstanceIdentity,
)
from omnivia_core_runtime.ownership.lease import (
    DEFAULT_LEASE_TTL_SECONDS,
    LeaseError,
    LeaseHeld,
    LeaseLifecycle,
    TakeoverRefusal,
    acquire_lease,
    assert_current_authority,
    evaluate_takeover,
    heartbeat,
    read_lease,
    release_lease,
)
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.storage.migrations import (
    bootstrap_generation_one,
    read_workspace_state,
)

WORKSPACE_ID = "ws-lease-0001"


def evidence_for(pid: int, start: str = "100", boot: str = "boot-a") -> ProcessEvidence:
    return ProcessEvidence(pid=pid, start_time=start, boot_id=boot, os_principal="me")


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    connection = open_database(tmp_path / "workspace.sqlite", OpenMode.EPHEMERAL)
    bootstrap_generation_one(
        connection,
        workspace_id=WORKSPACE_ID,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        expect_phase0_baseline=False,
    )
    yield connection
    connection.close()


@pytest.fixture
def identity(tmp_path: Path) -> ServiceInstanceIdentity:
    installation = InstallationIdentity.load_or_create(tmp_path / "state")
    return ServiceInstanceIdentity(
        service_instance_id="svc-one",
        installation_id=installation.installation_id,
        process=evidence_for(1111),
    )


def take(
    db: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    clock: FakeClock,
    **kwargs: object,
) -> object:
    return acquire_lease(
        db,
        identity,
        clock=clock,
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
        **kwargs,  # type: ignore[arg-type]
    )


# LE-01 / LE-02 / LE-04
def test_le01_le02_acquisition_is_atomic_and_increments_the_generation(
    db: sqlite3.Connection, identity: ServiceInstanceIdentity
) -> None:
    clock = FakeClock()
    before = read_workspace_state(db)
    assert before is not None and before.fencing_generation == 1

    lease = take(db, identity, clock)
    assert lease.fencing_generation == 2

    after = read_workspace_state(db)
    assert after is not None and after.fencing_generation == 2, "recorded in workspace state"
    assert read_lease(db) is not None


# LE-03
def test_le03_every_takeover_increments_the_generation(
    db: sqlite3.Connection, identity: ServiceInstanceIdentity
) -> None:
    clock = FakeClock()
    generations = [take(db, identity, clock).fencing_generation]
    for index in range(3):
        successor = ServiceInstanceIdentity(
            service_instance_id=f"svc-{index}",
            installation_id=identity.installation_id,
            process=evidence_for(2000 + index),
        )
        generations.append(take(db, successor, clock).fencing_generation)
    assert generations == sorted(generations)
    assert len(set(generations)) == len(generations), "strictly monotonic"
    assert generations == [2, 3, 4, 5]


# LE-06 / LE-11
def test_le06_le11_expired_heartbeat_alone_does_not_permit_takeover(
    db: sqlite3.Connection, identity: ServiceInstanceIdentity
) -> None:
    """The decisive case: a suspended owner still holds its storage lock."""
    clock = FakeClock()
    take(db, identity, clock)
    lease = read_lease(db)
    assert lease is not None

    clock.advance_monotonic(DEFAULT_LEASE_TTL_SECONDS + 10)
    assert lease.is_expired(clock), "the heartbeat is stale"

    source = FakeProcessEvidence(identity.process)
    # Owner suspended: heartbeat stale, process alive, lock still held.
    decision = evaluate_takeover(
        lease, clock=clock, evidence=source, storage_lock_available=False
    )
    assert not decision.permitted
    assert decision.refusal is TakeoverRefusal.STORAGE_LOCK_UNAVAILABLE
    assert decision.heartbeat_expired
    assert "does not prove the writer is dead" in decision.reason


# LE-07
def test_le07_takeover_requires_storage_lock_availability(
    db: sqlite3.Connection, identity: ServiceInstanceIdentity
) -> None:
    clock = FakeClock()
    take(db, identity, clock)
    lease = read_lease(db)
    assert lease is not None
    clock.advance_monotonic(DEFAULT_LEASE_TTL_SECONDS + 1)

    gone = FakeProcessEvidence(evidence_for(9999))
    gone.set_for_pid(identity.process.pid, None)

    assert not evaluate_takeover(
        lease, clock=clock, evidence=gone, storage_lock_available=False
    ).permitted
    assert evaluate_takeover(
        lease, clock=clock, evidence=gone, storage_lock_available=True
    ).permitted


# LE-09 / LE-10
def test_le09_le10_takeover_requires_process_evidence_not_just_a_pid(
    db: sqlite3.Connection, identity: ServiceInstanceIdentity
) -> None:
    clock = FakeClock()
    take(db, identity, clock)
    lease = read_lease(db)
    assert lease is not None
    clock.advance_monotonic(DEFAULT_LEASE_TTL_SECONDS + 1)

    # Same PID, same start time: still the owner, so refuse.
    same = FakeProcessEvidence(identity.process)
    decision = evaluate_takeover(
        lease, clock=clock, evidence=same, storage_lock_available=True
    )
    assert not decision.permitted
    assert decision.refusal is TakeoverRefusal.OWNER_STILL_ALIVE

    # Same PID, different start time: PID reuse, so the owner is gone.
    reused = FakeProcessEvidence(evidence_for(identity.process.pid, start="999999"))
    assert evaluate_takeover(
        lease, clock=clock, evidence=reused, storage_lock_available=True
    ).permitted

    # Same PID and start time, different boot: also a different process.
    rebooted = FakeProcessEvidence(evidence_for(identity.process.pid, boot="boot-b"))
    assert evaluate_takeover(
        lease, clock=clock, evidence=rebooted, storage_lock_available=True
    ).permitted


# LE-05 covered by the multi-process race in test_filesystem_locking; here the
# database-level guarantee that two acquisitions cannot share a generation.
def test_le05_two_acquisitions_never_share_a_generation(
    db: sqlite3.Connection, identity: ServiceInstanceIdentity
) -> None:
    clock = FakeClock()
    first = take(db, identity, clock)
    second = ServiceInstanceIdentity(
        service_instance_id="svc-two",
        installation_id=identity.installation_id,
        process=evidence_for(2222),
    )
    other = take(db, second, clock)
    assert first.fencing_generation != other.fencing_generation
    lease = read_lease(db)
    assert lease is not None and lease.service_instance_id == "svc-two"


# LE-12
def test_le12_stale_owner_cannot_reclaim_authority(
    db: sqlite3.Connection, identity: ServiceInstanceIdentity
) -> None:
    clock = FakeClock()
    original = take(db, identity, clock)
    successor = ServiceInstanceIdentity(
        service_instance_id="svc-successor",
        installation_id=identity.installation_id,
        process=evidence_for(3333),
    )
    take(db, successor, clock)

    # The predecessor's heartbeat and authority are both refused.
    with pytest.raises(LeaseHeld):
        heartbeat(db, identity, clock=clock)
    with pytest.raises(LeaseError, match="stale authority"):
        assert_current_authority(
            db, identity, WORKSPACE_ID, original.fencing_generation
        )


# LE-13 / LE-14
def test_le13_le14_heartbeat_is_monotonic_and_wall_adjustment_is_harmless(
    db: sqlite3.Connection, identity: ServiceInstanceIdentity
) -> None:
    clock = FakeClock()
    take(db, identity, clock)

    clock.advance_monotonic(5)
    renewed = heartbeat(db, identity, clock=clock)
    assert renewed.heartbeat_monotonic is not None

    # Wall clock jumps an hour backwards; monotonic time does not move.
    clock.advance_wall(-3600)
    lease = read_lease(db)
    assert lease is not None
    assert not lease.is_expired(clock), "a wall-clock jump must not expire a live lease"

    # And a backwards monotonic clock is refused rather than trusted.
    broken = FakeClock(monotonic=0.0)
    with pytest.raises(LeaseError, match="monotonic clock went backwards"):
        heartbeat(db, identity, clock=broken)


# LE-15
def test_le15_graceful_handover_keeps_generations_monotonic(
    db: sqlite3.Connection, identity: ServiceInstanceIdentity
) -> None:
    clock = FakeClock()
    original = take(db, identity, clock)
    released = release_lease(db, identity, clock=clock)
    assert released.lifecycle == LeaseLifecycle.RELEASED.value
    assert released.shutdown_state == "clean"

    # A released lease may be taken immediately, without waiting for expiry.
    decision = evaluate_takeover(
        read_lease(db),
        clock=clock,
        evidence=FakeProcessEvidence(identity.process),
        storage_lock_available=True,
    )
    assert decision.permitted

    successor = ServiceInstanceIdentity(
        service_instance_id="svc-next",
        installation_id=identity.installation_id,
        process=evidence_for(4444),
    )
    taken = take(db, successor, clock, predecessor=identity.service_instance_id)
    assert taken.fencing_generation > original.fencing_generation
    assert taken.takeover_predecessor == identity.service_instance_id


def test_releasing_another_instances_lease_is_refused(
    db: sqlite3.Connection, identity: ServiceInstanceIdentity
) -> None:
    clock = FakeClock()
    take(db, identity, clock)
    impostor = ServiceInstanceIdentity(
        service_instance_id="svc-impostor",
        installation_id=identity.installation_id,
        process=evidence_for(5555),
    )
    with pytest.raises(LeaseHeld):
        release_lease(db, impostor, clock=clock)


# LE-16
def test_le16_lease_record_carries_every_required_evidence_field(
    db: sqlite3.Connection, identity: ServiceInstanceIdentity
) -> None:
    clock = FakeClock()
    take(db, identity, clock, endpoint="unix:///tmp/omnivia.sock")
    lease = read_lease(db)
    assert lease is not None
    for field in (
        "workspace_id",
        "service_instance_id",
        "installation_id",
        "fencing_generation",
        "os_principal",
        "process_pid",
        "process_start",
        "boot_id",
        "endpoint",
        "lock_mechanism",
        "heartbeat_monotonic",
        "heartbeat_at",
        "acquired_at",
        "lifecycle",
    ):
        assert getattr(lease, field) is not None, field
    assert lease.lock_mechanism == "flock"
    assert lease.process is not None


# LE-17 / LE-19 / LE-20
def test_le20_generation_is_never_committed_without_the_storage_lock(
    db: sqlite3.Connection, identity: ServiceInstanceIdentity
) -> None:
    """ADR-037 invariant 17, enforced rather than documented."""
    clock = FakeClock()
    before = read_workspace_state(db)
    assert before is not None

    with pytest.raises(LeaseError, match="without the lifetime storage lock"):
        acquire_lease(
            db,
            identity,
            clock=clock,
            workspace_id=WORKSPACE_ID,
            holds_storage_lock=False,
            lock_mechanism="flock",
        )

    after = read_workspace_state(db)
    assert after is not None
    assert after.fencing_generation == before.fencing_generation, "no generation burned"
    assert read_lease(db) is None


def test_le18_readiness_requires_the_exact_current_lease_tuple(
    db: sqlite3.Connection, identity: ServiceInstanceIdentity
) -> None:
    clock = FakeClock()
    lease = take(db, identity, clock)
    generation = lease.fencing_generation

    assert_current_authority(db, identity, WORKSPACE_ID, generation)

    with pytest.raises(LeaseError, match="stale authority"):
        assert_current_authority(db, identity, WORKSPACE_ID, generation + 1)
    with pytest.raises(LeaseError, match="stale authority"):
        assert_current_authority(db, identity, "ws-other", generation)

    other = ServiceInstanceIdentity(
        service_instance_id="svc-other",
        installation_id=identity.installation_id,
        process=evidence_for(6666),
    )
    with pytest.raises(LeaseError, match="lease belongs to"):
        assert_current_authority(db, other, WORKSPACE_ID, generation)


def test_le22_acquiring_against_the_wrong_workspace_is_refused(
    db: sqlite3.Connection, identity: ServiceInstanceIdentity
) -> None:
    clock = FakeClock()
    with pytest.raises(LeaseError, match="workspace mismatch"):
        acquire_lease(
            db,
            identity,
            clock=clock,
            workspace_id="ws-somewhere-else",
            holds_storage_lock=True,
            lock_mechanism="flock",
        )
    assert read_lease(db) is None


def test_lease_cannot_be_acquired_before_the_substrate_exists(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "bare.sqlite", OpenMode.EPHEMERAL)
    try:
        identity = ServiceInstanceIdentity(
            service_instance_id="svc-x",
            installation_id="inst-x",
            process=evidence_for(7777),
        )
        with pytest.raises(LeaseError):
            acquire_lease(
                connection,
                identity,
                clock=FakeClock(),
                workspace_id=WORKSPACE_ID,
                holds_storage_lock=True,
                lock_mechanism="flock",
            )
    finally:
        connection.close()


def test_no_lease_permits_takeover_immediately() -> None:
    decision = evaluate_takeover(
        None,
        clock=FakeClock(),
        evidence=FakeProcessEvidence(evidence_for(1)),
        storage_lock_available=True,
    )
    assert decision.permitted
    assert decision.refusal is None


def test_fresh_lease_refuses_takeover_even_with_the_lock_free(
    db: sqlite3.Connection, identity: ServiceInstanceIdentity
) -> None:
    clock = FakeClock()
    take(db, identity, clock)
    decision = evaluate_takeover(
        read_lease(db),
        clock=clock,
        evidence=FakeProcessEvidence(identity.process),
        storage_lock_available=True,
    )
    assert not decision.permitted
    assert decision.refusal is TakeoverRefusal.LEASE_IS_FRESH


# --- discovery: BD-09, BD-10, BD-11 ------------------------------------------


def descriptor(instance: str = "svc-one", generation: int = 2, ready: bool = True) -> ServiceDescriptor:
    return ServiceDescriptor(
        workspace_id=WORKSPACE_ID,
        service_instance_id=instance,
        installation_id="inst-one",
        fencing_generation=generation,
        endpoint="unix:///tmp/omnivia.sock",
        readiness=(ReadinessState.READY if ready else ReadinessState.STARTING).value,
        api_version="1.0",
        workspace_format_version="1",
        pid=1111,
    )


# BD-09
def test_bd09_discovery_is_published_atomically(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    publish(runtime, descriptor())
    found = discover(runtime)
    assert found is not None and found.ready
    # No temporary file survives publication.
    assert [p.name for p in runtime.iterdir()] == ["service.json"]

    # Republishing replaces wholesale; a reader never sees a partial document.
    publish(runtime, descriptor(generation=3))
    again = discover(runtime)
    assert again is not None and again.fencing_generation == 3


# BD-10
def test_bd10_failed_startup_does_not_delete_another_instances_descriptor(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    publish(runtime, descriptor(instance="svc-healthy"))

    # A different instance failing during startup must not un-advertise the owner.
    assert compare_and_clean(runtime, "svc-failed") is False
    still = discover(runtime)
    assert still is not None and still.service_instance_id == "svc-healthy"

    # Its own descriptor it may remove.
    assert compare_and_clean(runtime, "svc-healthy") is True
    assert discover(runtime) is None
    assert compare_and_clean(runtime, "svc-healthy") is False


# BD-11
def test_bd11_discovery_reports_absent_for_missing_or_malformed_descriptors(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    assert discover(runtime) is None

    descriptor_path(runtime).write_text("{not json", encoding="utf-8")
    assert discover(runtime) is None, "garbage is absent, not a crash"

    descriptor_path(runtime).write_text('{"workspace_id": "x"}', encoding="utf-8")
    assert discover(runtime) is None, "an incomplete descriptor is absent"

    descriptor_path(runtime).write_text("[]", encoding="utf-8")
    assert discover(runtime) is None


def test_incompatible_service_is_not_treated_as_usable(tmp_path: Path) -> None:
    """A running service of the wrong version is not one this client can use."""
    found = descriptor()
    assert is_compatible(found, api_version="1.0", workspace_format_version="1")
    assert not is_compatible(found, api_version="2.0", workspace_format_version="1")
    assert not is_compatible(found, api_version="1.0", workspace_format_version="2")


def test_descriptor_round_trips_through_json(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    original = descriptor(ready=False)
    publish(runtime, original)
    restored = discover(runtime)
    assert restored == original
    assert not restored.ready
