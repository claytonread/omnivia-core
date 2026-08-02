"""Pure semantic validation for public Core service endpoint descriptors.

The generated DTO decoder is intentionally structural and tolerant. The strict JSON
Schema and this module share the generated ``ServiceEndpointUri`` pattern so production
decode/encode paths enforce the same dialable-transport boundary without importing a
schema library or Runtime.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    SERVICE_ENDPOINT_URI_PATTERN,
    ServiceEndpointDescriptor,
    ServiceProbeResult,
)

__all__ = [
    "decode_service_endpoint_descriptor",
    "decode_service_probe_result",
    "encode_service_endpoint_descriptor",
    "encode_service_probe_result",
    "validate_service_endpoint_descriptor",
    "validate_service_probe_result",
]

_ENDPOINT_URI_MAX: Final = 2048
_ENDPOINT_URI_RE: Final = re.compile(SERVICE_ENDPOINT_URI_PATTERN)
_ENDPOINT_URI_REFUSAL: Final = (
    "endpoint_uri is not an approved credential-free dialable Core transport URI"
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


def validate_service_endpoint_descriptor(descriptor: object) -> None:
    """Reject a descriptor whose endpoint is not safe to publish before authentication.

    Endpoint publication policy is semantic here. Structural field presence and types
    remain the generated decoder's responsibility, while strict schema conformance
    remains the canonical JSON Schema's responsibility.

    The refusal never includes the rejected URI: the value may contain exactly the
    credential or direct-storage location this boundary exists to keep private.
    """
    if not isinstance(descriptor, ServiceEndpointDescriptor):
        raise ContractSemanticError("descriptor is not a ServiceEndpointDescriptor")
    if not _endpoint_uri_is_valid(descriptor.endpoint_uri):
        raise ContractSemanticError(_ENDPOINT_URI_REFUSAL)


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
