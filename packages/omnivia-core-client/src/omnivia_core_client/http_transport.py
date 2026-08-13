"""The authenticated HTTP v1 :class:`ClientTransport`, the other end of the wire.

The peer is the runtime's HTTP adapter, and this dials the two routes it serves:
``POST /v1/application`` and ``POST /v1/probe``. Everything about the exchange is
the mirror of what that adapter enforces, and where the two could drift they are
made the same thing instead: the body admitted on arrival here is admitted by
:func:`~omnivia_core_client.decode_frame`, which is the same decoder the server
hands its request bodies to, so one canonical-JSON policy governs both directions
of both transports.

**No OVC1 frame travels over HTTP.** The body is bare canonical JSON, which is
what the server reads; the eight-byte header is put in front of a *received*
body locally, purely to reach the decoder, and never goes on a socket. That is
the same construction the server's own ``_admitted`` uses, and it is why there is
no second copy of "these bytes are an admissible document" in this package.

This is the only module in this package permitted to import ``http``, ``ssl``,
``urllib`` and ``ipaddress``, and ``tests/test_package_isolation.py`` pins all
four to this file by name. The rest of the package stays free of them: the
boundary that defines this distribution is its dependency edge -- still exactly
``omnivia-core`` -- and a third-party HTTP client remains forbidden outright.

Four things are decided here, and each of them is a refusal.

**The endpoint.** :func:`parse_http_endpoint` accepts a scheme, a host and a port
and nothing else. Anything in the ``userinfo`` position, a query, a fragment or a
path past ``/`` is refused rather than ignored: a credential must never travel in
a URL, and the way to guarantee that is to refuse a URL with a place to put one.
``urlsplit`` does the parsing rather than a hand-written splitter -- it is the
same parser the server uses, it owns the bracketed-IPv6 rules, and it raises on
malformed input in ways a hand-written one would silently accept. It *does*
raise, which is easy to miss because it looks total, so the split is guarded like
everything else here and the refusal is built where no exception is being
handled.

**Cleartext.** ``http://`` reaches a loopback **IP literal** and nothing else,
decided by ``ipaddress`` rather than by spelling. ``localhost`` is refused rather
than resolved: what a name resolves to is host configuration, and a policy an
``/etc/hosts`` line can move is not a policy. Every other endpoint is ``https://``
with :func:`ssl.create_default_context`, which is certificate verification and
hostname checking on. **There is no argument, field, environment read or code
path in this module that turns either off**, which is a stronger statement than a
default: a caller cannot ask for an unverified connection, so no caller can be
persuaded to.

**The credential.** One :class:`~omnivia_core_client.credentials.CredentialReference`
per transport, resolved through the injected
:class:`~omnivia_core_client.credentials.CredentialCache` against **this
endpoint's normalized origin**, at the moment of each call. The resolved secret
lives in a local variable and reaches exactly one place: the ``Authorization``
header of the request being sent. It is not in the URL, not in a field of this
transport, not in a diagnostic, not in a log -- this module has no logger -- and
not in any exception raised here. Nothing in this module renders the endpoint
either, for the same reason ``local_ipc.py`` renders no socket path: a diagnostic
is the value most likely to be logged.

It is sent on every ``/v1/application`` request and on the ``service.discover``
probe, and on nothing else. Health and readiness are the accepted unauthenticated
pair, and presenting a credential to a route that does not verify one would be
spending it for nothing.

**What comes back.** HTTP 200 is the only status that carries an answer, and both
of the answers it can carry -- a success envelope and a typed application *error*
envelope -- are returned normally, because an application error is an answer. Any
other status is reported as a
:class:`~omnivia_core_client.errors.TransportError` naming the number and nothing
else. It is deliberately not translated into a credential outcome: 401 and 403
are the server's own gate, this module cannot see which rule produced one, and a
client that guessed would be publishing a security decision it does not hold.
Redirects are not followed, and not because they are checked for -- ``http.client``
follows nothing, so a 3xx is an unexpected status like any other.

The connection model matches the server's, which is strictly unary: one
connection carries one request and one response, ``Connection: close``, and is
then closed. No pooling, no reuse, no keep-alive, and no retry -- a call that
failed is reported as having failed.

Standard library plus the public ``omnivia_core`` contracts only.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import ssl
from dataclasses import dataclass
from typing import Any, Final, NoReturn
from urllib.parse import urlsplit

from omnivia_core.contracts.v1 import (
    ContractDecodeError,
    RequestEnvelope,
    ResponseEnvelope,
    ServiceProbeRequest,
    ServiceProbeResult,
    codec,
    decode_service_probe_result,
)
from omnivia_core_client.credentials import CredentialCache, CredentialReference
from omnivia_core_client.deadline import CancellationToken, Deadline
from omnivia_core_client.errors import (
    DeadlineExceededError,
    ProtocolError,
    TransportError,
)
from omnivia_core_client.framing import (
    LENGTH_BYTES,
    MAGIC,
    MAXIMUM_JSON_BYTES,
    canonical_json_bytes,
    decode_frame,
)
from omnivia_core_client.transport import enforce_send_preconditions

__all__ = [
    "APPLICATION_PATH",
    "BEARER_PREFIX",
    "CONTENT_TYPE",
    "PROBE_PATH",
    "HttpEndpoint",
    "HttpTransport",
    "parse_http_endpoint",
]

APPLICATION_PATH: Final = "/v1/application"
"""The application route, spelled as the server spells it."""

PROBE_PATH: Final = "/v1/probe"
"""The probe route, spelled as the server spells it."""

CONTENT_TYPE: Final = "application/json"
"""The canonical JSON media type. Canonical JSON is UTF-8 by definition, so a
stated charset may say that and nothing else."""

CHARSET: Final = "utf-8"

BEARER_PREFIX: Final = "Bearer "

#: The default port each scheme carries when the endpoint states none. Named so
#: the normalized origin always has an explicit port -- which is what makes two
#: spellings of one endpoint one cache key rather than two.
_DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}

#: The one probe the server authenticates. Health and readiness publish only the
#: caller's echoed request id and the frozen public status facts; discovery
#: publishes the endpoint descriptor whole, so it is held to the same bearer
#: credential the application route is. Spelled here as ``discovery.py`` already
#: spells it, from the contract's own probe kind.
_DISCOVER_PROBE: Final = "service.discover"

_HOSTNAME_PATTERN: Final = re.compile(
    r"\A(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*\Z"
)
"""The lowercase ASCII DNS-host form admitted by :class:`HttpEndpoint`."""


#: Raised from outside the ``except`` blocks that decide on them, never inside.
#:
#: ``raise X from None`` inside a handler clears ``__cause__`` and leaves
#: ``__context__`` pointing at the exception being handled -- and here that is an
#: ``OSError`` naming a host and port, an ``ssl`` error naming a certificate
#: subject, or a ``ValueError`` from ``urlsplit`` or ``ipaddress`` **quoting the
#: endpoint text it could not read**. One attribute access on ``__context__``
#: recovers all of it. Raising after the handler ends means there is no exception
#: being handled, so ``__context__`` is genuinely ``None``. Same shape as
#: ``local_ipc.py`` and ``discovery.py``, and
#: ``scripts/check-raise-discipline.py`` is what keeps it that way.


def _raise_malformed_endpoint() -> NoReturn:
    raise TransportError("the endpoint is not a well-formed URL")


def _raise_unsupported_scheme() -> NoReturn:
    raise TransportError("this transport dials http and https endpoints only")


def _raise_endpoint_is_more_than_an_authority() -> NoReturn:
    raise TransportError(
        "the endpoint must name a host and port only, with no path, query or fragment"
    )


def _raise_endpoint_carries_userinfo() -> NoReturn:
    raise TransportError("the endpoint must carry no credential")


def _raise_endpoint_states_no_host() -> NoReturn:
    raise TransportError("the endpoint states no host")


def _raise_unusable_port() -> NoReturn:
    raise TransportError("the endpoint states an unusable port")


def _raise_cleartext_off_loopback() -> NoReturn:
    raise TransportError(
        "a cleartext http endpoint must be a loopback IP literal; every other "
        "endpoint requires https"
    )


def _raise_connect_deadline() -> NoReturn:
    raise DeadlineExceededError("the deadline passed while sending the request")


def _raise_unreachable() -> NoReturn:
    raise TransportError("the endpoint could not be reached")


def _raise_not_http() -> NoReturn:
    raise ProtocolError("the answer is not a well-formed HTTP response")


def _raise_read_deadline() -> NoReturn:
    raise DeadlineExceededError("the deadline passed while reading the response")


def _raise_dropped_mid_response() -> NoReturn:
    raise TransportError("the endpoint dropped the call mid-response")


def _raise_refused(status: int) -> NoReturn:
    raise TransportError(f"the endpoint refused the call with HTTP status {status}")


def _raise_not_a_response_envelope() -> NoReturn:
    raise ProtocolError("the answer is not a well-formed response envelope")


def _raise_not_a_probe_result() -> NoReturn:
    raise ProtocolError("the answer is not a well-formed probe result")


def _is_loopback_literal(host: str) -> bool:
    """Whether ``host`` is an IP literal the loopback rule admits.

    ``ipaddress`` decides, so the rule is the address family's own rather than a
    list of spellings someone remembered: ``127.0.0.1``, every other
    ``127.0.0.0/8`` address and ``::1`` are loopback, and ``0.0.0.0``, ``::`` and
    every routable address are not. A *name* is not an IP literal and so is not
    loopback here, which is the whole point -- ``ip_address`` quotes the value it
    could not read, so its ``ValueError`` is handled here and answered with
    ``False`` rather than allowed to become an exception carrying the host.
    """
    address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    return address is not None and address.is_loopback


def _normalized_host(host: str) -> str | None:
    """Return one canonical host spelling, or ``None`` for a malformed host.

    IP literals use :mod:`ipaddress`'s canonical spelling. Everything else is a
    lowercase ASCII DNS name whose labels obey the DNS length and hyphen rules.
    Keeping this invariant on the value type means a directly constructed
    endpoint has the same normalized origin as one built from a URL.
    """
    address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    if address is not None:
        return str(address)
    lowered = host.lower()
    if not lowered.isascii() or _HOSTNAME_PATTERN.match(lowered) is None:
        return None
    return lowered


@dataclass(frozen=True, slots=True)
class HttpEndpoint:
    """One normalized HTTP endpoint: a scheme, a host and an explicit port.

    Normally built by :func:`parse_http_endpoint`, which is what knows how to get
    here from a URL. **The transport rules live here rather than only there**, so
    they hold for every instance however it was made: a caller who constructs one
    directly, or reaches for ``dataclasses.replace``, gets the same refusals. A
    cleartext endpoint that is not a loopback IP literal is unrepresentable, not
    merely unparseable, and that is the difference between a policy and a parser.

    What stays in the parser is everything about *URL syntax* -- userinfo, query,
    fragment, path, a port that is not a number -- because those are facts about a
    string and there is no string here.

    ``host`` is the unbracketed form, because that is what a socket wants;
    :attr:`origin` brackets an IPv6 literal back, because that is what a URL
    origin is. Keeping both spellings derived from one field is what stops the
    two drifting.
    """

    scheme: str
    host: str
    port: int

    def __post_init__(self) -> None:
        if self.scheme not in _DEFAULT_PORTS:
            _raise_unsupported_scheme()
        if not isinstance(self.host, str) or not self.host:
            _raise_endpoint_states_no_host()
        normalized_host = _normalized_host(self.host)
        if normalized_host is None:
            _raise_endpoint_states_no_host()
        object.__setattr__(self, "host", normalized_host)
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            _raise_unusable_port()
        if not 1 <= self.port <= 65535:
            _raise_unusable_port()
        if self.scheme == "http" and not _is_loopback_literal(self.host):
            _raise_cleartext_off_loopback()

    @property
    def origin(self) -> str:
        """The normalized origin this endpoint's credential is bound to.

        Lowercase scheme and host, an IPv6 literal in brackets, and the port
        always written out even when it is the scheme's default. Every part of
        that is about being *one* string for one endpoint: this is the cache key
        and the value the resolver is asked about, and two spellings of one
        endpoint would be two credentials.
        """
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"


def parse_http_endpoint(endpoint_uri: str) -> HttpEndpoint:
    """The endpoint an ``http://host:port`` or ``https://host:port`` URI names.

    What is decided here is everything about the *string*: whether it is a URL at
    all, whether it carries userinfo, a query, a fragment or a path, and what its
    effective port is. What an endpoint may *be* is decided by
    :class:`HttpEndpoint` itself, so a caller cannot get past a rule by not using
    this function.

    Every refusal is a :class:`~omnivia_core_client.errors.TransportError` raised
    before anything is dialled, and none of them quotes the URI.
    """
    parts: Any = None
    try:
        parts = urlsplit(endpoint_uri)
    except (AttributeError, TypeError, ValueError):
        # `urlsplit` raises `ValueError` for a malformed bracketed netloc --
        # `http://[::1` is "Invalid IPv6 URL" and `http://[text]:1` quotes the
        # bracketed text -- and `AttributeError`/`TypeError` for a non-string.
        pass
    if parts is None:
        _raise_malformed_endpoint()
    if parts.scheme not in _DEFAULT_PORTS:
        _raise_unsupported_scheme()
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        _raise_endpoint_is_more_than_an_authority()
    if parts.username is not None or parts.password is not None:
        _raise_endpoint_carries_userinfo()

    host: str | None = None
    port: int | None = None
    unusable_port = False
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        # `urlsplit().port` quotes the port text it refused.
        unusable_port = True
    if unusable_port:
        _raise_unusable_port()
    if not host:
        _raise_endpoint_states_no_host()
    if port is None:
        port = _DEFAULT_PORTS[parts.scheme]
    # The port range and the cleartext-loopback rule are `HttpEndpoint`'s, and
    # this constructor is where they run.
    return HttpEndpoint(scheme=parts.scheme, host=host, port=port)


def _rearm(
    connection: http.client.HTTPConnection,
    *,
    deadline: Deadline,
    cancellation: CancellationToken | None,
    operation: str,
) -> None:
    """Re-check the whole-call budget and apply what is left to the live socket.

    Called before every blocking wait rather than once before the first, so a
    slow connect cannot buy the response read a fresh budget. It is
    :func:`~omnivia_core_client.transport.enforce_send_preconditions` doing the
    checking -- cancellation first, then the deadline -- because that is the one
    place this package states that order and a second statement of it would
    drift.
    """
    remaining = enforce_send_preconditions(
        deadline=deadline, cancellation=cancellation, operation=operation
    )
    sock = connection.sock
    if sock is not None:
        sock.settimeout(remaining)


def _response_length(response: http.client.HTTPResponse) -> int:
    """The exact body length this response declares, or the reason there is none.

    Read from the header rather than trusted to ``http.client``'s own accounting,
    and applied before a byte of the body is read: a length this client did not
    check is the peer choosing how much memory this process spends.

    A length that is stated twice arrives here folded into one value -- ``5, 5``
    -- and a stated ``Transfer-Encoding`` means there is no length at all. Both
    are refused. ``+5``, ``0x10`` and ``-1`` all reach ``int()`` intact, and ``٥``
    would too, so the digit check is ASCII-only and the sign is refused with it.
    """
    if response.getheader("Transfer-Encoding") is not None:
        _raise_not_http()
    stated = response.getheader("Content-Length")
    if stated is None:
        _raise_not_http()
    raw = stated.strip()
    if not raw.isascii() or not raw.isdigit():
        _raise_not_http()
    length = int(raw)
    if length == 0:
        raise ProtocolError("the answer declares a zero-byte body")
    if length > MAXIMUM_JSON_BYTES:
        raise ProtocolError(
            f"the answer declares {length} body bytes, above the "
            f"{MAXIMUM_JSON_BYTES}-byte maximum"
        )
    return length


def _media_type_is_canonical(response: http.client.HTTPResponse) -> bool:
    """Whether the body claims to be canonical JSON, and nothing else.

    Parsed by the standard library's own header parser rather than by splitting
    on ``;``. Canonical JSON is UTF-8, so a stated charset may only restate that;
    an absent one is accepted because the media type already says it.
    """
    if response.headers.get_content_type() != CONTENT_TYPE:
        return False
    charset = response.headers.get_param("charset")
    return charset is None or (isinstance(charset, str) and charset.lower() == CHARSET)


def _read_document(
    connection: http.client.HTTPConnection,
    response: http.client.HTTPResponse,
    *,
    deadline: Deadline,
    cancellation: CancellationToken | None,
    operation: str,
) -> dict[str, Any]:
    """The answer's body, admitted as the document local IPC would have admitted.

    Admission is OVC1's -- UTF-8, valid JSON, an object root, and RFC 8785
    canonical form -- and it is *reused* rather than restated: the body is handed
    to :func:`~omnivia_core_client.decode_frame` behind OVC1's own header, so the
    same bytes are accepted or refused on both transports by the same code. The
    header is built here and never sent; no frame goes over HTTP in either
    direction.
    """
    if response.status != 200:
        _raise_refused(response.status)
    if not _media_type_is_canonical(response):
        raise ProtocolError("the answer does not carry canonical JSON")
    length = _response_length(response)

    _rearm(
        connection, deadline=deadline, cancellation=cancellation, operation=operation
    )
    body = b""
    failure = ""
    try:
        body = response.read(length)
    except TimeoutError:
        failure = "deadline"
    except OSError:
        failure = "dropped"
    except http.client.IncompleteRead:
        failure = "dropped"
    except http.client.HTTPException:
        failure = "protocol"
    if failure == "deadline":
        _raise_read_deadline()
    if failure == "dropped":
        _raise_dropped_mid_response()
    if failure == "protocol":
        _raise_not_http()
    if len(body) != length:
        # A short read is the connection ending, not a malformed document: the
        # length was well formed and the peer simply stopped. Reported the same
        # way `local_ipc.py` reports a peer that closed mid-frame.
        raise TransportError(
            f"the endpoint closed after {len(body)} of {length} body bytes"
        )
    return decode_frame(MAGIC + length.to_bytes(LENGTH_BYTES, "big") + body)


@dataclass(frozen=True, slots=True, eq=False)
class HttpTransport:
    """A :class:`~omnivia_core_client.ClientTransport` over one HTTP v1 endpoint.

    Frozen and holding three things it does not mutate: where to dial, which
    credential to present there, and the cache that turns the second into a
    secret. There is no connection to keep, so there is no connection state to
    get wrong; structural typing is what makes this a ``ClientTransport``, and
    nothing here inherits from the protocol.

    ``eq=False`` because ``credentials`` is a live object with identity, and a
    generated value equality over it would be a comparison nobody asked for on a
    thing that holds secrets.

    The credential is resolved per call rather than once at construction, which
    is what lets a host revoke or rotate one without this transport being rebuilt
    -- and what keeps a secret out of this object's fields.
    """

    endpoint: HttpEndpoint
    credential_reference: CredentialReference
    credentials: CredentialCache

    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        """One unconnected connection object, configured for this endpoint.

        Nothing is dialled here -- ``http.client`` connects lazily on the first
        request -- so this is where the *decision* about TLS lives and nowhere
        else. An ``https`` endpoint gets :func:`ssl.create_default_context`,
        which is hostname checking and certificate verification on, and there is
        no branch, argument or attribute that produces any other context.

        The context is built per call. It loads the system trust store each time,
        which is real work at connection setup and is accepted here: this
        transport already opens one connection per call, and a module-level
        context would be shared mutable TLS configuration that any caller holding
        this package could reach in and change.
        """
        endpoint = self.endpoint
        if endpoint.scheme == "https":
            return http.client.HTTPSConnection(
                endpoint.host,
                endpoint.port,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(endpoint.host, endpoint.port, timeout=timeout)

    def _headers(self, body: bytes, *, authenticated: bool) -> dict[str, str]:
        """The request headers, including the bearer credential when one is due.

        The secret is read here and lives in this dictionary and in the bytes
        ``http.client`` writes from it. It is not stored on this transport, not
        put in the URL, not logged and not repeated into any exception: the whole
        of its lifetime is one request.
        """
        headers = {
            "Content-Type": f"{CONTENT_TYPE}; charset={CHARSET}",
            "Content-Length": str(len(body)),
            "Connection": "close",
        }
        if authenticated:
            credential = self.credentials.credential_for(
                self.credential_reference, self.endpoint.origin
            )
            headers["Authorization"] = BEARER_PREFIX + credential.reveal()
        return headers

    def _exchange(
        self,
        path: str,
        document: dict[str, Any],
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None,
        operation: str,
        authenticated: bool,
    ) -> dict[str, Any]:
        """One request out, one response back, on a connection used for nothing else."""
        enforce_send_preconditions(
            deadline=deadline, cancellation=cancellation, operation=operation
        )
        body = canonical_json_bytes(document)
        headers = self._headers(body, authenticated=authenticated)
        # Re-checked after resolution: an injected resolver may have taken time,
        # and the budget that bounds the connect must be the one that is left.
        remaining = enforce_send_preconditions(
            deadline=deadline, cancellation=cancellation, operation=operation
        )
        connection = self._connect(remaining)
        try:
            failure = ""
            try:
                try:
                    connection.request("POST", path, body=body, headers=headers)
                except TimeoutError:
                    failure = "deadline"
                except OSError:
                    # The operating system's own message names a host and a port,
                    # and an `ssl` failure names a certificate subject. Both are
                    # outside the payload-free rule this package's diagnostics keep,
                    # so the failure keeps its kind and loses its words.
                    failure = "dropped"
                except http.client.HTTPException:
                    failure = "dropped"
            finally:
                # The bearer token must not remain reachable through this
                # frame's locals when a later response or protocol failure is
                # inspected. ``http.client`` has consumed the mapping by the
                # time ``request`` returns (or fails), so erase it immediately.
                headers.clear()
            if failure == "deadline":
                _raise_connect_deadline()
            if failure == "dropped":
                _raise_unreachable()

            _rearm(
                connection,
                deadline=deadline,
                cancellation=cancellation,
                operation=operation,
            )
            response: http.client.HTTPResponse | None = None
            failure = ""
            try:
                response = connection.getresponse()
            except TimeoutError:
                failure = "deadline"
            except OSError:
                # `RemoteDisconnected` is both an `OSError` and a `BadStatusLine`,
                # and it is a peer drop rather than a malformed answer, so it is
                # caught here where the ordering makes that the reading.
                failure = "dropped"
            except http.client.HTTPException:
                failure = "protocol"
            if failure == "deadline":
                _raise_read_deadline()
            if failure == "dropped":
                _raise_dropped_mid_response()
            if failure == "protocol" or response is None:
                _raise_not_http()
            return _read_document(
                connection,
                response,
                deadline=deadline,
                cancellation=cancellation,
                operation=operation,
            )
        finally:
            connection.close()

    def call(
        self,
        request: RequestEnvelope,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ResponseEnvelope:
        """Send one application request and return the peer's response envelope.

        An application error is an answer, so it comes back as an error response
        envelope rather than as an exception. The reply is decoded through the
        public codec, not merely parsed: the codec is what checks the version
        window, the granted authority and the error retry class, and a reply that
        is structurally JSON but semantically impossible must not reach a caller
        as if it were valid.
        """
        document = self._exchange(
            APPLICATION_PATH,
            codec.encode_request(request),
            deadline=deadline,
            cancellation=cancellation,
            operation=request.operation,
            authenticated=True,
        )
        decoded = False
        try:
            response = codec.decode_response(document)
            decoded = True
        except ContractDecodeError:
            pass
        if not decoded:
            _raise_not_a_response_envelope()
        return response

    def probe(
        self,
        request: ServiceProbeRequest,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ServiceProbeResult:
        """Send one runtime probe and return the peer's probe result.

        ``service.discover`` carries the bearer credential because the server
        authenticates it; health and readiness carry none because the server does
        not, and presenting one to a route that verifies nothing would spend it
        for nothing.
        """
        document = self._exchange(
            PROBE_PATH,
            request.to_wire(),
            deadline=deadline,
            cancellation=cancellation,
            operation=str(request.probe),
            authenticated=request.probe == _DISCOVER_PROBE,
        )
        decoded = False
        try:
            result = decode_service_probe_result(document)
            decoded = True
        except ContractDecodeError:
            pass
        if not decoded:
            _raise_not_a_probe_result()
        return result
