"""Default-off local provider-route client for the F2a Chat generation executor.

Core owns no provider credentials, adapter registry, provider account ids, connection
ids or auth paths. What it may do, only when explicitly configured, is POST the
governed F2a :class:`~omnivia_core.chat_contract.v1.ProviderInvocationRequest` to a
Platform-owned loopback bridge and read back a bounded UTF-8 newline-delimited JSON
event stream -- exactly the shape :class:`ChatGenerationExecutor` already consumes from
any ``invoke`` boundary.

**Absent by default, and fails closed.** :func:`provider_route_from_env` returns
``None`` for every reason the route cannot be used: any required variable missing or
empty, or an endpoint that fails the loopback-only rule in :func:`_local_endpoint`. A
caller that gets ``None`` back is expected to fall through to Core's existing
no-route behaviour (`_UNCONFIGURED_PROVIDER_ROUTE` in ``main.py``), so a malformed
route and an absent one terminalize identically -- a real, durable
``provider-unavailable``, never a distinct failure mode for "misconfigured" and never a
silent fallback to some other route.

**Two different failures stay two different failures once the route is reached.** A
connection that cannot be opened, is refused, or drops mid-stream raises
:class:`ProviderRouteUnavailable` (terminalizes ``provider-unavailable``); a response
that opens fine but sends unparseable NDJSON or a malformed event raises an ordinary
:class:`GenerationExecutorError` (terminalizes ``malformed-response`` through the
executor's own bare-exception path). See ``chat_generation_executor.py`` for why the
split matters.

Nothing here logs or prints the endpoint, the token, the request body, provider text,
provider ids or exception text: every refusal below is a fixed, sanitized message.

Standard library only.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from typing import Any, Final
from urllib.parse import urlsplit

from omnivia_core.chat_contract.v1 import ProviderInvocationRequest, to_canonical_json
from omnivia_core_runtime.service.chat_generation_executor import (
    GenerationExecutorConfig,
    GenerationExecutorError,
    ProviderRouteUnavailable,
    ProviderStream,
)

__all__ = [
    "CLASSIFICATION_REF_ENV",
    "CONNECTION_ID_ENV",
    "DEADLINE_SECONDS_ENV",
    "ENDPOINT_ENV",
    "MODEL_ID_ENV",
    "POLICY_REF_ENV",
    "RESIDENCY_REF_ENV",
    "SERVICE_ACTOR_ID_ENV",
    "TOKEN_ENV",
    "LocalProviderRouteClient",
    "provider_route_from_env",
]

ENDPOINT_ENV: Final = "OMNIVIA_CHAT_PROVIDER_ROUTE_ENDPOINT"
TOKEN_ENV: Final = "OMNIVIA_CHAT_PROVIDER_ROUTE_TOKEN"
CONNECTION_ID_ENV: Final = "OMNIVIA_CHAT_PROVIDER_CONNECTION_ID"
MODEL_ID_ENV: Final = "OMNIVIA_CHAT_PROVIDER_MODEL_ID"
POLICY_REF_ENV: Final = "OMNIVIA_CHAT_PROVIDER_POLICY_REF"
CLASSIFICATION_REF_ENV: Final = "OMNIVIA_CHAT_PROVIDER_CLASSIFICATION_REF"
RESIDENCY_REF_ENV: Final = "OMNIVIA_CHAT_PROVIDER_RESIDENCY_REF"
DEADLINE_SECONDS_ENV: Final = "OMNIVIA_CHAT_PROVIDER_DEADLINE_SECONDS"
SERVICE_ACTOR_ID_ENV: Final = "OMNIVIA_CHAT_PROVIDER_SERVICE_ACTOR_ID"

#: Required for a route to be considered configured at all -- see `provider_route_from_env`.
_REQUIRED_ENV: Final[tuple[str, ...]] = (
    ENDPOINT_ENV,
    TOKEN_ENV,
    CONNECTION_ID_ENV,
    MODEL_ID_ENV,
    POLICY_REF_ENV,
    CLASSIFICATION_REF_ENV,
    RESIDENCY_REF_ENV,
)

_DEFAULT_DEADLINE_SECONDS: Final = 120
_DEFAULT_SERVICE_ACTOR_ID: Final = "core.chat.generation"

#: Bounded NDJSON reading. Generous relative to the executor's own 262_144-byte text
#: cap so a well-formed stream is never truncated here, and small enough that a
#: mis-behaving or hostile bridge cannot grow this process's memory without bound.
_CHUNK_BYTES: Final = 65_536
_MAX_LINE_BYTES: Final = 524_288
_MAX_RESPONSE_BYTES: Final = 4_194_304
_WORKSPACE_SCOPED_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MODEL_ID: Final = re.compile(r"^(?!.*://)[A-Za-z0-9][A-Za-z0-9/._:@-]{0,255}$")
_BOUNDED_REF_MAX_CHARS: Final = 128


@dataclass(frozen=True, slots=True)
class _LocalEndpoint:
    host: str
    port: int
    path: str


def _local_endpoint(endpoint: str) -> _LocalEndpoint | None:
    """The host/port/path a strictly local-only bridge URL names, or ``None``.

    Mirrors the loopback-only, no-userinfo, no-query, IP-literal-only rule
    ``HttpBind``/``parse_http_endpoint`` already enforce for the *server* side of this
    process's own HTTP transport (``http_transport.py``), applied here to the *client*
    side: a route this build calls must be exactly as local as one it would serve. A
    hostname -- ``localhost`` included -- is refused rather than resolved, for the same
    reason: what a name resolves to is host configuration, not a route policy.
    """
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return None
    if parts.scheme != "http":
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.query or parts.fragment:
        return None
    host: str | None = None
    port: int | None = None
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if host is None or port is None:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not address.is_loopback:
        return None
    return _LocalEndpoint(host=host, port=port, path=parts.path or "/")


def _open_response(
    connection: HTTPConnection, path: str, body: bytes, headers: Mapping[str, str]
) -> Any:
    """POST the request and return the opened response, or fail closed.

    Every failure to open, send or refuse is collapsed to the same sanitized
    :class:`ProviderRouteUnavailable` -- an unreachable host, a refused connection and
    a non-2xx status from the bridge are all "no route was actually usable", and none
    of them is a fact about the response body a caller should try to parse.

    The refusal is raised outside the ``except`` block on purpose (a boolean flag,
    not a direct ``raise`` inside the handler): once that block has exited, there is no
    exception currently being handled, so the new one carries neither ``__cause__`` nor
    a live ``__context__`` back to whatever the connection or the OS reported.
    """
    response: Any = None
    failed = False
    try:
        connection.request("POST", path, body=body, headers=dict(headers))
        response = connection.getresponse()
    except (OSError, HTTPException):
        failed = True
    if failed or response is None:
        raise ProviderRouteUnavailable("the local provider route could not be reached")
    if response.status != 200:
        raise ProviderRouteUnavailable("the local provider route refused the request")
    return response


def _read_chunk(response: Any) -> tuple[bytes, bool]:
    try:
        return response.read(_CHUNK_BYTES), False
    except OSError:
        return b"", True


def _decode_event(line: bytes) -> Mapping[str, Any]:
    decoded: Any = None
    failed = False
    try:
        decoded = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        failed = True
    if failed or not isinstance(decoded, Mapping):
        raise GenerationExecutorError("a provider route response line is malformed")
    return decoded


def _ndjson_events(response: Any) -> Iterator[Mapping[str, Any]]:
    """Yield each event mapping from a bounded UTF-8 NDJSON response body.

    A read failure once the response is open (a dropped connection, a reset, a
    timeout) is still a route failure -- :class:`ProviderRouteUnavailable`, not a
    malformed-response case -- because the bridge never finished answering. Once a
    line is fully read, whatever is wrong with its bytes is a bad response instead:
    :class:`GenerationExecutorError`, which the executor's own bare-exception path
    terminalizes as ``malformed-response``.
    """
    buffer = b""
    total = 0
    while True:
        chunk, read_failed = _read_chunk(response)
        if read_failed:
            raise ProviderRouteUnavailable("the local provider route connection failed")
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise GenerationExecutorError("the provider route response exceeds the bounded size")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if line.strip():
                yield _decode_event(line)
        if len(buffer) > _MAX_LINE_BYTES:
            raise GenerationExecutorError("a provider route response line exceeds the bounded size")
    if buffer.strip():
        yield _decode_event(buffer)


def _workspace_scoped_id(value: str) -> bool:
    return bool(_WORKSPACE_SCOPED_ID.fullmatch(value))


def _model_id(value: str) -> bool:
    return bool(_MODEL_ID.fullmatch(value))


def _bounded_ref(value: str) -> bool:
    return 1 <= len(value) <= _BOUNDED_REF_MAX_CHARS


@dataclass(frozen=True, slots=True)
class LocalProviderRouteClient:
    """The ``invoke`` boundary: one governed request in, a bounded NDJSON stream out.

    Holds nothing but what it takes to open the connection and authenticate -- no
    provider identity, no adapter selection, no credential beyond the one bearer
    token this instance was configured with.
    """

    host: str
    port: int
    path: str
    token: str
    timeout_seconds: float

    def __call__(self, request: ProviderInvocationRequest) -> Iterator[Mapping[str, Any]]:
        body = to_canonical_json(request.to_wire()).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/x-ndjson",
            "Authorization": f"Bearer {self.token}",
        }
        connection = HTTPConnection(self.host, self.port, timeout=self.timeout_seconds)
        try:
            response = _open_response(connection, self.path, body, headers)
            yield from _ndjson_events(response)
        finally:
            connection.close()


def provider_route_from_env(
    env: Mapping[str, str],
) -> tuple[ProviderStream, GenerationExecutorConfig] | None:
    """The local provider-route client and executor config an environment names.

    ``None`` for every reason the route is not usable: any required variable is
    missing or empty, the endpoint fails the loopback-only rule, or the optional
    deadline is not a positive integer. The caller's job is to treat ``None`` exactly
    like no route was ever configured.
    """
    values = {name: (env.get(name, "") or "").strip() for name in _REQUIRED_ENV}
    if not all(values.values()):
        return None
    if not (
        _workspace_scoped_id(values[CONNECTION_ID_ENV])
        and _model_id(values[MODEL_ID_ENV])
        and _bounded_ref(values[POLICY_REF_ENV])
        and _bounded_ref(values[CLASSIFICATION_REF_ENV])
        and _bounded_ref(values[RESIDENCY_REF_ENV])
    ):
        return None
    endpoint = _local_endpoint(values[ENDPOINT_ENV])
    if endpoint is None:
        return None

    deadline_seconds = _DEFAULT_DEADLINE_SECONDS
    raw_deadline = env.get(DEADLINE_SECONDS_ENV)
    if raw_deadline is not None:
        try:
            deadline_seconds = int(raw_deadline)
        except ValueError:
            return None
        if deadline_seconds <= 0:
            return None

    client = LocalProviderRouteClient(
        host=endpoint.host,
        port=endpoint.port,
        path=endpoint.path,
        token=values[TOKEN_ENV],
        timeout_seconds=float(deadline_seconds),
    )
    config = GenerationExecutorConfig(
        connection_id=values[CONNECTION_ID_ENV],
        model_id=values[MODEL_ID_ENV],
        policy_ref=values[POLICY_REF_ENV],
        classification_ref=values[CLASSIFICATION_REF_ENV],
        residency_ref=values[RESIDENCY_REF_ENV],
        service_actor_id=env.get(SERVICE_ACTOR_ID_ENV) or _DEFAULT_SERVICE_ACTOR_ID,
        deadline_seconds=deadline_seconds,
    )
    return client, config
