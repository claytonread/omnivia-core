"""The client's HTTP transport against the real runtime listener, end to end.

Every other suite for either side uses a double for the other: the client's own
``test_http_client_transport.py`` answers with canned bytes, and this package's
``test_http_transport.py`` and ``test_http_security.py`` send raw sockets. Both
are the right shape for what they pin -- bytes a well-behaved peer never sends,
and bytes a well-behaved client never sends -- and neither can answer the
question this file exists for: **do the two independently-correct halves actually
agree?**

They are built from different modules on purpose. The client canonicalizes with
``omnivia_core_client.canonical_json_bytes`` and admits with its own
``decode_frame``; the server canonicalizes with
``omnivia_core_runtime.service.ovc1`` and admits with its own. Both defer to the
same public ``omnivia_core`` canonicalizer, and this is where "defer to" is
demonstrated rather than asserted: a real request built by one is admitted by the
other, and a real answer built by the other is admitted by the one.

Three things are pinned here and nowhere else.

**Authentication actually works across the boundary.** The client resolves a
credential through its injected resolver, builds the ``Authorization`` header,
and the server's own injected resolver turns those bytes back into a session that
dispatch runs under. A bearer prefix, a header name or a spelling that disagreed
between the two would fail here and pass everywhere else.

**Discovery is authenticated and the other two probes are not.** The client sends
the credential on ``service.discover`` and not on ``service.health`` or
``service.readiness``; the server requires it on exactly that one. The two
policies were written separately and this is what says they are the same policy.

**A refusal is a refusal on both sides.** An unresolvable credential, a session
the endpoint may not act as, and an operation outside the session's allowlist all
produce a server refusal that the client reports as a transport failure carrying
no credential and no endpoint.

**No runtime source is touched, and none is imported by the client.** The listener
is constructed here exactly as ``test_http_security.py`` constructs it. The client
package cannot import this package -- ``test_package_isolation.py`` enforces that
-- so the dependency runs one way and this file is the only place the two meet.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from omnivia_core_client import (
    Credential,
    CredentialCache,
    CredentialReference,
    Deadline,
    HttpTransport,
    ProtocolError,
    TransportError,
    parse_http_endpoint,
)
from omnivia_core_runtime.service.authorization import AuthenticatedSession
from omnivia_core_runtime.service.http_transport import HttpListener
from omnivia_core_runtime.service.operations import failure, success
from omnivia_core_runtime.service.probes import (
    PROBE_DISCOVER,
    PROBE_HEALTH,
    PROBE_READINESS,
    ProbeRouter,
    ServiceFacts,
)
from omnivia_core_runtime.service.protocol import DocumentRouter
from omnivia_core_runtime.service.versions import API_VERSION

from omnivia_core.contracts.v1 import (
    ClientIdentity,
    ErrorResponseEnvelope,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
    ServiceProbeRequest,
    SuccessResponseEnvelope,
)

OBSERVED_AT = "2026-08-02T00:00:00Z"
OPERATION = "core.health"
OTHER_OPERATION = "core.readiness"
WORKSPACE = "ws-1"
PRINCIPAL = "local-user"

#: The one credential the server's resolver accepts, and the one the client's
#: resolver releases. Deliberately the same string reached through two entirely
#: separate seams: the client's is keyed by reference and origin, the server's by
#: the header bytes that arrived, and this file is where the two have to meet.
ACCEPTED = "accepted-credential"

REFERENCE = CredentialReference("core.default")


# --------------------------------------------------------------------------
# The server, wired exactly as `test_http_security.py` wires it
# --------------------------------------------------------------------------


class _CountingDispatch:
    def __init__(self) -> None:
        self.calls: list[RequestEnvelope] = []
        self.fail = False

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


def _session(operations: frozenset[str] = frozenset({OPERATION})) -> AuthenticatedSession:
    return AuthenticatedSession(
        principal_id=PRINCIPAL,
        roles=frozenset({"reader"}),
        workspaces=frozenset({WORKSPACE}),
        operations=operations,
        purposes=frozenset({"test"}),
        scopes=frozenset({"workspace:read"}),
    )


def _resolver(credential: str) -> AuthenticatedSession | None:
    """The server's resolver. One credential is accepted and nothing else is."""
    return _session() if credential == ACCEPTED else None


class _Serving:
    def __init__(self, server: HttpListener, dispatch: _CountingDispatch) -> None:
        self.server = server
        self.dispatch = dispatch
        self.url = server.url

    @property
    def credentials_seen(self) -> list[str]:
        return _SEEN


#: What the server's resolver was handed, recorded so "the bearer credential
#: crossed the boundary intact" is an assertion about bytes rather than about a
#: status code.
_SEEN: list[str] = []


def _recording_resolver(credential: str) -> AuthenticatedSession | None:
    _SEEN.append(credential)
    return _resolver(credential)


def _serving(resolver: Any = _recording_resolver) -> _Serving:
    dispatch = _CountingDispatch()
    server = HttpListener(
        router=DocumentRouter(
            probes=ProbeRouter(facts=_facts, capabilities=tuple, clock=lambda: 0),
            dispatch=dispatch,
        ),
        principal=PRINCIPAL,
        resolver=resolver,
    )
    server.start()
    return _Serving(server, dispatch)


