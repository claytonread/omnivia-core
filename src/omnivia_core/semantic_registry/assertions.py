"""Phase 2 governed-knowledge contracts: assertions, evidence links, lifecycle.

A `KnowledgeAssertion` is the unit of governed knowledge: a subject/predicate
bound to either an entity or a literal object, with a temporal validity
interval resolved through the shared `temporal` contract (never re-derived
here) and a classification. Supersession and retraction are separate
append-only records, never in-place mutation of the original assertion.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from omnivia_core.semantic_registry.canonical import content_digest
from omnivia_core.semantic_registry.errors import (
    SemanticErrorCode,
    SemanticValidationError,
    require,
)
from omnivia_core.semantic_registry.evidence import (
    Classification,
    EvidenceSupportRole,
)
from omnivia_core.semantic_registry.temporal import (
    EffectiveValidInterval,
    EndBoundaryState,
    TemporalInstant,
    resolve_effective_valid_interval,
)

ASSERTION_SCHEMA_VERSION = "1.0.0"


def _require_id(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and value.strip() != "",
        SemanticErrorCode.MISSING_FIELD,
        f"{field_name} is required",
    )


def _require_unit_interval(field_name: str, value: float) -> None:
    require(
        isinstance(value, (int, float)) and not isinstance(value, bool),
        SemanticErrorCode.INVALID_FIELD,
        f"{field_name} must be a number",
    )
    require(
        0.0 <= float(value) <= 1.0,
        SemanticErrorCode.INVALID_FIELD,
        f"{field_name} must be between 0 and 1",
    )


def _require_instant(field_name: str, value: Any) -> None:
    require(
        isinstance(value, TemporalInstant),
        SemanticErrorCode.INVALID_FIELD,
        f"{field_name} must be a TemporalInstant",
    )


def _validate_json_safe(field_name: str, value: Any) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return
    if isinstance(value, float):
        require(
            math.isfinite(value),
            SemanticErrorCode.UNSUPPORTED_VALUE,
            f"{field_name} must be a finite number",
        )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            require(
                isinstance(key, str),
                SemanticErrorCode.UNSUPPORTED_VALUE,
                f"{field_name} mapping keys must be strings",
            )
            _validate_json_safe(f"{field_name}.{key}", item)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_safe(f"{field_name}[{index}]", item)
        return
    raise SemanticValidationError(
        SemanticErrorCode.UNSUPPORTED_VALUE,
        f"{field_name} must be a JSON-canonicalizable scalar, list, or mapping",
    )


def _freeze_json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json_safe(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_safe(item) for item in value)
    return value


class KnowledgeObjectKind(str, Enum):
    """Whether an assertion's object is a graph entity or a literal value."""

    ENTITY = "entity"
    LITERAL = "literal"


