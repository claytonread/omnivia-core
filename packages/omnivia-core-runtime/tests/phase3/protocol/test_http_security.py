"""P2b: the loopback HTTP v1 trust boundary.

Three properties, and each is asserted on the thing that would actually be violated
rather than on the status code that reports it:

* an unverified bearer credential **never reaches dispatch**. The dispatcher is
  instrumented and asserted to be untouched, and so is the router in front of it. A
  test that only reads `401` proves the adapter can produce that number, which is not
  the same claim at all -- an adapter that dispatched and then answered `401` would
  satisfy it.
* a request claim can narrow the resolved session and can never widen it. Every one of
  principal, role, workspace, purpose and scope is tried in both directions.
* a planted credential and a planted path reach no response, no log, no exception
  message, no `args`, no `__cause__`, no `__context__` and no formatted traceback.
  Output is captured at the file-descriptor level because the listener answers on its
  own thread, where `capsys` would not see a thing.
"""

from __future__ import annotations

import socket
import traceback
from collections.abc import Iterator
from typing import Any

import pytest
from omnivia_core_runtime.service.authorization import AuthenticatedSession, Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.http_transport import (
    APPLICATION_PATH,
    CONTENT_TYPE,
    PROBE_PATH,
    HttpBind,
    HttpTransportError,
    LoopbackHttpServer,
    parse_http_endpoint,
)
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS, success
from omnivia_core_runtime.service.ovc1 import canonical_json_bytes
from omnivia_core_runtime.service.probes import PROBE_HEALTH, ProbeRouter, ServiceFacts
from omnivia_core_runtime.service.protocol import PROBE_FIELD, DocumentRouter
from omnivia_core_runtime.service.versions import API_VERSION

from omnivia_core.contracts.v1 import (
    ClientIdentity,
    PrincipalClaim,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
)

OBSERVED_AT = "2026-08-02T00:00:00Z"
OPERATION = "core.health"
WORKSPACE = "ws-1"
PRINCIPAL = "local-user"

#: The one credential this suite's resolver accepts. Everything else -- unknown,
#: expired, wrong-audience, malformed, absent -- resolves to no session, which is the
#: only thing the adapter is ever told.
ACCEPTED = "accepted-credential"
EXPIRED = "expired-credential"
WRONG_AUDIENCE = "wrong-audience-credential"

#: Planted values. Distinctive enough that finding either anywhere is unambiguous.
PLANTED_CREDENTIAL = "planted-credential-hunter2"
PLANTED_PATH = "/Users/alice/private/tokens.json"


class CountingDispatch:
    def __init__(self) -> None:
        self.calls: list[RequestEnvelope] = []
        #: Set to make dispatch raise something the adapter has no case for, which
        #: is the only way to reach the handler's own last-resort containment.
        self.explode = False

    def __call__(self, request: RequestEnvelope) -> ResponseEnvelope:
        self.calls.append(request)
        if self.explode:
            raise ZeroDivisionError(f"handler broke on {PLANTED_PATH}")
        return success(request, {"ok": True}, principal=PRINCIPAL)


class CountingRouter:
    """The shared router, instrumented one layer earlier than the dispatcher.

    The packet asks for the dispatcher to be asserted on. This asserts on the router
    as well, which is strictly stronger: a refusal that never routes cannot have
    dispatched, and it also cannot have reached the *probe* router, which answers
    unauthenticated and is the other thing behind this door.
    """

    def __init__(self, dispatch: CountingDispatch) -> None:
        self.dispatch = dispatch
        self.routed: list[object] = []
        self._router = _router_over(dispatch)

    def route(self, document: object) -> Any:
        self.routed.append(document)
        return self._router.route(document)


def _router_over(dispatch: Any) -> DocumentRouter:
    """One router over this dispatch, composed exactly as `main` composes it."""
    return DocumentRouter(
        probes=ProbeRouter(facts=_facts, capabilities=tuple, clock=lambda: 0),
        dispatch=dispatch,
    )


def _facts() -> ServiceFacts:
    return ServiceFacts(
        observed_at=OBSERVED_AT,
        health_status="pass",
        readiness_status="pass",
        discovery_status="pass",
    )


