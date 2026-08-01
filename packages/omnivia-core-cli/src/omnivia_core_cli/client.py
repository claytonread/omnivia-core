"""Core Service client for the `omnivia` CLI (B10).

Restrictions this module exists to honour, from the handoff brief and ADR-037:

- depends only on public `omnivia_core` contracts;
- does not import `omnivia_core_runtime`;
- never owns the authoritative workspace lease;
- never opens workspace SQLite directly for normal operation.

The first two are enforced by the package boundary checks. The last two are enforced
by construction: this module has no lock, no lease and no sqlite3 import, so there is
no code path through which it could acquire either.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnivia_core.contracts.v1 import (
    ClientIdentity,
    PrincipalClaim,
    RequestEnvelope,
    RequestMetadata,
    codec,
)

API_VERSION = "1.0"
CLIENT_NAME = "omnivia-cli"
CLIENT_VERSION = "0.1.0"


@dataclass(frozen=True)
class DiscoveredService:
    """A service this client may talk to, read from its discovery descriptor."""

    endpoint: str
    workspace_id: str
    service_instance_id: str
    fencing_generation: int
    api_version: str
    readiness: str

    @property
    def ready(self) -> bool:
        return self.readiness == "ready"


def read_descriptor(runtime_directory: Path) -> DiscoveredService | None:
    """Read a service descriptor without importing the runtime.

    The CLI reads the same file the service publishes, rather than linking against
    the runtime to ask. That keeps the dependency direction one-way.
    """
    path = runtime_directory / "service.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return DiscoveredService(
            endpoint=str(data["endpoint"]),
            workspace_id=str(data["workspace_id"]),
            service_instance_id=str(data["service_instance_id"]),
            fencing_generation=int(str(data["fencing_generation"])),
            api_version=str(data["api_version"]),
            readiness=str(data["readiness"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def build_request(
    operation: str,
    *,
    workspace_id: str,
    request_id: str,
    principal: str | None = None,
    scopes: tuple[str, ...] = (),
    payload: dict[str, Any] | None = None,
) -> RequestEnvelope:
    """Build a contract-valid request envelope.

    Constructed from the public contract types, so the CLI cannot invent a shape the
    service would not accept.
    """
    return RequestEnvelope(
        operation=operation,
        metadata=RequestMetadata(
            request_id=request_id,
            correlation_id=request_id,
            trace_id=request_id,
            api_version=API_VERSION,
            client=ClientIdentity(id=CLIENT_NAME, version=CLIENT_VERSION),
            workspace_id=workspace_id,
            scopes=tuple(scopes),
            purpose="cli",
            required_capabilities=(),
            # A claimed principal is a contract object, and it is only a
            # *claim*: the service decides authority from its own grant.
            principal_claim=(
                None if principal is None
                else PrincipalClaim(claimed_principal_id=principal)
            ),
        ),
        input=dict(payload or {}),
    )


def encode(request: RequestEnvelope) -> str:
    """Canonical wire form, for a transport to carry."""
    return codec.to_canonical_json(codec.encode_request(request))


__all__ = [
    "API_VERSION",
    "CLIENT_NAME",
    "CLIENT_VERSION",
    "DiscoveredService",
    "build_request",
    "encode",
    "read_descriptor",
]
