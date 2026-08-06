"""The `omnivia` CLI (B10).

A Core Service client and nothing more. It discovers a service, builds contract
envelopes and reports what it is told. It holds no lease, takes no lock and opens no
database.

`workspace show` is the first subcommand that actually *calls* a service rather
than printing the envelope it would have sent. It reaches the authorised
application path: `workspace.inspect`, under the fixed local-owner session the
service constructs for itself at startup, over the installation-local OVC1
endpoint.

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
import uuid
from pathlib import Path

from omnivia_core.contracts.v1 import (
    CapabilityRequirement,
    ServiceEndpointDescriptor,
    codec,
    get_operation_metadata,
)
from omnivia_core_cli.client import build_request, encode, read_descriptor

#: The whole-call budget for one `workspace show`, covering discovery's live probe
#: and the request itself. A local call that has not answered in this long is not
#: about to.
CALL_TIMEOUT_SECONDS = 10.0

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
        required=True,
        type=Path,
        help="installation runtime directory holding service.json",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("discover", help="show the discovered service, if any")

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


def _workspace_show(runtime_state: Path, service: ServiceEndpointDescriptor) -> int:
    """Call `workspace.inspect` on the discovered service and render the answer.

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
    not style. The subcommands that predate this one are proven on a
    three-operating-system matrix that installs this distribution but not
    `omnivia-core-client`; a module-scope import would make
    `omnivia_core_cli.main` unimportable there and take `discover`, `health` and
    `readiness` down with it.
    """
    from omnivia_core_client import ClientError, Deadline, discover_endpoint

    from omnivia_core_cli.transport import LocalIpcTransport

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
        sys.stderr.write("the advertised service did not pass its discovery checks\n")
        return 1
    if discovered is None:
        sys.stderr.write("no service is advertised; start omnivia-core-service first\n")
        return 1
    if discovered.descriptor != service:
        # Two descriptors, so the checks above were applied to a file this call
        # would not have used. Refuse rather than pick one.
        sys.stderr.write("the advertised service did not pass its discovery checks\n")
        return 1

    scopes, required_capabilities = _inspect_claims()
    request = build_request(
        WORKSPACE_INSPECT_OPERATION,
        workspace_id=discovered.descriptor.workspace_id,
        request_id=f"cli-{uuid.uuid4()}",
        scopes=scopes,
        purpose=WORKSPACE_INSPECTION_PURPOSE,
        required_capabilities=required_capabilities,
    )
    try:
        response = transport.call(request, deadline=deadline)
    except ClientError:
        sys.stderr.write("the service did not answer\n")
        return 1

    if response.metadata.correlation_id != request.metadata.correlation_id:
        # The answer correlates to a different request. On a strictly unary
        # connection that should be impossible, which is the reason to say so
        # rather than to render it: an answer that does not correlate is not this
        # call's answer, whatever it contains.
        sys.stderr.write("the service answered a different request\n")
        return 1

    error = getattr(response, "error", None)
    if error is not None:
        # Only the service's own code and message. Echoing any part of the
        # request back would put a caller-supplied value on the refusal surface,
        # which is the one thing a refusal must not carry.
        sys.stderr.write(f"{error.code}: {error.message}\n")
        return 1

    # Rendered from the wire form the public codec produces, not from the decoded
    # envelope's attributes. The decoded object holds read-only mapping views that
    # `json` cannot serialise, and reaching past them field by field would be this
    # CLI's own second opinion about the shape of a contract result.
    sys.stdout.write(
        json.dumps(codec.encode_response(response)["result"], indent=2, sort_keys=True)
        + "\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = read_descriptor(args.runtime_state)

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
                    "ready": service.ready,
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
        return _workspace_show(args.runtime_state, service)

    request = build_request(
        f"core.{args.command}",
        workspace_id=service.workspace_id,
        request_id=f"cli-{uuid.uuid4()}",
        principal=args.principal,
    )
    sys.stdout.write(encode(request) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