def _session(
    *,
    principal: str = PRINCIPAL,
    roles: frozenset[str] = frozenset({"reader", "writer"}),
    workspaces: frozenset[str] = frozenset({WORKSPACE}),
    operations: frozenset[str] = frozenset({OPERATION}),
    purposes: frozenset[str] = frozenset({"test"}),
    scopes: frozenset[str] = frozenset({"workspace:read", "workspace:write"}),
) -> AuthenticatedSession:
    return AuthenticatedSession(
        principal_id=principal,
        roles=roles,
        workspaces=workspaces,
        operations=operations,
        purposes=purposes,
        scopes=scopes,
    )


def _resolver(credential: str) -> AuthenticatedSession | None:
    """A resolver, not a store. One credential is accepted and nothing else is.

    `EXPIRED` and `WRONG_AUDIENCE` are separate names for the same answer on purpose:
    the seam reports *whether* a credential authenticated, never why not, so an
    adapter cannot become an oracle for which of the two a caller holds.
    """
    return _session() if credential == ACCEPTED else None


def _request(
    *,
    claimed_principal: str | None = None,
    claimed_roles: tuple[str, ...] | None = None,
    workspace_id: str | None = WORKSPACE,
    purpose: str = "test",
    scopes: tuple[str, ...] = ("workspace:read",),
) -> bytes:
    claim = None
    if claimed_principal is not None or claimed_roles is not None:
        claim = PrincipalClaim(
            claimed_principal_id=claimed_principal, claimed_roles=claimed_roles
        )
    envelope = RequestEnvelope(
        operation=OPERATION,
        metadata=RequestMetadata(
            request_id="req-1",
            correlation_id="corr-1",
            trace_id="trace-1",
            api_version=API_VERSION,
            client=ClientIdentity(id="test-client", version="0.1.0"),
            scopes=scopes,
            purpose=purpose,
            required_capabilities=(),
            workspace_id=workspace_id,
            principal_claim=claim,
        ),
        input={},
    )
    return canonical_json_bytes(envelope.to_wire())


class _Serving:
    def __init__(self, server: LoopbackHttpServer, router: CountingRouter) -> None:
        self.server = server
        self.router = router
        self.dispatch = router.dispatch
        self.port = int(server.url.rsplit(":", 1)[1])


def _serving(resolver: Any = _resolver, *, deadline: float = 10.0) -> _Serving:
    router = CountingRouter(CountingDispatch())
    server = LoopbackHttpServer(
        router=router,  # type: ignore[arg-type]
        principal=PRINCIPAL,
        resolver=resolver,
        request_deadline=deadline,
    )
    server.start()
    return _Serving(server, router)


@pytest.fixture
def serving() -> Iterator[_Serving]:
    live = _serving()
    try:
        yield live
    finally:
        live.server.stop()


def _exchange(port: int, head: list[str], body: bytes) -> tuple[int, bytes]:
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
    _, _, response_body = response.partition(b"\r\n\r\n")
    status = int(response.split(b"\r\n", 1)[0].split(b" ")[1])
    return status, response_body


def _post(
    port: int,
    path: str,
    body: bytes,
    *,
    authorization: list[str] | None = None,
) -> tuple[int, bytes]:
    head = [
        f"POST {path} HTTP/1.1",
        "Host: localhost",
        f"Content-Type: {CONTENT_TYPE}",
        f"Content-Length: {len(body)}",
    ]
    head.extend(f"Authorization: {value}" for value in authorization or [])
    return _exchange(port, head, body)


# --- nothing unauthenticated reaches dispatch ---------------------------------


