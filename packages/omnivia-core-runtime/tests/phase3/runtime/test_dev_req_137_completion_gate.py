"""DEV-REQ-137: Runtime-owned final completion, its decision storage, and the scheduler seam.

Final completion is accepted only from a Runtime decision made from accepted criteria and
independently collected evidence. Provider success, `result_kind`, transport status and artefact
existence are never inputs, so the tests below show that a refusal is a refusal whatever the
caller says, that it rolls back the whole final settlement, and that the same claim can later be
settled by valid proof. Intermediate steps are checked to be unchanged.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt106_runtime_scheduler as rt106
from _completion_gate_fixture import (
    COLLECTOR,
    CRITERIA,
    DEFINITION_DIGEST,
    IMPLEMENTER,
    REVIEWER,
    accepted_for,
    digest_for,
    gate,
    item_for,
    proven_readout,
    reader_answering,
)
from omnivia_core_runtime.ownership.fencing import StaleGeneration, fenced_transaction
from omnivia_core_runtime.service.completion_gate import (
    REFUSED_CRITERIA,
    REFUSED_IDENTITY,
    REFUSED_IMPLEMENTER_SELF,
    REFUSED_INCOMPLETE,
    REFUSED_MALFORMED,
    REFUSED_NO_CRITERIA,
    REFUSED_NO_GATE,
    REFUSED_NON_INDEPENDENT,
    REFUSED_STALE_FENCE,
    REFUSED_UNAVAILABLE,
    REFUSED_UNPROVEN,
    AcceptedCompletion,
    CompletionEvidenceUnavailable,
    CompletionGate,
    CompletionRefused,
    EvidenceItem,
    EvidenceReadout,
    SelfEvidenceException,
    decide_completion,
    settle_completion,
)
from omnivia_core_runtime.service.runtime_scheduler import RuntimeScheduler
from omnivia_core_runtime.storage.agent_runtime import append_run_step
from omnivia_core_runtime.storage.completion_decisions import (
    CompletionConflict,
    CompletionDecision,
    CompletionDecisionInvalid,
    read_decision,
    record_decision,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

from omnivia_core.contracts.v1 import to_canonical_json

WORKSPACE_ID = m1.WORKSPACE_ID
OTHER_WORKSPACE_ID = "ws-completion-other-0001"
BASE_US = rt106.BASE_US
RUN = "run-137"
JOB = "job-137"
STEP = "step-137"
ATTEMPT = "attempt-137"
GENERATION = 4
TABLE = "omnivia_runtime_completion_decisions"
POLICY_OWNER = "policy-owner"
_RAW_INSERT = (
    f"INSERT INTO {TABLE} (workspace_id, decision_digest, run_id, decision, decision_body, "
    "decided_under_generation, decided_at_us) VALUES (?, ?, ?, 'accepted', ?, ?, ?)"
)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


# --- Pure rule: `decide_completion` over a readout ----------------------------------------------


def _proven(**changes: Any) -> EvidenceReadout:
    readout = proven_readout(accepted_for(RUN), generation=GENERATION, workspace_id=WORKSPACE_ID)
    return replace(readout, **changes)


def _decide(
    readout: object,
    *,
    accepted: AcceptedCompletion | None = None,
    workspace_id: str = WORKSPACE_ID,
    generation: int = GENERATION,
) -> CompletionDecision:
    return decide_completion(
        accepted_for(RUN) if accepted is None else accepted,
        readout,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        job_id=JOB,
        run_step_id=STEP,
        runtime_attempt_id=ATTEMPT,
        fencing_generation=generation,
    )


def _refusal(
    readout: object,
    *,
    accepted: AcceptedCompletion | None = None,
    workspace_id: str = WORKSPACE_ID,
    generation: int = GENERATION,
) -> str:
    with pytest.raises(CompletionRefused) as raised:
        _decide(readout, accepted=accepted, workspace_id=workspace_id, generation=generation)
    return raised.value.reason


def test_a_fully_proven_observation_is_accepted_with_its_identities_and_evidence() -> None:
    decision = _decide(_proven())

    assert decision.proven_criteria == CRITERIA
    assert [reference.criterion for reference in decision.evidence] == list(CRITERIA)
    assert all(reference.collected_by == COLLECTOR for reference in decision.evidence)
    assert decision.definition_digest == DEFINITION_DIGEST
    assert decision.self_evidence_attributed_to is None
    assert decision.decided_under_generation == GENERATION


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"complete": False}, REFUSED_INCOMPLETE),
        ({"fencing_generation": GENERATION - 1}, REFUSED_STALE_FENCE),
        ({"fencing_generation": GENERATION + 1}, REFUSED_STALE_FENCE),
    ],
    ids=["partial-observation", "evidence-older-than-fence", "evidence-newer-than-fence"],
)
def test_an_incomplete_or_stale_observation_is_refused(
    changes: dict[str, Any], reason: str
) -> None:
    assert _refusal(_proven(**changes)) == reason


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workspace_id", OTHER_WORKSPACE_ID),
        ("run_id", "run-other"),
        ("candidate_id", "candidate-other"),
        ("binding_id", "binding-other"),
        ("definition_digest", digest_for("other-definition")),
        ("aggregate_id", None),
        ("package_id", "package-other"),
        ("application_id", "application-other"),
    ],
    ids=lambda value: str(value)[:24],
)
def test_every_readout_identity_must_match_the_accepted_identity_exactly(
    field: str, value: str | None
) -> None:
    assert _refusal(_proven(**{field: value})) == REFUSED_IDENTITY


def test_the_settlement_workspace_must_match_the_observed_workspace() -> None:
    assert _refusal(_proven(), workspace_id=OTHER_WORKSPACE_ID) == REFUSED_IDENTITY


@pytest.mark.parametrize(
    "items",
    [
        pytest.param(lambda items: items[:1], id="missing"),
        pytest.param(lambda items: (*items, item_for(RUN, "extra-criterion")), id="extra"),
        pytest.param(lambda items: (*items, items[0]), id="duplicate"),
        pytest.param(lambda items: (), id="none"),
    ],
)
def test_the_observation_must_cover_exactly_the_accepted_criteria(
    items: Callable[[tuple[EvidenceItem, ...]], tuple[EvidenceItem, ...]],
) -> None:
    readout = _proven()
    assert _refusal(replace(readout, items=items(readout.items))) == REFUSED_CRITERIA


@pytest.mark.parametrize("outcome", ["failed", "absent"])
def test_an_unproven_accepted_criterion_refuses_the_whole_completion(outcome: str) -> None:
    items = (item_for(RUN, CRITERIA[0], outcome=outcome), item_for(RUN, CRITERIA[1]))
    assert _refusal(_proven(items=items)) == REFUSED_UNPROVEN


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda r: replace(r, workspace_id=""), id="workspace-not-identifier"),
        pytest.param(lambda r: replace(r, workspace_id=None), id="workspace-absent"),
        pytest.param(lambda r: replace(r, fencing_generation=True), id="generation-is-bool"),
        pytest.param(lambda r: replace(r, complete="yes"), id="complete-not-bool"),
        pytest.param(lambda r: replace(r, items=list(r.items)), id="items-not-tuple"),
        pytest.param(lambda r: replace(r, items=({"criterion": "x"},)), id="item-not-evidence"),
        pytest.param(lambda r: replace(r, items=r.items[:1] + ("tests-pass",)), id="item-string"),
        pytest.param(
            lambda r: replace(r, items=(replace(r.items[0], outcome=None), r.items[1])),
            id="outcome-not-string",
        ),
        pytest.param(
            lambda r: replace(r, items=(replace(r.items[0], outcome="maybe"), r.items[1])),
            id="outcome-outside-shape",
        ),
        pytest.param(
            lambda r: replace(r, items=(replace(r.items[0], evidence_id=""), r.items[1])),
            id="evidence-id-empty",
        ),
        pytest.param(
            lambda r: replace(r, items=(replace(r.items[0], content_digest="sha256:XYZ"), r.items[1])),
            id="content-digest-malformed",
        ),
        pytest.param(
            lambda r: replace(r, items=(replace(r.items[0], collected_by="bad name"), r.items[1])),
            id="collector-not-identifier",
        ),
        pytest.param(
            lambda r: replace(r, items=(replace(r.items[0], criterion="Bad Name"), r.items[1])),
            id="criterion-not-identifier",
        ),
    ],
)
def test_a_malformed_observation_is_refused_by_name_before_any_comparison(
    change: Callable[[EvidenceReadout], object],
) -> None:
    assert _refusal(change(_proven())) == REFUSED_MALFORMED


def test_a_reader_that_returns_something_other_than_a_readout_is_refused() -> None:
    assert _refusal({"complete": True}) == REFUSED_MALFORMED


def test_the_collector_and_reviewer_must_be_different_parties() -> None:
    items = (item_for(RUN, CRITERIA[0], collected_by=COLLECTOR, reviewed_by=COLLECTOR), item_for(RUN, CRITERIA[1]))
    assert _refusal(_proven(items=items)) == REFUSED_NON_INDEPENDENT


def test_the_reviewer_must_be_independent_of_the_implementer() -> None:
    items = (item_for(RUN, CRITERIA[0], reviewed_by=IMPLEMENTER), item_for(RUN, CRITERIA[1]))
    assert _refusal(_proven(items=items)) == REFUSED_NON_INDEPENDENT


def test_the_implementer_may_not_collect_evidence_without_the_explicit_exception() -> None:
    items = (item_for(RUN, CRITERIA[0], collected_by=IMPLEMENTER), item_for(RUN, CRITERIA[1]))
    assert _refusal(_proven(items=items)) == REFUSED_IMPLEMENTER_SELF


def test_the_self_evidence_exception_covers_only_its_named_criterion() -> None:
    accepted = accepted_for(RUN, self_evidence=SelfEvidenceException(CRITERIA[1], POLICY_OWNER))
    items = (item_for(RUN, CRITERIA[0], collected_by=IMPLEMENTER), item_for(RUN, CRITERIA[1]))

    assert _refusal(_proven(items=items), accepted=accepted) == REFUSED_IMPLEMENTER_SELF


def test_the_self_evidence_exception_is_accepted_and_attributed_when_one_item_is_independent() -> None:
    accepted = accepted_for(RUN, self_evidence=SelfEvidenceException(CRITERIA[0], POLICY_OWNER))
    items = (item_for(RUN, CRITERIA[0], collected_by=IMPLEMENTER), item_for(RUN, CRITERIA[1]))

    decision = _decide(_proven(items=items), accepted=accepted)

    assert decision.self_evidence_attributed_to == POLICY_OWNER
    assert {reference.criterion: reference.collected_by for reference in decision.evidence} == {
        CRITERIA[0]: IMPLEMENTER,
        CRITERIA[1]: COLLECTOR,
    }


def test_a_single_self_collected_criterion_is_not_enough_even_with_an_exception_and_a_reviewer() -> None:
    accepted = accepted_for(
        RUN,
        criteria=(CRITERIA[0],),
        self_evidence=SelfEvidenceException(CRITERIA[0], POLICY_OWNER),
    )
    items = (item_for(RUN, CRITERIA[0], collected_by=IMPLEMENTER, reviewed_by=REVIEWER),)
    readout = replace(
        proven_readout(accepted, generation=GENERATION, workspace_id=WORKSPACE_ID), items=items
    )

    assert _refusal(readout, accepted=accepted) == REFUSED_IMPLEMENTER_SELF


@pytest.mark.parametrize(
    "kwargs",
    [
        {"criteria": ("tests-pass", "artefact-verified")},
        {"criteria": ()},
        {"self_evidence": SelfEvidenceException("not-accepted", POLICY_OWNER)},
        {"definition_digest": "sha256:short"},
        {"run_id": "not a run id"},
    ],
    ids=["unsorted", "empty", "exception-not-accepted", "digest-malformed", "run-id-malformed"],
)
def test_accepted_completion_refuses_a_definition_outside_its_closed_shape(
    kwargs: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        replace(accepted_for(RUN), **kwargs)


# --- Settlement seam: absent authority fails closed -------------------------------------------


def _settle(connection: sqlite3.Connection, gate_: CompletionGate | None, *, run: str = RUN) -> None:
    settle_completion(
        connection,
        gate_,
        workspace_id=WORKSPACE_ID,
        run_id=run,
        job_id=JOB,
        run_step_id=STEP,
        runtime_attempt_id=ATTEMPT,
        fencing_generation=GENERATION,
        decided_at_us=BASE_US,
    )


def test_no_configured_gate_refuses_every_final_settlement(owned: m1.Owned) -> None:
    with pytest.raises(CompletionRefused) as raised:
        _settle(owned.connection, None)
    assert raised.value.reason == REFUSED_NO_GATE


def test_a_run_without_accepted_criteria_is_refused(owned: m1.Owned) -> None:
    with pytest.raises(CompletionRefused) as raised:
        _settle(owned.connection, CompletionGate(reader=gate().reader, accepted=lambda _run: None))
    assert raised.value.reason == REFUSED_NO_CRITERIA

    other_run = CompletionGate(reader=gate().reader, accepted=lambda _run: accepted_for("run-other"))
    with pytest.raises(CompletionRefused) as raised:
        _settle(owned.connection, other_run)
    assert raised.value.reason == REFUSED_NO_CRITERIA


def test_an_unreachable_evidence_boundary_is_refused_not_defaulted(owned: m1.Owned) -> None:
    class Unreachable:
        def read(self, *_args: object, **_kwargs: object) -> EvidenceReadout:
            raise CompletionEvidenceUnavailable("evidence store is down")

    with pytest.raises(CompletionRefused) as raised:
        _settle(owned.connection, CompletionGate(reader=Unreachable(), accepted=accepted_for))  # type: ignore[arg-type]
    assert raised.value.reason == REFUSED_UNAVAILABLE


# --- Scheduler: provider success cannot bypass, refusal rolls back, claim stays retryable ------


def _scheduler(owned: m1.Owned, completion: CompletionGate | None) -> RuntimeScheduler:
    return RuntimeScheduler(
        owned.connection,
        owned.identity,
        WORKSPACE_ID,
        owned.generation,
        m1.FakeClock(wall=datetime.fromtimestamp((BASE_US + 1_000) / 1_000_000, UTC)),
        completion=completion,
    )


def _state(owned: m1.Owned, claim: Any) -> tuple[Any, ...]:
    """The rows a final settlement touches: job, step, attempt outcome, decisions and run events."""
    connection = owned.connection
    return (
        connection.execute(
            "SELECT state FROM omnivia_durable_jobs WHERE job_id = ?", (claim.job_id,)
        ).fetchone()[0],
        connection.execute(
            "SELECT status FROM omnivia_runtime_run_step_states WHERE run_step_id = ? "
            "ORDER BY state_sequence DESC LIMIT 1",
            (claim.run_step_id,),
        ).fetchone()[0],
        connection.execute(
            "SELECT COUNT(*) FROM omnivia_runtime_attempt_outcomes WHERE attempt_id = ?",
            (claim.runtime_attempt_id,),
        ).fetchone()[0],
        connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0],
        connection.execute(
            "SELECT COUNT(*) FROM omnivia_runtime_events WHERE run_id = ? AND run_status = 'succeeded'",
            (claim.run_id,),
        ).fetchone()[0],
    )


OPEN_STATE = ("claimed", "running", 0, 0, 0)
SETTLED_STATE = ("succeeded", "succeeded", 1, 1, 1)


def test_no_configured_gate_leaves_the_claim_open_and_a_later_proof_settles_it(
    owned: m1.Owned,
) -> None:
    rt106._seed_run(owned, job_id=JOB, run_id="run-nogate", step_id="step-nogate")
    claim = _scheduler(owned, None).claim_next()
    assert claim is not None

    with pytest.raises(CompletionRefused) as raised:
        _scheduler(owned, None).complete(claim, result_kind="runtime_completion", result={"ok": True})
    assert raised.value.reason == REFUSED_NO_GATE
    assert _state(owned, claim) == OPEN_STATE

    assert _scheduler(owned, gate()).complete(
        claim, result_kind="runtime_completion", result={"ok": True}
    ) is None
    assert _state(owned, claim) == SETTLED_STATE


def test_provider_success_cannot_bypass_an_unproven_gate(owned: m1.Owned) -> None:
    rt106._seed_run(owned, job_id="job-provider", run_id="run-provider", step_id="step-provider")
    claim = _scheduler(owned, None).claim_next()
    assert claim is not None
    refusing = gate(
        reader_answering(lambda readout: replace(readout, complete=False))
    )

    for result_kind, result in (
        ("runtime_completion", {"ok": True}),
        ("provider_succeeded", {"artefact": "written", "transport": "200"}),
    ):
        with pytest.raises(CompletionRefused) as raised:
            _scheduler(owned, refusing).complete(claim, result_kind=result_kind, result=result)
        assert raised.value.reason == REFUSED_INCOMPLETE
    assert _state(owned, claim) == OPEN_STATE


def test_a_refused_final_settlement_rolls_back_and_the_same_claim_is_retryable(
    owned: m1.Owned,
) -> None:
    rt106._seed_run(owned, job_id="job-retry", run_id="run-retry", step_id="step-retry")
    claim = _scheduler(owned, None).claim_next()
    assert claim is not None
    unproven = gate(
        reader_answering(
            lambda readout: replace(
                readout,
                items=(replace(readout.items[0], outcome="failed"), readout.items[1]),
            )
        )
    )

    with pytest.raises(CompletionRefused) as raised:
        _scheduler(owned, unproven).complete(claim, result_kind="runtime_completion", result={"ok": True})
    assert raised.value.reason == REFUSED_UNPROVEN
    assert _state(owned, claim) == OPEN_STATE

    assert _scheduler(owned, gate()).complete(
        claim, result_kind="runtime_completion", result={"ok": True}
    ) is None
    assert _state(owned, claim) == SETTLED_STATE


def test_an_intermediate_step_settles_without_any_decision_and_only_the_final_step_needs_proof(
    owned: m1.Owned,
) -> None:
    rt106._seed_run(owned, job_id="job-two", run_id="run-two", step_id="step-two-a")
    append_run_step(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        run_id="run-two",
        run_step_id="step-two-b",
        ordinal=2,
        step_kind="plan",
        created_at_us=BASE_US,
    )
    first = _scheduler(owned, None).claim_next()
    assert first is not None and first.run_step_id == "step-two-a"

    second = _scheduler(owned, None).complete(
        first, result_kind="runtime_completion", result={"ok": True}
    )

    assert second is not None and second.run_step_id == "step-two-b"
    assert owned.connection.execute(
        "SELECT status FROM omnivia_runtime_run_step_states WHERE run_step_id = 'step-two-a' "
        "ORDER BY state_sequence DESC LIMIT 1"
    ).fetchone() == ("succeeded",)
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone() == (0,)
    with pytest.raises(CompletionRefused) as raised:
        _scheduler(owned, None).complete(second, result_kind="runtime_completion", result={"ok": True})
    assert raised.value.reason == REFUSED_NO_GATE


def test_a_valid_final_settlement_is_atomic_and_its_event_carries_the_decision_digest(
    owned: m1.Owned,
) -> None:
    rt106._seed_run(owned, job_id="job-event", run_id="run-event", step_id="step-event")
    claim = _scheduler(owned, None).claim_next()
    assert claim is not None

    _scheduler(owned, gate()).complete(claim, result_kind="runtime_completion", result={"ok": True})

    stored = read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id)
    assert stored is not None
    assert stored.decision.job_id == claim.job_id
    assert stored.decision.run_step_id == claim.run_step_id
    assert stored.decision.runtime_attempt_id == claim.runtime_attempt_id
    assert stored.decision.decided_under_generation == owned.generation
    event = owned.connection.execute(
        "SELECT details_json FROM omnivia_runtime_events WHERE run_id = ? "
        "AND run_status = 'succeeded'",
        (claim.run_id,),
    ).fetchone()
    assert json.loads(event[0])["completion_decision_digest"] == stored.decision_digest


_FRESH_READER = """
import json, sys
from pathlib import Path
from omnivia_core_runtime.storage.completion_decisions import read_decision
from omnivia_core_runtime.storage.connection import OpenMode, open_database
connection = open_database(Path(sys.argv[1]), OpenMode.READ_ONLY)
stored = read_decision(connection, workspace_id=sys.argv[2], run_id=sys.argv[3])
d = stored.decision
print(json.dumps([stored.decision_digest, stored.decided_at_us, list(d.proven_criteria),
                  [[e.criterion, e.evidence_id, e.collected_by, e.reviewed_by] for e in d.evidence]]))
