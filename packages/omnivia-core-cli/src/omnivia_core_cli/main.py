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

`init`, `start`, `stop` and `status` make the service usable without the desktop
application, which is what shipping Core open source and driving it over MCP
requires. `init` is the first of the four in every sense: before R004-10 no
shipped command created a workspace, so `omnivia start` on a fresh machine had
nothing to start and the service refused an unbootstrapped directory outright.
It does no bootstrapping of its own -- the workspace is made by
`omnivia-core-service --init`, where an exclusive database open is legal -- and
it starts nothing, because `init` establishes state and `start` establishes the
process. They stay inside the same boundary as everything else here: the
console script `omnivia-core-service` is *launched* and the service is signalled
by pid, never imported, and every question about its state is asked by dialling
it. ADR-036 admits exactly that division -- locate or launch the executable,
communicate only through the application API.

`start` does not do the starting. R004-08 puts managed start in the service
package so the CLI and the MCP adapter share one implementation of discovery, the
bootstrap mutex, spawn, readiness and failed-child cleanup, and this command
reaches it by launching `omnivia-core-service --managed-start` and reading the one
versioned JSON document it answers with. That removed the accepted duplicate-spawn
race: two `start` commands running together used to both spawn, with the loser
refused by the lifetime storage lock; now the loser waits for the mutex holder and
attaches to the service it started.

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
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    CapabilityRequirement,
    ContractSemanticError,
    CoreSafeStatusV1,
    CoreTargetV1,
    ServiceEndpointDescriptor,
    codec,
    encode_core_safe_status,
    get_operation_metadata,
)
from omnivia_core_cli.client import build_request, encode, read_descriptor
from omnivia_core_cli.lifecycle import (
    IDENTITY_DIFFERENT,
    IDENTITY_UNREADABLE,
    STOP_TIMEOUT_SECONDS,
    Installation,
    LifecycleError,
    descriptor_is_gone,
    home_directory,
    process_identity,
    process_is_gone,
    request_init,
    request_managed_start,
    request_stop,
)
from omnivia_core_cli.safe_status import (
    degraded_status,
    incompatible_status,
    live_status,
    resolve_local_target,
    stopped_status,
    unreachable_status,
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

#: Version of the small lifecycle adapter document emitted by
#: ``start|stop|status --json``. This is intentionally not the managed-start
#: document: the latter is a service-owned launcher protocol, while this one is
#: the stable, least-privilege view adapters such as the Core status menu consume.
#:
#: **Version 2 removed `service` and `reason`.** Both were this installation's
#: internals -- a workspace id, a service instance id, the runtime's raw state
#: name and its raw `unmet` list; a free-form sentence naming directories,
#: endpoints, pids and the launcher's own words -- published to a surface that
#: has authenticated nothing. What replaced them is a required `code` from the
#: closed set below and an optional `safe_status`, which is `CoreSafeStatusV1`
#: and is rendered only by the contract encoder.
LIFECYCLE_ADAPTER_VERSION = 2

#: Every `code` this adapter may publish, and the whole of what a machine caller
#: learns about *why*. Closed and bounded on purpose: a caller can branch on
#: these, a UI can phrase them in its own words, and neither ever receives a
#: string this process assembled from a path, an endpoint, a launcher result or
#: an exception. `internal_error` is the fail-closed landing place for a code
#: this module did not declare -- publishing an undeclared one is the defect the
#: set exists to prevent.
LIFECYCLE_CODES: Final = frozenset(
    {
        "start_started",
        "start_attached",
        "start_workspace_missing",
        "start_incompatible_service",
        "start_timeout",
        "start_spawn_failed",
        "start_not_ready",
        "start_failed",
        "stop_stopped",
        "stop_not_running",
        "stop_service_unreachable",
        "stop_no_process",
        "stop_identity_mismatch",
        "stop_timeout",
        "stop_process_lingering",
        "status_running",
        "status_not_running",
        "internal_error",
    }
)

#: The launcher's five closed failure classes, as this adapter's own codes. The
#: mapping is deliberate rather than a passthrough: `ManagedStartFailure` is a
#: service-owned vocabulary free to grow, and a sixth class arriving from a newer
#: runtime must land on `start_failed` rather than be published unrecognised.
_MANAGED_START_FAILURE_CODES: Final = {
    "missing_workspace": "start_workspace_missing",
    "incompatible_service": "start_incompatible_service",
    "timeout": "start_timeout",
    "spawn_failure": "start_spawn_failed",
    "readiness_failure": "start_not_ready",
}


def _write_lifecycle_document(
    action: str,
    *,
    ok: bool,
    outcome: str,
    code: str,
    safe_status: CoreSafeStatusV1 | None = None,
) -> None:
    """Write the one machine-readable lifecycle adapter document.

    The safe status goes through `encode_core_safe_status` and through nothing
    else, so a status that does not satisfy the contract's cross-field
    invariants -- an action offered for a target that may not be acted on, a
    version disagreement -- is omitted rather than published. Omission is the
    fail-closed answer: a caller that receives no `safe_status` offers no
    actions, while a caller that receives an invalid one might.
    """
    document: dict[str, Any] = {
        "lifecycle_adapter_version": LIFECYCLE_ADAPTER_VERSION,
        "action": action,
        "ok": ok,
        "outcome": outcome,
        "code": code if code in LIFECYCLE_CODES else "internal_error",
    }
    if safe_status is not None:
        try:
            document["safe_status"] = encode_core_safe_status(safe_status)
        except ContractSemanticError:
            pass
    sys.stdout.write(json.dumps(document, sort_keys=True) + "\n")


def _finish_lifecycle(
    action: str,
    *,
    json_output: bool,
    returncode: int,
    outcome: str,
    code: str,
    safe_status: CoreSafeStatusV1 | None = None,
    human_stdout: str | None = None,
    human_stderr: str | None = None,
) -> int:
    """Render one lifecycle result without mixing JSON and human prose."""
    if json_output:
        _write_lifecycle_document(
            action,
            ok=returncode == 0,
            outcome=outcome,
            code=code,
            safe_status=safe_status,
        )
    else:
        if human_stdout:
            sys.stdout.write(human_stdout)
        if human_stderr:
            sys.stderr.write(human_stderr)
    return returncode


def _selected_target(installation: Installation, *, json_output: bool) -> CoreTargetV1 | None:
    """The one target this command addresses, or None if it cannot be formed.

    Resolved only when a machine document will be written: the human paths
    publish no target, and reading a manifest they will not use is work for
    nothing.
    """
    return resolve_local_target(installation) if json_output else None


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
        "init",
        help=(
            "create the workspace for this installation if there is not one "
            "already. Starts no service"
        ),
    )
    for name, help_text in (
        ("start", "start the service for this installation and wait until it is ready"),
        ("stop", "ask the running service to stop, and wait for it"),
        ("status", "dial the service and report the lifecycle state it answers with"),
    ):
        lifecycle = subparsers.add_parser(name, help=help_text)
        lifecycle.add_argument(
            "--json",
            action="store_true",
            help="emit the versioned lifecycle adapter document",
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
    down with it. `LocalIpcTransport` is in that same import because owner
    resolution 005 R005-01 moved it into the client package: the CLI constructs
    the client-owned transport and no longer ships one of its own.
    """
    from omnivia_core_client import (
        ClientError,
        Deadline,
        LocalIpcTransport,
        discover_endpoint,
    )

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


def _init(installation: Installation) -> int:
    """Ask the shared bootstrap path for a workspace, and report what it answered.

    **This command does not do the initialising.** R004-10 puts workspace bootstrap
    in the service package, where an exclusive database open is legal, so the whole
    of `init` is: launch `omnivia-core-service --init`, read its one JSON document,
    and put it into words. Nothing here opens a database, writes a manifest or takes
    a lock.

    Three outcomes, and the middle one matters as much as the first: `initialised`
    when a workspace was made, `already initialised` when one was there and nothing
    changed, and a refusal naming what it declined to overwrite. Repeating this
    command is safe, which is what makes it usable as a first step in a script that
    does not know whether it has run before.

    It prints where the workspace is and what it is called, and nothing else. There
    is no service yet to describe -- `start` is the next command, not a step this
    one takes.
    """
    try:
        result = request_init(installation)
    except LifecycleError as refusal:
        sys.stderr.write(f"{refusal}\n")
        return 1

    status = result.get("status")
    workspace = result.get("workspace")
    if status in ("initialised", "already_initialised") and isinstance(workspace, dict):
        headline = "initialised" if status == "initialised" else "already initialised"
        sys.stdout.write(
            headline
            + "\n"
            + f"workspace: {workspace.get('workspace_id')}\n"
            + f"workspace root: {workspace.get('workspace_root')}\n"
            + f"installation state: {workspace.get('installation_state')}\n"
            + f"format: {workspace.get('workspace_format_version')}\n"
            + f"start it with: omnivia --home {installation.home} start\n"
        )
        return 0

    sys.stderr.write(
        f"{result.get('reason') or 'the workspace could not be initialised'}\n"
    )
    return 1


def _start(installation: Installation, *, json_output: bool = False) -> int:
    """Ask the shared managed-start path for a service, and report what it answered.

    **This command no longer does the starting.** R004-08 makes managed start a
    service-owned path so that the CLI and the MCP adapter run one implementation
    rather than two, and R004-09 makes it the first production caller of
    `coordinated_startup`. So the whole of `start` is now: launch
    `omnivia-core-service --managed-start`, read its one JSON document, and put it
    into words. Discovery, compatibility, the bootstrap mutex, the recheck, the
    spawn, the readiness wait and the failed-child cleanup all happen behind that
    subprocess -- and the boundary is unchanged, because a subprocess is a
    subprocess whether it serves or arbitrates.

    What a user sees is deliberately the same as before. `already running` when one
    is up, `started (pid N)` when one was made, the same five-line description, and
    on a failure the started process's own words. The one thing that is different is
    what happens when two `start` commands race: the accepted duplicate spawn is
    gone, because the launcher that loses the mutex waits for the winner and then
    attaches to the service the winner started.

    The description's `state` and `writable` come from the launcher's live
    `core.readiness` answer, not from the descriptor's frozen fields, for the same
    reason `status` does not trust them.
    """
    target = _selected_target(installation, json_output=json_output)
    try:
        result = request_managed_start(
            installation, endpoint_uri=installation.endpoint_uri
        )
    except LifecycleError as refusal:
        # A refusal raised on this side: an over-long endpoint, or no manifest at
        # all. Start is still the safe offer -- it is the attach-first path, so
        # repeating it starts nothing that is already there.
        return _finish_lifecycle(
            "start",
            json_output=json_output,
            returncode=1,
            outcome="failed",
            code="start_failed",
            safe_status=(
                unreachable_status(target, may_start=True) if target is not None else None
            ),
            human_stderr=f"{refusal}\n",
        )

    status = result.get("status")
    service = result.get("service")
    if status in ("attached", "started") and isinstance(service, dict):
        headline = "already running" if status == "attached" else "started"
        pid = service.get("pid")
        if status == "started" and pid is not None:
            headline = f"started (pid {pid})"
        return _finish_lifecycle(
            "start",
            json_output=json_output,
            returncode=0,
            outcome=status,
            code="start_attached" if status == "attached" else "start_started",
            safe_status=(
                live_status(
                    target,
                    state=service.get("state"),
                    ready=service.get("ready"),
                    server_version=service.get("server_version"),
                    protocol_version=service.get("protocol_version"),
                )
                if target is not None
                else None
            ),
            human_stdout=headline + "\n" + _describe_service(service),
        )

    code = _MANAGED_START_FAILURE_CODES.get(
        str(result.get("failure")), "start_failed"
    )
    reason = str(result.get("reason") or "the service could not be started")
    child_output = result.get("child_output")
    human_stderr = reason + "\n"
    if isinstance(child_output, str) and child_output.strip():
        human_stderr += child_output.rstrip("\n") + "\n"
    if target is None:
        safe_status = None
    elif code == "start_incompatible_service":
        # Somebody else's authoritative service owns this workspace. Not ours to
        # stop, and the launcher already refused to start a second one.
        safe_status = incompatible_status(target)
    else:
        safe_status = unreachable_status(target, may_start=True)
    return _finish_lifecycle(
        "start",
        json_output=json_output,
        returncode=1,
        outcome="failed",
        code=code,
        safe_status=safe_status,
        human_stderr=human_stderr,
    )


def _describe_service(service: dict[str, Any]) -> str:
    """One running service, rendered from the managed-start result.

    The same five-to-seven lines `_describe` renders from a descriptor and a live
    readiness answer, because they are the same facts: the launcher already dialled
    `core.readiness` and put the answer in the result, so dialling again here would
    be a second opinion about a service this command did not start and cannot
    improve on.
    """
    unmet = service.get("unmet") or []
    lines = [
        f"endpoint: {service.get('endpoint_uri')}",
        f"workspace: {service.get('workspace_id')}",
        f"service instance: {service.get('service_instance_id')}",
        f"state: {service.get('state')}",
        f"writable: {'yes' if service.get('ready') else 'no'}",
    ]
    if service.get("pid") is not None:
        lines.append(f"pid: {service['pid']}")
    if unmet:
        lines.append(f"unmet: {', '.join(str(name) for name in unmet)}")
    return "".join(line + "\n" for line in lines)


def _stop(
    installation: Installation, runtime_state: Path, *, json_output: bool = False
) -> int:
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
    target = _selected_target(installation, json_output=json_output)
    service = read_descriptor(runtime_state)
    if service is None:
        # Symmetric with `start` finding one already running: asking for a state
        # the system is already in is not a failure.
        return _finish_lifecycle(
            "stop",
            json_output=json_output,
            returncode=0,
            outcome="not_running",
            code="stop_not_running",
            safe_status=stopped_status(target) if target is not None else None,
            human_stdout="not running\n",
        )

    if _dial(
        runtime_state, service, READINESS_OPERATION, quiet=json_output
    ) is None:
        reason = (
            f"a descriptor is advertised at {runtime_state} but the service it "
            "names is not answering; nothing was signalled. It is left in place: "
            "cleanup belongs to the instance that published it, and the next "
            "start replaces it"
        )
        return _finish_lifecycle(
            "stop",
            json_output=json_output,
            returncode=1,
            outcome="failed",
            code="stop_service_unreachable",
            # `start` recovers from exactly this -- a descriptor left by a service
            # that is gone -- so start is safe to offer and stop is not: nothing
            # was established to stop.
            safe_status=(
                unreachable_status(target, may_start=True) if target is not None else None
            ),
            human_stderr=reason + "\n",
        )

    process = service.process
    if process is None:
        reason = (
            "the advertised descriptor names no process, so there is nothing to "
            "signal"
        )
        return _finish_lifecycle(
            "stop",
            json_output=json_output,
            returncode=1,
            outcome="failed",
            code="stop_no_process",
            # Something live answered, so a start would be starting a second one,
            # and no ownership was established, so a stop has nothing to signal.
            # Neither action is offered.
            safe_status=(
                degraded_status(target, lifecycle_state="running")
                if target is not None
                else None
            ),
            human_stderr=reason + "\n",
        )

    identity = process_identity(process)
    if identity == IDENTITY_DIFFERENT:
        return _finish_lifecycle(
            "stop",
            json_output=json_output,
            returncode=1,
            outcome="failed",
            code="stop_identity_mismatch",
            safe_status=(
                degraded_status(target, lifecycle_state="running")
                if target is not None
                else None
            ),
            human_stderr=(
                f"pid {process.pid} is not the process that published this "
                "descriptor; nothing was signalled\n"
            ),
        )
    if identity == IDENTITY_UNREADABLE and not json_output:  # pragma: no cover
        sys.stderr.write(
            "note: this host offers no process start-time reading, so the pid "
            "was not corroborated against the published one\n"
        )

    request_stop(process.pid)
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    if not descriptor_is_gone(runtime_state, deadline):
        reason = (
            "the service was asked to stop but had not withdrawn its descriptor "
            f"after {STOP_TIMEOUT_SECONDS:.0f}s"
        )
        return _finish_lifecycle(
            "stop",
            json_output=json_output,
            returncode=1,
            outcome="failed",
            code="stop_timeout",
            # The stop was accepted and has not completed. The requested action is
            # already under way, so nothing is offered.
            safe_status=(
                degraded_status(target, lifecycle_state="stopping")
                if target is not None
                else None
            ),
            human_stderr=reason + "\n",
        )
    # Two events, in this order, and only both of them make "stopped" true. The
    # withdrawn descriptor proves the unwind ran; the process is still shutting
    # down after it. Reporting success here would let a caller start a
    # replacement into a lock the old process has not yet dropped.
    if not process_is_gone(process.pid, deadline):
        reason = (
            "the service withdrew its descriptor but its process was still "
            f"running after {STOP_TIMEOUT_SECONDS:.0f}s"
        )
        return _finish_lifecycle(
            "stop",
            json_output=json_output,
            returncode=1,
            outcome="failed",
            code="stop_process_lingering",
            safe_status=(
                degraded_status(target, lifecycle_state="stopping")
                if target is not None
                else None
            ),
            human_stderr=(
                f"the service withdrew its descriptor but pid {process.pid} was still "
                f"running after {STOP_TIMEOUT_SECONDS:.0f}s\n"
            ),
        )
    return _finish_lifecycle(
        "stop",
        json_output=json_output,
        returncode=0,
        outcome="stopped",
        code="stop_stopped",
        safe_status=stopped_status(target) if target is not None else None,
        human_stdout="stopped\n",
    )


def _status(
    installation: Installation, runtime_state: Path, *, json_output: bool = False
) -> int:
    """Report what a live call answered, never what the file claims."""
    target = _selected_target(installation, json_output=json_output)
    running = _live_service(runtime_state, quiet=json_output)
    if running is None:
        return _finish_lifecycle(
            "status",
            json_output=json_output,
            returncode=1,
            outcome="not_running",
            code="status_not_running",
            safe_status=stopped_status(target) if target is not None else None,
            human_stdout="not running\n",
        )
    service, answer = running
    return _finish_lifecycle(
        "status",
        json_output=json_output,
        returncode=0,
        outcome="running",
        code="status_running",
        safe_status=(
            live_status(
                target,
                state=answer.get("state"),
                ready=answer.get("ready"),
                server_version=service.server_version,
                protocol_version=service.protocol_version,
            )
            if target is not None
            else None
        ),
        human_stdout="running\n" + _describe(service, answer),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    installation = Installation(home_directory(args.home))
    json_output = bool(getattr(args, "json", False))

    if args.command == "init":
        # Before the `--runtime-state` resolution below, and not subject to it:
        # nothing is advertised yet on a machine where this is the first command
        # run, and refusing here for want of a runtime directory would make the
        # command that creates the workspace require a service to already exist.
        return _init(installation)

    if args.command == "start":
        # `--runtime-state` is not passed on. The managed-start path derives the one
        # runtime directory from the installation root and the workspace the
        # manifest names, which is the path a service actually publishes to; the
        # flag only ever selected which descriptor this command *polled*, and
        # pointing it elsewhere never changed where the started service wrote.
        return _start(installation, json_output=json_output)

    runtime_state = args.runtime_state
    if runtime_state is None:
        try:
            runtime_state = installation.runtime_state()
        except LifecycleError as refusal:
            # Nothing is advertised anywhere under this installation, which the
            # two lifecycle queries answer in their own vocabulary rather than as
            # a path error.
            target = _selected_target(installation, json_output=json_output)
            stopped = stopped_status(target) if target is not None else None
            if args.command == "stop":
                return _finish_lifecycle(
                    "stop",
                    json_output=json_output,
                    returncode=0,
                    outcome="not_running",
                    code="stop_not_running",
                    safe_status=stopped,
                    human_stdout="not running\n",
                )
            if args.command == "status":
                return _finish_lifecycle(
                    "status",
                    json_output=json_output,
                    returncode=1,
                    outcome="not_running",
                    code="status_not_running",
                    safe_status=stopped,
                    human_stdout="not running\n",
                    human_stderr=f"{refusal}\n",
                )
            sys.stderr.write(f"{refusal}\n")
            return 1

    if args.command == "status":
        return _status(installation, runtime_state, json_output=json_output)
    if args.command == "stop":
        return _stop(installation, runtime_state, json_output=json_output)

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
