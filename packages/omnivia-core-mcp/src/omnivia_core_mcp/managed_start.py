"""Making a Core service exist, from the MCP side (R004-07).

**This is an adapter over the service-owned path, not a second launcher.**
R004-08 rejected duplicated launch logic and put the one implementation in
`omnivia-core-service`; what it left each adapter is "one argv and one JSON
document", which is the whole of this module. Nothing here discovers, arbitrates,
spawns, polls for readiness or cleans up a failed child -- the launcher does all
six behind the subprocess boundary, and this reads its answer.

**The path convention is restated, and that is the honest cost.** ADR-036 forbids
this package importing `omnivia_core_cli`, so the `~/.omnivia` layout below is a
second copy of `omnivia_core_cli.lifecycle`'s. A restated constant is only safe
while something fails when the two drift -- the CLI restated the socket ceiling
and drifted 11 to 15 bytes from the runtime, which Packet D fixed -- so
`packages/omnivia-core-runtime/tests/phase2/test_service_and_adapters.py`
compares the two conventions field by field, from the runtime side that is
allowed to see both.

**The ceiling is deliberately not restated a third time.** The CLI checks the
86-byte local endpoint limit before spawning to produce a better message; this
does not, because that number has already drifted once between two copies and a
third would be the next place it drifts. An over-long endpoint reaches the
launcher, fails to bind, and comes back as `spawn_failure` carrying the child's
own diagnostic -- a worse sentence from a single source of truth.

**No environment variable.** R004-11: the home is the fixed convention or an
explicit argument, and nothing else. An environment lookup is an unrestricted
filesystem path arriving under another name.

**Nothing here creates a workspace.** R004-07 and R004-10: a missing workspace is
refused with an instruction to run `omnivia init`, and no directory is made.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

__all__ = [
    "DEFAULT_HOME_DIRECTORY",
    "MANAGED_START_TIMEOUT_SECONDS",
    "MANIFEST_NAME",
    "SERVICE_EXECUTABLE",
    "Attachment",
    "Installation",
    "ManagedStartError",
    "ensure_service",
    "home_directory",
    "locate_service",
]

#: The default installation root under the user's home directory (R004-11).
DEFAULT_HOME_DIRECTORY: Final = ".omnivia"

#: The console script `omnivia-core-runtime` installs. Located and launched,
#: never imported: ADR-036 admits exactly that and nothing more.
SERVICE_EXECUTABLE: Final = "omnivia-core-service"

#: The workspace manifest filename, checked before spawning so "no workspace" is
#: a sentence naming a directory rather than a launcher failure class.
MANIFEST_NAME: Final = "workspace.json"

#: How long the launcher is given to answer. It runs its own bounded wait and
#: cleans up after itself, so this only has to outlast it: killing it mid-spawn
#: is what would leave an orphan holding the storage lock.
MANAGED_START_TIMEOUT_SECONDS: Final = 120.0

#: What to tell a human for each of the launcher's five closed failure classes.
#: Keyed by the exact strings `ManagedStartFailure` defines; an unrecognised class
#: falls back to the launcher's own `reason`, so a sixth one added upstream is
#: reported rather than swallowed.
_FAILURE_ADVICE: Final[dict[str, str]] = {
    "missing_workspace": (
        "run `omnivia init` to create one, then start this server again"
    ),
    "incompatible_service": (
        "a service is already serving that workspace and this build cannot talk to "
        "it; stop it with `omnivia stop` and start a matching build"
    ),
    "timeout": "no service became usable in time; check the service log named above",
    "spawn_failure": f"{SERVICE_EXECUTABLE} could not be started or exited early",
    "readiness_failure": (
        "the service started but never became ready; the process it started was "
        "stopped again"
    ),
}


class ManagedStartError(Exception):
    """A refusal whose message is meant for a human, not a traceback.

    Every one of these is raised before the stdio session opens, so it reaches the
    host as a startup failure on stderr rather than as anything on the protocol
    stream. R004-07's "protocol-safe diagnostic" is satisfied by never having
    written a byte of protocol.
    """


@dataclass(frozen=True)
class Installation:
    """One installation root and the paths the fixed convention derives from it.

    Field for field the same layout `omnivia_core_cli.lifecycle.Installation`
    derives, because it has to be: a CLI-started service and an MCP-started one
    must land on one workspace and one socket or they are two services.
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
    def socket_path(self) -> Path:
        return self.run_directory / "s.sock"

    @property
    def log_path(self) -> Path:
        return self.run_directory / "service.log"

    @property
    def manifest_path(self) -> Path:
        return self.workspace_root / MANIFEST_NAME

    @property
    def endpoint_uri(self) -> str:
        """The endpoint this platform's service would serve and advertise.

        POSIX gets the socket path; Windows gets a named pipe, whose name has no
        filesystem component and so is derived from the installation root. The
        Windows form is unexercised -- no host in this lane can bind a named pipe
        -- and is written to be correct rather than claimed to be tested.
        """
        if os.name == "nt":  # pragma: no cover - POSIX-only suite
            # A digest, not `hash()`: string hashing is salted per interpreter
            # run, so `hash()` would name a different pipe every invocation.
            digest = hashlib.blake2s(str(self.home).encode("utf-8")).hexdigest()[:16]
            return f"pipe://omnivia-core-{digest}"
        return f"unix://{self.socket_path}"