@pytest.mark.parametrize(
    ("name", "authorization"),
    [
        ("absent", None),
        ("empty_header", [""]),
        ("not_a_bearer_scheme", ["Basic YWxpY2U6c2VjcmV0"]),
        ("bearer_with_no_credential", ["Bearer "]),
        ("bearer_with_a_lowercase_scheme", [f"bearer {ACCEPTED}"]),
        ("unknown_credential", ["Bearer unknown-credential"]),
        ("expired_credential", [f"Bearer {EXPIRED}"]),
        ("wrong_audience_credential", [f"Bearer {WRONG_AUDIENCE}"]),
        ("malformed_credential", ["Bearer \tnot a credential"]),
        ("two_headers_one_of_them_valid", [f"Bearer {ACCEPTED}", "Bearer other"]),
        ("two_valid_headers", [f"Bearer {ACCEPTED}", f"Bearer {ACCEPTED}"]),
    ],
)
def test_an_unverified_bearer_never_reaches_the_router_or_the_dispatcher(
    serving: _Serving, name: str, authorization: list[str] | None
) -> None:
    """Asserted on the dispatcher and the router, not on the number 401.

    The two-header cases are here because taking the first of a repeated
    `Authorization` is what reading a header normally does, and it would let a caller
    present one credential to this server and a different one to anything in front of
    it.
    """
    del name

    status, body = _post(
        serving.port, APPLICATION_PATH, _request(), authorization=authorization
    )

    assert status == 401
    assert body == b""
    assert serving.router.routed == []
    assert serving.dispatch.calls == []


def test_a_resolver_that_raises_authenticates_nothing(serving: _Serving) -> None:
    """A raising resolver is a failed verification, never a passed one."""
    live = _serving(
        resolver=lambda credential: (_ for _ in ()).throw(
            RuntimeError(f"store {PLANTED_PATH} rejected {credential}")
        )
    )
    try:
        status, body = _post(
            live.port,
            APPLICATION_PATH,
            _request(),
            authorization=[f"Bearer {ACCEPTED}"],
        )
    finally:
        live.server.stop()

    assert status == 401
    assert body == b""
    assert live.router.routed == []
    assert live.dispatch.calls == []


def test_a_resolver_returning_something_that_is_not_a_session_authenticates_nothing(
    serving: _Serving,
) -> None:
    """Truthy is not authenticated. The type is checked, not merely the emptiness."""
    del serving
    live = _serving(resolver=lambda credential: {"principal_id": PRINCIPAL})
    try:
        status, _ = _post(
            live.port,
            APPLICATION_PATH,
            _request(),
            authorization=[f"Bearer {ACCEPTED}"],
        )
    finally:
        live.server.stop()

    assert status == 401
    assert live.router.routed == []
    assert live.dispatch.calls == []


def test_a_credential_offered_in_the_url_is_not_a_route_and_never_dispatches(
    serving: _Serving,
) -> None:
    """A token in a URL is refused by the routing rule, never read out of it."""
    body = _request()
    status, response = _exchange(
        serving.port,
        [
            f"POST {APPLICATION_PATH}?access_token={ACCEPTED} HTTP/1.1",
            "Host: localhost",
            f"Content-Type: {CONTENT_TYPE}",
            f"Content-Length: {len(body)}",
        ],
        body,
    )

    assert status == 404
    assert response == b""
    assert serving.router.routed == []
    assert serving.dispatch.calls == []


def test_an_application_document_cannot_be_smuggled_through_the_probe_route(
    serving: _Serving,
) -> None:
    """The unauthenticated route must not become a way into the authenticated one.

    The router would route this document correctly -- it carries `operation`, so it is
    an application request -- which is exactly why the agreement between the URL and
    the document has to be settled before the router ever sees it.
    """
    status, body = _post(serving.port, PROBE_PATH, _request())

    assert status == 400
    assert body == b""
    assert serving.router.routed == []
    assert serving.dispatch.calls == []


def test_a_verified_credential_does_reach_dispatch(serving: _Serving) -> None:
    """The counterweight: the refusals above are refusals, not a broken adapter."""
    status, _ = _post(
        serving.port,
        APPLICATION_PATH,
        _request(),
        authorization=[f"Bearer {ACCEPTED}"],
    )

    assert status == 200
    assert len(serving.dispatch.calls) == 1


