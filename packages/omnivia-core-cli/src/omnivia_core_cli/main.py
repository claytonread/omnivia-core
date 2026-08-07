"""The `omnivia` CLI (B10).

A Core Service client and nothing more. It discovers a service, builds contract
envelopes and reports what it is told. It holds no lease, takes no lock and opens no
database.

Every subcommand but `discover` *calls* a service rather than printing the
envelope it would have sent. `workspace show` reaches the authorised
application path: `workspace.inspect`, under the fixed local-owner session the
service constructs for itself at startup, over the installation-local OVC1
endpoint. `health` and `readiness` reach the service-lifecycle operations
`core.health` and `core.readiness` over that same endpoint, through that same
call path. `discover` answers from the published descriptor alone, which is the
whole of what it claims to report.

A command that cannot reach the service fails, and says so on stderr with a
non-zero exit. None of these report on a service they did not dial: `health`
and `readiness` printed their own request envelope and exited 0 for a service
that was never contacted, which is a success-shaped answer that proves nothing
and which a launcher polling readiness would believe.

`start`, `stop` and `status` make the service usable without the desktop
application, which is what shipping Core open source and driving it over MCP
requires. They stay inside the same boundary as everything else here: the
service is *spawned* as the console script `omnivia-core-service` and signalled
by pid, never imported, and every question about its state is asked by dialling
it. ADR-036 admits exactly that division -- locate or launch the executable,
communicate only through the application API.

`status` is the one that has to be careful. The descriptor is written once, at
startup, and never rewritten, so its `ready` and `lifecycle_state` fields freeze
at their startup values and a service that was killed leaves a file still saying
`ready: true`. `status` therefore ignores those two fields entirely and reports
what a live `core.readiness` call answered, which is read from the running
service's own lifecycle object.

Two claims it states and one it does not. It states the purpose
`workspace_inspection`, and it states the scope and capability requirement the
operation's frozen catalogue entry obliges a caller to declare -- read off that
entry rather than written down here, so the two ends of the call cannot drift.
It claims no principal at all: this path's principal is fixed by
installation-local service configuration, a claim can only ever narrow, and
there is nothing here worth narrowing to. Reaching the endpoint is not a
verification of who is calling, and nothing this module prints says otherwise.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from omnivia_core.contracts.v1 import (
    CapabilityRequirement,
    ServiceEndpointDescriptor,
    codec,
    get_operation_metadata,
)
from omnivia_core_cli.client import build_request, encode, read_descriptor
from omnivia_core_cli.lifecycle import (
    IDENTITY_DIFFERENT,
    IDENTITY_UNREADABLE,
    POLL_SECONDS,
    START_TIMEOUT_SECONDS,
    STOP_TIMEOUT_SECONDS,
    Installation,
    LifecycleError,
    descriptor_is_gone,
    home_directory,
    process_identity,
    process_is_gone,
    request_stop,
    spawn_service,
)

#: The whole-call budget for one `workspace show`, covering discovery's live probe
#: and the request itself. A local call that has not answered in this long is not
#: about to.
CALL_TIMEOUT_SECONDS = 10.0

#: The service-lifecycle operation `status` and the two lifecycle commands dial.
#: Its handler reads the running service's live `lifecycle.state`, so what comes
#: back is the current `ServiceState` -- one of the nine names
#: `service/lifecycle.py` defines -- and not the value frozen into the descriptor
#: at startup.
READINESS_OPERATION = "core.readiness"

#: The one application operation this CLI can call, and the one purpose it may
#: claim. Both are literals here and neither is an argument: a caller-selected
#: operation or purpose is exactly what this path must not have. The scope and
#: the capability are *not* literals -- see `_inspect_claims`.
WORKSPACE_INSPECT_OPERATION = "workspace.inspect"
WORKSPACE_INSPECTION_PURPOSE = "workspace_inspection"


def _inspect_claims() -> tuple[tuple[str, ...], tuple[CapabilityRequirement, ...]]:
    """The scopes and capability requirement `workspace.inspect` obliges a caller to state.

    Read off the frozen catalogue entry rather than transcribed. Writing
    `("workspace:read",)` and `"workspace.read" v1.0` down here would be a second
    copy of a fact the catalogue already holds, free to drift from it, and the
    drift would surface as a refusal whose cause is two files away. The service
    builds its own session from the same entry, so both ends of this call read
    one source.
    """
    entry = get_operation_metadata(WORKSPACE_INSPECT_OPERATION)
    required = entry.required_capability
    return (
        tuple(entry.scope.required_scopes),
        (
            CapabilityRequirement(
                id=required.id,
                minimum_version=required.minimum_version,
                required=required.required,
            ),
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnivia",
        description="Talk to a running OmniVia Core Service. Never owns a workspace.",
    )
    parser.add_argument(
        "--runtime-state",
        default=None,
        type=Path,
        help=(
            "installation runtime directory holding service.json. Defaults to the "
            "single directory under <home>/installation-state/runtime"
        ),
    )
    parser.add_argument(
        "--home",
        default=None,
        type=Path,
        help=(
            "installation root holding workspace/, installation-state/ and run/. "
            "Defaults to ~/.omnivia"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("discover", help="show the discovered service, if any")
    subparsers.add_parser(
        "start", help="start the service for this installation and wait until it is ready"
    )
    subparsers.add_parser("stop", help="ask the running service to stop, and wait for it")
    subparsers.add_parser(
        "status", help="dial the service and report the lifecycle state it answers with"
    )

    for name, help_text in (
        ("health", "ask the service whether it is alive"),
        ("readiness", "ask the service whether it is writable-ready"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--principal", default=None)
        sub.add_argument("--json", action="store_true", help="emit the request envelope")

    workspace = subparsers.add_parser("workspace", help="ask about the served workspace")
    workspace_actions = workspace.add_subparsers(dest="action", required=True)
    workspace_actions.add_parser(
        "show", help="call workspace.inspect and render the workspace descriptor"
    )

    return parser


def _dial(
    runtime_state: Path,
    service: ServiceEndpointDescriptor,
    operation: str,
    *,
    principal: str | None = None,
    scopes: tuple[str, ...] = (),
    purpose: str = "cli",
    required_capabilities: tuple[CapabilityRequirement, ...] = (),
    quiet: bool = False,
) -> dict[str, Any] | None:
    """Call one operation on the discovered service and return its result, or None.

    Split out of `_call` when the lifecycle commands arrived, because they need
    the same dialling and a different rendering: `status` reports a lifecycle
    state in prose, and `start` and `stop` use a successful call as *evidence* --
    that a service is really there -- and print nothing from it at all. A second
    copy of the discovery checks below is a second set of them to keep in step
    with these, which is the failure this split exists to avoid.

    `quiet` suppresses the diagnostics, and it has exactly one caller: `start`
    asking "is one already running?" before it spawns. There, a descriptor that
    fails its checks is the ordinary case rather than a failure, and reporting it
    on stderr would put an error in front of a user whose command then succeeded.

    Every subcommand that dials goes through here. The claims differ per
    operation -- `workspace.inspect` states a scope, a capability and a purpose
    its catalogue entry obliges, the service-lifecycle operations state none --
    but the dialling does not, and a second copy of it is a second set of
    discovery checks to keep in step with these.

    `service` is the descriptor `read_descriptor` already located under
    `--runtime-state`, and it is the authority for what gets dialled. Discovery
    re-derives its own path from an installation root, so the two can name
    different files -- a symlinked `--runtime-state` is enough to separate them.
    When they do, discovery's provenance, mode, scheme and liveness checks land
    on one descriptor while the call would go to the other, which is exactly the
    gap those checks exist to close. So the root is derived from the *resolved*
    path, and the descriptor discovery validated must equal the one that was
    read, or nothing is called at all.

    The client is imported here rather than at module scope, and the reason is
    not style. `discover` answers from the published descriptor alone and must
    keep answering where this distribution is installed without
    `omnivia-core-client`; a module-scope import would make
    `omnivia_core_cli.main` unimportable there and take `discover` and `--help`
    down with it.
    """
    from omnivia_core_client import ClientError, Deadline, discover_endpoint

    from omnivia_core_cli.transport import LocalIpcTransport

    def refuse(reason: str) -> None:
        if not quiet:
            sys.stderr.write(reason + "\n")

    deadline = Deadline.after(CALL_TIMEOUT_SECONDS)
    transport = LocalIpcTransport(endpoint_uri=service.endpoint_uri)

    try:
        # Discovery is not a formality standing between the descriptor and the
        # call. It checks the file's provenance and the mode of the directories
        # above it, refuses an endpoint that is not this platform's local IPC,
        # negotiates all three versions, and proves the descriptor describes the
        # process that is actually listening -- before anything is asked of it.
        discovered = discover_endpoint(
            runtime_state.resolve().parent.parent,
            service.workspace_id,
            transport=transport,
            deadline=deadline,
        )
    except (ClientError, ValueError, OSError):
        # The diagnostic is discarded rather than rendered: the client's failures
        # are payload-free by construction, but a `ValueError` from the public
        # decoder is a statement about a document and can quote it.
        # Worded without "verified" on purpose. What failed is a check on the
        # advertised descriptor -- its provenance, its versions, its liveness --
        # and the vocabulary of identity verification has no business on this
        # path, in either direction.
        refuse("the advertised service did not pass its discovery checks")
        return None
    if discovered is None:
        refuse("no service is advertised; start omnivia-core-service first")
        return None
    if discovered.descriptor != service:
        # Two descriptors, so the checks above were applied to a file this call
        # would not have used. Refuse rather than pick one.
        refuse("the advertised service did not pass its discovery checks")
        return None

    request = build_request(
        operation,
        workspace_id=discovered.descriptor.workspace_id,
        request_id=f"cli-{uuid.uuid4()}",
        principal=principal,
        scopes=scopes,
        purpose=purpose,
        required_capabilities=required_capabilities,
    )
    try:
        response = transport.call(request, deadline=deadline)
    except ClientError:
        refuse("the service did not answer")
        return None

    if response.metadata.correlation_id != request.metadata.correlation_id:
        # The answer correlates to a different request. On a strictly unary
        # connection that should be impossible, which is the reason to say so
        # rather than to render it: an answer that does not correlate is not this
        # call's answer, whatever it contains.
        refuse("the service answered a different request")
        return None

    error = getattr(response, "error", None)
    if error is not None:
        # Only the service's own code and message. Echoing any part of the
        # request back would put a caller-supplied value on the refusal surface,
        # which is the one thing a refusal must not carry.
        refuse(f"{error.code}: {error.message}")
        return None

    # Returned as the wire form the public codec produces, not as the decoded
    # envelope's attributes. The decoded object holds read-only mapping views that
    # `json` cannot serialise, and reaching past them field by field would be this
    # CLI's own second opinion about the shape of a contract result.
    result = codec.encode_response(response)["result"]
    return dict(result) if isinstance(result, dict) else {}


def _call(
    runtime_state: Path,
    service: ServiceEndpointDescriptor,
    operation: str,
    *,
    principal: str | None = None,
    scopes: tuple[str, ...] = (),
    purpose: str = "cli",
    required_capabilities: tuple[CapabilityRequirement, ...] = (),
) -> int:
    """Dial one operation and render its whole result as JSON."""
    result = _dial(
        runtime_state,
        service,
        operation,
        principal=principal,
        scopes=scopes,
        purpose=purpose,
        required_capabilities=required_capabilities,
    )
    if result is None:
        return 1
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


def _report_child_output(installation: Installation) -> None:
    """Put the started process's own words in front of the caller.

    `service/main.py` prints its `StartupReport` -- and in particular `unmet`,
    which names the readiness precondition that failed -- before it exits. Without
    this, a failed `start` reports only that something went wrong, and the one
    document that says *what* sits unread in a log file.
    """
    try:
        recorded = installation.log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if recorded.strip():
        sys.stderr.write(recorded if recorded.endswith("\n") else recorded + "\n")


def _live_service(
    runtime_state: Path | None, *, quiet: bool
) -> tuple[ServiceEndpointDescriptor, dict[str, Any]] | None:
    """The advertised service and its live readiness answer, or None if there is none.

    Both halves, because either alone is a wrong answer. The descriptor is written
    once at startup and never rewritten, so a file claiming `ready: true` survives
    the process that wrote it and reading it alone reports a killed service as
    healthy. Dialling alone cannot be done at all -- the endpoint to dial is in
    the file.
    """
    if runtime_state is None:
        return None
    service = read_descriptor(runtime_state)
    if service is None:
        return None
    answer = _dial(runtime_state, service, READINESS_OPERATION, quiet=quiet)
    return None if answer is None else (service, answer)


def _describe(service: ServiceEndpointDescriptor, answer: dict[str, Any]) -> str:
    """One running service, as the facts a caller acts on.

    `state` and `ready` come from the live answer, never from the descriptor's own
    frozen `lifecycle_state` and `ready` fields. The state is one of the nine
    `ServiceState` names the runtime defines; no second vocabulary is invented here.
    """
    unmet = answer.get("unmet") or []
    lines = [
        f"endpoint: {service.endpoint_uri}",
        f"workspace: {service.workspace_id}",
        f"service instance: {service.service_instance_id}",
        f"state: {answer.get('state')}",
        f"writable: {'yes' if answer.get('ready') else 'no'}",
    ]
    if service.process is not None:
        lines.append(f"pid: {service.process.pid}")
    if unmet:
        lines.append(f"unmet: {', '.join(str(name) for name in unmet)}")
    return "".join(line + "\n" for line in lines)


def _start(installation: Installation, runtime_state: Path | None) -> int:
    """Spawn the service and wait until it answers, or say why it did not.

    Four things this waits on, and each is here because waiting on fewer reports
    a lie:

    - an **existing** service is dialled before anything is spawned, so a second
      `start` reports what is running instead of racing it;
    - readiness, not `Popen` returning. `Popen` returns as soon as the child is
      forked, which is before the migration, the recovery and the endpoint;
    - `poll()` on every pass, so a child that *exited* is reported as an exit with
      its own output rather than being waited out to the timeout and reported as
      a slow start;
    - a live call, not the descriptor's `ready` field, for the same reason
      `status` does not trust it.

    The accepted race: two `start` commands running together both find nothing
    advertised and both spawn. The loser is refused by the lifetime storage lock
    in `runner.py` -- "another service holds the lifetime storage lock" -- so
    there is no split brain, only a process that exits at once. The mutex that
    would prevent even that lives in the runtime, behind the boundary this CLI
    may not cross, so the cost is one wasted process and a message that says what
    happened.
    """
    running = _live_service(runtime_state or _advertised(installation), quiet=True)
    if running is not None:
        sys.stdout.write("already running\n" + _describe(*running))
        return 0

    try:
        process = spawn_service(installation, endpoint_uri=installation.endpoint_uri)
    except LifecycleError as refusal:
        sys.stderr.write(f"{refusal}\n")
        return 1

    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        exited = process.poll()
        if exited is not None:
            sys.stderr.write(
                f"omnivia-core-service exited with status {exited} before it "
                "was ready\n"
            )
            _report_child_output(installation)
            return 1
        started = _live_service(runtime_state or _advertised(installation), quiet=True)
        if started is not None:
            sys.stdout.write(f"started (pid {process.pid})\n" + _describe(*started))
            return 0
        time.sleep(POLL_SECONDS)

    # Nothing else will clean this up. The child was spawned by this command and
    # never became ready, so leaving it running would leave a process holding the
    # storage lock that no `stop` can find -- it advertises no descriptor.
    process.terminate()
    try:
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - only on a wedged child
        process.kill()
        process.wait(timeout=10)
    sys.stderr.write(
        f"omnivia-core-service did not become ready within "
        f"{START_TIMEOUT_SECONDS:.0f}s; the process this command started was stopped\n"
    )
    _report_child_output(installation)
    return 1


def _advertised(installation: Installation) -> Path | None:
    """The installation's one runtime directory, or None while it has none yet.

    `start` asks this before the service exists, when there is legitimately
    nothing there, so the refusal `runtime_state()` raises is an answer here
    rather than an error.
    """
    try:
        return installation.runtime_state()
    except LifecycleError:
        return None


def _stop(runtime_state: Path) -> int:
    """Signal the running service and wait for it to withdraw its own descriptor.

    **The identity check comes before the signal, and it is two checks.** A pid on
    its own is not enough -- pids are recycled, ADR-037 says so, and
    `service/bootstrap.py` says so again -- so signalling one read out of a file
    can hit a process that has nothing to do with Core.

    1. The service is dialled first. A live `core.readiness` answer that passed
       discovery's workspace and service-instance agreement establishes that the
       descriptor is *current*: a service that died left a file no live instance
       will vouch for, and that is the case in which its pid is stale.
    2. The published `start_time` is then compared against a reading taken now, so
       the window between the dial and the signal is covered too.

    **The residual ceiling, stated rather than papered over.** Neither check binds
    the process that answered on the socket to the pid in the file: this CLI holds
    no primitive that can ask a local socket which process is behind it, and
    acquiring one is a recorded deferral, not this lane's work. What is true is
    that the descriptor is published by the process it describes, that a live
    instance vouched for it, and that the pid's start time still matches. What is
    not established is the socket-to-pid link itself. `boot_id` is not compared
    either, for the reason `process_identity` gives.

    On a host that can offer no start-time reading -- Windows -- the second check
    is skipped and said to be skipped, rather than being quietly reported as a
    match.
    """
    service = read_descriptor(runtime_state)
    if service is None:
        # Symmetric with `start` finding one already running: asking for a state
        # the system is already in is not a failure.
        sys.stdout.write("not running\n")
        return 0

    if _dial(runtime_state, service, READINESS_OPERATION) is None:
        sys.stderr.write(
            f"a descriptor is advertised at {runtime_state} but the service it "
            "names is not answering; nothing was signalled. It is left in place: "
            "cleanup belongs to the instance that published it, and the next "
            "start replaces it\n"
        )
        return 1

    process = service.process
    if process is None:
        sys.stderr.write(
            "the advertised descriptor names no process, so there is nothing to "
            "signal\n"
        )
        return 1

    identity = process_identity(process)
    if identity == IDENTITY_DIFFERENT:
        sys.stderr.write(
            f"pid {process.pid} is not the process that published this "
            "descriptor; nothing was signalled\n"
        )
        return 1
    if identity == IDENTITY_UNREADABLE:  # pragma: no cover - Windows only
        sys.stderr.write(
            "note: this host offers no process start-time reading, so the pid "
            "was not corroborated against the published one\n"
        )

    request_stop(process.pid)
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    if not descriptor_is_gone(runtime_state, deadline):
        sys.stderr.write(
            f"the service was asked to stop but had not withdrawn its descriptor "
            f"after {STOP_TIMEOUT_SECONDS:.0f}s\n"
        )
        return 1
    # Two events, in this order, and only both of them make "stopped" true. The
    # withdrawn descriptor proves the unwind ran; the process is still shutting
    # down after it. Reporting success here would let a caller start a
    # replacement into a lock the old process has not yet dropped.
    if not process_is_gone(process.pid, deadline):
        sys.stderr.write(
            f"the service withdrew its descriptor but pid {process.pid} was still "
            f"running after {STOP_TIMEOUT_SECONDS:.0f}s\n"
        )
        return 1
    sys.stdout.write("stopped\n")
    return 0


def _status(runtime_state: Path) -> int:
    """Report what a live call answered, never what the file claims."""
    running = _live_service(runtime_state, quiet=False)
    if running is None:
        sys.stdout.write("not running\n")
        return 1
    sys.stdout.write("running\n" + _describe(*running))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    installation = Installation(home_directory(args.home))

    if args.command == "start":
        return _start(installation, args.runtime_state)

    runtime_state = args.runtime_state
    if runtime_state is None:
        try:
            runtime_state = installation.runtime_state()
        except LifecycleError as refusal:
            # Nothing is advertised anywhere under this installation, which the
            # two lifecycle queries answer in their own vocabulary rather than as
            # a path error.
            if args.command == "stop":
                sys.stdout.write("not running\n")
                return 0
            if args.command == "status":
                sys.stdout.write("not running\n")
                sys.stderr.write(f"{refusal}\n")
                return 1
            sys.stderr.write(f"{refusal}\n")
            return 1

    if args.command == "status":
        return _status(runtime_state)
    if args.command == "stop":
        return _stop(runtime_state)

    service = read_descriptor(runtime_state)

    if args.command == "discover":
        if service is None:
            sys.stdout.write("no service is advertised\n")
            return 1
        sys.stdout.write(
            json.dumps(
                {
                    # The output key stays `endpoint`: the value moved to
                    # `endpoint_uri` in the published document, what it means to a
                    # caller of this command did not, and renaming it would break
                    # every script reading this JSON for no gain.
                    "endpoint": service.endpoint_uri,
                    "workspace_id": service.workspace_id,
                    "service_instance_id": service.service_instance_id,
                    "fencing_generation": service.fencing_generation,
                    # `advertised_ready`, not `ready`. The descriptor is published
                    # once at startup and never rewritten, so this is what the
                    # service claimed when it started and not what is true now: a
                    # service killed hard leaves the file behind still saying true.
                    # A caller reading a bare `ready` gets exactly the false
                    # liveness signal `status` exists to avoid, and it would get it
                    # silently. `observation` names the source in the same breath,
                    # so a script has to opt into believing a stale claim rather
                    # than doing it by default.
                    "advertised_ready": service.ready,
                    "observation": "published-descriptor",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return 0

    if service is None:
        sys.stderr.write("no service is advertised; start omnivia-core-service first\n")
        return 1

    if args.command == "workspace":
        # The descriptor read above supplies the endpoint to dial and the
        # workspace to name. Neither is chosen here and neither comes from an
        # argument: this CLI cannot ask about a workspace other than the one the
        # endpoint it found was launched to serve.
        scopes, required_capabilities = _inspect_claims()
        return _call(
            runtime_state,
            service,
            WORKSPACE_INSPECT_OPERATION,
            scopes=scopes,
            purpose=WORKSPACE_INSPECTION_PURPOSE,
            required_capabilities=required_capabilities,
        )

    if args.json:
        # The one path that still prints instead of calling, and it is now the
        # opt-in the flag always advertised rather than the default. What comes
        # out is the envelope this CLI *would* send: a request, not an answer,
        # and no evidence whatsoever about the service.
        request = build_request(
            f"core.{args.command}",
            workspace_id=service.workspace_id,
            request_id=f"cli-{uuid.uuid4()}",
            principal=args.principal,
        )
        sys.stdout.write(encode(request) + "\n")
        return 0

    # `health` and `readiness` are the service-lifecycle operations, and they
    # state no scope, no capability and no purpose beyond the default: they are
    # dispatched against the service's own grant rather than the catalogue, so
    # there is no frozen entry obliging a caller to declare anything. Printing
    # the envelope here instead of sending it -- which is what this branch used
    # to do unconditionally -- answered "alive" and "ready" with exit 0 for a
    # service that was never dialled, and a launcher polling readiness would act
    # on it.
    return _call(
        runtime_state,
        service,
        f"core.{args.command}",
        principal=args.principal,
    )


if __name__ == "__main__":
    sys.exit(main())