@dataclass(frozen=True)
class Attachment:
    """The service this server is attached to, and how it got there.

    `status` is the launcher's own `attached` or `started`; a failed launch never
    becomes one of these, it raises. `endpoint_uri` is what a concrete
    `ClientTransport` will be constructed against once one exists -- see
    :mod:`omnivia_core_mcp.server`.
    """

    status: str
    endpoint_uri: str
    workspace_id: str


def home_directory(override: Path | None = None) -> Path:
    """The installation root: an explicit argument, otherwise the one convention.

    Deliberately not an environment variable (R004-11). A fixed convention is
    bounded and identical on every machine; an ambient path is not, and an MCP
    host's environment is the last place a filesystem root should come from.
    """
    if override is not None:
        return override.expanduser()
    return Path.home() / DEFAULT_HOME_DIRECTORY


def locate_service() -> str | None:
    """The service console script: on `PATH`, or failing that beside this one.

    `PATH` alone is not enough here. An MCP host spawns this server with whatever
    environment it happens to hold, and a virtual environment's `bin` is on `PATH`
    only for a shell that activated it. The two scripts are installed side by side
    by the same installer, so `sys.executable`'s directory is not a guess. `PATH`
    still wins, so a deliberately shadowed build is honoured.
    """
    found = shutil.which(SERVICE_EXECUTABLE)
    if found is not None:
        return found
    beside = Path(sys.executable).parent / SERVICE_EXECUTABLE
    return str(beside) if beside.is_file() and os.access(beside, os.X_OK) else None


def ensure_service(
    installation: Installation,
    *,
    timeout_seconds: float = MANAGED_START_TIMEOUT_SECONDS,
) -> Attachment:
    """Attach to a compatible service, or have exactly one started (R004-07).

    Steps 3 to 6 of R004-07 are all the launcher's; this contributes step 1 -- the
    resolved home -- and step 2, the invocation. It waits for the launcher, which
    itself waits for live readiness, so returning normally means a service is ready
    now and tools may be advertised.

    The two refusals raised before the subprocess are about *this installation's
    convention*, and answering them here names the directory a human can act on
    instead of surfacing a launcher failure class from behind a boundary. The
    launcher re-checks the workspace anyway; a check on both sides of a subprocess
    boundary is the cheap one.
    """
    executable = locate_service()
    if executable is None:
        raise ManagedStartError(
            f"{SERVICE_EXECUTABLE} was found neither on PATH nor beside "
            f"{Path(sys.executable).parent}; install omnivia-core-runtime"
        )
    if not installation.manifest_path.is_file():
        # R004-07: no initialised workspace is a refusal with an instruction, and
        # nothing is created. Note what is *not* here -- no mkdir of the workspace
        # root, no manifest written, no bootstrap call. R004-10 leaves that to
        # `omnivia init` and forbids MCP calling it automatically in v0.6.
        raise ManagedStartError(
            f"no OmniVia workspace at {installation.workspace_root}: "
            f"{installation.manifest_path} does not exist. "
            "Run `omnivia init` to create one, then start this server again. "
            "This server starts an existing workspace and creates none."
        )

    # The run directory only: it holds the socket the service will bind and the log
    # it will write, both of which are this convention's business rather than the
    # workspace's. No workspace state is created by this line.
    installation.run_directory.mkdir(parents=True, exist_ok=True)
    completed = _invoke(
        executable, installation=installation, timeout_seconds=timeout_seconds
    )
    return _attachment(_result(completed), installation=installation)


