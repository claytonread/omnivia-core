"""Programme B7: deterministic, replayable candidate aggregation and suppression.

Pure functions over the existing Phase 2 types -- no storage, no service, no
network, no LLM. Every rule here is bound to an explicit
`aggregation_version`/`normalization_version` and fails closed on a version it
does not implement, so a later rule revision can never silently reinterpret a
historical candidate (decision record section 6, "Rule authority").

Aggregation output is always a `draft` `SemanticCandidate`: nothing in this
module approves, converts or publishes anything (decision record section 7).

Normalisation, observation equivalence, evidence dedup, weight banding and
suppression activity already exist in
:mod:`omnivia_core.semantic_registry.observations`,
:mod:`omnivia_core.semantic_registry.evidence` and
:mod:`omnivia_core.semantic_registry.candidates`; this module binds them to a
rule version and composes them into the candidate pipeline rather than
restating them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from omnivia_core.semantic_registry.candidates import (
    AGGREGATION_RULE_VERSION,
    NORMALIZATION_RULE_VERSION,
    CandidateContribution,
    CandidateFeatureSummary,
    CandidateReconsideration,
    CandidateState,
    CandidateSuppression,
    ContributionRole,
    ReconsiderationReason,
    SemanticCandidate,
    aggregate_candidate_features,
    candidate_equivalence_signature,
    suppression_active,
)
from omnivia_core.semantic_registry.canonical import content_digest
from omnivia_core.semantic_registry.errors import (
    SemanticErrorCode,
    SemanticValidationError,
    require,
)
from omnivia_core.semantic_registry.evidence import (
    EvidenceItem,
    EvidenceSupportRole,
    evidence_dedup_signature,
)
from omnivia_core.semantic_registry.models import ModelVersion
from omnivia_core.semantic_registry.observations import (
    ObservationBundle,
    ObservationStatus,
    SemanticObservation,
    normalise_text,
    observation_digest,
    observation_equivalence_signature,
)
from omnivia_core.semantic_registry.operations import ChangeOperation
from omnivia_core.semantic_registry.temporal import TemporalInstant

#: Versions this module actually implements. An unknown version is rejected
#: rather than approximated with the newest rules.
_TEXT_NORMALIZERS: Mapping[str, Callable[[str], str]] = {
    NORMALIZATION_RULE_VERSION: normalise_text,
}
_SUPPORTED_AGGREGATION_VERSIONS = frozenset({AGGREGATION_RULE_VERSION})

#: One deduplicated evidence item is worth `_WEIGHT_PER_EVIDENCE`; each
#: *additional* independent source beyond the first is worth the same again, so
#: corroboration across sources outweighs volume from a single source. Capped at
#: `CandidateContribution`'s own maximum.
_WEIGHT_PER_EVIDENCE = 500
_WEIGHT_PER_EXTRA_SOURCE = 500
_MAX_CONTRIBUTION_WEIGHT = 10000


def _require_id(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and value.strip() != "",
        SemanticErrorCode.MISSING_FIELD,
        f"{field_name} is required",
    )


def require_supported_versions(
    aggregation_version: str, normalization_version: str
) -> None:
    """Fail closed unless both rule versions are ones this module implements."""
    _require_id("aggregation_version", aggregation_version)
    _require_id("normalization_version", normalization_version)
    require(
        aggregation_version in _SUPPORTED_AGGREGATION_VERSIONS,
        SemanticErrorCode.UNSUPPORTED_VALUE,
        f"unsupported aggregation_version {aggregation_version!r}",
    )
    require(
        normalization_version in _TEXT_NORMALIZERS,
        SemanticErrorCode.UNSUPPORTED_VALUE,
        f"unsupported normalization_version {normalization_version!r}",
    )


def normalize_text(
    value: str, normalization_version: str = NORMALIZATION_RULE_VERSION
) -> str:
    """The versioned text normal form (NFC, trimmed, collapsed, casefolded)."""
    _require_id("normalization_version", normalization_version)
    normalizer = _TEXT_NORMALIZERS.get(normalization_version)
    if normalizer is None:
        raise SemanticValidationError(
            SemanticErrorCode.UNSUPPORTED_VALUE,
            f"unsupported normalization_version {normalization_version!r}",
        )
    return normalizer(value)


def normalize_identifier(
    value: str, normalization_version: str = NORMALIZATION_RULE_VERSION
) -> str:
    """The versioned identifier normal form: text normalisation, hyphen-joined.

    `_` and `-` are treated as word separators, so `Org_Unit`, `org unit` and
    `org--unit` all normalise to `org-unit`. An identifier that normalises to
    nothing is rejected rather than silently becoming an empty key.
    """
    _require_id("value", value)
    words = normalize_text(
        value.replace("_", " ").replace("-", " "), normalization_version
    ).split()
    require(
        len(words) > 0,
        SemanticErrorCode.INVALID_FIELD,
        "identifier must not normalise to an empty string",
    )
    return "-".join(words)


def _dedup_representatives(
    items: Sequence[EvidenceItem], rule_version: str
) -> dict[str, str]:
    """Map every `evidence_id` to its dedup class representative.

    Exact duplicates share the workspace-scoped
    `(workspace_id, source_id, source version, content_digest)` dedup
    signature; the lowest `evidence_id` in a class represents it, so the choice
    does not depend on input order.
    """
    by_signature: dict[str, str] = {}
    for item in items:
        signature = evidence_dedup_signature(item, rule_version)
        current = by_signature.get(signature)
        if current is None or item.evidence_id < current:
            by_signature[signature] = item.evidence_id
    return {
        item.evidence_id: by_signature[evidence_dedup_signature(item, rule_version)]
        for item in items
    }


def deduplicate_evidence(
    items: Sequence[EvidenceItem], rule_version: str = AGGREGATION_RULE_VERSION
) -> tuple[EvidenceItem, ...]:
    """`items` reduced to one representative per dedup class, sorted by id."""
    _require_id("rule_version", rule_version)
    representatives = set(_dedup_representatives(items, rule_version).values())
    return tuple(
        sorted(
            (item for item in items if item.evidence_id in representatives),
            key=lambda item: item.evidence_id,
        )
    )


def evidence_snapshot_digest(
    items: Sequence[EvidenceItem], rule_version: str = AGGREGATION_RULE_VERSION
) -> str:
    """A digest over the deduplicated evidence set backing a candidate.

    Built from dedup signatures only -- never content -- so the snapshot can be
    logged and compared without disclosing protected material.
    """
    _require_id("rule_version", rule_version)
    signatures = sorted(
        {evidence_dedup_signature(item, rule_version) for item in items}
    )
    return content_digest({"rule_version": rule_version, "signatures": signatures})


def group_equivalent_observations(
    observations: Sequence[SemanticObservation],
    normalization_version: str = NORMALIZATION_RULE_VERSION,
) -> tuple[tuple[str, tuple[SemanticObservation, ...]], ...]:
    """`observations` grouped into equivalence classes, deterministically ordered.

    Groups are keyed by the existing versioned observation equivalence
    signature, sorted by signature; members are sorted by `observation_id`, so
    input order never changes the result.
    """
    _require_id("normalization_version", normalization_version)
    require(
        normalization_version in _TEXT_NORMALIZERS,
        SemanticErrorCode.UNSUPPORTED_VALUE,
        f"unsupported normalization_version {normalization_version!r}",
    )
    groups: dict[str, list[SemanticObservation]] = {}
    for observation in observations:
        require(
            isinstance(observation, SemanticObservation),
            SemanticErrorCode.INVALID_FIELD,
            "every observation must be a SemanticObservation",
        )
        signature = observation_equivalence_signature(
            observation, normalization_version
        )
        groups.setdefault(signature, []).append(observation)
    return tuple(
        (
            signature,
            tuple(sorted(members, key=lambda o: o.observation_id)),
        )
        for signature, members in sorted(groups.items())
    )


@dataclass(frozen=True, slots=True)
class CandidateEvidenceFeatures:
    """Support/contradiction aggregates plus evidence independence counts.

    `summary` is the existing role-keyed count/weight/band aggregate;
    `independent_evidence_count` and `independent_source_count` are the
    deduplicated breadth behind it -- five observations citing one document
    from one source are not five independent pieces of evidence.
    """

    summary: CandidateFeatureSummary
    independent_evidence_count: int
    independent_source_count: int
    normalization_version: str


@dataclass(frozen=True, slots=True)
class CandidateAggregation:
    """One aggregation run's full, replayable output."""

    candidate: SemanticCandidate
    contributions: tuple[CandidateContribution, ...]
    features: CandidateEvidenceFeatures
    equivalence_signature: str


