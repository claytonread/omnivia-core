"""P2b: the loopback HTTP v1 adapter as a transport.

What is pinned here is the transport half of the contract: the two routes and the
refusal of everything else, the media/length/body rules, the status mapping -- HTTP
200 for an accepted application *error* as much as for an accepted success -- the
loopback-only bind policy, and the equivalence between what local IPC and HTTP hand
to and get back from the one shared router.

The refusal corpus is a fixture rather than thirty test bodies, and it is asserted
twice: each case gets the status the contract gives it, *and* the whole table leaves
the injected dispatcher with zero invocations. A refusal that answered 400 after
dispatching would pass the first assertion and fail the second.

Trust is pinned next door in `test_http_security.py`.
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.service.http_transport import (
    APPLICATION_PATH,
    CONTENT_TYPE,
    PROBE_PATH,
    HttpBind,
    HttpListener,
    HttpTls,
    HttpTransportError,
    parse_http_endpoint,
)
from omnivia_core_runtime.service.main import build_parser
from omnivia_core_runtime.service.main import main as service_main
from omnivia_core_runtime.service.operations import failure, success
from omnivia_core_runtime.service.ovc1 import (
    HEADER_BYTES,
    canonical_json_bytes,
    encode_frame,
)
from omnivia_core_runtime.service.probes import PROBE_HEALTH, ProbeRouter, ServiceFacts
from omnivia_core_runtime.service.protocol import (
    OPERATION_FIELD,
    PROBE_FIELD,
    DocumentRouter,
)
from omnivia_core_runtime.service.transport import (
    DEFAULT_TIMEOUT_SECONDS,
    LocalSocketServer,
    endpoint_for_path,
)
from omnivia_core_runtime.service.versions import API_VERSION

from omnivia_core.contracts.v1 import (
    ClientIdentity,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
)

OBSERVED_AT = "2026-08-02T00:00:00Z"
OPERATION = "core.health"
WORKSPACE = "ws-1"
PRINCIPAL = "local-user"
ACCEPTED_CREDENTIAL = "accepted-credential"

FIXTURES = Path(__file__).parent / "fixtures" / "http-v1-cases.json"


# --- the shared router, and a dispatcher that can be counted -------------------


class CountingDispatch:
    """The injected application dispatcher, instrumented.

    `calls` is the assertion that matters for every refusal: a status code says what
    the caller was told, and only this says whether the request was answered before
    it was refused.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[RequestEnvelope] = []
        self.fail = fail

    def __call__(self, request: RequestEnvelope) -> ResponseEnvelope:
        self.calls.append(request)
        if self.fail:
            return failure(
                request,
                "core.operation_not_implemented",
                "this runtime implements no product operations yet",
                principal=PRINCIPAL,
            )
        return success(request, {"ok": True}, principal=PRINCIPAL)


def _facts() -> ServiceFacts:
    return ServiceFacts(
        observed_at=OBSERVED_AT,
        health_status="pass",
        readiness_status="pass",
        discovery_status="pass",
    )


def _router(dispatch: CountingDispatch) -> DocumentRouter:
    return DocumentRouter(
        probes=ProbeRouter(facts=_facts, capabilities=tuple, clock=lambda: 0),
        dispatch=dispatch,
    )


def _session() -> Any:
    from omnivia_core_runtime.service.authorization import AuthenticatedSession

    return AuthenticatedSession(
        principal_id=PRINCIPAL,
        roles=frozenset({"reader"}),
        workspaces=frozenset({WORKSPACE}),
        operations=frozenset({OPERATION}),
        purposes=frozenset({"test"}),
        scopes=frozenset({"workspace:read"}),
    )


def _resolver(credential: str) -> Any:
    return _session() if credential == ACCEPTED_CREDENTIAL else None


