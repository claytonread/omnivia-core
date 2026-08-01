"""T-0629D acceptance: identity, filesystem qualification and locks (FL-01 … FL-08)."""

from __future__ import annotations

import ctypes
import os
import platform
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.ownership.identity import (
    _ERROR_ACCESS_DENIED,
    _PROCESS_QUERY_LIMITED_INFORMATION,
    FakeClock,
    FakeProcessEvidence,
    InstallationIdentity,
    ProcessEvidence,
    ServiceInstanceIdentity,
    SystemClock,
    SystemProcessEvidence,
    _process_start_time,
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
    _windows_filesystem,
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


# --- what the platforms actually report ---------------------------------------
#
# Detection is the half of qualification a unit test kept missing: the rules above
# inject a filesystem name, so they pass no matter what the real probes return. On
# hosted CI both probes returned a name the rules then refused -- Linux because
# `stat -f -c %T` prints one label for the whole ext family, Windows because there
# was no probe at all -- and a standard runner was told its own disk was
# unqualified. These pin the reported values, not just the rules applied to them.


def test_the_linux_ext_statfs_label_is_qualified(tmp_path: Path) -> None:
    """`stat -f -c %T` prints `ext2/ext3` for ext2, ext3 and ext4 alike.

    GNU coreutils maps statfs magic 0xEF53 to that one string, so a hosted Ubuntu
    runner reports its ext4 workspace under a name that listing `ext2`, `ext3` and
    `ext4` individually never matched.
    """
    assert "ext2/ext3" in QUALIFIED_FILESYSTEMS
    qualification = qualify_filesystem(tmp_path, filesystem="ext2/ext3", probe_locking=False)
    assert qualification.writable, qualification
    assert qualification.verdict is FilesystemVerdict.QUALIFIED


def test_the_ext_label_is_matched_exactly_rather_than_by_substring(tmp_path: Path) -> None:
    """Recognising one compound label must not become a prefix or substring rule.

    A containment test would have been the shorter fix and would admit anything
    whose name happens to embed a qualified one, which is precisely the assumption
    default-deny exists to refuse.
    """
    for name in ("ext2/ext3/ext4", "ext", "ext5", "myext4", "ext4fs", "ext2/ext3fs"):
        qualification = qualify_filesystem(tmp_path, filesystem=name, probe_locking=False)
        assert not qualification.writable, name
        assert qualification.verdict is FilesystemVerdict.REFUSED_UNKNOWN, name


@pytest.mark.skipif(platform.system() != "Linux", reason="Linux statfs label")
def test_linux_detection_names_the_filesystem_it_is_running_on(tmp_path: Path) -> None:
    """The Ubuntu row's own evidence: detection must produce a real name here.

    Asserted directly rather than left to the service tests, where the same defect
    surfaced as twelve unrelated-looking startup failures.
    """
    detected = detect_filesystem(tmp_path)
    assert detected != "unknown"
    assert detected in QUALIFIED_FILESYSTEMS, (
        f"this Linux filesystem reports as {detected!r}, which the qualified set "
        "does not recognise"
    )


class _FakeKernel32:
    """The two `kernel32` entry points `_windows_filesystem` calls.

    They exist only on Windows, so macOS and Linux exercise the helper's logic
    through this fake -- output buffers included, since a real
    `create_unicode_buffer` works on every platform -- and the hosted Windows row
    is the proof that the real API is driven correctly.
    """

    def __init__(
        self,
        *,
        filesystem: str = "NTFS",
        drive_type: int = 3,  # DRIVE_FIXED
        volume_path_ok: bool = True,
        volume_info_ok: bool = True,
        root: str = "C:\\",
    ) -> None:
        self.filesystem = filesystem
        self.drive_type = drive_type
        self.volume_path_ok = volume_path_ok
        self.volume_info_ok = volume_info_ok
        self.root = root

    # Win32 spelling, deliberately: these stand in for the real entry points and
    # are looked up by exactly these names.
    def GetVolumePathNameW(self, path: str, buffer: Any, size: int) -> int:
        if not self.volume_path_ok:
            return 0
        buffer.value = self.root
        return 1

    def GetDriveTypeW(self, root: str) -> int:
        return self.drive_type

    def GetVolumeInformationW(
        self,
        root: str,
        volume_name: Any,
        volume_size: int,
        serial: Any,
        component_length: Any,
        flags: Any,
        filesystem_name: Any,
        filesystem_size: int,
    ) -> int:
        if not self.volume_info_ok:
            return 0
        filesystem_name.value = self.filesystem
        return 1


def _patch_kernel32(monkeypatch: pytest.MonkeyPatch, fake: _FakeKernel32) -> None:
    """`ctypes.WinDLL` does not exist off Windows, hence `raising=False`."""
    monkeypatch.setattr(ctypes, "WinDLL", lambda name: fake, raising=False)


def test_windows_detection_reports_the_volume_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_kernel32(monkeypatch, _FakeKernel32(filesystem="NTFS"))
    assert _windows_filesystem(tmp_path) == "ntfs"


def test_windows_detection_is_not_ntfs_by_assumption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Windows host is not evidence that the workspace sits on NTFS.

    Returning `ntfs` from the platform name would make the gate a tautology: it
    would report a ReFS or exFAT workspace as qualified without ever having asked
    the volume, and those lock semantics are exactly what has not been qualified.
    """
    _patch_kernel32(monkeypatch, _FakeKernel32(filesystem="ReFS"))
    assert _windows_filesystem(tmp_path) == "refs"
    assert not qualify_filesystem(tmp_path, filesystem="refs", probe_locking=False).writable

    _patch_kernel32(monkeypatch, _FakeKernel32(filesystem="exFAT"))
    assert _windows_filesystem(tmp_path) == "exfat"
    assert not qualify_filesystem(tmp_path, filesystem="exfat", probe_locking=False).writable


@pytest.mark.parametrize(
    ("case", "fake"),
    [
        ("no volume path", _FakeKernel32(volume_path_ok=False)),
        ("no volume information", _FakeKernel32(volume_info_ok=False)),
        ("empty filesystem name", _FakeKernel32(filesystem="   ")),
        # A network volume reports the *server's* filesystem, which says nothing
        # about cross-host locking; ADR-037 refuses those outright.
        ("network drive", _FakeKernel32(drive_type=4)),
    ],
)
def test_windows_detection_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str, fake: _FakeKernel32
) -> None:
    _patch_kernel32(monkeypatch, fake)
    assert _windows_filesystem(tmp_path) == "unknown", case
    qualification = qualify_filesystem(tmp_path, filesystem="unknown", probe_locking=False)
    assert not qualification.writable, case
    assert qualification.verdict is FilesystemVerdict.REFUSED_UNKNOWN, case


def test_windows_detection_survives_an_unavailable_kernel32(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A probe that raises is a probe that proved nothing, not a reason to crash."""

    def unavailable(name: str) -> object:
        raise OSError("kernel32 unavailable")

    monkeypatch.setattr(ctypes, "WinDLL", unavailable, raising=False)
    assert _windows_filesystem(tmp_path) == "unknown"


def test_detect_filesystem_probes_the_volume_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The dispatch, not only the helper: Windows used to fall through to unknown.

    Every Windows path was refused before this, which is why the Windows row could
    not qualify its own runner disk.
    """
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    _patch_kernel32(monkeypatch, _FakeKernel32(filesystem="NTFS"))
    assert detect_filesystem(tmp_path) == "ntfs"
    assert qualify_filesystem(tmp_path, probe_locking=False).writable


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
    if platform.system() in ("Linux", "Darwin", "Windows"):
        assert current.start_time, "start time must be readable on a supported platform"


def test_process_is_alive_distinguishes_present_from_absent() -> None:
    assert process_is_alive(os.getpid())
    assert not process_is_alive(2**30)
    assert not process_is_alive(0)


# --- the Windows process probes ----------------------------------------------
#
# The two cases above are the real proof, and on the hosted Windows runner they
# are what failed: `os.kill(pid, 0)` is not an existence probe there. Signal 0 is
# `CTRL_C_EVENT`, so probing this process's own liveness delivered a console
# control event to the process group and the run died of `KeyboardInterrupt`
# inside that assertion -- after the PowerShell launched per start-time probe had
# already stretched the preceding cases past three minutes.
#
# `OpenProcess` and `GetProcessTimes` cannot be called from macOS or Linux, so the
# decisions taken around them are pinned here through a fake kernel32, and the
# hosted Windows row is the proof that the real API is driven correctly.

#: `ERROR_INVALID_PARAMETER`, which is what opening an unused PID reports.
_ERROR_INVALID_PARAMETER = 87

#: A plausible `FILETIME` creation time, in the two halves Win32 reports it in.
_CREATION_LOW = 0x9DC53E00
_CREATION_HIGH = 0x01DBF7A2
_CREATION_100NS = str((_CREATION_HIGH << 32) | _CREATION_LOW)


class _FakeWinFunction:
    """One entry point of the fake kernel32.

    A `ctypes` foreign function carries `argtypes` and `restype`, and the probes
    set both on every entry point before calling anything. A bound method cannot
    hold those attributes, so each entry point is an object with a `__call__`
    rather than a method.
    """

    def __init__(self, implementation: Any) -> None:
        self._implementation = implementation
        self.argtypes: Any = None
        self.restype: Any = None

    def __call__(self, *arguments: Any) -> Any:
        # `is not None`, because `GetLastError` takes no arguments and is
        # configured with an empty -- and therefore falsy -- `argtypes`. The
        # signatures must be in place before the first call: the error code is
        # read from the thread straight after a failed `OpenProcess`, and
        # resolving an entry point in between could overwrite it.
        assert self.argtypes is not None, "called before its signature was configured"
        assert self.restype is not None, "called before its signature was configured"
        return self._implementation(*arguments)


def _behind_byref(argument: Any) -> Any:
    """The structure a `ctypes.byref` argument points at."""
    return getattr(argument, "_obj", argument)


class _FakeKernel32Process:
    """The `kernel32` entry points the Windows process probes drive."""

    def __init__(
        self,
        *,
        handle: int = 0x2A,
        last_error: int = _ERROR_INVALID_PARAMETER,
        creation: tuple[int, int] = (_CREATION_LOW, _CREATION_HIGH),
        times_ok: bool = True,
    ) -> None:
        self.handle = handle
        self.last_error = last_error
        self.creation = creation
        self.times_ok = times_ok
        self.access_rights: list[int] = []
        self.opened: list[int] = []
        self.closed: list[int] = []
        # Win32 spelling, deliberately: these stand in for the real entry points
        # and are looked up by exactly these names.
        self.OpenProcess = _FakeWinFunction(self._open_process)
        self.CloseHandle = _FakeWinFunction(self._close_handle)
        self.GetProcessTimes = _FakeWinFunction(self._get_process_times)
        self.GetLastError = _FakeWinFunction(lambda: self.last_error)

    def _open_process(self, access: int, inherit: int, pid: int) -> int:
        self.access_rights.append(access)
        if not self.handle:
            return 0  # NULL, exactly as a failed OpenProcess returns.
        self.opened.append(self.handle)
        return self.handle

    def _close_handle(self, handle: int) -> int:
        self.closed.append(handle)
        return 1

    def _get_process_times(
        self, handle: Any, created: Any, exited: Any, kernel: Any, user: Any
    ) -> int:
        if not self.times_ok:
            return 0
        low, high = self.creation
        target = _behind_byref(created)
        target.dwLowDateTime = low
        target.dwHighDateTime = high
        return 1


def _patch_windows_probes(monkeypatch: pytest.MonkeyPatch, fake: _FakeKernel32Process) -> None:
    """Drive the Windows branch from any host, with both POSIX escapes blocked.

    `os.kill` and `subprocess.run` are made to fail loudly rather than left alone.
    The defect being pinned is that the Windows path reached them at all, and a
    test that only checked the returned value would pass either way.

    `ctypes.WinDLL` does not exist off Windows, hence `raising=False`.
    """

    def signalled(*arguments: Any, **keywords: Any) -> object:
        raise AssertionError("signal 0 is CTRL_C_EVENT on Windows; os.kill must not run")

    def launched(*arguments: Any, **keywords: Any) -> object:
        raise AssertionError("the Windows probes must not launch a subprocess")

    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(ctypes, "WinDLL", lambda name: fake, raising=False)
    monkeypatch.setattr(os, "kill", signalled)
    monkeypatch.setattr(subprocess, "run", launched)


def test_windows_liveness_opens_and_closes_a_process_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An opened handle is the existence proof, and it is handed straight back."""
    fake = _FakeKernel32Process()
    _patch_windows_probes(monkeypatch, fake)

    assert process_is_alive(4242)
    assert fake.access_rights == [_PROCESS_QUERY_LIMITED_INFORMATION]
    assert fake.opened == [fake.handle]
    assert fake.closed == fake.opened, "every opened handle must be closed"


def test_windows_liveness_is_absent_when_the_process_cannot_be_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: an unused PID reports `ERROR_INVALID_PARAMETER`, not liveness."""
    fake = _FakeKernel32Process(handle=0, last_error=_ERROR_INVALID_PARAMETER)
    _patch_windows_probes(monkeypatch, fake)

    assert not process_is_alive(2**30)
    assert fake.opened == []
    assert fake.closed == [], "nothing was opened, so there is nothing to close"


def test_windows_access_denied_is_evidence_the_process_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused open means the kernel resolved the PID and denied this caller.

    Reading that as absence would declare a live lease owner dead whenever it runs
    as another principal, and take its storage over while it is still writing.
    """
    fake = _FakeKernel32Process(handle=0, last_error=_ERROR_ACCESS_DENIED)
    _patch_windows_probes(monkeypatch, fake)

    assert process_is_alive(4242)
    assert fake.closed == [], "a failed open returns no handle to close"


def test_windows_liveness_refuses_a_non_positive_pid_without_probing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PID 0 is the System Idle Process: genuinely present, never a lease owner."""
    fake = _FakeKernel32Process()
    _patch_windows_probes(monkeypatch, fake)

    assert not process_is_alive(0)
    assert not process_is_alive(-1)
    assert fake.access_rights == [], "the guard decides before any Win32 call"


def test_windows_start_time_comes_from_getprocesstimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeKernel32Process()
    _patch_windows_probes(monkeypatch, fake)

    assert _process_start_time(4242) == _CREATION_100NS
    assert fake.closed == [fake.handle]


def test_windows_start_time_uses_both_halves_of_the_filetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The low half wraps every 429 seconds, so alone it is not an identity.

    Two processes started further apart than that can share it, which would make
    a reused PID look like the same process -- the one thing this evidence exists
    to rule out.
    """
    later = _FakeKernel32Process(creation=(_CREATION_LOW, _CREATION_HIGH + 1))
    _patch_windows_probes(monkeypatch, later)

    expected = str(((_CREATION_HIGH + 1) << 32) | _CREATION_LOW)
    assert _process_start_time(4242) == expected
    assert expected != _CREATION_100NS, "the same low half must not produce the same evidence"


def test_windows_start_time_is_none_when_getprocesstimes_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeKernel32Process(times_ok=False)
    _patch_windows_probes(monkeypatch, fake)

    assert _process_start_time(4242) is None
    assert fake.closed == [fake.handle], "the failing path closes its handle too"


def test_windows_start_time_is_none_when_the_process_cannot_be_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeKernel32Process(handle=0)
    _patch_windows_probes(monkeypatch, fake)

    assert _process_start_time(2**30) is None
    # Asserted rather than assumed: "no evidence" is also what returning nothing at
    # all looks like, so the probe has to be shown to have run and failed.
    assert fake.access_rights == [_PROCESS_QUERY_LIMITED_INFORMATION]
    assert fake.closed == []


def test_windows_start_time_refuses_a_zero_creation_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero is not a creation time; reported as one it would make processes alike."""
    fake = _FakeKernel32Process(creation=(0, 0))
    _patch_windows_probes(monkeypatch, fake)

    assert _process_start_time(4242) is None
    assert fake.closed == [fake.handle]


def test_windows_current_process_evidence_needs_no_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole evidence path on Windows, with no process launched anywhere.

    A launch per probe is what made the hosted run take minutes to reach the
    failure, so "it works" is not the only requirement here.
    """
    fake = _FakeKernel32Process()
    _patch_windows_probes(monkeypatch, fake)

    evidence = SystemProcessEvidence().current()
    assert evidence.pid == os.getpid()
    assert evidence.start_time == _CREATION_100NS
    assert evidence.boot_id


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
