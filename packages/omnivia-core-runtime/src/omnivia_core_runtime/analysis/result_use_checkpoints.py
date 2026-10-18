"""Result-use checkpoints over the analysis-use authority seam (SPEC-CORE-DATA-001 §13.3).

Three fixed checkpoints (plan admission, result publication, result retrieval) each
resolve current authority once, compose the shared evaluator's input from the bound
query and snapshot only, call the one shared evaluator and decode its answer. The
checkpoint never decides a use itself, and it never turns an evaluator or decoder
error into an authority refusal. Deny and warning outcomes return normally.

Internal only: nothing here is exported from the analysis package, and nothing here
activates `analysis.start`, touches storage, a cache or the public API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from omnivia_core.contracts.v1 import (
    Identifier,
    ResultUseEvaluateInput,
    ResultUseEvaluateResult,
)
from omnivia_core.contracts.v1.semantics_result_use import evaluate_result_use
from omnivia_core_runtime.analysis.authority import (
    AnalysisUseAuthorityRefused,
    AnalysisUseAuthorityResolver,
    AnalysisUseAuthoritySnapshot,
    AnalysisUseAuthoritySubject,
    resolve_analysis_use_authority_for_subject,
)
from omnivia_core_runtime.storage.dataset_state import DatasetStateRecord

#: Literal, deliberately not the repository CONTRACT_VERSION.
_REQUEST_VERSION: Final = "1.0"


@dataclass(frozen=True, slots=True)
class AnalysisResultUseCheckpoint:
    """One checkpoint's resolved authority, composed evaluator input and decoded decision."""

    checkpoint: str
    authority: AnalysisUseAuthoritySnapshot
    evaluation_input: ResultUseEvaluateInput
    decision: ResultUseEvaluateResult


def evaluate_plan_admission_checkpoint(
    subject: AnalysisUseAuthoritySubject,
    *,
    dataset: DatasetStateRecord,
    subject_digest: Identifier,
    resolved_use_class: str,
    evaluation_instant: datetime,
    resolver: AnalysisUseAuthorityResolver,
) -> AnalysisResultUseCheckpoint:
    """Evaluate the plan-admission checkpoint."""
    return _evaluate_checkpoint(
        "plan_admission",
        subject,
        dataset=dataset,
        subject_digest=subject_digest,
        resolved_use_class=resolved_use_class,
        evaluation_instant=evaluation_instant,
        resolver=resolver,
    )


def evaluate_result_publication_checkpoint(
    subject: AnalysisUseAuthoritySubject,
    *,
    dataset: DatasetStateRecord,
    subject_digest: Identifier,
    resolved_use_class: str,
    evaluation_instant: datetime,
    resolver: AnalysisUseAuthorityResolver,
) -> AnalysisResultUseCheckpoint:
    """Evaluate the result-publication checkpoint."""
    return _evaluate_checkpoint(
        "result_publication",
        subject,
        dataset=dataset,
        subject_digest=subject_digest,
        resolved_use_class=resolved_use_class,
        evaluation_instant=evaluation_instant,
        resolver=resolver,
    )


def evaluate_result_retrieval_checkpoint(
    subject: AnalysisUseAuthoritySubject,
    *,
    dataset: DatasetStateRecord,
    subject_digest: Identifier,
    resolved_use_class: str,
    evaluation_instant: datetime,
    resolver: AnalysisUseAuthorityResolver,
) -> AnalysisResultUseCheckpoint:
    """Evaluate the result-retrieval checkpoint."""
    return _evaluate_checkpoint(
        "result_retrieval",
        subject,
        dataset=dataset,
        subject_digest=subject_digest,
        resolved_use_class=resolved_use_class,
        evaluation_instant=evaluation_instant,
        resolver=resolver,
    )


def _evaluate_checkpoint(
    checkpoint: str,
    subject: AnalysisUseAuthoritySubject,
    *,
    dataset: DatasetStateRecord,
    subject_digest: Identifier,
    resolved_use_class: str,
    evaluation_instant: datetime,
    resolver: AnalysisUseAuthorityResolver,
) -> AnalysisResultUseCheckpoint:
    snapshot = resolve_analysis_use_authority_for_subject(
        subject,
        dataset=dataset,
        subject_digest=subject_digest,
        use_class=resolved_use_class,
        evaluation_instant=evaluation_instant,
        resolver=resolver,
    )
    evaluation_input = _compose_result_use_input(snapshot)
    decision = ResultUseEvaluateResult.from_wire(
        evaluate_result_use(
            evaluation_input.to_wire(),
            evaluation_instant=snapshot.query.evaluation_instant,
        )
    )
    return AnalysisResultUseCheckpoint(
        checkpoint=checkpoint,
        authority=snapshot,
        evaluation_input=evaluation_input,
        decision=decision,
    )


def _compose_result_use_input(
    snapshot: AnalysisUseAuthoritySnapshot,
) -> ResultUseEvaluateInput:
    # Only the bound query and snapshot are read. `freshness_ok` is the resolver's
    # current-policy fact, never a comparison against the stored deadline.
    query = snapshot.query
    if query.initial_readiness != "ready":
        raise AnalysisUseAuthorityRefused()
    return ResultUseEvaluateInput(
        request_version=_REQUEST_VERSION,
        use_class=query.use_class,
        subject_digest=query.subject_digest,
        completeness=query.completeness,
        continuity=query.continuity,
        freshness_ok=snapshot.freshness_ok,
        schema_compatible=query.schema_compatibility == "compatible",
        evidence_available=(
            query.evidence_availability == "available"
            and snapshot.evidence_access_permitted
        ),
        policy_permits_partial_or_stale=snapshot.policy_permits_partial_or_stale,
        authority_epoch=snapshot.authority_epoch,
    )
