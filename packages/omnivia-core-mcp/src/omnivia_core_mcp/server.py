"""The stdio MCP server for OmniVia Core (R004-05, R004-06, R004-07).

Built on the official Model Context Protocol Python SDK v2. There is no
JSON-RPC, framing, session or capability code in this package and there must not
be: R004-05 makes the official SDK the sole MCP framework dependency, and rules
out both a bespoke stack and FastMCP.

**stdout is protocol-only, and the SDK is what makes it so.** `stdio_server()`
claims fd 0 and 1, serves the wire from private duplicates and points fd 1 at
stderr for the duration, so a stray `print` in any handler -- or in a child
process that inherits the descriptors -- misses the wire instead of tearing a
frame. The managed-start subprocess is captured as well, so its output is
contained twice over. No guard of this package's own would be better than the
transport's own claim on the descriptor, and a second one would be a second thing
to get wrong.

**Managed start runs before the transport opens, on purpose.** R004-07 requires
readiness before tools are advertised and a protocol-safe diagnostic when the
service cannot be made ready. Failing before `stdio_server()` is entered
satisfies both in the strongest available way: the tools are never advertised
because the server never starts, and the diagnostic is protocol-safe because not
one byte of protocol was written. The message goes to stderr and the process
exits non-zero, which is what an MCP host surfaces to its user.

**MCP does not own the lease and does not stop what it started.** Neither
appears below, and their absence is the implementation: there is no lease call,
no stop call and no shutdown hook. A service started here is an independent Core
service and outlives the stdio session, exactly as R004-07 requires.

**The call path is complete, and the transport is not this package's.**
`omnivia-core-client` exports the :class:`~omnivia_core_client.ClientTransport`
protocol, the OVC1 frame, deadlines, discovery -- and, since owner resolution 005
R005-01, :class:`~omnivia_core_client.LocalIpcTransport`, the one concrete local
transport in the repository. This package constructs it in
:func:`_default_transport_factory` and holds no dial loop of its own. It does not
import the CLI, which ADR-036 forbids, and a sibling copy would have been the
third implementation of one dial loop -- the shape R004-08 rejected for process
control, and the shape that let the CLI's socket-path guard drift 11 to 15 bytes
from the runtime's.

:data:`TransportFactory` remains the seam, now for tests rather than for a pending
decision: the server takes one, every call goes through it, and a test can pass a
double instead of standing up a service.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Final

import anyio
import mcp_types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from omnivia_core_client import (
    CLIENT_API_VERSION,
    ClientError,
    ClientTransport,
    Deadline,
    LocalIpcTransport,
)

from omnivia_core.contracts.v1 import (
    CapabilityRequirement,
    ClientIdentity,
    RequestEnvelope,
    RequestMetadata,
    SuccessResponseEnvelope,
    codec,
    get_operation_metadata,
)
from omnivia_core_mcp import __version__
from omnivia_core_mcp.managed_start import (
    Attachment,
    Installation,
    ManagedStartError,
    ensure_service,
    home_directory,
)
from omnivia_core_mcp.manifest import ExposedOperation, exposed_by_tool_name, tools

__all__ = [
    "CALL_TIMEOUT_SECONDS",
    "CLIENT_NAME",
    "SERVER_NAME",
    "TransportFactory",
    "build_server",
    "main",
    "serve",
]

#: The name this server advertises to a host. Stable MCP-facing vocabulary.
SERVER_NAME: Final = "omnivia-core"

#: This adapter's self-declared identity on every request. Diagnostic only, never
#: an authorization input, and distinct from the CLI's so a service log can tell
#: an agent's call from a human's.
CLIENT_NAME: Final = "omnivia-core-mcp"

#: Budget for one application call. A call budget, not a startup budget: the
#: service is already ready by the time any of these are made.
CALL_TIMEOUT_SECONDS: Final = 30.0

#: Constructs a transport for one endpoint URI. The seam between this server and
#: the client-owned local transport, kept so a test can substitute a double; see
#: the module docstring.
TransportFactory = Callable[[str], ClientTransport]


def _default_transport_factory(endpoint_uri: str) -> ClientTransport:
    """Construct the client-owned local transport for an endpoint.

    Owner resolution 005 R005-01 moved `LocalIpcTransport` into
    `omnivia-core-client`, and this is the whole of what that changed here: the
    seam was already in place, so constructing the real transport is the entire
    edit. Every caller already goes through :data:`TransportFactory`, the request
    envelopes are already built from the catalogue, and the response decoding
    already runs against the public contract.

    This package imports the transport from the client distribution it already
    declares. It does not import the CLI, and there is no copy of the dial loop
    here -- which is the point of the move.

    Nothing is caught here. A bad endpoint URI raises `TransportError` from
    `socket_path_for` at call time, not at construction: the dataclass is frozen
    and holds only the URI, so constructing it cannot fail. Either way the caller's
    `ClientError` handler is what answers.
    """
    return LocalIpcTransport(endpoint_uri=endpoint_uri)


def build_server(
    *,
    attachment: Attachment,
    transport_factory: TransportFactory | None = None,
) -> Server[object]:
    """The MCP server for one attached Core service.

    `transport_factory` defaults to the one that cannot be built yet, so a
    production server advertises its tools honestly and reports the missing dial
    when one is called. A test passes a double satisfying `ClientTransport`.
    """
    factory = (
        _default_transport_factory if transport_factory is None else transport_factory
    )

    async def on_list_tools(
        _context: object, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        """Every allow-listed tool, in manifest order, on every call.

        R004-06 requires this to be deterministic for a given package version. It
        is built once at import in :mod:`omnivia_core_mcp.manifest` and returned
        as it stands: nothing is filtered, sorted, or read from the environment
        here, and the attachment is not consulted -- a listing that varied with
        the service it happened to reach would not be deterministic.
        """
        return types.ListToolsResult(tools=list(tools()))

    async def on_call_tool(
        _context: object, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        return _call_tool(
            params, attachment=attachment, transport_factory=factory
        )

    return Server(
        SERVER_NAME,
        version=__version__,
        title="OmniVia Core",
        instructions=(
            "Read-only access to a local OmniVia Core workspace. Every tool is "
            "explicitly allow-listed; service lifecycle, workspace creation and "
            "every mutation are deliberately absent and cannot be called."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def _call_tool(
    params: types.CallToolRequestParams,
    *,
    attachment: Attachment,
    transport_factory: TransportFactory,
) -> types.CallToolResult:
    """Dispatch one tool call against the allow-list, or refuse it.

    The allow-list is the *only* lookup: there is no path from a tool name to an
    operation that does not go through it, so an operation absent from the
    manifest is not callable rather than merely unadvertised. A model that guesses
    `workspace_create` gets this refusal and no request is built, let alone sent.
    """
    exposed = exposed_by_tool_name(params.name)
    if exposed is None:
        return _failure(
            f"{params.name!r} is not a tool this server exposes. "
            f"Available: {', '.join(tool.name for tool in tools())}."
        )

    try:
        request = _request(exposed, workspace_id=attachment.workspace_id,
                           arguments=params.arguments)
    except ValueError as refusal:
        return _failure(f"{params.name}: {refusal}")

    try:
        transport = transport_factory(attachment.endpoint_uri)
        response = transport.call(
            request, deadline=Deadline.after(CALL_TIMEOUT_SECONDS)
        )
    except ClientError as failure:
        # Every way a transport is documented to fail: it could not carry the
        # call, what came back was not a frame, the deadline passed, or the
        # caller cancelled. All four are answers to give a model, not tracebacks.
        # The client's diagnostics are payload-free by construction, so passing
        # one through to a model quotes no workspace content and no local path.
        return _failure(f"{params.name} could not be called: {failure}")

    if not isinstance(response, SuccessResponseEnvelope):
        return _failure(
            f"{params.name} was refused by the service: "
            f"{codec.to_canonical_json(codec.encode_response(response))}"
        )
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=codec.to_canonical_json(dict(response.result)),
            )
        ]
    )


def _failure(message: str) -> types.CallToolResult:
    """A refusal the model can read, as a tool error rather than an exception.

    `is_error` rather than a raise: an exception out of a handler is a protocol
    error the model never sees the text of, and every refusal here is information
    it should act on -- a wrong tool name, an unavailable call, a service that
    said no.
    """
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )


def _request(
    exposed: ExposedOperation,
    *,
    workspace_id: str,
    arguments: Mapping[str, Any] | None,
) -> RequestEnvelope:
    """A contract-valid envelope for one allow-listed operation.

    **Scopes and the capability requirement are read off the frozen catalogue
    entry, never transcribed.** The catalogue validator refuses a request that
    declares no capability, or the wrong one, and the service builds its own
    session from that same entry -- so both ends of the call read one source and
    a renamed capability fails as a rename rather than as a mystery refusal two
    files away.

    `workspace_id` comes from the attachment, which came from the launcher, which
    read it from the running service. A model cannot name a workspace: there is no
    argument for one anywhere in this package, which is R004-06's "unrestricted
    filesystem path selection" boundary enforced by there being no path to select.
    """
    entry = get_operation_metadata(exposed.operation)
    required = entry.required_capability
    request_id = f"mcp-{uuid.uuid4()}"
    return RequestEnvelope(
        operation=exposed.operation,
        metadata=RequestMetadata(
            request_id=request_id,
            correlation_id=request_id,
            trace_id=request_id,
            api_version=CLIENT_API_VERSION,
            client=ClientIdentity(id=CLIENT_NAME, version=__version__),
            workspace_id=workspace_id,
            scopes=tuple(entry.scope.required_scopes),
            # A claim, not authority: the service decides from its own grant.
            purpose=exposed.purpose,
            required_capabilities=(
                CapabilityRequirement(
                    id=required.id,
                    minimum_version=required.minimum_version,
                    required=required.required,
                ),
            ),
        ),
        input=dict(arguments or {}),
    )


async def serve(
    *,
    attachment: Attachment,
    transport_factory: TransportFactory | None = None,
) -> None:
    """Serve one stdio session for an already-attached service.

    The `redirect_stdout` closes a real gap the SDK's descriptor claim cannot.
    `stdio_server()` has by this point taken its private duplicate of fd 1 for the
    wire and pointed fd 1 itself at stderr, so a *flushed* write from a handler
    already misses the protocol stream. An unflushed one does not: `print()` to a
    pipe is block-buffered, the bytes sit in `sys.stdout`'s buffer for the life of
    the session, and the interpreter flushes them at shutdown -- after the claim
    is released and fd 1 is back on the wire. They then land on the real stdout as
    trailing garbage behind a closed session. Rebinding the object here means
    those bytes never enter that buffer at all. Verified by
    `test_every_byte_the_server_writes_to_stdout_is_valid_protocol`, which caught
    exactly this leak.
    """
    server = build_server(
        attachment=attachment, transport_factory=transport_factory
    )
    async with stdio_server() as (read_stream, write_stream):
        with redirect_stdout(sys.stderr):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name=SERVER_NAME,
                    server_version=__version__,
                    # Every notification option left at its default `False`. The
                    # tool list is frozen at import for a given package version --
                    # that is R004-06's determinism requirement -- so a
                    # `tools/list_changed` capability would advertise an event
                    # this server can never send.
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnivia-core-mcp",
        description=(
            "Serve a local OmniVia Core workspace to an MCP host over stdio. "
            "Read-only, and never creates a workspace."
        ),
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help=(
            "the OmniVia installation root. Defaults to ~/.omnivia. There is no "
            "environment variable for this and there will not be one."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Resolve the installation, ensure a service, then serve stdio.

    Writes nothing to stdout on any failure path: every diagnostic goes to stderr
    and the exit status carries the outcome. That is R004-07's protocol-safe
    failure in its strongest form -- a host reading this process's stdout sees
    either valid MCP or nothing at all.
    """
    args = build_parser().parse_args(argv)
    installation = Installation(home=home_directory(args.home))
    try:
        attachment = ensure_service(installation)
    except ManagedStartError as refusal:
        sys.stderr.write(f"{refusal}\n")
        return 1

    sys.stderr.write(
        f"{SERVER_NAME}: {attachment.status} {attachment.workspace_id} at "
        f"{attachment.endpoint_uri}\n"
    )
    anyio.run(lambda: serve(attachment=attachment))
    return 0


if __name__ == "__main__":  # pragma: no cover - module execution shim
    raise SystemExit(main())
