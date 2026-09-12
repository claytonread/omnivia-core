"""Phase 2 candidate/aggregation/suppression contracts.

A `SemanticCandidate` is a proposed `ChangeOperation` plus the aggregated
support/novelty/risk evidence for it -- never an automatically approved or
published state (spec: candidates only ever carry `draft`/`active`/
`proposed`/`rejected`/`suppressed`/`reconsidered`; publication is a separate,
authoritative act outside this module). `CandidateSuppression` and
`CandidateReconsideration` record why an equivalent candidate should not be
re-raised, and under what explicit, evidenced condition it may be.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from omnivia_core.semantic_registry.canonical import (
    content_digest,
    operation_payload,
)
from omnivia_core.semantic_registry.errors import SemanticErrorCode, require
from omnivia_core.semantic_registry.operations import ChangeOperation
from omnivia_core.semantic_registry.temporal import TemporalInstant

CANDIDATE_SCHEMA_VERSION = "1.0.0"
AGGREGATION_RULE_VERSION = "candidate-aggregation-v1"
NORMALIZATION_RULE_VERSION = "observation-normalization-v1"
SUPPRESSION_RULE_VERSION = "candidate-suppression-v1"

#: Weight-band thresholds (inclusive upper bounds) shared by support/novelty.
_BAND_LOW_MAX = 999
_BAND_MEDIUM_MAX = 4999

#: Weight-band thresholds (inclusive upper bounds) for risk, keyed on the
#: contradicting weight -- any contradiction at all moves a candidate off
#: "low" risk.
_RISK_STANDARD_MAX = 999
_RISK_HIGH_MAX = 4999


def _require_id(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and value.strip() != "",
        SemanticErrorCode.MISSING_FIELD,
        f"{field_name} is required",
    )


def _require_enum(field_name: str, value: Any, enum_type: type[Enum]) -> None:
    require(
        isinstance(value, enum_type),
        SemanticErrorCode.UNSUPPORTED_VALUE,
        f"{field_name} must be a {enum_type.__name__}",
    )


class CandidateState(str, Enum):
    """Lifecycle state of a candidate -- never implies publication."""

    DRAFT = "draft"
    ACTIVE = "active"
    PROPOSED = "proposed"
    REJECTED = "rejected"
    SUPPRESSED = "suppressed"
    RECONSIDERED = "reconsidered"


class CandidateBand(str, Enum):
    """Support/novelty strength band, derived from aggregated weight."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CandidateRiskBand(str, Enum):
    """Contradiction-driven risk band, derived from aggregated weight."""

    LOW = "low"
    STANDARD = "standard"
    HIGH = "high"
    CRITICAL = "critical"


class ContributionRole(str, Enum):
    """How one observation contributes to a candidate."""

    SUPPORT = "support"
    CONTRADICT = "contradict"
    NOVELTY = "novelty"


class ReconsiderationReason(str, Enum):
    """Why a suppression is being reconsidered -- never inferred implicitly."""

    NEW_EVIDENCE = "new_evidence"
    RULE_VERSION_CHANGED = "rule_version_changed"
    EXPIRED = "expired"
    HUMAN_OVERRIDE = "human_override"


_TERMINAL_STATES_REQUIRING_SIGNATURE = frozenset(
    {CandidateState.REJECTED, CandidateState.SUPPRESSED}
)


