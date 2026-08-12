"""omnivia-core-client: the shared client protocol foundation for OmniVia Core.

The pieces every OmniVia client needs before any of them can talk to a service:
installation-local endpoint discovery, the frozen OVC1 frame, the whole-call
deadline and its cancellation token, the version rules that decide whether this
build can talk to an endpoint at all, and the transport contract each concrete
transport will satisfy.

It establishes the compile-time dependency boundary defined by ADR-036: this
distribution depends on the public ``omnivia-core`` contracts and on nothing
else -- not the runtime, not the CLI, not the MCP surface, and no third-party
library.

**What is here (Phase 3 P1b)**

- :mod:`~omnivia_core_client.framing` -- pure OVC1 frame encode and decode
- :mod:`~omnivia_core_client.deadline` -- :class:`Deadline`, :class:`CancellationToken`
- :mod:`~omnivia_core_client.transport` -- the :class:`ClientTransport` protocol
- :mod:`~omnivia_core_client.compatibility` -- API, protocol, and descriptor versions
- :mod:`~omnivia_core_client.discovery` -- safe descriptor discovery and live identity
- :mod:`~omnivia_core_client.errors` -- the typed failures those raise
- :mod:`~omnivia_core_client.local_ipc` -- :class:`LocalIpcTransport`, over the
  installation-local endpoint
- :mod:`~omnivia_core_client.credentials` -- the endpoint-bound credential seam:
  an opaque :class:`CredentialReference`, the opaque :class:`Credential` carrier,
  and the :class:`CredentialCache` that calls the host's injected resolver
- :mod:`~omnivia_core_client.http_transport` -- :class:`HttpTransport`, the
  authenticated HTTP v1 transport, over a verified ``https`` endpoint or a
  loopback cleartext one

**What is not here yet**, and must not be assumed: retry or backoff, managed
service startup, the Windows named-pipe transport, and the high-level client that
would put them together. Each arrives in its own packet.

:class:`LocalIpcTransport` and :class:`HttpTransport` are the two concrete
transports, and both live here -- the first by owner resolution 005 R005-01, the
second because it satisfies the same protocol over the same envelopes and would
otherwise be a second dial loop in whichever caller needed it first. Every caller
that dials a Core service constructs one of them from this package; there is no
third implementation anywhere in the repository.

**No credential source ships here, and none is implied.** The credential seam
takes a resolver the host injects and defines no token format, issuer, store,
broker or keychain, reads no environment variable or configuration file, and
holds no policy about when a service should serve HTTP at all.

Standard library plus the public ``omnivia_core`` contracts only.
"""

from __future__ import annotations

from omnivia_core_client.compatibility import (
    CLIENT_API_VERSION,
    CLIENT_SUPPORTED_API_VERSIONS,
    SUPPORTED_DESCRIPTOR_VERSION,
    SUPPORTED_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    NegotiatedEndpoint,
    negotiate_endpoint,
    select_api_version,
    select_protocol_version,
    validate_descriptor_version,
)
from omnivia_core_client.credentials import (
    DEFAULT_CREDENTIAL_TTL_SECONDS,
    MAXIMUM_CREDENTIAL_CHARACTERS,
    MAXIMUM_REFERENCE_CHARACTERS,
    Credential,
    CredentialCache,
    CredentialReference,
    CredentialResolver,
)
from omnivia_core_client.deadline import (
    MAXIMUM_DURATION_MS,
    MAXIMUM_TIMEOUT_SECONDS,
    CancellationToken,
    Deadline,
    MonotonicClock,
)
from omnivia_core_client.discovery import (
    MAXIMUM_DESCRIPTOR_BYTES,
    DiscoveredEndpoint,
    descriptor_path,
    discover_endpoint,
)
from omnivia_core_client.errors import (
    ClientError,
    CompatibilityError,
    CredentialDeniedError,
    CredentialError,
    CredentialInvalidError,
    CredentialMissingError,
    CredentialUnavailableError,
    DeadlineExceededError,
    OperationCancelledError,
    ProtocolError,
    TransportError,
)
from omnivia_core_client.framing import (
    CANONICAL_JSON_ALGORITHM,
    FRAME_FORMAT,
    HEADER_BYTES,
    LENGTH_BYTES,
    MAGIC,
    MAGIC_HEX,
    MAXIMUM_JSON_BYTES,
    MAXIMUM_JSON_NESTING_DEPTH,
    canonical_json_bytes,
    decode_frame,
    encode_frame,
)
from omnivia_core_client.http_transport import (
    HttpEndpoint,
    HttpTransport,
    parse_http_endpoint,
)
from omnivia_core_client.local_ipc import (
    LOCAL_IPC_SCHEME,
    LocalIpcTransport,
    socket_path_for,
)
from omnivia_core_client.transport import (
    ClientTransport,
    enforce_send_preconditions,
)

__version__ = "0.1.0"

__all__ = [
    "CANONICAL_JSON_ALGORITHM",
    "CLIENT_API_VERSION",
    "CLIENT_SUPPORTED_API_VERSIONS",
    "DEFAULT_CREDENTIAL_TTL_SECONDS",
    "FRAME_FORMAT",
    "HEADER_BYTES",
    "LENGTH_BYTES",
    "LOCAL_IPC_SCHEME",
    "MAGIC",
    "MAGIC_HEX",
    "MAXIMUM_CREDENTIAL_CHARACTERS",
    "MAXIMUM_DESCRIPTOR_BYTES",
    "MAXIMUM_DURATION_MS",
    "MAXIMUM_JSON_BYTES",
    "MAXIMUM_JSON_NESTING_DEPTH",
    "MAXIMUM_REFERENCE_CHARACTERS",
    "MAXIMUM_TIMEOUT_SECONDS",
    "SUPPORTED_DESCRIPTOR_VERSION",
    "SUPPORTED_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "CancellationToken",
    "ClientError",
    "ClientTransport",
    "CompatibilityError",
    "Credential",
    "CredentialCache",
    "CredentialDeniedError",
    "CredentialError",
    "CredentialInvalidError",
    "CredentialMissingError",
    "CredentialReference",
    "CredentialResolver",
    "CredentialUnavailableError",
    "Deadline",
    "DeadlineExceededError",
    "DiscoveredEndpoint",
    "HttpEndpoint",
    "HttpTransport",
    "LocalIpcTransport",
    "MonotonicClock",
    "NegotiatedEndpoint",
    "OperationCancelledError",
    "ProtocolError",
    "TransportError",
    "__version__",
    "canonical_json_bytes",
    "decode_frame",
    "descriptor_path",
    "discover_endpoint",
    "encode_frame",
    "enforce_send_preconditions",
    "negotiate_endpoint",
    "parse_http_endpoint",
    "select_api_version",
    "select_protocol_version",
    "socket_path_for",
    "validate_descriptor_version",
]