def _request(operation: str = OPERATION) -> RequestEnvelope:
    return RequestEnvelope(
        operation=operation,
        metadata=RequestMetadata(
            request_id="req-1",
            correlation_id="corr-1",
            trace_id="trace-1",
            api_version=API_VERSION,
            client=ClientIdentity(id="test-client", version="0.1.0"),
            scopes=("workspace:read",),
            purpose="test",
            required_capabilities=(),
            workspace_id=WORKSPACE,
        ),
        input={},
    )


#: The canonical documents the fixture names. Built from the public contract rather
#: than transcribed, so the corpus cannot drift from the accepted DTOs.
def _documents() -> Mapping[str, bytes]:
    application = dict(_request().to_wire())
    both = dict(application)
    both[PROBE_FIELD] = PROBE_HEALTH
    return {
        "application": canonical_json_bytes(application),
        "probe": canonical_json_bytes({PROBE_FIELD: PROBE_HEALTH}),
        "both": canonical_json_bytes(both),
        "neither": canonical_json_bytes({"metadata": {}, "input": {}}),
    }


# --- raw exchanges ------------------------------------------------------------
#
# Raw sockets rather than `http.client`, because most of the corpus is about bytes a
# well-behaved client refuses to send: two `Content-Length` headers, a folded one, an
# invented verb, a body shorter than its declared length. The response is read to EOF,
# which the adapter guarantees by answering `Connection: close`.


def _exchange(port: int, head: list[str], body: bytes) -> tuple[int, bytes, bytes]:
    """Send exactly these bytes, then read the whole answer. Status, headers, body.

    The head is encoded as latin-1 because that is what an HTTP header block is and
    what `http.server` decodes it back as. Encoding it as UTF-8 would quietly change
    what the parser sees for any non-ASCII byte -- a `Content-Length: \xb26` would
    arrive as two characters that are obviously not digits, and a corpus case about a
    non-decimal digit would pass without ever exercising it.

    The write side is closed once the request is sent. A client that has finished
    sending has finished sending, and saying so is what turns "the declared length
    never arrived" into an immediate short read instead of a wait for the connection
    deadline to expire.
    """
    raw = ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body
    with socket.create_connection(("127.0.0.1", port), timeout=20) as client:
        client.sendall(raw)
        client.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    response = b"".join(chunks)
    head_bytes, _, response_body = response.partition(b"\r\n\r\n")
    status = int(head_bytes.split(b"\r\n", 1)[0].split(b" ")[1])
    return status, head_bytes, response_body


def _post(
    port: int, path: str, body: bytes, *, credential: str | None = None
) -> tuple[int, bytes]:
    head = [
        f"POST {path} HTTP/1.1",
        "Host: localhost",
        f"Content-Type: {CONTENT_TYPE}",
        f"Content-Length: {len(body)}",
    ]
    if credential is not None:
        head.insert(2, f"Authorization: Bearer {credential}")
    status, _, response_body = _exchange(port, head, body)
    return status, response_body


class _Serving:
    """One started adapter, its port, and the dispatcher behind it."""

    def __init__(self, server: HttpListener, dispatch: CountingDispatch) -> None:
        self.server = server
        self.dispatch = dispatch
        self.port = int(server.url.rsplit(":", 1)[1])


@pytest.fixture
def serving() -> Iterator[_Serving]:
    dispatch = CountingDispatch()
    server = HttpListener(
        router=_router(dispatch), principal=PRINCIPAL, resolver=_resolver
    )
    server.start()
    try:
        yield _Serving(server, dispatch)
    finally:
        server.stop()


# --- the transport refusal corpus ---------------------------------------------


def _corpus() -> list[dict[str, Any]]:
    cases = json.loads(FIXTURES.read_text(encoding="utf-8"))["cases"]
    assert isinstance(cases, list) and cases
    return [dict(case) for case in cases]


def _case_body(case: Mapping[str, Any]) -> bytes:
    body = case["body"]
    if "document" in body:
        return _documents()[body["document"]]
    if "hex" in body:
        return bytes.fromhex(body["hex"])
    text: str = body["text"]
    return text.encode("utf-8")


