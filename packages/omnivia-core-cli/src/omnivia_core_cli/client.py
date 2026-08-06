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
from pathlib import Path
from typing import Any

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    CapabilityRequirement,
    ClientIdentity,
    PrincipalClaim,
    RequestEnvelope,
    RequestMetadata,
    ServiceEndpointDescriptor,
    codec,
    decode_service_endpoint_descriptor,
)

#: The application-contract revision this CLI claims on every request. Derived from
#: the contract this build links against, never restated. The literal `"1.0"` that
#: used to sit here was already two revisions stale, and nothing on the request path
#: validates the field -- so the stale claim produced no refusal, it just travelled:
#: the service builds the whole response version envelope *from it*, so a caller was
#: told the supported window was `[1.0, 1.0]` while the descriptor beside it
#: advertised `[1.2, 1.2]`. `ownership.discovery.is_compatible`, the one comparison
#: that reads a claimed version against an advertised window, answered `False` for
#: every request this CLI built. `omnivia_core_runtime.service.versions.API_VERSION`
#: and `omnivia_core_client.compatibility.CLIENT_API_VERSION` are derived the same
#: way, for the same reason.
API_VERSION = CONTRACT_VERSION

#: This distribution's own identity, which stays literal: neither is a contract
#: version and neither can be derived from one.
CLIENT_NAME = "omnivia-cli"
CLIENT_VERSION = "0.1.0"


def read_descriptor(runtime_directory: Path) -> ServiceEndpointDescriptor | None:
    """Read a service descriptor without importing the runtime.

    The CLI reads the same file the service publishes, rather than linking against
    the runtime to ask. That keeps the dependency direction one-way. It is decoded
    with the same public decoder the service encodes with, so this is a second
    *call* of one decoder rather than a second decoder: the private projection that
    used to live here read `endpoint`, `readiness` and `api_version` by hand, and
    when the published document moved to the public shape it silently reported
    every live service as absent.

    The public `ServiceEndpointDescriptor` is returned as it stands. A dataclass of
    this module's own would carry no fact the contract does not already carry, and
    keeping one is what let the two drift apart in the first place.

    A missing or malformed descriptor is absent rather than an error: the CLI's
    correct answer to garbage is "no service is advertised", the same as to an
    empty directory. `ContractDecodeError` and `ContractSemanticError` are both
    `ValueError`s, so every way the decoder refuses is caught here.
    """
    path = runtime_directory / "service.json"
    if not path.is_file():
        return None
    try:
        return decode_service_endpoint_descriptor(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, TypeError, ValueError):
        return None


def build_request(
    operation: str,
    *,
    workspace_id: str,
    request_id: str,
    principal: str | None = None,
    scopes: tuple[str, ...] = (),
    purpose: str = "cli",
    required_capabilities: tuple[CapabilityRequirement, ...] = (),
    payload: dict[str, Any] | None = None,
) -> RequestEnvelope:
    """Build a contract-valid request envelope.

    Constructed from the public contract types, so the CLI cannot invent a shape the
    service would not accept.

    `purpose` keeps its previous literal as the default, so every existing caller
    builds byte-identical requests to the ones it built before. It is a parameter
    now because the authorised application path grants a fixed allowlist of
    purposes and refuses anything outside it, and `"cli"` is not in that
    allowlist -- an operation-specific purpose has to be stated rather than
    assumed. Like every other field here it is only a *claim*: the service
    decides from its own grant whether the purpose it was handed is one this
    session may act under.

    `required_capabilities` defaults to empty for the same reason, and for
    catalogue operations it is not optional: the catalogue validator requires a
    request to declare exactly the capability its operation's frozen entry names,
    and refuses one that declares none. Callers derive it from that entry rather
    than writing an id and a version down here, so this CLI cannot claim a
    requirement the catalogue does not state.
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
            purpose=purpose,
            required_capabilities=tuple(required_capabilities),
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
    "build_request",
    "encode",
    "read_descriptor",
]