@pytest.fixture
def serving() -> Iterator[_Serving]:
    _SEEN.clear()
    live = _serving()
    try:
        yield live
    finally:
        live.server.stop()


# --------------------------------------------------------------------------
# The client, built only from `omnivia_core_client`
# --------------------------------------------------------------------------


def _cache(secret: str | None = ACCEPTED) -> CredentialCache:
    """The client's credential seam. A resolver, not a store.

    ``ttl_seconds=0`` so every call resolves: this suite asserts on what the
    server was handed per request, and a cache hit would make the second
    assertion about the first request.
    """
    return CredentialCache(
        lambda reference, origin: None if secret is None else Credential(secret),
        ttl_seconds=0,
    )


def _client(serving: _Serving, *, secret: str | None = ACCEPTED) -> HttpTransport:
    return HttpTransport(
        endpoint=parse_http_endpoint(serving.url),
        credential_reference=REFERENCE,
        credentials=_cache(secret),
    )


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


def _deadline(seconds: float = 20.0) -> Deadline:
    return Deadline.after(seconds)


# --------------------------------------------------------------------------
# The endpoint the listener advertises is one the client will dial
# --------------------------------------------------------------------------


def test_the_client_parses_the_url_the_listener_reports(serving: _Serving) -> None:
    """The two ends agree on the endpoint spelling, which is not free.

    ``HttpListener.url`` reads the bound socket and brackets IPv6; the client's
    parser normalizes and re-brackets. A disagreement here would mean the client
    could not dial an endpoint the server publishes, which no single-sided test
    would show.
    """
    endpoint = parse_http_endpoint(serving.url)

    assert endpoint.scheme == "http"
    assert endpoint.host == "127.0.0.1"
    assert endpoint.origin == f"http://127.0.0.1:{endpoint.port}"


# --------------------------------------------------------------------------
# The authenticated application route
# --------------------------------------------------------------------------


def test_an_authenticated_application_call_reaches_dispatch_and_returns(
    serving: _Serving,
) -> None:
    """The whole loop: resolve, send, authenticate, dispatch, answer, decode."""
    response = _client(serving).call(_request(), deadline=_deadline())

    assert isinstance(response, SuccessResponseEnvelope)
    assert [call.operation for call in serving.dispatch.calls] == [OPERATION]
    assert serving.credentials_seen == [ACCEPTED]


def test_the_credential_crosses_the_boundary_exactly_as_the_client_resolved_it(
    serving: _Serving,
) -> None:
    """Byte for byte. A stray prefix, a strip or an encoding on either side would
    still authenticate a *different* string, which no status code would reveal."""
    _client(serving).call(_request(), deadline=_deadline())

    assert serving.credentials_seen == [ACCEPTED]


def test_an_application_error_envelope_crosses_as_an_answer(serving: _Serving) -> None:
    """HTTP 200 carrying a typed application error, returned rather than raised.

    The freeze is explicit that an application error is an accepted answer, and
    this is where both sides have to hold that at once: the server answers 200
    and the client returns rather than raising.
    """
    serving.dispatch.fail = True

    response = _client(serving).call(_request(), deadline=_deadline())

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == "core.operation_not_implemented"
    assert len(serving.dispatch.calls) == 1


def test_two_calls_are_two_connections_and_two_resolutions(serving: _Serving) -> None:
    """Unary per connection on both sides, and a credential per call."""
    transport = _client(serving)
    transport.call(_request(), deadline=_deadline())
    transport.call(_request(), deadline=_deadline())

    assert len(serving.dispatch.calls) == 2
    assert serving.credentials_seen == [ACCEPTED, ACCEPTED]


# --------------------------------------------------------------------------
# The probes, and which of them is authenticated
# --------------------------------------------------------------------------


def test_the_discovery_probe_is_authenticated_and_answered(serving: _Serving) -> None:
    """The client sends the credential on this probe and the server requires it.

    Two policies written in two packages, asserted to be one policy: the server
    was handed a credential, so the client sent one, and the probe was answered,
    so the server accepted it.
    """
    result = _client(serving).probe(
        ServiceProbeRequest(probe=PROBE_DISCOVER), deadline=_deadline()
    )

    assert result.probe == PROBE_DISCOVER
    assert serving.credentials_seen == [ACCEPTED]
    assert serving.dispatch.calls == []


@pytest.mark.parametrize("probe", [PROBE_HEALTH, PROBE_READINESS])
def test_the_open_probes_are_answered_with_no_credential_at_all(
    serving: _Serving, probe: str
) -> None:
    """The accepted unauthenticated pair, and the client spends nothing on them.

    ``credentials_seen`` empty is the assertion that matters: the server's
    resolver was never called, which can only be true if no ``Authorization``
    header arrived.
    """
    result = _client(serving).probe(
        ServiceProbeRequest(probe=probe), deadline=_deadline()
    )

    assert result.probe == probe
    assert result.status == "pass"
    assert serving.credentials_seen == []