@dataclass(frozen=True, slots=True)
class KnowledgeAssertion:
    """One immutable, governed subject/predicate/object assertion.

    `valid_to_state` fixes which end-of-validity fields may be populated:
    `STATED` requires `valid_to` and forbids `attested_to`; `UNKNOWN` requires
    `attested_to` and forbids `valid_to`; `OPEN` forbids both. The effective
    validity interval is always resolved through
    `temporal.resolve_effective_valid_interval` -- this type never computes it
    itself, only asserts that resolution succeeds.
    """

    assertion_id: str
    workspace_id: str
    subject_id: str
    predicate_element_id: str
    model_version_id: str
    object_kind: KnowledgeObjectKind
    confidence: float
    attested_from: TemporalInstant
    valid_to_state: EndBoundaryState
    recorded_at: TemporalInstant
    classification: Classification
    object_id: str | None = None
    literal_value: Any = None
    valid_from: TemporalInstant | None = None
    valid_to: TemporalInstant | None = None
    attested_to: TemporalInstant | None = None
    recorded_until: TemporalInstant | None = None
    schema_version: str = field(init=False, default=ASSERTION_SCHEMA_VERSION)

    def __post_init__(self) -> None:
        _require_id("assertion_id", self.assertion_id)
        _require_id("workspace_id", self.workspace_id)
        _require_id("subject_id", self.subject_id)
        _require_id("predicate_element_id", self.predicate_element_id)
        _require_id("model_version_id", self.model_version_id)
        require(
            isinstance(self.object_kind, KnowledgeObjectKind),
            SemanticErrorCode.UNSUPPORTED_VALUE,
            "object_kind must be a KnowledgeObjectKind",
        )
        require(
            isinstance(self.classification, Classification),
            SemanticErrorCode.UNSUPPORTED_VALUE,
            "classification must be a Classification",
        )
        _require_unit_interval("confidence", self.confidence)

        if self.object_kind is KnowledgeObjectKind.ENTITY:
            _require_id("object_id", self.object_id or "")
            require(
                self.literal_value is None,
                SemanticErrorCode.INVALID_FIELD,
                "object_kind entity must not carry a literal_value",
            )
        else:
            require(
                self.object_id is None,
                SemanticErrorCode.INVALID_FIELD,
                "object_kind literal must not carry an object_id",
            )
            _validate_json_safe("literal_value", self.literal_value)
            object.__setattr__(
                self, "literal_value", _freeze_json_safe(self.literal_value)
            )

        _require_instant("attested_from", self.attested_from)
        _require_instant("recorded_at", self.recorded_at)
        require(
            isinstance(self.valid_to_state, EndBoundaryState),
            SemanticErrorCode.UNSUPPORTED_VALUE,
            "valid_to_state must be an EndBoundaryState",
        )
        if self.valid_from is not None:
            _require_instant("valid_from", self.valid_from)
        if self.recorded_until is not None:
            _require_instant("recorded_until", self.recorded_until)

        if self.valid_to_state is EndBoundaryState.STATED:
            _require_instant("valid_to", self.valid_to)
            require(
                self.attested_to is None,
                SemanticErrorCode.INVALID_FIELD,
                "valid_to_state stated must not carry an attested_to",
            )
        elif self.valid_to_state is EndBoundaryState.UNKNOWN:
            _require_instant("attested_to", self.attested_to)
            require(
                self.valid_to is None,
                SemanticErrorCode.INVALID_FIELD,
                "valid_to_state unknown must not carry a valid_to",
            )
        else:
            require(
                self.valid_to is None and self.attested_to is None,
                SemanticErrorCode.INVALID_FIELD,
                "valid_to_state open must not carry a valid_to or attested_to",
            )

        resolve_effective_valid_interval(
            valid_from=self.valid_from,
            attested_from=self.attested_from,
            end_state=self.valid_to_state,
            valid_to=self.valid_to,
            attested_to=self.attested_to,
        )

        if self.recorded_until is not None:
            require(
                self.recorded_until.value > self.recorded_at.value,
                SemanticErrorCode.INVALID_FIELD,
                "recorded_until must be strictly after recorded_at",
            )


@dataclass(frozen=True, slots=True)
class AssertionEvidence:
    """One evidence link supporting or contradicting a `KnowledgeAssertion`."""

    workspace_id: str
    assertion_id: str
    evidence_id: str
    role: EvidenceSupportRole
    confidence: float
    span_id: str | None = None

    def __post_init__(self) -> None:
        _require_id("workspace_id", self.workspace_id)
        _require_id("assertion_id", self.assertion_id)
        _require_id("evidence_id", self.evidence_id)
        require(
            isinstance(self.role, EvidenceSupportRole),
            SemanticErrorCode.UNSUPPORTED_VALUE,
            "role must be an EvidenceSupportRole",
        )
        _require_unit_interval("confidence", self.confidence)
        if self.span_id is not None:
            _require_id("span_id", self.span_id)


@dataclass(frozen=True, slots=True)
class AssertionSupersession:
    """One append-only record replacing a prior assertion with a successor."""

    workspace_id: str
    supersession_id: str
    prior_assertion_id: str
    successor_assertion_id: str
    reason_code: str
    decision_id: str
    recorded_at: TemporalInstant

    def __post_init__(self) -> None:
        _require_id("workspace_id", self.workspace_id)
        _require_id("supersession_id", self.supersession_id)
        _require_id("prior_assertion_id", self.prior_assertion_id)
        _require_id("successor_assertion_id", self.successor_assertion_id)
        _require_id("reason_code", self.reason_code)
        _require_id("decision_id", self.decision_id)
        _require_instant("recorded_at", self.recorded_at)
        require(
            self.prior_assertion_id != self.successor_assertion_id,
            SemanticErrorCode.INVALID_FIELD,
            "an assertion cannot supersede itself",
        )


@dataclass(frozen=True, slots=True)
class AssertionRetraction:
    """One append-only record retracting an assertion for cause."""

    workspace_id: str
    retraction_id: str
    assertion_id: str
    retracted_at: TemporalInstant
    reason_code: str
    policy_version: str
    actor_principal_id: str

    def __post_init__(self) -> None:
        _require_id("workspace_id", self.workspace_id)
        _require_id("retraction_id", self.retraction_id)
        _require_id("assertion_id", self.assertion_id)
        _require_instant("retracted_at", self.retracted_at)
        _require_id("reason_code", self.reason_code)
        _require_id("policy_version", self.policy_version)
        _require_id("actor_principal_id", self.actor_principal_id)