def _invoke(
    executable: str,
    *,
    installation: Installation,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    """Run `omnivia-core-service --managed-start` and capture both its streams.

    `capture_output=True` is load-bearing rather than convenient: the child writes
    its JSON result to *its* stdout and its human log to *its* stderr, and this
    process's own stdout is the MCP protocol stream. Capturing both is what keeps
    R004-07's "child-process output must never corrupt the stdio stream" true even
    when this runs before the SDK's transport has claimed the descriptors.
    """
    try:
        return subprocess.run(
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
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as expired:
        raise ManagedStartError(
            f"{SERVICE_EXECUTABLE} --managed-start did not answer within "
            f"{timeout_seconds:.0f}s"
        ) from expired
    except OSError as failure:
        raise ManagedStartError(
            f"{SERVICE_EXECUTABLE} --managed-start could not be run: {failure}"
        ) from failure


def _result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """The launcher's versioned JSON document, or a refusal naming what came back.

    Unparseable stdout is a broken launcher rather than a failed start -- its
    contract is that stdout carries protocol data and nothing else -- so its stderr
    is the half worth showing a human.
    """
    try:
        document = json.loads(completed.stdout)
    except ValueError as malformed:
        raise ManagedStartError(
            f"{SERVICE_EXECUTABLE} --managed-start did not answer with a result "
            f"document (exit {completed.returncode}): "
            f"{completed.stderr.strip() or completed.stdout.strip()!r}"
        ) from malformed
    if not isinstance(document, dict):
        raise ManagedStartError(
            f"{SERVICE_EXECUTABLE} --managed-start answered {type(document).__name__}, "
            "not a result object"
        )
    return document


def _attachment(
    document: dict[str, Any], *, installation: Installation
) -> Attachment:
    """One launcher result, as either an attachment or a raised refusal."""
    status = document.get("status")
    if status == "failed":
        failure = str(document.get("failure") or "unknown")
        reason = str(document.get("reason") or "no reason given")
        advice = _FAILURE_ADVICE.get(failure)
        message = f"could not make a Core service ready ({failure}): {reason}"
        if advice is not None:
            message = f"{message}. {advice}"
        child = str(document.get("child_output") or "").strip()
        if child:
            message = f"{message}\n--- {SERVICE_EXECUTABLE} output ---\n{child}"
        raise ManagedStartError(message)

    if status not in ("attached", "started"):
        raise ManagedStartError(
            f"{SERVICE_EXECUTABLE} --managed-start answered an unknown status "
            f"{status!r}"
        )

    service = document.get("service")
    if not isinstance(service, dict):
        raise ManagedStartError(
            f"{SERVICE_EXECUTABLE} --managed-start reported {status} without a "
            "service descriptor"
        )
    endpoint_uri = service.get("endpoint_uri")
    workspace_id = service.get("workspace_id")
    if not isinstance(endpoint_uri, str) or not isinstance(workspace_id, str):
        raise ManagedStartError(
            f"{SERVICE_EXECUTABLE} --managed-start reported {status} without a "
            f"usable endpoint for {installation.home}"
        )
    return Attachment(
        status=status, endpoint_uri=endpoint_uri, workspace_id=workspace_id
    )
