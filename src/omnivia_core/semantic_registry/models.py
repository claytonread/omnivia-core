"""Semantic models, immutable model versions and semantic elements.

Element kinds are explicit typed dataclasses (:class:`Concept`, :class:`Property`,
:class:`Relationship`, :class:`Constraint`, :class:`Alias`, :class:`VocabularyMember`,
:class:`ActionType`) rather than one generic bag, per the standards subset in
spec section 7.6 and the entity table in section 10.3. `Constraint.parameters`
is the one deliberately heterogeneous field: constraint parameters differ by
`constraint_kind` (a pattern string, a cardinality bound, a datatype IRI), so
it is a recursively validated JSON-value mapping rather than a fixed shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, TypeAlias

from omnivia_core.semantic_registry.errors import SemanticErrorCode, require


class LifecycleState(str, Enum):
    """Governance state of one semantic element."""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    REPLACED = "replaced"
    REMOVED = "removed"


class PropertyValueKind(str, Enum):
    """Whether a property holds a literal value or references another concept."""

    DATA = "data"
    OBJECT = "object"


class ConstraintKind(str, Enum):
    """The bounded constraint vocabulary from the standards subset (spec 7.6)."""

    REQUIRED = "required"
    OPTIONAL = "optional"
    MIN_CARDINALITY = "min_cardinality"
    MAX_CARDINALITY = "max_cardinality"
    DATATYPE = "datatype"
    PATTERN = "pattern"
    DISJOINTNESS = "disjointness"
    EQUIVALENCE = "equivalence"


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


def _no_duplicates(field_name: str, values: tuple[str, ...]) -> None:
    require(
        len(values) == len(set(values)),
        SemanticErrorCode.DUPLICATE_ID,
        f"{field_name} must not repeat an id",
    )


@dataclass(frozen=True, slots=True)
class Concept:
    """A class in the semantic model: `semantic_concepts` (spec 10.3)."""

    element_id: str
    label: str
    description: str | None = None
    parent_concept_ids: tuple[str, ...] = ()
    abstract: bool = False
    classifications: tuple[str, ...] = ()
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE

    def __post_init__(self) -> None:
        _require_id("element_id", self.element_id)
        _require_text("label", self.label)
        _no_duplicates("parent_concept_ids", self.parent_concept_ids)
        require(
            self.element_id not in self.parent_concept_ids,
            SemanticErrorCode.INVALID_FIELD,
            "a concept cannot be its own parent",
        )


@dataclass(frozen=True, slots=True)
class Property:
    """A data or object property: `semantic_properties` (spec 10.3)."""

    element_id: str
    label: str
    value_kind: PropertyValueKind
    domain_id: str | None = None
    range_id: str | None = None
    datatype: str | None = None
    min_cardinality: int | None = None
    max_cardinality: int | None = None
    description: str | None = None
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE

    def __post_init__(self) -> None:
        _require_id("element_id", self.element_id)
        _require_text("label", self.label)
        for name, bound in (
            ("min_cardinality", self.min_cardinality),
            ("max_cardinality", self.max_cardinality),
        ):
            require(
                bound is None or (isinstance(bound, int) and bound >= 0),
                SemanticErrorCode.INVALID_FIELD,
                f"{name} must be a non-negative integer",
            )
        if self.min_cardinality is not None and self.max_cardinality is not None:
            require(
                self.min_cardinality <= self.max_cardinality,
                SemanticErrorCode.INVALID_FIELD,
                "min_cardinality must not exceed max_cardinality",
            )


@dataclass(frozen=True, slots=True)
class Relationship:
    """An object relationship: `semantic_relationships` (spec 10.3)."""

    element_id: str
    label: str
    subject_concept_id: str
    object_concept_id: str
    inverse_id: str | None = None
    characteristics: tuple[str, ...] = ()
    description: str | None = None
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE

    def __post_init__(self) -> None:
        _require_id("element_id", self.element_id)
        _require_text("label", self.label)
        _require_id("subject_concept_id", self.subject_concept_id)
        _require_id("object_concept_id", self.object_concept_id)
        _no_duplicates("characteristics", self.characteristics)


JsonMapping: TypeAlias = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Constraint:
    """A constraint on a target element: `semantic_constraints` (spec 10.3)."""

    element_id: str
    target_element_id: str
    constraint_kind: ConstraintKind
    parameters: JsonMapping = field(default_factory=dict)
    severity: str = "error"
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE

    def __post_init__(self) -> None:
        _require_id("element_id", self.element_id)
        _require_id("target_element_id", self.target_element_id)
        require(
            isinstance(self.parameters, Mapping),
            SemanticErrorCode.INVALID_FIELD,
            "parameters must be a mapping",
        )
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class Alias:
    """An alternate label for a target element: `semantic_aliases` (spec 10.3)."""

    element_id: str
    target_element_id: str
    value: str
    locale: str | None = None
    scope: str = "workspace"

    def __post_init__(self) -> None:
        _require_id("element_id", self.element_id)
        _require_id("target_element_id", self.target_element_id)
        _require_text("value", self.value)


@dataclass(frozen=True, slots=True)
class VocabularyMember:
    """One member of a controlled vocabulary: `semantic_vocabularies` (spec 10.3).

    `order_key` carries the vocabulary's own display/enumeration order, which
    is semantically meaningful and therefore never re-sorted by canonicalization
    (spec 7.5: "Collections sorted by defined semantic keys unless order is
    semantically meaningful").
    """

    element_id: str
    vocabulary_element_id: str
    value: str
    order_key: int = 0

    def __post_init__(self) -> None:
        _require_id("element_id", self.element_id)
        _require_id("vocabulary_element_id", self.vocabulary_element_id)
        _require_text("value", self.value)


@dataclass(frozen=True, slots=True)
class ActionType:
    """A typed organisational action: `action_types` (spec 10.10)."""

    element_id: str
    label: str
    subject_concept_id: str
    parameter_schema_digest: str
    target_concept_id: str | None = None
    result_schema_digest: str | None = None
    risk_class: str = "standard"
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE

    def __post_init__(self) -> None:
        _require_id("element_id", self.element_id)
        _require_text("label", self.label)
        _require_id("subject_concept_id", self.subject_concept_id)
        _require_id("parameter_schema_digest", self.parameter_schema_digest)


SemanticElement: TypeAlias = (
    Concept
    | Property
    | Relationship
    | Constraint
    | Alias
    | VocabularyMember
    | ActionType
)


@dataclass(frozen=True, slots=True)
class SemanticModel:
    """One Organisational Model: `semantic_models` (spec 10.2)."""

    model_id: str
    workspace_id: str
    name: str
    purpose: str = ""
    meta_model_version: str = "1.0.0"

    def __post_init__(self) -> None:
        _require_id("model_id", self.model_id)
        _require_id("workspace_id", self.workspace_id)
        _require_text("name", self.name)
        _require_id("meta_model_version", self.meta_model_version)


@dataclass(frozen=True, slots=True)
class ModelVersion:
    """One immutable published semantic version: `semantic_model_versions` (spec 10.2).

    `content_digest` is carried here as data (its computation lives in
    :mod:`omnivia_core.semantic_registry.canonical`) so a version can be
    reconstructed from storage and its digest re-verified without recomputing
    it from scratch every time.
    """

    model_version_id: str
    model_id: str
    version_sequence: int
    version_label: str
    content_digest: str
    parent_version_ids: tuple[str, ...] = ()
    meta_model_version: str = "1.0.0"
    elements: tuple[SemanticElement, ...] = ()

    def __post_init__(self) -> None:
        _require_id("model_version_id", self.model_version_id)
        _require_id("model_id", self.model_id)
        require(
            isinstance(self.version_sequence, int) and self.version_sequence >= 1,
            SemanticErrorCode.INVALID_FIELD,
            "version_sequence must be a positive integer",
        )
        parts = self.version_label.split(".")
        require(
            len(parts) == 3 and all(part.isdigit() for part in parts),
            SemanticErrorCode.INVALID_FIELD,
            "version_label must be major.minor.patch",
        )
        _require_id("content_digest", self.content_digest)
        require(
            self.model_version_id not in self.parent_version_ids,
            SemanticErrorCode.INVALID_FIELD,
            "a version cannot be its own parent",
        )
        _no_duplicates("parent_version_ids", self.parent_version_ids)
        element_ids = tuple(element.element_id for element in self.elements)
        _no_duplicates("elements", element_ids)


__all__ = [
    "ActionType",
    "Alias",
    "Concept",
    "Constraint",
    "ConstraintKind",
    "JsonMapping",
    "LifecycleState",
    "ModelVersion",
    "Property",
    "PropertyValueKind",
    "Relationship",
    "SemanticElement",
    "SemanticModel",
    "VocabularyMember",
]
