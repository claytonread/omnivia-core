"""The authenticated HTTP transport, against a peer that answers exactly the bytes asked of it.

A raw-socket peer rather than ``http.server``, because most of what has to be
pinned here is bytes a well-behaved server never sends: a folded
``Content-Length``, a length that lies, a media type that is not JSON, a status
line that is not one, a body that stops halfway, a connection that goes away
before the first byte. A real server would not produce them and a mock of the
client would not exercise the code that reads them.

What the peer records is as load-bearing as what it returns: every request's
head is kept, so "the credential was sent" and "the credential was not sent" are
both assertions about the bytes that crossed the socket rather than about a
call to a double.

The one cross-package assertion -- this transport against the *real* runtime
listener -- lives in
``packages/omnivia-core-runtime/tests/phase3/protocol/test_client_http_transport.py``,
because it needs the runtime installed and this package must not.

**The file name mirrors the module with one word inserted, deliberately.** The
package's convention is ``test_<module>.py``, but ``test_http_transport.py`` is
already taken by ``omnivia-core-runtime``'s suite for the server side of the same
protocol. Neither ``tests`` directory is a Python package, so pytest names both
modules by basename and a bare repository-wide run collects one and fails on the
other -- which is exactly what ``scripts/check-test-collection.py`` exists to
catch. ``test_transport_protocol.py`` beside this one already carries the same
kind of qualifier for the same kind of reason.
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_client import (
    CancellationToken,
    Credential,
    CredentialCache,
    CredentialMissingError,
    CredentialReference,
    Deadline,
    DeadlineExceededError,
    HttpEndpoint,
    HttpTransport,
    OperationCancelledError,
    ProtocolError,
    TransportError,
    canonical_json_bytes,
    parse_http_endpoint,
)
from omnivia_core_client.http_transport import (
    APPLICATION_PATH,
    BEARER_PREFIX,
    PROBE_PATH,
)

from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    RequestEnvelope,
    ServiceProbeRequest,
    SuccessResponseEnvelope,
    codec,
)

SECRET = "s3cret-material-nobody-should-see"
REFERENCE = CredentialReference("core.default")


# --------------------------------------------------------------------------
# The documents on the wire
#
# Taken from the accepted OVC1 vector manifest rather than hand-built, for the
# same reason the framing suite recomputes it: these are the exact documents the
# frozen format pins, so a body this transport accepts here is a body a second
# implementation is held to. Building a plausible envelope by hand instead would
# test this transport against a shape nothing else agrees to.
# --------------------------------------------------------------------------


MANIFEST: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "ovc1-v1.json").read_text(
        encoding="utf-8"
    )
)


def _vector(vector_id: str) -> Any:
    return next(
        vector["payload"] for vector in MANIFEST["vectors"] if vector["id"] == vector_id
    )


def _request() -> RequestEnvelope:
    return codec.decode_request(_vector("application.request"))


def _success_body() -> bytes:
    return canonical_json_bytes(_vector("application.success"))


def _error_body() -> bytes:
    return canonical_json_bytes(_vector("application.error"))


def _probe_body(probe: str = "service.health") -> bytes:
    """A probe result of the requested kind.

    ``service.discover`` is the health vector with its ``probe`` field swapped
    rather than the manifest's own discovery vector, because that one carries a
    descriptor whose ``ovc1+tcp://`` endpoint the current publication policy no
    longer admits -- ``test_framing.py`` and ``test_ovc1_compatibility.py``
    already exclude it from decode for the same reason. What is being pinned here
    is which route the credential goes on, not the descriptor's own grammar.
    """
    if probe == "service.discover":
        return canonical_json_bytes({**_vector("probe.health.result"), "probe": probe})
    return canonical_json_bytes(
        _vector(
            "probe.readiness.result"
            if probe == "service.readiness"
            else "probe.health.result"
        )
    )


# --------------------------------------------------------------------------
# The peer
# --------------------------------------------------------------------------


class _Peer:
    """A listener that answers with exactly the bytes it was handed.

    ``answer`` is the whole response, status line included, so a test can send
    something that is not a valid HTTP response at all. ``requests`` is what
    arrived, and ``head_of`` is what the assertions about headers read.

    ``hold`` keeps the connection open after whatever was sent, until the peer
    is closed. Without it the connection ends when the handler returns, and a
    test meaning "this peer is slow" would in fact be testing "this peer hung
    up" -- which is a different failure with a different error.
    """

    def __init__(
        self, answer: bytes | None, *, drop: bool = False, hold: bool = False
    ) -> None:
        self.answer = answer
        self.drop = drop
        self.hold = hold
        self.requests: list[bytes] = []
        self._closing = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(5.0)
                received = b""
                try:
                    while b"\r\n\r\n" not in received:
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        received += chunk
                    head, _, rest = received.partition(b"\r\n\r\n")
                    length = _declared_length(head)
                    while len(rest) < length:
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        rest += chunk
                    self.requests.append(head + b"\r\n\r\n" + rest)
                    if self.drop:
                        continue
                    if self.answer is not None:
                        connection.sendall(self.answer)
                    if self.hold:
                        self._closing.wait(30.0)
                except OSError:  # pragma: no cover - a test that closed early
                    pass

    def head_of(self, index: int = 0) -> str:
        return self.requests[index].split(b"\r\n\r\n", 1)[0].decode("latin-1")

    def body_of(self, index: int = 0) -> bytes:
        return self.requests[index].split(b"\r\n\r\n", 1)[1]

    def close(self) -> None:
        self._closing.set()
        self._listener.close()


def _declared_length(head: bytes) -> int:
    for line in head.split(b"\r\n"):
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            return int(value.strip())
    return 0


def _http_answer(
    body: bytes,
    *,
    status: str = "HTTP/1.1 200 OK",
    content_type: str = "application/json; charset=utf-8",
    content_length: str | None = None,
    extra: tuple[str, ...] = (),
) -> bytes:
    head = [status]
    if content_type:
        head.append(f"Content-Type: {content_type}")
    stated = str(len(body)) if content_length is None else content_length
    if stated != "":
        head.append(f"Content-Length: {stated}")
    head.extend(extra)
    head.append("Connection: close")
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body


@pytest.fixture
def peer() -> Iterator[_Peer]:
    """A peer that answers one application success. Tests override ``answer``."""
    served = _Peer(_http_answer(_success_body()))
    try:
        yield served
    finally:
        served.close()


# --------------------------------------------------------------------------
# The transport under test
# --------------------------------------------------------------------------


def _cache(secret: str | None = SECRET) -> CredentialCache:
    return CredentialCache(
        lambda reference, origin: None if secret is None else Credential(secret),
        ttl_seconds=0,
    )


def _transport(
    port: int, *, credentials: CredentialCache | None = None
) -> HttpTransport:
    return HttpTransport(
        endpoint=parse_http_endpoint(f"http://127.0.0.1:{port}"),
        credential_reference=REFERENCE,
        credentials=_cache() if credentials is None else credentials,
    )


def _deadline(seconds: float = 10.0) -> Deadline:
    return Deadline.after(seconds)


# --------------------------------------------------------------------------
# Endpoint normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("uri", "scheme", "host", "port", "origin"),
    [
        (
            "https://core.example",
            "https",
            "core.example",
            443,
            "https://core.example:443",
        ),
        (
            "https://core.example/",
            "https",
            "core.example",
            443,
            "https://core.example:443",
        ),
        (
            "https://core.example:8443",
            "https",
            "core.example",
            8443,
            "https://core.example:8443",
        ),
        ("http://127.0.0.1:8080", "http", "127.0.0.1", 8080, "http://127.0.0.1:8080"),
        ("http://127.0.0.1", "http", "127.0.0.1", 80, "http://127.0.0.1:80"),
        ("http://127.9.9.9:1", "http", "127.9.9.9", 1, "http://127.9.9.9:1"),
        ("http://[::1]:8080", "http", "::1", 8080, "http://[::1]:8080"),
        ("http://[::1]", "http", "::1", 80, "http://[::1]:80"),
        (
            "https://[2001:db8::1]:8443",
            "https",
            "2001:db8::1",
            8443,
            "https://[2001:db8::1]:8443",
        ),
        (
            "https://[2001:DB8::1]",
            "https",
            "2001:db8::1",
            443,
            "https://[2001:db8::1]:443",
        ),
        (
            "HTTPS://Core.Example:443",
            "https",
            "core.example",
            443,
            "https://core.example:443",
        ),
    ],
)
def test_an_endpoint_normalizes_to_one_origin(
    uri: str, scheme: str, host: str, port: int, origin: str
) -> None:
    """Scheme, host and effective port, in the one spelling that is the cache key.

    The port is written out even when it is the scheme's default, and an IPv6
    literal is bracketed back after ``urlsplit`` stripped it -- so
    ``https://core.example`` and ``https://core.example:443`` are one origin and
    one credential rather than two.
    """
    endpoint = parse_http_endpoint(uri)

    assert (endpoint.scheme, endpoint.host, endpoint.port) == (scheme, host, port)
    assert endpoint.origin == origin


REJECTED_ENDPOINTS = [
    ("userinfo", "https://user@core.example"),
    ("userinfo_with_password", "https://user:password@core.example"),
    ("empty_userinfo", "https://@core.example"),
    ("query", "https://core.example?token=abc"),
    ("fragment", "https://core.example#abc"),
    ("path", "https://core.example/v1/application"),
    ("deep_path", "https://core.example/a/b"),
    ("path_and_query", "https://core.example/v1?token=abc"),
    ("unclosed_ipv6", "http://[::1"),
    ("bracketed_text", "http://[not-an-address]:1"),
    ("no_host", "https://"),
    ("no_scheme", "core.example:443"),
    ("unix_scheme", "unix:///run/core.sock"),
    ("pipe_scheme", "pipe://core"),
    ("file_scheme", "file:///etc/passwd"),
    ("ftp_scheme", "ftp://core.example"),
    ("port_out_of_range", "https://core.example:70000"),
    ("port_is_not_a_number", "https://core.example:abc"),
    ("negative_port", "https://core.example:-1"),
    ("empty", ""),
    ("cleartext_hostname", "http://core.example"),
    ("cleartext_localhost", "http://localhost:8080"),
    ("cleartext_routable_ipv4", "http://192.0.2.1:8080"),
    ("cleartext_wildcard", "http://0.0.0.0:8080"),
    ("cleartext_routable_ipv6", "http://[2001:db8::1]:8080"),
]


@pytest.mark.parametrize(
    "uri",
    [uri for _, uri in REJECTED_ENDPOINTS],
    ids=[n for n, _ in REJECTED_ENDPOINTS],
)
def test_a_refused_endpoint_is_refused_without_being_quoted(uri: str) -> None:
    """Every refusal is a ``TransportError`` with nothing chained onto it.

    ``urlsplit`` and ``ipaddress`` both quote the text they could not read, and
    ``__context__`` survives ``raise ... from None`` -- so the absence of both
    links is the assertion, not the rendered message.
    """
    with pytest.raises(TransportError) as caught:
        parse_http_endpoint(uri)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("uri", [None, 7, b"https://core.example"])
def test_an_endpoint_that_is_not_a_string_is_refused_as_one(uri: object) -> None:
    with pytest.raises(TransportError):
        parse_http_endpoint(uri)  # type: ignore[arg-type]


def test_cleartext_is_admitted_only_to_a_loopback_literal() -> None:
    """``localhost`` is refused rather than resolved: what a name resolves to is
    host configuration, and a policy an ``/etc/hosts`` line can move is not one."""
    assert parse_http_endpoint("http://127.0.0.1:1").host == "127.0.0.1"
    assert parse_http_endpoint("http://[::1]:1").host == "::1"
    with pytest.raises(TransportError):
        parse_http_endpoint("http://localhost:1")


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("cleartext_hostname", {"scheme": "http", "host": "core.example", "port": 80}),
        ("cleartext_routable", {"scheme": "http", "host": "192.0.2.1", "port": 80}),
        ("cleartext_wildcard", {"scheme": "http", "host": "0.0.0.0", "port": 80}),
        ("unsupported_scheme", {"scheme": "unix", "host": "127.0.0.1", "port": 80}),
        ("empty_host", {"scheme": "https", "host": "", "port": 443}),
        (
            "host_with_path",
            {"scheme": "https", "host": "core.example/path", "port": 443},
        ),
        (
            "host_with_userinfo",
            {"scheme": "https", "host": "user@core.example", "port": 443},
        ),
        ("port_zero", {"scheme": "https", "host": "core.example", "port": 0}),
        (
            "port_out_of_range",
            {"scheme": "https", "host": "core.example", "port": 70000},
        ),
        (
            "port_is_not_an_int",
            {"scheme": "https", "host": "core.example", "port": "443"},
        ),
        ("port_is_a_bool", {"scheme": "https", "host": "core.example", "port": True}),
    ],
)
def test_the_rules_hold_for_a_hand_built_endpoint_too(
    name: str, kwargs: dict[str, Any]
) -> None:
    """The invariant is on the type, not only on the parser.

    A caller who constructs an endpoint directly, or reaches for
    ``dataclasses.replace``, must not get past the cleartext rule -- otherwise
    "cleartext reaches loopback only" is a statement about one function rather
    than about this transport.
    """
    with pytest.raises(TransportError):
        HttpEndpoint(**kwargs)


def test_replacing_a_field_is_re_checked() -> None:
    """``dataclasses.replace`` re-runs ``__post_init__``, which is the point."""
    import dataclasses

    endpoint = parse_http_endpoint("https://core.example")
    with pytest.raises(TransportError):
        dataclasses.replace(endpoint, scheme="http")


def test_a_hand_built_endpoint_is_normalized_too() -> None:
    endpoint = HttpEndpoint(scheme="https", host="Core.Example", port=443)

    assert endpoint.host == "core.example"
    assert endpoint.origin == "https://core.example:443"


# --------------------------------------------------------------------------
# TLS
# --------------------------------------------------------------------------


def test_an_https_endpoint_connects_with_the_verified_default_context() -> None:
    """Verification and hostname checking on, read off the object that will dial."""
    transport = HttpTransport(
        endpoint=parse_http_endpoint("https://core.example"),
        credential_reference=REFERENCE,
        credentials=_cache(),
    )
    connection = transport._connect(1.0)
    context = connection._context  # type: ignore[attr-defined]

    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert context.get_ca_certs() != []


def test_there_is_no_way_to_ask_for_an_unverified_connection() -> None:
    """A stronger statement than a safe default: a caller cannot ask.

    The transport has three fields and none of them is a context, a verification
    flag or a certificate path, so there is no argument a caller can be talked
    into passing and no attribute a caller can be talked into setting.
    """
    fields = set(HttpTransport.__dataclass_fields__)
    assert fields == {"endpoint", "credential_reference", "credentials"}

    transport = HttpTransport(
        endpoint=parse_http_endpoint("https://core.example"),
        credential_reference=REFERENCE,
        credentials=_cache(),
    )
    with pytest.raises((AttributeError, TypeError)):
        transport.endpoint = parse_http_endpoint("http://127.0.0.1:1")  # type: ignore[misc]


def test_the_module_never_disables_verification() -> None:
    """Asserted on the source, because the risk is a *later* line, not this one."""
    import inspect

    from omnivia_core_client import http_transport

    source = inspect.getsource(http_transport)
    for forbidden in (
        "CERT_NONE",
        "check_hostname = False",
        "check_hostname=False",
        "_create_unverified_context",
        "verify_mode =",
    ):
        assert forbidden not in source, forbidden


def test_a_cleartext_endpoint_uses_no_tls_at_all() -> None:
    transport = _transport(1)
    connection = transport._connect(1.0)

    assert not hasattr(connection, "context")


# --------------------------------------------------------------------------
# The bearer credential
# --------------------------------------------------------------------------


def _header(head: str, name: str) -> str | None:
    for line in head.split("\r\n")[1:]:
        field, _, value = line.partition(":")
        if field.strip().lower() == name.lower():
            return value.strip()
    return None


def test_an_application_request_carries_the_bearer_credential(peer: _Peer) -> None:
    _transport(peer.port).call(_request(), deadline=_deadline())

    head = peer.head_of()
    assert _header(head, "Authorization") == BEARER_PREFIX + SECRET
    assert head.startswith(f"POST {APPLICATION_PATH} ")


def test_the_discovery_probe_carries_the_bearer_credential(peer: _Peer) -> None:
    peer.answer = _http_answer(_probe_body("service.discover"))
    _transport(peer.port).probe(
        ServiceProbeRequest(probe="service.discover"), deadline=_deadline()
    )

    head = peer.head_of()
    assert _header(head, "Authorization") == BEARER_PREFIX + SECRET
    assert head.startswith(f"POST {PROBE_PATH} ")


@pytest.mark.parametrize("probe", ["service.health", "service.readiness"])
def test_the_unauthenticated_probes_carry_no_credential(
    peer: _Peer, probe: str
) -> None:
    """Presenting a credential to a route that verifies nothing spends it for nothing."""
    peer.answer = _http_answer(_probe_body(probe))
    _transport(peer.port).probe(ServiceProbeRequest(probe=probe), deadline=_deadline())

    assert _header(peer.head_of(), "Authorization") is None
    assert SECRET not in peer.requests[0].decode("latin-1")


def test_the_credential_is_never_in_the_request_line(peer: _Peer) -> None:
    """Not in the path, not in a query. The route is the route and nothing else."""
    _transport(peer.port).call(_request(), deadline=_deadline())

    request_line = peer.head_of().split("\r\n")[0]
    assert request_line == f"POST {APPLICATION_PATH} HTTP/1.1"
    assert SECRET not in request_line
    assert SECRET not in peer.body_of().decode("utf-8")


def test_a_credential_failure_stops_the_call_before_a_connection(peer: _Peer) -> None:
    """A resolver that answers nothing costs no socket and reaches no peer."""
    with pytest.raises(CredentialMissingError):
        _transport(peer.port, credentials=_cache(None)).call(
            _request(), deadline=_deadline()
        )

    assert peer.requests == []


def test_the_credential_is_bound_to_this_endpoints_origin(peer: _Peer) -> None:
    asked: list[str] = []

    def resolver(reference: CredentialReference, origin: str) -> Credential:
        asked.append(origin)
        return Credential(SECRET)

    _transport(peer.port, credentials=CredentialCache(resolver, ttl_seconds=0)).call(
        _request(), deadline=_deadline()
    )

    assert asked == [f"http://127.0.0.1:{peer.port}"]


def test_the_credential_reaches_no_diagnostic(peer: _Peer) -> None:
    """Every failure this transport can raise, checked for the secret at once.

    Written as one sweep rather than an assertion inside each failure test,
    because the property is about the *module*: a diagnostic added later that
    renders the request would pass every individual test and fail this one.
    """
    peer.answer = _http_answer(b"", status="HTTP/1.1 401 Unauthorized")
    transport = _transport(peer.port)

    with pytest.raises(TransportError) as caught:
        transport.call(_request(), deadline=_deadline())

    rendered = "\n".join(
        [
            str(caught.value),
            repr(caught.value),
            *(repr(arg) for arg in caught.value.args),
        ]
    )
    assert SECRET not in rendered
    assert "core.default" not in rendered
    assert "127.0.0.1" not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    exchange_frames = []
    trace = caught.value.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code.co_name == "_exchange":
            exchange_frames.append(trace.tb_frame)
        trace = trace.tb_next
    assert len(exchange_frames) == 1
    assert SECRET not in repr(exchange_frames[0].f_locals)


# --------------------------------------------------------------------------
# The exchange
# --------------------------------------------------------------------------


def test_the_request_is_unary_canonical_json_that_closes(peer: _Peer) -> None:
    """The wire shape the server reads: bare canonical JSON, one connection, closed.

    No OVC1 frame. The magic appearing in a request body would mean this
    transport was sending the local transport's framing over HTTP, which the
    server does not read.
    """
    _transport(peer.port).call(_request(), deadline=_deadline())

    head, body = peer.head_of(), peer.body_of()
    assert _header(head, "Content-Type") == "application/json; charset=utf-8"
    assert _header(head, "Connection") == "close"
    assert _header(head, "Content-Length") == str(len(body))
    assert not body.startswith(b"OVC1")
    assert body == canonical_json_bytes(codec.encode_request(_request()))


def test_a_success_envelope_comes_back_decoded(peer: _Peer) -> None:
    response = _transport(peer.port).call(_request(), deadline=_deadline())

    assert isinstance(response, SuccessResponseEnvelope)
    # Round-tripped rather than field-compared: the decoded envelope holds
    # immutable views, and re-encoding is what says the whole document survived.
    assert codec.encode_response(response) == _vector("application.success")


def test_an_application_error_envelope_returns_normally(peer: _Peer) -> None:
    """An application error is an answer, not an exception."""
    peer.answer = _http_answer(_error_body())

    response = _transport(peer.port).call(_request(), deadline=_deadline())

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == _vector("application.error")["error"]["code"]


def test_a_probe_result_comes_back_decoded(peer: _Peer) -> None:
    peer.answer = _http_answer(_probe_body())

    result = _transport(peer.port).probe(
        ServiceProbeRequest(probe="service.health"), deadline=_deadline()
    )

    assert result.probe == "service.health"
    assert result.status == "pass"


def test_each_call_opens_its_own_connection(peer: _Peer) -> None:
    transport = _transport(peer.port)
    transport.call(_request(), deadline=_deadline())
    transport.call(_request(), deadline=_deadline())

    assert len(peer.requests) == 2


# --------------------------------------------------------------------------
# What the peer may answer with, and what it may not
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        "HTTP/1.1 301 Moved Permanently",
        "HTTP/1.1 302 Found",
        "HTTP/1.1 307 Temporary Redirect",
        "HTTP/1.1 308 Permanent Redirect",
    ],
)
def test_a_redirect_is_not_followed(peer: _Peer, status: str) -> None:
    """Reported as the unexpected status it is, and dialled exactly once.

    ``http.client`` follows nothing, so this is a property of the client chosen
    rather than of a check -- and the request count is what says so.
    """
    peer.answer = _http_answer(
        b"",
        status=status,
        extra=("Location: https://elsewhere.example/v1/application",),
    )

    with pytest.raises(TransportError):
        _transport(peer.port).call(_request(), deadline=_deadline())

    assert len(peer.requests) == 1


@pytest.mark.parametrize(
    "status",
    [
        "HTTP/1.1 400 Bad Request",
        "HTTP/1.1 401 Unauthorized",
        "HTTP/1.1 403 Forbidden",
        "HTTP/1.1 404 Not Found",
        "HTTP/1.1 405 Method Not Allowed",
        "HTTP/1.1 411 Length Required",
        "HTTP/1.1 413 Payload Too Large",
        "HTTP/1.1 415 Unsupported Media Type",
        "HTTP/1.1 500 Internal Server Error",
        "HTTP/1.1 204 No Content",
    ],
)
def test_any_status_but_200_is_a_transport_refusal(peer: _Peer, status: str) -> None:
    """One rule for every refusal, and deliberately no credential taxonomy.

    401 and 403 are the server's own gate; this transport cannot see which rule
    produced one, and translating a status into a credential outcome would be
    publishing a security decision it does not hold.
    """
    peer.answer = _http_answer(b"", status=status)

    with pytest.raises(TransportError) as caught:
        _transport(peer.port).call(_request(), deadline=_deadline())

    assert status.split()[1] in str(caught.value)


MALFORMED_ANSWERS = [
    ("not_json", b"this is not JSON"),
    ("json_array_root", b"[]"),
    ("json_scalar_root", b"1"),
    ("json_null_root", b"null"),
    ("non_canonical_spacing", b'{"a": 1}'),
    ("non_canonical_member_order", b'{"b":1,"a":2}'),
    ("invalid_utf8", b"\xff\xfe"),
    ("duplicate_member", b'{"a":1,"a":2}'),
    ("truncated_json", b'{"a":'),
]


@pytest.mark.parametrize(
    "body",
    [body for _, body in MALFORMED_ANSWERS],
    ids=[n for n, _ in MALFORMED_ANSWERS],
)
def test_a_body_the_frame_decoder_refuses_is_a_protocol_error(
    peer: _Peer, body: bytes
) -> None:
    """The same admission the local transport applies, applied to an HTTP body.

    Non-canonical JSON is in this list on purpose: the two transports share one
    canonical-JSON policy, so a spelling refused on one is refused on the other.
    """
    peer.answer = _http_answer(body)

    with pytest.raises(ProtocolError) as caught:
        _transport(peer.port).call(_request(), deadline=_deadline())

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_a_well_formed_document_of_the_wrong_shape_is_a_protocol_error(
    peer: _Peer,
) -> None:
    """Canonical JSON that is not a response envelope. Decoded through the public
    codec rather than merely parsed, so a semantically impossible reply is refused."""
    peer.answer = _http_answer(canonical_json_bytes({"not": "an envelope"}))

    with pytest.raises(ProtocolError):
        _transport(peer.port).call(_request(), deadline=_deadline())


def test_a_probe_answered_with_a_response_envelope_is_a_protocol_error(
    peer: _Peer,
) -> None:
    peer.answer = _http_answer(_success_body())

    with pytest.raises(ProtocolError):
        _transport(peer.port).probe(
            ServiceProbeRequest(probe="service.health"), deadline=_deadline()
        )


BAD_LENGTHS = [
    ("absent", ""),
    ("folded_duplicate", "12, 12"),
    ("signed", "+12"),
    ("negative", "-1"),
    ("hexadecimal", "0x10"),
    # `²` rather than an Arabic-Indic digit because a header block is latin-1 and
    # this is a header a server could actually emit. `str.isdigit()` says yes to
    # it, which is exactly why the check is ASCII-only.
    ("non_ascii_digit", "\xb2"),
    ("not_a_number", "twelve"),
    ("zero", "0"),
    ("over_the_maximum", str(4 * 1024 * 1024 + 1)),
]


@pytest.mark.parametrize(
    "stated", [stated for _, stated in BAD_LENGTHS], ids=[n for n, _ in BAD_LENGTHS]
)
def test_an_unusable_content_length_is_refused_before_the_body_is_read(
    peer: _Peer, stated: str
) -> None:
    """Refused on what the answer *declares*. Reading it first to find out how big
    it is would be the peer choosing how much memory this process spends."""
    peer.answer = _http_answer(_success_body(), content_length=stated)

    with pytest.raises(ProtocolError):
        _transport(peer.port).call(_request(), deadline=_deadline())


def test_a_body_shorter_than_its_declared_length_is_a_dropped_call(peer: _Peer) -> None:
    body = _success_body()
    peer.answer = _http_answer(body[:10], content_length=str(len(body)))

    with pytest.raises(TransportError):
        _transport(peer.port).call(_request(), deadline=_deadline())


def test_a_chunked_answer_is_refused(peer: _Peer) -> None:
    """There is no length to bound the read by, which is the unbounded read this
    package does not do."""
    body = _success_body()
    peer.answer = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        + f"{len(body):x}\r\n".encode()
        + body
        + b"\r\n0\r\n\r\n"
    )

    with pytest.raises(ProtocolError):
        _transport(peer.port).call(_request(), deadline=_deadline())


BAD_MEDIA_TYPES = [
    ("absent", ""),
    ("text", "text/plain"),
    ("html", "text/html; charset=utf-8"),
    ("problem_json", "application/problem+json"),
    ("wrong_charset", "application/json; charset=latin-1"),
    ("octet_stream", "application/octet-stream"),
]


@pytest.mark.parametrize(
    "content_type",
    [value for _, value in BAD_MEDIA_TYPES],
    ids=[n for n, _ in BAD_MEDIA_TYPES],
)
def test_an_answer_that_does_not_claim_canonical_json_is_refused(
    peer: _Peer, content_type: str
) -> None:
    peer.answer = _http_answer(_success_body(), content_type=content_type)

    with pytest.raises(ProtocolError):
        _transport(peer.port).call(_request(), deadline=_deadline())


@pytest.mark.parametrize(
    "content_type", ["application/json", "application/json; charset=utf-8"]
)
def test_a_stated_charset_may_only_restate_utf8(peer: _Peer, content_type: str) -> None:
    """Canonical JSON is UTF-8 by definition, so a stated charset may say that and
    nothing else -- and an absent one is fine because the media type already said it."""
    peer.answer = _http_answer(_success_body(), content_type=content_type)

    assert _transport(peer.port).call(_request(), deadline=_deadline())


@pytest.mark.parametrize(
    ("name", "answer"),
    [
        ("not_a_status_line", b"NOT HTTP AT ALL\r\n\r\n"),
        ("empty", b""),
        ("status_line_only_garbage", b"\x00\x01\x02\r\n\r\n"),
    ],
)
def test_an_answer_that_is_not_http_is_refused(
    peer: _Peer, name: str, answer: bytes
) -> None:
    """A wrong-protocol listener squatting the port is named immediately rather
    than held onto until the deadline and then reported as a slow service."""
    peer.answer = answer

    with pytest.raises((ProtocolError, TransportError)) as caught:
        _transport(peer.port).call(_request(), deadline=_deadline())

    assert caught.value.__context__ is None


def test_a_peer_that_drops_before_answering_is_a_transport_failure(peer: _Peer) -> None:
    peer.drop = True

    with pytest.raises(TransportError):
        _transport(peer.port).call(_request(), deadline=_deadline())


def test_an_endpoint_that_refuses_the_connection_is_a_transport_failure() -> None:
    """A closed port. The refusal keeps its kind and loses the operating system's
    words, which name the host and the port."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    listener.close()

    with pytest.raises(TransportError) as caught:
        _transport(port).call(_request(), deadline=_deadline())

    assert str(port) not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