# --- claims narrow, and never widen -------------------------------------------


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("a_principal_the_session_did_not_authenticate", {"claimed_principal": "root"}),
        ("a_role_the_session_does_not_hold", {"claimed_roles": ("reader", "admin")}),
        ("only_a_role_the_session_does_not_hold", {"claimed_roles": ("admin",)}),
        ("a_workspace_the_session_is_not_granted", {"workspace_id": "ws-other"}),
        ("a_purpose_the_session_may_not_act_for", {"purpose": "exfiltrate"}),
        ("a_scope_the_session_does_not_grant", {"scopes": ("workspace:admin",)}),
        (
            "a_granted_scope_alongside_one_that_is_not",
            {"scopes": ("workspace:read", "workspace:admin")},
        ),
    ],
)
def test_a_claim_that_widens_the_session_is_refused_before_dispatch(
    serving: _Serving, name: str, kwargs: dict[str, Any]
) -> None:
    del name

    status, body = _post(
        serving.port,
        APPLICATION_PATH,
        _request(**kwargs),
        authorization=[f"Bearer {ACCEPTED}"],
    )

    assert status == 403
    assert body == b""
    assert serving.router.routed == []
    assert serving.dispatch.calls == []


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("claiming_nothing_runs_as_the_session", {}),
        ("claiming_the_authenticated_principal", {"claimed_principal": PRINCIPAL}),
        ("claiming_a_subset_of_the_held_roles", {"claimed_roles": ("reader",)}),
        ("claiming_no_roles_at_all", {"claimed_roles": ()}),
        ("naming_the_granted_workspace", {"workspace_id": WORKSPACE}),
        ("claiming_a_subset_of_the_granted_scopes", {"scopes": ("workspace:read",)}),
        (
            "claiming_every_granted_scope",
            {"scopes": ("workspace:read", "workspace:write")},
        ),
    ],
)
def test_a_claim_inside_the_session_is_accepted(
    serving: _Serving, name: str, kwargs: dict[str, Any]
) -> None:
    del name

    status, _ = _post(
        serving.port,
        APPLICATION_PATH,
        _request(**kwargs),
        authorization=[f"Bearer {ACCEPTED}"],
    )

    assert status == 200
    assert len(serving.dispatch.calls) == 1


# --- the two authority facts that are not claims ------------------------------


@pytest.mark.parametrize(
    ("name", "operations"),
    [
        ("granted_no_operations_at_all", frozenset()),
        ("granted_only_some_other_operation", frozenset({"core.discovery"})),
    ],
)
def test_an_operation_outside_the_session_allowlist_is_refused_before_dispatch(
    name: str, operations: frozenset[str]
) -> None:
    """`operations` is documented as an authority grant, so it has to be one.

    It defaults to empty, which means an under-specified session invoking anything at
    all was a field the type promised and nothing read. Bounded by the dispatcher's
    own Grant, so this was never an escalation -- but a grant nothing enforces is not
    a grant, and the designed seam enforces it at its step 9.
    """
    del name
    live = _serving(resolver=lambda credential: _session(operations=operations))
    try:
        status, body = _post(
            live.port,
            APPLICATION_PATH,
            _request(),
            authorization=[f"Bearer {ACCEPTED}"],
        )
    finally:
        live.server.stop()

    assert status == 403
    assert body == b""
    assert live.router.routed == []
    assert live.dispatch.calls == []


def test_a_session_for_a_principal_this_endpoint_cannot_act_as_is_refused() -> None:
    """Fail closed rather than silently act as somebody else.

    The dispatcher behind this adapter runs everything as one fixed principal. Passing
    a session for `alice` through and letting it execute as `local-user` is the wrong
    answer twice over: it serves a request under an identity nobody authenticated, and
    -- because the dispatcher *does* check a claimed principal against its own Grant --
    it made an honest request naming `alice` fail while a silent one succeeded.
    Penalising honesty and serving silence is backwards, so both are refused here.
    """
    live = _serving(resolver=lambda credential: _session(principal="alice"))
    try:
        silent, _ = _post(
            live.port,
            APPLICATION_PATH,
            _request(),
            authorization=[f"Bearer {ACCEPTED}"],
        )
        honest, _ = _post(
            live.port,
            APPLICATION_PATH,
            _request(claimed_principal="alice"),
            authorization=[f"Bearer {ACCEPTED}"],
        )
    finally:
        live.server.stop()

    assert (silent, honest) == (403, 403)
    assert live.router.routed == []
    assert live.dispatch.calls == []


def test_a_session_for_the_principal_this_endpoint_acts_as_is_served(
    serving: _Serving,
) -> None:
    """The other direction, including the honest claim that used to be punished."""
    silent, _ = _post(
        serving.port,
        APPLICATION_PATH,
        _request(),
        authorization=[f"Bearer {ACCEPTED}"],
    )
    honest, _ = _post(
        serving.port,
        APPLICATION_PATH,
        _request(claimed_principal=PRINCIPAL),
        authorization=[f"Bearer {ACCEPTED}"],
    )

    assert (silent, honest) == (200, 200)
    assert len(serving.dispatch.calls) == 2