def assertion_effective_interval(assertion: KnowledgeAssertion) -> EffectiveValidInterval:
    """The effective validity interval for `assertion`, via the shared resolver only."""
    return resolve_effective_valid_interval(
        valid_from=assertion.valid_from,
        attested_from=assertion.attested_from,
        end_state=assertion.valid_to_state,
        valid_to=assertion.valid_to,
        attested_to=assertion.attested_to,
    )


def _instant_payload(instant: TemporalInstant | None) -> dict[str, Any] | None:
    if instant is None:
        return None
    return {
        "value": instant.value.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "precision": instant.precision.value,
        "provenance": instant.provenance.value,
    }


def assertion_payload(assertion: KnowledgeAssertion) -> dict[str, Any]:
    """Canonical, JSON-safe semantic content of `assertion` -- no evidence content."""
    return {
        "assertion_id": assertion.assertion_id,
        "workspace_id": assertion.workspace_id,
        "subject_id": assertion.subject_id,
        "predicate_element_id": assertion.predicate_element_id,
        "model_version_id": assertion.model_version_id,
        "object_kind": assertion.object_kind.value,
        "object_id": assertion.object_id,
        "literal_value": assertion.literal_value,
        "confidence": assertion.confidence,
        "valid_from": _instant_payload(assertion.valid_from),
        "valid_to_state": assertion.valid_to_state.value,
        "valid_to": _instant_payload(assertion.valid_to),
        "attested_from": _instant_payload(assertion.attested_from),
        "attested_to": _instant_payload(assertion.attested_to),
        "recorded_at": _instant_payload(assertion.recorded_at),
        "recorded_until": _instant_payload(assertion.recorded_until),
        "classification": assertion.classification.value,
        "schema_version": assertion.schema_version,
    }


def assertion_digest(assertion: KnowledgeAssertion) -> str:
    return content_digest(assertion_payload(assertion))


def assertion_evidence_payload(evidence: AssertionEvidence) -> dict[str, Any]:
    """Canonical, JSON-safe semantic content of `evidence` -- link only."""
    return {
        "workspace_id": evidence.workspace_id,
        "assertion_id": evidence.assertion_id,
        "evidence_id": evidence.evidence_id,
        "role": evidence.role.value,
        "confidence": evidence.confidence,
        "span_id": evidence.span_id,
    }


def assertion_evidence_digest(evidence: AssertionEvidence) -> str:
    return content_digest(assertion_evidence_payload(evidence))


def assertion_supersession_payload(
    supersession: AssertionSupersession,
) -> dict[str, Any]:
    """Canonical, JSON-safe semantic content of `supersession`."""
    return {
        "workspace_id": supersession.workspace_id,
        "supersession_id": supersession.supersession_id,
        "prior_assertion_id": supersession.prior_assertion_id,
        "successor_assertion_id": supersession.successor_assertion_id,
        "reason_code": supersession.reason_code,
        "decision_id": supersession.decision_id,
        "recorded_at": _instant_payload(supersession.recorded_at),
    }


def assertion_supersession_digest(supersession: AssertionSupersession) -> str:
    return content_digest(assertion_supersession_payload(supersession))


def assertion_retraction_payload(retraction: AssertionRetraction) -> dict[str, Any]:
    """Canonical, JSON-safe semantic content of `retraction`."""
    return {
        "workspace_id": retraction.workspace_id,
        "retraction_id": retraction.retraction_id,
        "assertion_id": retraction.assertion_id,
        "retracted_at": _instant_payload(retraction.retracted_at),
        "reason_code": retraction.reason_code,
        "policy_version": retraction.policy_version,
        "actor_principal_id": retraction.actor_principal_id,
    }


def assertion_retraction_digest(retraction: AssertionRetraction) -> str:
    return content_digest(assertion_retraction_payload(retraction))


def _single_workspace_id(
    assertions: Sequence[KnowledgeAssertion],
    supersessions: Sequence[AssertionSupersession],
    retractions: Sequence[AssertionRetraction],
) -> str:
    workspace_ids = (
        {a.workspace_id for a in assertions}
        | {s.workspace_id for s in supersessions}
        | {r.workspace_id for r in retractions}
    )
    require(
        len(workspace_ids) == 1,
        SemanticErrorCode.CROSS_WORKSPACE_ACCESS,
        "assertion history records must all share one workspace_id",
    )
    return next(iter(workspace_ids))


def _require_unique(field_name: str, ids: Sequence[str]) -> None:
    require(
        len(ids) == len(set(ids)),
        SemanticErrorCode.DUPLICATE_ID,
        f"{field_name} contains duplicate IDs",
    )


