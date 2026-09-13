"""The shared OV-CJ-1 canonical-byte content-size limit (KI-01 6.2, KI-04 9.2).

Position and feedback profile *content* is bounded to 16 KiB of canonical bytes
under the accepted OV-CJ-1 implementation: `semantic_registry.canonical`, which
itself delegates to `contracts.v1.canonical_json`'s RFC 8785 (JCS) serializer --
the "accepted canonical JSON implementation" the binding pass is required to
reuse rather than a second serializer (spec 2.3). The bound is over canonical
*bytes*, not characters or any one field, so it cannot be dodged by an
alternate encoding of byte-identical content.
"""

from __future__ import annotations

from typing import Any

from omnivia_core.governed_knowledge.errors import (
    GovernedKnowledgeErrorCode,
    GovernedKnowledgeValidationError,
    require,
)
from omnivia_core.semantic_registry.canonical import canonical_bytes
from omnivia_core.semantic_registry.errors import SemanticRegistryError

OV_CJ1_MAX_CONTENT_BYTES = 16 * 1024


def enforce_ov_cj1_content_limit(payload: Any) -> bytes:
    """Return `payload`'s OV-CJ-1 canonical bytes, or raise if over 16 KiB.

    A payload that is not itself JSON-canonicalizable (an unsupported Python
    value, a non-finite number, a duplicate object member) is refused the same
    way -- as a typed `GovernedKnowledgeValidationError`, never the lower
    layer's own `SemanticRegistryError` escaping this package's boundary.
    """
    try:
        encoded = canonical_bytes(payload)
    except SemanticRegistryError as error:
        raise GovernedKnowledgeValidationError(
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            f"profile content is not OV-CJ-1 canonicalizable: {error}",
        ) from error
    require(
        len(encoded) <= OV_CJ1_MAX_CONTENT_BYTES,
        GovernedKnowledgeErrorCode.PAYLOAD_TOO_LARGE,
        f"profile content exceeds the {OV_CJ1_MAX_CONTENT_BYTES}-byte OV-CJ-1 "
        "canonical limit",
    )
    return encoded


__all__ = ["OV_CJ1_MAX_CONTENT_BYTES", "enforce_ov_cj1_content_limit"]
