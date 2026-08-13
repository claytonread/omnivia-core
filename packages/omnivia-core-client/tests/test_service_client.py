"""The high-level client: configure a service, connect to it, call it.

Three kinds of assertion, and they are deliberately not the same kind:

- **Composition.** What `connect` builds, from what, in which order -- asserted
  against a recording transport injected in place of the real one, because the
  questions here are "was the transport built from the endpoint the descriptor
  published" and "was *this* deadline object handed on", and neither is visible
  from the far side of a socket.
- **End to end.** The same paths over a real listening socket and a real
  loopback HTTP peer, so the composition is shown to produce a client that
  actually exchanges bytes rather than one that only satisfies a double.
- **Refusal.** Every configuration and connect failure, including what the
  diagnostic must not contain: no endpoint, no path, no reference, no secret, and
  neither `__cause__` nor `__context__`.

The peers answer from the accepted OVC1 vector manifest where an application
envelope is needed, for the same reason `test_http_client_transport.py` does:
those are the documents the frozen format pins.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import tempfile
import threading
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_client import (
    CLIENT_API_VERSION,
    CancellationToken,
    CompatibilityError,
    Credential,
    CredentialCache,
    CredentialDeniedError,
    CredentialInvalidError,
    CredentialMissingError,
    CredentialReference,
    CredentialUnavailableError,
    Deadline,
    DeadlineExceededError,
    HttpServiceConfig,
    HttpTransport,
    InstallationServiceConfig,
    LocalIpcTransport,
    OperationCancelledError,
    ProtocolError,
    ServiceClient,
    TransportError,
    canonical_json_bytes,
    descriptor_path,
    encode_frame,
    service_client,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    RequestEnvelope,
    ResponseEnvelope,
    ServiceEndpointDescriptor,
    ServiceProbeRequest,
    ServiceProbeResult,
    SuccessResponseEnvelope,
    codec,
)

WORKSPACE_ID = "workspace-alpha"
SERVICE_INSTANCE_ID = "service-instance-01"
SECRET = "s3cret-material-nobody-should-see"
REFERENCE = CredentialReference("core.default")
HTTP_ENDPOINT_URI = "https://core.example:8443"

LOCAL_IPC_URI = (
    "unix:///var/run/omnivia/core.sock"
    if os.name == "posix"
    else "pipe://omnivia-core-abc"
)

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


# --------------------------------------------------------------------------
# The documents
# --------------------------------------------------------------------------


def descriptor_wire(**overrides: object) -> dict[str, Any]:
    document: dict[str, Any] = {
        "descriptor_version": CONTRACT_VERSION,
        "workspace_id": WORKSPACE_ID,
        "service_instance_id": SERVICE_INSTANCE_ID,
        "installation_id": "installation-alpha",
        "endpoint_uri": LOCAL_IPC_URI,
        "protocol_version": "1.0",
        "server_version": "1.2.5",
        "supported_api_versions": {
            "minimum": f"{CONTRACT_VERSION.split('.')[0]}.0",
            "maximum": CONTRACT_VERSION,
        },
        "supported_workspace_versions": {"minimum": "1.0", "maximum": "1.0"},
        "workspace_format_version": "1.0",
        "ready": True,
        "lifecycle_state": "serving",
        "fencing_generation": 7,
        "published_at": "2026-07-30T11:59:58Z",
    }
    document.update(overrides)
    return document


def descriptor(**overrides: object) -> ServiceEndpointDescriptor:
    return ServiceEndpointDescriptor.from_wire(descriptor_wire(**overrides))


def probe_result_wire(**overrides: object) -> dict[str, Any]:
    document: dict[str, Any] = {
        "probe": "service.discover",
        "status": "pass",
        "server_version": "1.2.5",
        "api_version": CONTRACT_VERSION,
        "observed_at": "2026-07-30T12:00:00Z",
        "descriptor": descriptor_wire(),
    }
    document.update(overrides)
    return document


def probe_result(
    published: ServiceEndpointDescriptor | None = None, **overrides: Any
) -> ServiceProbeResult:
    fields: dict[str, Any] = {
        "probe": "service.discover",
        "status": "pass",
        "server_version": "1.2.5",
        "api_version": CONTRACT_VERSION,
        "observed_at": "2026-07-30T12:00:00Z",
        "descriptor": descriptor() if published is None else published,
    }
    fields.update(overrides)
    return ServiceProbeResult(**fields)


def publish(root: Path, document: object | None = None) -> Path:
    """Write the descriptor where the installation publishes it, as it publishes it."""
    path = descriptor_path(root, WORKSPACE_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        (root / "runtime").chmod(0o700)
        path.parent.chmod(0o700)
    path.write_text(
        json.dumps(descriptor_wire() if document is None else document),
        encoding="utf-8",
    )
    if os.name == "posix":
        path.chmod(0o600)
    return path


def deadline(seconds: float = 30.0) -> Deadline:
    return Deadline.after(seconds)


def assert_payload_free(error: BaseException, *planted: str) -> None:
    """No chain, and none of these strings anywhere the failure can be rendered."""
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.TracebackException.from_exception(error).format())
    for secret in planted:
        for exposed in (str(error), repr(error.args), rendered):
            assert secret not in exposed, secret


# --------------------------------------------------------------------------
# The transport the composition tests inject
# --------------------------------------------------------------------------


class RecordingTransport:
    """A `ClientTransport` that records the exact objects it was handed.

    The deadline and the token are kept by identity rather than by value,
    because "the same whole-call deadline was passed unchanged" is a statement
    about the object and a re-derived equal one would satisfy a value check.
    """

    def __init__(
        self,
        *,
        result: ServiceProbeResult | None = None,
        response: ResponseEnvelope | None = None,
        probe_error: Exception | None = None,
    ) -> None:
        self.result = probe_result() if result is None else result
        self.response = response
        self.probe_error = probe_error
        self.probes: list[
            tuple[ServiceProbeRequest, Deadline, CancellationToken | None]
        ] = []
        self.calls: list[
            tuple[RequestEnvelope, Deadline, CancellationToken | None]
        ] = []

    def probe(
        self,
        request: ServiceProbeRequest,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ServiceProbeResult:
        self.probes.append((request, deadline, cancellation))
        if self.probe_error is not None:
            raise self.probe_error
        return self.result

    def call(
        self,
        request: RequestEnvelope,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ResponseEnvelope:
        self.calls.append((request, deadline, cancellation))
        assert self.response is not None
        return self.response


class TransportFactory:
    """Stands in for a transport class, recording how it was constructed."""

    def __init__(self, transport: RecordingTransport) -> None:
        self.transport = transport
        self.built: list[dict[str, Any]] = []
        self.on_build: list[Any] = []

    def __call__(self, **fields: Any) -> RecordingTransport:
        self.built.append(fields)
        for hook in self.on_build:
            hook()
        return self.transport


@pytest.fixture
def local_factory(monkeypatch: pytest.MonkeyPatch) -> TransportFactory:
    factory = TransportFactory(RecordingTransport())
    monkeypatch.setattr(service_client, "LocalIpcTransport", factory)
    return factory


@pytest.fixture
def http_factory(monkeypatch: pytest.MonkeyPatch) -> TransportFactory:
    factory = TransportFactory(
        RecordingTransport(
            result=probe_result(descriptor(endpoint_uri="https://core.example:8443"))
        )
    )
    monkeypatch.setattr(service_client, "HttpTransport", factory)
    return factory


def cache(secret: str | None = SECRET) -> CredentialCache:
    return CredentialCache(
        lambda reference, origin: None if secret is None else Credential(secret),
        ttl_seconds=0,
    )


# --------------------------------------------------------------------------
# Configuring an installation
# --------------------------------------------------------------------------


def test_an_installation_configuration_states_a_root_and_a_workspace(
    tmp_path: Path,
) -> None:
    config = InstallationServiceConfig(
        installation_state=tmp_path, workspace_id=WORKSPACE_ID
    )
    assert config.installation_state == tmp_path
    assert config.workspace_id == WORKSPACE_ID


def test_an_installation_root_that_is_not_a_path_is_refused_where_it_is_written() -> (
    None
):
    with pytest.raises(TypeError):
        InstallationServiceConfig(
            installation_state="/var/lib/omnivia",  # type: ignore[arg-type]
            workspace_id=WORKSPACE_ID,
        )


@pytest.mark.parametrize(
    "workspace_id",
    ["", "../escape", "has space", "x" * 129, "tab\tid"],
)
def test_an_inadmissible_workspace_identifier_is_refused(
    tmp_path: Path, workspace_id: str
) -> None:
    """The rule is `descriptor_path`'s, so it cannot drift from discovery's."""
    with pytest.raises(ValueError):
        InstallationServiceConfig(
            installation_state=tmp_path, workspace_id=workspace_id
        )


def test_a_workspace_identifier_that_is_not_a_string_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        InstallationServiceConfig(
            installation_state=tmp_path,
            workspace_id=None,  # type: ignore[arg-type]
        )


def test_an_installation_configuration_is_frozen_and_revalidates_on_replace(
    tmp_path: Path,
) -> None:
    config = InstallationServiceConfig(
        installation_state=tmp_path, workspace_id=WORKSPACE_ID
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.workspace_id = "other"  # type: ignore[misc]
    with pytest.raises(ValueError):
        dataclasses.replace(config, workspace_id="../escape")


# --------------------------------------------------------------------------
# Configuring an explicit HTTP service
# --------------------------------------------------------------------------


def test_an_http_configuration_normalizes_the_endpoint_it_names() -> None:
    config = HttpServiceConfig(
        endpoint_uri="https://Core.Example/",
        credential_reference=REFERENCE,
        credentials=cache(),
    )
    assert config.endpoint.scheme == "https"
    assert config.endpoint.host == "core.example"
    assert config.endpoint.port == 443
    assert config.endpoint.origin == "https://core.example:443"


def test_an_http_configuration_will_not_hold_a_secret() -> None:
    """The field is a *name*. A `Credential` in it is a refusal, not a shortcut."""
    with pytest.raises(TypeError) as raised:
        HttpServiceConfig(
            endpoint_uri=HTTP_ENDPOINT_URI,
            credential_reference=Credential(SECRET),  # type: ignore[arg-type]
            credentials=cache(),
        )
    assert_payload_free(raised.value, SECRET)


def test_a_bare_string_is_not_a_credential_reference() -> None:
    with pytest.raises(TypeError):
        HttpServiceConfig(
            endpoint_uri=HTTP_ENDPOINT_URI,
            credential_reference="core.default",  # type: ignore[arg-type]
            credentials=cache(),
        )


def test_the_credential_seam_must_be_the_packages_own_cache() -> None:
    with pytest.raises(TypeError):
        HttpServiceConfig(
            endpoint_uri=HTTP_ENDPOINT_URI,
            credential_reference=REFERENCE,
            credentials=(lambda reference, origin: Credential(SECRET)),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "endpoint_uri",
    [
        "http://core.example:8080",
        "http://localhost:8080",
        "https://core.example:8443/v1",
        "https://user:pass@core.example:8443",
        "https://core.example:8443?token=abc",
        "https://core.example:8443#fragment",
        "ftp://core.example",
        "not a url at all",
        "https://core.example:0",
    ],
)
def test_an_endpoint_this_client_may_not_dial_is_refused(endpoint_uri: str) -> None:
    """Cleartext off loopback, a resolvable name for loopback, a place to put a
    credential, and anything that is not an authority: all refused where the
    configuration is written, all as the transport's own `TransportError`."""
    with pytest.raises(TransportError) as raised:
        HttpServiceConfig(
            endpoint_uri=endpoint_uri,
            credential_reference=REFERENCE,
            credentials=cache(),
        )
    assert_payload_free(raised.value, endpoint_uri)