def assertion_history_payload(
    assertions: Sequence[KnowledgeAssertion],
    supersessions: Sequence[AssertionSupersession] = (),
    retractions: Sequence[AssertionRetraction] = (),
) -> dict[str, Any]:
    """The canonical, JSON-safe append-only history of one assertion lineage.

    Each record set is sorted by its own recorded time then ID, so
    input-order variations digest identically. Rejects duplicate IDs within
    each record set and any record whose `workspace_id` does not match the
    rest of the history.
    """
    require(
        len(assertions) > 0,
        SemanticErrorCode.MISSING_FIELD,
        "assertion history requires at least one assertion",
    )
    workspace_id = _single_workspace_id(assertions, supersessions, retractions)

    _require_unique(
        "assertions", [assertion.assertion_id for assertion in assertions]
    )
    _require_unique(
        "supersessions",
        [supersession.supersession_id for supersession in supersessions],
    )
    _require_unique(
        "retractions", [retraction.retraction_id for retraction in retractions]
    )

    sorted_assertions = sorted(
        assertions, key=lambda a: (a.recorded_at.value, a.assertion_id)
    )
    sorted_supersessions = sorted(
        supersessions, key=lambda s: (s.recorded_at.value, s.supersession_id)
    )
    sorted_retractions = sorted(
        retractions, key=lambda r: (r.retracted_at.value, r.retraction_id)
    )

    return {
        "workspace_id": workspace_id,
        "assertions": [assertion_payload(a) for a in sorted_assertions],
        "supersessions": [
            assertion_supersession_payload(s) for s in sorted_supersessions
        ],
        "retractions": [assertion_retraction_payload(r) for r in sorted_retractions],
    }


def assertion_history_digest(
    assertions: Sequence[KnowledgeAssertion],
    supersessions: Sequence[AssertionSupersession] = (),
    retractions: Sequence[AssertionRetraction] = (),
) -> str:
    return content_digest(
        assertion_history_payload(assertions, supersessions, retractions)
    )


def current_assertion_id(
    assertion_id: str,
    supersessions: Sequence[AssertionSupersession],
    known_assertion_ids: Sequence[str] | None = None,
) -> str:
    """Follow the supersession chain from `assertion_id` to its current successor.

    Deterministic and read-only: it never mutates any prior assertion, only
    walks `AssertionSupersession` edges. Rejects an unknown reference (a
    supersession naming an assertion outside `known_assertion_ids`, when
    given), a branch (two supersessions sharing one `prior_assertion_id`), and
    a cycle.
    """
    if known_assertion_ids is not None:
        known = set(known_assertion_ids)
        require(
            assertion_id in known,
            SemanticErrorCode.UNKNOWN_REFERENCE,
            f"unknown assertion_id: {assertion_id!r}",
        )
        for supersession in supersessions:
            require(
                supersession.prior_assertion_id in known,
                SemanticErrorCode.UNKNOWN_REFERENCE,
                f"supersession {supersession.supersession_id!r} references "
                "an unknown prior_assertion_id",
            )
            require(
                supersession.successor_assertion_id in known,
                SemanticErrorCode.UNKNOWN_REFERENCE,
                f"supersession {supersession.supersession_id!r} references "
                "an unknown successor_assertion_id",
            )

    successor_by_prior: dict[str, str] = {}
    for supersession in supersessions:
        prior = supersession.prior_assertion_id
        require(
            prior not in successor_by_prior,
            SemanticErrorCode.INVALID_FIELD,
            f"assertion {prior!r} has more than one direct successor",
        )
        successor_by_prior[prior] = supersession.successor_assertion_id

    current = assertion_id
    visited = {current}
    while current in successor_by_prior:
        current = successor_by_prior[current]
        require(
            current not in visited,
            SemanticErrorCode.CYCLIC_DEPENDENCY,
            "supersession chain contains a cycle",
        )
        visited.add(current)
    return current


__all__ = [
    "ASSERTION_SCHEMA_VERSION",
    "AssertionEvidence",
    "AssertionRetraction",
    "AssertionSupersession",
    "KnowledgeAssertion",
    "KnowledgeObjectKind",
    "assertion_digest",
    "assertion_effective_interval",
    "assertion_evidence_digest",
    "assertion_evidence_payload",
    "assertion_history_digest",
    "assertion_history_payload",
    "assertion_payload",
    "assertion_retraction_digest",
    "assertion_retraction_payload",
    "assertion_supersession_digest",
    "assertion_supersession_payload",
    "current_assertion_id",
]
