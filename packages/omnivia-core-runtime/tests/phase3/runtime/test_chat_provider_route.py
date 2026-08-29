"""Core's default-off local Chat provider route.

The bridge itself is Platform-owned.  These tests prove only Core's side of the
boundary: explicit local configuration can turn a governed F2a request into one
bounded NDJSON POST, and every unsafe or absent route stays fail-closed before a
provider is treated as reached.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import omnivia_core_runtime.service.chat_provider_route as route
import pytest
from omnivia_core_runtime.service.chat_generation_executor import (
    GenerationExecutorError,
    ProviderRouteUnavailable,
)

from omnivia_core.chat_contract.v1 import ProviderInvocationRequest


def _request() -> ProviderInvocationRequest:
    return ProviderInvocationRequest(
        invocation_id="inv-route-1",
        workspace_id="workspace-route-1",
        conversation_id="conv-route-1",
        job_id="job-route-1",
        attempt_id="attempt-route-1",
        connection_id="provider-connection-1",
        model_id="provider-model-1",
        operation="language.stream",
        messages=({"role": "user", "parts": [{"kind": "text", "text": "hello"}]},),
        response_format={"kind": "text"},
        policy_ref="policy-1",
        classification_ref="classification-1",
        residency_ref="residency-1",
        idempotency_key="idem-route-1",
        correlation_id="corr-route-1",
        deadline_at="2052-05-23T00:00:30Z",
        requested_at="2052-05-23T00:00:00Z",
    )


def _event(request: ProviderInvocationRequest, ordinal: int, event_type: str, **extra: Any) -> bytes:
    payload = {
        "invocationId": request.invocation_id,
        "attemptId": request.attempt_id,
        "ordinal": ordinal,
        "schemaVersion": 1,
        "occurredAt": "2052-05-23T00:00:01Z",
        "receivedAt": "2052-05-23T00:00:01Z",
        "eventType": event_type,
        **extra,
    }
    return json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n"


class _FakeResponse:
    def __init__(self, chunks: list[bytes], *, status: int = 200) -> None:
        self._chunks = chunks
        self.status = status

    def read(self, _size: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _RecordingConnection:
    instances: ClassVar[list[_RecordingConnection]] = []
    next_response: ClassVar[_FakeResponse] = _FakeResponse([])
    request_exception: ClassVar[BaseException | None] = None

    def __init__(self, host: str, port: int, *, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.method: str | None = None
        self.path: str | None = None
        self.body: bytes | None = None
        self.headers: dict[str, str] | None = None
        self.closed = False
        self.__class__.instances.append(self)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        if self.__class__.request_exception is not None:
            raise self.__class__.request_exception
        self.method = method
        self.path = path
        self.body = body
        self.headers = headers

    def getresponse(self) -> _FakeResponse:
        return self.__class__.next_response

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def reset_connection() -> None:
    _RecordingConnection.instances = []
    _RecordingConnection.next_response = _FakeResponse([])
    _RecordingConnection.request_exception = None


def _configured_env(**overrides: str) -> dict[str, str]:
    return {
        route.ENDPOINT_ENV: "http://127.0.0.1:49152/f2a/provider-stream",
        route.TOKEN_ENV: "route-token-secret",
        route.CONNECTION_ID_ENV: "provider-connection-1",
        route.MODEL_ID_ENV: "provider-model-1",
        route.POLICY_REF_ENV: "policy-1",
        route.CLASSIFICATION_REF_ENV: "classification-1",
        route.RESIDENCY_REF_ENV: "residency-1",
        **overrides,
    }


def test_provider_route_from_env_is_absent_by_default() -> None:
    assert route.provider_route_from_env({}) is None


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://127.0.0.1:49152/f2a/provider-stream",
        "http://localhost:49152/f2a/provider-stream",
        "http://192.0.2.10:49152/f2a/provider-stream",
        "http://127.0.0.1/f2a/provider-stream",
        "http://user@127.0.0.1:49152/f2a/provider-stream",
        "http://127.0.0.1:49152/f2a/provider-stream?token=nope",
    ),
)
def test_provider_route_from_env_refuses_non_local_or_ambiguous_endpoints(
    endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(route, "HTTPConnection", _RecordingConnection)

    assert route.provider_route_from_env(_configured_env(**{route.ENDPOINT_ENV: endpoint})) is None
    assert _RecordingConnection.instances == []


def test_provider_route_from_env_refuses_values_that_would_not_build_f2a_request() -> None:
    assert route.provider_route_from_env(
        _configured_env(**{route.CONNECTION_ID_ENV: "not a workspace id"})
    ) is None
    assert route.provider_route_from_env(
        _configured_env(**{route.MODEL_ID_ENV: "https://not-a-model"})
    ) is None
    assert route.provider_route_from_env(
        _configured_env(**{route.POLICY_REF_ENV: "p" * 129})
    ) is None


def test_local_provider_route_client_posts_canonical_request_and_reads_ndjson(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    _RecordingConnection.next_response = _FakeResponse(
        [
            _event(request, 0, "stream-start"),
            _event(
                request,
                1,
                "text-delta",
                partId="part-route-1",
                stepId="step-route-1",
                delta="hello from bridge",
            ),
            _event(request, 2, "finish", finishReason="stop"),
        ]
    )
    monkeypatch.setattr(route, "HTTPConnection", _RecordingConnection)

    resolved = route.provider_route_from_env(_configured_env())
    assert resolved is not None
    invoke, config = resolved
    events = list(invoke(request))

    assert config.connection_id == "provider-connection-1"
    assert [event["eventType"] for event in events] == [
        "stream-start",
        "text-delta",
        "finish",
    ]
    connection = _RecordingConnection.instances[0]
    assert (connection.host, connection.port, connection.path) == (
        "127.0.0.1",
        49152,
        "/f2a/provider-stream",
    )
    assert connection.timeout == 120.0
    assert connection.closed is True
    assert connection.method == "POST"
    assert connection.headers == {
        "Content-Type": "application/json",
        "Accept": "application/x-ndjson",
        "Authorization": "Bearer route-token-secret",
    }
    assert connection.body is not None
    assert json.loads(connection.body.decode("utf-8")) == request.to_wire()
    assert "route-token-secret" not in connection.body.decode("utf-8")


def test_open_failure_is_provider_route_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingConnection.request_exception = OSError("raw socket path must not escape")
    monkeypatch.setattr(route, "HTTPConnection", _RecordingConnection)
    resolved = route.provider_route_from_env(_configured_env())
    assert resolved is not None
    invoke, _config = resolved

    with pytest.raises(ProviderRouteUnavailable) as raised:
        list(invoke(_request()))
    assert "raw socket path" not in str(raised.value)


def test_non_success_status_is_provider_route_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingConnection.next_response = _FakeResponse(
        [b"raw provider or bridge detail must not be parsed"],
        status=503,
    )
    monkeypatch.setattr(route, "HTTPConnection", _RecordingConnection)
    resolved = route.provider_route_from_env(_configured_env())
    assert resolved is not None
    invoke, _config = resolved

    with pytest.raises(ProviderRouteUnavailable):
        list(invoke(_request()))
    assert _RecordingConnection.next_response._chunks == [
        b"raw provider or bridge detail must not be parsed"
    ]


def test_malformed_ndjson_is_a_response_error_after_route_is_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingConnection.next_response = _FakeResponse([b"{not-json}\n"])
    monkeypatch.setattr(route, "HTTPConnection", _RecordingConnection)
    resolved = route.provider_route_from_env(_configured_env())
    assert resolved is not None
    invoke, _config = resolved

    with pytest.raises(GenerationExecutorError) as raised:
        list(invoke(_request()))
    assert "not-json" not in str(raised.value)