def test_cleartext_is_admitted_for_a_loopback_ip_literal_only() -> None:
    config = HttpServiceConfig(
        endpoint_uri="http://127.0.0.1:9999",
        credential_reference=REFERENCE,
        credentials=cache(),
    )
    assert config.endpoint.origin == "http://127.0.0.1:9999"


def test_an_http_configuration_is_frozen_and_revalidates_on_replace() -> None:
    config = HttpServiceConfig(
        endpoint_uri=HTTP_ENDPOINT_URI,
        credential_reference=REFERENCE,
        credentials=cache(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.endpoint_uri = "http://core.example:80"  # type: ignore[misc]
    with pytest.raises(TransportError):
        dataclasses.replace(config, endpoint_uri="http://core.example:80")


def test_an_http_configuration_renders_no_secret() -> None:
    config = HttpServiceConfig(
        endpoint_uri=HTTP_ENDPOINT_URI,
        credential_reference=REFERENCE,
        credentials=cache(),
    )
    assert SECRET not in repr(config)


# --------------------------------------------------------------------------
# Connecting to an installation: composition
# --------------------------------------------------------------------------


def connect_local(
    root: Path,
    *,
    call_deadline: Deadline | None = None,
    cancellation: CancellationToken | None = None,
) -> ServiceClient | None:
    return ServiceClient.connect(
        InstallationServiceConfig(installation_state=root, workspace_id=WORKSPACE_ID),
        deadline=deadline() if call_deadline is None else call_deadline,
        cancellation=cancellation,
    )


def test_connecting_locally_builds_the_transport_from_the_published_endpoint(
    tmp_path: Path, local_factory: TransportFactory
) -> None:
    """The caller named a root and a workspace, and nothing else.

    It did not read `service.json`, did not see the endpoint URI, and did not
    choose between a socket and a pipe -- which is the whole point of the seam.
    """
    publish(tmp_path)
    client = connect_local(tmp_path)

    assert client is not None
    assert local_factory.built == [{"endpoint_uri": LOCAL_IPC_URI}]
    assert client.transport is local_factory.transport
    assert client.descriptor == descriptor()
    assert client.negotiated.api_version == CLIENT_API_VERSION
    assert client.negotiated.protocol_version == "1.0"
    assert client.negotiated.descriptor_version == CONTRACT_VERSION


def test_connecting_locally_verifies_the_endpoint_is_live_before_returning(
    tmp_path: Path, local_factory: TransportFactory
) -> None:
    publish(tmp_path)
    call_deadline = deadline()
    token = CancellationToken()

    assert connect_local(tmp_path, call_deadline=call_deadline, cancellation=token)

    ((request, passed_deadline, passed_token),) = local_factory.transport.probes
    assert request.probe == "service.discover"
    assert passed_deadline is call_deadline
    assert passed_token is token


def test_an_installation_that_has_published_nothing_is_absent_not_broken(
    tmp_path: Path, local_factory: TransportFactory
) -> None:
    assert connect_local(tmp_path) is None
    assert local_factory.built == []
    assert local_factory.transport.probes == []


def test_an_installation_state_root_that_does_not_exist_is_absent_not_broken(
    tmp_path: Path, local_factory: TransportFactory
) -> None:
    missing = tmp_path / "installation-state"

    assert connect_local(missing) is None
    assert local_factory.built == []
    assert local_factory.transport.probes == []


def test_a_descriptor_replaced_between_the_two_reads_is_refused(
    tmp_path: Path, local_factory: TransportFactory
) -> None:
    """Publication is atomic, so the two reads can see different files.

    The transport is built from the first and discovery vouches for the second.
    Republishing exactly there -- in the moment between them -- is the race, and
    it is reproduced rather than simulated: the factory rewrites the descriptor.
    """
    publish(tmp_path)
    successor = descriptor(service_instance_id="service-instance-02")
    local_factory.transport.result = probe_result(successor)
    local_factory.on_build.append(
        lambda: publish(
            tmp_path, descriptor_wire(service_instance_id="service-instance-02")
        )
    )

    with pytest.raises(TransportError) as raised:
        connect_local(tmp_path)
    assert_payload_free(raised.value, str(tmp_path), LOCAL_IPC_URI, WORKSPACE_ID)


def test_a_live_identity_that_disagrees_with_the_file_is_refused(
    tmp_path: Path, local_factory: TransportFactory
) -> None:
    publish(tmp_path)
    local_factory.transport.result = probe_result(
        descriptor(service_instance_id="service-instance-99")
    )
    with pytest.raises(TransportError) as raised:
        connect_local(tmp_path)
    assert_payload_free(raised.value, str(tmp_path), WORKSPACE_ID)


def test_an_endpoint_this_build_cannot_speak_to_is_refused(
    tmp_path: Path, local_factory: TransportFactory
) -> None:
    publish(tmp_path, descriptor_wire(protocol_version="2.0"))
    with pytest.raises(CompatibilityError):
        connect_local(tmp_path)
    assert local_factory.transport.probes == []


def test_a_published_endpoint_that_is_not_local_is_refused(
    tmp_path: Path, local_factory: TransportFactory
) -> None:
    """The locality rule is discovery's and it still runs: a remote URI reached
    through a local coordination file is the descriptor being used as authority."""
    publish(tmp_path, descriptor_wire(endpoint_uri="https://core.example:8443"))
    with pytest.raises(TransportError) as raised:
        connect_local(tmp_path)
    assert local_factory.built == []
    assert_payload_free(raised.value, "core.example")


def test_a_descriptor_that_is_not_a_descriptor_is_refused(
    tmp_path: Path, local_factory: TransportFactory
) -> None:
    publish(tmp_path, {"descriptor_version": CONTRACT_VERSION, "workspace_id": 7})
    with pytest.raises(ProtocolError) as raised:
        connect_local(tmp_path)
    assert_payload_free(raised.value, str(tmp_path))


def test_connecting_locally_refuses_an_already_cancelled_call(
    tmp_path: Path, local_factory: TransportFactory
) -> None:
    publish(tmp_path)
    token = CancellationToken()
    token.cancel()
    with pytest.raises(OperationCancelledError):
        connect_local(tmp_path, cancellation=token)
    assert local_factory.transport.probes == []


def test_connecting_locally_refuses_a_call_that_is_already_out_of_time(
    tmp_path: Path, local_factory: TransportFactory
) -> None:
    publish(tmp_path)
    with pytest.raises(DeadlineExceededError):
        connect_local(tmp_path, call_deadline=Deadline.after(0.0))
    assert local_factory.transport.probes == []


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (DeadlineExceededError("peer said so"), DeadlineExceededError),
        (OperationCancelledError("peer said so"), OperationCancelledError),
        (TransportError("the socket at /tmp/x.sock died"), TransportError),
        (RuntimeError("token abc123 rejected"), TransportError),
    ],
)
def test_a_transport_failure_keeps_its_kind_and_loses_its_words(
    tmp_path: Path,
    local_factory: TransportFactory,
    raised: Exception,
    expected: type[Exception],
) -> None:
    publish(tmp_path)
    local_factory.transport.probe_error = raised
    with pytest.raises(expected) as caught:
        connect_local(tmp_path)
    assert_payload_free(caught.value, "peer said so", "/tmp/x.sock", "abc123")


