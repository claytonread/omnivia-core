"""HTTP v1 in front of the same router local IPC uses (P2b).

**Embedder-only for v0.6.** This adapter exists, is exercised, and is not served by
the standard Core service, which is a decision rather than an unfinished wiring:
`--http-endpoint` defaults to `None`, the console-script entry point supplies no
credential resolver, and so every run that would actually serve one that way exits
2. `--check-only` is outside that rule rather than an exception to it: it serves
nothing, so it never reaches this adapter and never parses the endpoint it was
given -- a wildcard host, an `https` URL and unparseable text all exit 0 there on a
ready workspace. Only an approved embedder or conformance harness calling
`service.main.main` with a resolver of its own can bring this listener up. The
reason is that no approved credential source or bearer-session resolver has been
defined yet, and it does not
remove authenticated HTTP from the target architecture, which still needs it for
LAN, NAS, private-server, Cloud and hybrid Desktop-to-remote-Core operation. The
declaration, the credential-source boundary and the revisit trigger are in:

    docs/development/omnivia-core-staged-startup-and-embedder-only-http-2026-08-05.md

Two routes and nothing else: `POST /v1/application` and `POST /v1/probe`. This is a
transport adapter, so what it owns is bytes, headers, status codes and the
authentication check that produces a session -- and nothing past that line. The
document it admits is handed to the *same* :class:`DocumentRouter` instance the local
transport is given, so there is one probe router and one application dispatcher behind
both, and no second error catalogue can grow here: this module constructs no
`ApiError`, no `ErrorResponseEnvelope` and no error code at all.

Five things are decided here rather than inherited, and each of them is a refusal.

**The bind.** IP literals only, checked with `ipaddress` rather than by spelling. A
*hostname*, including `localhost`, is refused rather than resolved, because what a
name resolves to is host configuration and a bind policy an `/etc/hosts` line can move
is not one. A non-loopback address -- wildcard included -- is refused *unless
server-side TLS is configured on the bind itself*. The configuration lives there, and
not on the server, so the refusal stays at construction where it already was: there is
still no state in which an unconfigured listener is up. Cleartext loopback is
unchanged and needs nothing.

**TLS.** Stated or absent, never inferred and never defaulted. :class:`HttpTls` names
a certificate chain and a private key; a bind carries one or carries nothing, so a
half-configured listener is unrepresentable rather than refused. The context is built
from it in :meth:`HttpListener.start` *before a socket exists*, so material that
is missing, unreadable or mismatched refuses with nothing bound rather than with a
listener up and a handshake that fails one caller at a time. The floor is stated
rather than inherited from whatever the platform default is this year:
`minimum_version` is TLS 1.2. It is a floor rather than 1.3 because two modern peers
negotiate 1.3 regardless, while a 1.3 *minimum* refuses a peer legitimately capped at
1.2 -- that is a compatibility cliff, not a security gain, and what actually needs
excluding is 1.1 and below. `maximum_version` is left alone for the same reason in the
other direction. Client certificates are out of scope -- there is no mutual TLS in
this lane -- so `PROTOCOL_TLS_SERVER`'s own `CERT_NONE` stands as a deliberate
boundary rather than an oversight.

And there is no downgrade. :meth:`_HttpService.get_request` branches on what the bind
*asked for*, not on what was loaded, so a TLS bind whose context is somehow absent
closes the connection rather than falling through to the cleartext return above it.
Neither failing path constructs a handler, so a plaintext byte cannot be answered by a
listener that was asked for TLS.

**The credential.** Every application request carries a verified bearer credential,
loopback included. Verification is an injected :data:`CredentialResolver`: this module
invents no token format, no credential store and no principal registry -- it takes a
credential and is handed back the server-side :class:`AuthenticatedSession` that
credential resolves to, or nothing. A missing resolver is a configuration error and
refuses startup; it is never a permissive default. The credential itself reaches no
message, no exception, no log and no response: the resolver is called behind
:func:`_resolved`, which answers "no session" and drops whatever the resolver raised
rather than chaining it.

The check runs *before the body is read*, which is what makes "never reaches dispatch"
a structural property rather than a status code. An unauthenticated application
request is refused with a byte of the router untouched.

**The session and its claims.** A resolved session is a server fact, and
:func:`_session_admits` decides three things before dispatch. The dispatcher behind
this adapter acts as one fixed principal, so a session for anyone else is refused
rather than silently executed as that one. The operation has to be on the session's
own allowlist. And `principal_claim`, `workspace_id`, `purpose` and `scopes` in the
request are claims: each must name something the session already holds, so a claim may
select a subset and can never add. This is a gate in front of dispatch, not a second
authorization vocabulary -- it produces a status code, never an envelope.

**The route/document agreement.** A probe is answered unauthenticated, so the probe
route must not become a way to reach the application path. The document's own branch
field therefore has to agree with the URL it arrived on: an `operation` document
posted to `/v1/probe` is refused before routing rather than dispatched by a router
that would, correctly, see an application request.

That rule is unchanged, but the sentence that used to justify it -- "unauthenticated
*on loopback*" -- no longer describes every bind this module can serve. A non-loopback
TLS listener answers `/v1/probe` to any peer that completes the handshake, and the
handshake asks for no client certificate, so `ServiceFacts` -- health, readiness,
`server_version`, `api_version` -- is now reachable off-host in a way it was not in
any previous configuration. The premise widened; the behaviour did not. **Whether that
surface is intended is an owner decision and is recorded rather than resolved here**:
gating the probe would be a new authentication rule for a route the accepted freeze
deliberately leaves open, which is not this lane's to make. The rule above stands on
its own without the loopback premise -- an unauthenticated route must not reach the
authenticated one, wherever it is bound.

Concurrency is deliberately absent. `HTTPServer`, not `ThreadingHTTPServer`: the local
transport serves one connection at a time and the merged P2b review recorded that
serialization as an accepted property of the dispatcher behind it. HTTP shares that
dispatcher, so matching the serialization is what keeps the property true rather than
quietly making it a claim about a code path that no longer holds it.

Head-of-line blocking is the cost of that, and because the listener is serialized the
bound on one request is the bound on every other request's wait. So it is a *total*
deadline over reading one request, not a socket timeout: a socket timeout bounds one
`recv`, and a client dripping a byte inside each window holds a serialized listener
open indefinitely while every per-operation deadline is honoured. That is the
unbounded-wait class the OVC1 lane fixed, and inheriting `transport.py`'s constant
without its semantics reintroduced it -- measured at 17 seconds against a claimed 10.
`_Handler.setup` arms that total budget. HTTP/1.0 with `Connection: close` gives one
request per connection, and the body is read to an exact `Content-Length`, never to
EOF.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Final, Self, TypeAlias
from urllib.parse import urlsplit

from omnivia_core.contracts.v1 import ContractDecodeError, RequestEnvelope
from omnivia_core_runtime.service.authorization import AuthenticatedSession
from omnivia_core_runtime.service.ovc1 import (
    LENGTH_BYTES,
    MAGIC,
    MAXIMUM_JSON_BYTES,
    OVC1Error,
    canonical_json_bytes,
    decode_frame,
)
from omnivia_core_runtime.service.probes import ProbeError
from omnivia_core_runtime.service.protocol import (
    OPERATION_FIELD,
    PROBE_FIELD,
    DocumentRouter,
    ProtocolError,
)
from omnivia_core_runtime.service.transport import DEFAULT_TIMEOUT_SECONDS

#: The two routes this adapter serves, and the whole of them.
APPLICATION_PATH: Final = "/v1/application"
PROBE_PATH: Final = "/v1/probe"

#: The canonical JSON media type. Canonical JSON is UTF-8 by definition, so a stated
#: charset may say that and nothing else.
CONTENT_TYPE: Final = "application/json"
CHARSET: Final = "utf-8"

BEARER_PREFIX: Final = "Bearer "

#: The default bind. IPv4 loopback, per the accepted freeze; `::1` is opt-in by
#: naming it, and anything that is not a loopback literal is refused.
DEFAULT_HOST: Final = "127.0.0.1"

#: What a bearer credential resolves to, injected.
#:
#: Takes the credential and returns the server-side session it authenticates, or
#: `None` when it authenticates nothing -- missing, malformed, expired, wrong
#: audience, revoked, unknown. Which of those it was is the resolver's business and
#: is deliberately not reportable through this seam: an adapter that could tell a
#: caller *why* a credential failed is an adapter that has become a credential
#: oracle. This module supplies no implementation. Where one comes from -- a store, a
#: signed token, a broker -- is a separate, separately approved decision, and
#: inventing one here would be inventing the security design it belongs to.
CredentialResolver: TypeAlias = Callable[[str], AuthenticatedSession | None]


class HttpTransportError(Exception):
    """The HTTP adapter could not be configured, bound or started.

    Startup only. A *request* never raises out of this module: it is answered with a
    status code, so nothing a caller sent can reach an exception message.
    """


@dataclass(frozen=True)
class HttpTls:
    """The server-side TLS material a listener presents. Both halves or neither.

    One value rather than two optional fields on :class:`HttpBind`, so "a chain and no
    key" is a state that cannot be written down instead of a state that has to be
    caught. Absent is then the only other state, and absent means cleartext loopback.

    Paths, not a caller-built :class:`ssl.SSLContext`. A context handed in cannot be
    checked here -- one with nothing loaded is indistinguishable from a good one until
    a peer connects, which is a listener already bound and a refusal that arrives one
    handshake at a time rather than at startup -- and its protocol floor would be
    whoever built it's, not this module's. Taking the material and building the context
    is what makes "require valid server-side TLS configuration" a startup fact.

    Nothing is read here. Validation is deliberately at :func:`_tls_context`, called
    from `start` before a socket exists, so constructing a bind touches no filesystem.
    """

    certificate_chain: Path
    private_key: Path


@dataclass(frozen=True)
class HttpBind:
    """Where this adapter listens, and whether it terminates TLS.

    `host` is an IP literal, never a name. `ipaddress` decides whether it is loopback,
    so the rule is the address family's own rather than a list of spellings someone
    remembered: `127.0.0.1`, any other `127.0.0.0/8` address and `::1` are loopback,
    and `0.0.0.0`, `::` and every routable address are not.

    A hostname is refused rather than resolved. `localhost` usually resolves to
    loopback and is under no obligation to: resolution is the host's configuration,
    not this process's, and a bind policy that can be moved by an `/etc/hosts` line is
    not a bind policy. That holds under TLS too -- a server binds an *address*, and the
    hostname is the peer's concern and the certificate's subject.

    `tls` is the one thing that opens a non-loopback bind, and it is stated rather than
    implied. Without it the refusal below is the merged one, unchanged and at
    construction: `HttpBind(host="192.0.2.1")` still raises, so there is no state in
    which a routable listener is up without TLS and no reachable path that answers a
    non-loopback caller in the clear. With it, one rule covers wildcard and routable
    alike -- non-loopback needs TLS -- rather than a second rule about `0.0.0.0` that
    would have to be justified and pinned separately.

    Port `0` means "the OS picks", which is what a test binds and what
    :attr:`HttpListener.url` reports back after binding.
    """

    host: str = DEFAULT_HOST
    port: int = 0
    tls: HttpTls | None = None

    def __post_init__(self) -> None:
        # Parsed here and raised below, outside the handler. `ip_address` quotes the
        # value it could not read, and an endpoint string is exactly where a caller's
        # credential or path would be -- `raise ... from None` is not enough to keep
        # it out, because it clears `__cause__` and leaves `__context__` referencing
        # the original, one attribute access from anything that logs the refusal.
        address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError:
            pass
        if address is None:
            raise HttpTransportError("HTTP bind host must be a loopback IP literal")
        if not address.is_loopback and self.tls is None:
            # Wildcard and every routable address land here. Serving one in the clear
            # is what is refused, not serving one at all: TLS on the bind is the whole
            # of what opens this, and its absence keeps the merged refusal exactly
            # where it was.
            raise HttpTransportError("HTTP bind host is not a loopback address")
        if not 0 <= self.port <= 65535:
            raise HttpTransportError("HTTP bind port is out of range")

    @property
    def is_ipv6(self) -> bool:
        return isinstance(ipaddress.ip_address(self.host), ipaddress.IPv6Address)


def _tls_context(tls: HttpTls) -> ssl.SSLContext:
    """The server-side context this material configures, or a refusal.

    Called from `start` before anything is bound, which is what turns "unusable TLS
    configuration" into a startup refusal with no socket in existence.

    The floor is set here rather than inherited. `PROTOCOL_TLS_SERVER` picks up
    whatever the linked OpenSSL considers acceptable this year, which is a number this
    module has not decided and cannot pin; `minimum_version` states it. TLS 1.2 rather
    than 1.3, because 1.3 is what two modern peers negotiate anyway and a 1.3 *floor*
    only adds a refusal of peers capped at 1.2. `verify_mode` is left at
    `PROTOCOL_TLS_SERVER`'s `CERT_NONE`: mutual TLS is not in this lane, and asking for
    a client certificate nothing verifies would be a ceremony rather than a check.

    `load_cert_chain` quotes the file it could not read -- a `FileNotFoundError` names
    the path, and an `SSLError` from a key that does not match its chain names it too.
    So the refusal is built outside the handler, exactly as the bind, the endpoint
    parser and `start` build theirs: `from None` clears `__cause__` and leaves
    `__context__` referencing the original, one attribute access from anything that
    logs the refusal, and a certificate path is a filesystem layout this process is not
    entitled to publish.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    failed = False
    try:
        context.load_cert_chain(certfile=tls.certificate_chain, keyfile=tls.private_key)
    except (OSError, ValueError):
        failed = True
    if failed:
        raise HttpTransportError("HTTP TLS material could not be loaded")
    return context


