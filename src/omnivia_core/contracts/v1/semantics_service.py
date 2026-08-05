"""Pure semantic validation for public Core service endpoint descriptors.

The generated DTO decoder is intentionally structural and tolerant. The strict JSON
Schema and this module share the generated ``ServiceEndpointUri`` and ``Timestamp``
patterns so production decode/encode paths enforce the same dialable-transport and
publication-instant boundaries without importing a schema library or Runtime.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    SERVICE_ENDPOINT_URI_PATTERN,
    TIMESTAMP_PATTERN,
    ServiceEndpointDescriptor,
    ServiceProbeResult,
)

__all__ = [
    "decode_service_endpoint_descriptor",
    "decode_service_probe_result",
    "encode_service_endpoint_descriptor",
    "encode_service_probe_result",
    "validate_service_endpoint_descriptor",
    "validate_service_endpoint_uri",
    "validate_service_probe_result",
]

_ENDPOINT_URI_MAX: Final = 2048
_ENDPOINT_URI_RE: Final = re.compile(SERVICE_ENDPOINT_URI_PATTERN)
_ENDPOINT_URI_REFUSAL: Final = (
    "endpoint_uri is not an approved credential-free dialable Core transport URI"
)

_TIMESTAMP_RE: Final = re.compile(TIMESTAMP_PATTERN)
_PUBLISHED_AT_REFUSAL: Final = (
    "published_at is not a canonical RFC 3339 UTC Timestamp"
)


def _endpoint_uri_is_valid(value: object) -> bool:
    """Apply the generated pattern, and nothing else.

    The pattern is the whole policy. Adding a Python-only post-check here -- an
    ``urlsplit`` reparse, say -- would make this runtime accept a different set of
    endpoints than the generated TypeScript guard, which is the drift the shared
    fixture exists to catch.
    """
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _ENDPOINT_URI_MAX
        and _ENDPOINT_URI_RE.fullmatch(value) is not None
    )


def validate_service_endpoint_uri(value: object) -> None:
    """Raise unless `value` is an endpoint URI Core's policy will publish.

    The endpoint question on its own, for a caller that has one field and not a
    descriptor. A caller holding a whole descriptor should keep using
    :func:`validate_service_endpoint_descriptor`.

    This exists because the descriptor validator answers about the *descriptor*,
    and a caller that catches its `ContractSemanticError` cannot tell which field
    produced it. That was invisible while the descriptor validator checked one
    field, and became a wrong answer the moment it checked two: Runtime's probe
    boundary caught the descriptor validator and reported every refusal as an
    endpoint-URI fault, so a malformed `published_at` was published to an
    unauthenticated caller as `endpoint_uri is not an approved transport
    endpoint`. Narrowing the question is what stops that recurring once per field
    as more of the declared value domains are enforced.
    """
    if not _endpoint_uri_is_valid(value):
        raise ContractSemanticError(_ENDPOINT_URI_REFUSAL)


def _published_at_is_valid(value: object) -> bool:
    """Apply the generated ``Timestamp`` pattern, then the calendar it cannot express.

    Both halves come from the schema. ``Timestamp`` declares a ``pattern`` *and*
    ``format: date-time``, and the two say different things: the pattern fixes the
    spelling -- UTC, a literal ``Z``, no numeric offset -- while ``2026-13-01T00:00:00Z``
    satisfies it character for character and names no instant that has ever existed.
    ``conformance`` applies exactly these two halves to a declared ``date-time``, and
    every sibling semantics module parses a pattern-conforming timestamp for the same
    reason, so nothing is invented here.

    No length bound is restated. ``Timestamp`` declares ``maxLength: 40`` and its
    longest in-language value is 30 characters, so the bound cannot bind and a
    Python-only length check would be a rule this pattern's other bindings do not have.
    """
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_service_endpoint_descriptor(descriptor: object) -> None:
    """Reject a descriptor whose published values are not the values it declares.

    Endpoint publication policy and the publication instant are semantic here.
    Structural field presence and types remain the generated decoder's responsibility,
    while strict schema conformance remains the canonical JSON Schema's responsibility.

    ``published_at`` is checked because the contract declares it a ``Timestamp``, not
    because anything downstream reads it. Nothing today turns on the value; the point
    is that a caller handed a decoded descriptor must never hold a field that
    contradicts the type the public contract publishes for it. Whether the other eight
    patterned descriptor fields join it here is a scope decision, not this function's.

    Neither refusal includes the rejected value. For the URI that is a privacy
    requirement -- it may carry exactly the credential or direct-storage location this
    pre-authentication boundary exists to keep private -- and the timestamp keeps the
    same discipline so this module has one rule rather than two, which also makes both
    refusal strings fixed and therefore stable to match on.
    """
    if not isinstance(descriptor, ServiceEndpointDescriptor):
        raise ContractSemanticError("descriptor is not a ServiceEndpointDescriptor")
    validate_service_endpoint_uri(descriptor.endpoint_uri)
    if not _published_at_is_valid(descriptor.published_at):
        raise ContractSemanticError(_PUBLISHED_AT_REFUSAL)


def decode_service_endpoint_descriptor(
    payload: object,
    path: str = "ServiceEndpointDescriptor",
) -> ServiceEndpointDescriptor:
    """Structurally decode and then enforce endpoint publication semantics."""
    descriptor = ServiceEndpointDescriptor.from_wire(payload, path)
    validate_service_endpoint_descriptor(descriptor)
    return descriptor


def encode_service_endpoint_descriptor(
    descriptor: object,
) -> Mapping[str, Any]:
    """Validate a descriptor before rendering it for public publication."""
    validate_service_endpoint_descriptor(descriptor)
    assert isinstance(descriptor, ServiceEndpointDescriptor)
    return descriptor.to_wire()


def validate_service_probe_result(result: object) -> None:
    """Validate nested discovery data before it reaches a public probe boundary."""
    if not isinstance(result, ServiceProbeResult):
        raise ContractSemanticError("result is not a ServiceProbeResult")
    if result.descriptor is not None:
        validate_service_endpoint_descriptor(result.descriptor)


def decode_service_probe_result(
    payload: object,
    path: str = "ServiceProbeResult",
) -> ServiceProbeResult:
    """Structurally decode a probe result, then validate its nested descriptor."""
    result = ServiceProbeResult.from_wire(payload, path)
    validate_service_probe_result(result)
    return result


def encode_service_probe_result(result: object) -> Mapping[str, Any]:
    """Validate a probe result before rendering public discovery data."""
    validate_service_probe_result(result)
    assert isinstance(result, ServiceProbeResult)
    return result.to_wire()