@dataclass(frozen=True, slots=True)
class SemanticCandidate:
    """One proposed change with its aggregated evidence bands.

    No automatic approval or publication state exists here: `state` only ever
    reflects the candidate's own lifecycle, never whether it was published.
    """

    candidate_id: str
    workspace_id: str
    candidate_kind: str
    target_model_id: str
    proposed_operation: ChangeOperation
    support_band: CandidateBand
    novelty_band: CandidateBand
    risk_band: CandidateRiskBand
    state: CandidateState
    aggregation_version: str
    normalization_version: str
    base_version_id: str
    evidence_snapshot_digest: str
    created_at: TemporalInstant
    rejection_signature: str | None = None
    schema_version: str = field(init=False, default=CANDIDATE_SCHEMA_VERSION)

    def __post_init__(self) -> None:
        _require_id("candidate_id", self.candidate_id)
        _require_id("workspace_id", self.workspace_id)
        _require_id("candidate_kind", self.candidate_kind)
        _require_id("target_model_id", self.target_model_id)
        require(
            isinstance(self.proposed_operation, ChangeOperation),
            SemanticErrorCode.INVALID_FIELD,
            "proposed_operation must be a ChangeOperation",
        )
        _require_enum("support_band", self.support_band, CandidateBand)
        _require_enum("novelty_band", self.novelty_band, CandidateBand)
        _require_enum("risk_band", self.risk_band, CandidateRiskBand)
        _require_enum("state", self.state, CandidateState)
        _require_id("aggregation_version", self.aggregation_version)
        _require_id("normalization_version", self.normalization_version)
        _require_id("base_version_id", self.base_version_id)
        _require_id("evidence_snapshot_digest", self.evidence_snapshot_digest)
        require(
            isinstance(self.created_at, TemporalInstant),
            SemanticErrorCode.INVALID_FIELD,
            "created_at must be a TemporalInstant",
        )
        if self.state in _TERMINAL_STATES_REQUIRING_SIGNATURE:
            _require_id("rejection_signature", self.rejection_signature or "")
        else:
            require(
                self.rejection_signature is None,
                SemanticErrorCode.INVALID_FIELD,
                f"state {self.state.value!r} must not carry a rejection_signature",
            )


@dataclass(frozen=True, slots=True)
class CandidateContribution:
    """One observation's contribution to a candidate's aggregated evidence."""

    workspace_id: str
    candidate_id: str
    observation_id: str
    role: ContributionRole
    weight: int
    observation_digest: str

    def __post_init__(self) -> None:
        _require_id("workspace_id", self.workspace_id)
        _require_id("candidate_id", self.candidate_id)
        _require_id("observation_id", self.observation_id)
        _require_enum("role", self.role, ContributionRole)
        require(
            isinstance(self.weight, int) and not isinstance(self.weight, bool),
            SemanticErrorCode.INVALID_FIELD,
            "weight must be an int",
        )
        require(
            0 <= self.weight <= 10000,
            SemanticErrorCode.INVALID_FIELD,
            "weight must be between 0 and 10000 inclusive",
        )
        _require_id("observation_digest", self.observation_digest)


@dataclass(frozen=True, slots=True)
class CandidateSuppression:
    """An append-only record that an equivalence class is suppressed."""

    workspace_id: str
    suppression_id: str
    equivalence_signature: str
    rejection_ref: str
    suppression_rule_version: str
    created_at: TemporalInstant
    evidence_snapshot_digest: str
    aggregation_version: str
    expires_at: TemporalInstant | None = None

    def __post_init__(self) -> None:
        _require_id("workspace_id", self.workspace_id)
        _require_id("suppression_id", self.suppression_id)
        _require_id("equivalence_signature", self.equivalence_signature)
        _require_id("rejection_ref", self.rejection_ref)
        _require_id("suppression_rule_version", self.suppression_rule_version)
        require(
            isinstance(self.created_at, TemporalInstant),
            SemanticErrorCode.INVALID_FIELD,
            "created_at must be a TemporalInstant",
        )
        _require_id("evidence_snapshot_digest", self.evidence_snapshot_digest)
        _require_id("aggregation_version", self.aggregation_version)
        if self.expires_at is not None:
            require(
                isinstance(self.expires_at, TemporalInstant),
                SemanticErrorCode.INVALID_FIELD,
                "expires_at must be a TemporalInstant",
            )
            require(
                self.expires_at.value > self.created_at.value,
                SemanticErrorCode.TEMPORAL_INTERVAL_INVALID,
                "expires_at must be strictly after created_at",
            )