@pytest.mark.parametrize("case", _corpus(), ids=lambda case: str(case["name"]))
def test_the_transport_refusal_corpus_is_bounded_and_never_dispatches(
    serving: _Serving, case: Mapping[str, Any]
) -> None:
    """Every malformed exchange is answered, and none of them reaches the router.

    The second assertion is the one that cannot be satisfied by a status code alone.
    """
    body = _case_body(case)
    head = [
        line.replace("{length}", str(len(body))).replace(
            "{bearer}", ACCEPTED_CREDENTIAL
        )
        for line in case["head"]
    ]

    status, _, response_body = _exchange(serving.port, head, body)

    assert status == case["expect_status"]
    # Bounded, and carrying nothing: a refusal is its status code and no body at all,
    # which is why no request value can be reflected into one.
    assert response_body == b""
    assert serving.dispatch.calls == []


def test_the_corpus_covers_every_required_transport_failure_class() -> None:
    """The fixture is evidence, so what it must contain is asserted rather than hoped.

    Trimming a case out of the JSON would otherwise make this suite quietly narrower
    while every remaining test still passed.
    """
    statuses = {int(case["expect_status"]) for case in _corpus()}
    assert {405, 501, 404, 415, 411, 400, 413} <= statuses


# --- the two routes, and the status mapping -----------------------------------


def test_an_authenticated_application_request_is_answered_with_an_accepted_envelope(
    serving: _Serving,
) -> None:
    request = _request()

    status, body = _post(
        serving.port,
        APPLICATION_PATH,
        canonical_json_bytes(request.to_wire()),
        credential=ACCEPTED_CREDENTIAL,
    )

    assert status == 200
    document = json.loads(body)
    assert document["metadata"]["request_id"] == "req-1"
    assert document["result"] == {"ok": True}
    assert [seen.operation for seen in serving.dispatch.calls] == [OPERATION]


def test_a_typed_application_error_is_also_http_200(serving: _Serving) -> None:
    """An accepted application error is an accepted answer, not a transport failure.

    Mapping it onto a 4xx or 5xx is precisely the second error taxonomy the freeze
    refuses: the envelope already carries the code, and a status code beside it would
    be a competing one.
    """
    serving.dispatch.fail = True

    status, body = _post(
        serving.port,
        APPLICATION_PATH,
        canonical_json_bytes(_request().to_wire()),
        credential=ACCEPTED_CREDENTIAL,
    )

    assert status == 200
    document = json.loads(body)
    assert document["error"]["code"] == "core.operation_not_implemented"
    assert "result" not in document


def test_a_loopback_probe_is_answered_without_a_credential(serving: _Serving) -> None:
    status, body = _post(
        serving.port, PROBE_PATH, canonical_json_bytes({PROBE_FIELD: PROBE_HEALTH})
    )

    assert status == 200
    document = json.loads(body)
    assert document[PROBE_FIELD] == PROBE_HEALTH
    assert document["api_version"] == API_VERSION
    assert serving.dispatch.calls == []


def test_an_unknown_probe_kind_fails_closed(serving: _Serving) -> None:
    status, body = _post(
        serving.port, PROBE_PATH, canonical_json_bytes({PROBE_FIELD: "service.secrets"})
    )

    assert status == 400
    assert body == b""


def test_an_accepted_response_is_canonical_json_at_the_canonical_media_type(
    serving: _Serving,
) -> None:
    body = canonical_json_bytes({PROBE_FIELD: PROBE_HEALTH})
    head = [
        f"POST {PROBE_PATH} HTTP/1.1",
        "Host: localhost",
        f"Content-Type: {CONTENT_TYPE}",
        f"Content-Length: {len(body)}",
    ]

    status, head_bytes, response_body = _exchange(serving.port, head, body)

    assert status == 200
    assert f"content-type: {CONTENT_TYPE}; charset=utf-8".encode() in head_bytes.lower()
    # Round-tripping through the canonical encoder must be a no-op on what was sent.
    assert canonical_json_bytes(json.loads(response_body)) == response_body


