"""Service-owned managed start (R004-08, owner resolution 004 Packet A).

The one shared implementation of "make a service exist for this workspace, and
tell me where it is". The CLI invokes it as a subprocess and the MCP adapter will
do the same; neither imports this module, and neither carries a second copy of
process control. ADR-036 admits exactly that division -- locate or launch the
service executable, communicate only through the application API -- so the
adapter's whole dependency on this path is one argv and one JSON document.

**Where the line falls between this and `coordinated_startup`.** R004-08 lists
eight responsibilities. `service/bootstrap.py` already owns the first four --
discover, verify compatibility, arbitrate through the bootstrap mutex, recheck
after acquiring it -- and it is reused rather than reimplemented. The remaining
four are here:

    5. spawn the independent service when required;
    6. wait for live readiness, not descriptor publication alone;
    7. return the usable connection descriptor to the invoking adapter;
    8. clean up a failed child, failed descriptor, and owned transient state.

Steps 5 to 7 run **inside** the `with coordinated_startup(...)` block, and that
placement is the whole point of the mutex rather than a detail of it. That
function's own docstring states the contract: a `SPAWN` decision "is only true
while this launcher still holds the mutex", and spawn-and-wait-for-readiness
"happens entirely in the caller". A launcher that left the block before its child
was ready would be telling the next launcher nothing, and both would spawn.

**`REFUSED_MUTEX_UNAVAILABLE` is not an error and is not returned as one.**
Another launcher is mid-spawn; the right answer is to wait and ask again, which is
what turns "several concurrent starters" into "one authoritative service, reported
identically to all of them" rather than into one winner and several failures.

**Live readiness, not the descriptor.** `publish()` has a single call site and is
never rewritten, so `ready: true` freezes at startup and a service that was killed
leaves a file still claiming health. Waiting for the file to appear is therefore
not waiting for readiness. This dials `core.readiness` on the advertised endpoint
and believes the answer the running service's own lifecycle object gives, which is
the same thing `omnivia start`'s `status` does and for the same reason.

**This creates no workspace.** An unbootstrapped directory is refused with
`missing_workspace` and nothing is written. `omnivia init` is Packet B.

**No auto-start, no daemon, no persistence.** On-demand start only: this runs when
an adapter asks, spawns one detached child and exits. D-0018 and R004-17.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ClientIdentity,
    RequestEnvelope,
    RequestMetadata,
    ServiceEndpointDescriptor,
    SuccessResponseEnvelope,
)
from omnivia_core_runtime.ownership.discovery import descriptor_path, discover
from omnivia_core_runtime.service.bootstrap import (
    StartupOutcome,
    coordinated_startup,
)
from omnivia_core_runtime.service.transport import (
    LOCAL_SCHEME,
    LocalSocketTransport,
    TransportError,
    parse_endpoint,
)
from omnivia_core_runtime.service.versions import (
    API_VERSION,
    SERVER_VERSION,
    WorkspaceVersionError,
    workspace_contract_version,
)
from omnivia_core_runtime.storage.backup import InstallationLayout
from omnivia_core_runtime.workspace.layout import WorkspaceLayout
from omnivia_core_runtime.workspace.manifest_store import (
    ManifestStoreError,
    read_manifest,
)

#: The console script this spawns. The same name the CLI locates, because it is the
#: same executable: a managed start runs *as* `omnivia-core-service` and starts
#: another one.
SERVICE_EXECUTABLE: Final = "omnivia-core-service"

#: Version of the machine-readable result document below. Bumped when a consumer
#: would have to change to keep reading it; additive fields do not bump it.
MANAGED_START_VERSION: Final = "1.0"

#: Whole-operation budget, covering the mutex wait, the spawn and the readiness
#: wait. A startup budget rather than a call budget: the started service runs
#: migrations and recovery before it answers.
MANAGED_START_TIMEOUT_SECONDS: Final = 90.0

#: Gap between polls, for the mutex retry and the readiness wait alike.
POLL_SECONDS: Final = 0.1

#: How long a failed child is given to unwind before it is killed.
CHILD_STOP_TIMEOUT_SECONDS: Final = 15.0

#: Budget for one `core.readiness` call. A local call that has not answered in this
#: long is not about to.
READINESS_CALL_TIMEOUT_SECONDS: Final = 5.0

#: The service-lifecycle operation that answers from the running service's live
#: lifecycle object rather than from the frozen descriptor.
READINESS_OPERATION: Final = "core.readiness"

#: This caller's self-declared identity. Diagnostic only, never an authorization
#: input, and distinct from the CLI's so a service log can tell them apart.
CLIENT_NAME: Final = "omnivia-core-managed-start"

#: The purpose claimed on the readiness call. The shape requires one; the probe path
#: does not read it, and a claim is not authority in any case.
READINESS_PURPOSE: Final = "managed_start"

#: Tail of the failed child's own output carried back in the result, so an adapter
#: can put the readiness precondition that went unmet in front of a human without
#: reading a log file it does not know the path of.
MAX_CHILD_OUTPUT_BYTES: Final = 8192


class ManagedStartStatus(str, Enum):
    """Whether a service was found, made, or neither."""

    ATTACHED = "attached"
    STARTED = "started"
    FAILED = "failed"


class ManagedStartFailure(str, Enum):
    """The deterministic failure classes R004-08 requires.

    Closed on purpose. An adapter branches on these, so a sixth class appearing
    unannounced is a breaking change to the result contract, not a detail.
    """

    MISSING_WORKSPACE = "missing_workspace"
    INCOMPATIBLE_SERVICE = "incompatible_service"
    TIMEOUT = "timeout"
    SPAWN_FAILURE = "spawn_failure"
    READINESS_FAILURE = "readiness_failure"


@dataclass(frozen=True)
class ManagedStartResult:
    """What the invoking adapter receives, and the whole of it.

    `status` answers R004-08's "attached to an existing service or started a new
    one". `failure` is set on exactly the failed results and is one of five closed
    names. `service` is the usable connection descriptor -- step 7 -- merged with
    the live readiness answer, so a caller that renders a status line needs no
    second dial of its own.
    """

    status: ManagedStartStatus
    reason: str
    failure: ManagedStartFailure | None = None
    descriptor: ServiceEndpointDescriptor | None = None
    readiness: Mapping[str, Any] | None = None
    child_output: str = ""

    def to_dict(self) -> dict[str, Any]:
        """The versioned machine-readable document, and nothing human in it.

        Protocol data only. Every human word this path produces goes to stderr;
        `reason` is the one exception and it is a stable sentence about the result,
        not a log line.
        """
        document: dict[str, Any] = {
            "managed_start_version": MANAGED_START_VERSION,
            "status": self.status.value,
            "failure": None if self.failure is None else self.failure.value,
            "reason": self.reason,
            "service": self._service(),
        }
        if self.child_output:
            document["child_output"] = self.child_output
        return document

    def _service(self) -> dict[str, Any] | None:
        descriptor = self.descriptor
        if descriptor is None:
            return None
        answer = dict(self.readiness or {})
        return {
            "endpoint_uri": descriptor.endpoint_uri,
            "workspace_id": descriptor.workspace_id,
            "service_instance_id": descriptor.service_instance_id,
            "fencing_generation": descriptor.fencing_generation,
            "pid": None if descriptor.process is None else descriptor.process.pid,
            # From the live answer, never from the descriptor's own frozen fields.
            "state": answer.get("state"),
            "ready": answer.get("ready"),
            "unmet": list(answer.get("unmet") or []),
        }


def managed_start(
    *,
    workspace_root: Path,
    installation_root: Path,
    endpoint_uri: str,
    core_version: str = "0.1.0",
    log_path: Path | None = None,
    timeout_seconds: float = MANAGED_START_TIMEOUT_SECONDS,
) -> ManagedStartResult:
    """Converge on one authoritative writable service for this workspace.

    `log_path` is where the started child's own output goes, and it defaults to the
    runtime directory this path already owns. The CLI passes its existing
    `<home>/run/service.log` so its convention is unchanged; an adapter with no
    such convention can leave it alone.
    """
    layout = WorkspaceLayout(root=workspace_root)
    try:
        manifest = read_manifest(layout)
    except ManifestStoreError as refusal:
        # Nothing is created here. `omnivia init` is a separate authorised command
        # and this one starts an existing workspace only.
        return ManagedStartResult(
            status=ManagedStartStatus.FAILED,
            failure=ManagedStartFailure.MISSING_WORKSPACE,
            reason=(
                f"no usable workspace at {workspace_root} ({refusal}); this command "
                "starts an existing workspace and creates none"
            ),
        )

    try:
        contract_version = workspace_contract_version(
            manifest.compatibility.workspace_format_version
        )
    except WorkspaceVersionError as refusal:
        return ManagedStartResult(
            status=ManagedStartStatus.FAILED,
            failure=ManagedStartFailure.INCOMPATIBLE_SERVICE,
            reason=f"this build cannot serve {layout.manifest_path}: {refusal}",
        )

    runtime_directory = InstallationLayout(root=installation_root).runtime_for(
        manifest.workspace_id
    )
    deadline = time.monotonic() + timeout_seconds

    while True:
        with coordinated_startup(
            runtime_directory,
            api_version=API_VERSION,
            workspace_contract_version=contract_version,
        ) as decision:
            if decision.outcome is StartupOutcome.REFUSED_INCOMPATIBLE:
                # Not replaced, not stopped, not raced. An incompatible service is
                # somebody's authoritative owner of this workspace.
                return ManagedStartResult(
                    status=ManagedStartStatus.FAILED,
                    failure=ManagedStartFailure.INCOMPATIBLE_SERVICE,
                    reason=decision.reason,
                    descriptor=decision.existing,
                )

            if decision.outcome in (
                StartupOutcome.USE_EXISTING,
                StartupOutcome.ANOTHER_LAUNCHER_WON,
            ):
                existing = decision.existing
                assert existing is not None  # both outcomes carry one
                answer = _dial_readiness(existing)
                if answer is not None and answer.get("ready"):
                    return ManagedStartResult(
                        status=ManagedStartStatus.ATTACHED,
                        reason=decision.reason,
                        descriptor=existing,
                        readiness=answer,
                    )
                # Advertised and answering the connect, but not writable-ready: it
                # is still coming up, or going down. Neither is this launcher's to
                # resolve, so ask again until the budget runs out.

            elif decision.outcome is StartupOutcome.SPAWN:
                # Steps 5 to 7, inside the mutex. Leaving the block first would
                # release it before the child is ready, which is the exact window
                # holding it is for.
                return _spawn_and_wait(
                    workspace_root=workspace_root,
                    installation_root=installation_root,
                    runtime_directory=runtime_directory,
                    endpoint_uri=endpoint_uri,
                    core_version=core_version,
                    log_path=(
                        runtime_directory / "service.log"
                        if log_path is None
                        else log_path
                    ),
                    deadline=deadline,
                )

            # REFUSED_MUTEX_UNAVAILABLE falls through: another launcher holds the
            # mutex and is spawning, which R004-09 records as not an error. Waiting
            # is what makes every concurrent starter converge on that one service
            # rather than fail beside it.

        if time.monotonic() >= deadline:
            return ManagedStartResult(
                status=ManagedStartStatus.FAILED,
                failure=ManagedStartFailure.TIMEOUT,
                reason=(
                    f"no service became usable for {manifest.workspace_id} within "
                    f"{timeout_seconds:.0f}s"
                ),
            )
        time.sleep(POLL_SECONDS)


def _spawn_and_wait(
    *,
    workspace_root: Path,
    installation_root: Path,
    runtime_directory: Path,
    endpoint_uri: str,
    core_version: str,
    log_path: Path,
    deadline: float,
) -> ManagedStartResult:
    """Start the independent service and wait until it actually answers.

    Four things are waited on, and each is here because waiting on fewer reports
    something that is not true: the child not having exited, a descriptor appearing,
    that descriptor answering a live `core.readiness`, and that answer saying
    writable. `Popen` returning proves only that a fork happened.
    """
    executable = _service_executable()
    if executable is None:
        return ManagedStartResult(
            status=ManagedStartStatus.FAILED,
            failure=ManagedStartFailure.SPAWN_FAILURE,
            reason=(
                f"{SERVICE_EXECUTABLE} was found neither on PATH nor beside "
                f"{Path(sys.executable).parent}; install omnivia-core-runtime"
            ),
        )

    try:
        child = _spawn(
            executable,
            workspace_root=workspace_root,
            installation_root=installation_root,
            endpoint_uri=endpoint_uri,
            core_version=core_version,
            log_path=log_path,
        )
    except OSError as failure:
        return ManagedStartResult(
            status=ManagedStartStatus.FAILED,
            failure=ManagedStartFailure.SPAWN_FAILURE,
            reason=f"{SERVICE_EXECUTABLE} could not be started: {failure}",
        )

    while time.monotonic() < deadline:
        exited = child.poll()
        if exited is not None:
            # An exited child cleans up after itself on every path but a kill, and
            # `compare_and_clean` is compare-by-instance so a descriptor it did
            # leave is still removed only if it is that child's own.
            _clean_child_descriptor(runtime_directory, child.pid)
            return ManagedStartResult(
                status=ManagedStartStatus.FAILED,
                failure=ManagedStartFailure.SPAWN_FAILURE,
                reason=(
                    f"{SERVICE_EXECUTABLE} exited with status {exited} before it "
                    "was ready"
                ),
                child_output=_child_output(log_path),
            )
        advertised = discover(runtime_directory)
        if advertised is not None:
            answer = _dial_readiness(advertised)
            if answer is not None and answer.get("ready"):
                return ManagedStartResult(
                    status=ManagedStartStatus.STARTED,
                    reason=f"started {SERVICE_EXECUTABLE} (pid {child.pid})",
                    descriptor=advertised,
                    readiness=answer,
                )
        time.sleep(POLL_SECONDS)

    # Step 8. Nothing else will clean this up: the child was spawned here and never
    # became ready, so leaving it would leave a process holding the storage lock
    # that no `stop` can find, and possibly a descriptor claiming it is healthy.
    _stop_child(child)
    _clean_child_descriptor(runtime_directory, child.pid)
    return ManagedStartResult(
        status=ManagedStartStatus.FAILED,
        failure=ManagedStartFailure.READINESS_FAILURE,
        reason=(
            f"{SERVICE_EXECUTABLE} did not become ready in time; the process this "
            "command started was stopped"
        ),
        child_output=_child_output(log_path),
    )


def _spawn(
    executable: str,
    *,
    workspace_root: Path,
    installation_root: Path,
    endpoint_uri: str,
    core_version: str,
    log_path: Path,
) -> subprocess.Popen[bytes]:
    """Start the service as a detached child, logging to `log_path`.

    Detached because the started service outlives this launcher: on POSIX
    `start_new_session=True` puts it in its own session, so a terminal's `SIGINT`
    cannot reach it, and on Windows a new process group is what makes
    `CTRL_BREAK_EVENT` deliverable later.

    Output goes to a file rather than a pipe. A pipe whose read end dies when this
    launcher exits leaves the service writing into a closed descriptor, and the file
    is also what a failed start reads back: `service/main.py` prints its
    `StartupReport`, and `unmet` names the readiness precondition that was not met.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Truncated per start, so what the file holds is this run's own output and a
    # failure diagnostic cannot be read off a previous attempt.
    log = log_path.open("wb")
    try:
        return subprocess.Popen(
            [
                executable,
                "--workspace",
                str(workspace_root),
                "--installation-state",
                str(installation_root),
                "--endpoint",
                endpoint_uri,
                "--core-version",
                core_version,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    finally:
        log.close()


def _service_executable() -> str | None:
    """The service console script: this process's own, on `PATH`, or beside it.

    `sys.argv[0]` first, because a managed start is itself an invocation of the
    script it is about to spawn -- so the running process already is the answer,
    and honouring it keeps a deliberately shadowed or relocated build in play. The
    two fallbacks are the CLI's, for the case where this was reached some other way:
    `PATH`, then `sys.executable`'s directory, because a virtual environment's `bin`
    is on `PATH` only for a shell that activated it and an adapter host need not
    have.
    """
    argv0 = Path(sys.argv[0] or "").resolve()
    if argv0.name == SERVICE_EXECUTABLE and os.access(argv0, os.X_OK):
        return str(argv0)
    found = shutil.which(SERVICE_EXECUTABLE)
    if found is not None:
        return found
    beside = Path(sys.executable).parent / SERVICE_EXECUTABLE
    return str(beside) if beside.is_file() and os.access(beside, os.X_OK) else None


def _stop_child(child: subprocess.Popen[bytes]) -> None:
    """Take the failed child away, gracefully if it will go that way."""
    child.terminate()
    try:
        child.wait(timeout=CHILD_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - only on a wedged child
        child.kill()
        try:
            child.wait(timeout=CHILD_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def _clean_child_descriptor(runtime_directory: Path, pid: int) -> None:
    """Remove a descriptor left behind by *this launcher's own* child.

    Compare before removing, exactly as `compare_and_clean` does and for the same
    reason: a descriptor that belongs to a different, healthy service must not be
    un-advertised by a failed start. The comparison here is the child's pid rather
    than a service-instance id, because a launcher never learns the instance id of a
    child that died before it could answer -- but it does know which process it
    spawned, and a descriptor naming that pid was published by it.

    A clean unwind takes the descriptor with it, so this is the kill path only.
    """
    advertised = discover(runtime_directory)
    if advertised is None or advertised.process is None:
        return
    if advertised.process.pid != pid:
        return
    try:
        descriptor_path(runtime_directory).unlink()
    except OSError:  # pragma: no cover - platform dependent
        pass


def _child_output(log_path: Path) -> str:
    """The tail of the child's own output, for a human to read via the adapter."""
    try:
        recorded = log_path.read_bytes()
    except OSError:
        return ""
    return recorded[-MAX_CHILD_OUTPUT_BYTES:].decode("utf-8", "replace").strip()


def _dial_readiness(
    descriptor: ServiceEndpointDescriptor,
) -> dict[str, Any] | None:
    """Ask the advertised service whether it is writable-ready, right now.

    R004-08 step 6: live readiness, not descriptor publication alone. The descriptor
    is written once and never rewritten, so its own `ready` field is a startup-time
    claim that survives the process. This answer is read from the running service's
    live lifecycle object, and a service that is gone answers nothing at all.
    """
    endpoint = parse_endpoint(descriptor.endpoint_uri)
    if endpoint is None or endpoint.scheme is not LOCAL_SCHEME:
        return None
    request = _readiness_request(descriptor.workspace_id)
    try:
        response = LocalSocketTransport(
            endpoint=endpoint, timeout=READINESS_CALL_TIMEOUT_SECONDS
        ).call(request)
    except (TransportError, OSError, ValueError):
        return None
    if response.metadata.correlation_id != request.metadata.correlation_id:
        # An answer that does not correlate is not this call's answer, whatever it
        # holds. On a strictly unary connection it should be impossible.
        return None
    if not isinstance(response, SuccessResponseEnvelope):
        return None
    return dict(response.result)


def _readiness_request(workspace_id: str) -> RequestEnvelope:
    """A contract-valid `core.readiness` envelope, claiming nothing.

    No principal, no scope, no capability: the service-lifecycle operations are
    dispatched against the service's own grant rather than the catalogue, so there
    is no frozen entry obliging a caller to declare anything, and this caller has no
    authority worth claiming. The purpose is stated because the shape requires one,
    and it is only a claim -- the service decides from its own grant.
    """
    request_id = f"managed-start-{uuid.uuid4()}"
    return RequestEnvelope(
        operation=READINESS_OPERATION,
        metadata=RequestMetadata(
            request_id=request_id,
            correlation_id=request_id,
            trace_id=request_id,
            api_version=API_VERSION,
            client=ClientIdentity(id=CLIENT_NAME, version=SERVER_VERSION),
            workspace_id=workspace_id,
            scopes=(),
            purpose=READINESS_PURPOSE,
            required_capabilities=(),
        ),
        input={},
    )


def render_result(result: ManagedStartResult) -> str:
    """The result as the one line of protocol data this mode writes to stdout."""
    return json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"


__all__ = [
    "MANAGED_START_TIMEOUT_SECONDS",
    "MANAGED_START_VERSION",
    "SERVICE_EXECUTABLE",
    "ManagedStartFailure",
    "ManagedStartResult",
    "ManagedStartStatus",
    "managed_start",
    "render_result",
]
