"""What this client build can talk to, and at which versions.

Three independent version questions, answered separately because they fail for
different reasons and a caller needs to know which one failed:

- the **API contract version** -- negotiated, by picking the highest version
  both this build and the endpoint admit;
- the **OVC1 protocol version** -- not negotiated. This build implements
  exactly :data:`SUPPORTED_PROTOCOL_VERSION`, and anything else is refused;
- the **descriptor shape version** -- read, not negotiated. A same-major
  descriptor is readable however many additive minors ahead of this build it
  is; another major is not.

**Nothing here hard-codes an API version.** The API version this build speaks
is ``omnivia_core.contracts.v1.CONTRACT_VERSION`` and the client window is
derived from it, so a Core contract release moves this client with it rather
than leaving a stale literal behind. The one version literal in this module is
the OVC1 protocol version, which is this package's own frozen wire format and
belongs to it.

**Every failure is closed, and every failure is a**
:class:`~omnivia_core_client.errors.CompatibilityError`. A malformed or reversed
window, windows with no overlap, a protocol or descriptor major this build does
not implement: each raises rather than returning a best guess. Version
negotiation is the one place where guessing produces a peer that appears to work
and then misreads a document, which is strictly worse than not connecting.

Every function here is a *direct* entry point. A descriptor can be built by
hand or reshaped with ``dataclasses.replace``, and a version field is only a
string by convention, so anything at all can arrive where a ``major.minor``
belongs. Each of them is checked before it reaches the public contract helpers,
which raise a bare ``TypeError`` from inside a regular-expression call for a
non-string and a ``ContractSemanticError`` for a malformed one. Neither escapes:
a caller of this package catches
:class:`~omnivia_core_client.errors.ClientError` and should not have to also
know the contract layer's exception hierarchy, nor guard against the standard
library's.

Standard library plus the public ``omnivia_core`` contracts only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ContractSemanticError,
    ServiceEndpointDescriptor,
    VersionWindow,
    parse_contract_version,
    validate_version_window,
)
from omnivia_core_client.errors import CompatibilityError

__all__ = [
    "CLIENT_API_VERSION",
    "CLIENT_SUPPORTED_API_VERSIONS",
    "SUPPORTED_DESCRIPTOR_VERSION",
    "SUPPORTED_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "NegotiatedEndpoint",
    "negotiate_endpoint",
    "select_api_version",
    "select_protocol_version",
    "validate_descriptor_version",
]

CLIENT_API_VERSION: Final = CONTRACT_VERSION
"""The API contract version this build was written against. Taken from the
public contract, never restated as a literal."""

SUPPORTED_DESCRIPTOR_VERSION: Final = CONTRACT_VERSION
"""The service endpoint descriptor shape this build fully understands. The
descriptor is part of the same contract, so it moves with it."""

SUPPORTED_PROTOCOL_VERSION: Final = "1.0"
"""The OVC1 wire protocol version this build implements, exactly.