# --- one router, two transports -----------------------------------------------


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="requires a real Unix socket"
)
def test_local_ipc_and_http_answer_the_same_accepted_bytes_identically() -> None:
    """The same document, over both transports, through one router.

    Byte-equivalent in: both transports are handed the identical canonical body.
    Byte-equivalent out: OVC1's frame is a header plus a canonical body, and HTTP's
    body is that same canonical body, so the two responses are compared as bytes
    rather than as "semantically similar" documents.
    """
    dispatch = CountingDispatch()
    router = _router(dispatch)
    document = canonical_json_bytes(_request().to_wire())

    with tempfile.TemporaryDirectory(prefix="ovh-") as directory:
        endpoint = endpoint_for_path(Path(directory) / "s.sock")
        local = LocalSocketServer(router=router, endpoint=endpoint)
        local.start()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(10)
                client.connect(endpoint.address)
                client.sendall(encode_frame(json.loads(document)))
                frame = b""
                while True:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    frame += chunk
        finally:
            local.stop()

        http = HttpListener(
            router=router, principal=PRINCIPAL, resolver=_resolver
        )
        http.start()
        try:
            port = int(http.url.rsplit(":", 1)[1])
            status, body = _post(
                port, APPLICATION_PATH, document, credential=ACCEPTED_CREDENTIAL
            )
        finally:
            http.stop()

    assert status == 200
    assert frame[HEADER_BYTES:] == body
    assert len(dispatch.calls) == 2
    assert dispatch.calls[0] == dispatch.calls[1]


# --- the wait a caller can impose ---------------------------------------------


def test_a_slow_client_is_bounded_by_a_total_deadline_not_a_per_read_one() -> None:
    """A drip client must not be able to extend the request past the budget.

    This is the defect a socket timeout does *not* fix, and the reason it looks fixed:
    `StreamRequestHandler` applies `timeout` per `recv`, so every individual read is
    honoured while the request as a whole runs forever. The listener is serialized, so
    the bound on this request is the bound on every other caller's wait.

    Asserted on the clock and on the connection, not on a status: what has to be true
    is that the server stops listening to this client at a time this test picked, and
    that it is serving again immediately afterwards. The drip runs on its own thread
    so the assertion is a blocking read on the main one -- measuring the moment the
    server let go, rather than the moment the client noticed.
    """
    dispatch = CountingDispatch()
    server = HttpListener(
        router=_router(dispatch),
        principal=PRINCIPAL,
        resolver=_resolver,
        request_deadline=1.0,
    )
    server.start()
    port = int(server.url.rsplit(":", 1)[1])
    stop = threading.Event()
    started = time.monotonic()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=15) as client:
            # A complete request line, then a header line that never ends.
            client.sendall(b"POST /v1/probe HTTP/1.1\r\nX-Drip: ")

            def drip() -> None:
                try:
                    while not stop.is_set():
                        client.sendall(b"x")
                        time.sleep(0.05)
                except OSError:
                    pass

            worker = threading.Thread(target=drip, daemon=True)
            worker.start()
            # Read to EOF. Either answer is acceptable and both are bounded: a
            # refusal then a close, or a close with nothing. What is measured is
            # when the server let go of the connection, whichever it chose.
            client.settimeout(10.0)
            try:
                while client.recv(65536):
                    pass
                released = True
            except (TimeoutError, OSError):
                released = False
            elapsed = time.monotonic() - started

            # The listener has to be free *while the drip is still dripping*, which
            # is the property that actually matters on a serialized server and the
            # one a cooperative client would hide. Closing the drip socket first
            # would release the handler by itself and prove nothing about the
            # deadline -- so the second caller is served with the first still live.
            assert worker.is_alive()
            next_started = time.monotonic()
            status, _ = _post(
                port, PROBE_PATH, canonical_json_bytes({PROBE_FIELD: PROBE_HEALTH})
            )
            served_after = time.monotonic() - next_started
            stop.set()
            worker.join(timeout=2)
    finally:
        stop.set()
        server.stop()

    assert released
    assert elapsed < 5.0, f"the drip held the connection for {elapsed:.1f}s"
    assert status == 200
    assert served_after < 3.0, (
        f"the drip held the serialized listener for a further {served_after:.1f}s"
    )
    assert dispatch.calls == []