def test_the_declared_principal_must_agree_with_the_dispatcher_behind_the_router() -> (
    None
):
    """Two arguments that must agree, so the disagreement is refused at construction.

    Declaring `alice` over a dispatcher that acts as `local-user` silently reopens
    exactly the hole the gate closes: sessions for `alice` would be admitted and then
    executed as `local-user`. Nothing about passing the principal and the router
    separately prevents that, so where the dispatcher can state who it acts as -- a
    bound `Dispatcher.dispatch`, which is what production wires -- it is checked.
    """
    dispatcher = Dispatcher.for_service_operations(
        Grant(
            principal=PRINCIPAL,
            workspaces=frozenset({WORKSPACE}),
            operations=frozenset(SERVICE_OPERATIONS),
        )
    )
    router = DocumentRouter(
        probes=ProbeRouter(facts=_facts, capabilities=tuple, clock=lambda: 0),
        dispatch=dispatcher.dispatch,
    )

    with pytest.raises(HttpTransportError, match="does not act as"):
        LoopbackHttpServer(router=router, principal="alice", resolver=_resolver)

    # The agreeing wiring builds, which is what makes the refusal above a refusal
    # rather than this check being broken in the useful direction.
    agreeing = LoopbackHttpServer(
        router=router, principal=PRINCIPAL, resolver=_resolver
    )
    assert agreeing.principal == PRINCIPAL


def test_a_dispatcher_that_cannot_state_a_principal_is_not_itself_a_refusal() -> None:
    """`ApplicationDispatch` is a plain callable, and most of them say nothing.

    An unreadable dispatcher is not evidence of disagreement, so it is not refused --
    what is refused is a dispatcher that states a principal and an adapter that
    declares a different one.
    """
    live = _serving()
    live.server.stop()

    assert live.server.principal == PRINCIPAL


def test_a_grant_that_raises_on_access_cannot_say_rather_than_escaping() -> None:
    """Reading an object of unknown shape includes shapes that raise.

    `getattr`'s default covers `AttributeError` and nothing else, so a `grant` whose
    `principal` is a property raising anything else would leave construction as a
    bare exception from a path that promises `HttpTransportError`. The honest answer
    for a shape that cannot be read is the same as for a plain function: it cannot
    say. Unreachable from any wiring in this repo -- `Grant.principal` is a plain
    `str` on a frozen dataclass -- and pinned so the function keeps matching its own
    contract for every shape rather than for the ones that happen to exist.
    """

    class HostileGrant:
        @property
        def principal(self) -> str:
            raise RuntimeError(f"grant exploded reading {PLANTED_PATH}")

    class HostileDispatcher:
        grant = HostileGrant()

        def dispatch(self, request: RequestEnvelope) -> ResponseEnvelope:
            return success(request, {"ok": True}, principal=PRINCIPAL)

    server = LoopbackHttpServer(
        router=_router_over(HostileDispatcher().dispatch),
        principal=PRINCIPAL,
        resolver=_resolver,
    )

    assert server.principal == PRINCIPAL


def test_the_principal_has_no_default_and_must_be_stated() -> None:
    """A default is a guess about somebody else's `Grant`.

    Pinned because the rationale was previously only written down: adding
    `principal: str = "local-user"` left every other test in this suite green, which
    is how an unenforced rationale rots into a silent wrong default.
    """
    router = _router_over(CountingDispatch())

    with pytest.raises(TypeError, match="principal"):
        LoopbackHttpServer(router=router, resolver=_resolver)  # type: ignore[call-arg]


def test_a_session_granted_nothing_can_do_nothing() -> None:
    """Every grant defaults to empty, so an under-specified session refuses."""
    live = _serving(resolver=lambda credential: AuthenticatedSession(PRINCIPAL))
    try:
        status, _ = _post(
            live.port,
            APPLICATION_PATH,
            _request(),
            authorization=[f"Bearer {ACCEPTED}"],
        )
    finally:
        live.server.stop()

    assert status == 403
    assert live.dispatch.calls == []