A literal, and legitimately so: this is the version of *this package's* frozen
frame format, not of the Core contract. It changes when the frame format
changes, which is a change to this package."""

SUPPORTED_PROTOCOL_VERSIONS: Final = (SUPPORTED_PROTOCOL_VERSION,)
"""Every OVC1 protocol version this build implements. Exactly one, today."""


def _client_api_window() -> VersionWindow:
    """The inclusive API window this build offers a server, derived from the contract.

    The maximum is the contract version this build was written against. The
    minimum is ``<major>.0``: within a major, minor releases are additive, so a
    build that understands ``1.2`` can still talk to a ``1.0`` server -- it
    simply gets fewer fields, which the tolerant public decoders already handle.
    Both bounds are derived, so neither can go stale.
    """
    major, _ = parse_contract_version(CONTRACT_VERSION)
    return VersionWindow(minimum=f"{major}.0", maximum=CONTRACT_VERSION)


CLIENT_SUPPORTED_API_VERSIONS: Final = _client_api_window()
"""The inclusive API contract version window this build offers an endpoint."""


@dataclass(frozen=True, slots=True)
class NegotiatedEndpoint:
    """The three versions in force for calls to one endpoint, once agreed."""

    api_version: str
    """The API contract version both peers admit, and the highest such."""

    protocol_version: str
    """The OVC1 wire protocol version, always :data:`SUPPORTED_PROTOCOL_VERSION`."""

    descriptor_version: str
    """The shape version of the descriptor this was read from, verbatim. Kept
    because it may be a newer minor than this build knows: a caller logging or
    reporting the endpoint should say what the endpoint actually published."""


def _require_version(value: str, label: str) -> tuple[int, int]:
    """Return a ``major.minor``'s parsed components, refusing anything else.

    The type check is this package's rather than the contract's because the
    contract has none: ``parse_contract_version`` matches a regular expression,
    which raises ``TypeError`` for a non-string from inside the standard
    library. That is a true statement about ``re``, and no statement at all
    about versions, so it is replaced here by one that names the argument and
    the shape expected of it.
    """
    if not isinstance(value, str):
        raise CompatibilityError(
            f"{label} must be a `major.minor` string, got {type(value).__name__}"
        )
    try:
        return parse_contract_version(value)
    except (ContractSemanticError, TypeError, ValueError) as error:
        raise CompatibilityError(
            f"{label} {value!r} is not a `major.minor` version"
        ) from error


def _require_window(
    window: VersionWindow, label: str
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return a window's parsed bounds, refusing anything malformed.

    A reversed window (``minimum`` above ``maximum``) is malformed input, not a
    window that matches nothing: passed through, it would make every selection
    below fail with "no overlap" and hide the real fault. The public contract
    already draws that line, so the check is delegated to it and only the
    exception type is translated.

    The bounds are type-checked here first, before ``validate_version_window``
    sees them. A ``VersionWindow`` is an ordinary dataclass, so it holds
    whatever it was constructed with, and a non-string bound would otherwise
    reach the contract's regular expression and come back as a bare
    ``TypeError`` rather than as a statement about this window.
    """
    if not isinstance(window, VersionWindow):
        raise CompatibilityError(
            f"{label}: expected a VersionWindow, got {type(window).__name__}"
        )
    minimum = _require_version(window.minimum, f"{label} minimum")
    maximum = _require_version(window.maximum, f"{label} maximum")
    try:
        validate_version_window(window)
    except ContractSemanticError as error:
        raise CompatibilityError(f"{label}: {error}") from error
    return minimum, maximum


def select_api_version(client: VersionWindow, server: VersionWindow) -> str:
    """Return the highest API contract version both inclusive windows admit.

    Both windows are ranges over ``(major, minor)`` pairs, so their overlap is
    the range ``[max(minimums), min(maximums)]`` and its top is a version each
    side independently declared it supports. Highest rather than lowest because
    a minor release is additive: the newest mutually supported minor is the one
    where both peers have the most fields available, and choosing lower would
    discard capability neither side objected to.

    A selected version always sits inside one major, which is what makes it
    meaningful -- a major change is breaking by definition, so there is no
    version "between" two majors to fall back to. When there is no overlap the
    failure distinguishes the two cases, because they call for different
    actions: no major in common means one peer must be replaced, while a shared
    major with disjoint minors means one peer must be upgraded.
    """
    client_bounds = _require_window(client, "client API version window")
    server_bounds = _require_window(server, "server API version window")
    lower = max(client_bounds[0], server_bounds[0])
    upper = min(client_bounds[1], server_bounds[1])
    if lower <= upper:
        return f"{upper[0]}.{upper[1]}"

    client_majors = range(client_bounds[0][0], client_bounds[1][0] + 1)
    server_majors = range(server_bounds[0][0], server_bounds[1][0] + 1)
    shared_major = set(client_majors) & set(server_majors)
    reason = (
        "no API major version in common, which no upgrade of either side alone resolves"
        if not shared_major
        else "a major version in common but no minor version in common, so one side "
        "must be upgraded"
    )
    raise CompatibilityError(
        f"no mutually supported API version: this client supports "
        f"[{client.minimum}, {client.maximum}] and the endpoint supports "
        f"[{server.minimum}, {server.maximum}] -- {reason}"
    )