# --------------------------------------------------------------------------
# The deadline and cancellation
# --------------------------------------------------------------------------


@pytest.fixture
def silent_peer() -> Iterator[_Peer]:
    """Accepts, reads the request, and then says nothing while holding the socket."""
    served = _Peer(None, hold=True)
    try:
        yield served
    finally:
        served.close()


def test_a_peer_that_never_answers_exceeds_the_deadline(silent_peer: _Peer) -> None:
    """The wait on the status line is bounded, which it can only be if the
    remaining budget was applied to the socket before the read."""
    with pytest.raises(DeadlineExceededError):
        _transport(silent_peer.port).call(_request(), deadline=Deadline.after(0.4))


def test_a_peer_that_stalls_after_its_headers_exceeds_the_deadline() -> None:
    """The second blocking wait is bounded separately from the first.

    A slow status line must not buy the body read a fresh budget, which is why
    the budget is re-read before each wait rather than computed once. This peer
    answers with a complete header block declaring a body it then never sends.
    """
    body = _success_body()
    head = _http_answer(body)[: -len(body)]
    served = _Peer(head, hold=True)
    try:
        with pytest.raises(DeadlineExceededError):
            _transport(served.port).call(_request(), deadline=Deadline.after(0.4))
    finally:
        served.close()


def test_a_call_with_no_budget_left_is_never_sent(peer: _Peer) -> None:
    with pytest.raises(DeadlineExceededError):
        _transport(peer.port).call(_request(), deadline=Deadline.after(0.0))

    assert peer.requests == []