# --- nothing planted survives into any diagnostic surface ---------------------


def _assert_hides(error: Exception, *secrets: str) -> None:
    rendered = "".join(
        (
            str(error),
            repr(error.args),
            repr(error.__cause__),
            repr(error.__context__),
            "".join(traceback.format_exception(error)),
        )
    )
    for secret in secrets:
        assert secret not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


def test_a_planted_credential_and_path_reach_no_response_and_no_output(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The whole refusal surface at once, with both plants in play.

    Captured at the file-descriptor level: the listener answers on its own thread, and
    `capsys` replaces `sys.stdout`/`sys.stderr` on the thread that installed it, so a
    handler writing to the real stderr would go straight past it. `socketserver`'s
    default `handle_error` and `BaseHTTPRequestHandler`'s default logging both write
    there, which is exactly what this has to see.
    """
    live = _serving(
        resolver=lambda credential: (_ for _ in ()).throw(
            RuntimeError(f"resolver read {PLANTED_PATH} for {credential}")
        )
    )
    responses: list[bytes] = []
    try:
        for path in (
            APPLICATION_PATH,
            f"/v1/{PLANTED_CREDENTIAL}",
            f"{PROBE_PATH}?token={PLANTED_CREDENTIAL}",
        ):
            _, body = _post(
                live.port,
                path,
                _request(),
                authorization=[f"Bearer {PLANTED_CREDENTIAL}"],
            )
            responses.append(body)
        # An invented verb reaches the base class's own error path, which by default
        # renders the method into an HTML body.
        _, body = _exchange(
            live.port,
            [
                f"{PLANTED_CREDENTIAL.upper().replace('-', '')} {PROBE_PATH} HTTP/1.1",
                "Host: localhost",
            ],
            b"",
        )
        responses.append(body)
    finally:
        live.server.stop()

    captured = capfd.readouterr()
    for secret in (PLANTED_CREDENTIAL, PLANTED_PATH, PLANTED_CREDENTIAL.upper()):
        assert all(secret.encode() not in body for body in responses)
        assert secret not in captured.out
        assert secret not in captured.err
    assert responses == [b"", b"", b"", b""]
    assert live.router.routed == []
    assert live.dispatch.calls == []


def test_a_successful_request_publishes_no_credential(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The accepted path too: a verified credential is still a credential.

    `AuthenticatedSession` carries no field for one, which is what makes this hold all
    the way through dispatch rather than only up to the adapter.
    """
    live = _serving(
        resolver=lambda credential: (
            _session() if credential == PLANTED_CREDENTIAL else None
        )
    )
    try:
        status, body = _post(
            live.port,
            APPLICATION_PATH,
            _request(),
            authorization=[f"Bearer {PLANTED_CREDENTIAL}"],
        )
    finally:
        live.server.stop()

    captured = capfd.readouterr()
    assert status == 200
    assert PLANTED_CREDENTIAL.encode() not in body
    assert PLANTED_CREDENTIAL not in captured.out + captured.err
    # Nothing carried it into the request the dispatcher was handed either.
    assert PLANTED_CREDENTIAL not in repr(live.dispatch.calls[0])


def test_an_endpoint_refusal_hides_a_credential_carried_in_the_url() -> None:
    """A URL with a place to put a credential is refused rather than parsed.

    The refusal itself must then say nothing about what was in it -- including through
    `__cause__` and `__context__`, which a `raise ... from None` alone does not clear.
    """
    with pytest.raises(HttpTransportError) as caught:
        parse_http_endpoint(f"http://alice:{PLANTED_CREDENTIAL}@127.0.0.1:8080")

    _assert_hides(caught.value, PLANTED_CREDENTIAL)


def test_a_bind_refusal_hides_the_host_it_was_handed() -> None:
    with pytest.raises(HttpTransportError) as caught:
        HttpBind(host=f"{PLANTED_PATH}#{PLANTED_CREDENTIAL}")

    _assert_hides(caught.value, PLANTED_CREDENTIAL, PLANTED_PATH)


def test_a_start_refusal_hides_the_address_it_could_not_bind() -> None:
    """A bind failure's OS message names the address and errno; neither is repeated."""
    taken = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    router = CountingRouter(CountingDispatch())
    server = LoopbackHttpServer(
        router=router,  # type: ignore[arg-type]
        principal=PRINCIPAL,
        resolver=_resolver,
        bind=HttpBind(port=port),
    )
    try:
        with pytest.raises(HttpTransportError) as caught:
            server.start()
    finally:
        taken.close()

    _assert_hides(caught.value, str(port), "127.0.0.1")


def test_an_endpoint_refusal_hides_an_unusable_port() -> None:
    """`urlsplit().port` quotes the text it could not cast, so it is dropped too.

    The same raise-outside-the-handler discipline as the bind and start refusals, and
    unpinned until now: nothing failed if the `from None` crept back in here.
    """
    with pytest.raises(HttpTransportError) as caught:
        parse_http_endpoint(f"http://127.0.0.1:{PLANTED_CREDENTIAL}")

    _assert_hides(caught.value, PLANTED_CREDENTIAL)


def test_an_endpoint_refusal_hides_a_malformed_bracketed_host() -> None:
    """`urlsplit` validates a bracketed netloc *during the split* and quotes it.

    It looks like a total function and is not, which is how this escaped the adapter
    as an unhandled `ValueError` carrying caller-supplied text.
    """
    with pytest.raises(HttpTransportError) as caught:
        parse_http_endpoint(f"http://[{PLANTED_CREDENTIAL}]:8080")

    _assert_hides(caught.value, PLANTED_CREDENTIAL)


def test_a_handler_failure_is_contained_and_prints_nothing(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """`socketserver` prints a traceback here by default, rendering the request.

    Reached when a write fails against a client that has gone away, which is the one
    path where a credential-bearing request could reach an output stream without any
    of this module's own code running. Invoked directly because provoking a broken
    pipe is a race, and what is being pinned is that the override is silent -- the
    base class it replaces prints the whole exception.
    """
    live = _serving()
    service = live.server._service
    assert service is not None
    try:
        try:
            raise RuntimeError(f"{PLANTED_CREDENTIAL} at {PLANTED_PATH}")
        except RuntimeError:
            service.handle_error(object(), ("127.0.0.1", 1))
    finally:
        live.server.stop()

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_a_dispatch_that_raises_is_contained_without_disclosing_anything(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The handler's last-resort containment, which nothing reached before.

    A handler raising something the adapter has no case for must not take the
    serialized listener down, must not answer with anything it knows, and must not
    print. `500` because no accepted application envelope can be produced -- the
    router never returned one -- which is the freeze's own rule for non-2xx.
    """
    live = _serving()
    live.dispatch.explode = True
    try:
        status, body = _post(
            live.port,
            APPLICATION_PATH,
            _request(),
            authorization=[f"Bearer {ACCEPTED}"],
        )
        # Contained: the listener is still serving the next caller.
        live.dispatch.explode = False
        after, _ = _post(
            live.port,
            APPLICATION_PATH,
            _request(),
            authorization=[f"Bearer {ACCEPTED}"],
        )
    finally:
        live.server.stop()

    captured = capfd.readouterr()
    assert status == 500
    assert body == b""
    assert after == 200
    assert PLANTED_PATH not in captured.out + captured.err


def test_the_probe_route_answers_before_authentication_exists(
    serving: _Serving,
) -> None:
    """Loopback probes are unauthenticated, per the freeze, and stay off the app path."""
    status, body = _post(
        serving.port, PROBE_PATH, canonical_json_bytes({PROBE_FIELD: PROBE_HEALTH})
    )

    assert status == 200
    assert serving.dispatch.calls == []
    assert PLANTED_CREDENTIAL.encode() not in body


def test_the_session_seam_carries_no_credential_field() -> None:
    """The reason none of the above can regress by accident.

    Authentication is transport-owned and what survives it is the *result*. A field
    added here would put the secret into every context, log line and refusal the
    authorization seam produces, so its absence is pinned rather than assumed.
    """
    import dataclasses

    fields = {field.name for field in dataclasses.fields(AuthenticatedSession)}
    assert fields == {
        "principal_id",
        "roles",
        "installations",
        "workspaces",
        "operations",
        "scopes",
        "purposes",
        "capabilities",
    }