@dataclass(frozen=True, slots=True)
class CandidateReconsideration:
    """A record that a suppression was reconsidered for an explicit reason.

    Nothing here triggers reconsideration implicitly -- this record is the
    only way a suppression's `active` status can be attributed a reason.
    """

    workspace_id: str
    reconsideration_id: str
    suppression_id: str
    reason: ReconsiderationReason
    recorded_at: TemporalInstant
    previous_evidence_digest: str | None = None
    new_evidence_digest: str | None = None
    previous_rule_version: str | None = None
    new_rule_version: str | None = None
    actor_principal_id: str | None = None

    def __post_init__(self) -> None:
        _require_id("workspace_id", self.workspace_id)
        _require_id("reconsideration_id", self.reconsideration_id)
        _require_id("suppression_id", self.suppression_id)
        _require_enum("reason", self.reason, ReconsiderationReason)
        require(
            isinstance(self.recorded_at, TemporalInstant),
            SemanticErrorCode.INVALID_FIELD,
            "recorded_at must be a TemporalInstant",
        )
        if self.reason is ReconsiderationReason.NEW_EVIDENCE:
            _require_id("previous_evidence_digest", self.previous_evidence_digest or "")
            _require_id("new_evidence_digest", self.new_evidence_digest or "")
            require(
                self.previous_evidence_digest != self.new_evidence_digest,
                SemanticErrorCode.INVALID_FIELD,
                "new_evidence requires a changed evidence digest",
            )
            require(
                self.previous_rule_version is None and self.new_rule_version is None,
                SemanticErrorCode.INVALID_FIELD,
                "new_evidence must not carry rule-version change fields",
            )
        elif self.reason is ReconsiderationReason.RULE_VERSION_CHANGED:
            _require_id("previous_rule_version", self.previous_rule_version or "")
            _require_id("new_rule_version", self.new_rule_version or "")
            require(
                self.previous_rule_version != self.new_rule_version,
                SemanticErrorCode.INVALID_FIELD,
                "rule_version_changed requires a changed rule version",
            )
            require(
                self.previous_evidence_digest is None
                and self.new_evidence_digest is None,
                SemanticErrorCode.INVALID_FIELD,
                "rule_version_changed must not carry evidence change fields",
            )
        elif self.reason is ReconsiderationReason.EXPIRED:
            require(
                self.previous_evidence_digest is None
                and self.new_evidence_digest is None
                and self.previous_rule_version is None
                and self.new_rule_version is None,
                SemanticErrorCode.INVALID_FIELD,
                "expired must not carry any change fields",
            )
        else:
            require(
                self.actor_principal_id is not None
                and self.actor_principal_id.strip() != "",
                SemanticErrorCode.MISSING_FIELD,
                "human_override requires an actor_principal_id",
            )


def candidate_equivalence_signature(
    workspace_id: str,
    candidate_kind: str,
    target_model_id: str,
    proposed_operation: ChangeOperation,
    normalization_version: str,
    aggregation_version: str,
) -> str:
    """A workspace/version-scoped equivalence signature for a proposed change.

    Excludes the candidate's own id, timestamps and state -- two candidates
    that propose the same change under the same rule versions are equivalent
    regardless of when or as what id they were raised.
    """
    _require_id("workspace_id", workspace_id)
    _require_id("candidate_kind", candidate_kind)
    _require_id("target_model_id", target_model_id)
    _require_id("normalization_version", normalization_version)
    _require_id("aggregation_version", aggregation_version)
    require(
        isinstance(proposed_operation, ChangeOperation),
        SemanticErrorCode.INVALID_FIELD,
        "proposed_operation must be a ChangeOperation",
    )
    return content_digest(
        {
            "workspace_id": workspace_id,
            "candidate_kind": candidate_kind,
            "target_model_id": target_model_id,
            "proposed_operation_digest": content_digest(operation_payload(proposed_operation)),
            "normalization_version": normalization_version,
            "aggregation_version": aggregation_version,
        }
    )