def parse_http_endpoint(endpoint: str, *, tls: HttpTls | None = None) -> HttpBind:
    """The bind an advertised `http://host:port` or `https://host:port` names.

    Nothing but scheme, host and port is accepted. A path, a query, a fragment or
    anything in the `userinfo` position is refused rather than ignored: a credential
    must never travel in a URL, and the way to guarantee that is to refuse a URL with
    a place to put one instead of parsing it and looking away.

    A URL cannot carry a certificate, so `tls` is a separate argument and the two have
    to *agree*. `https` with no material refuses, because a scheme is a promise and a
    listener that cannot keep it must not be built; `http` with material refuses too,
    because the honest reading of "here is a key, serve it in the clear" is that one of
    the two is a mistake, and guessing which would be guessing whether to downgrade.
    Neither direction has a fallback. A non-loopback `http://` endpoint then refuses in
    :class:`HttpBind` as it always has -- reaching TLS is the only way past it.

    `urlsplit` itself raises, which is easy to miss because it looks like a total
    function. A bracketed netloc is validated during the split -- `http://[<text>]:1`
    raises `ValueError` **quoting the bracketed text**, and `http://[::1` raises
    "Invalid IPv6 URL" -- so the split is guarded like everything else here. Left
    unguarded it escaped this module entirely as an unhandled `ValueError` carrying
    caller-supplied text, past the `HttpTransportError` handler in `main`, and it is
    reached before the resolver refusal, so the shipped entry point crashed on it
    instead of refusing.
    """
    parts: Any = None
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        pass
    if parts is None:
        raise HttpTransportError("HTTP endpoint is not a well-formed URL")
    if parts.scheme not in ("http", "https"):
        raise HttpTransportError("HTTP endpoint must name the http or https scheme")
    if parts.scheme == "https" and tls is None:
        raise HttpTransportError("HTTPS endpoint states no TLS material")
    if parts.scheme == "http" and tls is not None:
        raise HttpTransportError("HTTP endpoint states TLS material it cannot serve")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise HttpTransportError("HTTP endpoint must name a host and port only")
    host: str | None = None
    port: int | None = None
    unusable_port = False
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        # `urlsplit().port` quotes the port text it refused, and the refusal is
        # raised outside this handler so neither `__cause__` nor `__context__`
        # carries the endpoint string onward.
        unusable_port = True
    if unusable_port:
        raise HttpTransportError("HTTP endpoint states an unusable port")
    if parts.username is not None or parts.password is not None:
        raise HttpTransportError("HTTP endpoint must carry no credential")
    if host is None or port is None:
        raise HttpTransportError("HTTP endpoint must state a host and a port")
    return HttpBind(host=host, port=port, tls=tls)


