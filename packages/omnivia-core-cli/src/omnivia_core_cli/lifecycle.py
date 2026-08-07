"""Where a default installation lives, and how to start and stop its service.

This module holds the two things `main.py`'s dialling code does not: a path
convention, and process control. Neither crosses the package boundary.

**The service is launched, never imported.** ADR-036 admits exactly that -- "MCP
and CLI may locate or launch the service executable, but communicate only through
the application API" -- so `omnivia-core-service` is located on `PATH` and spawned
as a subprocess. Nothing here imports `omnivia_core_runtime`, opens a database or
takes a lease; the started process owns all three, as ADR-037 requires.

**The path convention.** Core had none: every caller passed `--workspace` and
`--installation-state` explicitly, which is workable for a launcher and useless
for an MCP client where no human types flags. One convention, rooted at a single
directory:

```text
~/.omnivia                    (an explicit flag still wins)
├── workspace/                --workspace
├── installation-state/       --installation-state
│   └── runtime/<workspace-id>/service.json
└── run/
    ├── s.sock                the advertised local endpoint
    └── service.log           the started process's own output
```

`~/.omnivia` follows the only precedent in the tree,
`src/omnivia_core/workspace/models.py`'s `Path.home() / ".omnivia"`. `run/` is
short on purpose: `sockaddr_un` caps a socket path at 104 bytes -- 103 usable,
and fewer still once the staging name is counted -- so a deep default would
refuse to bind at all.

**No environment variable.** The convention is fixed, and
`test_the_cli_reads_no_environment_variable_to_find_a_service` is why: an
environment lookup is an unrestricted filesystem path arriving by another name,
which that packet makes a stop condition. A fixed convention is bounded and
identical on every machine; an override is not. Only an explicit flag overrides.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnivia_core.contracts.v1 import ServiceProcessEvidence
from omnivia_core_cli.client import read_descriptor

__all__ = [
    "DEFAULT_HOME_DIRECTORY",
    "SERVICE_EXECUTABLE",
    "START_TIMEOUT_SECONDS",
    "STOP_TIMEOUT_SECONDS",
    "Installation",
    "LifecycleError",
    "descriptor_is_gone",
    "home_directory",
    "locate_service",
    "process_identity",
    "process_is_gone",
    "request_managed_start",
    "request_stop",
]

#: The default installation root under the user's home directory.
DEFAULT_HOME_DIRECTORY = ".omnivia"

#: The console script `omnivia-core-runtime` installs. Located on `PATH`, spawned,
#: and never imported.
SERVICE_EXECUTABLE = "omnivia-core-service"

#: How long `start` waits for a spawned service to advertise and answer. Startup
#: runs migrations and recovery, so the budget is a startup budget rather than a
#: call budget.
START_TIMEOUT_SECONDS = 90.0

#: How long `stop` waits for the service to unwind and take its descriptor with it.
STOP_TIMEOUT_SECONDS = 30.0

#: Gap between polls, for both waits.
POLL_SECONDS = 0.1

#: `sockaddr_un.sun_path`, checked here so an over-long default is refused with the
#: reason rather than as an opaque transport failure from the started process. The
#: same number the runtime's own `MAX_SOCKET_PATH_BYTES` holds; it is a kernel
#: structure size, and the boundary forbids importing the module that states it.
MAX_SOCKET_PATH_BYTES = 104

#: The workspace manifest's filename, mirroring the runtime's `MANIFEST_NAME` for
#: the same reason: it cannot be imported, and checking for it before spawning
#: turns "no workspace manifest at ..." from a traceback in a log file into a
#: sentence naming the directory that is missing one.
MANIFEST_NAME = "workspace.json"

#: What `process_identity` can conclude about a pid the descriptor named.
IDENTITY_SAME = "same"
IDENTITY_DIFFERENT = "different"
IDENTITY_UNREADABLE = "unreadable"


class LifecycleError(Exception):
    """A refusal whose message is meant for the caller, not a traceback."""


def locate_service() -> str | None:
    """The service console script: on `PATH`, or failing that beside this one.

    `PATH` alone is not enough for the case this command exists to serve. An MCP
    client spawns the CLI directly, with whatever environment its host happens to
    hold, and a virtual environment's `bin` is on `PATH` only for a shell that
    activated it. The two scripts are installed side by side by the same
    installer, so the interpreter running this one already knows where the other
    is -- `sys.executable`'s directory. Looking there is not a guess.

    `PATH` still wins, so a deliberately shadowed build is still honoured.
    """
    found = shutil.which(SERVICE_EXECUTABLE)
    if found is not None:
        return found
    beside = Path(sys.executable).parent / SERVICE_EXECUTABLE
    return str(beside) if beside.is_file() and os.access(beside, os.X_OK) else None


def home_directory(override: Path | None = None) -> Path:
    """The installation root: an explicit flag, otherwise one fixed convention.

    Deliberately *not* an environment variable.
    `test_the_cli_reads_no_environment_variable_to_find_a_service` forbids it and
    gives the reason: an environment lookup is "an unrestricted filesystem path
    arriving by another name", which that packet makes a stop condition. A fixed
    convention is a different thing -- it is bounded, it is the same on every
    machine, and there is nothing an attacker can point somewhere else.

    That test's docstring also says `--runtime-state` is "the only way in". This
    default makes that sentence inaccurate rather than false: a flag still wins,
    and no caller-supplied ambient path is admitted. Whether the packet should be
    amended to record the convention is the owner's call, not this module's.
    """
    if override is not None:
        return override.expanduser()
    return Path.home() / DEFAULT_HOME_DIRECTORY


@dataclass(frozen=True)
class Installation:
    """One installation root and the paths the convention derives from it."""

    home: Path

    @property
    def workspace_root(self) -> Path:
        return self.home / "workspace"

    @property
    def installation_state(self) -> Path:
        return self.home / "installation-state"

    @property
    def run_directory(self) -> Path:
        return self.home / "run"

    @property
    def socket_path(self) -> Path:
        return self.run_directory / "s.sock"

    @property
    def log_path(self) -> Path:
        return self.run_directory / "service.log"

    @property
    def endpoint_uri(self) -> str:
        """The endpoint this platform's service would serve and advertise.

        POSIX gets the socket path; Windows gets a named pipe, whose name has no
        filesystem component and so is derived from the installation root rather
        than from `run/`. **The Windows form is unexercised** -- no host in this
        lane can bind a named pipe -- and it is written to be correct rather than
        claimed to be tested.
        """
        if os.name == "nt":  # pragma: no cover - POSIX-only suite
            # A digest, not `hash()`: string hashing is salted per interpreter run,
            # so `hash()` would name a different pipe on every invocation.
            digest = hashlib.blake2s(str(self.home).encode("utf-8")).hexdigest()[:16]
            return f"pipe://omnivia-core-{digest}"
        return f"unix://{self.socket_path}"

    def runtime_directories(self) -> list[Path]:
        """Every advertised workspace runtime directory under this installation."""
        runtime = self.installation_state / "runtime"
        if not runtime.is_dir():
            return []
        return sorted(child for child in runtime.iterdir() if child.is_dir())

    def runtime_state(self) -> Path:
        """The one runtime directory this installation advertises.

        Zero and many are both refused rather than guessed. A default that picked
        one of several would silently talk to a different workspace than the one a
        caller has in mind, and that is worse than asking for `--runtime-state`.
        """
        found = self.runtime_directories()
        if not found:
            raise LifecycleError(
                f"no service is advertised under {self.installation_state}"
            )
        if len(found) > 1:
            names = ", ".join(path.name for path in found)
            raise LifecycleError(
                f"{self.installation_state} advertises several workspaces "
                f"({names}); name one with --runtime-state"
            )
        return found[0]


def _process_start_time(pid: int) -> str | None:
    """This host's reading of a process's start time, in the runtime's own format.

    Read the same way `omnivia_core_runtime.ownership.identity` reads it, because
    the value being compared is the one that module published: field 22 of
    `/proc/<pid>/stat` on Linux, `ps -o lstart` on macOS and the BSDs. A different
    reading of the same fact would never compare equal, which would make every
    `stop` refuse.

    Windows answers `None`: its reading is `GetProcessTimes`, and reproducing that
    here means a second copy of sixty lines of `ctypes` for a platform this lane
    cannot run on. That is a stated gap, not an oversight -- see `process_identity`.
    """
    system = platform.system()
    if system == "Linux":
        try:
            return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[21]
        except (OSError, IndexError):
            return None
    if system in ("Darwin", "FreeBSD"):
        try:
            completed = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - ps is present
            return None
        return completed.stdout.strip() or None
    return None  # pragma: no cover - Windows and anything else


def process_identity(process: ServiceProcessEvidence) -> str:
    """Whether the pid the descriptor named is still the process that published it.

    ADR-037 and `service/bootstrap.py` both say a pid alone is insufficient: pids
    are recycled, so a descriptor left behind by a dead service can name a live
    process that has nothing to do with it. The descriptor carries `start_time`
    for exactly this comparison, and a reading taken now is what it is compared
    against.

    Three answers, and the middle one is the reason this exists:

    - `IDENTITY_SAME` -- a reading was taken and equals the published one.
    - `IDENTITY_DIFFERENT` -- a reading was taken and disagrees, or this host has
      no such process at all. On a platform that answers, no reading means no
      process.
    - `IDENTITY_UNREADABLE` -- this host offers no reading. Windows only.

    **`boot_id` is not compared, and the reason is that it cannot be.** A reading
    taken here is a reading taken in *this* boot, so the only descriptor a boot
    identifier could catch is one that outlived a reboot -- and such a descriptor
    would have to carry a start-time string minted in the previous boot and have
    it coincide with a live process's, after already having answered a live call
    on its advertised endpoint. The residual gap is stated in `stop`'s own
    documentation rather than closed here.
    """
    if process.pid <= 0:
        return IDENTITY_DIFFERENT
    observed = _process_start_time(process.pid)
    if observed is not None:
        return IDENTITY_SAME if observed == process.start_time else IDENTITY_DIFFERENT
    if platform.system() in ("Linux", "Darwin", "FreeBSD"):
        # A platform that answers, answering nothing, means there is no such
        # process -- not that the question could not be asked.
        return IDENTITY_DIFFERENT
    return IDENTITY_UNREADABLE  # pragma: no cover - Windows only


def request_stop(pid: int) -> None:
    """Ask the service to unwind, by the one graceful route each platform has.

    There is no `service.shutdown` operation to call: `CANONICAL_PROBES` is health,
    readiness and discovery, and nothing on the application path stops a service.
    A signal is the whole of the graceful path, and `service/main.py` installs a
    handler for it.

    On Windows `os.kill(pid, SIGTERM)` calls `TerminateProcess`, which runs no
    handler at all -- the process dies with its descriptor still on disk claiming
    a ready service. `CTRL_BREAK_EVENT` is the one stop signal that reaches the
    handler `service/main.py` installs for `SIGBREAK`, and it is delivered to a
    process *group*, which is why `spawn_service` creates one there.
    **Unexercised on Windows**: no host in this lane runs it.
    """
    break_event = getattr(signal, "CTRL_BREAK_EVENT", None)
    if break_event is None:
        os.kill(pid, signal.SIGTERM)
        return
    os.kill(pid, break_event)  # pragma: no cover - Windows only


def request_managed_start(
    installation: Installation, *, endpoint_uri: str
) -> dict[str, Any]:
    """Ask the service package to make a service exist, and return its result.

    **This command no longer spawns the service itself, and that is the point of
    R004-08.** It used to run `omnivia-core-service` directly, holding no mutex, so
    two `omnivia start` commands running together both found nothing advertised and
    both spawned -- the loser refused by the lifetime storage lock in `runner.py`,
    which is a backstop and not a mechanism. The mutex that prevents even that lives
    in the runtime, behind the boundary this package may not cross.

    So the one shared implementation is invoked the only way ADR-036 admits:
    `omnivia-core-service --managed-start` is *located and launched*, never
    imported, and it answers with one versioned JSON document on stdout. The same
    path the MCP adapter will call. Nothing here opens a database, takes a lease or
    holds a lock; the service the launcher starts owns all three.

    The two refusals below stay on this side because they are about *this
    installation's convention* -- where the CLI decided the workspace and the socket
    live -- and answering them here names the directory a user can act on rather
    than surfacing a launcher failure class. The launcher checks the workspace
    again; a check on both sides of a subprocess boundary is the cheap one.
    """
    executable = locate_service()
    if executable is None:
        raise LifecycleError(
            f"{SERVICE_EXECUTABLE} was found neither on PATH nor beside "
            f"{Path(sys.executable).parent}; install omnivia-core-runtime"
        )
    manifest = installation.workspace_root / MANIFEST_NAME
    if not manifest.is_file():
        raise LifecycleError(
            f"no workspace manifest at {manifest}; this command starts an existing "
            "workspace and creates none -- migrate or create one first"
        )
    if os.name != "nt" and len(str(installation.socket_path).encode()) + 1 > (
        MAX_SOCKET_PATH_BYTES
    ):
        raise LifecycleError(
            f"the local endpoint path under {installation.home} is longer than the "
            f"{MAX_SOCKET_PATH_BYTES}-byte limit a local socket address allows; "
            "pass --home with a shorter directory"
        )

    installation.run_directory.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [
                executable,
                "--managed-start",
                "--workspace",
                str(installation.workspace_root),
                "--installation-state",
                str(installation.installation_state),
                "--endpoint",
                endpoint_uri,
                "--managed-start-log",
                str(installation.log_path),
            ],
            capture_output=True,
            text=True,
            # The launcher runs its own bounded wait and cleans up after itself, so
            # this budget only has to outlast it. Killing the launcher mid-spawn is
            # what would leave an orphan, which is why it is not the shorter number.
            timeout=START_TIMEOUT_SECONDS + STOP_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as expired:
        raise LifecycleError(
            f"{SERVICE_EXECUTABLE} --managed-start did not answer within "
            f"{START_TIMEOUT_SECONDS + STOP_TIMEOUT_SECONDS:.0f}s"
        ) from expired
    except OSError as failure:
        raise LifecycleError(
            f"{SERVICE_EXECUTABLE} --managed-start could not be run: {failure}"
        ) from failure

    try:
        result = json.loads(completed.stdout)
    except ValueError as malformed:
        # stdout carries protocol data and nothing else, so unparseable stdout is a
        # broken launcher rather than a failed start. Its stderr is the human half
        # and is the useful thing to show.
        raise LifecycleError(
            f"{SERVICE_EXECUTABLE} --managed-start did not answer with a result "
            f"document ({malformed}); it said: {completed.stderr.strip()}"
        ) from malformed
    if not isinstance(result, dict):
        raise LifecycleError(
            f"{SERVICE_EXECUTABLE} --managed-start answered with a "
            f"{type(result).__name__}, not a result document"
        )
    return result


def descriptor_is_gone(runtime_state: Path, deadline: float) -> bool:
    """Wait for the service to take its own descriptor away.

    That the file is gone is the service's own confirmation that the unwind
    completed: `service/main.py` releases the resource stack in reverse
    acquisition order and the descriptor's cleanup is on it. A signal delivered is
    not a service stopped, and waiting on the pid instead would say nothing about
    whether the lease and the storage lock were released.

    The file is never removed from here. Cleanup is compare-by-instance in the
    runtime precisely so a failed start cannot un-advertise a healthy owner, and a
    removal from this side would have no instance to compare.

    `read_descriptor` answers the question rather than a second `exists()` check on
    a path spelled out again here: absent and unreadable are one answer -- "nothing
    is advertised" -- and that is already this CLI's stated reading of the file.
    """
    while time.monotonic() < deadline:
        if read_descriptor(runtime_state) is None:
            return True
        time.sleep(POLL_SECONDS)
    return read_descriptor(runtime_state) is None


def process_is_gone(pid: int, deadline: float) -> bool:
    """Wait for a signalled process to actually leave.

    The descriptor going away and the process going away are two different
    events, in that order, and only the pair of them makes "stopped" true. The
    descriptor's cleanup is on the resource stack, so its removal proves the
    unwind ran -- but the interpreter is still shutting down after it, and a
    `stop` that returns there reports a process that is still on the process
    table. A caller who trusts it and starts a replacement immediately can lose
    the race for the storage lock and be refused.

    `signal.SIGCONT` rather than signal 0: both only test for existence, and
    `SIGCONT` to an already-running process is ignored. The pid was corroborated
    against the descriptor's start time before it was signalled, so this is
    waiting for a known process, not probing an arbitrary one.
    """
    while time.monotonic() < deadline:
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            return True
        except PermissionError:  # pragma: no cover - a recycled pid we may not signal
            return True
        time.sleep(POLL_SECONDS)
    try:
        os.kill(pid, signal.SIGCONT)
    except (ProcessLookupError, PermissionError):
        return True
    return False
