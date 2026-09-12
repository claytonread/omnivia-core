"""Phase 2 observation contracts: raw semantic observations and their features.

Observations carry only normalised text/roles and identifiers -- never raw
evidence content or bytes -- per spec 7.5/11: a digest or canonical payload
built from these types must never let protected evidence content leak into a
log line or an error message.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from omnivia_core.semantic_registry.canonical import canonical_bytes, content_digest
from omnivia_core.semantic_registry.errors import (
    SemanticErrorCode,
    SemanticValidationError,
    require,
)
from omnivia_core.semantic_registry.evidence import (
    Classification,
    EvidenceLink,
    effective_classification,
    evidence_link_payload,
)
from omnivia_core.semantic_registry.temporal import TemporalInstant, instant_payload

OBSERVATION_SCHEMA_VERSION = "1.0.0"

_MAX_KIND_LEN = 200


def _require_id(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and value.strip() != "",
        SemanticErrorCode.MISSING_FIELD,
        f"{field_name} is required",
    )


def _require_text(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and value.strip() != "",
        SemanticErrorCode.MISSING_FIELD,
        f"{field_name} is required",
    )


class ObservationStatus(str, Enum):
    """Lifecycle state of an observation."""

    RECORDED = "recorded"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class ObservationGeneration(str, Enum):
    """How an observation was produced."""

    MANUAL = "manual"
    DETERMINISTIC_RULE = "deterministic_rule"


class ObservationValueKind(str, Enum):
    """The shape of the value an observation carries."""

    TEXT = "text"
    IDENTIFIER = "identifier"
    RELATIONSHIP = "relationship"
    CONSTRAINT = "constraint"


@dataclass(frozen=True, slots=True)
class SemanticObservation:
    """One recorded semantic observation -- normalised text and roles only."""

    observation_id: str
    workspace_id: str
    kind: str
    value_kind: ObservationValueKind
    original_form: str
    normalized_form: str
    proposed_semantic_role: str
    classification: Classification
    generation: ObservationGeneration
    recorded_at: TemporalInstant
    status: ObservationStatus = ObservationStatus.RECORDED
    source_time: TemporalInstant | None = None
    supersedes_observation_id: str | None = None
    rule_version: str | None = None
    schema_version: str = field(init=False, default=OBSERVATION_SCHEMA_VERSION)

    def __post_init__(self) -> None:
        _require_id("observation_id", self.observation_id)
        _require_id("workspace_id", self.workspace_id)
        _require_text("kind", self.kind)
        require(
            len(self.kind) <= _MAX_KIND_LEN,
            SemanticErrorCode.INVALID_FIELD,
            f"kind must be at most {_MAX_KIND_LEN} characters",
        )
        require(
            isinstance(self.value_kind, ObservationValueKind),
            SemanticErrorCode.UNSUPPORTED_VALUE,
            "value_kind must be an ObservationValueKind",
        )
        _require_text("original_form", self.original_form)
        _require_text("normalized_form", self.normalized_form)
        _require_text("proposed_semantic_role", self.proposed_semantic_role)
        require(
            isinstance(self.classification, Classification),
            SemanticErrorCode.UNSUPPORTED_VALUE,
            "classification must be a Classification",
        )
        require(
            isinstance(self.generation, ObservationGeneration),
            SemanticErrorCode.UNSUPPORTED_VALUE,
            "generation must be an ObservationGeneration",
        )
        require(
            isinstance(self.status, ObservationStatus),
            SemanticErrorCode.UNSUPPORTED_VALUE,
            "status must be an ObservationStatus",
        )
        require(
            isinstance(self.recorded_at, TemporalInstant),
            SemanticErrorCode.INVALID_FIELD,
            "recorded_at must be a TemporalInstant",
        )
        if self.source_time is not None:
            require(
                isinstance(self.source_time, TemporalInstant),
                SemanticErrorCode.INVALID_FIELD,
                "source_time must be a TemporalInstant",
            )
        if self.generation is ObservationGeneration.MANUAL:
            require(
                self.rule_version is None,
                SemanticErrorCode.INVALID_FIELD,
                "manual observations must not carry a rule_version",
            )
        else:
            _require_id("rule_version", self.rule_version or "")
        if self.supersedes_observation_id is not None:
            _require_id(
                "supersedes_observation_id", self.supersedes_observation_id
            )
            require(
                self.supersedes_observation_id != self.observation_id,
                SemanticErrorCode.INVALID_FIELD,
                "an observation cannot supersede itself",
            )


def _validate_feature_value(field_name: str, value: Any) -> None:
    if isinstance(value, float):
        raise SemanticValidationError(
            SemanticErrorCode.UNSUPPORTED_VALUE,
            f"{field_name} must be a fixed-point integer, not a float",
        )
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            require(
                isinstance(key, str),
                SemanticErrorCode.UNSUPPORTED_VALUE,
                f"{field_name} mapping keys must be strings",
            )
            _validate_feature_value(f"{field_name}.{key}", item)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_feature_value(f"{field_name}[{index}]", item)
        return
    raise SemanticValidationError(
        SemanticErrorCode.UNSUPPORTED_VALUE,
        f"{field_name} must be a JSON-canonicalizable scalar, list, or mapping",
    )


def _freeze_feature_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_feature_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_feature_value(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class ObservationFeature:
    """One derived feature attached to an observation -- JSON-safe value only."""

    workspace_id: str
    observation_id: str
    feature_name: str
    value: Any
    policy_version: str
    calculation_version: str
    schema_version: str = field(init=False, default=OBSERVATION_SCHEMA_VERSION)

    def __post_init__(self) -> None:
        _require_id("workspace_id", self.workspace_id)
        _require_id("observation_id", self.observation_id)
        _require_id("feature_name", self.feature_name)
        _require_id("policy_version", self.policy_version)
        _require_id("calculation_version", self.calculation_version)
        _validate_feature_value("value", self.value)
        frozen_value = _freeze_feature_value(self.value)
        object.__setattr__(self, "value", frozen_value)
        canonical_bytes(frozen_value)


@dataclass(frozen=True, slots=True)
class ObservationBundle:
    """One observation together with its evidence links and derived features."""

    observation: SemanticObservation
    evidence_links: tuple[EvidenceLink, ...]
    features: tuple[ObservationFeature, ...] = ()

    def __post_init__(self) -> None:
        require(
            isinstance(self.observation, SemanticObservation),
            SemanticErrorCode.INVALID_FIELD,
            "observation must be a SemanticObservation",
        )
        require(
            len(self.evidence_links) > 0,
            SemanticErrorCode.MISSING_FIELD,
            "an observation bundle requires at least one evidence link",
        )
        seen_links: set[tuple[str, str | None, str]] = set()
        for index, link in enumerate(self.evidence_links):
            require(
                isinstance(link, EvidenceLink),
                SemanticErrorCode.INVALID_FIELD,
                f"evidence_links[{index}] must be an EvidenceLink",
            )
            require(
                link.workspace_id == self.observation.workspace_id,
                SemanticErrorCode.CROSS_WORKSPACE_ACCESS,
                f"evidence_links[{index}].workspace_id does not match the observation",
            )
            require(
                link.observation_id == self.observation.observation_id,
                SemanticErrorCode.UNKNOWN_REFERENCE,
                f"evidence_links[{index}].observation_id does not match the observation",
            )
            key = (link.evidence_id, link.span_id, link.role.value)
            require(
                key not in seen_links,
                SemanticErrorCode.DUPLICATE_ID,
                f"evidence_links[{index}] duplicates an existing "
                "(evidence_id, span_id, role)",
            )
            seen_links.add(key)

        seen_features: set[str] = set()
        for index, feature in enumerate(self.features):
            require(
                isinstance(feature, ObservationFeature),
                SemanticErrorCode.INVALID_FIELD,
                f"features[{index}] must be an ObservationFeature",
            )
            require(
                feature.workspace_id == self.observation.workspace_id,
                SemanticErrorCode.CROSS_WORKSPACE_ACCESS,
                f"features[{index}].workspace_id does not match the observation",
            )
            require(
                feature.observation_id == self.observation.observation_id,
                SemanticErrorCode.UNKNOWN_REFERENCE,
                f"features[{index}].observation_id does not match the observation",
            )
            require(
                feature.feature_name not in seen_features,
                SemanticErrorCode.DUPLICATE_ID,
                f"features[{index}].feature_name is duplicated: "
                f"{feature.feature_name!r}",
            )
            seen_features.add(feature.feature_name)


def normalise_text(value: str) -> str:
    """Deterministic normal form: NFC, stripped, whitespace-collapsed, casefolded."""
    require(
        isinstance(value, str),
        SemanticErrorCode.INVALID_FIELD,
        "value must be a string",
    )
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def observation_equivalence_signature(
    observation: SemanticObservation, normalization_version: str
) -> str:
    """A workspace-scoped, versioned equivalence signature for `observation`.

    Binds `workspace_id`, `kind`, `value_kind`, the normalised form,
    `proposed_semantic_role`, `generation`, `rule_version` and
    `normalization_version` -- so equivalence never crosses workspaces and
    normalisation-rule changes do not silently collide old and new signatures.
    """
    _require_id("normalization_version", normalization_version)
    return content_digest(
        {
            "workspace_id": observation.workspace_id,
            "kind": observation.kind,
            "value_kind": observation.value_kind.value,
            "normalized_form": normalise_text(observation.normalized_form),
            "proposed_semantic_role": observation.proposed_semantic_role,
            "generation": observation.generation.value,
            "rule_version": observation.rule_version,
            "normalization_version": normalization_version,
        }
    )


def _instant_payload(instant: TemporalInstant) -> dict[str, Any]:
    return instant_payload(instant)


def observation_payload(observation: SemanticObservation) -> dict[str, Any]:
    """Canonical, JSON-safe semantic content of `observation`."""
    return {
        "observation_id": observation.observation_id,
        "workspace_id": observation.workspace_id,
        "kind": observation.kind,
        "value_kind": observation.value_kind.value,
        "original_form": observation.original_form,
        "normalized_form": observation.normalized_form,
        "proposed_semantic_role": observation.proposed_semantic_role,
        "classification": observation.classification.value,
        "generation": observation.generation.value,
        "status": observation.status.value,
        "recorded_at": _instant_payload(observation.recorded_at),
        "source_time": (
            None
            if observation.source_time is None
            else _instant_payload(observation.source_time)
        ),
        "supersedes_observation_id": observation.supersedes_observation_id,
        "rule_version": observation.rule_version,
        "schema_version": observation.schema_version,
    }


def observation_digest(observation: SemanticObservation) -> str:
    return content_digest(observation_payload(observation))


def observation_feature_payload(feature: ObservationFeature) -> dict[str, Any]:
    """Canonical, JSON-safe semantic content of `feature`."""
    return {
        "workspace_id": feature.workspace_id,
        "observation_id": feature.observation_id,
        "feature_name": feature.feature_name,
        "value": feature.value,
        "policy_version": feature.policy_version,
        "calculation_version": feature.calculation_version,
        "schema_version": feature.schema_version,
    }


def observation_feature_digest(feature: ObservationFeature) -> str:
    return content_digest(observation_feature_payload(feature))


def _evidence_link_sort_key(link: EvidenceLink) -> tuple[str, str, str]:
    return (link.evidence_id, link.span_id or "", link.role.value)


def _feature_sort_key(feature: ObservationFeature) -> str:
    return feature.feature_name


def observation_bundle_payload(bundle: ObservationBundle) -> dict[str, Any]:
    """Canonical, JSON-safe semantic content of `bundle`.

    Evidence links and features are sorted by stable semantic keys so
    input-order variations digest identically.
    """
    sorted_links = sorted(bundle.evidence_links, key=_evidence_link_sort_key)
    sorted_features = sorted(bundle.features, key=_feature_sort_key)
    return {
        "observation": observation_payload(bundle.observation),
        "evidence_links": [evidence_link_payload(link) for link in sorted_links],
        "features": [
            observation_feature_payload(feature) for feature in sorted_features
        ],
    }


def observation_bundle_digest(bundle: ObservationBundle) -> str:
    return content_digest(observation_bundle_payload(bundle))


def effective_observation_classification(
    workspace_floor: Classification,
    source: Classification,
    observation: SemanticObservation,
    linked_evidence: Sequence[Classification] = (),
) -> Classification:
    """The effective classification for `observation`, folding in linked evidence."""
    return effective_classification(
        workspace_floor,
        source,
        observation.classification,
        tuple(linked_evidence),
    )


__all__ = [
    "OBSERVATION_SCHEMA_VERSION",
    "ObservationBundle",
    "ObservationFeature",
    "ObservationGeneration",
    "ObservationStatus",
    "ObservationValueKind",
    "SemanticObservation",
    "effective_observation_classification",
    "normalise_text",
    "observation_bundle_digest",
    "observation_bundle_payload",
    "observation_digest",
    "observation_equivalence_signature",
    "observation_feature_digest",
    "observation_feature_payload",
    "observation_payload",
]
