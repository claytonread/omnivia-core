"""The 18 typed change-operation kinds (spec section 11).

One `ChangeOperation` dataclass carries the shape every kind shares --
identity, target/proposed stable ID, preconditions, `before`/`after` state,
dependency edges and evidence references -- because that shape really is
uniform across kinds. What differs per kind is which keys `after` (and
`before`, for a `Change*` kind) must carry, which :data:`_REQUIRED_AFTER_KEYS`
states and `__post_init__` enforces. `after`/`before` stay recursively
validated JSON-value mappings rather than 19 bespoke dataclasses because their
content is genuinely heterogeneous (a cardinality bound, a hierarchy edge, an
action parameter schema digest, ...) -- the one case the task brief calls out
as acceptable for a mapping at an authoritative boundary.

`rationale` is carried but excluded from the semantic content digest (spec:
"Human-authored rationale kept outside the semantic content digest"); see
:mod:`omnivia_core.semantic_registry.canonical`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any

from omnivia_core.contracts.v1.canonical_json import canonicalize
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.semantic_registry.errors import (
    SemanticErrorCode,
    SemanticValidationError,
    require,
)


class OperationKind(str, Enum):
    """The 18 initial change-operation types (spec section 11)."""

    ADD_CONCEPT = "AddConcept"
    ADD_PROPERTY = "AddProperty"
    ADD_RELATIONSHIP = "AddRelationship"
    ADD_CONSTRAINT = "AddConstraint"
    ADD_ALIAS = "AddAlias"
    ADD_VOCABULARY_MEMBER = "AddVocabularyMember"
    ADD_ACTION_TYPE = "AddActionType"
    CHANGE_LABEL = "ChangeLabel"
    CHANGE_DESCRIPTION = "ChangeDescription"
    CHANGE_HIERARCHY = "ChangeHierarchy"
    CHANGE_DOMAIN_RANGE = "ChangeDomainRange"
    CHANGE_CARDINALITY = "ChangeCardinality"
    CHANGE_EQUIVALENCE = "ChangeEquivalence"
    CHANGE_DISJOINTNESS = "ChangeDisjointness"
    CHANGE_ACTION_CONTRACT = "ChangeActionContract"
    DEPRECATE_ELEMENT = "DeprecateElement"
    REPLACE_ELEMENT = "ReplaceElement"
    REMOVE_ELEMENT = "RemoveElement"


#: Kinds that mint a new stable ID rather than target an existing one.
_ADD_KINDS = frozenset(
    {
        OperationKind.ADD_CONCEPT,
        OperationKind.ADD_PROPERTY,
        OperationKind.ADD_RELATIONSHIP,
        OperationKind.ADD_CONSTRAINT,
        OperationKind.ADD_ALIAS,
        OperationKind.ADD_VOCABULARY_MEMBER,
        OperationKind.ADD_ACTION_TYPE,
    }
)

#: Every `Add*` kind's set is all the fields its typed element constructor
#: requires (spec 11: an added element must be fully constructible); every
#: other kind's set is the field(s) that kind changes. All-of, except the two
#: kinds in `_ANY_OF_KINDS` below, where a partial change is legitimate.
_REQUIRED_AFTER_KEYS: Mapping[OperationKind, frozenset[str]] = {
    OperationKind.ADD_CONCEPT: frozenset({"label"}),
    OperationKind.ADD_PROPERTY: frozenset({"label", "value_kind"}),
    OperationKind.ADD_RELATIONSHIP: frozenset(
        {"label", "subject_concept_id", "object_concept_id"}
    ),
    OperationKind.ADD_CONSTRAINT: frozenset({"target_element_id", "constraint_kind"}),
    OperationKind.ADD_ALIAS: frozenset({"target_element_id", "value"}),
    OperationKind.ADD_VOCABULARY_MEMBER: frozenset(
        {"vocabulary_element_id", "value"}
    ),
    OperationKind.ADD_ACTION_TYPE: frozenset(
        {"label", "subject_concept_id", "parameter_schema_digest"}
    ),
    OperationKind.CHANGE_LABEL: frozenset({"label"}),
    OperationKind.CHANGE_DESCRIPTION: frozenset({"description"}),
    OperationKind.CHANGE_HIERARCHY: frozenset({"parent_concept_ids"}),
    OperationKind.CHANGE_DOMAIN_RANGE: frozenset({"domain_id", "range_id"}),
    OperationKind.CHANGE_CARDINALITY: frozenset({"min_cardinality", "max_cardinality"}),
    OperationKind.CHANGE_EQUIVALENCE: frozenset({"equivalent_element_id"}),
    OperationKind.CHANGE_DISJOINTNESS: frozenset({"disjoint_with_element_id"}),
    OperationKind.CHANGE_ACTION_CONTRACT: frozenset({"parameter_schema_digest"}),
    OperationKind.DEPRECATE_ELEMENT: frozenset({"reason_code"}),
    OperationKind.REPLACE_ELEMENT: frozenset({"replacement_element_id"}),
}
#: `RemoveElement` is the one kind whose `after` must be empty: there is no
#: "after state" for a removed element.
_EMPTY_AFTER_KINDS = frozenset({OperationKind.REMOVE_ELEMENT})

#: The two kinds where a partial change is legitimate -- a domain/range or a
#: cardinality edit may touch only one side -- so `after` needs at least one
#: of the named keys, not all of them.
_ANY_OF_KINDS = frozenset(
    {OperationKind.CHANGE_DOMAIN_RANGE, OperationKind.CHANGE_CARDINALITY}
)


def _validate_json_mapping(
    field_name: str, value: Mapping[str, Any]
) -> Mapping[str, Any]:
    require(
        isinstance(value, Mapping),
        SemanticErrorCode.INVALID_FIELD,
        f"{field_name} must be a mapping",
    )
    try:
        canonicalize(dict(value))
    except ContractSemanticError as error:
        raise SemanticValidationError(
            SemanticErrorCode.UNSUPPORTED_VALUE, f"{field_name}: {error}"
        ) from error
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class ChangeOperation:
    """One typed, normalised change against an explicit base version (spec 11)."""

    operation_id: str
    kind: OperationKind
    after: Mapping[str, Any]
    target_element_id: str | None = None
    proposed_element_id: str | None = None
    base_payload_digest: str | None = None
    before: Mapping[str, Any] | None = None
    depends_on_operation_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    #: Human-authored, excluded from the semantic content digest.
    rationale: str | None = None
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        require(
            isinstance(self.operation_id, str) and self.operation_id.strip() != "",
            SemanticErrorCode.MISSING_FIELD,
            "operation_id is required",
        )
        require(
            isinstance(self.kind, OperationKind),
            SemanticErrorCode.UNKNOWN_OPERATION_KIND,
            f"{self.kind!r} is not a known operation kind",
        )
        if self.kind in _ADD_KINDS:
            require(
                self.proposed_element_id is not None and self.target_element_id is None,
                SemanticErrorCode.INVALID_FIELD,
                f"{self.kind.value} must set proposed_element_id and not target_element_id",
            )
        else:
            require(
                self.target_element_id is not None and self.proposed_element_id is None,
                SemanticErrorCode.INVALID_FIELD,
                f"{self.kind.value} must set target_element_id and not proposed_element_id",
            )
        object.__setattr__(self, "after", _validate_json_mapping("after", self.after))
        if self.before is not None:
            object.__setattr__(
                self, "before", _validate_json_mapping("before", self.before)
            )
        if self.kind in _EMPTY_AFTER_KINDS:
            require(
                not self.after,
                SemanticErrorCode.INVALID_FIELD,
                f"{self.kind.value} must carry an empty after payload",
            )
        else:
            required = _REQUIRED_AFTER_KEYS[self.kind]
            if self.kind in _ANY_OF_KINDS:
                require(
                    required & self.after.keys(),
                    SemanticErrorCode.MISSING_FIELD,
                    f"{self.kind.value} requires at least one of "
                    f"{sorted(required)} in after",
                )
            else:
                require(
                    required <= self.after.keys(),
                    SemanticErrorCode.MISSING_FIELD,
                    f"{self.kind.value} requires all of {sorted(required)} in after",
                )
        require(
            len(self.depends_on_operation_ids)
            == len(set(self.depends_on_operation_ids)),
            SemanticErrorCode.DUPLICATE_ID,
            "depends_on_operation_ids must not repeat an id",
        )
        require(
            self.operation_id not in self.depends_on_operation_ids,
            SemanticErrorCode.CYCLIC_DEPENDENCY,
            "an operation cannot depend on itself",
        )


def _new_operation(
    *,
    operation_id: str,
    kind: OperationKind,
    after: Mapping[str, Any],
    target_element_id: str | None = None,
    proposed_element_id: str | None = None,
    before: Mapping[str, Any] | None = None,
    base_payload_digest: str | None = None,
    depends_on_operation_ids: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
    rationale: str | None = None,
) -> ChangeOperation:
    return ChangeOperation(
        operation_id=operation_id,
        kind=kind,
        after=after,
        target_element_id=target_element_id,
        proposed_element_id=proposed_element_id,
        before=before,
        base_payload_digest=base_payload_digest,
        depends_on_operation_ids=depends_on_operation_ids,
        evidence_refs=evidence_refs,
        rationale=rationale,
    )


def add_concept(
    operation_id: str, proposed_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.ADD_CONCEPT,
        proposed_element_id=proposed_element_id,
        after=after,
        **kw,
    )


def add_property(
    operation_id: str, proposed_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.ADD_PROPERTY,
        proposed_element_id=proposed_element_id,
        after=after,
        **kw,
    )


def add_relationship(
    operation_id: str, proposed_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.ADD_RELATIONSHIP,
        proposed_element_id=proposed_element_id,
        after=after,
        **kw,
    )


def add_constraint(
    operation_id: str, proposed_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.ADD_CONSTRAINT,
        proposed_element_id=proposed_element_id,
        after=after,
        **kw,
    )


def add_alias(
    operation_id: str, proposed_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.ADD_ALIAS,
        proposed_element_id=proposed_element_id,
        after=after,
        **kw,
    )


def add_vocabulary_member(
    operation_id: str, proposed_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.ADD_VOCABULARY_MEMBER,
        proposed_element_id=proposed_element_id,
        after=after,
        **kw,
    )


def add_action_type(
    operation_id: str, proposed_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.ADD_ACTION_TYPE,
        proposed_element_id=proposed_element_id,
        after=after,
        **kw,
    )


def change_label(
    operation_id: str, target_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.CHANGE_LABEL,
        target_element_id=target_element_id,
        after=after,
        **kw,
    )


def change_description(
    operation_id: str, target_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.CHANGE_DESCRIPTION,
        target_element_id=target_element_id,
        after=after,
        **kw,
    )


def change_hierarchy(
    operation_id: str, target_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.CHANGE_HIERARCHY,
        target_element_id=target_element_id,
        after=after,
        **kw,
    )


def change_domain_range(
    operation_id: str, target_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.CHANGE_DOMAIN_RANGE,
        target_element_id=target_element_id,
        after=after,
        **kw,
    )


def change_cardinality(
    operation_id: str, target_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.CHANGE_CARDINALITY,
        target_element_id=target_element_id,
        after=after,
        **kw,
    )


def change_equivalence(
    operation_id: str, target_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.CHANGE_EQUIVALENCE,
        target_element_id=target_element_id,
        after=after,
        **kw,
    )


def change_disjointness(
    operation_id: str, target_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.CHANGE_DISJOINTNESS,
        target_element_id=target_element_id,
        after=after,
        **kw,
    )


def change_action_contract(
    operation_id: str, target_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.CHANGE_ACTION_CONTRACT,
        target_element_id=target_element_id,
        after=after,
        **kw,
    )


def deprecate_element(
    operation_id: str, target_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.DEPRECATE_ELEMENT,
        target_element_id=target_element_id,
        after=after,
        **kw,
    )


def replace_element(
    operation_id: str, target_element_id: str, after: Mapping[str, Any], **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.REPLACE_ELEMENT,
        target_element_id=target_element_id,
        after=after,
        **kw,
    )


def remove_element(
    operation_id: str, target_element_id: str, **kw: Any
) -> ChangeOperation:
    return _new_operation(
        operation_id=operation_id,
        kind=OperationKind.REMOVE_ELEMENT,
        target_element_id=target_element_id,
        after={},
        **kw,
    )


__all__ = [
    "ChangeOperation",
    "OperationKind",
    "add_action_type",
    "add_alias",
    "add_concept",
    "add_constraint",
    "add_property",
    "add_relationship",
    "add_vocabulary_member",
    "change_action_contract",
    "change_cardinality",
    "change_description",
    "change_disjointness",
    "change_domain_range",
    "change_equivalence",
    "change_hierarchy",
    "change_label",
    "deprecate_element",
    "remove_element",
    "replace_element",
]