"""


def test_the_accepted_decision_is_durable_and_readable_from_a_fresh_connection(
    owned: m1.Owned,
) -> None:
    rt106._seed_run(owned, job_id="job-fresh", run_id="run-fresh", step_id="step-fresh")
    claim = _scheduler(owned, None).claim_next()
    assert claim is not None
    _scheduler(owned, gate()).complete(claim, result_kind="runtime_completion", result={"ok": True})
    written = read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id)
    assert written is not None
    path = owned.path
    owned.connection.close()

    completed = subprocess.run(
        [sys.executable, "-c", _FRESH_READER, str(path), WORKSPACE_ID, claim.run_id],
        capture_output=True,
        text=True,
        check=True,
    )

    digest, decided_at, criteria, evidence = json.loads(completed.stdout)
    assert digest == written.decision_digest
    assert decided_at == written.decided_at_us
    assert criteria == list(CRITERIA)
    assert evidence == [
        [criterion, f"evidence-{claim.run_id}-{criterion}", COLLECTOR, REVIEWER]
        for criterion in CRITERIA
    ]


# --- Storage: replay, conflict, tamper, and the schema's own guards ---------------------------


def _decision(owned: m1.Owned, *, run: str = RUN, readout: EvidenceReadout | None = None) -> CompletionDecision:
    accepted = accepted_for(run)
    observed = readout or proven_readout(accepted, generation=owned.generation, workspace_id=WORKSPACE_ID)
    return decide_completion(
        accepted,
        observed,
        workspace_id=WORKSPACE_ID,
        job_id=JOB,
        run_step_id=STEP,
        runtime_attempt_id=ATTEMPT,
        fencing_generation=owned.generation,
    )


def _record(owned: m1.Owned, decision: CompletionDecision, *, decided_at_us: int = BASE_US) -> Any:
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as fenced:
        return record_decision(fenced, decision=decision, decided_at_us=decided_at_us)


def _count(owned: m1.Owned) -> int:
    return int(owned.connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])


def test_an_exact_replay_dedups_to_the_stored_row_and_keeps_its_first_recorded_time(
    owned: m1.Owned,
) -> None:
    decision = _decision(owned)
    first = _record(owned, decision, decided_at_us=BASE_US)

    replayed = _record(owned, decision, decided_at_us=BASE_US + 5)

    assert replayed.decision_digest == first.decision_digest == decision.decision_digest
    assert replayed.decided_at_us == BASE_US
    assert _count(owned) == 1


def test_a_different_decision_for_the_same_run_conflicts_and_writes_nothing(owned: m1.Owned) -> None:
    first = _decision(owned)
    _record(owned, first)
    changed = proven_readout(accepted_for(RUN), generation=owned.generation, workspace_id=WORKSPACE_ID)
    other = decide_completion(
        accepted_for(RUN),
        replace(
            changed,
            items=(
                item_for(RUN, CRITERIA[0], reviewed_by="another-reviewer"),
                changed.items[1],
            ),
        ),
        workspace_id=WORKSPACE_ID,
        job_id=JOB,
        run_step_id=STEP,
        runtime_attempt_id=ATTEMPT,
        fencing_generation=owned.generation,
    )

    with pytest.raises(CompletionConflict):
        _record(owned, other)

    assert _count(owned) == 1
    stored = read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN)
    assert stored is not None and stored.decision_digest == first.decision_digest


def test_a_stale_writer_cannot_record_a_decision_after_takeover(owned: m1.Owned) -> None:
    decision = _decision(owned)
    rt106._takeover(owned)

    with pytest.raises(StaleGeneration):
        _record(owned, decision)
    assert _count(owned) == 0


def test_a_decision_outside_its_closed_shape_is_refused_before_storage(owned: m1.Owned) -> None:
    with pytest.raises(CompletionDecisionInvalid):
        _record(owned, _decision(owned), decided_at_us=0)
    assert _count(owned) == 0


_MUTATIONS: dict[str, Callable[[dict[str, Any]], None]] = {
    "unknown-top-level-key": lambda body: body.update(extra=1),
    "unknown-evidence-key": lambda body: body["evidence"][0].update(extra="x"),
    "bool-as-generation": lambda body: body.update(decided_under_generation=True),
    "string-where-criteria-array": lambda body: body.update(proven_criteria=CRITERIA[0]),
    "string-where-evidence-array": lambda body: body.update(evidence="artefact-verified"),
    "number-where-criteria-array": lambda body: body.update(proven_criteria=7),
    "duplicate-criteria": lambda body: body.update(proven_criteria=[CRITERIA[0], CRITERIA[0]]),
    "unsorted-criteria": lambda body: body.update(proven_criteria=list(reversed(CRITERIA))),
    "unproven-criteria-present": lambda body: body.update(unproven_criteria=[CRITERIA[1]]),
    "string-where-unproven-array": lambda body: body.update(unproven_criteria="tests-pass"),
    "missing-key": lambda body: body.pop("job_id"),
    "decision-not-accepted": lambda body: body.update(decision="rejected"),
    "body-run-differs-from-row": lambda body: body.update(run_id="run-other"),
    "evidence-is-not-a-list-of-objects": lambda body: body.update(evidence=[CRITERIA[0]]),
}


@pytest.mark.parametrize("mutation", sorted(_MUTATIONS), ids=sorted(_MUTATIONS))
def test_a_stored_body_outside_its_closed_shape_is_refused_on_read(
    owned: m1.Owned, mutation: str
) -> None:
    decision = _decision(owned)
    body = json.loads(to_canonical_json(decision.to_body()))
    _MUTATIONS[mutation](body)
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as fenced:
        fenced.execute(
            _RAW_INSERT,
            (WORKSPACE_ID, decision.decision_digest, RUN, json.dumps(body), owned.generation, BASE_US),
        )

    with pytest.raises(CompletionDecisionInvalid):
        read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN)


def test_a_stored_body_that_is_not_canonical_or_does_not_match_its_digest_is_refused(
    owned: m1.Owned,
) -> None:
    decision = _decision(owned)
    pretty = json.dumps(json.loads(to_canonical_json(decision.to_body())), indent=2)
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as fenced:
        fenced.execute(
            _RAW_INSERT,
            (WORKSPACE_ID, decision.decision_digest, RUN, pretty, owned.generation, BASE_US),
        )
    with pytest.raises(CompletionDecisionInvalid, match="does not verify its digest"):
        read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN)


def test_a_stored_digest_column_that_does_not_name_its_body_is_refused(owned: m1.Owned) -> None:
    decision = _decision(owned)
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as fenced:
        fenced.execute(
            _RAW_INSERT,
            (
                WORKSPACE_ID,
                digest_for("not-this-body"),
                RUN,
                to_canonical_json(decision.to_body()),
                owned.generation,
                BASE_US,
            ),
        )
    with pytest.raises(CompletionDecisionInvalid, match="does not verify its digest"):
        read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN)


def test_the_schema_refuses_an_unguarded_write_a_foreign_binding_and_any_update_or_delete(
    owned: m1.Owned,
) -> None:
    decision = _decision(owned)
    body = to_canonical_json(decision.to_body())

    # The connection's authorizer refuses a raw write before the table's guard trigger runs.
    with pytest.raises(sqlite3.DatabaseError, match="unguarded INSERT|not authorized"):
        owned.connection.execute(
            _RAW_INSERT,
            (WORKSPACE_ID, decision.decision_digest, RUN, body, owned.generation, BASE_US),
        )

    with pytest.raises(sqlite3.DatabaseError, match="must bind the open workspace"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as fenced:
        fenced.execute(
            _RAW_INSERT,
            (OTHER_WORKSPACE_ID, decision.decision_digest, RUN, body, owned.generation, BASE_US),
        )

    with pytest.raises(sqlite3.DatabaseError, match="current fencing generation"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as fenced:
        fenced.execute(
            _RAW_INSERT,
            (WORKSPACE_ID, decision.decision_digest, RUN, body, owned.generation + 1, BASE_US),
        )

    _record(owned, decision)
    for statement in (f"UPDATE {TABLE} SET decision_body = decision_body", f"DELETE FROM {TABLE}"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"), fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as fenced:
            fenced.execute(statement)
    assert _count(owned) == 1


def test_read_back_returns_the_recorded_decision_it_was_written_as(owned: m1.Owned) -> None:
    written = _record(owned, _decision(owned))

    assert read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN) == written