def test_the_request_deadline_defaults_to_the_local_transport_s_reviewed_value() -> (
    None
):
    """The number is inherited from the reviewed local transport; the semantics are not."""
    server = HttpListener(
        router=_router(CountingDispatch()), principal=PRINCIPAL, resolver=_resolver
    )

    assert server.request_deadline == DEFAULT_TIMEOUT_SECONDS


# --- the bind policy ----------------------------------------------------------


def test_the_default_bind_is_ipv4_loopback_and_serves() -> None:
    dispatch = CountingDispatch()
    server = HttpListener(
        router=_router(dispatch), principal=PRINCIPAL, resolver=_resolver
    )

    url = server.start()
    try:
        assert url.startswith("http://127.0.0.1:")
        port = int(url.rsplit(":", 1)[1])
        status, _ = _post(
            port, PROBE_PATH, canonical_json_bytes({PROBE_FIELD: PROBE_HEALTH})
        )
    finally:
        server.stop()

    assert status == 200


@pytest.mark.skipif(not socket.has_ipv6, reason="requires IPv6")
def test_ipv6_loopback_is_opt_in_and_binds() -> None:
    dispatch = CountingDispatch()
    server = HttpListener(
        router=_router(dispatch),
        principal=PRINCIPAL,
        resolver=_resolver,
        bind=HttpBind(host="::1"),
    )

    url = server.start()
    try:
        assert url.startswith("http://[::1]:")
    finally:
        server.stop()


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "::",
        "192.0.2.1",
        "2001:db8::1",
        "localhost",
        "example.invalid",
        "",
        "127.0.0.1 ",
    ],
)
def test_a_wildcard_or_non_loopback_bind_refuses(host: str) -> None:
    """Refused at construction, so there is no state in which one is listening.

    A hostname is refused with the routable addresses on purpose: what `localhost`
    resolves to is host configuration, and a bind policy an `/etc/hosts` line can move
    is not one.
    """
    with pytest.raises(HttpTransportError):
        HttpBind(host=host)


@pytest.mark.parametrize("port", [-1, 65536, 999999])
def test_a_port_outside_the_range_refuses(port: int) -> None:
    with pytest.raises(HttpTransportError):
        HttpBind(port=port)


def test_the_adapter_refuses_to_exist_without_a_credential_resolver() -> None:
    """Absent is a configuration error, never a permissive default."""
    with pytest.raises(HttpTransportError, match="credential resolver"):
        HttpListener(router=_router(CountingDispatch()), principal=PRINCIPAL)


def test_starting_twice_is_refused() -> None:
    server = HttpListener(
        router=_router(CountingDispatch()), principal=PRINCIPAL, resolver=_resolver
    )
    server.start()
    try:
        with pytest.raises(HttpTransportError):
            server.start()
    finally:
        server.stop()


# --- the advertised endpoint form ---------------------------------------------


def test_an_accepted_loopback_endpoint_parses() -> None:
    assert parse_http_endpoint("http://127.0.0.1:8080") == HttpBind("127.0.0.1", 8080)
    assert parse_http_endpoint("http://[::1]:8080") == HttpBind("::1", 8080)


#: Stated material for the endpoint rules below. `parse_http_endpoint` reads no file
#: -- validation is at `start`, before a socket exists -- so these paths need not, and
#: deliberately do not, exist: what is under test here is the agreement between a
#: scheme and a configuration, not the bytes behind it.
STATED_TLS = HttpTls(
    certificate_chain=Path("chain.pem-that-is-never-read"),
    private_key=Path("key.pem-that-is-never-read"),
)


