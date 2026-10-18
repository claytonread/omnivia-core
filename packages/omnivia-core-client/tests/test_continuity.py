"""Trusted client admission for ``continuity.session.register`` responses.

These tests keep the peer adversarial.  A response is authoritative only after
the shared client has proved its envelope identity, server-issued authority,
binding target, active state, generation, and live lease.  Every other answer
collapses to one payload-free error rather than becoming adapter state.
"""

from __future__ import annotations

import socket
import threading
import traceback
from typing import cast

import omnivia_core_client.continuity as continuity_module
import pytest
from omnivia_core_client import (
    CLIENT_API_VERSION,
    CancellationToken,
    ContinuityRegistrationError,
    ContinuityRegistrationRequest,
    Credential,
    CredentialCache,
    CredentialReference,
    Deadline,
    DeadlineExceededError,
    HttpTransport,
    OperationCancelledError,
    ServiceClient,
    negotiate_endpoint,
    parse_http_endpoint,
    register_continuity_session,
)

from omnivia_core.contracts.v1 import (
    ApiError,
    CapabilityRef,
    CapabilitySet,
    ClientIdentity,
    CompatibilityMetadata,
    ContinuitySessionBinding,
    ContinuitySessionRegisterInput,
    EngineeringSnapshotRef,
    ErrorResponseEnvelope,
    GrantedAuthority,
    RequestEnvelope,
    ResponseEnvelope,
    ResponseMetadata,
    ServiceEndpointDescriptor,
    ServiceProbeRequest,
    ServiceProbeResult,
    SuccessResponseEnvelope,
    UpgradeState,
    VersionCapabilityEnvelope,
    VersionWindow,
    canonical_timestamp_nanoseconds,
    get_operation_metadata,
)

WORKSPACE_ID = "workspace-continuity"
PRINCIPAL_ID = "principal-continuity"
REQUEST_ID = "request-continuity"
CORRELATION_ID = "correlation-continuity"
TRACE_ID = "trace-continuity"
IDEMPOTENCY_KEY = "idempotency-continuity"
SECRET = "peer-secret-marker-must-not-escape"
CREDENTIAL_SECRET = "credential-secret-marker-must-not-escape"
FAILURE_MESSAGE = "the continuity registration did not return a trusted binding"


