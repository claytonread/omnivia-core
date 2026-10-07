"""Pure Stage 2 producer: exact caller-held content in, derived report and canonical records out."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from omnivia_core.contracts.v1.canonical_json import canonicalize
from omnivia_core.governed_knowledge.assisted import worker_binding_to_content
from omnivia_core.governed_knowledge.errors import GovernedKnowledgeErrorCode, require
from omnivia_core.governed_knowledge.evaluation import (
    KnowledgeEvaluationReport,
    aggregate_evaluation,
    candidate_overlay_to_content,
    evaluation_attempt_to_content,
    evaluation_case_to_content,
    evaluation_report_to_content,
    evaluation_suite_to_content,
)
from omnivia_core.governed_knowledge.stage2_content import (
    candidate_overlay_from_content,
    evaluation_attempt_from_content,
    evaluation_case_from_content,
    evaluation_suite_from_content,
    worker_binding_from_content,
)
from omnivia_core.semantic_registry.evidence import Classification

PRODUCER_INPUT_KEYS = frozenset({"overlay", "suite", "cases", "attempts", "worker_bindings"})


@dataclass(frozen=True, slots=True)
class CanonicalRecord:
    kind: str
    record_id: str
    canonical_json: str
    checksum: str


@dataclass(frozen=True, slots=True)
class Stage2Production:
    report: KnowledgeEvaluationReport
    records: tuple[CanonicalRecord, ...]


def canonical_record(kind: str, record_id: str, content: Mapping[str, Any]) -> CanonicalRecord:
    text = canonicalize(content)
    return CanonicalRecord(kind, record_id, text, "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest())


def _list(value: object, name: str) -> list[Any]:
    require(isinstance(value, list), GovernedKnowledgeErrorCode.INVALID_FIELD, f"{name} must be a list")
    return cast(list[Any], value)


def _distinct(values: list[Any], name: str) -> None:
    require(len(set(values)) == len(values), GovernedKnowledgeErrorCode.INVALID_FIELD, f"{name} contains duplicates")


def produce_evaluation_report(
    content: Mapping[str, Any],
    *,
    report_id: str,
    triggering_case_ref: str,
    classification: Classification,
    retention_class: str,
) -> Stage2Production:
    """Derive the evaluation report only through `aggregate_evaluation`.

    `content` carries exactly the caller-held inputs. Status, coverage, findings,
    totals, summaries, binding refs and the integrity digest are never accepted
    from the caller; they are computed here.
    """
    require(
        isinstance(content, Mapping) and set(content) == PRODUCER_INPUT_KEYS,
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "producer input fields are not exact",
    )
    overlay = candidate_overlay_from_content(content["overlay"])
    suite = evaluation_suite_from_content(content["suite"])
    cases = tuple(evaluation_case_from_content(item) for item in _list(content["cases"], "cases"))
    attempts = tuple(evaluation_attempt_from_content(item) for item in _list(content["attempts"], "attempts"))
    bindings = tuple(worker_binding_from_content(item) for item in _list(content["worker_bindings"], "worker_bindings"))

    require(bool(attempts), GovernedKnowledgeErrorCode.MISSING_FIELD, "at least one evaluation attempt is required")
    require(
        tuple(item.case_id for item in cases) == suite.case_refs,
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "cases must match the suite exactly, in order",
    )
    _distinct([item.binding_id for item in bindings], "worker binding ids")
    _distinct([item.attempt_id for item in attempts], "attempt ids")
    _distinct([(item.case_ref, item.ordinal) for item in attempts], "attempt case and ordinal pairs")
    require(
        {item.worker_binding_ref for item in attempts} == {item.binding_id for item in bindings},
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "worker bindings must be exactly those referenced by attempts",
    )

    report = aggregate_evaluation(
        report_id=report_id, suite=suite, cases=cases, overlay=overlay, attempts=attempts,
        triggering_case_ref=triggering_case_ref, classification=classification,
        retention_class=retention_class, worker_bindings=bindings,
    )
    records = (
        canonical_record("candidate_overlay", overlay.overlay_id, candidate_overlay_to_content(overlay)),
        canonical_record("evaluation_suite", suite.suite_id, evaluation_suite_to_content(suite)),
        *(canonical_record("evaluation_case", item.case_id, evaluation_case_to_content(item)) for item in cases),
        *(canonical_record("evaluation_attempt", item.attempt_id, evaluation_attempt_to_content(item)) for item in attempts),
        *(canonical_record("worker_binding", item.binding_id, worker_binding_to_content(item)) for item in bindings),
        canonical_record("evaluation_report", report.report_id, evaluation_report_to_content(report)),
    )
    return Stage2Production(report=report, records=records)


__all__ = [
    "PRODUCER_INPUT_KEYS", "CanonicalRecord", "Stage2Production",
    "canonical_record", "produce_evaluation_report",
]