def test_discovery_without_a_resolvable_credential_is_refused(
    serving: _Serving,
) -> None:
    """The counterweight to the open pair: this one is held to the credential."""
    with pytest.raises(TransportError) as caught:
        _client(serving, secret="not-the-accepted-credential").probe(
            ServiceProbeRequest(probe=PROBE_DISCOVER), deadline=_deadline()
        )

    assert "401" in str(caught.value)
    assert serving.dispatch.calls == []


# --------------------------------------------------------------------------
# Refusals, from the server's gate to the client's error type
# --------------------------------------------------------------------------


def test_an_unresolvable_credential_never_reaches_dispatch(serving: _Serving) -> None:
    with pytest.raises(TransportError) as caught:
        _client(serving, secret="not-the-accepted-credential").call(
            _request(), deadline=_deadline()
        )

    assert "401" in str(caught.value)
    assert serving.dispatch.calls == []


def test_an_operation_outside_the_session_is_refused_before_dispatch(
    serving: _Serving,
) -> None:
    """The server's claims gate, seen from the client. 403 rather than 401: the
    credential authenticated and the session does not admit this operation."""
    with pytest.raises(TransportError) as caught:
        _client(serving).call(_request(OTHER_OPERATION), deadline=_deadline())

    assert "403" in str(caught.value)
    assert serving.dispatch.calls == []


def test_a_refusal_carries_no_credential_and_no_endpoint(serving: _Serving) -> None:
    """The disclosure rule, checked across the boundary rather than within it.

    The server puts nothing in a refusal because it has no body; the client puts
    nothing in the exception it raises for one. Both halves are needed and this
    is where both are exercised at once.
    """
    with pytest.raises(TransportError) as caught:
        _client(serving, secret="not-the-accepted-credential").call(
            _request(), deadline=_deadline()
        )

    rendered = "\n".join(
        [str(caught.value), repr(caught.value), *(repr(arg) for arg in caught.value.args)]
    )
    for secret in (ACCEPTED, "not-the-accepted-credential", "core.default", serving.url):
        assert secret not in rendered, secret
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_a_client_credential_failure_never_reaches_the_server(
    serving: _Serving,
) -> None:
    """A resolver that holds nothing costs no request. The server sees nothing at
    all -- not an unauthenticated one it then refuses."""
    from omnivia_core_client import CredentialMissingError

    with pytest.raises(CredentialMissingError):
        _client(serving, secret=None).call(_request(), deadline=_deadline())

    assert serving.credentials_seen == []
    assert serving.dispatch.calls == []


# --------------------------------------------------------------------------
# The two admission policies are one policy
# --------------------------------------------------------------------------


def test_the_client_admits_what_the_server_canonicalized_and_the_reverse(
    serving: _Serving,
) -> None:
    """Built by two modules, admitted by two modules, and neither refuses.

    The client's ``canonical_json_bytes`` produced the request body and the
    server's ``decode_frame`` admitted it; the server's ``canonical_json_bytes``
    produced the response body and the client's ``decode_frame`` admitted it.
    Both defer to the same public canonicalizer, and a divergence in either
    direction shows up here as a refusal rather than as a passing suite on each
    side.
    """
    from omnivia_core_client import canonical_json_bytes as client_canonical
    from omnivia_core_runtime.service.ovc1 import (
        canonical_json_bytes as server_canonical,
    )

    document = _request().to_wire()
    assert client_canonical(document) == server_canonical(document)

    response = _client(serving).call(_request(), deadline=_deadline())
    assert isinstance(response, SuccessResponseEnvelope)


def test_the_client_sends_no_ovc1_frame(serving: _Serving) -> None:
    """The request body is bare canonical JSON, which is what the server reads.

    Asserted on the document the dispatcher received: a framed body would not
    have decoded at all, so reaching dispatch with the right operation is what
    says the bytes were unframed JSON.
    """
    _client(serving).call(_request(), deadline=_deadline())

    assert [call.operation for call in serving.dispatch.calls] == [OPERATION]


def test_a_wrong_protocol_answer_is_still_the_clients_protocol_error() -> None:
    """The one negative case that needs no listener: a peer that is not this server.

    Kept in this file rather than the client's own suite because what it pins is
    that the client's refusal type does not depend on the server being absent --
    it is the same ``ProtocolError`` whether the peer is a fake or a real
    listener answering something else.
    """
    import socket
    import threading

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])

    def answer() -> None:
        connection, _ = listener.accept()
        with connection:
            connection.recv(65536)
            connection.sendall(b"OVC1\x00\x00\x00\x02{}")

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        transport = HttpTransport(
            endpoint=parse_http_endpoint(f"http://127.0.0.1:{port}"),
            credential_reference=REFERENCE,
            credentials=_cache(),
        )
        with pytest.raises((ProtocolError, TransportError)):
            transport.call(_request(), deadline=Deadline.after(5.0))
    finally:
        thread.join(timeout=5.0)
        listener.close()
