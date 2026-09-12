"""Canonical UTF-8 JSON bytes and SHA-256 digests over semantic content.

Serialisation itself is delegated to :mod:`omnivia_core.contracts.v1.canonical_json`,
the existing RFC 8785 (JCS) implementation: it already sorts object keys,
rejects non-finite/non-lossless numbers, and rejects anything that is not a
JSON value -- exactly the "canonical UTF-8 JSON", "finite/stable decimal
handling" and "rejection of unsupported values" requirements. What is added
here, on top, is domain-specific:

- NFC normalisation of every string (spec 7.5), which JCS itself is silent on;
- semantic stable ordering -- elements by `element_id`, otherwise-unordered
  string collections by their own value -- since JCS preserves array order
  rather than imposing one;
- exclusion of publication/reviewer/display metadata from the digest, by
  building the payload from only the fields spec 7.5 says are semantic
  content: for a `ModelVersion`, its stable `model_id`, `meta_model_version`
  and elements -- never its generated `model_version_id`, version-framing
  (`version_sequence`/`version_label`/`parent_version_ids`) or stored
  `content_digest`; for a `ChangeOperation`, everything but `rationale`; and
  anything not part of either type at all, such as
  `ReviewRecord`/`PublicationRecord`.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, cast

from omnivia_core.contracts.v1.canonical_json import (
    canonical_bytes as _jcs_canonical_bytes,
)
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.semantic_registry.errors import (
    SemanticErrorCode,
    SemanticRegistryError,
)
from omnivia_core.semantic_registry.models import ModelVersion
from omnivia_core.semantic_registry.operations import ChangeOperation

CANONICALIZER_VERSION = "semantic-registry-canonical-v1"


def _plain(value: Any) -> Any:
    """Recursively lower `value` to a JSON-safe tree with semantic ordering.

    Enums become their `.value`; dataclasses become field dicts; mappings
    become plain dicts with NFC-normalised string keys, rejecting a
    non-string key outright and a post-normalisation key collision (two
    distinct keys that fold to the same NFC form would otherwise silently
    overwrite one another in the dict comprehension, before JCS ever sees a
    duplicate to reject); every string value is NFC-normalised (spec 7.5).

    A `tuple` of strings is this domain's one set-like collection shape --
    every dataclass field declared `tuple[str, ...]` (`parent_concept_ids`,
    `characteristics`, `depends_on_operation_ids`, ...) is an id/tag set
    whose own order carries no meaning, so it is sorted. A `list` is not:
    it is how arbitrary JSON array content arrives inside a genuinely
    heterogeneous payload (`Constraint.parameters`, `ChangeOperation.after`/
    `before`), where a caller's given order (e.g. a vocabulary's own
    enumeration order) may be semantically meaningful and must never be
    silently reordered. Callers that need a specific *dataclass* element
    order instead (the `elements` list, sorted by `element_id`) already
    produce that order as a list before calling this function.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, Enum):
        return _plain(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, bool) or not isinstance(key, str):
                raise SemanticRegistryError(
                    SemanticErrorCode.UNSUPPORTED_VALUE,
                    f"mapping keys must be strings, got {type(key).__name__}",
                )
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in result:
                raise SemanticRegistryError(
                    SemanticErrorCode.DUPLICATE_ID,
                    f"mapping keys collide after NFC normalisation: {normalized_key!r}",
                )
            result[normalized_key] = _plain(item)
        return result
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, tuple):
        items = [_plain(item) for item in value]
        if items and all(isinstance(item, str) for item in items):
            return sorted(items)
        return items
    if isinstance(value, Sequence):
        return [_plain(item) for item in value]
    return value


def canonical_bytes(value: Any) -> bytes:
    """Canonical UTF-8 JSON bytes for `value`, lowered through `_plain` first."""
    try:
        return _jcs_canonical_bytes(_plain(value))
    except ContractSemanticError as error:
        raise SemanticRegistryError(
            SemanticErrorCode.UNSUPPORTED_VALUE, str(error)
        ) from error


def content_digest(value: Any) -> str:
    """`sha256:<hex>` over the canonical bytes of `value`."""
    return f"sha256:{hashlib.sha256(canonical_bytes(value)).hexdigest()}"


def model_version_payload(version: ModelVersion) -> dict[str, Any]:
    """The semantic content of one version: stable model identity, meta-model
    version and canonically ordered elements -- never its generated version
    identity (`model_version_id`), version-framing (`version_sequence`,
    `version_label`, `parent_version_ids`), its own stored `content_digest`, or
    any timestamp/publication metadata (spec 7.5: a semantic content digest
    carries no volatile database identity).
    """
    elements_sorted = sorted(version.elements, key=lambda element: element.element_id)
    return {
        "model_id": version.model_id,
        "meta_model_version": version.meta_model_version,
        "elements": [_plain(element) for element in elements_sorted],
    }


def model_version_digest(version: ModelVersion) -> str:
    """The digest a version's own `content_digest` field must equal."""
    return content_digest(model_version_payload(version))


def operation_payload(operation: ChangeOperation) -> dict[str, Any]:
    """One operation's semantic content: `rationale` excluded (spec 11)."""
    payload = cast(dict[str, Any], _plain(operation))
    del payload["rationale"]
    return payload


def change_set_payload(
    base_version_id: str,
    base_digest: str,
    ordered_operations: Sequence[ChangeOperation],
) -> dict[str, Any]:
    """The semantic content of a change set, given its operations in canonical order."""
    return {
        "base_version_id": base_version_id,
        "base_digest": base_digest,
        "operations": [
            operation_payload(operation) for operation in ordered_operations
        ],
    }


def change_set_digest(
    base_version_id: str,
    base_digest: str,
    ordered_operations: Sequence[ChangeOperation],
) -> str:
    return content_digest(
        change_set_payload(base_version_id, base_digest, ordered_operations)
    )


__all__ = [
    "CANONICALIZER_VERSION",
    "canonical_bytes",
    "change_set_digest",
    "change_set_payload",
    "content_digest",
    "model_version_digest",
    "model_version_payload",
]