def _band_from_weight(weight: int) -> CandidateBand:
    if weight <= _BAND_LOW_MAX:
        return CandidateBand.LOW
    if weight <= _BAND_MEDIUM_MAX:
        return CandidateBand.MEDIUM
    return CandidateBand.HIGH


def _risk_band_from_weight(weight: int) -> CandidateRiskBand:
    if weight == 0:
        return CandidateRiskBand.LOW
    if weight <= _RISK_STANDARD_MAX:
        return CandidateRiskBand.STANDARD
    if weight <= _RISK_HIGH_MAX:
        return CandidateRiskBand.HIGH
    return CandidateRiskBand.CRITICAL


@dataclass(frozen=True, slots=True)
class CandidateFeatureSummary:
    """Deterministic aggregate of a candidate's contributions."""

    support_count: int
    support_weight: int
    contradict_count: int
    contradict_weight: int
    novelty_count: int
    novelty_weight: int
    support_band: CandidateBand
    novelty_band: CandidateBand
    risk_band: CandidateRiskBand
    aggregation_version: str


def _require_consistent_contributions(
    contributions: Sequence[CandidateContribution],
) -> None:
    """Fail closed on mixed candidate/workspace or duplicate semantic keys.

    All contributions in one aggregation must belong to the same
    `(workspace_id, candidate_id)` -- an aggregation must never blend
    contributions across workspaces or candidates. Each `observation_id`
    may contribute to a candidate at most once -- a duplicate would silently
    double-count that observation's weight.
    """
    seen_candidate: tuple[str, str] | None = None
    seen_observation_ids: set[str] = set()
    for contribution in contributions:
        candidate_key = (contribution.workspace_id, contribution.candidate_id)
        if seen_candidate is None:
            seen_candidate = candidate_key
        else:
            require(
                candidate_key == seen_candidate,
                SemanticErrorCode.CROSS_WORKSPACE_ACCESS,
                "contributions must all share the same workspace_id and candidate_id",
            )
        require(
            contribution.observation_id not in seen_observation_ids,
            SemanticErrorCode.DUPLICATE_ID,
            f"duplicate contribution for observation_id {contribution.observation_id!r}",
        )
        seen_observation_ids.add(contribution.observation_id)


def aggregate_candidate_features(
    contributions: Sequence[CandidateContribution], aggregation_version: str
) -> CandidateFeatureSummary:
    """Pure, input-order-invariant aggregation of `contributions`.

    Sums weight and counts per role, then bands support/novelty weight on the
    shared low/medium/high thresholds and risk on the contradicting weight.
    """
    _require_id("aggregation_version", aggregation_version)
    for contribution in contributions:
        require(
            isinstance(contribution, CandidateContribution),
            SemanticErrorCode.INVALID_FIELD,
            "every contribution must be a CandidateContribution",
        )
    _require_consistent_contributions(contributions)
    support_count = support_weight = 0
    contradict_count = contradict_weight = 0
    novelty_count = novelty_weight = 0
    for contribution in contributions:
        if contribution.role is ContributionRole.SUPPORT:
            support_count += 1
            support_weight += contribution.weight
        elif contribution.role is ContributionRole.CONTRADICT:
            contradict_count += 1
            contradict_weight += contribution.weight
        else:
            novelty_count += 1
            novelty_weight += contribution.weight

    return CandidateFeatureSummary(
        support_count=support_count,
        support_weight=support_weight,
        contradict_count=contradict_count,
        contradict_weight=contradict_weight,
        novelty_count=novelty_count,
        novelty_weight=novelty_weight,
        support_band=_band_from_weight(support_weight),
        novelty_band=_band_from_weight(novelty_weight),
        risk_band=_risk_band_from_weight(contradict_weight),
        aggregation_version=aggregation_version,
    )