def test_a_call_cancelled_before_it_starts_is_never_sent(peer: _Peer) -> None:
    """Cancellation is tested before the deadline, so a call the caller abandoned
    is reported as cancelled even if time also ran out."""
    cancellation = CancellationToken()
    cancellation.cancel()

    with pytest.raises(OperationCancelledError):
        _transport(peer.port).call(
            _request(), deadline=Deadline.after(0.0), cancellation=cancellation
        )

    assert peer.requests == []


def test_a_cancelled_probe_is_never_sent(peer: _Peer) -> None:
    cancellation = CancellationToken()
    cancellation.cancel()

    with pytest.raises(OperationCancelledError):
        _transport(peer.port).probe(
            ServiceProbeRequest(probe="service.health"),
            deadline=_deadline(),
            cancellation=cancellation,
        )

    assert peer.requests == []


def test_a_call_cancelled_while_the_credential_resolves_is_never_sent(
    peer: _Peer,
) -> None:
    """The budget is re-checked after resolution, before anything is dialled.

    A resolver is injected and may take time -- a store, a broker, a prompt --
    and a call the caller abandoned during that time must not then be put on the
    wire. Asserted on the peer having seen nothing, not on the error alone.
    """
    cancellation = CancellationToken()

    def resolver(reference: CredentialReference, origin: str) -> Credential:
        cancellation.cancel()
        return Credential(SECRET)

    transport = HttpTransport(
        endpoint=parse_http_endpoint(f"http://127.0.0.1:{peer.port}"),
        credential_reference=REFERENCE,
        credentials=CredentialCache(resolver, ttl_seconds=0),
    )

    with pytest.raises(OperationCancelledError):
        transport.call(_request(), deadline=_deadline(), cancellation=cancellation)

    assert peer.requests == []


def test_the_transport_satisfies_the_client_transport_protocol() -> None:
    from omnivia_core_client import ClientTransport

    assert isinstance(_transport(1), ClientTransport)


def test_the_transport_holds_no_secret_of_its_own(peer: _Peer) -> None:
    """The credential is resolved per call, so there is nothing on this object to
    read -- which is also what lets a host rotate one without a rebuild."""
    transport = _transport(peer.port)
    transport.call(_request(), deadline=_deadline())

    rendered: Any = repr(transport)
    assert SECRET not in rendered
    assert not any(
        SECRET in repr(getattr(transport, name))
        for name in HttpTransport.__dataclass_fields__
    )