# --------------------------------------------------------------------------
# Connecting to an explicit HTTP service: composition
# --------------------------------------------------------------------------


def http_config(endpoint_uri: str = HTTP_ENDPOINT_URI) -> HttpServiceConfig:
    return HttpServiceConfig(
        endpoint_uri=endpoint_uri,
        credential_reference=REFERENCE,
        credentials=cache(),
    )


def test_connecting_over_http_builds_the_transport_from_the_configuration(
    http_factory: TransportFactory,
) -> None:
    config = http_config()
    client = ServiceClient.connect(config, deadline=deadline())

    assert client is not None
    assert http_factory.built == [
        {
            "endpoint": config.endpoint,
            "credential_reference": REFERENCE,
            "credentials": config.credentials,
        }
    ]
    assert client.transport is http_factory.transport


def test_connecting_over_http_negotiates_what_the_service_answers_with(
    http_factory: TransportFactory,
) -> None:
    """There is no file here, so the live descriptor is the only descriptor."""
    call_deadline = deadline()
    token = CancellationToken()
    client = ServiceClient.connect(
        http_config(), deadline=call_deadline, cancellation=token
    )

    assert client is not None
    assert client.descriptor.endpoint_uri == "https://core.example:8443"
    assert client.negotiated.api_version == CLIENT_API_VERSION
    ((request, passed_deadline, passed_token),) = http_factory.transport.probes
    assert request.probe == "service.discover"
    assert passed_deadline is call_deadline
    assert passed_token is token


