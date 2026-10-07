"""C16b: `knowledge.evaluation.produce` through the production application surface over a migrated workspace.

Every behaviour below is driven through `ProductionApplicationSurface.dispatch_for_session`, composed by
`service.main`, over a real migrated workspace. The caller submits Stage 2 content and a source; Core derives
the report and registers each canonical record as evidence under its profile ID. The principal is the
session's, and no payload member can state a verdict, an actor or a workspace.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.identity import SystemClock
from omnivia_core_runtime.service.application import (
    KNOWLEDGE_EVALUATION_FAMILY_PURPOSES,
    ProductionApplicationSurface,
    build_installation_application_dispatcher,
)
from omnivia_core_runtime.service.authorization import AuthenticatedSession, Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers.knowledge_evaluation import (
    OPERATION_EVALUATION_PRODUCE,
)
from omnivia_core_runtime.service.main import _build_production_application_surface
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.storage.semantic_evidence import read_evidence_item

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_WORKSPACE_NOT_GRANTED,
    ErrorResponseEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
)
from omnivia_core.governed_knowledge.assisted import (
    DIAGNOSIS_CAPABILITY,
    EVALUATION_CAPABILITY,
    AssistedWorkerBinding,
    ModelIdentityKind,
    worker_binding_to_content,
)
from omnivia_core.governed_knowledge.evaluation import (
    EVALUATION_PURPOSE,
    PILOT_CRITICAL_CASE_IDS,
    CandidateOverlay,
    DeterministicCheck,
    DeterministicCheckStatus,
    EvaluationReportStatus,
    build_renewal_pilot_suite,
    candidate_overlay_to_content,
    evaluation_attempt_to_content,
    evaluation_case_to_content,
    evaluation_suite_to_content,
    run_evaluation_attempt,
)
from omnivia_core.governed_knowledge.stage2_producer import produce_evaluation_report
from omnivia_core.semantic_registry.evidence import Classification
from omnivia_core.semantic_registry.temporal import (
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
)

WS = m1.WORKSPACE_ID
CONTRIBUTOR = "contributor-a"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
SOURCE_LOCATOR = "urn:omnivia:stage2:renewal-pilot"


class _InstallationService:
    """Construction-only shape; its bound production handlers are never invoked."""

    authority = SimpleNamespace(installation_id=s0.INSTALLATION_ID)


#: The service instance's own principal. Callers vary per request through the session; the wiring does not.
SERVICE_PRINCIPAL = "local-user"


def _surface(holder: Any) -> ProductionApplicationSurface:
    probe = Dispatcher.for_service_operations(
        Grant(
            principal=SERVICE_PRINCIPAL,
            workspaces=frozenset({WS}),
            operations=frozenset(SERVICE_OPERATIONS),
        ),
        holder,
    )
    started = SimpleNamespace(**vars(holder), workspace_id=WS, clock=SystemClock())
    installation = build_installation_application_dispatcher(
        service=_InstallationService(),  # type: ignore[arg-type]
        principal_id=SERVICE_PRINCIPAL,
        fallback=probe,
    )
    return _build_production_application_surface(
        started=started,  # type: ignore[arg-type]
        probe=probe,
        installation=installation,
    )


# -- Stage 2 content, built as a caller holds it -----------------------------------------


def _instant(offset: int = 0) -> TemporalInstant:
    return TemporalInstant(
        value=datetime(2026, 9, 13, tzinfo=UTC) + timedelta(seconds=offset),
        precision=TemporalPrecision.SECOND,
        provenance=TemporalProvenance.EVIDENCE_ATTESTED,
    )


def _overlay() -> CandidateOverlay:
    return CandidateOverlay(
        overlay_id="overlay-1", workspace_id="ws-1", purpose=EVALUATION_PURPOSE,
        baseline_digest=DIGEST_A, candidate_digest=DIGEST_B,
        candidate_refs=("proposal-1",), authorised_context_refs=("manifest-1",),
        created_at=_instant(), expires_at=_instant(3600),
        classification=Classification.INTERNAL, retention_class="evaluation-pilot",
    )


def _binding() -> AssistedWorkerBinding:
    return AssistedWorkerBinding(
        binding_id="binding-1", workspace_id="ws-1", source_id="platform.local",
        executor_id="knowledge.evaluator", executor_version="1.0.0",
        executor_build_hash=DIGEST_C, executor_content_hash=DIGEST_A,
        runtime_profile_ref="profile.stage2-v1", policy_ref="policy.stage2-v1",
        required_capabilities=(DIAGNOSIS_CAPABILITY, EVALUATION_CAPABILITY),
        minimum_isolation=2, resolved_isolation=3, run_ref="run-1", step_ref="step-1",
        attempt_ref="attempt-binding", provider_ref="provider.local",
        model_ref="model-version-1", model_identity_kind=ModelIdentityKind.EXACT,
        observed_at_ref="observation-1",
    )


def _passing_worker(_case: object, _candidate: object, ordinal: int) -> dict[str, object]:
    return {
        "status": "pass", "output_ref": f"output-{ordinal}", "judgement_ref": f"judge-{ordinal}",
        "safe_error_code": None, "input_tokens": 10, "output_tokens": 5, "cost_microunits": 2,
    }


def _content() -> dict[str, Any]:
    """Every pilot case, every attempt, and the binding, all passing."""
    checks = (DeterministicCheck("security", DeterministicCheckStatus.PASS, "check-evidence-1"),)
    suite, cases = build_renewal_pilot_suite(
        workspace_id="ws-1", owner_ref="commercial-owner", owner_review_ref="review-1"
    )
    attempts = []
    for case in cases:
        count = 3 if case.case_id in PILOT_CRITICAL_CASE_IDS or case.case_id == "PC-06" else 1
        for ordinal in range(1, count + 1):
            attempts.append(
                run_evaluation_attempt(
                    case=case, overlay=_overlay(), binding=_binding(), ordinal=ordinal,
                    evaluated_at=_instant(1), deterministic_checks=checks, worker=_passing_worker,
                )
            )
    return {
        "overlay": candidate_overlay_to_content(_overlay()),
        "suite": evaluation_suite_to_content(suite),
        "cases": [evaluation_case_to_content(item) for item in cases],
        "attempts": [evaluation_attempt_to_content(item) for item in attempts],
        "worker_bindings": [worker_binding_to_content(_binding())],
    }


def _attempt(content: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    return next(item for item in content["attempts"] if item["attempt_id"] == attempt_id)


def _request(
    content: dict[str, Any],
    *,
    report_id: str = "report-1",
    source_id: str = "src-stage2",
    locator: str = SOURCE_LOCATOR,
    **extra: Any,
) -> dict[str, Any]:
    """The wire request a caller sends: the content, the identifiers and the source. Nothing else."""
    return {
        **content,
        "report_id": report_id,
        "triggering_case_ref": "PC-06",
        "classification": "internal",
        "retention_class": "evaluation-pilot",
        "source": {
            "source_id": source_id, "kind": "record", "locator_scheme": "urn",
            "locator": locator, "version": "v1",
        },
        **extra,
    }


# -- harness -----------------------------------------------------------------------------


class Harness:
    """One migrated workspace behind the production surface, called as one principal at a time."""

    def __init__(self, holder: m1.Owned) -> None:
        self.holder = holder
        self.surface = _surface(holder)
        self._requests = 0

    def session(self, principal: str) -> AuthenticatedSession:
        base = self.surface.session_for(OPERATION_EVALUATION_PRODUCE)
        assert base is not None
        return dataclasses.replace(
            base, principal_id=principal, operations=frozenset({OPERATION_EVALUATION_PRODUCE})
        )

    def call(
        self,
        payload: dict[str, Any],
        *,
        principal: str = CONTRIBUTOR,
        key: str | None = None,
        **metadata: Any,
    ) -> Any:
        self._requests += 1
        request_id = f"req-eval-{self._requests}"
        entry = get_operation_metadata(OPERATION_EVALUATION_PRODUCE)
        overrides: dict[str, Any] = {
            "request_id": request_id,
            "correlation_id": f"cor-{request_id}",
            "trace_id": f"trc-{request_id}",
            "purpose": KNOWLEDGE_EVALUATION_FAMILY_PURPOSES[OPERATION_EVALUATION_PRODUCE],
            "workspace_id": WS,
            "idempotency_key": key or f"idem-{request_id}",
        }
        overrides.update(metadata)
        envelope = s0.envelope_for(entry, operation_input=payload, **overrides)
        return self.surface.dispatch_for_session(envelope, self.session(principal))

    def ok(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        response = self.call(payload, **kwargs)
        assert isinstance(response, SuccessResponseEnvelope), response
        return dict(response.to_wire()["result"])

    def code(self, payload: dict[str, Any], **kwargs: Any) -> str:
        response = self.call(payload, **kwargs)
        assert isinstance(response, ErrorResponseEnvelope), response
        return str(response.error.code)

    def evidence_count(self) -> int:
        return int(
            self.holder.connection.execute(
                "SELECT COUNT(*) FROM omnivia_semantic_evidence_items"
            ).fetchone()[0]
        )

    def stored(self, evidence_id: str) -> Any:
        return read_evidence_item(self.holder.connection, WS, evidence_id)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    m1.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WS)
    holder = m1.take_ownership(path, workspace_id=WS)
    yield holder
    holder.connection.close()


@pytest.fixture
def harness(owned: m1.Owned) -> Harness:
    return Harness(owned)


# -- registration -------------------------------------------------------------------------


def test_the_operation_is_registered_natively_under_its_own_mutation_purpose(harness: Harness) -> None:
    harness.surface.registry.assert_complete()
    assert OPERATION_EVALUATION_PRODUCE in harness.surface.registry.operations
    assert KNOWLEDGE_EVALUATION_FAMILY_PURPOSES == {OPERATION_EVALUATION_PRODUCE: "knowledge_evaluation"}


# -- derivation and registration --------------------------------------------------------


def test_an_eligible_pilot_registers_every_record_under_its_profile_id_and_checksum(
    harness: Harness,
) -> None:
    content = _content()
    expected = produce_evaluation_report(
        content,
        report_id="report-1",
        triggering_case_ref="PC-06",
        classification=Classification.INTERNAL,
        retention_class="evaluation-pilot",
    )
    result = harness.ok(_request(content))

    assert result["report_id"] == "report-1"
    assert result["report"]["status"] == EvaluationReportStatus.ELIGIBLE_FOR_REVIEW.value
    assert result["submitted_by"] == CONTRIBUTOR
    assert [item["evidence_id"] for item in result["evidence"]] == [
        record.record_id for record in expected.records
    ]
    for item, record in zip(result["evidence"], expected.records, strict=True):
        assert item["record_kind"] == record.kind
        assert item["record_id"] == record.record_id
        assert item["content_digest"] == record.checksum
        stored = harness.stored(record.record_id)
        assert stored is not None
        assert stored.evidence_id == record.record_id
        assert stored.content_digest == record.checksum
        assert stored.integrity_digest == record.checksum
    assert harness.evidence_count() == len(expected.records) == 42


def test_the_principal_is_the_authenticated_session_and_no_caller_field_is_honoured(
    harness: Harness,
) -> None:
    content = _content()
    result = harness.ok(_request(content), principal="contributor-b")
    assert result["submitted_by"] == "contributor-b"


@pytest.mark.parametrize(
    "extra",
    [
        {"status": "eligible_for_review"},
        {"verdict": "eligible_for_review"},
        {"submitted_by": "someone-else"},
        {"actor": "someone-else"},
        {"workspace_id": "ws-other"},
        {"unexpected": 1},
    ],
    ids=["status", "verdict", "submitted_by", "actor", "workspace_id", "unknown"],
)
def test_a_caller_verdict_actor_or_workspace_field_is_refused_before_any_write(
    harness: Harness, extra: dict[str, Any]
) -> None:
    before = harness.evidence_count()
    assert harness.code(_request(_content(), **extra)) == ERROR_CODE_INVALID_REQUEST
    assert harness.evidence_count() == before


def test_an_unreviewed_case_derives_inconclusive(harness: Harness) -> None:
    content = _content()
    content["cases"][0]["lifecycle"] = "proposed"
    content["cases"][0]["owner_review_ref"] = None
    result = harness.ok(_request(content))
    assert result["report"]["status"] == EvaluationReportStatus.INCONCLUSIVE.value


def test_a_failed_critical_attempt_derives_blocked(harness: Harness) -> None:
    content = _content()
    _attempt(content, "PC-02-attempt-1").update(
        status="fail", output_ref="failed-output", safe_error_code=None
    )
    result = harness.ok(_request(content))
    assert result["report"]["status"] == EvaluationReportStatus.BLOCKED.value


def test_an_incomplete_attempt_set_derives_blocked_and_incomplete(harness: Harness) -> None:
    content = _content()
    content["attempts"] = [
        item for item in content["attempts"] if item["attempt_id"] != "PC-02-attempt-1"
    ]
    result = harness.ok(_request(content))
    assert result["report"]["status"] == EvaluationReportStatus.BLOCKED.value
    assert result["report"]["coverage_complete"] is False


def test_an_attempt_bound_to_an_unsubmitted_worker_binding_is_refused(harness: Harness) -> None:
    content = _content()
    content["worker_bindings"] = []
    before = harness.evidence_count()
    assert harness.code(_request(content)) == ERROR_CODE_INVALID_REQUEST
    assert harness.evidence_count() == before


def test_a_request_for_another_workspace_is_refused_before_any_write(harness: Harness) -> None:
    before = harness.evidence_count()
    assert harness.code(_request(_content()), workspace_id="ws-other") == (
        ERROR_CODE_WORKSPACE_NOT_GRANTED
    )
    assert harness.evidence_count() == before


# -- identity conflicts roll back every write -------------------------------------------


def test_a_source_identity_conflict_writes_none_of_the_records(harness: Harness) -> None:
    content = _content()
    harness.ok(_request(content))
    before = harness.evidence_count()
    other = harness.call(
        _request(content, report_id="report-2", locator="urn:omnivia:stage2:other-pilot")
    )
    assert isinstance(other, ErrorResponseEnvelope)
    assert other.error.code == ERROR_CODE_CONFLICT
    assert harness.evidence_count() == before
    assert harness.stored("report-2") is None


def test_a_record_identity_conflict_writes_none_of_the_records(harness: Harness) -> None:
    harness.ok(_request(_content()))
    before = harness.evidence_count()
    changed = _content()
    _attempt(changed, "PC-05-attempt-1")["output_ref"] = "a-different-output"
    assert harness.code(_request(changed, report_id="report-2")) == ERROR_CODE_CONFLICT
    assert harness.evidence_count() == before
    assert harness.stored("report-2") is None


# -- idempotency --------------------------------------------------------------------------


def test_an_equivalent_replay_returns_the_stored_result_and_writes_nothing_more(
    harness: Harness,
) -> None:
    request = _request(_content())
    first = harness.ok(request, key="idem-produce")
    count = harness.evidence_count()
    assert harness.ok(request, key="idem-produce") == first
    assert harness.evidence_count() == count


def test_the_same_key_for_a_different_input_is_an_idempotency_conflict(harness: Harness) -> None:
    content = _content()
    harness.ok(_request(content), key="idem-one")
    count = harness.evidence_count()
    assert harness.code(_request(content, report_id="report-2"), key="idem-one") == (
        ERROR_CODE_IDEMPOTENCY_CONFLICT
    )
    assert harness.evidence_count() == count