@dataclass(frozen=True, slots=True)
class SuppressionActivity:
    """The pure result of evaluating a suppression at one point in time."""

    active: bool
    reason: ReconsiderationReason | None


def suppression_active(
    suppression: CandidateSuppression,
    at: TemporalInstant,
    evidence_snapshot_digest: str,
    aggregation_version: str,
) -> SuppressionActivity:
    """Whether `suppression` is still active at `at`.

    Ends only on an expiry timestamp reached, changed evidence, or a changed
    aggregation rule version -- never on an implicit "someone looked at it
    again"; human override is represented only by an explicit
    `CandidateReconsideration`, never inferred here.
    """
    require(
        isinstance(suppression, CandidateSuppression),
        SemanticErrorCode.INVALID_FIELD,
        "suppression must be a CandidateSuppression",
    )
    require(
        isinstance(at, TemporalInstant),
        SemanticErrorCode.INVALID_FIELD,
        "at must be a TemporalInstant",
    )
    _require_id("evidence_snapshot_digest", evidence_snapshot_digest)
    _require_id("aggregation_version", aggregation_version)

    if suppression.expires_at is not None and at.value >= suppression.expires_at.value:
        return SuppressionActivity(active=False, reason=ReconsiderationReason.EXPIRED)
    if evidence_snapshot_digest != suppression.evidence_snapshot_digest:
        return SuppressionActivity(
            active=False, reason=ReconsiderationReason.NEW_EVIDENCE
        )
    if aggregation_version != suppression.aggregation_version:
        return SuppressionActivity(
            active=False, reason=ReconsiderationReason.RULE_VERSION_CHANGED
        )
    return SuppressionActivity(active=True, reason=None)


def _instant_payload(instant: TemporalInstant) -> dict[str, Any]:
    return {
        "value": instant.value.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "precision": instant.precision.value,
        "provenance": instant.provenance.value,
    }


def candidate_payload(candidate: SemanticCandidate) -> dict[str, Any]:
    """Canonical, JSON-safe semantic content of `candidate`."""
    return {
        "candidate_id": candidate.candidate_id,
        "workspace_id": candidate.workspace_id,
        "candidate_kind": candidate.candidate_kind,
        "target_model_id": candidate.target_model_id,
        "proposed_operation": operation_payload(candidate.proposed_operation),
        "support_band": candidate.support_band.value,
        "novelty_band": candidate.novelty_band.value,
        "risk_band": candidate.risk_band.value,
        "state": candidate.state.value,
        "aggregation_version": candidate.aggregation_version,
        "normalization_version": candidate.normalization_version,
        "base_version_id": candidate.base_version_id,
        "evidence_snapshot_digest": candidate.evidence_snapshot_digest,
        "created_at": _instant_payload(candidate.created_at),
        "rejection_signature": candidate.rejection_signature,
        "schema_version": candidate.schema_version,
    }


def candidate_digest(candidate: SemanticCandidate) -> str:
    return content_digest(candidate_payload(candidate))


def candidate_contribution_payload(
    contribution: CandidateContribution,
) -> dict[str, Any]:
    """Canonical, JSON-safe semantic content of `contribution`."""
    return {
        "workspace_id": contribution.workspace_id,
        "candidate_id": contribution.candidate_id,
        "observation_id": contribution.observation_id,
        "role": contribution.role.value,
        "weight": contribution.weight,
        "observation_digest": contribution.observation_digest,
    }


def candidate_contribution_digest(contribution: CandidateContribution) -> str:
    return content_digest(candidate_contribution_payload(contribution))


def _contribution_sort_key(
    contribution: CandidateContribution,
) -> tuple[str, str, str]:
    return (
        contribution.role.value,
        contribution.observation_id,
        contribution.observation_digest,
    )