def _resolved(
    resolve: CredentialResolver | None, credential: str
) -> AuthenticatedSession | None:
    """Call the resolver so nothing it raises can carry the credential onward.

    A `None` resolver authenticates nothing. Construction already refused one, so this
    is the second lock rather than a second rule: no reachable state of this adapter
    can answer an application request without a resolver having verified it.

    The credential is this seam's argument, so it is in the resolver's frame, in
    whatever it builds from it, and in the message of anything it raises -- a store
    quoting the key it looked up, a decoder quoting the bytes it could not parse, an
    HTTP client quoting the URL it was given. Any of those escaping here would put the
    credential into an exception this module then has to hold, and `__cause__` and
    `__context__` keep it reachable even when nothing renders it.

    So a raising resolver authenticates nothing, and the original is dropped rather
    than chained. The `pass` ends the handler, so the `return` below runs with no
    exception being handled and there is nothing to chain to in the first place.

    `BaseException` is deliberately not caught: `KeyboardInterrupt` and `SystemExit`
    are this process stopping, not a credential failing to verify.
    """
    if resolve is None:
        return None
    try:
        return resolve(credential)
    except Exception:  # noqa: BLE001, S110 -- deliberate; see the docstring
        pass
    return None


def _admitted(body: bytes) -> dict[str, Any] | None:
    """The body as the document local IPC would have admitted, or nothing.

    Admission is OVC1's -- UTF-8, valid JSON, an object root, and RFC 8785 canonical
    form -- and it is *reused* rather than restated: the body is handed to
    `decode_frame` behind OVC1's own header, so the same bytes are accepted or refused
    on both transports by the same code. Re-implementing the checks here would be a
    second admission policy, and the two would drift.

    An empty body is refused by that decoder as a zero-byte payload, so it needs no
    check of its own here.
    """
    frame = MAGIC + len(body).to_bytes(LENGTH_BYTES, "big") + body
    try:
        return decode_frame(frame)
    except OVC1Error:
        pass
    return None


