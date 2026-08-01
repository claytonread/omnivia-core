"""`omnivia-core-service` entry point (T-0629G).

Minimal by design. This owns and advertises one writable workspace and nothing
else. A2 has frozen the product operation catalogue, but building against it is the
separately approved Phase 4 packet, so no product operation is registered here.
Health, readiness and discovery are deliberately distinct from product operations,
per ADR-037.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.transport import (
    LOCAL_SCHEME,
    LocalEndpoint,
    LocalSocketServer,
    parse_endpoint,
)

#: The service serves the OS user that started it. There is no multi-principal
#: story yet, and inventing one here would be a security surface with no design
#: behind it.
LOCAL_PRINCIPAL = "local-user"


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
        "--core-version", default="0.1.0", help="Core version for compatibility checks"
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="run startup, report readiness as JSON, then stop without serving",
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


def main(argv: list[str] | None = None) -> int:
    """Own one workspace until told to stop.

    The advertised endpoint has to be served by this process and the descriptor has
    to die with it. Returning 0 straight after printing readiness -- which is what
    this did -- released the storage lock and the SQLite connection on the way out
    while leaving a descriptor behind that named a ready service at a dead pid, so
    the next launcher discovered it, believed it and connected to nothing.
    """
    args = build_parser().parse_args(argv)
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
        sys.stderr.write(
            f"refusing to serve: --endpoint must be a {LOCAL_SCHEME.value}:// endpoint "
            f"on this platform, got {settings.endpoint!r}. Use --check-only to run "
            "startup without serving.\n"
        )
        return 2

    def serve(started: ServiceRunner) -> None:
        """Start the endpoint, and hand its shutdown to the resource stack.

        Called by `start()` before the discovery descriptor is written, so a bind
        failure, an over-long socket path or a permissions refusal unwinds the whole
        startup instead of exiting with a ready descriptor pointing at a process that
        is about to die.
        """
        assert endpoint is not None and started.workspace_id is not None
        server = LocalSocketServer(
            dispatcher=Dispatcher.for_service_operations(
                Grant(
                    principal=LOCAL_PRINCIPAL,
                    workspaces=frozenset({started.workspace_id}),
                    operations=frozenset(SERVICE_OPERATIONS),
                ),
                started,
            ),
            endpoint=endpoint,
        )
        server.start()
        started.lifecycle.resources.push("socket_server", server.stop)

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
        stopping.wait()
    finally:
        # One unwind, in reverse acquisition order: the socket server was pushed onto
        # the same stack as the guard, lease, connection and lock.
        runner.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
