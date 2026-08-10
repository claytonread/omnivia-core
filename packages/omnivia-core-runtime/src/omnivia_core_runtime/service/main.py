"""`omnivia-core-service` entry point (T-0629G).

Minimal by design. This owns and advertises one writable workspace and nothing
else. Health, readiness and discovery are deliberately distinct from product
operations, per ADR-037, and stay on the probe dispatcher unchanged.

One product operation is now registered, and it is registered on a *second* path:
`workspace.inspect`, the first authorised application operation, is decided by
`authorize_application_request` through `service.application`. It is not in
`SERVICE_OPERATIONS`, not in `build_service_registry()` and not in the probe grant,
and it cannot be: the owner's binding clause forbids routing it through the probe
`Dispatcher`, and the two operation sets are disjoint by construction.

**One console script, three kinds of process.** `--managed-start` (R004-08) does
not serve: it arbitrates through the bootstrap mutex, starts an independent service
when one is needed, waits for that service to answer a live readiness call, prints
a versioned result document and exits. `--init` (R004-10) does not serve either: it
creates the workspace a service can then own, prints its own versioned result, and
starts nothing. Every other mode here belongs to a process that *is* the service.
The CLI and, later, the MCP adapter reach both shared paths by launching this
script -- never by importing the runtime -- so there is one implementation of
workspace bootstrap and one of process control, rather than one per adapter.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Protocol

from omnivia_core.contracts.v1 import RequestEnvelope, ResponseEnvelope
from omnivia_core_runtime.service.application import (
    EVIDENCE_SEARCH_OPERATION,
    GRAPH_TRAVERSE_OPERATION,
    KNOWLEDGE_SEARCH_OPERATION,
    LOCAL_TRANSPORT_ADAPTER,
    MEMORY_SEARCH_OPERATION,
    WORKSPACE_INSPECT_OPERATION,
    ApplicationDispatcher,
    build_application_registry,
    local_owner_session,
)
from omnivia_core_runtime.service.authorization import Grant, ServiceBinding
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.http_transport import (
    CredentialResolver,
    HttpBind,
    HttpListener,
    HttpTransportError,
    parse_http_endpoint,
)
from omnivia_core_runtime.service.managed_start import (
    ManagedStartStatus,
    managed_start,
    render_result,
)
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    server_capability_snapshot,
)
from omnivia_core_runtime.service.probes import ProbeRouter, ServiceFacts
from omnivia_core_runtime.service.protocol import DocumentRouter
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.transport import (
    LOCAL_SCHEME,
    LocalEndpoint,
    LocalSocketServer,
    parse_endpoint,
)
from omnivia_core_runtime.service.workspace_init import (
    WorkspaceInitStatus,
    initialise_workspace,
)
from omnivia_core_runtime.service.workspace_init import (
    render_result as render_init_result,
)
from omnivia_core_runtime.storage.projections.fts import (
    build_search_projection,
    open_search_projection,
)

#: The one principal this service instance acts as, on both its paths. Fixed by
#: trusted installation-local service configuration -- not by anything a request
#: carries, and not by the operating-system identity this process runs as.
#:
#: It is **not** a verified operating-system peer identity and must not be described
#: as one: this repository holds no peer-credential primitive, so what is true is that
#: a caller reached a protected local endpoint, and that access to that endpoint
#: establishes permission to act as this principal for a bounded Personal-mode local
#: deployment. The filesystem permissions on the socket and the descriptor are channel
#: trust, not proof of who connected. Verified peer identity is the recorded deferral
#: `LOCAL-IPC-PEER-IDENTITY-DEFERRED`, required before shared-host, multi-user or
#: Organisation-mode local deployment.
#:
#: Still one principal, not many. The application session below narrows this one; it
#: does not introduce a second.
LOCAL_PRINCIPAL = "local-user"


class _ProbeFactsSource(Protocol):
    def probe_facts(self) -> ServiceFacts: ...


class _ApplicationDispatch(Protocol):
    """An object that can answer one decoded application request.

    Widened from `Dispatcher` when the application path arrived, because the router
    now receives the object that holds *both* paths. Stated as a protocol rather than
    a union so the router keeps knowing nothing about authority, which is the whole
    reason `DocumentRouter` takes a plain callable.
    """

    def dispatch(self, request: RequestEnvelope) -> ResponseEnvelope: ...


def _router_for(
    started: _ProbeFactsSource, dispatcher: _ApplicationDispatch
) -> DocumentRouter:
    """Compose the accepted structural router around the existing dispatcher."""
    return DocumentRouter(
        probes=ProbeRouter(
            facts=started.probe_facts,
            capabilities=tuple,
            clock=time.monotonic_ns,
        ),
        dispatch=dispatcher.dispatch,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnivia-core-service",
        description="Own and advertise one writable OmniVia workspace.",
    )
    parser.add_argument("--workspace", required=True, type=Path, help="workspace root")
    parser.add_argument(
        "--installation-state",
        required=True,
        type=Path,
        help="installation-local state root (backups, attempts, runtime)",
    )
    parser.add_argument(
        "--endpoint",
        default=None,
        help=(
            "endpoint to serve and advertise, as a URL: unix://<socket path> on "
            "POSIX, pipe://<name> on Windows"
        ),
    )
    parser.add_argument(
        "--http-endpoint",
        default=None,
        help=(
            "loopback endpoint for HTTP v1, as a URL: http://127.0.0.1:<port> or "
            "http://[::1]:<port>. Through this console script no value serves: "
            "a non-loopback or wildcard host refuses on the endpoint rule, and "
            "every remaining value refuses on the trusted credential resolver "
            "this script does not supply, so a run that would serve exits 2 "
            "either way. Only an embedder calling main() with a resolver can "
            "bring the listener up. Ignored under --check-only, which does not "
            "parse it"
        ),
    )
    parser.add_argument(
        "--core-version", default="0.1.0", help="Core version for compatibility checks"
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help=(
            "run startup, report readiness as JSON, then stop without serving. "
            "This checks the workspace, not the invocation: neither --endpoint "
            "nor --http-endpoint is parsed or validated in this mode"
        ),
    )
    parser.add_argument(
        "--managed-start",
        action="store_true",
        help=(
            "do not serve; make sure a service exists for this workspace and "
            "print a versioned machine-readable result on stdout. Attaches to a "
            "compatible ready service, or spawns an independent one and waits "
            "for it to answer. This process exits; the service it started does "
            "not. Requires --endpoint, and takes precedence over --check-only, "
            "which is a mode of a process that serves"
        ),
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help=(
            "do not serve; make --workspace into a workspace a service can own "
            "and print a versioned machine-readable result on stdout. Idempotent "
            "and non-destructive: an existing compatible workspace is kept, and "
            "an incompatible manifest, an unrelated non-empty directory or an "
            "unrecognised installation state is refused rather than overwritten. "
            "This starts no service. Takes precedence over every other mode, "
            "which all belong to a process that serves or arbitrates"
        ),
    )
    parser.add_argument(
        "--managed-start-log",
        default=None,
        type=Path,
        help=(
            "where a service started by --managed-start writes its own output. "
            "Defaults to service.log beside the discovery descriptor"
        ),
    )
    return parser


def _endpoint_to_serve(endpoint: str | None) -> LocalEndpoint | None:
    """The endpoint this platform can actually serve, or None when there is none.

    The scheme has to match the platform, not merely parse. A `unix://` endpoint on
    Windows is not a socket this process can bind with a different constant in front
    of it -- CPython has no `AF_UNIX` there at all -- so accepting one would put a
    live process behind an endpoint nothing can open and advertise it as ready.
    """
    if endpoint is None:
        return None
    parsed = parse_endpoint(endpoint)
    if parsed is None or parsed.scheme is not LOCAL_SCHEME:
        return None
    return parsed


def _http_bind_to_serve(endpoint: str | None) -> HttpBind | None:
    """The HTTP bind this lane may serve, or `None` when none was asked for.

    Refused before startup rather than after, for the reason the local endpoint is:
    a refusal that arrives once the process is live has already advertised a service
    it cannot honour. A wildcard or non-loopback host, a URL carrying a credential and
    a URL naming a path all raise out of `parse_http_endpoint`, and this lane
    implements no TLS, so none of them can be served.
    """
    if endpoint is None:
        return None
    return parse_http_endpoint(endpoint)


def _init(args: argparse.Namespace) -> int:
    """Run the shared bootstrap path and write its result to stdout.

    Same output contract as `--managed-start`, for the same reason: the versioned
    result document is the whole of stdout so an adapter can read it without a
    parser that skips prose, and the human sentence goes to stderr. R004-10.

    No `--endpoint`. Nothing is bound, nothing is advertised and nothing is
    started -- `init` establishes state, and `start` or MCP managed start
    establishes the process.

    Exit 0 covers both initialising and finding it already done: a caller that
    only checks the exit code learns whether it has a startable workspace, and one
    that reads the status line learns which of the two happened.
    """
    result = initialise_workspace(
        workspace_root=args.workspace,
        installation_root=args.installation_state,
        core_version=args.core_version,
    )
    sys.stdout.write(render_init_result(result))
    sys.stdout.flush()
    if result.status is WorkspaceInitStatus.REFUSED:
        sys.stderr.write(result.reason + "\n")
        return 1
    return 0


def _managed_start(args: argparse.Namespace) -> int:
    """Run the shared managed-start path and write its result to stdout.

    **Protocol data and human logs are separate streams, and that is the output
    contract rather than a convention.** The versioned result document is the whole
    of stdout, so an adapter can read it without a parser that skips prose; the
    child's own words -- which are a human diagnostic -- go to stderr. R004-08.

    `--endpoint` is required here even though it is optional for a serving run,
    because it is the address the started service will bind and advertise, and there
    is nothing sensible to default it to from inside the runtime.

    Exit code 0 covers both attaching and starting: an adapter that only checks the
    status line still learns which happened, and one that only checks the exit code
    learns whether it has a usable service.
    """
    if args.endpoint is None:
        sys.stderr.write("--managed-start needs --endpoint: the address to serve\n")
        return 2

    result = managed_start(
        workspace_root=args.workspace,
        installation_root=args.installation_state,
        endpoint_uri=args.endpoint,
        core_version=args.core_version,
        log_path=args.managed_start_log,
    )
    sys.stdout.write(render_result(result))
    sys.stdout.flush()
    if result.status is ManagedStartStatus.FAILED:
        sys.stderr.write(result.reason + "\n")
        if result.child_output:
            sys.stderr.write(result.child_output + "\n")
        return 1
    return 0


def main(
    argv: list[str] | None = None,
    *,
    resolve_credential: CredentialResolver | None = None,
) -> int:
    """Own one workspace until told to stop.

    `resolve_credential` is the trusted credential resolver seam. It is a parameter
    rather than something read from the environment or a file because this lane
    deliberately ships no credential store, no token format and no principal registry:
    whoever embeds this service supplies the resolver, and until one exists
    `--http-endpoint` refuses startup instead of serving an unauthenticated HTTP
    listener. The console-script entry point passes none, so no HTTP bind asked for
    through it can be resolvable and every run that would actually serve one exits 2.

    `--check-only` is outside that rule rather than an exception to it. It serves
    nothing, so it never reaches `_http_bind_to_serve`: the argument is not parsed,
    not validated and not bound, and the exit code reports the workspace alone.
    `--check-only --http-endpoint <anything>` therefore exits 0 on a ready
    workspace -- measured for a wildcard host, an `https` URL and unparseable text
    alike, each with empty stderr. The same is already true of `--endpoint`, which
    this mode has always ignored, and both flags now say so in their help text.

    That is the v0.6 decision rather than an unfinished wiring: HTTP is embedder-only
    and intentionally unreachable from the standard Core service, because no approved
    credential source or bearer-session resolver has been defined yet. It does not
    remove authenticated HTTP from the target architecture. See:

        docs/development/omnivia-core-staged-startup-and-embedder-only-http-2026-08-05.md

    The advertised endpoint has to be served by this process and the descriptor has
    to die with it. Returning 0 straight after printing readiness -- which is what
    this did -- released the storage lock and the SQLite connection on the way out
    while leaving a descriptor behind that named a ready service at a dead pid, so
    the next launcher discovered it, believed it and connected to nothing.
    """
    args = build_parser().parse_args(argv)

    if args.init:
        # First, because it is the only mode that can run before a workspace
        # exists. This process serves nothing, owns nothing beyond the bootstrap
        # itself, and starts nothing.
        return _init(args)

    if args.managed_start:
        # This process serves nothing and owns nothing. It arbitrates, may start an
        # independent service, waits for that service to answer, and exits. Every
        # branch below belongs to a process that *is* the service.
        return _managed_start(args)

    settings = ServiceSettings(
        workspace_root=args.workspace,
        installation_root=args.installation_state,
        core_version=args.core_version,
        endpoint=args.endpoint,
    )
    runner = ServiceRunner(settings)
    endpoint = None if args.check_only else _endpoint_to_serve(settings.endpoint)
    if not args.check_only and endpoint is None:
        # Refused before startup, not after. Blocking here would leave a live process
        # advertising readiness at an endpoint nothing listens on -- the same lie the
        # exit-immediately bug told, with a live pid behind it instead of a dead one.
        sys.stderr.write("refusing to serve: local service endpoint is invalid\n")
        return 2

    try:
        http_bind = None if args.check_only else _http_bind_to_serve(args.http_endpoint)
    except HttpTransportError:
        # The refusal's own message is not repeated: it is derived from the endpoint
        # string a caller supplied, and this one names the rule instead.
        sys.stderr.write(
            "refusing to serve: HTTP endpoint is not an accepted loopback endpoint\n"
        )
        return 2
    if http_bind is not None and resolve_credential is None:
        sys.stderr.write(
            "refusing to serve: HTTP needs a trusted credential resolver\n"
        )
        return 2

    def serve(started: ServiceRunner) -> None:
        """Start the endpoint, and hand its shutdown to the resource stack.

        Called by `start()` before the discovery descriptor is written, so a bind
        failure, an over-long socket path or a permissions refusal unwinds the whole
        startup instead of exiting with a ready descriptor pointing at a process that
        is about to die. The `evidence.search` projection is brought up on that same
        rule and for the same reason -- see the build below.
        """
        assert endpoint is not None and started.workspace_id is not None
        assert started.identity is not None
        assert started.connection is not None and started.generation is not None

        # The projection `evidence.search` is served from, brought level with the
        # workspace and materialised **before** anything can ask for it. This is the
        # service's own maintenance work and this is the only place it happens: here
        # the process holds the connection, the identity and the current fencing
        # generation at once, and no request path holds any of them.
        #
        # Ordering is the property. `serve` runs after every readiness precondition is
        # satisfied and before the endpoint binds or the discovery descriptor is
        # written, so a build or a materialisation that fails raises out of here, the
        # startup sequence unwinds, and nothing is ever advertised as ready over a
        # projection that is not there. The alternative -- start, then serve refusals,
        # or worse, fall back to an unindexed ordering -- publishes a service that
        # answers `evidence.search` with something this build does not claim to serve.
        build_search_projection(
            started.connection,
            started.identity,
            workspace_id=started.workspace_id,
            fencing_generation=started.generation,
            now_us=time.time_ns() // 1000,
        )
        open_search_projection(started.connection, workspace_id=started.workspace_id)

        dispatcher = Dispatcher.for_service_operations(
            Grant(
                principal=LOCAL_PRINCIPAL,
                workspaces=frozenset({started.workspace_id}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            started,
        )
        # The application session, constructed here and only here: once per served
        # endpoint, at startup, from facts this process already holds -- the configured
        # principal, the installation identity persisted under the installation-local
        # state root, and the one workspace this endpoint was launched to own. No field
        # of it comes from a request, and no handler builds one.
        installation_id = started.identity.installation_id
        registry = build_application_registry()
        application = ApplicationDispatcher(
            registry=registry,
            session=local_owner_session(
                principal_id=LOCAL_PRINCIPAL,
                installation_id=installation_id,
                workspace_id=started.workspace_id,
                # The production grant, as a literal at the wiring site. This is the
                # one place the local owner's authority is decided, and it is here
                # rather than inside `local_owner_session` so that widening it is a
                # visible edit to the service's own startup rather than a change of
                # default one module away. Scopes, capabilities and purposes all follow
                # from these names -- none of them is written out anywhere.
                operations=frozenset(
                    {
                        WORKSPACE_INSPECT_OPERATION,
                        EVIDENCE_SEARCH_OPERATION,
                        KNOWLEDGE_SEARCH_OPERATION,
                        MEMORY_SEARCH_OPERATION,
                        GRAPH_TRAVERSE_OPERATION,
                    }
                ),
            ),
            # `workspace_id` is set deliberately. This endpoint fronts exactly one
            # workspace, and setting it arms the seam's second, independent workspace
            # check: reaching this endpoint and naming a different workspace is refused
            # even if the session's grant ever widened.
            binding=ServiceBinding(
                installation_id=installation_id, workspace_id=started.workspace_id
            ),
            # The server's own snapshot of what this build supports, derived from the
            # handlers actually registered above. Never the per-response
            # `CapabilitySet`, which is built from the caller's own claimed version.
            supported_capabilities=server_capability_snapshot(registry),
            # Stated rather than inferred: a dispatch callable is handed a request and
            # nothing else, so it cannot see which adapter carried it. This names the
            # adapter this vertical is authorised behind.
            transport=LOCAL_TRANSPORT_ADAPTER,
            probe=dispatcher,
            # No caller-recording sink. This repository configures no logging at all,
            # and standing one up under this packet would be an observability substrate
            # with no design behind it; `None` states that as a choice rather than
            # leaving an argument out. The record itself is built and proved by the
            # lane's evidence, and wiring a sink is a successor lane.
            record=None,
            service=started,
        )
        # One router, handed to both transports. That is the whole of how HTTP shares
        # the probe router and the application dispatcher rather than growing its own:
        # there is one object, and neither transport knows the other exists.
        router = _router_for(started, application)
        server = LocalSocketServer(router=router, endpoint=endpoint)
        server.start()
        started.lifecycle.resources.push("socket_server", server.stop)
        if http_bind is not None:
            http = HttpListener(
                router=router,
                # The principal this endpoint's dispatcher acts as, so HTTP can
                # refuse a session for anyone else rather than run it as this one.
                principal=LOCAL_PRINCIPAL,
                resolver=resolve_credential,
                bind=http_bind,
            )
            http.start()
            started.lifecycle.resources.push("http_server", http.stop)

    report = runner.start(serve=None if args.check_only else serve)
    sys.stdout.write(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
    sys.stdout.flush()
    if not report.ready:
        runner.stop()
        return 1
    if args.check_only:
        runner.stop()
        return 0

    # Wait. SIGTERM is what a POSIX supervisor sends and SIGINT is what a terminal
    # sends; both mean stop, and both have to unwind through the same path as a clean
    # exit so the descriptor and the locks go with the process.
    #
    # SIGBREAK is the Windows member of that set, and it is not optional there.
    # Windows has no signal delivery: `os.kill(pid, SIGTERM)` calls `TerminateProcess`,
    # which runs no handler, so a supervisor using it would leave the descriptor
    # advertising a ready service at a pid that no longer exists. `CTRL_BREAK_EVENT`
    # raises SIGBREAK in the target and is the one stop signal a Windows caller can
    # send that this process can act on.
    stopping = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    stop_signals = [signal.SIGTERM, signal.SIGINT]
    windows_break = getattr(signal, "SIGBREAK", None)
    if windows_break is not None:
        stop_signals.append(windows_break)
    for signum in stop_signals:
        signal.signal(signum, request_stop)

    try:
        # A single indefinite wait() never returns to the bytecode loop, so on
        # Windows the pending signal set by the console control handler thread
        # is never serviced: Event.wait() with no timeout blocks in a native
        # WaitForSingleObject(INFINITE) call, and CPython only checks for
        # pending signals between bytecode instructions on the main thread.
        # POSIX does not need this -- a blocking syscall there is interrupted
        # (EINTR) and the signal runs immediately -- but polling is harmless
        # there too, so one path serves both platforms.
        while not stopping.wait(timeout=0.25):
            pass
    finally:
        # One unwind, in reverse acquisition order: the socket server was pushed onto
        # the same stack as the guard, lease, connection and lock.
        runner.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