def test_an_http_service_this_build_cannot_speak_to_is_refused(
    http_factory: TransportFactory,
) -> None:
    http_factory.transport.result = probe_result(
        descriptor(endpoint_uri="https://core.example:8443", protocol_version="2.0")
    )
    with pytest.raises(CompatibilityError) as raised:
        ServiceClient.connect(http_config(), deadline=deadline())
    assert_payload_free(raised.value, "core.example", "2.0")


def test_an_http_service_that_does_not_answer_the_probe_is_refused(
    http_factory: TransportFactory,
) -> None:
    http_factory.transport.probe_error = TransportError("https://core.example refused")
    with pytest.raises(TransportError) as raised:
        ServiceClient.connect(http_config(), deadline=deadline())
    assert_payload_free(raised.value, "core.example")


def test_an_http_service_that_answers_without_a_descriptor_is_refused(
    http_factory: TransportFactory,
) -> None:
    http_factory.transport.result = ServiceProbeResult(
        probe="service.discover",
        status="pass",
        server_version="1.2.5",
        api_version=CONTRACT_VERSION,
        observed_at="2026-07-30T12:00:00Z",
    )
    with pytest.raises(TransportError):
        ServiceClient.connect(http_config(), deadline=deadline())


def test_an_http_service_that_answers_a_failing_probe_is_refused(
    http_factory: TransportFactory,
) -> None:
    http_factory.transport.result = probe_result(
        descriptor(endpoint_uri="https://core.example:8443"), status="fail"
    )
    with pytest.raises(TransportError):
        ServiceClient.connect(http_config(), deadline=deadline())