def test_an_https_endpoint_parses_once_tls_material_is_stated() -> None:
    """The case `https://127.0.0.1:8080` moved out of the refusal table below.

    It was refused there because this adapter implemented no TLS at all. It does now,
    so the scheme is expressible -- and the guarantee that replaces the old refusal is
    stronger than "https is refused": `https` parses **only** with material behind it,
    a non-loopback `http` endpoint still refuses (the table keeps
    `http://198.51.100.7:8080`), and the two disagreeing directions are refused below.
    """
    assert parse_http_endpoint("https://127.0.0.1:8080", tls=STATED_TLS) == HttpBind(
        "127.0.0.1", 8080, tls=STATED_TLS
    )
    assert parse_http_endpoint("https://[::1]:8080", tls=STATED_TLS) == HttpBind(
        "::1", 8080, tls=STATED_TLS
    )
    # And a routable host, which `http://` cannot reach at any port.
    assert parse_http_endpoint("https://198.51.100.7:8443", tls=STATED_TLS) == HttpBind(
        "198.51.100.7", 8443, tls=STATED_TLS
    )


@pytest.mark.parametrize(
    ("name", "endpoint", "tls"),
    [
        ("https_with_nothing_to_serve_it_with", "https://127.0.0.1:8080", None),
        (
            "https_non_loopback_with_nothing_to_serve_it_with",
            "https://198.51.100.7:1",
            None,
        ),
        (
            "http_carrying_material_it_would_not_use",
            "http://127.0.0.1:8080",
            STATED_TLS,
        ),
        ("http_non_loopback_carrying_material", "http://198.51.100.7:1", STATED_TLS),
    ],
)
def test_an_endpoint_whose_scheme_and_tls_material_disagree_refuses(
    name: str, endpoint: str, tls: HttpTls | None
) -> None:
    """Both directions refuse, and neither has a fallback.

    `https` with nothing behind it must not become a listener, and `http` holding a key
    must not become one either -- guessing which of the two the caller meant is
    guessing whether to downgrade, and there is no answer to that question that is not
    a downgrade half the time.
    """
    del name

    with pytest.raises(HttpTransportError):
        parse_http_endpoint(endpoint, tls=tls)


@pytest.mark.parametrize(
    "endpoint",
    [
        "unix:///tmp/s.sock",
        "http://0.0.0.0:8080",
        "http://[::]:8080",
        "http://localhost:8080",
        "http://198.51.100.7:8080",
        "http://127.0.0.1",
        "http://127.0.0.1:8080/v1/application",
        "http://127.0.0.1:8080?access_token=planted",
        "http://127.0.0.1:8080#planted",
        "http://user:planted@127.0.0.1:8080",
        "http://127.0.0.1:not-a-port",
        "http://127.0.0.1:99999",
        # `urlsplit` validates a bracketed netloc during the split and raises there,
        # which is easy to miss because it looks like a total function.
        "http://[not-an-address]:8080",
        "http://[::1",
        "http://[]:8080",
    ],
)
def test_an_endpoint_this_lane_cannot_serve_refuses(endpoint: str) -> None:
    """Everything with no bind behind it, still refused with no TLS material stated.

    `https://127.0.0.1:8080` used to head this table because the adapter implemented no
    TLS at all. It has moved up to two named tests rather than been deleted: the scheme
    now parses, but only with material, and the guarantee this case carried -- a URL
    can never talk this adapter into serving a routable address in the clear -- is what
    `http://0.0.0.0:8080`, `http://198.51.100.7:8080` and `http://localhost:8080`
    below still hold, unchanged and with no `tls` argument in sight.
    """
    with pytest.raises(HttpTransportError):
        parse_http_endpoint(endpoint)


# --- startup wiring -----------------------------------------------------------


