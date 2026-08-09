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
- :mod:`~omnivia_core_client.local_ipc` -- :class:`LocalIpcTransport`, the one
  concrete transport, over the installation-local endpoint

**What is not here yet**, and must not be assumed: an HTTP transport, retry or
backoff, managed service startup, and the high-level client that would put them
together. Each arrives in its own packet.

:class:`LocalIpcTransport` is the single concrete transport, placed here by owner
resolution 005 R005-01. Every caller that dials a local Core service constructs it
from this package; there is no second implementation anywhere in the repository.

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
    "FRAME_FORMAT",
    "HEADER_BYTES",
    "LENGTH_BYTES",
    "LOCAL_IPC_SCHEME",
    "MAGIC",
    "MAGIC_HEX",
    "MAXIMUM_DESCRIPTOR_BYTES",
    "MAXIMUM_DURATION_MS",
    "MAXIMUM_JSON_BYTES",
    "MAXIMUM_JSON_NESTING_DEPTH",
    "MAXIMUM_TIMEOUT_SECONDS",
    "SUPPORTED_DESCRIPTOR_VERSION",
    "SUPPORTED_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "CancellationToken",
    "ClientError",
    "ClientTransport",
    "CompatibilityError",
    "Deadline",
    "DeadlineExceededError",
    "DiscoveredEndpoint",
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
    "select_api_version",
    "select_protocol_version",
    "socket_path_for",
    "validate_descriptor_version",
]
