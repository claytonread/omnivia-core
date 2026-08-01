"""Installation and service identity, clocks and process evidence (T-0629D).

ADR-037: "PID identity alone is insufficient because of PID reuse." Deciding
whether an apparently stale lease owner is actually dead therefore needs more than
a PID — it needs a process start time and a boot identifier, because a PID plus a
start time can still repeat across a reboot.

Clock and process evidence are injectable protocols rather than direct calls to
`time` and `os`. Without that seam, LE-14 (a wall-clock adjustment must not expire
a live lease) and LE-10 (PID reuse is not proof of liveness) cannot be written at
all, and the bugs they catch would be invisible.
"""

from __future__ import annotations

import ctypes
import os
import platform
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

INSTALLATION_ID_FILE = "installation-id"


class Clock(Protocol):
    """Time source for heartbeat and expiry decisions."""

    def monotonic(self) -> float:
        """Monotonic seconds. Used for every expiry decision."""

    def wall_time(self) -> datetime:
        """Wall-clock time. Recorded for diagnostics, never used for expiry."""


class SystemClock:
    """The real clock."""

    def monotonic(self) -> float:
        return time.monotonic()

    def wall_time(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Test clock whose monotonic and wall components move independently.

    The independence is the point. A single `now()` cannot express "wall time
    jumped backwards while monotonic time advanced normally", which is exactly the
    scenario that catches expiry logic wrongly built on wall time.
    """

    def __init__(
        self,
        *,
        monotonic: float = 1_000.0,
        wall: datetime | None = None,
    ) -> None:
        self._monotonic = monotonic
        self._wall = wall or datetime(2026, 7, 30, tzinfo=UTC)

    def monotonic(self) -> float:
        return self._monotonic

    def wall_time(self) -> datetime:
        return self._wall

    def advance_monotonic(self, seconds: float) -> None:
        self._monotonic += seconds

    def set_wall_time(self, moment: datetime) -> None:
        self._wall = moment

    def advance_wall(self, seconds: float) -> None:
        self._wall = datetime.fromtimestamp(self._wall.timestamp() + seconds, tz=UTC)


@dataclass(frozen=True)
class ProcessEvidence:
    """Evidence about a process, sufficient to survive PID reuse.

    `boot_id` participates because a PID and a start time together can repeat after
    a reboot. All three must match for a process to be considered the same one.
    """

    pid: int
    start_time: str
    boot_id: str
    os_principal: str

    def is_same_process(self, other: ProcessEvidence) -> bool:
        return (
            self.pid == other.pid
            and self.start_time == other.start_time
            and self.boot_id == other.boot_id
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "start_time": self.start_time,
            "boot_id": self.boot_id,
            "os_principal": self.os_principal,
        }


class ProcessEvidenceSource(Protocol):
    """Supplies process evidence for this process or another."""

    def current(self) -> ProcessEvidence: ...

    def for_pid(self, pid: int) -> ProcessEvidence | None: ...


class SystemProcessEvidence:
    """Real process evidence.

    Start time is read per platform: `/proc/<pid>/stat` field 22 on Linux, `ps
    -o lstart` on macOS, and `GetProcessTimes` on Windows. That platform split is
    precisely why the lease logic takes this as a protocol — a test that had to
    produce a real reused PID would be untestable.
    """

    def __init__(self) -> None:
        self._boot_id = _boot_id()

    def current(self) -> ProcessEvidence:
        evidence = self.for_pid(os.getpid())
        if evidence is None:  # pragma: no cover - the current process always exists
            raise RuntimeError("cannot read evidence for the current process")
        return evidence

    def for_pid(self, pid: int) -> ProcessEvidence | None:
        start = _process_start_time(pid)
        if start is None:
            return None
        return ProcessEvidence(
            pid=pid,
            start_time=start,
            boot_id=self._boot_id,
            os_principal=_os_principal(),
        )


class FakeProcessEvidence:
    """Test double allowing arbitrary evidence, including reused PIDs."""

    def __init__(self, current: ProcessEvidence) -> None:
        self._current = current
        self._table: dict[int, ProcessEvidence] = {current.pid: current}

    def current(self) -> ProcessEvidence:
        return self._current

    def for_pid(self, pid: int) -> ProcessEvidence | None:
        return self._table.get(pid)

    def set_for_pid(self, pid: int, evidence: ProcessEvidence | None) -> None:
        if evidence is None:
            self._table.pop(pid, None)
        else:
            self._table[pid] = evidence


def _os_principal() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # noqa: BLE001  # pragma: no cover - any failure means unknown
        return str(os.getuid()) if hasattr(os, "getuid") else "unknown"


def _boot_id() -> str:
    """An identifier that changes when the machine reboots."""
    system = platform.system()
    if system == "Linux":
        for candidate in (Path("/proc/sys/kernel/random/boot_id"),):
            try:
                return candidate.read_text(encoding="utf-8").strip()
            except OSError:
                continue
    if system == "Darwin":
        try:
            output = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout.strip()
            return output
        except Exception:  # noqa: BLE001,S110  # pragma: no cover - fall through to the labelled unknown
            pass
    # Last resort: not reboot-stable, so it is labelled rather than pretending.
    return f"unknown-boot:{platform.node()}"


# --- Windows process probes ---------------------------------------------------
#
# Native Win32 through `ctypes`, which is standard library, so this needs no
# dependency. Neither POSIX route works here:
#
# `os.kill(pid, 0)` is not an existence probe on Windows. Signal 0 is
# `CTRL_C_EVENT`, so the apparently harmless probe sends a console control event
# to the process group and raises `KeyboardInterrupt` in the caller -- which is
# how a hosted Windows run died inside the liveness test that called it.
#
# A subprocess per probe is not viable either. Asking PowerShell for
# `(Get-Process -Id n).StartTime` costs a process launch on every single probe,
# which is what turned this suite into a multi-minute run on the hosted runner.

#: `PROCESS_QUERY_LIMITED_INFORMATION`. The narrowest access right that both
#: answers "does this process exist" and satisfies `GetProcessTimes`, and unlike
#: `PROCESS_QUERY_INFORMATION` it is granted for processes at other integrity
#: levels, so an ordinary probe is not refused for lack of privilege.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: `ERROR_ACCESS_DENIED`. Refusing to open a process is evidence that it exists:
#: the kernel resolved the PID and then denied this caller.
_ERROR_ACCESS_DENIED = 5


class _FILETIME(ctypes.Structure):
    """Win32 `FILETIME`: 100-nanosecond intervals since 1601-01-01 UTC, in halves."""

    _fields_ = (
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    )


def _kernel32() -> Any:
    """`kernel32` with every entry point used here already configured, or None.

    All four signatures are set before any call is made, deliberately.
    `GetLastError` reports the calling thread's last error, so resolving it lazily
    after a failed `OpenProcess` would place a `GetProcAddress` between the failure
    and the read, and the code read back could be that lookup's.

    None means kernel32 could not be loaded -- every non-Windows host, and any
    Windows host where the load or configuration fails. That is "proved nothing",
    which each caller below turns into its fail-closed answer rather than a guess.
    """
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return None
    try:
        kernel32 = loader("kernel32")
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int32
        kernel32.GetProcessTimes.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
        )
        kernel32.GetProcessTimes.restype = ctypes.c_int32
        kernel32.GetLastError.argtypes = ()
        kernel32.GetLastError.restype = ctypes.c_uint32
    except Exception:  # noqa: BLE001 - an unusable kernel32 is evidence of nothing
        return None
    return kernel32


def _windows_process_is_alive(pid: int) -> bool:
    """Whether `pid` exists, decided by whether a handle to it can be opened.

    Two outcomes are evidence of existence: a handle that opens, and an open
    refused with `ERROR_ACCESS_DENIED`. Everything else is reported as absent --
    `ERROR_INVALID_PARAMETER` for a PID nothing is using, an unusable kernel32, an
    error this does not recognise -- because none of them is evidence of a live
    process, and claiming liveness without evidence is what keeps a dead owner's
    lease alive forever.
    """
    kernel32 = _kernel32()
    if kernel32 is None:  # pragma: no cover - kernel32 always loads on Windows
        return False
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        return bool(kernel32.GetLastError() == _ERROR_ACCESS_DENIED)
    kernel32.CloseHandle(handle)
    return True


def _windows_process_start_time(pid: int) -> str | None:
    """Creation time from `GetProcessTimes`, as an opaque comparable string.

    The handle is closed on every path, the failing ones included. A leaked
    process handle keeps the kernel object alive after the process exits, which
    holds its PID out of circulation and hides the PID reuse this evidence exists
    to detect.
    """
    kernel32 = _kernel32()
    if kernel32 is None:  # pragma: no cover - kernel32 always loads on Windows
        return None
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        return None
    created = _FILETIME()
    exited = _FILETIME()
    kernel_time = _FILETIME()
    user_time = _FILETIME()
    try:
        # All four outputs are required parameters; only the first is wanted.
        read = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
    finally:
        kernel32.CloseHandle(handle)
    if not read:
        return None
    # The halves are one 64-bit count. Either half on its own repeats, so a start
    # time built from one would collide between genuinely different processes.
    hundred_nanoseconds = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
    if not hundred_nanoseconds:
        # Zero is not a creation time. Reported as evidence it would make every
        # process look alike, so it is reported as no evidence at all.
        return None
    return str(hundred_nanoseconds)


def _process_start_time(pid: int) -> str | None:
    """Process start time as an opaque comparable string, or None if absent."""
    system = platform.system()
    if system == "Linux":
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
            return fields[21]
        except (OSError, IndexError):
            return None
    if system in ("Darwin", "FreeBSD"):
        try:
            result = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            value = result.stdout.strip()
            return value or None
        except Exception:  # noqa: BLE001  # pragma: no cover - an unreadable start time is None
            return None
    if system == "Windows":
        return _windows_process_start_time(pid)
    return None  # pragma: no cover - unsupported platform


def process_is_alive(pid: int) -> bool:
    """Whether a PID currently exists. Not proof it is the process you mean."""
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        # Not a shared implementation detail: on Windows `os.kill(pid, 0)` raises
        # `KeyboardInterrupt` in this process instead of answering the question.
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another principal.
        return True
    except OSError:  # pragma: no cover - platform dependent
        return False
    return True


@dataclass(frozen=True)
class InstallationIdentity:
    """Stable identity of this Core installation, persisted across restarts."""

    installation_id: str

    @classmethod
    def load_or_create(cls, directory: Path) -> InstallationIdentity:
        """Read the installation id, creating it once if absent.

        Written atomically so two services starting together cannot observe a
        partially written identity.
        """
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / INSTALLATION_ID_FILE
        if path.is_file():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return cls(installation_id=existing)

        value = f"inst-{uuid.uuid4()}"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(value + "\n", encoding="utf-8")
        try:
            temporary.replace(path)
        except OSError:  # pragma: no cover - platform dependent
            pass
        # Re-read: another process may have won the race, and there must be exactly
        # one installation identity.
        return cls(installation_id=path.read_text(encoding="utf-8").strip())


@dataclass(frozen=True)
class ServiceInstanceIdentity:
    """Identity of one service start. Unique per start, never reused."""

    service_instance_id: str
    installation_id: str
    process: ProcessEvidence

    @classmethod
    def create(
        cls,
        installation: InstallationIdentity,
        evidence: ProcessEvidenceSource,
    ) -> ServiceInstanceIdentity:
        return cls(
            service_instance_id=f"svc-{uuid.uuid4()}",
            installation_id=installation.installation_id,
            process=evidence.current(),
        )


__all__ = [
    "INSTALLATION_ID_FILE",
    "Clock",
    "FakeClock",
    "FakeProcessEvidence",
    "InstallationIdentity",
    "ProcessEvidence",
    "ProcessEvidenceSource",
    "ServiceInstanceIdentity",
    "SystemClock",
    "SystemProcessEvidence",
    "process_is_alive",
]
