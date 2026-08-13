"""The stdio MCP server for OmniVia Core (R004-05, R004-06, R004-07).

Built on the official Model Context Protocol Python SDK v2. There is no
JSON-RPC, framing, session or capability code in this package and there must not
be: R004-05 makes the official SDK the sole MCP framework dependency, and rules
out both a bespoke stack and FastMCP.

**stdout is protocol-only, and the SDK is what makes it so.** `stdio_server()`
claims fd 0 and 1, serves the wire from private duplicates and points fd 1 at
stderr for the duration, so a stray `print` in any handler -- or in a child
process that inherits the descriptors -- misses the wire instead of tearing a
frame. A service the shared client starts on this server's behalf has its output
captured there as well, so it is contained twice over. No guard of this package's
own would be better than the transport's own claim on the descriptor, and a
second one would be a second thing to get wrong.

**Everything is decided before the transport opens.** The configuration is read,
the service is connected, and the workspace is agreed before `stdio_server()` is
entered, so a refusal is a startup failure on stderr with not one byte of
protocol written -- R004-07's protocol-safe diagnostic in its strongest form --
and tools are never advertised by a server that cannot serve them.

**The call path is composed by `omnivia-core-client`, not here.**
:class:`~omnivia_core_client.ServiceClient` reads the published descriptor,
picks the transport, negotiates versions and proves the endpoint is live; this
package holds no dial loop, no descriptor read, no transport choice and no
credential source. What was a `TransportFactory` seam is gone with the direct
transport construction it existed for: a connected :class:`ConnectedSession` is
what a test substitutes now, and it substitutes the same object production uses.

**Authority is the configuration's, never the model's.** The principal, the
workspace, the allowed purposes, the endpoint and the credential *reference* all
come from the trusted `omnivia.mcp-config.v1` document, which is read from an
explicit owner-private path before anything else happens. A tool call cannot
name any of them: :data:`RESERVED_ARGUMENTS` refuses the attempt by name and the
advertised closed schema refuses it again as an undeclared key, both before a
request exists, let alone a call.

**MCP does not own the lease and does not stop what it started.** Neither
appears below, and their absence is the implementation: there is no lease call,
no stop call and no shutdown hook. A service started here is an independent Core
service and outlives the stdio session, exactly as R004-07 requires. The one
thing this process does drop at shutdown is its own credential cache.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Mapping
from contextlib import redirect_stdout
from dataclasses import dataclass
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
    CredentialCache,
    CredentialResolver,
    Deadline,
    HttpServiceConfig,
    InstallationServiceConfig,
    ManagedStartError,
    ServiceClient,
    connect_managed_local,
)

from omnivia_core.contracts.v1 import (
    CapabilityRequirement,
    ClientIdentity,
    PrincipalClaim,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    codec,
    get_operation_metadata,
)
from omnivia_core_mcp import __version__
from omnivia_core_mcp.configuration import (
    McpConfiguration,
    McpConfigurationError,
    read_configuration,
)
from omnivia_core_mcp.manifest import (
    ExposedOperation,
    exposed_by_tool_name,
    input_schema,
    tools,
)

__all__ = [
    "CALL_TIMEOUT_SECONDS",
    "CLIENT_NAME",
    "CONNECT_TIMEOUT_SECONDS",
    "RESERVED_ARGUMENTS",
    "SERVER_NAME",
    "ConnectedSession",
    "StartupError",
    "build_server",
    "connect",
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

#: Budget for one connect, including the live probe behind it.
CONNECT_TIMEOUT_SECONDS: Final = 30.0

#: Budget for reaching a managed-local service, which is a different question
#: from reaching one that is already up: it covers the probe, a service being
#: started, whatever recovery or migration that service does before it reports
#: ready, and the probe again. Thirty seconds is a connect budget and would
#: expire in the middle of a cold start, leaving a service running that this
#: process has just refused to talk to.
MANAGED_START_TIMEOUT_SECONDS: Final = 180.0

#: Argument names a tool call may never carry, whatever a schema says.
#:
#: Every one of these is authority the trusted configuration fixes: who the
#: caller is, which workspace it reaches, what it may claim to be doing, what it
#: has been granted, whether it may write, where the service is and which
#: credential is presented there. The advertised schemas are closed and declare
#: none of them, so this is the second of two refusals rather than the only one
#: -- and it is the one that stays true if a canonical contract ever grows a
#: field with one of these names.
RESERVED_ARGUMENTS: Final[frozenset[str]] = frozenset(
    {
        "principal_id",
        "principal_claim",
        "claimed_principal_id",
        "claimed_roles",
        "workspace_id",
        "allowed_workspace_ids",
        "default_workspace_id",
        "purpose",
        "purposes",
        "allowed_purposes",
        "scopes",
        "grants",
        "granted_authority",
        "required_capabilities",
        "mutation_enabled",
        "endpoint",
        "endpoint_uri",
        "credential_reference",
        "credentials",
    }
)


class StartupError(Exception):
    """A fixed-text refusal raised before MCP initialization.

    Same contract as :class:`~omnivia_core_mcp.configuration.McpConfigurationError`
    and for the same reason: every one of these is decided before the stdio
    transport opens, reaches the host on stderr, and quotes no endpoint, path,
    workspace identifier or credential reference.
    """


_AMBIGUOUS_WORKSPACE: Final = (
    "the MCP configuration does not select one unambiguous workspace"
)
_WORKSPACE_MISMATCH: Final = (
    "the connected service does not serve the selected workspace"
)
#: The one thing a failed managed start is told, whatever failed. The shared
#: client collapses every cause -- an unrecognised installation layout, no
#: workspace, no service program, a launcher that would not answer or answered
#: with something unreadable, a start that never became reachable -- into one
#: payload-free refusal, and this is that refusal in this server's vocabulary
#: plus the instruction only an adapter can give. Naming which of the causes it
#: was would mean reporting a path, a launcher field or a child's output.
_MANAGED_START_UNREACHABLE: Final = (
    "the managed service could not be started for this configuration. If this "
    "installation has no workspace yet, run `omnivia init` to create one and "
    "start this server again: this server starts an existing workspace and "
    "creates none"
)
_NO_CREDENTIAL_RESOLVER: Final = (
    "remote service mode requires an injected trusted credential resolver"
)
_SERVICE_UNAVAILABLE: Final = "the configured service could not be connected"


@dataclass(frozen=True, slots=True)
class ConnectedSession:
    """One trusted configuration, one live service, and how it was reached.

    Built only by :func:`connect`, which is what makes the fields mean something
    together: `workspace_id` is the configuration's unambiguous selection *and*
    the descriptor the connected service answered with, checked equal before
    this exists. Frozen, because nothing serving a session may swap the service
    or the authority under it.

    `credentials` is the cache this process created for a remote endpoint, held
    for exactly one reason -- :meth:`clear_credentials` at shutdown and on every
    failed startup path. Local mode has none, and `None` is that fact rather
    than an empty one.
    """

    configuration: McpConfiguration
    client: ServiceClient
    workspace_id: str
    status: str
    credentials: CredentialCache | None = None

    def clear_credentials(self) -> None:
        """Drop any credential this process resolved. Safe to call twice."""
        if self.credentials is not None:
            self.credentials.clear()


def connect(
    configuration: McpConfiguration,
    *,
    credential_resolver: CredentialResolver | None = None,
) -> ConnectedSession:
    """Reach the service this configuration names, or refuse before MCP starts.

    The workspace is settled first and settled once. A configuration that
    allow-lists several workspaces without naming a default selects none of
    them, and this server takes no argument that would choose between them --
    so it refuses rather than picking, because picking is the decision R004-06
    keeps away from the model.

    `credential_resolver` is the host's, injected. There is no default, no
    environment lookup, no argv secret and no file beside the configuration: a
    remote endpoint with no resolver fails closed here.
    """
    workspace_id = configuration.selected_workspace_id
    if workspace_id is None:
        raise StartupError(_AMBIGUOUS_WORKSPACE)

    if configuration.service_mode == "managed_local":
        client, status = _connect_managed_local(configuration, workspace_id)
        session = ConnectedSession(
            configuration=configuration,
            client=client,
            workspace_id=workspace_id,
            status=status,
        )
    else:
        session = _connect_service_client(
            configuration, workspace_id, credential_resolver
        )

    if session.client.descriptor.workspace_id != workspace_id:
        # The one check both modes need and neither transport can make: a
        # service may be reachable, compatible and live, and still be serving a
        # workspace this configuration never allow-listed.
        session.clear_credentials()
        raise StartupError(_WORKSPACE_MISMATCH)
    return session


def _connect_managed_local(
    configuration: McpConfiguration, workspace_id: str
) -> tuple[ServiceClient, str]:
    """Hand the whole of managed-local startup to the shared client.

    Two lines of this adapter's own: the configuration says which installation
    and which workspace, and a budget says how long the whole of it may take.
    Everything else -- the descriptor read, the transport choice, the liveness
    probe, whether the layout authorises a start at all, locating and running the
    service program, reading its bounded result, and reconnecting afterwards --
    is :func:`~omnivia_core_client.connect_managed_local`'s, in the one package
    that owns it. There is no launcher, no path convention, no argv and no
    process control in this package, which is what
    `packages/omnivia-core-runtime/tests/phase2/test_service_and_adapters.py`
    asserts by reading this file.

    Its refusal is already a fixed payload-free sentence, and it is still
    translated rather than re-raised: what reaches a host on stderr is this
    server's own vocabulary about its own startup, with one instruction added
    that this adapter can give and the shared client deliberately cannot -- the
    client does not know that `omnivia init` is the command, and must not carry
    a CLI's name.
    """
    state = configuration.installation_state
    if state is None:  # pragma: no cover - the configuration model forbids it
        raise StartupError(_SERVICE_UNAVAILABLE)
    service_config = InstallationServiceConfig(
        installation_state=state, workspace_id=workspace_id
    )
    deadline = Deadline.after(MANAGED_START_TIMEOUT_SECONDS)
    try:
        connected = connect_managed_local(service_config, deadline=deadline)
    except ManagedStartError:
        pass
    else:
        return connected.client, connected.status
    raise StartupError(_MANAGED_START_UNREACHABLE)


def _connect_service_client(
    configuration: McpConfiguration,
    workspace_id: str,
    credential_resolver: CredentialResolver | None,
) -> ConnectedSession:
    """Connect to the configured HTTP endpoint with the host's own resolver.

    The configuration carries the *name* of a credential and the normalized
    origin it may be presented to; the secret exists only inside the cache, only
    for as long as its TTL, and only because a resolver this process was handed
    answered for that pair. A startup that does not complete drops the cache on
    the way out, so a refused start leaves nothing resolved behind it.
    """
    if credential_resolver is None:
        raise StartupError(_NO_CREDENTIAL_RESOLVER)
    endpoint = configuration.endpoint
    reference = configuration.credential_reference
    if endpoint is None or reference is None:  # pragma: no cover - model forbids it
        raise StartupError(_SERVICE_UNAVAILABLE)

    credentials = CredentialCache(credential_resolver)
    connected: ServiceClient | None = None
    try:
        connected = ServiceClient.connect(
            HttpServiceConfig(
                endpoint_uri=endpoint,
                credential_reference=reference,
                credentials=credentials,
            ),
            deadline=Deadline.after(CONNECT_TIMEOUT_SECONDS),
        )
    finally:
        if connected is None:
            credentials.clear()
    if connected is None:
        raise StartupError(_SERVICE_UNAVAILABLE)
    return ConnectedSession(
        configuration=configuration,
        client=connected,
        workspace_id=workspace_id,
        status="connected",
        credentials=credentials,
    )


def build_server(*, session: ConnectedSession) -> Server[object]:
    """The MCP server for one connected Core service.

    Everything authority-shaped is already settled in `session`, so there is no
    factory, no endpoint and no credential argument here any more: a test that
    wants a different service connects a different session, which is the same
    object this takes in production.
    """

    async def on_list_tools(
        _context: object, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        """Every allow-listed tool, in manifest order, on every call.

        R004-06 requires this to be deterministic for a given package version. It
        is built once at import in :mod:`omnivia_core_mcp.manifest` and returned
        as it stands: nothing is filtered, sorted, or read from the environment
        here, and neither the session nor the configuration is consulted. In
        particular the allowed-purpose set does *not* filter the listing -- a
        listing that varied with the authority granted to one host would not be
        deterministic, and the purpose is enforced on call instead, which is
        where refusing it is a decision rather than a disappearance.
        """
        return types.ListToolsResult(tools=list(tools()))

    async def on_call_tool(
        _context: object, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        return _call_tool(params, session=session)

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
    params: types.CallToolRequestParams, *, session: ConnectedSession
) -> types.CallToolResult:
    """Dispatch one tool call against the allow-list and the configured authority.

    Three refusals come before anything is sent, in this order and for three
    different reasons. The allow-list is the *only* lookup, so an operation
    absent from the manifest is not callable rather than merely unadvertised.
    The manifest's purpose must be one the configuration allows, so a host
    granted `workspace_inspection` alone cannot retrieve knowledge with a tool
    it can see. And the payload must be one the advertised schema declares, with
    no authority-shaped key anywhere in it.

    None of the three reaches the client, so none of them costs a dial, a
    credential resolution or a service round trip.
    """
    exposed = exposed_by_tool_name(params.name)
    if exposed is None:
        return _failure(
            f"{params.name!r} is not a tool this server exposes. "
            f"Available: {', '.join(tool.name for tool in tools())}."
        )

    if exposed.purpose not in session.configuration.allowed_purposes:
        return _failure(
            f"{params.name} states a purpose this server's configuration does "
            "not allow, so it was not called."
        )

    try:
        request = _request(
            exposed,
            configuration=session.configuration,
            workspace_id=session.workspace_id,
            arguments=params.arguments,
        )
    except ValueError as refusal:
        return _failure(f"{params.name}: {refusal}")

    try:
        response = session.client.call(
            request, deadline=Deadline.after(CALL_TIMEOUT_SECONDS)
        )
    except ClientError as failure:
        # Every way a client call is documented to fail: it could not carry the
        # call, what came back was not a frame, a credential did not resolve,
        # the deadline passed, or the caller cancelled. All of them are answers
        # to give a model, not tracebacks. The client's diagnostics are
        # payload-free by construction, so passing one through to a model quotes
        # no workspace content, no endpoint and no local path.
        return _failure(f"{params.name} could not be called: {failure}")

    if not _correlates(request, response):
        # Before either branch below publishes anything: an answer that does not
        # carry this request's correlation identifier is not this request's
        # answer, and publishing it as `structuredContent` would attribute one
        # call's result to another.
        return _failure(
            f"{params.name} was answered by a response that does not correlate "
            "with the request, so the answer was not published."
        )

    if not isinstance(response, SuccessResponseEnvelope):
        return _failure(
            f"{params.name} was refused by the service: "
            f"{codec.to_canonical_json(codec.encode_response(response))}"
        )
    # Through the codec's own encoder, not `dict(response.result)`. `dict()`
    # converts the top level only, and a decoded envelope carries read-only
    # mappings further down -- `to_canonical_json` is `json.dumps`, which refuses
    # a nested `mappingproxy` outright. `encode_response` is the accepted way to
    # get a wire-shaped document, and is what the CLI has always used.
    encoded = codec.encode_response(response)["result"]
    if not isinstance(encoded, dict):
        # Refused rather than substituted. This read `encoded if isinstance(...)
        # else {}`, which published an empty success document for a shape the
        # advertised output schema does not describe -- inert while no schema was
        # advertised, and a lie the moment one is.
        return _failure(
            f"{params.name} returned a result this server cannot publish: the "
            f"encoded result is {type(encoded).__name__}, not a JSON object"
        )
    return types.CallToolResult(
        # The contract-encoded result itself. The advertised `output_schema`
        # describes exactly this document, and the official client validates it
        # against that schema on every successful call.
        structured_content=encoded,
        # Exactly one text item, and it is the same document: a host that predates
        # structured content still gets the whole answer, and one that has it can
        # check the two agree. Serialised through the codec's canonical encoder, so
        # the mirror is byte-stable rather than dict-order-dependent.
        content=[types.TextContent(type="text", text=codec.to_canonical_json(encoded))],
    )


def _correlates(request: RequestEnvelope, response: ResponseEnvelope) -> bool:
    """Whether this response is an answer to this request.

    Both identifiers are checked because both are this process's own: the
    request id and the correlation id are minted together below, and a peer that
    echoes neither -- or echoes one from an earlier call -- has not answered the
    call being published.
    """
    return (
        response.metadata.correlation_id == request.metadata.correlation_id
        and response.metadata.request_id == request.metadata.request_id
    )


def _failure(message: str) -> types.CallToolResult:
    """A refusal the model can read, as a tool error rather than an exception.

    `is_error` rather than a raise: an exception out of a handler is a protocol
    error the model never sees the text of, and every refusal here is information
    it should act on -- a wrong tool name, a purpose it was not granted, an
    unavailable call, a service that said no.
    """
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )


def _request(
    exposed: ExposedOperation,
    *,
    configuration: McpConfiguration,
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

    **The principal and the workspace come from the trusted configuration.** The
    principal claim is exactly `principal_id` from the configuration document and
    is a *claim*: the service decides from its own grant, and a claim that is not
    granted is refused there. The workspace is the one unambiguous allow-listed
    selection the session connected to and proved the service serves.

    **A model supplies neither, and cannot.** :data:`RESERVED_ARGUMENTS` refuses
    an authority-shaped key by name, and the advertised schema's own `properties`
    refuses every key it does not declare -- read off the projection rather than
    from a literal list, so what `tools/list` says and what this accepts stay one
    document. Every advertised payload declares `unevaluatedProperties: false`,
    so a key outside that set is one the contract refuses anyway; refusing it
    here costs the model a round trip to find that out. Value-level validation
    stays where it belongs: the service validates the payload against the
    operation contract and answers with its own typed refusal.
    """
    supplied = set(arguments or {})
    reserved = sorted(supplied & RESERVED_ARGUMENTS)
    if reserved:
        raise ValueError(
            f"{exposed.tool_name} does not take {reserved[0]!r}: the principal, "
            "the workspace, the purpose, the granted authority, the endpoint and "
            "the credential are fixed by this server's trusted configuration and "
            "cannot be set by a caller"
        )
    entry = get_operation_metadata(exposed.operation)
    advertised = set(input_schema(entry)["properties"])
    unknown = sorted(supplied - advertised)
    if unknown:
        raise ValueError(
            f"{exposed.tool_name} accepts no argument named {unknown[0]!r}"
            + (f" (or {len(unknown) - 1} other(s))" if len(unknown) > 1 else "")
            + f"; its advertised schema declares {sorted(advertised)} and is closed"
        )
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
            principal_claim=PrincipalClaim(
                claimed_principal_id=configuration.principal_id
            ),
        ),
        input=dict(arguments or {}),
    )


