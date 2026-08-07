"""Pure semantic validation for public Core service endpoint descriptors.

The generated DTO decoder is intentionally structural and tolerant. The strict JSON
Schema and this module share the generated ``ServiceEndpointUri`` and ``Timestamp``
value domains so production decode/encode paths enforce the same dialable-transport
and publication-instant boundaries without importing a schema library or Runtime.

The value domains are applied by calling the generated guards rather than by
compiling the published patterns again here. The generator emits those guards into
both bindings from one loop over the schema, so this module and the generated
TypeScript cannot disagree about what a well-formed value is; a second local copy
of the rule is exactly how they came to disagree before.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    ServiceEndpointDescriptor,
    ServiceProbeResult,
    is_service_endpoint_uri,
    is_timestamp,
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

_ENDPOINT_URI_REFUSAL: Final = (
    "endpoint_uri is not an approved credential-free dialable Core transport URI"
)

_PUBLISHED_AT_REFUSAL: Final = (
    "published_at is not a canonical RFC 3339 UTC Timestamp"
)

_OBSERVED_AT_REFUSAL: Final = (
    "observed_at is not a canonical RFC 3339 UTC Timestamp"
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
    if not is_service_endpoint_uri(value):
        raise ContractSemanticError(_ENDPOINT_URI_REFUSAL)


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
    if not is_timestamp(descriptor.published_at):
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
    """Validate discovery data before it reaches a public probe boundary.

    ``observed_at`` is checked here for the reason ``published_at`` is checked on the
    descriptor: the contract declares it a ``Timestamp``, and a caller handed a decoded
    probe result must never hold a field that contradicts the type the public contract
    publishes for it. It was the one field this call decoded and ignored -- the nested
    descriptor's ``published_at`` was enforced while its sibling one field away was
    not, which is an incoherence rather than a policy.

    Its refusal names ``observed_at`` and not the descriptor. A caller that catches
    ``ContractSemanticError`` cannot otherwise tell which field produced it, and
    reporting a probe-result fault as a descriptor fault is the same misattribution
    that made a malformed ``published_at`` surface as an endpoint-URI fault.
    """
    if not isinstance(result, ServiceProbeResult):
        raise ContractSemanticError("result is not a ServiceProbeResult")
    if not is_timestamp(result.observed_at):
        raise ContractSemanticError(_OBSERVED_AT_REFUSAL)
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
