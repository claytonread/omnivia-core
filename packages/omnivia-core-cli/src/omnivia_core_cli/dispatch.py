"""Turning one frozen command into one call on an already-connected client.

`surface.py` says what the commands *are*; this module is the only place that
turns one of them into a request and sends it. It owns four decisions and no
others.

*The catalogue is the source of the claims.* Scopes and the required capability
are read off the frozen `OperationMetadata` for `command.operation` --
:func:`~omnivia_core.contracts.v1.get_operation_metadata`, never transcribed --
and the capability requirement is passed through as the very object the
catalogue holds. The service builds its own session from that same entry, so a
renamed capability fails as a rename rather than as a refusal two files away,
and this CLI cannot claim a requirement the catalogue does not state.

*An answer must be an answer to the call that was made.* A request id is minted
here when the caller supplies none, and it is the correlation and trace id too;
a response echoing a different request or correlation id is refused rather than
returned, because a caller reading it would be reading some other call's result.
The probe path makes the same check against the id the runtime echoes under
`details` -- `ServiceProbeResult` has no `request_id` field of its own, so
`details["request_id"]` is the only place the accepted DTO permits the echo, and
it is the caller's own value coming back rather than any server-side
observation.

*Transport belongs to the caller.* An already-connected
:class:`~omnivia_core_client.ServiceClient` is injected. Nothing here discovers
an endpoint, reads a descriptor file, chooses a transport, opens a process or
touches the filesystem, and the deadline and cancellation token are forwarded
exactly as they arrived so the whole-call budget the caller set is the one the
wire waits on.

*Failures that are the peer's stay the peer's.* :class:`DispatchError` is raised
only for the two refusals this module owns -- an unknown command path and an
uncorrelated answer -- and carries one fixed sentence with nothing from a
payload, a peer, an operation or an identifier in it. Every other failure is
passed through untouched: the client's transport, deadline and cancellation
errors, and application error envelopes, which are answers the service gave and
not exceptions to raise.

Imports reach the public contracts and the client package only. Neither the
runtime nor the MCP adapter is importable from here, and no filesystem,
subprocess or storage API is used.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Final

from omnivia_core_client import CancellationToken, Deadline, ServiceClient

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    SCOPE_KIND_INSTALLATION,
    SCOPE_KIND_WORKSPACE,
    ClientIdentity,
    MutationPrecondition,
    PrincipalClaim,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
    ServiceProbeRequest,
    ServiceProbeResult,
    get_operation_metadata,
)
from omnivia_core_cli.surface import (
    APPLICATION_COMMANDS,
    PROBE_COMMANDS,
    ApplicationCommand,
    ProbeCommand,
)

__all__ = [
    "DispatchError",
    "dispatch_application",
    "dispatch_probe",
    "find_application_command",
    "find_probe_command",
]

#: This distribution's identity, as claimed on every request. Literal because
#: neither half is a contract version: the API version beside it is
#: `CONTRACT_VERSION` and is derived rather than restated.
_CLIENT_ID: Final = "omnivia-cli"
_CLIENT_VERSION: Final = "0.1.0"

#: The prefix on every request id this CLI mints, so a service-side log can tell
#: which client a call came from without a payload field for it.
_REQUEST_ID_PREFIX: Final = "cli"

#: The key the runtime echoes a probe request id under. `ServiceProbeResult`
#: carries no `request_id` field, so `details` is where the echo lands.
_REQUEST_ID_DETAIL: Final = "request_id"

#: The one thing a refusal from this module says. Fixed and payload-free: the
#: cases it covers are an unrecognised command path and an answer that does not
#: correlate, and naming the path, the operation, the identifiers or anything the
#: peer sent would put caller or peer material into an exception a host may log.
_DIAGNOSTIC: Final = "the CLI did not dispatch this call"

_APPLICATION_BY_PATH: Final[Mapping[tuple[str, ...], ApplicationCommand]] = {
    command.path: command for command in APPLICATION_COMMANDS
}
_PROBE_BY_PATH: Final[Mapping[tuple[str, ...], ProbeCommand]] = {
    command.path: command for command in PROBE_COMMANDS
}


class DispatchError(Exception):
    """A call this module refused to make, or an answer it refused to return.

    Takes no argument and carries no detail, so there is no way to construct one
    that quotes a payload, an endpoint, an identifier or a peer's own words. The
    caller learns *that* the CLI would not dispatch, and the exit code it maps to
    is the surface's business rather than this exception's.
    """

    def __init__(self) -> None:
        super().__init__(_DIAGNOSTIC)


def find_application_command(path: tuple[str, ...]) -> ApplicationCommand:
    """The frozen application command at exactly `path`.

    Exact tuple lookup and nothing else: no prefix match, no abbreviation, no
    case folding, no nearest-neighbour suggestion. A path the surface does not
    declare is refused, because guessing at one would dispatch an operation the
    user did not name.
    """
    command = _APPLICATION_BY_PATH.get(path)
    if command is None:
        raise DispatchError
    return command


def find_probe_command(path: tuple[str, ...]) -> ProbeCommand:
    """The frozen probe command at exactly `path`, refusing anything else.

    Separate from :func:`find_application_command` for the reason the surface
    keeps the two lists apart: a probe is not a catalogue operation, carries no
    purpose, scope or capability, and is answerable before any of those exist.
    """
    command = _PROBE_BY_PATH.get(path)
    if command is None:
        raise DispatchError
    return command


def dispatch_application(
    client: ServiceClient,
    command: ApplicationCommand,
    *,
    payload: Mapping[str, Any],
    deadline: Deadline,
    cancellation: CancellationToken | None = None,
    principal: str | None = None,
    request_id: str | None = None,
    idempotency_key: str | None = None,
    record_version: str | None = None,
) -> ResponseEnvelope:
    """Issue `command` over `client` and return the answer the service gave.

    The envelope is assembled from the catalogue entry for `command.operation`
    and from the client's own descriptor: the workspace is the one the connected
    endpoint publishes, not one chosen here, so this CLI cannot ask about a
    workspace the endpoint it reached was not launched to serve.

    **The workspace is stated only where the catalogue's scope kind admits one.**
    `validate_operation_scope_workspace_id` -- which the service runs on every
    request -- requires a `workspace_id` on a workspace-scoped operation and
    refuses one on an installation-scoped operation, and `workspace.list` and
    `workspace.create` are installation-scoped. Naming the descriptor's workspace
    unconditionally would therefore make those two commands refusable by
    construction, so the scope kind decides, read off the same frozen entry the
    scopes and the capability come from.

    `principal` and the claims around it are exactly that -- *claims*. The
    service decides authority from its own grant and refuses a claim it did not
    grant; nothing asserted here grants anything.

    `idempotency_key` and `record_version` populate the two mutation metadata
    fields the catalogue declares. They remain absent for reads and for
    mutations whose caller has not supplied them; the application contract,
    not this adapter, decides whether either is required for this operation.

    `payload` is copied shallowly into `input`: the envelope must not alias a
    mapping the caller can still mutate after the request is built, and a deep
    copy would be a second traversal of a document the contract encoder walks
    anyway.

    An error *response* is returned, not raised. A peer that answered with an
    application error has answered, and the surface's exit codes are written for
    exactly those error codes. The only refusal here is an answer that does not
    correlate with the request.
    """
    if _APPLICATION_BY_PATH.get(command.path) != command:
        raise DispatchError
    entry = get_operation_metadata(command.operation)
    if entry.scope.scope_kind == SCOPE_KIND_WORKSPACE:
        workspace_id = client.descriptor.workspace_id
    elif entry.scope.scope_kind == SCOPE_KIND_INSTALLATION:
        workspace_id = None
    else:  # pragma: no cover - the frozen catalogue validation excludes this.
        raise DispatchError
    identifier = request_id if request_id is not None else _new_request_id()
    request = RequestEnvelope(
        operation=command.operation,
        metadata=RequestMetadata(
            request_id=identifier,
            correlation_id=identifier,
            trace_id=identifier,
            api_version=CONTRACT_VERSION,
            client=ClientIdentity(id=_CLIENT_ID, version=_CLIENT_VERSION),
            workspace_id=workspace_id,
            scopes=tuple(entry.scope.required_scopes),
            purpose=command.purpose,
            # The catalogue's own requirement object, passed through. The
            # validator refuses a request declaring none, or a different one.
            required_capabilities=(entry.required_capability,),
            # What is left of the caller's budget at the moment of the send, so
            # the peer works to the same clock the caller is waiting on.
            deadline_ms=deadline.remaining_ms(),
            idempotency_key=idempotency_key,
            mutation_precondition=(
                None
                if record_version is None
                else MutationPrecondition(record_version=record_version)
            ),
            principal_claim=(
                None
                if principal is None
                else PrincipalClaim(claimed_principal_id=principal)
            ),
        ),
        input=dict(payload),
    )
    response = client.call(request, deadline=deadline, cancellation=cancellation)
    if (
        response.metadata.request_id != identifier
        or response.metadata.correlation_id != identifier
    ):
        raise DispatchError
    return response


def dispatch_probe(
    client: ServiceClient,
    command: ProbeCommand,
    *,
    deadline: Deadline,
    cancellation: CancellationToken | None = None,
    request_id: str | None = None,
) -> ServiceProbeResult:
    """Answer `command` over `client`'s transport and return the result unchanged.

    The probe goes to the transport the client already composed. There is no
    second discovery, no second transport and no descriptor read here: a probe
    issued to some endpoint other than the connected one would answer a question
    about a service the caller is not talking to.

    The result is returned exactly as it arrived, including a `degraded` or
    `unhealthy` status -- a probe that answers "not ready" has answered, and
    deciding what that means is the caller's.
    """
    if _PROBE_BY_PATH.get(command.path) != command:
        raise DispatchError
    identifier = request_id if request_id is not None else _new_request_id()
    request = ServiceProbeRequest(
        probe=command.probe,
        request_id=identifier,
        deadline_ms=deadline.remaining_ms(),
    )
    result = client.transport.probe(
        request, deadline=deadline, cancellation=cancellation
    )
    # `details` is where the echo lands, and an absent echo is treated the same
    # as a wrong one: this request carried an id, so an answer to it has that id
    # in it, and an answer without it is not one this call can vouch for.
    details = result.details
    if details is None or details.get(_REQUEST_ID_DETAIL) != identifier:
        raise DispatchError
    return result


def _new_request_id() -> str:
    """A fresh CLI-prefixed request id.

    A random UUID rather than a counter: two `omnivia` processes share no state,
    so any sequence they each mint would collide in the service's logs.
    """
    return f"{_REQUEST_ID_PREFIX}-{uuid.uuid4()}"