class RecordingTransport:
    """Return one scripted value while retaining the exact call arguments."""

    def __init__(
        self,
        response: object | None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[
            tuple[RequestEnvelope, Deadline, CancellationToken | None]
        ] = []

    def call(
        self,
        request: RequestEnvelope,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ResponseEnvelope:
        self.calls.append((request, deadline, cancellation))
        if self.error is not None:
            raise self.error
        return cast(ResponseEnvelope, self.response)

    def probe(
        self,
        request: ServiceProbeRequest,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ServiceProbeResult:
        raise AssertionError("registration must not probe or rediscover an endpoint")


def _descriptor(endpoint_uri: str = "https://core.example:8443") -> ServiceEndpointDescriptor:
    return ServiceEndpointDescriptor.from_wire(
        {
            "descriptor_version": CLIENT_API_VERSION,
            "workspace_id": WORKSPACE_ID,
            "service_instance_id": "service-continuity",
            "installation_id": "installation-continuity",
            "endpoint_uri": endpoint_uri,
            "protocol_version": "1.0",
            "server_version": "1.2.5",
            "supported_api_versions": {
                "minimum": f"{CLIENT_API_VERSION.split('.')[0]}.0",
                "maximum": CLIENT_API_VERSION,
            },
            "supported_workspace_versions": {
                "minimum": "1.0",
                "maximum": "1.0",
            },
            "workspace_format_version": "1.0",
            "ready": True,
            "lifecycle_state": "serving",
            "fencing_generation": 7,
            "published_at": "2026-09-28T00:00:00Z",
        }
    )


def _client(
    transport: RecordingTransport | HttpTransport,
    *,
    endpoint_uri: str = "https://core.example:8443",
) -> ServiceClient:
    descriptor = _descriptor(endpoint_uri)
    return ServiceClient(
        transport=transport,
        descriptor=descriptor,
        negotiated=negotiate_endpoint(descriptor),
    )


def _target(snapshot_id: str = "snapshot-continuity") -> EngineeringSnapshotRef:
    return EngineeringSnapshotRef(
        snapshot_id=snapshot_id,
        repository_id="repository-continuity",
        snapshot_kind="git_commit",
        branch_label="main",
    )


def _registration(
    *,
    repository_target: EngineeringSnapshotRef | None = None,
) -> ContinuityRegistrationRequest:
    return ContinuityRegistrationRequest(
        input=ContinuitySessionRegisterInput(
            schema_version="engineering.1",
            repository_target=_target() if repository_target is None else repository_target,
            checkout_hint="checkout-continuity",
            host_session_ref="host-session-continuity",
        ),
        principal_id=PRINCIPAL_ID,
        workspace_id=WORKSPACE_ID,
        request_id=REQUEST_ID,
        correlation_id=CORRELATION_ID,
        trace_id=TRACE_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        client=ClientIdentity(id="continuity-adapter", version="1.0.0"),
    )


def _metadata(
    *,
    request_id: str = REQUEST_ID,
    correlation_id: str = CORRELATION_ID,
    principal_id: str = PRINCIPAL_ID,
    api_version: str = CLIENT_API_VERSION,
) -> ResponseMetadata:
    capabilities: tuple[CapabilityRef, ...] = ()
    return ResponseMetadata(
        request_id=request_id,
        correlation_id=correlation_id,
        version=VersionCapabilityEnvelope(
            api_version=api_version,
            server_version="1.2.5",
            workspace_format_version="1.0",
            compatibility=CompatibilityMetadata(
                selected_api_version=api_version,
                selected_workspace_version="1.0",
                supported_api_versions=VersionWindow(
                    minimum=api_version, maximum=api_version
                ),
                supported_workspace_versions=VersionWindow(
                    minimum="1.0", maximum="1.0"
                ),
                status="compatible",
                upgrade_state=UpgradeState(value="none"),
                deprecations=(),
            ),
            capabilities=CapabilitySet(
                supported=capabilities,
                granted=capabilities,
                effective=capabilities,
            ),
        ),
        authority=GrantedAuthority(
            principal_id=principal_id,
            roles=(),
            capabilities=capabilities,
        ),
    )


def _success(
    *,
    request_id: str = REQUEST_ID,
    correlation_id: str = CORRELATION_ID,
    authority_principal: str = PRINCIPAL_ID,
    api_version: str = CLIENT_API_VERSION,
    session_overrides: dict[str, object] | None = None,
) -> SuccessResponseEnvelope:
    session: dict[str, object] = {
        "session_id": "session-continuity",
        "principal_id": PRINCIPAL_ID,
        "workspace_id": WORKSPACE_ID,
        "binding_generation": 3,
        "lease_expires_at": "2099-01-01T00:00:00Z",
        "state": "active",
        "repository_target": _target().to_wire(),
    }
    if session_overrides is not None:
        session.update(session_overrides)
    return SuccessResponseEnvelope(
        metadata=_metadata(
            request_id=request_id,
            correlation_id=correlation_id,
            principal_id=authority_principal,
            api_version=api_version,
        ),
        # The ignored marker proves that even an untrusted additive field cannot
        # be repeated into the public failure when another check rejects the reply.
        result={"session": session, "untrusted_peer_field": SECRET},
    )


def _unsuccessful() -> ErrorResponseEnvelope:
    return ErrorResponseEnvelope(
        metadata=_metadata(),
        error=ApiError(
            code="authorization_denied",
            message=SECRET,
            retry_class="never",
            details={"peer_diagnostic": SECRET},
        ),
    )


def _assert_payload_free(error: BaseException, *markers: str) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.TracebackException.from_exception(error).format())
    for marker in markers:
        assert marker not in str(error)
        assert marker not in repr(error)
        assert marker not in rendered


def test_registration_builds_the_governed_request_and_returns_the_typed_binding() -> None:
    transport = RecordingTransport(_success())
    client = _client(transport)
    registration = _registration()
    deadline = Deadline.after(10)
    cancellation = CancellationToken()

    binding = register_continuity_session(
        client,
        registration,
        deadline=deadline,
        cancellation=cancellation,
    )

    assert isinstance(binding, ContinuitySessionBinding)
    assert binding.session_id == "session-continuity"
    assert binding.principal_id == PRINCIPAL_ID
    assert binding.workspace_id == WORKSPACE_ID
    assert binding.repository_target == _target()
    assert len(transport.calls) == 1
    request, sent_deadline, sent_cancellation = transport.calls[0]
    entry = get_operation_metadata("continuity.session.register")
    assert request.operation == "continuity.session.register"
    assert request.input == registration.input.to_wire()
    assert request.metadata.request_id == REQUEST_ID
    assert request.metadata.correlation_id == CORRELATION_ID
    assert request.metadata.trace_id == TRACE_ID
    assert request.metadata.workspace_id == WORKSPACE_ID
    assert request.metadata.scopes == tuple(entry.scope.required_scopes)
    assert request.metadata.purpose == "continuity_session"
    assert request.metadata.idempotency_key == IDEMPOTENCY_KEY
    assert request.metadata.required_capabilities == (entry.required_capability,)
    assert request.metadata.principal_claim is not None
    assert request.metadata.principal_claim.claimed_principal_id == PRINCIPAL_ID
    assert sent_deadline is deadline
    assert sent_cancellation is cancellation


@pytest.mark.parametrize(
    ("case", "response"),
    [
        ("absent", None),
        ("malformed_envelope", {"peer_diagnostic": SECRET}),
        (
            "malformed_typed_metadata",
            _success(request_id=cast(str, {"peer_diagnostic": SECRET})),
        ),
        ("unsuccessful", _unsuccessful()),
        ("request_id", _success(request_id="another-request")),
        ("correlation_id", _success(correlation_id="another-correlation")),
        ("api_version", _success(api_version="1.2")),
        ("authority", _success(authority_principal="another-principal")),
        (
            "principal",
            _success(session_overrides={"principal_id": "another-principal"}),
        ),
        (
            "workspace",
            _success(session_overrides={"workspace_id": "another-workspace"}),
        ),
        (
            "repository_target",
            _success(
                session_overrides={
                    "repository_target": _target("another-snapshot").to_wire()
                }
            ),
        ),
        ("state", _success(session_overrides={"state": "closed"})),
        ("zero_generation", _success(session_overrides={"binding_generation": 0})),
        ("boolean_generation", _success(session_overrides={"binding_generation": True})),
        (
            "expired_lease",
            _success(session_overrides={"lease_expires_at": "2020-01-01T00:00:00Z"}),
        ),
        (
            "malformed_lease",
            _success(session_overrides={"lease_expires_at": SECRET}),
        ),
        ("malformed_result", SuccessResponseEnvelope(_metadata(), {"secret": SECRET})),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_untrusted_registration_responses_fail_closed_without_payload(
    case: str, response: object | None
) -> None:
    del case
    client = _client(RecordingTransport(response))

    with pytest.raises(ContinuityRegistrationError) as caught:
        register_continuity_session(
            client,
            _registration(),
            deadline=Deadline.after(10),
        )

    assert str(caught.value) == FAILURE_MESSAGE
    _assert_payload_free(caught.value, SECRET)


def test_a_missing_requested_repository_target_requires_a_missing_returned_target() -> None:
    registration = ContinuityRegistrationRequest(
        input=ContinuitySessionRegisterInput(
            schema_version="engineering.1",
            repository_target=None,
        ),
        principal_id=PRINCIPAL_ID,
        workspace_id=WORKSPACE_ID,
        request_id=REQUEST_ID,
        correlation_id=CORRELATION_ID,
        trace_id=TRACE_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        client=ClientIdentity(id="continuity-adapter", version="1.0.0"),
    )
    response = _success(session_overrides={"repository_target": _target().to_wire()})

    with pytest.raises(ContinuityRegistrationError):
        register_continuity_session(
            _client(RecordingTransport(response)),
            registration,
            deadline=Deadline.after(10),
        )


@pytest.mark.parametrize(
    ("transport_error", "error_type", "message"),
    [
        (
            RuntimeError(SECRET),
            ContinuityRegistrationError,
            FAILURE_MESSAGE,
        ),
        (
            DeadlineExceededError(SECRET),
            DeadlineExceededError,
            "the deadline passed during continuity registration",
        ),
        (
            OperationCancelledError(SECRET),
            OperationCancelledError,
            "the continuity registration was cancelled",
        ),
    ],
)
def test_transport_exceptions_are_replaced_with_fixed_payload_free_failures(
    transport_error: Exception,
    error_type: type[Exception],
    message: str,
) -> None:
    client = _client(RecordingTransport(None, error=transport_error))

    with pytest.raises(error_type) as caught:
        register_continuity_session(
            client,
            _registration(),
            deadline=Deadline.after(10),
        )

    assert str(caught.value) == message
    _assert_payload_free(caught.value, SECRET)


@pytest.mark.parametrize(
    ("lease_expires_at", "trusted"),
    [
        ("2026-09-28T00:00:00Z", False),
        ("2026-09-28T00:00:00.000000001Z", True),
    ],
)
def test_the_lease_must_be_strictly_later_than_the_admission_instant(
    monkeypatch: pytest.MonkeyPatch,
    lease_expires_at: str,
    trusted: bool,
) -> None:
    now = canonical_timestamp_nanoseconds("2026-09-28T00:00:00Z")
    monkeypatch.setattr(continuity_module.time, "time_ns", lambda: now)
    client = _client(
        RecordingTransport(
            _success(session_overrides={"lease_expires_at": lease_expires_at})
        )
    )

    if trusted:
        binding = register_continuity_session(
            client,
            _registration(),
            deadline=Deadline.after(10),
        )
        assert binding.lease_expires_at == lease_expires_at
    else:
        with pytest.raises(ContinuityRegistrationError):
            register_continuity_session(
                client,
                _registration(),
                deadline=Deadline.after(10),
            )


def _read_http_request(connection: socket.socket) -> bytes:
    received = b""
    while b"\r\n\r\n" not in received:
        chunk = connection.recv(65536)
        if not chunk:
            break
        received += chunk
    head, _, body = received.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    while len(body) < length:
        chunk = connection.recv(65536)
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body


class _RedirectTarget:
    """A second loopback listener makes any redirect-following attempt observable."""

    def __init__(self) -> None:
        self.requests: list[bytes] = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._listener.settimeout(1)
        self.port = cast(tuple[str, int], self._listener.getsockname())[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            connection, _ = self._listener.accept()
            with connection:
                self.requests.append(_read_http_request(connection))
                connection.sendall(
                    b"HTTP/1.1 500 Internal Server Error\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
        except OSError:
            pass

    def close(self) -> None:
        self._listener.close()
        self._thread.join(timeout=2)


class _RedirectPeer:
    """One real HTTP peer returning a redirect with attacker-controlled headers."""

    def __init__(self, status: str, location: str) -> None:
        self.requests: list[bytes] = []
        self._status = status
        self._location = location
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(2)
        self._listener.settimeout(5)
        self.port = cast(tuple[str, int], self._listener.getsockname())[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            connection, _ = self._listener.accept()
            with connection:
                self.requests.append(_read_http_request(connection))
                answer = (
                    f"{self._status}\r\n"
                    "Content-Length: 0\r\n"
                    f"Location: {self._location}\r\n"
                    f"X-Peer-Diagnostic: {SECRET}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                connection.sendall(answer)
        except OSError:
            pass

    def close(self) -> None:
        self._listener.close()
        self._thread.join(timeout=5)


@pytest.mark.parametrize(
    "status",
    [
        "HTTP/1.1 301 Moved Permanently",
        "HTTP/1.1 302 Found",
        "HTTP/1.1 307 Temporary Redirect",
        "HTTP/1.1 308 Permanent Redirect",
    ],
)
def test_a_real_http_redirect_is_not_followed_and_its_headers_do_not_escape(
    status: str,
) -> None:
    target = _RedirectTarget()
    location = f"http://127.0.0.1:{target.port}/{SECRET}"
    peer = _RedirectPeer(status, location)
    endpoint_uri = f"http://127.0.0.1:{peer.port}"
    credentials = CredentialCache(
        lambda reference, origin: Credential(CREDENTIAL_SECRET),
        ttl_seconds=0,
    )
    transport = HttpTransport(
        endpoint=parse_http_endpoint(endpoint_uri),
        credential_reference=CredentialReference("continuity.default"),
        credentials=credentials,
    )
    try:
        with pytest.raises(ContinuityRegistrationError) as caught:
            register_continuity_session(
                _client(transport, endpoint_uri=endpoint_uri),
                _registration(),
                deadline=Deadline.after(10),
            )
    finally:
        peer.close()
        target.close()

    assert len(peer.requests) == 1
    assert target.requests == []
    assert f"Authorization: Bearer {CREDENTIAL_SECRET}".encode() in peer.requests[0]
    assert str(caught.value) == FAILURE_MESSAGE
    _assert_payload_free(caught.value, SECRET, CREDENTIAL_SECRET)


def test_the_registration_workflow_is_a_public_client_export() -> None:
    import omnivia_core_client

    assert "ContinuityRegistrationRequest" in omnivia_core_client.__all__
    assert "ContinuityRegistrationError" in omnivia_core_client.__all__
    assert "register_continuity_session" in omnivia_core_client.__all__