def _session_admits(
    request: RequestEnvelope, session: AuthenticatedSession, principal: str
) -> bool:
    """Whether this session may run this request at this endpoint.

    Three fail-closed questions, in this order.

    **Can this endpoint act as this session's principal at all?** The dispatcher
    behind this adapter holds one fixed `Grant` and runs everything as its own
    principal; per-session dispatch grants are a separate, unapproved decision. So a
    session for anyone else is refused here rather than quietly executed as somebody
    else. Refusing is the only honest answer available: the alternative, which this
    adapter did until review caught it, was to discard the authenticated principal
    and serve the request as the service principal -- which made an *honest* request
    naming its own principal fail with a principal mismatch while a silent one
    succeeded, penalising honesty and serving silence. **HTTP can therefore serve
    only sessions whose principal is the one this service's Grant acts as.**

    **Is the operation on the session's allowlist?** `operations` is documented as an
    authority grant and defaults to empty, so a session granted none can invoke none.
    Left unread it was a field the type promised and nothing enforced.

    **Do the request's claims fit inside the session?** Claims narrow, never add.
    `principal_claim`, `workspace_id`, `purpose` and `scopes` are what the *request*
    says about itself, and a request naming a principal, role, workspace, purpose or
    scope outside the session is refused rather than answered under the session's own
    wider authority.

    Absent is not the same as empty. A request that claims no principal and no roles
    is running as the session, which is exactly what it is entitled to do; a request
    that claims them has to be inside them. `purpose` has no absent form -- the
    contract requires one -- so it is always checked, and a session granted no
    purposes can act for none.
    """
    if session.principal_id != principal:
        return False
    if request.operation not in session.operations:
        return False
    metadata = request.metadata
    claim = metadata.principal_claim
    if claim is not None:
        claimed_principal = claim.claimed_principal_id
        if claimed_principal is not None and claimed_principal != session.principal_id:
            return False
        claimed_roles = claim.claimed_roles
        if claimed_roles is not None and not frozenset(claimed_roles) <= session.roles:
            return False
    workspace_id = metadata.workspace_id
    if workspace_id is not None and workspace_id not in session.workspaces:
        return False
    if metadata.purpose not in session.purposes:
        return False
    return frozenset(metadata.scopes) <= session.scopes