def test_connecting_over_http_refuses_an_already_cancelled_call(
    http_factory: TransportFactory,
) -> None:
    token = CancellationToken()
    token.cancel()
    with pytest.raises(OperationCancelledError):
        ServiceClient.connect(http_config(), deadline=deadline(), cancellation=token)
    assert http_factory.transport.probes == []


def test_connecting_over_http_refuses_a_call_that_is_already_out_of_time(
    http_factory: TransportFactory,
) -> None:
    with pytest.raises(DeadlineExceededError):
        ServiceClient.connect(http_config(), deadline=Deadline.after(0.0))
    assert http_factory.transport.probes == []


# --------------------------------------------------------------------------
# What `connect` refuses outright
# --------------------------------------------------------------------------


@pytest.mark.parametrize("config", [None, "installation", 7, object()])
def test_connect_takes_one_of_the_two_configurations_and_nothing_else(
    config: object,
) -> None:
    with pytest.raises(TypeError):
        ServiceClient.connect(config, deadline=deadline())  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Issuing calls
# --------------------------------------------------------------------------


def test_a_call_is_forwarded_to_the_transport_unchanged(
    tmp_path: Path, local_factory: TransportFactory
) -> None:
    """Same request, same deadline object, same token object, same answer.

    Identity rather than equality: a re-derived deadline of the same length is a
    fresh budget, which is exactly what the whole-call deadline must never be.
    """
    publish(tmp_path)
    answer = codec.decode_response(_vector("application.success"))
    local_factory.transport.response = answer
    client = connect_local(tmp_path)
    assert client is not None

    request = _request()
    call_deadline = deadline()
    token = CancellationToken()
    returned = client.call(request, deadline=call_deadline, cancellation=token)

    assert returned is answer
    ((sent, passed_deadline, passed_token),) = local_factory.transport.calls
    assert sent is request
    assert passed_deadline is call_deadline
    assert passed_token is token


