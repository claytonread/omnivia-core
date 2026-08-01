"""T-0629D acceptance: identity, filesystem qualification and locks (FL-01 … FL-08)."""

from __future__ import annotations

import platform
import sqlite3
from pathlib import Path

import pytest
from omnivia_core_runtime.ownership.identity import (
    FakeClock,
    FakeProcessEvidence,
    InstallationIdentity,
    ProcessEvidence,
    ServiceInstanceIdentity,
    SystemClock,
    SystemProcessEvidence,
    process_is_alive,
)
from omnivia_core_runtime.ownership.locks import (
    IS_WINDOWS,
    QUALIFIED_FILESYSTEMS,
    REFUSED_FILESYSTEMS,
    FilesystemVerdict,
    LockRole,
    LockUnavailable,
    PosixFileLock,
    WindowsFileLock,
    create_lock,
    detect_filesystem,
    qualify_filesystem,
)

from .harness import Barrier, run_child, spawn, wait

posix_only = pytest.mark.skipif(IS_WINDOWS, reason="POSIX lock semantics")
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="Windows lock semantics")


# FL-01
@posix_only
def test_fl01_posix_two_process_exclusion(tmp_path: Path) -> None:
    """A real second process cannot take a lock another process holds."""
    lock_path = tmp_path / "locks" / "storage.lock"
    barrier_dir = tmp_path / "barrier"
    barrier = Barrier(barrier_dir, participants=2)

    holder = spawn("hold_lock.py", str(lock_path), str(barrier_dir), "holder", "10")
    try:
        assert barrier.wait(timeout=15, count=1), "holder never reported arrival"
        # Only now, with the lock provably held, release the contender.
        barrier.release()
        contender = run_child("contend_lock.py", str(lock_path), str(barrier_dir), "contender")
        assert contender.ok, contender.stderr
        assert contender.report.get("acquired") is False, contender.report
    finally:
        (barrier_dir / "release").write_text("1", encoding="utf-8")
        holder_result = wait(holder, "holder", timeout=15)
    assert holder_result.report.get("acquired") is True, holder_result.report


@windows_only
# FL-02
def test_fl02_windows_two_process_exclusion(tmp_path: Path) -> None:  # pragma: no cover
    """The same exclusion on Windows, via msvcrt byte-range locking."""
    lock_path = tmp_path / "locks" / "storage.lock"
    barrier_dir = tmp_path / "barrier"
    barrier = Barrier(barrier_dir, participants=2)

    holder = spawn("hold_lock.py", str(lock_path), str(barrier_dir), "holder", "10")
    try:
        assert barrier.wait(timeout=15, count=1)
        barrier.release()
        contender = run_child("contend_lock.py", str(lock_path), str(barrier_dir), "contender")
        assert contender.report.get("acquired") is False, contender.report
    finally:
        (barrier_dir / "release").write_text("1", encoding="utf-8")
        wait(holder, "holder", timeout=15)


def test_simultaneous_acquisition_has_exactly_one_winner(tmp_path: Path) -> None:
    """BD-07 / LE-05: two racers released together, exactly one wins."""
    lock_path = tmp_path / "locks" / "race.lock"
    barrier_dir = tmp_path / "race-barrier"
    barrier = Barrier(barrier_dir, participants=2)

    first = spawn("race_lock.py", str(lock_path), str(barrier_dir), "one")
    second = spawn("race_lock.py", str(lock_path), str(barrier_dir), "two")
    try:
        assert barrier.wait(timeout=20), "both racers must reach the start line"
        barrier.release()
    finally:
        one = wait(first, "one", timeout=20)
        two = wait(second, "two", timeout=20)

    winners = [
        result.report.get("child")
        for result in (one, two)
        if result.report.get("acquired") is True
    ]
    assert len(winners) == 1, f"expected one winner, got {winners}: {one.report} {two.report}"


# FL-03
def test_fl03_lock_interface_is_identical_across_platforms() -> None:
    """One frozen interface, two implementations."""
    for implementation in (PosixFileLock, WindowsFileLock):
        for member in ("acquire", "release", "path", "role", "held", "read_payload"):
            assert hasattr(implementation, member), f"{implementation.__name__}.{member}"
    # The factory returns the platform implementation behind that interface.
    assert create_lock(Path("/tmp/x.lock"), LockRole.BOOTSTRAP_MUTEX).role is (
        LockRole.BOOTSTRAP_MUTEX
    )