def _dispatch_principal(router: DocumentRouter) -> str | None:
    """The principal the router's dispatcher actually acts as, if it can be read.

    `ApplicationDispatch` is a plain callable by design -- the router constructs no
    grant and knows nothing about authority -- so there is no general way to ask one
    who it acts as. There is one shape that answers: a bound method of an object
    holding a `grant`, which is exactly what `main` wires in when it passes
    `Dispatcher.dispatch`. That is read here through public attributes only; nothing
    in `dispatch.py` or `authorization.py` is touched or needs to be.

    `None` means "this dispatcher cannot say", which is the honest answer for a test
    double or any other plain callable, and it is why an unreadable dispatcher is not
    itself a refusal. What is refused is a *disagreement*: a dispatcher that states a
    principal and an adapter that declares a different one. See
    :meth:`HttpListener.__post_init__`.

    The reads are guarded because this walks an object of unknown shape on purpose,
    and "unknown shape" includes one that raises. `getattr`'s default covers only
    `AttributeError`, so a `grant` whose `principal` is a property raising anything
    else would leave this function as a bare exception from inside a startup path
    that promises `HttpTransportError` -- the same discipline as `_resolved` and the
    endpoint parser, and the last attribute access here that did not follow it.
    Nothing in this repo can produce such a grant today; the point is that the
    function degrades the way its own contract says it does, for every shape.
    """
    principal: object = None
    try:
        grant = getattr(getattr(router.dispatch, "__self__", None), "grant", None)
        principal = getattr(grant, "principal", None)
    except Exception:  # noqa: BLE001, S110 -- a shape that raises cannot say either
        pass
    return principal if isinstance(principal, str) else None


