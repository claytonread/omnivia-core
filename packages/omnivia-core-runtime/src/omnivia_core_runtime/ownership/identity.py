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

import os
import platform
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

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
    if system == "Windows":  # pragma: no cover - exercised on the Windows runner
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-Process -Id {pid}).StartTime.Ticks",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            value = result.stdout.strip()
            return value or None
        except Exception:  # noqa: BLE001
            return None
    return None  # pragma: no cover - unsupported platform


def process_is_alive(pid: int) -> bool:
    """Whether a PID currently exists. Not proof it is the process you mean."""
    if pid <= 0:
        return False
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