@pytest.mark.parametrize("filesystem", sorted(REFUSED_FILESYSTEMS))
# FL-04 / FL-05 / FL-06
def test_fl04_fl05_fl06_remote_filesystems_refuse_writable_operation(
    tmp_path: Path, filesystem: str
) -> None:
    """NFS, SMB/CIFS, SSHFS and friends are refused for direct writable use.

    The verdict is injected rather than requiring a real mount, because a CI runner
    has none. The real-mount cases are the conditionally-skipped ones below.
    """
    qualification = qualify_filesystem(tmp_path, filesystem=filesystem, probe_locking=False)
    assert not qualification.writable
    assert qualification.verdict is FilesystemVerdict.REFUSED_REMOTE
    assert filesystem in qualification.reason


# FL-07
def test_fl07_unknown_lock_semantics_refuse_writable_operation(tmp_path: Path) -> None:
    """Default-deny: "we did not recognise it" is not evidence that locking works."""
    for name in ("unknown", "somefs", "", "exfat"):
        qualification = qualify_filesystem(tmp_path, filesystem=name, probe_locking=False)
        assert not qualification.writable, name
        assert qualification.verdict is FilesystemVerdict.REFUSED_UNKNOWN


def test_a_qualified_local_filesystem_is_accepted(tmp_path: Path) -> None:
    detected = detect_filesystem(tmp_path)
    qualification = qualify_filesystem(tmp_path)
    if detected in QUALIFIED_FILESYSTEMS:
        assert qualification.writable, qualification
        assert qualification.verdict is FilesystemVerdict.QUALIFIED
    else:  # pragma: no cover - unusual developer filesystem
        assert not qualification.writable
        pytest.skip(f"local filesystem {detected!r} is not in the qualified set")


def test_lock_probe_is_part_of_qualification(tmp_path: Path) -> None:
    """A qualified name still has to honour a real lock probe."""
    qualification = qualify_filesystem(tmp_path, filesystem="apfs", probe_locking=True)
    assert qualification.verdict in (
        FilesystemVerdict.QUALIFIED,
        FilesystemVerdict.REFUSED_NO_LOCKING,
    )
    # The probe leaves nothing behind.
    assert not (tmp_path / ".omnivia-lock-probe").exists()


# FL-08
def test_fl08_lifetime_lock_is_held_for_the_whole_ownership_lifetime(tmp_path: Path) -> None:
    lock = create_lock(tmp_path / "storage.lock", LockRole.LIFETIME_STORAGE)
    assert not lock.held
    assert lock.acquire()
    try:
        assert lock.held
        # Re-acquiring in-process is idempotent, not a second grant.
        assert lock.acquire()
        assert lock.held
    finally:
        lock.release()
    assert not lock.held


def test_lock_roles_state_what_they_grant() -> None:
    """The bootstrap mutex must not be mistakable for write authority."""
    assert not LockRole.BOOTSTRAP_MUTEX.grants_write_authority
    assert not LockRole.TAKEOVER_COORDINATION.grants_write_authority
    assert LockRole.LIFETIME_STORAGE.grants_write_authority


def test_lock_context_manager_raises_when_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "ctx.lock"
    holder = create_lock(path, LockRole.LIFETIME_STORAGE)
    assert holder.acquire()
    try:
        # A second in-process lock object on the same file: flock is per-fd, so a
        # distinct descriptor genuinely contends.
        other = create_lock(path, LockRole.LIFETIME_STORAGE)
        if not IS_WINDOWS:
            with pytest.raises(LockUnavailable), other:
                pass
    finally:
        holder.release()


def test_lock_payload_is_advisory_only(tmp_path: Path) -> None:
    """Ownership comes from the OS lock; the payload is diagnostics.

    A readable payload proves nothing about whether its writer still holds
    anything, which is exactly why lease takeover requires lock *availability* as
    evidence rather than trusting a file's contents.
    """
    path = tmp_path / "advisory.lock"
    lock = create_lock(path, LockRole.LIFETIME_STORAGE, {"service": "svc-1"})
    assert lock.acquire()
    lock.release()

    stale = create_lock(path, LockRole.LIFETIME_STORAGE)
    payload = stale.read_payload()
    assert payload is not None and payload.get("service") == "svc-1"
    # The stale payload does not prevent acquisition.
    assert stale.acquire()
    stale.release()


# --- identity, clock and process evidence ------------------------------------


def test_installation_identity_is_stable_across_restarts(tmp_path: Path) -> None:
    first = InstallationIdentity.load_or_create(tmp_path / "state")
    second = InstallationIdentity.load_or_create(tmp_path / "state")
    assert first.installation_id == second.installation_id
    assert first.installation_id.startswith("inst-")


