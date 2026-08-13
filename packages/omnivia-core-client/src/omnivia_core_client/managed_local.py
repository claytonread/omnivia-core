"""Reaching an installation's service, having one started if there is none.

One production implementation of "make this installation's service exist", owned
here because both first-party adapters need it and ADR-036 forbids either
importing the other. The convention and the launcher invocation used to exist
twice, once on each side, kept in step only by a comparison test on the runtime's
side. There is one copy now and nothing to compare.

**Nothing here is a launcher.** R004-08 put the whole of discovery, arbitration,
spawning, readiness polling and failed-child cleanup inside the service package,
behind ``--managed-start``. What is left for a caller is one argv and one bounded
JSON document, which is the whole of this module: no runtime import, no socket of
its own, no descriptor read. :class:`ServiceClient` does all three, before and
after.

**The caller's deadline is the whole budget.** :func:`connect_managed_local`
takes one :class:`~omnivia_core_client.Deadline` and uses that same object for the
first connect, the launcher's bounded wait and the reconnect, so "start a service
and then call it" costs what the caller asked for. There is no timeout argument,
because a second budget beside the one the caller stated is a budget nobody
stated.

**Nothing here creates a workspace** (R004-07, R004-10). A workspace with no
manifest is refused before any process is located or run, and no directory of the
workspace's is made on any path. The run directory -- which holds the socket the
service binds and the log it writes -- is this convention's own, and it is
created only once a start has been authorised.

**No environment variable** (R004-11). The installation root is derived from the
state root the caller already named, and from nothing ambient.

**One fixed sentence, and it is the whole diagnostic.** Every refusal below is
:data:`_MANAGED_START_FAILED` verbatim. The launcher's ``reason``, its
``failure`` class, the service descriptor it reports, the child's own output, the
executable path, the installation paths, the endpoint and every caught
exception's text are all untrusted diagnostic material, and none of them reaches
a caller -- not in the message, and not through ``__cause__`` or ``__context__``,
because the refusal is raised outside every handler that decides on one.

Standard library plus this package's own parts.
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
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn

from omnivia_core.contracts.v1 import ServiceProcessEvidence
from omnivia_core_client.deadline import Deadline
from omnivia_core_client.discovery import descriptor_path
from omnivia_core_client.errors import EndpointUnavailableError, ManagedStartError
from omnivia_core_client.service_client import InstallationServiceConfig, ServiceClient

__all__ = [
    "MANAGED_START_RESULT_MAXIMUM_BYTES",
    "MANAGED_START_VERSION",
    "SERVICE_EXECUTABLE",
    "ManagedServiceConnection",
    "StopResult",
    "connect_managed_local",
    "locate_service",
    "stop_managed_local",
]

#: The console script ``omnivia-core-runtime`` installs. Located and launched,
#: never imported: ADR-036 admits exactly that and nothing more.
SERVICE_EXECUTABLE: Final = "omnivia-core-service"

#: The launcher result document version this build reads. Exact, not a floor: a
#: document announcing anything else is one written against a contract this build
#: has not been checked against, and reading it optimistically is how a changed
#: field meaning becomes a service started on the wrong terms.
MANAGED_START_VERSION: Final = "1.0"

#: How much of the launcher's stdout is read as a result document. The document
#: is a handful of scalars; anything past this is not one, and parsing an
#: unbounded child's stdout lets the child choose this process's memory.
MANAGED_START_RESULT_MAXIMUM_BYTES: Final = 64 * 1024

#: The two statuses a successful launcher result carries, and the two
#: :class:`ManagedServiceConnection` reports. ``attached`` after an absent first
#: connect is not a contradiction: another process may have started the same
#: installation's service in between, and the launcher arbitrates that race.
_ATTACHED: Final = "attached"
_STARTED: Final = "started"

#: The workspace manifest, checked before anything is located or run so that an
#: uninitialised installation costs no process.
_MANIFEST_NAME: Final = "workspace.json"

#: The whole diagnostic. See the module docstring: everything that could make
#: this sentence more specific is material from a child process, a filesystem
#: path or a caught exception, and none of the three may cross this boundary.
_MANAGED_START_FAILED: Final = "the managed service could not be started"

_POLL_SECONDS: Final = 0.05


@dataclass(frozen=True, slots=True)
class StopResult:
    """The closed result of one administrative stop attempt."""

    status: str


@dataclass(frozen=True, slots=True)
class ManagedServiceConnection:
    """A live service for one workspace, and how this process came to have it.

    Built only by :func:`connect_managed_local`, which is what makes the two
    fields mean something together: ``client`` was connected *after* ``status``
    was decided, so ``started`` is never a claim about a service nothing has
    since reached.
    """

    client: ServiceClient
    """The connected client, exactly as :meth:`ServiceClient.connect` built it."""

    status: str
    """``"attached"`` if a compatible service was already serving this
    installation, ``"started"`` if the launcher started one."""


@dataclass(frozen=True, slots=True)
class _Installation:
    """One installation root and the paths the fixed convention derives from it.

    The single copy of the layout. Private, because a caller states an
    installation state root and this derives the rest: publishing it would
    re-open the possibility of a second copy being written against it.
    """

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
    def log_path(self) -> Path:
        return self.run_directory / "service.log"

    @property
    def manifest_path(self) -> Path:
        return self.workspace_root / _MANIFEST_NAME

    @property
    def endpoint_uri(self) -> str:
        """The endpoint this platform's service would serve and advertise.

        POSIX gets the socket under the run directory; Windows gets a named pipe,
        whose name has no filesystem component and so is derived from the
        installation root. The Windows form is unexercised -- no host in this lane
        can bind a named pipe -- and is written to be correct rather than claimed
        to be tested.
        """
        if os.name == "nt":  # pragma: no cover - POSIX-only suite
            # A digest, not `hash()`: string hashing is salted per interpreter
            # run, so `hash()` would name a different pipe every invocation.
            digest = hashlib.blake2s(str(self.home).encode("utf-8")).hexdigest()[:16]
            return f"pipe://omnivia-core-{digest}"
        return f"unix://{self.run_directory / 's.sock'}"


def connect_managed_local(
    config: InstallationServiceConfig, *, deadline: Deadline
) -> ManagedServiceConnection:
    """Connect to this installation's service, having one started if there is none.

    Attaching is the ordinary case and costs one connect: an installation already
    publishing a live compatible descriptor comes back as ``attached`` and nothing
    is launched. An absent descriptor and a provenance-checked descriptor whose
    endpoint cannot answer both reach the launcher. The latter is the crash path:
    the launcher owns process-evidence validation and compare-and-clean under its
    startup mutex, so it may retire that dead instance safely. Every other descriptor
    refusal still fails closed here.

    **A start is authorised by the layout, not by the request for one.** The state
    root the caller named must be the ``installation-state`` this convention
    derives from its own parent, and that parent must hold an initialised
    workspace. A configuration pointing at a bare state directory somewhere else
    is refused rather than answered with a service started against a root nobody
    sanctioned, and an uninitialised installation is refused rather than
    bootstrapped.

    Then exactly one launcher invocation, one reconnect on the same ``deadline``,
    and a live client required: a launcher reporting success while nothing is
    reachable is a failure here, because what was asked for is a service that can
    be called rather than a process that exists.
    """
    try:
        attached = ServiceClient.connect(config, deadline=deadline)
    except EndpointUnavailableError:
        # A killed service cannot remove its descriptor. Do not delete it here:
        # the Runtime launcher validates process evidence and cleans only the dead
        # instance under the installation's startup mutex.
        attached = None
    if attached is not None:
        return ManagedServiceConnection(client=attached, status=_ATTACHED)

    installation = _Installation(home=config.installation_state.parent)
    if installation.installation_state != config.installation_state:
        _refuse()
    if not installation.manifest_path.is_file():
        _refuse()
    executable = locate_service()
    if executable is None:
        _refuse()

    # The run directory only: it holds the socket the service will bind and the
    # log it will write, both of which are this convention's business rather than
    # the workspace's. No workspace state is created by this line.
    could_not_prepare = False
    try:
        installation.run_directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        could_not_prepare = True
    if could_not_prepare:
        _refuse()
    status = _status(_invoke(executable, installation, deadline))

    started = ServiceClient.connect(config, deadline=deadline)
    if started is None:
        _refuse()
    return ManagedServiceConnection(client=started, status=status)


def _process_start_time(pid: int) -> str | None:
    """Read a process start time in the format Runtime publishes."""
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
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            return None
        return completed.stdout.strip() or None
    return None  # pragma: no cover - Windows and other hosts


def _same_process(process: ServiceProcessEvidence) -> bool | None:
    """True for a matching process, false for a mismatch, None if unreadable."""
    if process.pid <= 0:
        return False
    observed = _process_start_time(process.pid)
    if observed is not None:
        return observed == process.start_time
    if platform.system() in ("Linux", "Darwin", "FreeBSD"):
        return False
    return None  # pragma: no cover - Windows only


def _request_stop(pid: int) -> None:
    """Use the graceful signal Runtime installs on this platform."""
    break_event = getattr(signal, "CTRL_BREAK_EVENT", None)
    os.kill(pid, signal.SIGTERM if break_event is None else break_event)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _pause(deadline: Deadline) -> None:
    time.sleep(min(_POLL_SECONDS, deadline.remaining_seconds()))


def stop_managed_local(
    config: InstallationServiceConfig, *, deadline: Deadline
) -> StopResult:
    """Stop ``config`` only after its live service corroborates process identity.

    A stale descriptor never authorises a signal. Success requires the service
    to withdraw its own descriptor and the corroborated process to exit within
    the caller's original deadline.
    """
    try:
        client = ServiceClient.connect(config, deadline=deadline)
    except EndpointUnavailableError:
        return StopResult("unreachable")
    if client is None:
        return StopResult("not_running")

    process = client.descriptor.process
    if process is None:
        return StopResult("no_process")
    if _same_process(process) is False:
        return StopResult("identity_mismatch")

    try:
        _request_stop(process.pid)
    except (OSError, ValueError):
        return StopResult("identity_mismatch")

    advertised = descriptor_path(config.installation_state, config.workspace_id)
    while advertised.exists() and not deadline.expired:
        _pause(deadline)
    if advertised.exists():
        return StopResult("timeout")

    while _process_exists(process.pid) and not deadline.expired:
        _pause(deadline)
    if _process_exists(process.pid):
        return StopResult("process_lingering")
    return StopResult("stopped")


def _refuse() -> NoReturn:
    """Raise the one sentence this module has.

    Called from outside every ``except`` block that decides on a refusal, never
    inside, so ``__context__`` is genuinely ``None`` and a caught exception's own
    words cannot be recovered from the error a caller holds. Same shape and same
    reason as ``discovery.py``'s and ``service_client.py``'s ``_raise_*``
    helpers, and ``scripts/check-raise-discipline.py`` is what keeps it that way.
    """
    raise ManagedStartError(_MANAGED_START_FAILED)


def locate_service() -> str | None:
    """The service console script: on ``PATH``, or failing that beside this one.

    ``PATH`` alone is not enough. An MCP host spawns its servers with whatever
    environment it happens to hold, and a virtual environment's ``bin`` is on
    ``PATH`` only for a shell that activated it. The two scripts are installed
    side by side by the same installer, so ``sys.executable``'s directory is not
    a guess. ``PATH`` still wins, so a deliberately shadowed build is honoured.
    """
    found = shutil.which(SERVICE_EXECUTABLE)
    if found is not None:
        return found
    beside = Path(sys.executable).parent / SERVICE_EXECUTABLE
    return str(beside) if beside.is_file() and os.access(beside, os.X_OK) else None


def _invoke(executable: str, installation: _Installation, deadline: Deadline) -> str:
    """Run ``omnivia-core-service --managed-start`` and return its stdout.

    The result stream is redirected to a temporary file and stderr is discarded,
    and that is load-bearing rather than convenient: the child writes its result
    document to *its* stdout and its human log to *its* stderr, while this
    process's stdout may be an MCP protocol stream. A pipe would keep the stream
    away from MCP too, but ``subprocess.run`` would buffer it without a ceiling;
    this process reads only the admitted result size plus one byte.

    Bounded by what is left of the caller's budget, so a launcher that never
    answers costs the deadline rather than a second timeout nobody asked for.
    """
    completed: subprocess.CompletedProcess[bytes] | None = None
    stdout = b""
    try:
        with tempfile.TemporaryFile() as captured:
            completed = subprocess.run(
                [
                    executable,
                    "--managed-start",
                    "--workspace",
                    str(installation.workspace_root),
                    "--installation-state",
                    str(installation.installation_state),
                    "--endpoint",
                    installation.endpoint_uri,
                    "--managed-start-log",
                    str(installation.log_path),
                ],
                stdout=captured,
                stderr=subprocess.DEVNULL,
                timeout=deadline.remaining_seconds(),
                check=False,
            )
            captured.seek(0)
            stdout = captured.read(MANAGED_START_RESULT_MAXIMUM_BYTES + 1)
    except (OSError, subprocess.SubprocessError):
        # Every way the child fails to be a child: it could not be executed, or
        # it did not answer inside the budget. Both are the one refusal, and the
        # exception itself is dropped here rather than chained -- its text names
        # the executable and, for a timeout, quotes the argv.
        completed = None
    if completed is None:
        _refuse()
    if completed.returncode != 0:
        _refuse()
    if len(stdout) > MANAGED_START_RESULT_MAXIMUM_BYTES:
        _refuse()
    decoded: str | None = None
    try:
        decoded = stdout.decode("utf-8")
    except UnicodeDecodeError:
        decoded = None
    if decoded is None:
        _refuse()
    return decoded


def _status(stdout: str) -> str:
    """The launcher's answer as ``attached`` or ``started``, or a refusal.

    Fails closed on every reading: output past the bound, output that is not
    JSON, a root that is not an object, a version this build was not written
    against, a reported failure, or a status outside the two. Only
    ``managed_start_version``, ``status`` and nothing else is read -- the service
    descriptor beside them is deliberately ignored, because the reconnect that
    follows proves liveness against what the *installation* published rather than
    against a claim the child made about itself.
    """
    if len(stdout.encode("utf-8")) > MANAGED_START_RESULT_MAXIMUM_BYTES:
        _refuse()
    document: Any = None
    malformed = False
    try:
        document = json.loads(stdout)
    except (ValueError, RecursionError):
        malformed = True
    if malformed or not isinstance(document, dict):
        _refuse()
    if document.get("managed_start_version") != MANAGED_START_VERSION:
        _refuse()
    status = document.get("status")
    if status == _ATTACHED:
        return _ATTACHED
    if status != _STARTED:
        _refuse()
    return _STARTED