def _is_novel(base: ModelVersion, operation: ChangeOperation) -> bool:
    """Whether `operation` concerns an element the base version does not have."""
    target = operation.target_element_id or operation.proposed_element_id
    if target is None:
        return False
    return target not in {element.element_id for element in base.elements}


def build_candidate(
    *,
    candidate_id: str,
    candidate_kind: str,
    workspace_id: str,
    base: ModelVersion,
    proposed_operation: ChangeOperation,
    bundles: Sequence[ObservationBundle],
    evidence: Mapping[str, EvidenceItem],
    created_at: TemporalInstant,
    aggregation_version: str = AGGREGATION_RULE_VERSION,
    normalization_version: str = NORMALIZATION_RULE_VERSION,
) -> CandidateAggregation:
    """Aggregate `bundles` into one `draft` candidate against `base`.

    `base` is the explicit current model version the change is proposed
    against: it supplies `target_model_id`/`base_version_id` and decides
    novelty, so the same observations aggregated against a different base
    produce a different candidate rather than a silently rebased one.

    Contradicting observations are kept as `contradict` contributions, never
    dropped or folded into support (decision record section 6). Retracted
    observations are excluded -- a retracted statement must not back a
    candidate.
    """
    require_supported_versions(aggregation_version, normalization_version)
    _require_id("candidate_id", candidate_id)
    _require_id("workspace_id", workspace_id)
    require(
        isinstance(base, ModelVersion),
        SemanticErrorCode.INVALID_FIELD,
        "base must be a ModelVersion",
    )
    require(
        isinstance(proposed_operation, ChangeOperation),
        SemanticErrorCode.INVALID_FIELD,
        "proposed_operation must be a ChangeOperation",
    )
    require(
        isinstance(created_at, TemporalInstant),
        SemanticErrorCode.INVALID_FIELD,
        "created_at must be a TemporalInstant",
    )
    kind = normalize_identifier(candidate_kind, normalization_version)

    for key, item in evidence.items():
        require(
            isinstance(item, EvidenceItem),
            SemanticErrorCode.INVALID_FIELD,
            "every evidence value must be an EvidenceItem",
        )
        require(
            item.evidence_id == key,
            SemanticErrorCode.UNKNOWN_REFERENCE,
            f"evidence key {key!r} does not match its item",
        )
        require(
            item.workspace_id == workspace_id,
            SemanticErrorCode.CROSS_WORKSPACE_ACCESS,
            f"evidence {key!r} belongs to another workspace",
        )

    active: list[ObservationBundle] = []
    referenced: list[EvidenceItem] = []
    seen_evidence: set[str] = set()
    for bundle in bundles:
        require(
            isinstance(bundle, ObservationBundle),
            SemanticErrorCode.INVALID_FIELD,
            "every bundle must be an ObservationBundle",
        )
        require(
            bundle.observation.workspace_id == workspace_id,
            SemanticErrorCode.CROSS_WORKSPACE_ACCESS,
            "every observation must belong to the candidate's workspace",
        )
        if bundle.observation.status is ObservationStatus.RETRACTED:
            continue
        active.append(bundle)
        for link in bundle.evidence_links:
            require(
                link.evidence_id in evidence,
                SemanticErrorCode.UNKNOWN_REFERENCE,
                f"evidence {link.evidence_id!r} is not in the supplied evidence set",
            )
            if link.evidence_id not in seen_evidence:
                seen_evidence.add(link.evidence_id)
                referenced.append(evidence[link.evidence_id])
    require(
        len(active) > 0,
        SemanticErrorCode.MISSING_FIELD,
        "a candidate requires at least one non-retracted observation",
    )

    representative = _dedup_representatives(referenced, aggregation_version)
    novel = _is_novel(base, proposed_operation)
    contributions: list[CandidateContribution] = []
    all_evidence: set[str] = set()
    all_sources: set[str] = set()
    for bundle in active:
        evidence_ids = {
            representative[link.evidence_id] for link in bundle.evidence_links
        }
        source_ids = {evidence[item_id].source.source_id for item_id in evidence_ids}
        all_evidence |= evidence_ids
        all_sources |= source_ids
        if any(
            link.role is EvidenceSupportRole.CONTRADICT
            for link in bundle.evidence_links
        ):
            role = ContributionRole.CONTRADICT
        elif novel:
            role = ContributionRole.NOVELTY
        else:
            role = ContributionRole.SUPPORT
        contributions.append(
            CandidateContribution(
                workspace_id=workspace_id,
                candidate_id=candidate_id,
                observation_id=bundle.observation.observation_id,
                role=role,
                weight=min(
                    _MAX_CONTRIBUTION_WEIGHT,
                    _WEIGHT_PER_EVIDENCE * len(evidence_ids)
                    + _WEIGHT_PER_EXTRA_SOURCE * (len(source_ids) - 1),
                ),
                observation_digest=observation_digest(bundle.observation),
            )
        )
    contributions.sort(key=lambda contribution: contribution.observation_id)

    summary = aggregate_candidate_features(tuple(contributions), aggregation_version)
    snapshot = evidence_snapshot_digest(
        [evidence[item_id] for item_id in sorted(all_evidence)], aggregation_version
    )
    candidate = SemanticCandidate(
        candidate_id=candidate_id,
        workspace_id=workspace_id,
        candidate_kind=kind,
        target_model_id=base.model_id,
        proposed_operation=proposed_operation,
        support_band=summary.support_band,
        novelty_band=summary.novelty_band,
        risk_band=summary.risk_band,
        state=CandidateState.DRAFT,
        aggregation_version=aggregation_version,
        normalization_version=normalization_version,
        base_version_id=base.model_version_id,
        evidence_snapshot_digest=snapshot,
        created_at=created_at,
    )
    return CandidateAggregation(
        candidate=candidate,
        contributions=tuple(contributions),
        features=CandidateEvidenceFeatures(
            summary=summary,
            independent_evidence_count=len(all_evidence),
            independent_source_count=len(all_sources),
            normalization_version=normalization_version,
        ),
        equivalence_signature=candidate_equivalence_signature(
            workspace_id,
            kind,
            base.model_id,
            proposed_operation,
            normalization_version,
            aggregation_version,
        ),
    )


