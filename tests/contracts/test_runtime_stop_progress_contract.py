"""Tests for the runtime stop-progress contract (C05a).

C05a adds `RuntimeStopProjection` and its two progress vocabularies
(`RuntimeStopPhase`, `RuntimeStopCleanupState`), an optional `stop` field on
`WorkflowControlResult` and `WorkflowReviewResult`, and one new
`WorkflowControlDisposition` member (`cancellation_pending_reconciliation`).
It is a contract and reservation packet: no runtime handler, store or process
reads or writes any of this yet, so every case here proves a rule the
generated DTOs and `semantics_runtime` validators enforce in isolation, the
same way `test_runtime_contracts.py` proves the rest of the Runtime domain.

Every invariant is proved by its failure mode: a valid value, then the
smallest mutation that breaks the rule, then the refusal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from omnivia_core.contracts.v1 import semantics_runtime as runtime
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    RuntimeStopProjection,
    WorkflowControlResult,
    WorkflowReviewResult,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
BASE_URI = "https://contracts.omnivia.dev/application/v1/"

RUN_ID = "run-0001"
DIGEST = "sha256:" + "a" * 64


# --------------------------------------------------------------------------
# Fixture-free builders: small wire documents, overridden per test.
# --------------------------------------------------------------------------


def _definition() -> dict[str, Any]:
    return {
        "definition_kind": "workflow",
        "definition_id": "workflow-0001",
        "definition_version": "1.0.0",
    }


def _run_projection(**overrides: Any) -> dict[str, Any]:
    document = {
        "run_id": RUN_ID,
        "definition": _definition(),
        "plan_digest": DIGEST,
        "state": "running",
        "run_status": "running",
        "binding": {},
    }
    document.update(overrides)
    return document


def _stop(**overrides: Any) -> dict[str, Any]:
    document = {
        "stop_request_id": "stop-0001",
        "phase": "requested",
        "requested_at": "2026-08-22T09:10:00Z",
        "request_audit_ref": "audit-stop-0001",
        "pending_effect_count": 0,
        "pending_effect_ids": [],
        "pending_effects_truncated": False,
        "retry_eligible": False,
        "cleanup_state": "not_required",
    }
    document.update(overrides)
    return document


def _control_result(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "run": _run_projection(),
        "disposition": "wait_resolved",
    }
    document.update(overrides)
    return document


def _review_result(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "run": _run_projection(),
        "journal": [],
        "resumable": True,
    }
    document.update(overrides)
    return document


def _stop_projection(**overrides: Any) -> RuntimeStopProjection:
    return RuntimeStopProjection.from_wire(_stop(**overrides))


# --------------------------------------------------------------------------
# Old valid results remain valid: stop is optional.
# --------------------------------------------------------------------------


def test_legacy_control_result_without_stop_remains_valid() -> None:
    result = WorkflowControlResult.from_wire(_control_result())
    assert result.stop is None
    runtime.validate_workflow_control_result(result)


def test_legacy_review_result_without_stop_remains_valid() -> None:
    result = WorkflowReviewResult.from_wire(_review_result())
    assert result.stop is None
    runtime.validate_workflow_review_result(result)


def test_legacy_review_result_round_trips_without_a_stop_key() -> None:
    document = _review_result()
    result = WorkflowReviewResult.from_wire(document)
    assert "stop" not in result.to_wire()
    assert result.to_wire() == document


# --------------------------------------------------------------------------
# Valid pending and settled projections.
# --------------------------------------------------------------------------


def test_pending_reconciliation_result_with_zero_effects_and_uncertain_cleanup_is_valid() -> None:
    """Zero pending effects is not proof cleanup finished.

    This is the case the contract exists to make representable: an empty
    `pending_effect_ids` with `uncertain` cleanup, so an effect count alone can
    never be read as implying cleanup is complete.
    """
    stop = _stop(phase="pending_reconciliation", cleanup_state="uncertain")
    result = WorkflowControlResult.from_wire(
        _control_result(
            run=_run_projection(run_status="uncertain", state="indeterminate"),
            disposition="cancellation_pending_reconciliation",
            stop=stop,
        )
    )
    assert result.stop is not None
    assert result.stop.pending_effect_count == 0
    assert result.stop.cleanup_state == "uncertain"
    runtime.validate_workflow_control_result(result)


def test_cancellation_accepted_with_settled_stop_over_cancelled_run_is_valid() -> None:
    stop = _stop(phase="settled", cleanup_state="completed", retry_eligible=True)
    result = WorkflowControlResult.from_wire(
        _control_result(
            run=_run_projection(run_status="cancelled", state="cancelled"),
            disposition="cancellation_accepted",
            stop=stop,
        )
    )
    runtime.validate_workflow_control_result(result)


def test_settled_projection_alone_is_valid_on_a_review_result() -> None:
    stop = _stop(phase="settled", cleanup_state="completed", retry_eligible=True)
    result = WorkflowReviewResult.from_wire(
        _review_result(run=_run_projection(run_status="cancelled", state="cancelled"), stop=stop)
    )
    runtime.validate_workflow_review_result(result)


# --------------------------------------------------------------------------
# Bounded, unique pending_effect_ids.
# --------------------------------------------------------------------------


def test_pending_effect_ids_over_the_bound_are_refused() -> None:
    ids = [f"effect-{index:04d}" for index in range(129)]
    stop = _stop_projection(pending_effect_ids=ids, pending_effect_count=129)
    with pytest.raises(ContractSemanticError, match="more than 128"):
        runtime.validate_runtime_stop_projection(stop)


def test_pending_effect_ids_at_the_bound_is_valid() -> None:
    ids = [f"effect-{index:04d}" for index in range(128)]
    stop = _stop_projection(pending_effect_ids=ids, pending_effect_count=128)
    runtime.validate_runtime_stop_projection(stop)


def test_duplicate_pending_effect_ids_are_refused() -> None:
    stop = _stop_projection(
        pending_effect_ids=["effect-0001", "effect-0001"], pending_effect_count=2
    )
    with pytest.raises(ContractSemanticError, match="appears more than once"):
        runtime.validate_runtime_stop_projection(stop)


def test_pending_effect_count_over_its_own_bound_is_refused() -> None:
    stop = _stop_projection(pending_effect_count=257, pending_effects_truncated=True)
    with pytest.raises(ContractSemanticError, match="more than 256"):
        runtime.validate_runtime_stop_projection(stop)


# --------------------------------------------------------------------------
# Counts and truncation must agree.
# --------------------------------------------------------------------------


def test_truncated_projection_requires_a_count_exceeding_the_listed_ids() -> None:
    stop = _stop_projection(
        pending_effect_ids=["effect-0001"], pending_effect_count=1, pending_effects_truncated=True
    )
    with pytest.raises(ContractSemanticError, match="does not exceed"):
        runtime.validate_runtime_stop_projection(stop)


def test_truncated_projection_with_count_over_the_listed_ids_is_valid() -> None:
    stop = _stop_projection(
        pending_effect_ids=["effect-0001"], pending_effect_count=5, pending_effects_truncated=True
    )
    runtime.validate_runtime_stop_projection(stop)


def test_nontruncated_projection_requires_the_count_to_equal_the_listed_ids() -> None:
    stop = _stop_projection(
        pending_effect_ids=["effect-0001"],
        pending_effect_count=2,
        pending_effects_truncated=False,
    )
    with pytest.raises(ContractSemanticError, match="does not equal"):
        runtime.validate_runtime_stop_projection(stop)


def test_nontruncated_projection_with_matching_count_is_valid() -> None:
    stop = _stop_projection(
        pending_effect_ids=["effect-0001", "effect-0002"],
        pending_effect_count=2,
        pending_effects_truncated=False,
    )
    runtime.validate_runtime_stop_projection(stop)


# --------------------------------------------------------------------------
# Requested/pending phases refuse retry eligibility.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["requested", "pending_reconciliation"])
def test_retry_eligible_is_refused_before_settled(phase: str) -> None:
    stop = _stop_projection(phase=phase, retry_eligible=True)
    with pytest.raises(ContractSemanticError, match="retry_eligible"):
        runtime.validate_runtime_stop_projection(stop)


def test_settled_phase_may_be_retry_eligible() -> None:
    stop = _stop_projection(phase="settled", retry_eligible=True)
    runtime.validate_runtime_stop_projection(stop)


def test_settled_phase_need_not_be_retry_eligible() -> None:
    stop = _stop_projection(phase="settled", retry_eligible=False)
    runtime.validate_runtime_stop_projection(stop)


# --------------------------------------------------------------------------
# `settled` claims every stop obligation resolved: zero pending effects and a
# resolved cleanup_state (not_required or completed). This is the C05a repair
# -- retry_eligible was previously checked only against phase, so a settled
# projection could carry an unresolved cleanup_state or a nonzero pending
# effect count and still validate.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cleanup_state", ["requested", "failed", "partial", "uncertain"])
def test_settled_with_unresolved_cleanup_state_is_refused(cleanup_state: str) -> None:
    stop = _stop_projection(phase="settled", cleanup_state=cleanup_state)
    with pytest.raises(ContractSemanticError, match="cleanup_state"):
        runtime.validate_runtime_stop_projection(stop)


@pytest.mark.parametrize("cleanup_state", ["not_required", "completed"])
def test_settled_with_resolved_cleanup_state_is_valid(cleanup_state: str) -> None:
    stop = _stop_projection(phase="settled", cleanup_state=cleanup_state)
    runtime.validate_runtime_stop_projection(stop)


def test_settled_with_a_nontruncated_positive_pending_count_is_refused() -> None:
    stop = _stop_projection(
        phase="settled",
        pending_effect_ids=["effect-1"],
        pending_effect_count=1,
        pending_effects_truncated=False,
    )
    with pytest.raises(ContractSemanticError, match="pending_effect_count"):
        runtime.validate_runtime_stop_projection(stop)


def test_settled_with_a_truncated_positive_pending_count_is_refused() -> None:
    stop = _stop_projection(
        phase="settled",
        pending_effect_ids=["effect-1"],
        pending_effect_count=5,
        pending_effects_truncated=True,
        retry_eligible=True,
        cleanup_state="uncertain",
    )
    with pytest.raises(ContractSemanticError, match="pending_effect_count"):
        runtime.validate_runtime_stop_projection(stop)


def test_settled_with_the_reviewed_regression_shape_is_refused() -> None:
    """The exact case the C05a independent review flagged.

    `phase=settled`, `pending_effect_count=1`, `pending_effect_ids=['effect-1']`,
    `retry_eligible=true`, `cleanup_state=uncertain` previously passed
    `validate_runtime_stop_projection`: `settled` alone was never cross-checked
    against a nonzero pending count or an unresolved cleanup_state.
    """
    stop = _stop_projection(
        phase="settled",
        pending_effect_count=1,
        pending_effect_ids=["effect-1"],
        pending_effects_truncated=False,
        retry_eligible=True,
        cleanup_state="uncertain",
    )
    with pytest.raises(ContractSemanticError, match="pending_effect_count"):
        runtime.validate_runtime_stop_projection(stop)


@pytest.mark.parametrize("cleanup_state", ["not_required", "completed"])
@pytest.mark.parametrize("retry_eligible", [True, False])
def test_settled_with_zero_pending_and_resolved_cleanup_is_valid_regardless_of_retry(
    retry_eligible: bool, cleanup_state: str
) -> None:
    stop = _stop_projection(
        phase="settled", cleanup_state=cleanup_state, retry_eligible=retry_eligible
    )
    runtime.validate_runtime_stop_projection(stop)


@pytest.mark.parametrize("cleanup_state", ["requested", "failed", "partial", "uncertain"])
def test_pending_reconciliation_may_report_zero_effects_with_any_unresolved_cleanup_state(
    cleanup_state: str,
) -> None:
    """Never coerce cleanup to completed from an empty effect list before settled."""
    stop = _stop_projection(phase="pending_reconciliation", cleanup_state=cleanup_state)
    runtime.validate_runtime_stop_projection(stop)


@pytest.mark.parametrize("cleanup_state", ["requested", "failed", "partial", "uncertain"])
def test_requested_may_report_zero_effects_with_any_unresolved_cleanup_state(
    cleanup_state: str,
) -> None:
    stop = _stop_projection(phase="requested", cleanup_state=cleanup_state)
    runtime.validate_runtime_stop_projection(stop)


# --------------------------------------------------------------------------
# cancellation_pending_reconciliation: requires a stop, and requires the Run
# to actually be in the uncertain/indeterminate state it claims.
# --------------------------------------------------------------------------


def test_pending_reconciliation_disposition_without_a_stop_is_refused() -> None:
    result = WorkflowControlResult.from_wire(
        _control_result(
            run=_run_projection(run_status="uncertain", state="indeterminate"),
            disposition="cancellation_pending_reconciliation",
        )
    )
    with pytest.raises(ContractSemanticError, match="must carry the stop"):
        runtime.validate_workflow_control_result(result)


def test_pending_reconciliation_disposition_requires_stop_phase_pending_reconciliation() -> None:
    stop = _stop(phase="requested")
    result = WorkflowControlResult.from_wire(
        _control_result(
            run=_run_projection(run_status="uncertain", state="indeterminate"),
            disposition="cancellation_pending_reconciliation",
            stop=stop,
        )
    )
    with pytest.raises(ContractSemanticError, match="stop.phase"):
        runtime.validate_workflow_control_result(result)


def test_pending_reconciliation_disposition_requires_uncertain_run_status() -> None:
    stop = _stop(phase="pending_reconciliation")
    result = WorkflowControlResult.from_wire(
        _control_result(
            run=_run_projection(run_status="running", state="indeterminate"),
            disposition="cancellation_pending_reconciliation",
            stop=stop,
        )
    )
    with pytest.raises(ContractSemanticError, match="run_status"):
        runtime.validate_workflow_control_result(result)


def test_pending_reconciliation_disposition_requires_indeterminate_workflow_state() -> None:
    stop = _stop(phase="pending_reconciliation")
    result = WorkflowControlResult.from_wire(
        _control_result(
            run=_run_projection(run_status="uncertain", state="waiting"),
            disposition="cancellation_pending_reconciliation",
            stop=stop,
        )
    )
    with pytest.raises(ContractSemanticError, match="Workflow state"):
        runtime.validate_workflow_control_result(result)


# --------------------------------------------------------------------------
# cancellation_accepted with a supplied stop requires settled + cancelled.
# --------------------------------------------------------------------------


def test_cancellation_accepted_with_nonsettled_stop_is_refused() -> None:
    stop = _stop(phase="pending_reconciliation")
    result = WorkflowControlResult.from_wire(
        _control_result(
            run=_run_projection(run_status="uncertain", state="indeterminate"),
            disposition="cancellation_accepted",
            stop=stop,
        )
    )
    with pytest.raises(ContractSemanticError, match="has not reached phase"):
        runtime.validate_workflow_control_result(result)


def test_cancellation_accepted_settled_stop_requires_a_cancelled_run() -> None:
    stop = _stop(phase="settled")
    result = WorkflowControlResult.from_wire(
        _control_result(
            run=_run_projection(run_status="running", state="running"),
            disposition="cancellation_accepted",
            stop=stop,
        )
    )
    with pytest.raises(ContractSemanticError, match="run_status"):
        runtime.validate_workflow_control_result(result)


def test_cancellation_accepted_settled_stop_requires_a_cancelled_workflow_state() -> None:
    """`run_status` alone is not enough: the Workflow `state` reading must also agree."""
    stop = _stop(phase="settled")
    result = WorkflowControlResult.from_wire(
        _control_result(
            run=_run_projection(run_status="cancelled", state="running"),
            disposition="cancellation_accepted",
            stop=stop,
        )
    )
    with pytest.raises(ContractSemanticError, match="Workflow state"):
        runtime.validate_workflow_control_result(result)


def test_cancellation_accepted_without_a_stop_is_not_constrained_by_these_rules() -> None:
    """No disposition requires `stop`; only two constrain it when it is there."""
    result = WorkflowControlResult.from_wire(
        _control_result(
            run=_run_projection(run_status="cancelled", state="cancelled"),
            disposition="cancellation_accepted",
        )
    )
    runtime.validate_workflow_control_result(result)


def test_cancellation_ignored_already_terminal_is_unconstrained_by_stop_rules() -> None:
    stop = _stop(phase="requested")
    result = WorkflowControlResult.from_wire(
        _control_result(
            run=_run_projection(run_status="succeeded", state="completed"),
            disposition="cancellation_ignored_already_terminal",
            stop=stop,
        )
    )
    runtime.validate_workflow_control_result(result)


# --------------------------------------------------------------------------
# Unknown-field rejection: strict schema, independent of the tolerant decoder.
# --------------------------------------------------------------------------


def _strict_validator(reference: str) -> Draft202012Validator:
    names = tuple(
        sorted(path.name.removesuffix(".schema.json") for path in SCHEMA_DIR.glob("*.schema.json"))
    )
    resources = [
        Resource.from_contents(
            json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
        )
        for name in names
    ]
    entries = [(resource.id(), resource) for resource in resources]
    registry: Registry = Registry().with_resources(entries)  # type: ignore[arg-type]
    return Draft202012Validator(
        {"$ref": reference}, registry=registry, format_checker=Draft202012Validator.FORMAT_CHECKER
    )


def test_unknown_field_on_stop_projection_is_rejected_by_strict_schema() -> None:
    validator = _strict_validator(f"{BASE_URI}runtime.schema.json#/$defs/RuntimeStopProjection")
    document = _stop(unexpected_field="surprise")
    assert list(validator.iter_errors(document)), "strict schema must reject the unknown field"
    # The tolerant generated decoder still accepts it, by design: unknown fields are
    # ignored so a newer peer's additive minor release still decodes here.
    decoded = RuntimeStopProjection.from_wire(document)
    assert not hasattr(decoded, "unexpected_field")


def test_stop_projection_with_only_known_fields_is_strict_schema_valid() -> None:
    validator = _strict_validator(f"{BASE_URI}runtime.schema.json#/$defs/RuntimeStopProjection")
    assert list(validator.iter_errors(_stop())) == []


def test_control_result_without_stop_is_still_strict_schema_valid() -> None:
    """Adding an optional field does not make an old-shaped result schema-invalid."""
    validator = _strict_validator(f"{BASE_URI}runtime.schema.json#/$defs/WorkflowControlResult")
    assert list(validator.iter_errors(_control_result())) == []


def test_review_result_without_stop_is_still_strict_schema_valid() -> None:
    validator = _strict_validator(f"{BASE_URI}runtime.schema.json#/$defs/WorkflowReviewResult")
    assert list(validator.iter_errors(_review_result())) == []


# --------------------------------------------------------------------------
# Unknown open values fail safe: they decode unchanged, and semantic
# validation refuses to act on them, the same way validate_attempt refuses an
# unrecognized AttemptStatus.
# --------------------------------------------------------------------------


def test_unrecognized_disposition_decodes_unchanged_but_is_refused_by_validation() -> None:
    document = _control_result(disposition="quiesced_experimental")
    result = WorkflowControlResult.from_wire(document)
    assert result.disposition == "quiesced_experimental"
    with pytest.raises(ContractSemanticError):
        runtime.validate_workflow_control_result(result)


def test_unrecognized_stop_phase_decodes_unchanged_but_is_refused_by_validation() -> None:
    stop = RuntimeStopProjection.from_wire(_stop(phase="quiesced_experimental"))
    assert stop.phase == "quiesced_experimental"
    with pytest.raises(ContractSemanticError):
        runtime.validate_runtime_stop_projection(stop)


def test_unrecognized_cleanup_state_decodes_unchanged_but_is_refused_by_validation() -> None:
    stop = RuntimeStopProjection.from_wire(_stop(cleanup_state="quiesced_experimental"))
    assert stop.cleanup_state == "quiesced_experimental"
    with pytest.raises(ContractSemanticError):
        runtime.validate_runtime_stop_projection(stop)


# --------------------------------------------------------------------------
# Shape guards on the projection itself.
# --------------------------------------------------------------------------


def test_validate_runtime_stop_projection_rejects_the_wrong_type() -> None:
    with pytest.raises(ContractSemanticError):
        runtime.validate_runtime_stop_projection(_stop())


def test_validate_workflow_control_result_rejects_the_wrong_type() -> None:
    with pytest.raises(ContractSemanticError):
        runtime.validate_workflow_control_result(_control_result())


def test_validate_workflow_review_result_rejects_the_wrong_type() -> None:
    with pytest.raises(ContractSemanticError):
        runtime.validate_workflow_review_result(_review_result())
