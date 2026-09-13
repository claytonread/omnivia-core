"""KI-01: `OrganisationalPosition` -- an evidence-backed, governed profile (spec 6.2).

A position is a *profile over* an existing governed-record `content` payload, not
a new canonical database entity (spec 5.1): `organisational_position_from_content`
validates and lifts a governed record's `content` mapping into this immutable
type, and `organisational_position_to_content` is its exact inverse, so a caller
storing a position keeps using its existing governed-record write path.

Auth/capability context is never accepted from `content` (spec 5.2): there is no
`acting_as`/`granted_by`/permission field on this type, and none is read from the
record content by the conversion function.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from omnivia_core.governed_knowledge.applicability import (
    ApplicabilityExpression,
)
from omnivia_core.governed_knowledge.content_limits import (
    enforce_ov_cj1_content_limit,
)
from omnivia_core.governed_knowledge.errors import (
    GovernedKnowledgeErrorCode,
    require,
)
from omnivia_core.governed_knowledge.wire import (
    decode_expression,
    decode_opt_instant,
    decode_opt_interval,
    encode_expression,
    encode_opt_instant,
    encode_opt_interval,
    require_str,
    require_str_list,
)
from omnivia_core.semantic_registry.evidence import Classification
from omnivia_core.semantic_registry.temporal import (
    EffectiveValidInterval,
    TemporalInstant,
)

POSITION_PROFILE_VERSION = "governed-knowledge-position-v1"


def _require_id(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and value.strip() != "",
        GovernedKnowledgeErrorCode.MISSING_FIELD,
        f"{field_name} is required",
    )


def _require_text(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and value.strip() != "",
        GovernedKnowledgeErrorCode.MISSING_FIELD,
        f"{field_name} is required",
    )


class PositionLifecycleState(str, Enum):
    """The logical position lifecycle (spec 6.6): `proposed -> review -> admitted
    -> superseded/retracted`, mapped onto the existing knowledge lifecycle by the
    runtime binding this profile onto -- this type only records which state a
    given profile snapshot claims to be in.
    """

    PROPOSED = "proposed"
    REVIEW = "review"
    ADMITTED = "admitted"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


@dataclass(frozen=True, slots=True)
class PositionException:
    """One explicit exclusion exception and its effect (spec 6.2: "Exceptions").

    No "more specific wins" rule is implied by ordering: every exception in a
    position's `exceptions` tuple is independently evaluated, and any one of
    them being conclusively true excludes the position (spec 6.3/6.5).
    """

    exception_id: str
    condition: ApplicabilityExpression
    rationale: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_id("exception_id", self.exception_id)
        require(
            isinstance(self.condition, ApplicabilityExpression),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "PositionException.condition must be an ApplicabilityExpression",
        )
        _require_text("rationale", self.rationale)
        for index, ref in enumerate(self.evidence_refs):
            _require_id(f"evidence_refs[{index}]", ref)


@dataclass(frozen=True, slots=True)
class OrganisationalPosition:
    """One immutable, versioned governed-knowledge position (spec 6.2).

    Groups map directly onto spec 6.2's table: identity, meaning, model binding,
    scope, applicability, exceptions, authority, evidence, time, relations,
    freshness and security. Self-declared expertise is never sufficient
    authority -- `approved_by_reference` must point at an existing
    approval/admission record, never a free-text claim.
    """

    # Identity
    position_id: str
    version_ref: str
    profile_version: str = field(init=False, default=POSITION_PROFILE_VERSION)

    # Meaning
    title: str = ""
    statement: str = ""
    domain_refs: tuple[str, ...] = ()
    position_kind: str = ""

    # Model binding
    semantic_model_version_ref: str = ""
    referenced_element_ids: tuple[str, ...] = ()

    # Scope
    workspace_id: str = ""
    scope_entity_refs: tuple[str, ...] = ()

    # Applicability
    required_conditions: ApplicabilityExpression | None = None
    declared_required_facts: tuple[str, ...] = ()
    exceptions: tuple[PositionException, ...] = ()
    evaluator_version: str = ""

    # Authority
    owning_domain: str = ""
    approved_by_reference: str | None = None

    # Evidence
    supporting_evidence_refs: tuple[str, ...] = ()
    contradicting_evidence_refs: tuple[str, ...] = ()

    # Time
    valid_interval: EffectiveValidInterval | None = None
    recorded_at: TemporalInstant | None = None
    review_due_at: TemporalInstant | None = None

    # Relations
    replaces_position_ref: str | None = None
    superseded_by_ref: str | None = None
    depends_on_position_refs: tuple[str, ...] = ()
    contradicts_position_refs: tuple[str, ...] = ()

    # Freshness
    last_substantive_review_at: TemporalInstant | None = None
    review_policy: str | None = None

    # Security
    classification: Classification = Classification.INTERNAL
    retention_class: str = ""

    lifecycle_state: PositionLifecycleState = PositionLifecycleState.PROPOSED

    def __post_init__(self) -> None:
        _require_id("position_id", self.position_id)
        _require_id("version_ref", self.version_ref)
        _require_text("title", self.title)
        _require_text("statement", self.statement)
        _require_id("workspace_id", self.workspace_id)
        _require_id("owning_domain", self.owning_domain)
        _require_id("retention_class", self.retention_class)
        require(
            isinstance(self.classification, Classification),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "classification must be a Classification",
        )
        require(
            isinstance(self.lifecycle_state, PositionLifecycleState),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "lifecycle_state must be a PositionLifecycleState",
        )
        if self.required_conditions is not None:
            require(
                isinstance(self.required_conditions, ApplicabilityExpression),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "required_conditions must be an ApplicabilityExpression",
            )
            _require_id("evaluator_version", self.evaluator_version)
        for index, exception in enumerate(self.exceptions):
            require(
                isinstance(exception, PositionException),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"exceptions[{index}] must be a PositionException",
            )
        if self.valid_interval is not None:
            require(
                isinstance(self.valid_interval, EffectiveValidInterval),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "valid_interval must be an EffectiveValidInterval",
            )
        for name, value in (
            ("recorded_at", self.recorded_at),
            ("review_due_at", self.review_due_at),
            ("last_substantive_review_at", self.last_substantive_review_at),
        ):
            if value is not None:
                require(
                    isinstance(value, TemporalInstant),
                    GovernedKnowledgeErrorCode.INVALID_FIELD,
                    f"{name} must be a TemporalInstant",
                )
        if self.replaces_position_ref is not None:
            _require_id("replaces_position_ref", self.replaces_position_ref)
        if self.superseded_by_ref is not None:
            _require_id("superseded_by_ref", self.superseded_by_ref)

    @property
    def is_review_overdue(self) -> bool:
        """Whether this position is overdue for review *as of `recorded_at`*.

        Distinct from expiry/retraction/supersession (spec 6.5): a strict
        profile may choose to block overdue material, another may include it
        with a warning -- this property only reports the state, never a policy
        decision. Requires both `review_due_at` and `recorded_at` to be set;
        without them, overdue status is not established and this returns
        `False` rather than guessing.
        """
        if self.review_due_at is None or self.recorded_at is None:
            return False
        return self.recorded_at.value >= self.review_due_at.value


def _evidence_ref_payload(refs: Sequence[str]) -> list[str]:
    return list(refs)


def organisational_position_to_content(
    position: OrganisationalPosition,
) -> dict[str, Any]:
    """Project `position` to a governed-record `content` payload.

    Exact inverse of :func:`organisational_position_from_content`: round-tripping
    through both functions reproduces the same `OrganisationalPosition`.
    """
    content: dict[str, Any] = {
        "position_id": position.position_id,
        "version_ref": position.version_ref,
        "profile_version": position.profile_version,
        "title": position.title,
        "statement": position.statement,
        "domain_refs": _evidence_ref_payload(position.domain_refs),
        "position_kind": position.position_kind,
        "semantic_model_version_ref": position.semantic_model_version_ref,
        "referenced_element_ids": _evidence_ref_payload(
            position.referenced_element_ids
        ),
        "workspace_id": position.workspace_id,
        "scope_entity_refs": _evidence_ref_payload(position.scope_entity_refs),
        "evaluator_version": position.evaluator_version,
        "declared_required_facts": _evidence_ref_payload(
            position.declared_required_facts
        ),
        "owning_domain": position.owning_domain,
        "approved_by_reference": position.approved_by_reference,
        "supporting_evidence_refs": _evidence_ref_payload(
            position.supporting_evidence_refs
        ),
        "contradicting_evidence_refs": _evidence_ref_payload(
            position.contradicting_evidence_refs
        ),
        "replaces_position_ref": position.replaces_position_ref,
        "superseded_by_ref": position.superseded_by_ref,
        "depends_on_position_refs": _evidence_ref_payload(
            position.depends_on_position_refs
        ),
        "contradicts_position_refs": _evidence_ref_payload(
            position.contradicts_position_refs
        ),
        "review_policy": position.review_policy,
        "classification": position.classification.value,
        "retention_class": position.retention_class,
        "required_conditions": (
            None
            if position.required_conditions is None
            else encode_expression(position.required_conditions)
        ),
        "exceptions": [
            {
                "exception_id": item.exception_id,
                "condition": encode_expression(item.condition),
                "rationale": item.rationale,
                "evidence_refs": list(item.evidence_refs),
            }
            for item in position.exceptions
        ],
        "valid_interval": encode_opt_interval(position.valid_interval),
        "recorded_at": encode_opt_instant(position.recorded_at),
        "review_due_at": encode_opt_instant(position.review_due_at),
        "last_substantive_review_at": encode_opt_instant(
            position.last_substantive_review_at
        ),
    }
    enforce_ov_cj1_content_limit(content)
    return content


def organisational_position_from_content(
    *,
    position_id: str,
    version_ref: str,
    content: Mapping[str, Any],
    trusted_lifecycle_state: PositionLifecycleState = PositionLifecycleState.PROPOSED,
) -> OrganisationalPosition:
    """Validate and lift a governed record's `content` into an `OrganisationalPosition`.

    `content` is treated as untrusted structural data: malicious text or
    frontmatter inside its string fields is inert -- it is read only as opaque
    string values, never interpreted, executed or templated. The typed
    Applicability and temporal data are decoded from the same versioned content.
    Governance state is deliberately supplied separately by the trusted outer
    governed-record boundary; content alone can never self-declare admission.
    """
    require(
        isinstance(content, Mapping),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "content must be a mapping",
    )

    def _text(key: str, default: str = "") -> str:
        value = content.get(key, default)
        require(
            isinstance(value, str),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            f"content[{key!r}] must be a string",
        )
        assert isinstance(value, str)
        return value

    def _opt_text(key: str) -> str | None:
        value = content.get(key)
        if value is None:
            return None
        require(
            isinstance(value, str),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            f"content[{key!r}] must be a string or null",
        )
        assert isinstance(value, str)
        return value

    def _str_tuple(key: str) -> tuple[str, ...]:
        value = content.get(key, ())
        require(
            isinstance(value, (list, tuple)),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            f"content[{key!r}] must be a list of strings",
        )
        for index, item in enumerate(value):
            require(
                isinstance(item, str),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"content[{key!r}][{index}] must be a string",
            )
        return tuple(value)

    profile_version = _text("profile_version")
    require(
        profile_version == POSITION_PROFILE_VERSION,
        GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
        "content profile_version is unsupported",
    )
    classification_raw = content.get("classification", Classification.INTERNAL.value)
    require(
        isinstance(classification_raw, str),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "content['classification'] must be a string",
    )
    require(
        classification_raw in {member.value for member in Classification},
        GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
        "content['classification'] is not a recognised Classification",
    )
    classification = Classification(classification_raw)
    require(
        isinstance(trusted_lifecycle_state, PositionLifecycleState),
        GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
        "trusted_lifecycle_state must be a PositionLifecycleState",
    )

    required_raw = content.get("required_conditions")
    required_conditions = (
        None if required_raw is None else decode_expression(required_raw)
    )
    exceptions_raw = content.get("exceptions", [])
    require(
        isinstance(exceptions_raw, list),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "content exceptions must be a list",
    )
    parsed_exceptions: list[PositionException] = []
    for raw in exceptions_raw:
        require(
            isinstance(raw, Mapping),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "each content exception must be a mapping",
        )
        evidence_raw = raw.get("evidence_refs", [])
        require(
            isinstance(evidence_raw, list),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "exception evidence_refs must be a list",
        )
        parsed_exceptions.append(
            PositionException(
                exception_id=require_str(
                    raw.get("exception_id"), "exception exception_id"
                ),
                condition=decode_expression(raw.get("condition")),
                rationale=require_str(raw.get("rationale"), "exception rationale"),
                evidence_refs=tuple(
                    require_str_list(evidence_raw, "exception evidence_refs")
                ),
            )
        )

    position = OrganisationalPosition(
        position_id=position_id,
        version_ref=version_ref,
        title=_text("title"),
        statement=_text("statement"),
        domain_refs=_str_tuple("domain_refs"),
        position_kind=_text("position_kind"),
        semantic_model_version_ref=_text("semantic_model_version_ref"),
        referenced_element_ids=_str_tuple("referenced_element_ids"),
        workspace_id=_text("workspace_id"),
        scope_entity_refs=_str_tuple("scope_entity_refs"),
        required_conditions=required_conditions,
        declared_required_facts=_str_tuple("declared_required_facts"),
        exceptions=tuple(parsed_exceptions),
        evaluator_version=_text("evaluator_version"),
        owning_domain=_text("owning_domain"),
        approved_by_reference=_opt_text("approved_by_reference"),
        supporting_evidence_refs=_str_tuple("supporting_evidence_refs"),
        contradicting_evidence_refs=_str_tuple("contradicting_evidence_refs"),
        valid_interval=decode_opt_interval(content.get("valid_interval")),
        recorded_at=decode_opt_instant(content.get("recorded_at")),
        review_due_at=decode_opt_instant(content.get("review_due_at")),
        replaces_position_ref=_opt_text("replaces_position_ref"),
        superseded_by_ref=_opt_text("superseded_by_ref"),
        depends_on_position_refs=_str_tuple("depends_on_position_refs"),
        contradicts_position_refs=_str_tuple("contradicts_position_refs"),
        last_substantive_review_at=decode_opt_instant(
            content.get("last_substantive_review_at")
        ),
        review_policy=_opt_text("review_policy"),
        classification=classification,
        retention_class=_text("retention_class"),
        lifecycle_state=trusted_lifecycle_state,
    )
    enforce_ov_cj1_content_limit(organisational_position_to_content(position))
    return position


__all__ = [
    "POSITION_PROFILE_VERSION",
    "OrganisationalPosition",
    "PositionException",
    "PositionLifecycleState",
    "organisational_position_from_content",
    "organisational_position_to_content",
]