def select_protocol_version(advertised: str) -> str:
    """Return the OVC1 protocol version to speak, or refuse to speak at all.

    Not a negotiation. This build implements exactly
    :data:`SUPPORTED_PROTOCOL_VERSION`, so the only acceptable answer is that
    the endpoint advertises exactly that. A different *major* can never be
    spoken: the frame format itself differs, and a reader that guessed would be
    misreading bytes rather than misreading a document. A newer *minor* is
    refused too, and for a plainer reason -- this build does not implement it,
    and claiming to would be a claim about frames it has never seen.
    """
    supported = parse_contract_version(SUPPORTED_PROTOCOL_VERSION)
    offered = _require_version(advertised, "endpoint protocol version")
    if offered[0] != supported[0]:
        raise CompatibilityError(
            f"endpoint speaks OVC1 protocol major {offered[0]}; this client implements "
            f"exactly {SUPPORTED_PROTOCOL_VERSION}. A protocol major is a different "
            "frame format, so the call is refused rather than attempted"
        )
    if offered != supported:
        raise CompatibilityError(
            f"endpoint advertises OVC1 protocol {advertised}; this client implements "
            f"exactly {SUPPORTED_PROTOCOL_VERSION} and does not implement that minor"
        )
    return SUPPORTED_PROTOCOL_VERSION


def validate_descriptor_version(descriptor_version: str) -> str:
    """Return ``descriptor_version`` when this build can read that descriptor shape.

    Same major as :data:`SUPPORTED_DESCRIPTOR_VERSION` is readable at any minor,
    including one ahead of this build: within a major, a minor release only
    *adds* optional fields, and the public DTO decoder already ignores fields it
    does not know. Another major is refused -- that is what a major means.

    Accepting a newer minor here says nothing about how its extra fields decode.
    Unknown optional fields stay the decoder's concern, and this check does not
    loosen the public DTO decoding in any way: a newer minor whose *required*
    shape this build cannot satisfy still fails where it is decoded, which is
    the layer that can actually tell.
    """
    supported_major, _ = parse_contract_version(SUPPORTED_DESCRIPTOR_VERSION)
    major, _ = _require_version(descriptor_version, "descriptor version")
    if major != supported_major:
        raise CompatibilityError(
            f"descriptor shape version {descriptor_version} is major {major}; this "
            f"client reads descriptor major {supported_major} only"
        )
    return descriptor_version


def negotiate_endpoint(
    descriptor: ServiceEndpointDescriptor,
    *,
    client_api_versions: VersionWindow = CLIENT_SUPPORTED_API_VERSIONS,
) -> NegotiatedEndpoint:
    """Decide whether this build can talk to one published endpoint, and how.

    The checks run shape-first: a descriptor whose own version this build
    cannot read is not a descriptor whose fields may be trusted, so its
    protocol and API claims are not consulted at all. Protocol comes next,
    because a frame format mismatch makes the API version moot.

    Readiness is deliberately not checked. ``descriptor.ready`` and
    ``lifecycle_state`` are runtime state that changes without the versions
    changing; deciding what to do about a not-yet-ready endpoint is the caller's
    to make, and folding it in here would conflate "cannot talk to this build"
    with "cannot talk to it yet".

    Workspace format versions are likewise not negotiated here. Which workspace
    format a server can serve is settled between the server and its workspace;
    a client neither reads nor writes that storage, and a client-side check
    would be an opinion about a decision it has no part in.
    """
    if not isinstance(descriptor, ServiceEndpointDescriptor):
        raise CompatibilityError(
            f"expected a ServiceEndpointDescriptor, got {type(descriptor).__name__}"
        )
    descriptor_version = validate_descriptor_version(descriptor.descriptor_version)
    protocol_version = select_protocol_version(descriptor.protocol_version)
    api_version = select_api_version(
        client_api_versions, descriptor.supported_api_versions
    )
    return NegotiatedEndpoint(
        api_version=api_version,
        protocol_version=protocol_version,
        descriptor_version=descriptor_version,
    )