class _HttpService(HTTPServer):
    """The listener, carrying the adapter the handler answers from."""

    adapter: HttpListener
    #: The server-side context, installed by `start` before the serving thread exists.
    #: `None` is a *cleartext* listener and nothing else -- see `get_request`, which
    #: reads the bind rather than this to decide whether TLS was asked for.
    tls: ssl.SSLContext | None = None

    def get_request(self) -> tuple[Any, Any]:
        """Accept one connection, and finish the handshake before a handler exists.

        The handshake happens before there is a request, so the total request budget
        `_Handler.setup` arms cannot bound it: a peer that connects and never speaks
        would hold this *serialized* listener for as long as it liked, with every
        other caller behind it. That is the unbounded-wait class the OVC1 lane fixed,
        reintroduced one layer lower down, so the accepted socket is given the same
        budget a request gets before the handshake is attempted. `_Handler.setup`
        replaces that with the per-operation floor once there is a handler, which is
        what the merged deadline already does for a cleartext connection.

        `socketserver._handle_request_noblock` catches `OSError` out of here and moves
        on without constructing a handler, calling `handle_error` or stopping the
        listener -- and `ssl.SSLError` and `TimeoutError` are both `OSError`. So a
        failed, refused or abandoned handshake is one dropped connection. It does not
        `shutdown_request` what it never received, though, so the accepted socket is
        closed here or it is leaked once per failed handshake.

        The branch is on `bind.tls` -- what the caller *asked for* -- and not on
        `self.tls`, which is what was loaded. A listener asked for TLS with no context
        therefore refuses the connection instead of falling through to the cleartext
        return above, and neither failing path reaches a handler. That is what makes
        "a TLS bind never answers plaintext" a property of this function rather than
        of the order of two assignments in `start`.
        """
        sock, address = super().get_request()
        if self.adapter.bind.tls is None:
            return sock, address
        sock.settimeout(self.adapter.request_deadline)
        context = self.tls
        try:
            if context is None:
                raise OSError("HTTP TLS was asked for and no context was loaded")
            return context.wrap_socket(sock, server_side=True), address
        except OSError:
            sock.close()
            raise

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Contain one connection's failure, and print nothing.

        `socketserver` prints a traceback to stderr here by default. That traceback
        renders the request being served, and this is the one place in the module a
        credential-bearing frame could reach an output stream. Containment is the
        contract; observability belongs to whoever runs the service, exactly as it
        does on the local transport.
        """


class _HttpService6(_HttpService):
    """The IPv6 listener. `::1` is opt-in and needs its own address family."""

    address_family = socket.AF_INET6


class _Handler(BaseHTTPRequestHandler):
    """One request, refused or routed.

    Everything a refusal says is its status code. There is no body on any non-2xx
    response, which is both the reason no credential and no path can appear in one and
    the reason no second error catalogue can appear either: there is nowhere to put a
    code.
    """

    server_version = "omnivia-core"
    #: The interpreter's version is server state, and a `Server:` header naming it
    #: tells an unauthenticated caller which CPython this runs on.
    sys_version = ""
    #: HTTP/1.0: one request per connection, no keep-alive and no pipelining, which is
    #: the same unary-per-connection rule OVC1 enforces on the local transport.
    protocol_version = "HTTP/1.0"
    #: A floor, not the bound. `StreamRequestHandler` applies this to the socket, and
    #: a socket timeout is **per operation**: it bounds one `recv`, so a client that
    #: sends one byte inside every window extends the connection for as long as it
    #: keeps dripping. Inheriting this number and calling the request bounded is
    #: exactly the unbounded-wait defect the OVC1 lane fixed -- `transport.py`
    #: computes a *total* deadline once and shrinks what remains on each chunk. The
    #: total bound here is `_expire` below; this stays as the per-operation floor and
    #: as the bound on the response write, which is what the local transport also
    #: leaves to the plain socket timeout.
    timeout = DEFAULT_TIMEOUT_SECONDS

    def setup(self) -> None:
        """Arm one deadline for the whole of reading one request.

        A watchdog rather than a shrinking per-chunk timeout, because the reads that
        have to be bounded are not this module's to instrument: the request line and
        the header block are read by `BaseHTTPRequestHandler` through a buffered file
        object, and each `recv` underneath it gets a fresh socket timeout however the
        connection is configured. Shutting the read side down from a timer bounds all
        of them at once, including the ones inside the stdlib.

        The effect at each point it can fire is a bounded refusal rather than a hang,
        and a refusal is what comes back: the header parse completes on EOF rather
        than failing, so an unfinished header block is answered `411` -- it stated no
        length -- and a body that stops short of its declared length is answered
        `400`. Both carry an empty body, like every other refusal here.

        It stays armed for the whole handler and is cancelled in `finish`,
        because :meth:`_expire` closes only the read side -- a handler still running
        when it fires is not interrupted and its response still goes out, so there is
        nothing a mid-request disarm would buy.
        """
        super().setup()
        self._deadline = threading.Timer(
            self.server_adapter.request_deadline, self._expire
        )
        self._deadline.daemon = True
        self._deadline.start()

    def _expire(self) -> None:
        """Stop the caller spending any more of this server's time.

        The **read** side only. That is what makes this a bound on the caller rather
        than on the request: dispatch keeps running and the response still writes, so
        a handler that is merely slow is not cut off by a deadline meant for a client
        that will not finish speaking. Every remaining read returns EOF, which is an
        unfinished header block answered `411` or a short body answered `400`.

        Over TLS it is not a half-close, and that is assessed rather than assumed:
        `ssl.SSLSocket.shutdown` sets `self._sslobj = None` before delegating, so the
        session ends in both directions and the next read finds no SSL object at all.
        The handler unwinds into `handle_error`, which prints nothing, and the caller
        gets a close rather than a `411`. Stricter containment, not weaker, and the
        bound is the same one -- the caller is let go of on time and the serialized
        listener is free for the next, which is the property the OVC1 lane's fix is
        about. Reaching around `SSLSocket.shutdown` for a true half-close would mean
        driving stdlib internals to buy a status code on a request that never finished
        arriving, and the merged deadline never promised one.
        """
        try:
            self.connection.shutdown(socket.SHUT_RD)
        except OSError:
            pass

    def finish(self) -> None:
        self._deadline.cancel()
        super().finish()

    def log_message(self, format: str, *args: Any) -> None:
        """Nothing is logged.

        The default writes the request line to stderr, and the request line is the
        caller's own path. A planted path -- or a caller that puts anything at all in
        one -- would be republished into this service's output by the act of being
        refused. This module has no logger and does not acquire one to spend on that.
        """

    def send_error(
        self, code: int, message: str | None = None, explain: str | None = None
    ) -> None:
        """Refuse with the status and nothing else.

        Overridden rather than inherited because the default body is an HTML page
        quoting the request: `send_error(501)` names the method the caller invented
        and `send_error(400)` the request line it sent. That is a reflection of
        caller-controlled bytes into a response, and it is reached without this
        handler's own code running at all -- `handle_one_request` calls it for an
        unknown verb, an over-long request line and a malformed header block.
        """
        del message, explain
        self._refuse(code)

    # --- the two routes -------------------------------------------------------

    def do_POST(self) -> None:
        path = self.path
        if path not in (APPLICATION_PATH, PROBE_PATH):
            # A query string lands here too, and is meant to: `/v1/probe?token=...`
            # is not one of the two routes, so a credential in a URL is refused by
            # the routing rule rather than by a rule about credentials.
            self._refuse(HTTPStatus.NOT_FOUND)
            return

        # Authentication first, and before the body is read. This is what makes
        # "an unauthenticated request never reaches dispatch" a property of the
        # order of these lines rather than a claim about a status code.
        session: AuthenticatedSession | None = None
        if path == APPLICATION_PATH:
            session = self._session()
            if session is None:
                self._refuse(HTTPStatus.UNAUTHORIZED)
                return

        length = self._body_length()
        if length is None:
            return
        if not self._media_type_is_canonical():
            self._refuse(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return

        body = self.rfile.read(length)
        if len(body) != length:
            self._refuse(HTTPStatus.BAD_REQUEST)
            return
        document = _admitted(body)
        if document is None:
            self._refuse(HTTPStatus.BAD_REQUEST)
            return

        # The document's own branch has to be the branch the URL named. Without
        # this, an `operation` document posted to the unauthenticated probe route
        # would be routed -- correctly, by a router that only ever sees documents --
        # straight into application dispatch.
        wanted, refused = (
            (PROBE_FIELD, OPERATION_FIELD)
            if path == PROBE_PATH
            else (OPERATION_FIELD, PROBE_FIELD)
        )
        if wanted not in document or refused in document:
            self._refuse(HTTPStatus.BAD_REQUEST)
            return

        if session is not None and not self._session_admits_request(document, session):
            return

        self._route(document)

    def _route(self, document: Mapping[str, Any]) -> None:
        """Hand the document to the shared router and answer with what comes back.

        HTTP 200 for whatever the router produced -- an application success envelope,
        a typed application error envelope, or a probe result. All three are accepted
        answers to an accepted request, and the freeze is explicit that an application
        error is one of them. Non-2xx is for the cases above, where no accepted
        envelope exists to return.

        Every refusal below is a status code and no body. `ProtocolError`,
        `ProbeError` and `ContractDecodeError` all carry text describing what the
        caller sent, and none of it is repeated back. The bare `Exception` is the same
        per-request containment the local transport applies per connection: the set of
        ways a caller can be bad is not enumerable from here, and one that escaped
        would reach `handle_error` with the request attached.
        """
        try:
            result = self.server_adapter.router.route(document)
            payload = canonical_json_bytes(result.to_wire())
        except (ProtocolError, ProbeError, ContractDecodeError, OVC1Error):
            self._refuse(HTTPStatus.BAD_REQUEST)
            return
        except Exception:  # noqa: BLE001 -- per-request containment; see the docstring
            self._refuse(HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{CONTENT_TYPE}; charset={CHARSET}")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def _method_not_allowed(self) -> None:
        """A known route reached with the wrong verb; anything else is not a route."""
        if self.path in (APPLICATION_PATH, PROBE_PATH):
            self._refuse(HTTPStatus.METHOD_NOT_ALLOWED, allow=True)
            return
        self._refuse(HTTPStatus.NOT_FOUND)

    do_GET = _method_not_allowed
    do_HEAD = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed

    # --- what a request has to carry ------------------------------------------

    @property
    def server_adapter(self) -> HttpListener:
        service = self.server
        assert isinstance(service, _HttpService)
        return service.adapter

    def _session(self) -> AuthenticatedSession | None:
        """The session this request's bearer credential resolves to, or nothing.

        One `Authorization` header exactly. Two is ambiguous, and resolving the
        ambiguity by taking the first -- which is what reading the header normally
        does -- would let a caller present a credential this server verified alongside
        one an intermediary reads differently.

        The credential is a local variable and reaches nothing else. It is not stored
        on the handler, not put in a header, not logged and not placed in the session:
        `AuthenticatedSession` deliberately has no field for it, and what survives
        verification is its *result*.
        """
        headers = self.headers.get_all("Authorization") or []
        if len(headers) != 1:
            return None
        header = headers[0]
        if not header.startswith(BEARER_PREFIX):
            return None
        credential = header[len(BEARER_PREFIX) :]
        if not credential:
            return None
        session = _resolved(self.server_adapter.resolver, credential)
        # Checked rather than trusted: an injected resolver can return anything, and
        # a truthy non-session would otherwise authenticate the request and then fail
        # inside the claims gate with the wrong error.
        if not isinstance(session, AuthenticatedSession):
            return None
        return session

    def _body_length(self) -> int | None:
        """The exact body length, or `None` once the request has been refused.

        Chunked bodies are out in v1, and a request with no length is one this server
        would have to read to EOF -- which is the unbounded read the local transport
        was fixed to stop doing. Both are `411`: what is missing is a length.

        A length that is stated twice, or is not a plain count, is ambiguous rather
        than absent. `5, 5` (the folded form of a duplicate) and `+5` and `0x10` all
        reach here as one header value, and `-1` and `٥` are worse: `int()` accepts
        both, so the digit check is ASCII-only and the sign is refused with it.
        """
        if self.headers.get("Transfer-Encoding") is not None:
            self._refuse(HTTPStatus.LENGTH_REQUIRED)
            return None
        stated = self.headers.get_all("Content-Length") or []
        if not stated:
            self._refuse(HTTPStatus.LENGTH_REQUIRED)
            return None
        if len(stated) != 1:
            self._refuse(HTTPStatus.BAD_REQUEST)
            return None
        raw = stated[0].strip()
        if not raw.isascii() or not raw.isdigit():
            self._refuse(HTTPStatus.BAD_REQUEST)
            return None
        length = int(raw)
        if length > MAXIMUM_JSON_BYTES:
            # Refused on what the request *declares*, before a byte of it is read.
            # Reading it first to find out how big it is would be the caller
            # choosing how much memory this server spends.
            self._refuse(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return None
        return length

    def _media_type_is_canonical(self) -> bool:
        """Whether the body claims to be canonical JSON, and nothing else.

        Parsed by the stdlib's own header parser rather than by splitting on `;`.
        Canonical JSON is UTF-8, so a stated charset may only restate that; an absent
        one is accepted because the media type already says it.
        """
        if self.headers.get_content_type() != CONTENT_TYPE:
            return False
        charset = self.headers.get_param("charset")
        return charset is None or (
            isinstance(charset, str) and charset.lower() == CHARSET
        )

    def _session_admits_request(
        self, document: Mapping[str, Any], session: AuthenticatedSession
    ) -> bool:
        """Whether the session may run this request here, refusing if not.

        Decoded through the contract's own public decoder, which is also what the
        router will use -- so this reads the request the router will route, not a
        second reading of the same bytes by a second set of rules.
        """
        try:
            request = RequestEnvelope.from_wire(document)
        except ContractDecodeError:
            self._refuse(HTTPStatus.BAD_REQUEST)
            return False
        if not _session_admits(request, session, self.server_adapter.principal):
            self._refuse(HTTPStatus.FORBIDDEN)
            return False
        return True

    def _refuse(self, status: int, *, allow: bool = False) -> None:
        """Answer with a status and a zero-length body.

        No body at all, on any refusal. Nothing this server knows -- a path, a
        credential, a parser's complaint, a resolver's failure -- has anywhere to go,
        so keeping them out of a refusal is structural rather than a rule each call
        site has to remember.
        """
        self.send_response(status)
        if allow:
            self.send_header("Allow", "POST")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True


@dataclass
class HttpListener:
    """The HTTP v1 listener: loopback in the clear, or any address over TLS.

    **The name states no bind policy, because this class holds none.** The policy
    lives on :class:`HttpBind`, which is what refuses a hostname, refuses a
    non-loopback address in the clear, and admits one carrying :class:`HttpTls` --
    so a bind handed to this listener may well be routable, and nothing about an
    instance of it should be read as a promise that the address is local.

    Constructed with the *same* :class:`DocumentRouter` the local transport is given,
    which is the whole of how the two share a probe router and an application
    dispatcher. Nothing local-IPC-specific is imported to do it: the router is passed
    in already composed, so this module never learns that a socket or a pipe exists.

    `principal` is the principal the dispatcher behind `router` actually acts as. It
    has no default, because a default is a guess about somebody else's `Grant` and a
    wrong guess admits sessions this endpoint cannot honestly serve. It is also
    cross-checked against the dispatcher wherever the dispatcher can say -- see
    :func:`_dispatch_principal` -- so in the shipped wiring the two cannot drift
    apart, and where they can (a test double that states nothing) only the
    declaration governs. See :func:`_session_admits` for what it decides.

    `resolver` is required in fact and optional in the type, deliberately. Absent is a
    configuration mistake with exactly one safe response -- refuse to start -- and
    making the field non-optional would move that refusal to a `TypeError` at a call
    site instead of a stated one here.
    """

    router: DocumentRouter
    principal: str
    resolver: CredentialResolver | None = None
    bind: HttpBind = field(default_factory=HttpBind)
    #: The total budget for reading one request, not a per-read one. Named on the
    #: adapter so a test can assert the bound in a second rather than in ten.
    request_deadline: float = DEFAULT_TIMEOUT_SECONDS
    _service: _HttpService | None = None
    _thread: threading.Thread | None = None

    def __post_init__(self) -> None:
        if self.resolver is None:
            raise HttpTransportError(
                "refusing to serve HTTP without a trusted credential resolver"
            )
        # `principal` and `router` are two arguments, and nothing about passing them
        # separately stops them disagreeing. A wiring that declares one principal
        # over a dispatcher acting as another silently reopens the hole the gate in
        # `_session_admits` closes: sessions for the *declared* principal would be
        # admitted and then executed as the dispatcher's. So where the dispatcher can
        # state who it acts as -- which is the case for the shipped `service.main`
        # wiring an approved embedder drives -- the declaration is checked against it
        # rather than trusted.
        acts_as = _dispatch_principal(self.router)
        if acts_as is not None and acts_as != self.principal:
            raise HttpTransportError(
                "HTTP declares a principal the dispatcher behind its router does "
                "not act as"
            )

    def start(self) -> str:
        """Bind, serve, and return the URL actually bound."""
        if self._service is not None:
            raise HttpTransportError("HTTP transport is already started")
        # Before the socket, deliberately. Material that cannot be loaded refuses with
        # nothing bound at all, rather than with a listener up and a refusal that
        # arrives one handshake at a time -- which is a live port answering nothing.
        tls = None if self.bind.tls is None else _tls_context(self.bind.tls)
        service_class = _HttpService6 if self.bind.is_ipv6 else _HttpService
        service: _HttpService | None = None
        failed = False
        try:
            service = service_class((self.bind.host, self.bind.port), _Handler)
        except OSError:
            # Normalized, and raised outside the handler. The OS error names the
            # address and the errno, and it stays reachable through `__context__`
            # even under `from None`, so the refusal is built where there is no
            # exception being handled at all.
            failed = True
        if failed:
            raise HttpTransportError("HTTP transport could not bind")
        assert service is not None
        service.adapter = self
        service.tls = tls
        self._service = service
        self._thread = threading.Thread(
            target=service.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="omnivia-http",
            daemon=True,
        )
        self._thread.start()
        return self.url

    @property
    def url(self) -> str:
        """The URL this adapter is reachable at, after binding.

        Read from the bound socket rather than from `bind`, so a port of `0` reports
        the port the OS chose instead of the request that it choose one.

        The scheme is read the same way -- from the configuration that decides what is
        served -- rather than written as a literal. A hardcoded `http://` became a
        defect the moment TLS landed: it is what a caller connects to, and pointing a
        cleartext client at a TLS listener is a downgrade attempt this adapter would
        have invited itself.
        """
        service = self._service
        host, port = self.bind.host, self.bind.port
        if service is not None:
            address = service.server_address
            host, port = str(address[0]), int(address[1])
        scheme = "http" if self.bind.tls is None else "https"
        if ":" in host:
            return f"{scheme}://[{host}]:{port}"
        return f"{scheme}://{host}:{port}"

    def stop(self) -> None:
        service, thread = self._service, self._thread
        self._service, self._thread = None, None
        if service is None:
            return
        service.shutdown()
        service.server_close()
        if thread is not None:
            thread.join(timeout=DEFAULT_TIMEOUT_SECONDS)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


__all__ = [
    "APPLICATION_PATH",
    "BEARER_PREFIX",
    "CONTENT_TYPE",
    "DEFAULT_HOST",
    "PROBE_PATH",
    "CredentialResolver",
    "HttpBind",
    "HttpListener",
    "HttpTls",
    "HttpTransportError",
    "parse_http_endpoint",
]
