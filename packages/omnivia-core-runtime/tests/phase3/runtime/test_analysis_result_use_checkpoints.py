"""Result-use authority checkpoints (SPEC-CORE-DATA-001 §13.3).

Every subject, dataset observation, resolver answer and decision is a real production
type. The shared evaluator runs for real except where a test must count its calls or
make it return an impossible document, and then only the checkpoint module's name is
replaced. The properties proved: each fixed label reaches the decision once, the
resolver is asked once, composition reads only the bound query and snapshot, the
resolver's freshness and current epoch are the ones the evaluator sees, a non-ready
dataset and a refused resolver never reach the evaluator, and every evaluator or
decoder failure surfaces unchanged.
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from types import ModuleType
from typing import Any

import pytest
from omnivia_core_runtime.analysis import result_use_checkpoints as checkpoints_module
from omnivia_core_runtime.analysis.authority import (
    REFUSE_ANALYSIS_USE_AUTHORITY,
    AnalysisUseAuthorityQuery,
    AnalysisUseAuthorityRefused,
    AnalysisUseAuthoritySnapshot,
    AnalysisUseAuthoritySubject,
)
from omnivia_core_runtime.analysis.result_use_checkpoints import (
    AnalysisResultUseCheckpoint,
    evaluate_plan_admission_checkpoint,
    evaluate_result_publication_checkpoint,
    evaluate_result_retrieval_checkpoint,
)
from omnivia_core_runtime.storage.dataset_state import (
    EVIDENCE_AVAILABILITY,
    INITIAL_READINESS,
    SCHEMA_COMPATIBILITY,
    DatasetStateObservation,
    DatasetStateRecord,
)

from omnivia_core.contracts.v1 import (
    CapabilityRef,
    ContractDecodeError,
    GrantedAuthority,
    ResultUseEvaluateInput,
    ResultUseEvaluateResult,
    ResultUseRequestError,
)
from omnivia_core.contracts.v1.semantics_result_use import (
    OUTCOME_ALLOW,
    OUTCOME_ALLOW_WITH_WARNING,
    OUTCOME_DENY,
    USE_ACTION_INPUT,
    USE_CURRENT_PUBLICATION,
    USE_EXPLORATION,
    USE_HISTORICAL_DISPLAY,
)

#: The raw text a hostile resolver or evaluator would carry. Never printed or compared.
SENTINEL = "do not echo: 7c2e"

OPERATION = "memory.get"
PURPOSE = "operations.read"
SCOPE = "memory:read"
WORKSPACE = "ws-0001"
SUBJECT_DIGEST = "subject-1"
DATASET_ID = "dataset-1"
SCOPE_DIGEST = "sha256:" + "5" * 64
MANIFEST_ID = "manifest-1"
MANIFEST_REVISION = "manifest-rev-1"
MANIFEST_DIGEST = "sha256:" + "6" * 64
POLICY_REF = "policy-1"
POLICY_DIGEST = "sha256:" + "7" * 64
EPOCH_OBSERVED = "epoch-observed-1"
EPOCH_CURRENT = "epoch-current-2"
INSTANT = datetime(2026, 10, 4, 1, 0, tzinfo=UTC)
INSTANT_US = int(INSTANT.timestamp()) * 1_000_000
REFUSAL = REFUSE_ANALYSIS_USE_AUTHORITY
USE_CLASSES = (
    USE_EXPLORATION,
    USE_HISTORICAL_DISPLAY,
    USE_CURRENT_PUBLICATION,
    USE_ACTION_INPUT,
)
WRAPPERS = (
    (evaluate_plan_admission_checkpoint, "plan_admission"),
    (evaluate_result_publication_checkpoint, "result_publication"),
    (evaluate_result_retrieval_checkpoint, "result_retrieval"),
)

EvaluatorCall = tuple[dict[str, Any], datetime]


def _subject() -> AnalysisUseAuthoritySubject:
    return AnalysisUseAuthoritySubject(
        operation=OPERATION,
        workspace_id=WORKSPACE,
        authority=GrantedAuthority(
            principal_id="principal-1",
            roles=("reader",),
            capabilities=(CapabilityRef(id="memory.read", version="1.4"),),
        ),
        scopes=(SCOPE,),
        purpose=PURPOSE,
    )


def _observation(**overrides: Any) -> DatasetStateObservation:
    fields_: dict[str, Any] = {
        "dataset_id": DATASET_ID,
        "dataset_revision": "rev-1",
        "dataset_incarnation": "inc-1",
        "initial_readiness": "ready",
        "completeness": "complete",
        "continuity": "verified",
        "operational_health": "healthy",
        "schema_compatibility": "compatible",
        "content_observation": "nonempty",
        "evidence_availability": "available",
        "observed_authority_epoch": EPOCH_OBSERVED,
        "scope_digest": SCOPE_DIGEST,
        "coverage": {
            "scope_digest": SCOPE_DIGEST,
            "proof_kind": "complete_enumeration",
        },
        "source_observation": {"source_ref": {"id": "source-erp"}},
        "verified_at_us": 1_790_000_000_000_000,
        "freshness_deadline_at_us": None,
        "manifest_id": MANIFEST_ID,
        "manifest_revision": MANIFEST_REVISION,
        "manifest_digest": MANIFEST_DIGEST,
    }
    fields_.update(overrides)
    return DatasetStateObservation(**fields_)


def _record(**overrides: Any) -> DatasetStateRecord:
    fields_: dict[str, Any] = {
        "workspace_id": WORKSPACE,
        "state_generation": 3,
        "observation": _observation(),
        "coverage_digest": "sha256:" + "8" * 64,
        "source_observation_digest": "sha256:" + "9" * 64,
        "recorded_at_us": 1_790_000_000_000_001,
        "audit_ref": "audit-1",
    }
    fields_.update(overrides)
    return DatasetStateRecord(**fields_)


def _snapshot(
    query: AnalysisUseAuthorityQuery, **overrides: Any
) -> AnalysisUseAuthoritySnapshot:
    fields_: dict[str, Any] = {
        "query": query,
        "authority_epoch": EPOCH_CURRENT,
        "evidence_access_permitted": True,
        "freshness_ok": True,
        "policy_permits_partial_or_stale": False,
        "policy_ref": POLICY_REF,
        "policy_digest": POLICY_DIGEST,
    }
    fields_.update(overrides)
    return AnalysisUseAuthoritySnapshot(**fields_)


def _raise(query: AnalysisUseAuthorityQuery) -> Any:
    raise RuntimeError(SENTINEL)


class _Resolver:
    """Counts its calls, keeps every query and answer, and answers from `overrides`."""

    def __init__(
        self,
        *,
        answer: Callable[[AnalysisUseAuthorityQuery], Any] | None = None,
        **overrides: Any,
    ) -> None:
        self.calls = 0
        self.queries: list[AnalysisUseAuthorityQuery] = []
        self.answers: list[Any] = []
        self._answer = answer
        self._overrides = overrides

    def resolve(self, query: AnalysisUseAuthorityQuery) -> Any:
        self.calls += 1
        self.queries.append(query)
        answer = (
            self._answer(query)
            if self._answer is not None
            else _snapshot(query, **self._overrides)
        )
        self.answers.append(answer)
        return answer


@pytest.fixture
def evaluator(monkeypatch: pytest.MonkeyPatch) -> list[EvaluatorCall]:
    """Records every document and instant the checkpoint sends, then runs the real evaluator."""
    calls: list[EvaluatorCall] = []
    real = checkpoints_module.evaluate_result_use

    def recording(document: Any, *, evaluation_instant: datetime) -> dict[str, Any]:
        calls.append((document, evaluation_instant))
        return real(document, evaluation_instant=evaluation_instant)

    monkeypatch.setattr(checkpoints_module, "evaluate_result_use", recording)
    return calls


def _run(
    wrapper: Callable[..., AnalysisResultUseCheckpoint],
    resolver: _Resolver | None = None,
    *,
    dataset: DatasetStateRecord | None = None,
    use_class: str = USE_CURRENT_PUBLICATION,
    instant: datetime = INSTANT,
) -> AnalysisResultUseCheckpoint:
    return wrapper(
        _subject(),
        dataset=dataset if dataset is not None else _record(),
        subject_digest=SUBJECT_DIGEST,
        resolved_use_class=use_class,
        evaluation_instant=instant,
        resolver=resolver if resolver is not None else _Resolver(),
    )


@pytest.mark.parametrize(("wrapper", "label"), WRAPPERS)
def test_each_wrapper_carries_its_label_and_runs_one_resolver_and_one_evaluation(
    wrapper: Callable[..., AnalysisResultUseCheckpoint],
    label: str,
    evaluator: list[EvaluatorCall],
) -> None:
    resolver = _Resolver()

    result = _run(wrapper, resolver)

    assert type(result) is AnalysisResultUseCheckpoint
    assert result.checkpoint == label
    assert resolver.calls == 1
    assert len(evaluator) == 1
    assert result.decision.outcome == OUTCOME_ALLOW
    assert result.decision.reasons == ()


def test_the_checkpoint_module_exposes_only_the_three_fixed_wrappers() -> None:
    module: ModuleType = checkpoints_module
    public = {
        name
        for name, value in inspect.getmembers(module, inspect.isfunction)
        if value.__module__ == module.__name__ and not name.startswith("_")
    }

    assert public == {
        "evaluate_plan_admission_checkpoint",
        "evaluate_result_publication_checkpoint",
        "evaluate_result_retrieval_checkpoint",
    }


def test_the_wrappers_share_one_keyword_only_shape() -> None:
    def shape(wrapper: Callable[..., Any]) -> list[tuple[str, Any, Any, Any]]:
        return [
            (p.name, p.annotation, p.kind, p.default)
            for p in inspect.signature(wrapper).parameters.values()
        ]

    plan = shape(evaluate_plan_admission_checkpoint)
    publication = shape(evaluate_result_publication_checkpoint)
    retrieval = shape(evaluate_result_retrieval_checkpoint)

    assert plan == publication == retrieval
    assert [name for name, *_ in plan] == [
        "subject",
        "dataset",
        "subject_digest",
        "resolved_use_class",
        "evaluation_instant",
        "resolver",
    ]
    assert [kind for _, _, kind, _ in plan[1:]] == [inspect.Parameter.KEYWORD_ONLY] * 5
    assert {wrapper.__annotations__["return"] for wrapper, _ in WRAPPERS} == {
        "AnalysisResultUseCheckpoint"
    }


@pytest.mark.parametrize(("wrapper", "label"), WRAPPERS)
@pytest.mark.parametrize("use_class", USE_CLASSES)
def test_composed_input_is_exact_for_every_accepted_use_class(
    wrapper: Callable[..., AnalysisResultUseCheckpoint],
    label: str,
    use_class: str,
    evaluator: list[EvaluatorCall],
) -> None:
    result = _run(wrapper, use_class=use_class)

    assert type(result.evaluation_input) is ResultUseEvaluateInput
    assert result.evaluation_input == ResultUseEvaluateInput(
        request_version="1.0",
        use_class=use_class,
        subject_digest=SUBJECT_DIGEST,
        completeness="complete",
        continuity="verified",
        freshness_ok=True,
        schema_compatible=True,
        evidence_available=True,
        policy_permits_partial_or_stale=False,
        authority_epoch=EPOCH_CURRENT,
    )
    assert evaluator[0][0] == result.evaluation_input.to_wire()


@pytest.mark.parametrize("schema", sorted(SCHEMA_COMPATIBILITY))
def test_only_compatible_schema_maps_to_true(schema: str) -> None:
    result = _run(
        evaluate_plan_admission_checkpoint,
        dataset=_record(observation=_observation(schema_compatibility=schema)),
    )

    assert result.evaluation_input.schema_compatible is (schema == "compatible")


@pytest.mark.parametrize("availability", sorted(EVIDENCE_AVAILABILITY))
@pytest.mark.parametrize("access_permitted", [True, False])
def test_evidence_needs_both_availability_and_access(
    availability: str, access_permitted: bool
) -> None:
    result = _run(
        evaluate_result_publication_checkpoint,
        _Resolver(evidence_access_permitted=access_permitted),
        dataset=_record(observation=_observation(evidence_availability=availability)),
    )

    expected = availability == "available" and access_permitted
    assert result.evaluation_input.evidence_available is expected


@pytest.mark.parametrize(
    ("deadline_offset_us", "freshness_ok"),
    [
        (-1_000_000, True),
        (1_000_000, False),
        (-1_000_000, False),
        (1_000_000, True),
        (None, True),
        (None, False),
    ],
)
def test_resolver_freshness_wins_over_the_stored_deadline(
    deadline_offset_us: int | None, freshness_ok: bool
) -> None:
    deadline = None if deadline_offset_us is None else INSTANT_US + deadline_offset_us
    result = _run(
        evaluate_result_retrieval_checkpoint,
        _Resolver(freshness_ok=freshness_ok),
        dataset=_record(observation=_observation(freshness_deadline_at_us=deadline)),
    )

    assert result.evaluation_input.freshness_ok is freshness_ok


def test_policy_permission_for_partial_or_stale_is_a_separate_input() -> None:
    result = _run(
        evaluate_result_retrieval_checkpoint,
        _Resolver(freshness_ok=False, policy_permits_partial_or_stale=True),
        use_class=USE_EXPLORATION,
    )

    assert result.evaluation_input.freshness_ok is False
    assert result.evaluation_input.policy_permits_partial_or_stale is True
    assert result.decision.outcome == OUTCOME_ALLOW_WITH_WARNING
    assert result.decision.reasons == (
        "freshness_stale_exploration",
        "exploration_non_certifying",
    )


def test_the_current_authority_epoch_reaches_the_evaluator_not_the_observed_one(
    evaluator: list[EvaluatorCall],
) -> None:
    result = _run(evaluate_plan_admission_checkpoint)

    assert EPOCH_OBSERVED != EPOCH_CURRENT
    assert result.evaluation_input.authority_epoch == EPOCH_CURRENT
    assert evaluator[0][0]["authority_epoch"] == EPOCH_CURRENT
    assert result.decision.authority_epoch == EPOCH_CURRENT


def test_the_normalized_query_instant_is_the_one_the_evaluator_receives(
    evaluator: list[EvaluatorCall],
) -> None:
    local = datetime(2026, 10, 4, 11, 0, tzinfo=timezone(timedelta(hours=10)))

    result = _run(evaluate_result_publication_checkpoint, instant=local)

    assert result.authority.query.evaluation_instant == INSTANT
    assert result.authority.query.evaluation_instant.tzinfo is UTC
    assert evaluator[0][1] == INSTANT
    assert evaluator[0][1].tzinfo is UTC
    assert result.decision.valid_until == "2026-10-04T01:00:00Z"


@pytest.mark.parametrize("readiness", sorted(INITIAL_READINESS - {"ready"}))
@pytest.mark.parametrize(("wrapper", "label"), WRAPPERS)
def test_every_non_ready_dataset_refuses_before_the_evaluator_runs(
    readiness: str,
    wrapper: Callable[..., AnalysisResultUseCheckpoint],
    label: str,
    evaluator: list[EvaluatorCall],
) -> None:
    resolver = _Resolver()

    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _run(
            wrapper,
            resolver,
            dataset=_record(observation=_observation(initial_readiness=readiness)),
        )

    assert str(raised.value) == REFUSAL
    assert raised.value.reason == REFUSAL
    assert resolver.calls == 1
    assert evaluator == []


@pytest.mark.parametrize(
    "answer",
    [lambda query: None, _raise],
    ids=["non-snapshot", "resolver-raises"],
)
def test_a_refused_resolver_answer_prevents_the_evaluator(
    answer: Callable[[AnalysisUseAuthorityQuery], Any],
    evaluator: list[EvaluatorCall],
) -> None:
    resolver = _Resolver(answer=answer)

    with pytest.raises(AnalysisUseAuthorityRefused) as raised:
        _run(evaluate_result_publication_checkpoint, resolver)

    assert str(raised.value) == REFUSAL
    assert resolver.calls == 1
    assert evaluator == []


@pytest.mark.parametrize(
    ("use_class", "dataset_overrides", "outcome", "reasons"),
    [
        (
            USE_CURRENT_PUBLICATION,
            {"completeness": "unknown"},
            OUTCOME_DENY,
            ("completeness_unknown",),
        ),
        (
            USE_EXPLORATION,
            {},
            OUTCOME_ALLOW_WITH_WARNING,
            ("exploration_non_certifying",),
        ),
    ],
)
def test_deny_and_warning_outcomes_return_normally(
    use_class: str,
    dataset_overrides: dict[str, Any],
    outcome: str,
    reasons: tuple[str, ...],
) -> None:
    result = _run(
        evaluate_result_publication_checkpoint,
        dataset=_record(observation=_observation(**dataset_overrides)),
        use_class=use_class,
    )

    assert result.decision.outcome == outcome
    assert result.decision.reasons == reasons


def test_a_resolver_rewrite_of_the_dataset_after_resolution_cannot_change_composition(
    evaluator: list[EvaluatorCall],
) -> None:
    dataset = _record()

    def rewrite(query: AnalysisUseAuthorityQuery) -> AnalysisUseAuthoritySnapshot:
        object.__setattr__(
            dataset,
            "observation",
            _observation(
                initial_readiness="blocked",
                completeness="partial",
                continuity="gap_detected",
                schema_compatibility="incompatible",
                evidence_availability="unavailable",
                freshness_deadline_at_us=INSTANT_US - 1_000_000,
            ),
        )
        return _snapshot(query)

    resolver = _Resolver(answer=rewrite)
    result = _run(evaluate_plan_admission_checkpoint, resolver, dataset=dataset)

    assert dataset.observation.initial_readiness == "blocked"
    assert resolver.calls == 1
    assert result.evaluation_input == ResultUseEvaluateInput(
        request_version="1.0",
        use_class=USE_CURRENT_PUBLICATION,
        subject_digest=SUBJECT_DIGEST,
        completeness="complete",
        continuity="verified",
        freshness_ok=True,
        schema_compatible=True,
        evidence_available=True,
        policy_permits_partial_or_stale=False,
        authority_epoch=EPOCH_CURRENT,
    )
    assert evaluator[0][0] == result.evaluation_input.to_wire()


def test_the_checkpoint_is_frozen_slotted_and_keeps_the_exact_objects() -> None:
    resolver = _Resolver()

    result = _run(evaluate_result_retrieval_checkpoint, resolver)

    assert dataclasses.is_dataclass(AnalysisResultUseCheckpoint)
    assert AnalysisResultUseCheckpoint.__dataclass_params__.frozen is True
    assert not hasattr(result, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.checkpoint = "plan_admission"  # type: ignore[misc]
    assert type(result.authority) is AnalysisUseAuthoritySnapshot
    assert result.authority is resolver.answers[0]
    assert result.authority.query is resolver.queries[0]
    assert type(result.evaluation_input) is ResultUseEvaluateInput
    assert type(result.decision) is ResultUseEvaluateResult


def test_an_evaluator_failure_surfaces_as_raised_and_is_not_recast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError(SENTINEL)

    def failing(document: Any, *, evaluation_instant: datetime) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(checkpoints_module, "evaluate_result_use", failing)

    with pytest.raises(RuntimeError) as raised:
        _run(evaluate_plan_admission_checkpoint)

    assert raised.value is error
    assert not isinstance(raised.value, AnalysisUseAuthorityRefused)


def test_a_request_error_from_the_evaluator_is_not_recast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ResultUseRequestError("invalid_request")

    def rejecting(document: Any, *, evaluation_instant: datetime) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(checkpoints_module, "evaluate_result_use", rejecting)

    with pytest.raises(ResultUseRequestError) as raised:
        _run(evaluate_plan_admission_checkpoint)

    assert raised.value is error


def test_an_impossible_evaluator_document_surfaces_as_a_decode_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def malformed(document: Any, *, evaluation_instant: datetime) -> dict[str, Any]:
        return {"outcome": OUTCOME_ALLOW}

    monkeypatch.setattr(checkpoints_module, "evaluate_result_use", malformed)

    with pytest.raises(ContractDecodeError):
        _run(evaluate_plan_admission_checkpoint)