def candidate_bundle_payload(
    candidate: SemanticCandidate,
    contributions: Sequence[CandidateContribution],
) -> dict[str, Any]:
    """Canonical, JSON-safe semantic content of a candidate and its contributions.

    Contributions -- contradictory ones included -- are sorted by a stable
    semantic key so input order never changes the digest. Every contribution
    must belong to `candidate`'s own workspace and id -- a cross-workspace or
    cross-candidate contribution fails closed rather than silently joining
    the bundle.
    """
    _require_consistent_contributions(contributions)
    for contribution in contributions:
        require(
            contribution.workspace_id == candidate.workspace_id
            and contribution.candidate_id == candidate.candidate_id,
            SemanticErrorCode.CROSS_WORKSPACE_ACCESS,
            "contribution workspace_id/candidate_id must match the candidate",
        )
    sorted_contributions = sorted(contributions, key=_contribution_sort_key)
    return {
        "candidate": candidate_payload(candidate),
        "contributions": [
            candidate_contribution_payload(contribution)
            for contribution in sorted_contributions
        ],
    }


def candidate_bundle_digest(
    candidate: SemanticCandidate,
    contributions: Sequence[CandidateContribution],
) -> str:
    return content_digest(candidate_bundle_payload(candidate, contributions))


def suppression_payload(suppression: CandidateSuppression) -> dict[str, Any]:
    """Canonical, JSON-safe semantic content of `suppression`."""
    return {
        "workspace_id": suppression.workspace_id,
        "suppression_id": suppression.suppression_id,
        "equivalence_signature": suppression.equivalence_signature,
        "rejection_ref": suppression.rejection_ref,
        "suppression_rule_version": suppression.suppression_rule_version,
        "created_at": _instant_payload(suppression.created_at),
        "expires_at": (
            None
            if suppression.expires_at is None
            else _instant_payload(suppression.expires_at)
        ),
        "evidence_snapshot_digest": suppression.evidence_snapshot_digest,
        "aggregation_version": suppression.aggregation_version,
    }


def suppression_digest(suppression: CandidateSuppression) -> str:
    return content_digest(suppression_payload(suppression))


def reconsideration_payload(
    reconsideration: CandidateReconsideration,
) -> dict[str, Any]:
    """Canonical, JSON-safe semantic content of `reconsideration`."""
    return {
        "workspace_id": reconsideration.workspace_id,
        "reconsideration_id": reconsideration.reconsideration_id,
        "suppression_id": reconsideration.suppression_id,
        "reason": reconsideration.reason.value,
        "recorded_at": _instant_payload(reconsideration.recorded_at),
        "previous_evidence_digest": reconsideration.previous_evidence_digest,
        "new_evidence_digest": reconsideration.new_evidence_digest,
        "previous_rule_version": reconsideration.previous_rule_version,
        "new_rule_version": reconsideration.new_rule_version,
        "actor_principal_id": reconsideration.actor_principal_id,
    }


def reconsideration_digest(reconsideration: CandidateReconsideration) -> str:
    return content_digest(reconsideration_payload(reconsideration))


__all__ = [
    "AGGREGATION_RULE_VERSION",
    "CANDIDATE_SCHEMA_VERSION",
    "NORMALIZATION_RULE_VERSION",
    "SUPPRESSION_RULE_VERSION",
    "CandidateBand",
    "CandidateContribution",
    "CandidateFeatureSummary",
    "CandidateReconsideration",
    "CandidateRiskBand",
    "CandidateState",
    "CandidateSuppression",
    "ContributionRole",
    "ReconsiderationReason",
    "SemanticCandidate",
    "SuppressionActivity",
    "aggregate_candidate_features",
    "candidate_bundle_digest",
    "candidate_bundle_payload",
    "candidate_contribution_digest",
    "candidate_contribution_payload",
    "candidate_digest",
    "candidate_equivalence_signature",
    "candidate_payload",
    "reconsideration_digest",
    "reconsideration_payload",
    "suppression_active",
    "suppression_digest",
    "suppression_payload",
]