def test_a_call_without_a_token_passes_none_through(
    tmp_path: Path, local_factory: TransportFactory
) -> None:
    publish(tmp_path)
    local_factory.transport.response = codec.decode_response(
        _vector("application.success")
    )
    client = connect_local(tmp_path)
    assert client is not None
    client.call(_request(), deadline=deadline())
    assert local_factory.transport.calls[0][2] is None


def test_a_client_is_frozen(tmp_path: Path, local_factory: TransportFactory) -> None:
    publish(tmp_path)
    client = connect_local(tmp_path)
    assert client is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        client.transport = RecordingTransport()  # type: ignore[misc]


# --------------------------------------------------------------------------
# End to end: a real local endpoint
# --------------------------------------------------------------------------


def _recv_exact(connection: socket.socket, count: int) -> bytes | None:
    received = b""
    while len(received) < count:
        chunk = connection.recv(count - len(received))
        if not chunk:
            return None
        received += chunk
    return received


class FramePeer:
    """A real listener that answers one prepared frame per connection.

    Unary like the server it stands for: one frame in, one frame out, connection
    closed. `requests` is what actually crossed the socket, so "the discovery
    probe went out before the application call" is an assertion about bytes.
    """

    def __init__(self, family: int, address: Any, answers: list[bytes]) -> None:
        self.answers = list(answers)
        self.requests: list[bytes] = []
        self._listener = socket.socket(family, socket.SOCK_STREAM)
        self._listener.bind(address)
        self._listener.listen(4)
        self.address = self._listener.getsockname()
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
                try:
                    header = _recv_exact(connection, 8)
                    if header is None:
                        continue
                    body = _recv_exact(connection, int.from_bytes(header[4:], "big"))
                    self.requests.append(header + (body or b""))
                    if self.answers:
                        connection.sendall(self.answers.pop(0))
                except OSError:  # pragma: no cover - a test that closed early
                    pass

    def close(self) -> None:
        self._listener.close()