async def serve(*, session: ConnectedSession) -> None:
    """Serve one stdio session against an already-connected service.

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
    server = build_server(session=session)
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
            "Serve one OmniVia Core workspace to an MCP host over stdio. "
            "Read-only, and never creates a workspace."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help=(
            "absolute path to the trusted omnivia.mcp-config.v1 file. It must be "
            "a regular owner-private file, and it is the only place the "
            "principal, the workspace allow-list, the allowed purposes and the "
            "service location come from. There is no default, no environment "
            "variable and no fallback."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Read the trusted configuration, connect, then serve stdio.

    Writes nothing to stdout on any path: the startup line and every diagnostic
    go to stderr and the exit status carries the outcome. That is R004-07's
    protocol-safe failure in its strongest form -- a host reading this process's
    stdout sees either valid MCP or nothing at all.

    **There is no ambient credential resolver here, and that is the design.**
    A console process holds no trusted way to release a secret, so a
    configuration in `service_client` mode fails closed at :func:`connect`
    rather than reaching for an environment variable, an argv secret or a file
    beside the configuration. A host that has a resolver calls :func:`connect`
    and :func:`serve` itself and injects one.
    """
    args = build_parser().parse_args(argv)
    try:
        session = connect(read_configuration(args.config))
    except (
        McpConfigurationError,
        StartupError,
        ManagedStartError,
        ClientError,
    ) as refusal:
        sys.stderr.write(f"{refusal}\n")
        return 1

    sys.stderr.write(f"{SERVER_NAME}: {session.status} {session.workspace_id}\n")
    try:
        anyio.run(lambda: serve(session=session))
    finally:
        session.clear_credentials()
    return 0


if __name__ == "__main__":  # pragma: no cover - module execution shim
    raise SystemExit(main())