@dataclass(frozen=True, slots=True)
class SuppressionDecision:
    """Whether an aggregated candidate is suppressed, and why it stopped being.

    `reason` is set only when a matching suppression *ended*; it is never a
    guess -- `human_override` is never inferred here, only recorded explicitly
    by a `CandidateReconsideration`.
    """

    suppressed: bool
    suppression_id: str | None = None
    reason: ReconsiderationReason | None = None


def decide_suppression(
    equivalence_signature: str,
    suppressions: Sequence[CandidateSuppression],
    *,
    at: TemporalInstant,
    evidence_snapshot_digest: str,
    aggregation_version: str = AGGREGATION_RULE_VERSION,
) -> SuppressionDecision:
    """Whether a candidate with `equivalence_signature` must stay suppressed.

    A suppression ends only on its recorded expiry, a changed evidence
    snapshot, or a changed aggregation rule version -- the conditions
    `suppression_active` already enforces. Matching suppressions are examined
    in `suppression_id` order so the reported id and reason do not depend on
    input order.
    """
    _require_id("equivalence_signature", equivalence_signature)
    _require_id("evidence_snapshot_digest", evidence_snapshot_digest)
    _require_id("aggregation_version", aggregation_version)
    matching = sorted(
        (
            suppression
            for suppression in suppressions
            if suppression.equivalence_signature == equivalence_signature
        ),
        key=lambda suppression: suppression.suppression_id,
    )
    ended: SuppressionDecision | None = None
    for suppression in matching:
        activity = suppression_active(
            suppression, at, evidence_snapshot_digest, aggregation_version
        )
        if activity.active:
            return SuppressionDecision(
                suppressed=True, suppression_id=suppression.suppression_id
            )
        if ended is None:
            ended = SuppressionDecision(
                suppressed=False,
                suppression_id=suppression.suppression_id,
                reason=activity.reason,
            )
    return ended or SuppressionDecision(suppressed=False)