def _service_argv(tmp_path: Path, endpoint: str) -> list[str]:
    return [
        "--workspace",
        str(tmp_path / "workspace"),
        "--installation-state",
        str(tmp_path / "installation"),
        "--endpoint",
        endpoint_for_path(tmp_path / "locks" / "s.sock").url,
        "--http-endpoint",
        endpoint,
    ]


def test_the_service_accepts_an_http_endpoint_under_that_exact_name(
    tmp_path: Path,
) -> None:
    """Parsed, not merely mentioned in the help text.

    A help-text substring passes for `--http-endpoint-disabled` too, which is a
    different flag that serves nothing.
    """
    parsed = build_parser().parse_args(
        [
            "--workspace",
            str(tmp_path),
            "--installation-state",
            str(tmp_path),
            "--http-endpoint",
            "http://127.0.0.1:1",
        ]
    )

    assert parsed.http_endpoint == "http://127.0.0.1:1"


def test_a_wildcard_http_endpoint_refuses_startup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = service_main(
        _service_argv(tmp_path, "http://0.0.0.0:0"), resolve_credential=_resolver
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.err == (
        "refusing to serve: HTTP endpoint is not an accepted loopback endpoint\n"
    )


def test_a_non_loopback_http_endpoint_refuses_startup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = service_main(
        _service_argv(tmp_path, "http://198.51.100.7:8080"),
        resolve_credential=_resolver,
    )

    assert result == 2
    assert "loopback" in capsys.readouterr().err


@pytest.mark.parametrize(
    "endpoint", ["http://[planted-credential-hunter2]:8080", "http://[::1"]
)
def test_a_malformed_bracketed_http_endpoint_refuses_startup_rather_than_crashing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], endpoint: str
) -> None:
    """`urlsplit` raises on a bracketed netloc, and it raises *quoting the brackets*.

    Unguarded, that `ValueError` escaped the adapter as an unhandled traceback
    carrying caller-supplied text -- past `main`'s `HttpTransportError` handler, and
    reached before the resolver refusal, so the shipped entry point crashed here
    instead of refusing. A resolver is passed so the failure cannot be masked by the
    resolver refusal that follows it.
    """
    result = service_main(
        _service_argv(tmp_path, endpoint), resolve_credential=_resolver
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.err == (
        "refusing to serve: HTTP endpoint is not an accepted loopback endpoint\n"
    )
    assert "planted-credential-hunter2" not in captured.out + captured.err


def test_http_without_a_trusted_credential_resolver_refuses_startup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The console-script entry point supplies none, so this is the shipped default."""
    result = service_main(_service_argv(tmp_path, "http://127.0.0.1:0"))

    captured = capsys.readouterr()
    assert result == 2
    assert captured.err == (
        "refusing to serve: HTTP needs a trusted credential resolver\n"
    )


@pytest.mark.parametrize(
    "endpoint",
    ["http://0.0.0.0:8080", "https://127.0.0.1:8080", "nonsense", "http://[::1"],
)
def test_check_only_does_not_parse_the_http_endpoint_it_was_given(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], endpoint: str
) -> None:
    """What `--check-only`'s help text now states, held to the code.

    Each of these exits 2 with a named refusal when the run would actually serve --
    asserted here first, so the pair is a contrast rather than a bare exit code. Add
    `--check-only` and the argument never reaches `_http_bind_to_serve`: nothing
    parses it, so the exit reports the workspace alone. It is 1 here because no
    workspace exists; on a ready workspace these exit 0.

    This mode has always ignored `--endpoint` the same way. The silence was the
    defect -- `--check-only --http-endpoint nonsense` told an operator nothing --
    and the fix was to say so in both help texts, so this pins the behaviour those
    texts now describe.
    """
    argv = _service_argv(tmp_path, endpoint)

    assert service_main(argv) == 2
    assert "loopback" in capsys.readouterr().err

    assert service_main([*argv, "--check-only"]) == 1
    assert capsys.readouterr().err == ""


def test_the_local_transport_still_starts_with_no_http_endpoint_asked_for(
    tmp_path: Path,
) -> None:
    """The wiring is additive: HTTP off is the unchanged local-IPC path."""
    assert (
        build_parser()
        .parse_args(
            [
                "--workspace",
                str(tmp_path),
                "--installation-state",
                str(tmp_path),
            ]
        )
        .http_endpoint
        is None
    )


def _main_function(name: str) -> Any:
    """The AST of one function in `service.main`.

    Read out of the source rather than asserted through a live startup, which would
    need a workspace, a lock and a storage backend to prove a wiring fact.
    """
    import ast
    import inspect

    from omnivia_core_runtime.service import main as module

    tree = ast.parse(inspect.getsource(module))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_the_router_receives_the_dispatcher_s_own_bound_method_unwrapped() -> None:
    """`_router_for` must pass `dispatcher.dispatch`, not something built from it.

    This is what keeps the principal cross-check load-bearing. `_dispatch_principal`
    can only read the grant off a *bound method*; wrapping the dispatch here --
    `dispatch=trace(dispatcher.dispatch)`, a decorator, a partial, a lambda -- makes
    it a plain function, the dispatcher silently becomes one that "cannot say", and
    the check degrades to a no-op with every other test still green.

    So the shape is pinned at the one place production composes it: an attribute
    access, never a call. Nothing today wraps it; this is the guard against the
    refactor that would, and against it being invisible when it happens.
    """
    import ast

    router_for = _main_function("_router_for")
    dispatch_arguments = [
        keyword.value
        for node in ast.walk(router_for)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "DocumentRouter"
        for keyword in node.keywords
        if keyword.arg == "dispatch"
    ]

    assert len(dispatch_arguments) == 1
    passed = dispatch_arguments[0]
    assert isinstance(passed, ast.Attribute), (
        "the router must receive the dispatcher's own bound method; anything "
        "computed here reads as a plain callable and silently disables the "
        "principal cross-check"
    )
    assert passed.attr == "dispatch"
    assert isinstance(passed.value, ast.Name)


def test_the_shared_router_object_is_handed_to_both_transports() -> None:
    """One `_router_for` call in `serve`, and both servers get its result.

    Two calls would be two routers over one dispatcher -- harmless today and exactly
    the drift the packet's "shares the router" requirement exists to stop.
    """
    import ast

    serve = _main_function("serve")
    router_calls = [
        node
        for node in ast.walk(serve)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_router_for"
    ]
    assert len(router_calls) == 1

    def _named_arguments(call: ast.Call) -> dict[str, str]:
        """The keyword arguments of `call` that are passed as a bare name."""
        return {
            keyword.arg: keyword.value.id
            for keyword in call.keywords
            if keyword.arg is not None and isinstance(keyword.value, ast.Name)
        }

    calls = {
        node.func.id: _named_arguments(node)
        for node in ast.walk(serve)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"LocalSocketServer", "HttpListener", "Grant"}
    }
    assert set(calls) == {"LocalSocketServer", "HttpListener", "Grant"}
    assert calls["LocalSocketServer"]["router"] == "router"
    assert calls["HttpListener"]["router"] == "router"

    # And the declared principal is the *same name* the dispatcher's Grant is built
    # from. Passing a literal here, or a different constant, would declare one
    # principal over a dispatcher acting as another -- which admits sessions for the
    # declared one and then runs them as the Grant's. The adapter refuses that
    # combination at construction, so this pins the shipped wiring never to reach it.
    assert calls["HttpListener"]["principal"] == "LOCAL_PRINCIPAL"
    assert calls["Grant"]["principal"] == "LOCAL_PRINCIPAL"
    assert calls["HttpListener"]["principal"] == calls["Grant"]["principal"]


def test_the_operation_field_names_the_application_branch() -> None:
    """Pins the constant the route/document agreement is written against."""
    assert OPERATION_FIELD in _request().to_wire()