def test_service_instance_identity_is_unique_per_start(tmp_path: Path) -> None:
    installation = InstallationIdentity.load_or_create(tmp_path / "state")
    evidence = SystemProcessEvidence()
    first = ServiceInstanceIdentity.create(installation, evidence)
    second = ServiceInstanceIdentity.create(installation, evidence)
    assert first.service_instance_id != second.service_instance_id
    assert first.installation_id == second.installation_id


def test_pid_reuse_is_not_proof_of_liveness() -> None:
    """LE-10: same PID, different start time, is a different process."""
    original = ProcessEvidence(pid=4242, start_time="100", boot_id="boot-a", os_principal="me")
    reused = ProcessEvidence(pid=4242, start_time="999", boot_id="boot-a", os_principal="me")
    rebooted = ProcessEvidence(pid=4242, start_time="100", boot_id="boot-b", os_principal="me")

    assert original.is_same_process(original)
    assert not original.is_same_process(reused), "start time must disambiguate"
    assert not original.is_same_process(rebooted), "boot id must disambiguate"


def test_fake_process_evidence_can_express_a_dead_pid() -> None:
    current = ProcessEvidence(pid=1, start_time="1", boot_id="b", os_principal="me")
    source = FakeProcessEvidence(current)
    assert source.for_pid(1) == current
    assert source.for_pid(9999) is None
    source.set_for_pid(1, None)
    assert source.for_pid(1) is None


def test_real_process_evidence_reads_the_current_process() -> None:
    source = SystemProcessEvidence()
    current = source.current()
    assert current.pid > 0
    assert current.os_principal
    if platform.system() in ("Linux", "Darwin"):
        assert current.start_time, "start time must be readable on a supported platform"


def test_process_is_alive_distinguishes_present_from_absent() -> None:
    import os as _os

    assert process_is_alive(_os.getpid())
    assert not process_is_alive(2**30)
    assert not process_is_alive(0)


def test_fake_clock_moves_monotonic_and_wall_independently() -> None:
    """LE-14 depends on this: wall time must be able to jump while monotonic does not."""
    clock = FakeClock()
    monotonic_before = clock.monotonic()
    wall_before = clock.wall_time()

    clock.advance_monotonic(30.0)
    assert clock.monotonic() == monotonic_before + 30.0
    assert clock.wall_time() == wall_before, "monotonic movement must not move wall time"

    clock.advance_wall(-3600)
    assert clock.wall_time() < wall_before, "wall time can go backwards"
    assert clock.monotonic() == monotonic_before + 30.0


def test_system_clock_is_monotonic() -> None:
    clock = SystemClock()
    first = clock.monotonic()
    second = clock.monotonic()
    assert second >= first
    assert clock.wall_time().tzinfo is not None


def test_stock_sqlite_child_imports_no_omnivia_code(tmp_path: Path) -> None:
    """The FM-12 child must be a plain sqlite3 client.

    If it imported the runtime it would exercise the runtime's own guards rather
    than the stock SQLite VFS, which is the opposite of what that case claims.
    """
    from .harness import CHILDREN

    source = (CHILDREN / "stock_sqlite_writer.py").read_text(encoding="utf-8")
    assert "omnivia" not in source.replace("OmniVia", "").replace("omnivia-core", "")

    database = tmp_path / "plain.sqlite"
    connection = sqlite3.connect(str(database))
    connection.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    result = run_child(
        "stock_sqlite_writer.py", str(database), "INSERT INTO t (id) VALUES ('a')"
    )
    assert result.ok, result.stderr
    assert result.report.get("succeeded") is True, result.report


def test_harness_barrier_blocks_until_every_participant_arrives(tmp_path: Path) -> None:
    barrier = Barrier(tmp_path / "b", participants=2)
    barrier.arrive("one")
    assert not barrier.wait(timeout=0.2), "one of two is not a rendezvous"
    barrier.arrive("two")
    assert barrier.wait(timeout=2.0)
    assert not barrier.wait_for_release(timeout=0.1)
    barrier.release()
    assert barrier.wait_for_release(timeout=2.0)


def test_harness_reports_a_timeout_rather_than_hanging(tmp_path: Path) -> None:
    """A hung child must not wedge CI."""
    barrier_dir = tmp_path / "never"
    barrier_dir.mkdir()
    process = spawn("contend_lock.py", str(tmp_path / "x.lock"), str(barrier_dir), "waiter")
    result = wait(process, "waiter", timeout=1.0)
    assert result.returncode == -9
    assert result.stderr == "timeout"
