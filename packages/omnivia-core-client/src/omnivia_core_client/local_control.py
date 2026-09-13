"""Authenticated calls and installed-MCP administration over the local endpoint.

Two things this package could already do -- an application call as the service's
own principal, and a probe -- and one it could not: say *who* a call is for. The
installed MCP server holds a bearer for a dedicated principal, and until now
there was no way to present it over the installation-local endpoint, so the only
authenticated transport was HTTP. This module adds that, plus the separate
administration family a human uses to create, inspect and revoke the principal in
the first place.

**These are controls, not operations.** Nothing here is in the operation
catalogue, has a schema, or can be named by an MCP tool, and that is the point:
configure mints authority, and an authority a model could mint is not one. The
wrapper is internal to one installation, versioned by a frozen string, and
refused outright by a service that does not know the version.

**The endpoint and the conventions are the ones already here.** Every call below
goes through :meth:`~omnivia_core_client.LocalIpcTransport.exchange` -- the same
connect, the same OVC1 frame, the same whole-call
:class:`~omnivia_core_client.Deadline` re-read at each wait, the same
:class:`~omnivia_core_client.CancellationToken` checked before the deadline is.
There is no second dial loop, no pooled connection, no retry and no separate
timeout here.

**A credential is an argument, never a field.** It is passed positionally to the
one call that presents it and is never stored on a configuration, an endpoint
descriptor, a result, or this module's own state; it never reaches a process
argument, a host configuration file or a log.
:class:`AuthenticatedLocalTransport` is the one thing here that holds anything
credential-shaped at all, and what it holds is a *callable* rather than a
credential: it is asked once per call, immediately before the call goes on the
wire, so what it presents is whatever the store holds at that moment and a
revoked or rotated credential lands on the next call rather than at a restart. The one secret that travels the
other way -- the bearer a freshly rotated `configure` mints -- arrives inside
:class:`McpConfigureResult`, whose ``repr`` is redacted and whose value is
reachable only through :meth:`~McpConfigureResult.reveal`, the same discipline
:class:`~omnivia_core_client.Credential` keeps. ``status`` and ``revoke`` have no
field that could carry one.

Standard library plus the public ``omnivia_core`` contracts only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Final, NoReturn

from omnivia_core.contracts.v1 import (
    ContractDecodeError,
    RequestEnvelope,
    ResponseEnvelope,
    ServiceProbeRequest,
    ServiceProbeResult,
    codec,
)
from omnivia_core_client.credentials import Credential
from omnivia_core_client.deadline import CancellationToken, Deadline
from omnivia_core_client.errors import ClientError, ProtocolError, TransportError
from omnivia_core_client.local_ipc import LocalIpcTransport
from omnivia_core_client.service_client import ServiceClient

__all__ = [
    "LOCAL_CONTROL_VERSION",
    "AuthenticatedLocalTransport",
    "AuthoringAdmissionResult",
    "LocalControlRefused",
    "McpConfigureResult",
    "McpRevokeResult",
    "McpSetupView",
    "McpStatusResult",
    "authenticated_client",
    "call_authenticated",
    "local_control_transport",
    "mcp_authoring_admission",
    "mcp_configure",
    "mcp_revoke",
    "mcp_status",
]

#: The wrapper version this build speaks. Exactly one, matched exactly: an
#: installation ships both ends together, so there is nothing to negotiate and a
#: mismatch is a broken installation rather than an older peer.
LOCAL_CONTROL_VERSION: Final = "omnivia.local-control.v1"

_FIELD: Final = "local_control"
_RESULT_FIELD: Final = "local_control_result"

#: What ``operation=`` carries for these calls. A fixed label, never a kind and
#: never anything derived from a credential: it reaches the deadline and
#: cancellation diagnostics, which are rendered.
_LABEL: Final = "local.control"

_MAXIMUM_SETUPS: Final = 64

#: Every string this module admits off the wire is an identifier or a fixed
#: vocabulary word -- a setup id, a host, a principal, a workspace, a status.
#: None of them is prose, so one bound covers all of them, and it exists so a
#: peer cannot make this process build a megabyte-long "principal id".
_MAXIMUM_TEXT_CHARACTERS: Final = 256

#: What one answered control may carry at the top level, in each direction a
#: reply can go. Exact, because an extra member is a member this build does not
#: know the meaning of, and a peer that can add one to an admitted reply is a
#: peer choosing what a later build reads.
_ANSWER_RESULT: Final = frozenset({_RESULT_FIELD, "kind", "result"})
_ANSWER_ERROR: Final = frozenset({_RESULT_FIELD, "kind", "error"})

#: The exact members of each control's ``result``, by the kind that asked.
_RESULT_MEMBERS: Final[dict[str, tuple[frozenset[str], frozenset[str]]]] = {
    # kind -> (required, optional)
    "application.call": (frozenset({"response"}), frozenset()),
    "mcp.configure": (frozenset({"setup", "rotated"}), frozenset({"secret"})),
    "mcp.status": (frozenset({"setups"}), frozenset()),
    "mcp.revoke": (frozenset({"setup"}), frozenset()),
    "mcp.authoring_admission": (
        frozenset({"admitted", "principal_id", "workspace_id"}),
        frozenset(),
    ),
}

#: The exact members of one setup view. The same nine :class:`McpSetupView`
#: declares, stated once so a reply carrying a tenth is refused rather than
#: quietly dropped -- an ignored member is a member the peer still chose.
_SETUP_MEMBERS: Final = frozenset(
    {
        "setup_id",
        "host",
        "workspace_id",
        "principal_id",
        "profile",
        "authoring_intent",
        "credential_reference",
        "status",
        "setup_generation",
    }
)


#: The refusal vocabulary this build knows, and the sentence it renders for
#: each. Closed, and keyed by code rather than copied from the reply, because
#: the peer that sends a refusal is the peer that was just handed a bearer: a
#: message taken from the wire is a message that can be the credential itself.
#: The codes are the service's own -- ``local_control.LocalControlError`` in the
#: runtime -- and the runtime's suite pins them there; they are restated rather
#: than imported because this package depends on no runtime.
_REFUSAL_UNKNOWN: Final = "unknown"

_REFUSALS: Final[dict[str, str]] = {
    "malformed": "the local control document is not one this service admits",
    "unsupported": "this endpoint does not serve the requested local control",
    "unauthenticated": (
        "the presented credential does not resolve to live installed MCP authority"
    ),
    "unauthorized": (
        "installed MCP administration requires a local installation administrator"
    ),
    "refused": "the installed MCP authority refused the requested change",
    "unavailable": "the authoritative installation service could not be reached",
    _REFUSAL_UNKNOWN: "the service refused the local control",
}


class LocalControlRefused(ClientError):
    """The service refused a control, and said which of its fixed reasons.

    A :class:`~omnivia_core_client.ClientError` because it is an answer the
    caller must handle, not a transport fault: ``unauthenticated`` after a
    revocation is the system working. ``code`` is one of :data:`_REFUSALS`'
    keys and is what a caller should branch on; ``args[0]`` is *this package's*
    sentence for that code.

    Nothing the peer wrote reaches either member. The constructor takes a code,
    looks the sentence up, and has no parameter a message could be passed
    through -- so there is no edit that could start carrying peer text without
    changing this signature.
    """

    def __init__(self, code: str) -> None:
        self.code = code if code in _REFUSALS else _REFUSAL_UNKNOWN
        super().__init__(_REFUSALS[self.code])


@dataclass(frozen=True, slots=True)
class McpSetupView:
    """One configured host as the service reports it: durable state, redacted.

    Every member here is already safe to print. There is no salt, digest or
    secret field to leave out, and ``credential_reference`` is a name -- the
    opaque handle a host configuration may hold -- not the bearer it names.
    """

    setup_id: str
    host: str
    workspace_id: str
    principal_id: str
    profile: str
    authoring_intent: bool
    credential_reference: str
    status: str
    setup_generation: int


@dataclass(frozen=True, slots=True, repr=False)
class McpConfigureResult:
    """What one ``configure`` settled on, and at most one freshly minted bearer.

    ``secret`` is present exactly when ``rotated`` is true, because a configure
    that found the requested state already live minted nothing -- the credential
    already in the host's private configuration is still the credential.

    No generated ``repr`` and no generated ``__eq__``: the first would print the
    bearer wherever this value is logged or a container holding it is rendered,
    and the second would make the bearer comparable, which is an oracle nothing
    needs. :meth:`reveal` is a verb so that the one place a caller reads it stays
    greppable and reviewable in a diff.
    """

    setup: McpSetupView
    rotated: bool
    _secret: str | None

    def reveal(self) -> str | None:
        """The newly minted bearer, or ``None`` when nothing was rotated.

        Write it straight into owner-private storage and drop it. It is not
        retrievable again: the service stores only a salted digest, so a caller
        that loses this value must rotate rather than ask.
        """
        return self._secret

    def __repr__(self) -> str:
        return f"McpConfigureResult(rotated={self.rotated}, <redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class McpStatusResult:
    """Every configured host, or the one that was asked about."""

    setups: tuple[McpSetupView, ...]


@dataclass(frozen=True, slots=True)
class McpRevokeResult:
    """The setup as it stands after revocation, or ``None`` if there was none.

    ``None`` is not a failure: a host that was never configured is already in the
    state a revoke was asking for, and revoke is idempotent by design.
    """

    setup: McpSetupView | None


@dataclass(frozen=True, slots=True)
class AuthoringAdmissionResult:
    """Whether the presented bearer is admitted to author, and for whom.

    The two identifiers are the service's answer about *the credential that was
    presented*, not something the caller asked about. A caller that holds a
    configured principal and workspace should compare them and treat a mismatch
    as not admitted -- that comparison is what makes this an answer about the
    session rather than about a name the caller supplied.
    """

    admitted: bool
    principal_id: str
    workspace_id: str


@dataclass(frozen=True, slots=True, repr=False)
class AuthenticatedLocalTransport:
    """A transport that issues every application call as whoever holds a bearer.

    The composition the installed MCP server needs and the only one it needs.
    :class:`~omnivia_core_client.ServiceClient` and everything above it keep
    calling :meth:`ServiceClient.call`; what changes underneath is that the call
    travels :func:`call_authenticated` rather than the unauthenticated
    application path, so the service dispatches it under the dedicated principal
    the bearer resolves to instead of under its own.

    **The credential is a callable, not a value, and that is the whole point.**
    It is asked on every call, so the material this transport presents is
    whatever the store holds at that moment: a credential revoked or rotated
    between two calls fails, or succeeds as the new one, on the very next call.
    Holding a resolved :class:`~omnivia_core_client.Credential` in a field here
    would be a copy outliving the decision behind it, which is exactly what makes
    revocation take a restart.

    ``probe`` is forwarded unchanged. A probe is answerable before any authority
    exists -- that is why the contract gives it its own request and result types
    -- so presenting a bearer on one would be claiming an identity for a question
    that has none.
    """

    transport: LocalIpcTransport
    """The installation-local endpoint. Dialled by :func:`call_authenticated`,
    with this module's one set of connect, deadline and framing rules."""

    credential: Callable[[], Credential]
    """Asked once per call, immediately before the call is put on the wire."""

    def call(
        self,
        request: RequestEnvelope,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ResponseEnvelope:
        """One application call, presented as the principal the bearer names."""
        return call_authenticated(
            self.transport,
            self.credential().reveal(),
            request,
            deadline=deadline,
            cancellation=cancellation,
        )

    def probe(
        self,
        request: ServiceProbeRequest,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ServiceProbeResult:
        """One runtime probe, unauthenticated, exactly as the endpoint serves it."""
        return self.transport.probe(
            request, deadline=deadline, cancellation=cancellation
        )

    def __repr__(self) -> str:
        """No generated ``repr``: it would render the credential callable, and a
        bound method or closure's ``repr`` names the object it was taken from."""
        return "AuthenticatedLocalTransport(<redacted>)"

    __str__ = __repr__


def authenticated_client(
    client: ServiceClient, credential: Callable[[], Credential]
) -> ServiceClient:
    """The same connected service, calling as whoever `credential` answers with.

    A replacement rather than a mutation: :class:`ServiceClient` is frozen, and
    the descriptor and the version negotiation carried alongside are the ones
    that connection already established -- nothing about presenting a bearer
    re-opens either. A caller that wants both is expected to keep only this one:
    an unauthenticated client kept beside it is a way to call as the service's
    own principal by accident.
    """
    return replace(
        client,
        transport=AuthenticatedLocalTransport(
            transport=local_control_transport(client), credential=credential
        ),
    )


def local_control_transport(client: ServiceClient) -> LocalIpcTransport:
    """The local transport behind `client`, or a refusal if there is not one.

    Controls travel the installation-local endpoint and only that endpoint. An
    HTTP-configured client is refused here rather than at the wire, because the
    honest reason is that this family has no HTTP form at all -- not that some
    request failed.

    An :class:`AuthenticatedLocalTransport` is unwrapped rather than refused. It
    *is* the installation-local endpoint, with a bearer presented on application
    calls; the administration family below presents its own credential or none,
    so it wants the endpoint underneath rather than that wrapping.
    """
    transport = client.transport
    if isinstance(transport, AuthenticatedLocalTransport):
        transport = transport.transport
    if not isinstance(transport, LocalIpcTransport):
        raise TransportError(
            "local controls travel the installation-local endpoint only"
        )
    return transport


def call_authenticated(
    transport: LocalIpcTransport,
    credential: str,
    request: RequestEnvelope,
    *,
    deadline: Deadline,
    cancellation: CancellationToken | None = None,
) -> ResponseEnvelope:
    """Issue one application call as whoever holds `credential`.

    The service resolves the bearer against durable state on this call and
    dispatches under exactly what it resolves to; nothing is established by this
    function and nothing is kept by it. So a credential revoked between two calls
    fails the second, and the same call replayed with a revoked bearer fails
    immediately rather than at some session expiry.

    An application error comes back as an error response envelope, exactly as it
    does from :meth:`~omnivia_core_client.ServiceClient.call` -- a peer that
    answered with an application error has answered. Only a refusal of the
    *control* raises.
    """
    result = _exchange(
        transport,
        {
            _FIELD: LOCAL_CONTROL_VERSION,
            "kind": "application.call",
            "credential": credential,
            "request": codec.encode_request(request),
        },
        "application.call",
        deadline=deadline,
        cancellation=cancellation,
    )
    document = result.get("response")
    if not isinstance(document, dict):
        _raise_malformed()
    decoded = False
    try:
        response = codec.decode_response(document)
        decoded = True
    except ContractDecodeError:
        pass
    if not decoded:
        _raise_malformed()
    return response


def mcp_configure(
    transport: LocalIpcTransport,
    *,
    host: str,
    workspace_id: str,
    profile: str,
    authoring_intent: bool,
    deadline: Deadline,
    cancellation: CancellationToken | None = None,
) -> McpConfigureResult:
    """Provision or re-provision one host's dedicated MCP principal.

    Carries no credential: reaching this endpoint is the operating system's
    owner-private proof, and the administrator the service checks is the session
    *it* established, which no argument here can influence. The caller chooses a
    host, a workspace, a profile and whether authoring intent is being recorded,
    and nothing else -- there is no scope, capability, purpose or operation
    argument, because the rights a profile implies are the service's to derive.
    """
    result = _exchange(
        transport,
        _administration(
            "mcp.configure",
            {
                "host": host,
                "workspace_id": workspace_id,
                "profile": profile,
                "authoring_intent": authoring_intent,
            },
        ),
        "mcp.configure",
        deadline=deadline,
        cancellation=cancellation,
    )
    rotated = result.get("rotated")
    secret = result.get("secret")
    if type(rotated) is not bool or (rotated is False and secret is not None):
        _raise_malformed()
    if rotated and type(secret) is not str:
        _raise_malformed()
    return McpConfigureResult(
        setup=_setup(result.get("setup")),
        rotated=rotated,
        _secret=secret if isinstance(secret, str) else None,
    )


def mcp_status(
    transport: LocalIpcTransport,
    *,
    host: str | None = None,
    deadline: Deadline,
    cancellation: CancellationToken | None = None,
) -> McpStatusResult:
    """Report configured hosts as durable, redacted state.

    There is no branch in this family that returns a secret, and no argument that
    could ask for one: :class:`McpSetupView` has no field to put one in.
    """
    result = _exchange(
        transport,
        _administration("mcp.status", {} if host is None else {"host": host}),
        "mcp.status",
        deadline=deadline,
        cancellation=cancellation,
    )
    setups = result.get("setups")
    if not isinstance(setups, list) or len(setups) > _MAXIMUM_SETUPS:
        _raise_malformed()
    return McpStatusResult(setups=tuple(_setup(entry) for entry in setups))


def mcp_revoke(
    transport: LocalIpcTransport,
    *,
    host: str,
    deadline: Deadline,
    cancellation: CancellationToken | None = None,
) -> McpRevokeResult:
    """Invalidate one host's authority. Idempotent, and effective immediately.

    The service overwrites the stored verifier and advances the setup generation
    in the transaction that marks the row revoked, so the previous bearer stops
    authenticating on the next call and on the next same-key replay.
    """
    result = _exchange(
        transport,
        _administration("mcp.revoke", {"host": host}),
        "mcp.revoke",
        deadline=deadline,
        cancellation=cancellation,
    )
    setup = result.get("setup")
    return McpRevokeResult(setup=None if setup is None else _setup(setup))


def mcp_authoring_admission(
    transport: LocalIpcTransport,
    credential: str,
    *,
    deadline: Deadline,
    cancellation: CancellationToken | None = None,
) -> AuthoringAdmissionResult:
    """Ask whether the presented bearer is actively admitted to author.

    The protected answer the MCP server's startup decision needs and cannot give
    itself: nothing readable from its public configuration is evidence that a
    human recorded authoring intent, so it asks the service, which reads durable
    state fresh on every call. A revoked or downgraded principal is not admitted
    from the next call onward.
    """
    result = _exchange(
        transport,
        {
            _FIELD: LOCAL_CONTROL_VERSION,
            "kind": "mcp.authoring_admission",
            "credential": credential,
        },
        "mcp.authoring_admission",
        deadline=deadline,
        cancellation=cancellation,
    )
    admitted = result.get("admitted")
    if type(admitted) is not bool:
        _raise_malformed()
    return AuthoringAdmissionResult(
        admitted=admitted,
        principal_id=_text(result.get("principal_id")),
        workspace_id=_text(result.get("workspace_id")),
    )


def _administration(kind: str, arguments: Mapping[str, object]) -> dict[str, object]:
    return {_FIELD: LOCAL_CONTROL_VERSION, "kind": kind, "arguments": dict(arguments)}


def _exchange(
    transport: LocalIpcTransport,
    document: dict[str, object],
    kind: str,
    *,
    deadline: Deadline,
    cancellation: CancellationToken | None,
) -> Mapping[str, object]:
    """One control out, its ``result`` members back, or the service's refusal."""
    answer = transport.exchange(
        document, deadline=deadline, cancellation=cancellation, operation=_LABEL
    )
    if answer.get(_RESULT_FIELD) != LOCAL_CONTROL_VERSION or answer.get("kind") != kind:
        _raise_malformed()
    members = frozenset(answer)
    if members == _ANSWER_ERROR:
        error = answer.get("error")
        if not isinstance(error, dict):
            _raise_malformed()
        _raise_refused(error)
    if members != _ANSWER_RESULT:
        _raise_malformed()
    result = answer.get("result")
    if not isinstance(result, dict):
        _raise_malformed()
    required, optional = _RESULT_MEMBERS[kind]
    present = frozenset(result)
    if not required <= present or not present <= (required | optional):
        _raise_malformed()
    return result


def _setup(value: object) -> McpSetupView:
    if not isinstance(value, dict) or frozenset(value) != _SETUP_MEMBERS:
        _raise_malformed()
    generation = value.get("setup_generation")
    intent = value.get("authoring_intent")
    if type(generation) is not int or type(intent) is not bool:
        _raise_malformed()
    return McpSetupView(
        setup_id=_text(value.get("setup_id")),
        host=_text(value.get("host")),
        workspace_id=_text(value.get("workspace_id")),
        principal_id=_text(value.get("principal_id")),
        profile=_text(value.get("profile")),
        authoring_intent=intent,
        credential_reference=_text(value.get("credential_reference")),
        status=_text(value.get("status")),
        setup_generation=generation,
    )


def _text(value: object) -> str:
    if not _bounded_text(value):
        _raise_malformed()
    assert type(value) is str
    return value


def _bounded_text(value: object) -> bool:
    """Whether `value` is a string short enough to be an identifier at all."""
    return type(value) is str and len(value) <= _MAXIMUM_TEXT_CHARACTERS


#: Raised from outside every handler, never inside. The rule and its reason are
#: ``local_ipc.py``'s, and ``scripts/check-raise-discipline.py`` covers this file
#: for the same reason it covers that one: a ``ContractDecodeError`` reachable
#: through ``__context__`` names the field path of a document that, here, holds a
#: bearer credential.


def _raise_malformed() -> NoReturn:
    raise ProtocolError("the answer is not a well-formed local control result")


def _raise_refused(error: Mapping[str, object]) -> NoReturn:
    """Raise this package's refusal for the code the service named.

    A code this build does not know still raises: an unknown refusal is still a
    refusal, and inventing a success from one would be the worst possible reading
    of it. It raises as ``unknown``, with ``unknown``'s sentence -- the peer's
    spelling is not preserved, because a code member is a string the peer chose
    and a bounded string is enough room for a bearer.

    The ``message`` member is read only to require it to be a string of some
    bounded length, which is the shape a service's reply has. Its *value* is
    never carried anywhere: the sentence comes from :data:`_REFUSALS`.
    """
    if not _bounded_text(error.get("message")) or set(error) != {"code", "message"}:
        _raise_malformed()
    code = error.get("code")
    raise LocalControlRefused(code if type(code) is str else _REFUSAL_UNKNOWN)