@pytest.fixture
def socket_directory() -> Iterator[Path]:
    """A socket directory outside `tmp_path`, which nests too deep for `sun_path`."""
    directory = Path(tempfile.mkdtemp(prefix="ovc-", dir=tempfile.gettempdir()))
    try:
        yield directory
    finally:
        for entry in directory.iterdir():
            entry.unlink(missing_ok=True)
        directory.rmdir()


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="the end-to-end local case needs AF_UNIX; the pipe case has its own suite",
)
def test_a_local_client_connects_and_calls_over_a_real_endpoint(
    tmp_path: Path, socket_directory: Path
) -> None:
    """The composition, end to end, with nothing injected.

    The descriptor on disk is the only thing that says where the socket is, the
    transport is built from it by `connect`, and the two exchanges that follow
    are real frames on a real file descriptor.
    """
    endpoint = socket_directory / "core.sock"
    endpoint_uri = f"unix://{endpoint}"
    published = descriptor_wire(endpoint_uri=endpoint_uri)
    publish(tmp_path, published)
    peer = FramePeer(
        socket.AF_UNIX,
        str(endpoint),
        [
            encode_frame(probe_result_wire(descriptor=published)),
            encode_frame(_vector("application.success")),
        ],
    )
    try:
        client = ServiceClient.connect(
            InstallationServiceConfig(
                installation_state=tmp_path, workspace_id=WORKSPACE_ID
            ),
            deadline=deadline(),
        )
        assert client is not None
        assert isinstance(client.transport, LocalIpcTransport)
        assert client.transport.endpoint_uri == endpoint_uri
        assert client.descriptor.endpoint_uri == endpoint_uri
        assert client.negotiated.api_version == CLIENT_API_VERSION

        response = client.call(_request(), deadline=deadline())
        assert isinstance(response, SuccessResponseEnvelope)
    finally:
        peer.close()

    assert len(peer.requests) == 2
    assert b"service.discover" in peer.requests[0]


# --------------------------------------------------------------------------
# End to end: a real loopback HTTP service
# --------------------------------------------------------------------------


class HttpPeer:
    """A loopback listener answering prepared HTTP responses, one per connection."""

    def __init__(self, answers: list[bytes]) -> None:
        self.answers = list(answers)
        self.requests: list[bytes] = []
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
                    if self.answers:
                        connection.sendall(self.answers.pop(0))
                except OSError:  # pragma: no cover - a test that closed early
                    pass

    def head_of(self, index: int = 0) -> str:
        return self.requests[index].split(b"\r\n\r\n", 1)[0].decode("latin-1")

    def close(self) -> None:
        self._listener.close()


def _declared_length(head: bytes) -> int:
    for line in head.split(b"\r\n"):
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            return int(value.strip())
    return 0