def build_reconsideration(
    *,
    reconsideration_id: str,
    suppression: CandidateSuppression,
    decision: SuppressionDecision,
    recorded_at: TemporalInstant,
    evidence_snapshot_digest: str,
    aggregation_version: str = AGGREGATION_RULE_VERSION,
) -> CandidateReconsideration:
    """The receipt for a suppression that `decision` found had ended.

    Only the three machine-determinable reasons are receiptable here;
    `human_override` needs an actor and is recorded directly by the operator
    path, never derived from aggregation.
    """
    _require_id("reconsideration_id", reconsideration_id)
    require(
        isinstance(suppression, CandidateSuppression),
        SemanticErrorCode.INVALID_FIELD,
        "suppression must be a CandidateSuppression",
    )
    require(
        not decision.suppressed and decision.reason is not None,
        SemanticErrorCode.INVALID_FIELD,
        "decision must record an ended suppression",
    )
    require(
        decision.suppression_id == suppression.suppression_id,
        SemanticErrorCode.UNKNOWN_REFERENCE,
        "decision does not refer to this suppression",
    )
    require(
        decision.reason is not ReconsiderationReason.HUMAN_OVERRIDE,
        SemanticErrorCode.UNSUPPORTED_VALUE,
        "human_override is never derived from aggregation",
    )
    if decision.reason is ReconsiderationReason.NEW_EVIDENCE:
        return CandidateReconsideration(
            workspace_id=suppression.workspace_id,
            reconsideration_id=reconsideration_id,
            suppression_id=suppression.suppression_id,
            recorded_at=recorded_at,
            reason=ReconsiderationReason.NEW_EVIDENCE,
            previous_evidence_digest=suppression.evidence_snapshot_digest,
            new_evidence_digest=evidence_snapshot_digest,
        )
    if decision.reason is ReconsiderationReason.RULE_VERSION_CHANGED:
        return CandidateReconsideration(
            workspace_id=suppression.workspace_id,
            reconsideration_id=reconsideration_id,
            suppression_id=suppression.suppression_id,
            recorded_at=recorded_at,
            reason=ReconsiderationReason.RULE_VERSION_CHANGED,
            previous_rule_version=suppression.aggregation_version,
            new_rule_version=aggregation_version,
        )
    return CandidateReconsideration(
        workspace_id=suppression.workspace_id,
        reconsideration_id=reconsideration_id,
        suppression_id=suppression.suppression_id,
        recorded_at=recorded_at,
        reason=ReconsiderationReason.EXPIRED,
    )


__all__ = [
    "CandidateAggregation",
    "CandidateEvidenceFeatures",
    "SuppressionDecision",
    "build_candidate",
    "build_reconsideration",
    "decide_suppression",
    "deduplicate_evidence",
    "evidence_snapshot_digest",
    "group_equivalent_observations",
    "normalize_identifier",
    "normalize_text",
    "require_supported_versions",
]