def _http_answer(body: bytes, *, status: str = "HTTP/1.1 200 OK") -> bytes:
    head = "\r\n".join(
        [
            status,
            "Content-Type: application/json; charset=utf-8",
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
    )
    return (head + "\r\n\r\n").encode("latin-1") + body


def test_an_http_client_connects_and_calls_over_a_real_loopback_service() -> None:
    remote = descriptor_wire(endpoint_uri="https://core.example:8443")
    peer = HttpPeer(
        [
            _http_answer(canonical_json_bytes(probe_result_wire(descriptor=remote))),
            _http_answer(canonical_json_bytes(_vector("application.success"))),
        ]
    )
    try:
        config = HttpServiceConfig(
            endpoint_uri=f"http://127.0.0.1:{peer.port}",
            credential_reference=REFERENCE,
            credentials=cache(),
        )
        client = ServiceClient.connect(config, deadline=deadline())
        assert client is not None
        assert isinstance(client.transport, HttpTransport)
        assert client.descriptor.endpoint_uri == "https://core.example:8443"
        assert client.negotiated.api_version == CLIENT_API_VERSION

        response = client.call(_request(), deadline=deadline())
        assert isinstance(response, SuccessResponseEnvelope)
    finally:
        peer.close()

    assert "POST /v1/probe" in peer.head_of(0)
    assert f"Authorization: Bearer {SECRET}" in peer.head_of(0)
    assert "POST /v1/application" in peer.head_of(1)
    assert f"Authorization: Bearer {SECRET}" in peer.head_of(1)


def test_an_http_client_presents_the_credential_bound_to_the_endpoints_origin() -> None:
    """The origin the resolver is asked about is this endpoint's, normalized."""
    asked: list[tuple[CredentialReference, str]] = []

    def resolver(reference: CredentialReference, origin: str) -> Credential:
        asked.append((reference, origin))
        return Credential(SECRET)

    remote = descriptor_wire(endpoint_uri="https://core.example:8443")
    peer = HttpPeer(
        [_http_answer(canonical_json_bytes(probe_result_wire(descriptor=remote)))]
    )
    try:
        config = HttpServiceConfig(
            endpoint_uri=f"http://127.0.0.1:{peer.port}",
            credential_reference=REFERENCE,
            credentials=CredentialCache(resolver, ttl_seconds=0),
        )
        assert ServiceClient.connect(config, deadline=deadline()) is not None
    finally:
        peer.close()

    assert asked == [(REFERENCE, f"http://127.0.0.1:{peer.port}")]


@pytest.mark.parametrize(
    ("resolver", "expected"),
    [
        pytest.param(
            lambda reference, origin: (_ for _ in ()).throw(
                RuntimeError(f"the store at /secrets/{SECRET}.txt is unreachable")
            ),
            CredentialUnavailableError,
            id="unreachable-store",
        ),
        pytest.param(
            lambda reference, origin: SECRET,
            CredentialInvalidError,
            id="not-a-credential",
        ),
        pytest.param(
            lambda reference, origin: None,
            CredentialMissingError,
            id="nothing-held",
        ),
        pytest.param(
            lambda reference, origin: (_ for _ in ()).throw(
                CredentialDeniedError(f"the store denied {SECRET}")
            ),
            CredentialDeniedError,
            id="denied",
        ),
    ],
)
def test_a_credential_that_does_not_resolve_stops_the_connect_before_the_socket(
    resolver: Any,
    expected: type[Exception],
) -> None:
    """Nothing is dialled, and nothing about the store, the answer or the secret
    survives.

    The credential kind survives the discovery probe so the first-party clients
    can distinguish a missing, denied, unavailable or invalid credential from a
    network failure. The injected resolver's own object and words do not survive.
    """
    peer = HttpPeer([])
    try:
        config = HttpServiceConfig(
            endpoint_uri=f"http://127.0.0.1:{peer.port}",
            credential_reference=REFERENCE,
            credentials=CredentialCache(resolver, ttl_seconds=0),
        )
        with pytest.raises(expected) as raised:
            ServiceClient.connect(config, deadline=deadline())
    finally:
        peer.close()
    assert_payload_free(raised.value, SECRET, "/secrets", "unreachable")
    assert peer.requests == []


def test_a_credential_failure_on_a_call_keeps_its_kind() -> None:
    """Connect translates; a call does not. `client.call` is a forward, so what
    the credential seam raised is what a caller catches."""
    released = [True]

    def resolver(reference: CredentialReference, origin: str) -> Credential:
        if not released[0]:
            raise RuntimeError(f"the store holding {SECRET} went away")
        return Credential(SECRET)

    remote = descriptor_wire(endpoint_uri="https://core.example:8443")
    peer = HttpPeer(
        [_http_answer(canonical_json_bytes(probe_result_wire(descriptor=remote)))]
    )
    try:
        config = HttpServiceConfig(
            endpoint_uri=f"http://127.0.0.1:{peer.port}",
            credential_reference=REFERENCE,
            credentials=CredentialCache(resolver, ttl_seconds=0),
        )
        client = ServiceClient.connect(config, deadline=deadline())
        assert client is not None
        released[0] = False
        with pytest.raises(CredentialUnavailableError) as raised:
            client.call(_request(), deadline=deadline())
    finally:
        peer.close()
    assert_payload_free(raised.value, SECRET)
    assert len(peer.requests) == 1


def test_an_http_service_that_refuses_the_probe_is_reported_as_a_transport_failure() -> (
    None
):
    peer = HttpPeer([_http_answer(b"{}", status="HTTP/1.1 401 Unauthorized")])
    try:
        config = HttpServiceConfig(
            endpoint_uri=f"http://127.0.0.1:{peer.port}",
            credential_reference=REFERENCE,
            credentials=cache(),
        )
        with pytest.raises(TransportError) as raised:
            ServiceClient.connect(config, deadline=deadline())
    finally:
        peer.close()
    assert_payload_free(raised.value, SECRET, "127.0.0.1")


def test_a_connected_client_renders_no_secret() -> None:
    remote = descriptor_wire(endpoint_uri="https://core.example:8443")
    peer = HttpPeer(
        [_http_answer(canonical_json_bytes(probe_result_wire(descriptor=remote)))]
    )
    try:
        config = HttpServiceConfig(
            endpoint_uri=f"http://127.0.0.1:{peer.port}",
            credential_reference=REFERENCE,
            credentials=cache(),
        )
        client = ServiceClient.connect(config, deadline=deadline())
    finally:
        peer.close()
    assert client is not None
    assert SECRET not in repr(client)
