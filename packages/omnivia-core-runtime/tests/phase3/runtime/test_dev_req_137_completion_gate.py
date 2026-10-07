"""DEV-REQ-137: Runtime-owned final completion, its decision storage, and the scheduler seam.

Final completion is accepted only from a Runtime decision made from accepted criteria and
independently collected evidence. Provider success, `result_kind`, transport status and artefact
existence are never inputs, so the tests below show that a refusal is a refusal whatever the
caller says, that it rolls back the whole final settlement, and that the same claim can later be
settled by valid proof. Intermediate steps are checked to be unchanged.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt106_runtime_scheduler as rt106
from _completion_gate_fixture import (
    COLLECTOR,
    CRITERIA,
    DEFINITION_DIGEST,
    IMPLEMENTER,
    REVIEWER,
    ScriptedReader,
    accepted_for,
    digest_for,
    gate,
    item_for,
    proven_readout,
    reader_answering,
)
from omnivia_core_runtime.ownership.fencing import StaleGeneration, fenced_transaction
from omnivia_core_runtime.service import runtime_scheduler as scheduler_module
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
    IndependentEvidenceReader,
    SelfEvidenceException,
    decide_completion,
    settle_completion,
)
from omnivia_core_runtime.service.jobs import _terminalize_application_job
from omnivia_core_runtime.service.runtime_scheduler import (
    RuntimeClaim,
    RuntimeScheduler,
)
from omnivia_core_runtime.storage.agent_runtime import (
    append_run_step,
    transaction_local_writer,
)
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
#: The instant a hand-closed attempt, its decision and its event share. After the attempt started.
DECIDED_US = BASE_US + 2_000
RUN = "run-137"
JOB = "job-137"
STEP = "step-137"
ATTEMPT = "attempt-137"
GENERATION = 4
APPLICATION_ATTEMPT = 1
TABLE = "omnivia_runtime_completion_decisions"
POLICY_OWNER = "policy-owner"
SETTLED_SEQUENCE = 2
SETTLED = "run_succeeded"
SUCCEEDED = "succeeded"
OPEN_STATE = ("claimed", "running", 0, 0, 0)
SETTLED_STATE = ("succeeded", "succeeded", 1, 1, 1)
#: A hand-closed attempt and step, with no decision, event or job terminalization yet.
CLOSED_STATE = ("claimed", "succeeded", 1, 0, 0)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


# --- Pure rule: `decide_completion` over a readout ----------------------------------------------

_GETTER_CALLS: list[str] = []


def _proven(**changes: Any) -> EvidenceReadout:
    readout = proven_readout(accepted_for(RUN), generation=GENERATION, workspace_id=WORKSPACE_ID)
    return replace(readout, **changes)


def _decide(
    readout: object,
    *,
    accepted: object | None = None,
    workspace_id: str = WORKSPACE_ID,
    generation: int = GENERATION,
) -> CompletionDecision:
    return decide_completion(
        accepted_for(RUN) if accepted is None else accepted,  # type: ignore[arg-type]
        readout,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        job_id=JOB,
        run_step_id=STEP,
        runtime_attempt_id=ATTEMPT,
        application_attempt_number=APPLICATION_ATTEMPT,
        fencing_generation=generation,
        settled_sequence=SETTLED_SEQUENCE,
    )


def _refusal(
    readout: object,
    *,
    accepted: object | None = None,
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
    assert decision.application_attempt_number == APPLICATION_ATTEMPT


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"complete": False}, REFUSED_INCOMPLETE),
        ({"fencing_generation": GENERATION - 1}, REFUSED_STALE_FENCE),
        ({"fencing_generation": GENERATION + 1}, REFUSED_STALE_FENCE),
    ],
    ids=["partial-observation", "evidence-older-than-fence", "evidence-newer-than-fence"],
)
def test_an_incomplete_or_stale_observation_is_refused(changes: dict[str, Any], reason: str) -> None:
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


class _HostileEquality:
    """A non-string whose equality is user code. Any comparison of it is recorded and answers yes."""

    calls: ClassVar[list[str]] = []

    def __eq__(self, other: object) -> bool:
        _HostileEquality.calls.append("eq")
        return True

    def __ne__(self, other: object) -> bool:
        _HostileEquality.calls.append("ne")
        return False

    def __hash__(self) -> int:
        return 0


class _StringWithEquality(str):
    """A `str` subclass whose equality is user code, which an isinstance check alone would admit."""

    def __eq__(self, other: object) -> bool:
        _HostileEquality.calls.append("str-eq")
        return True

    def __ne__(self, other: object) -> bool:
        _HostileEquality.calls.append("str-ne")
        return False

    def __hash__(self) -> int:
        return hash(str(self))


@pytest.mark.parametrize(
    "field",
    [
        "workspace_id",
        "run_id",
        "candidate_id",
        "binding_id",
        "definition_digest",
        "aggregate_id",
        "package_id",
        "application_id",
    ],
)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(_HostileEquality(), id="hostile-object"),
        pytest.param(_StringWithEquality("candidate-run-137"), id="str-subclass"),
        pytest.param(42, id="integer"),
        pytest.param(b"candidate-run-137", id="bytes"),
        pytest.param(("candidate-run-137",), id="tuple"),
        pytest.param("not an identifier", id="string-outside-shape"),
    ],
)
def test_a_malformed_identity_is_refused_before_any_equality_is_invoked(
    field: str, value: object
) -> None:
    _HostileEquality.calls.clear()
    assert _refusal(_proven(**{field: value})) == REFUSED_MALFORMED
    assert _HostileEquality.calls == []


@pytest.mark.parametrize(
    "value",
    ["sha256:not-a-digest", "sha256:" + "A" * 64, "md5:" + "a" * 32],
    ids=["non-hex", "upper-case", "wrong-algorithm"],
)
def test_a_definition_digest_outside_its_shape_is_refused_as_malformed(value: str) -> None:
    assert _refusal(_proven(definition_digest=value)) == REFUSED_MALFORMED


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
    items = (
        item_for(RUN, CRITERIA[0], collected_by=COLLECTOR, reviewed_by=COLLECTOR),
        item_for(RUN, CRITERIA[1]),
    )
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


# --- Exact types and one snapshot: hostile subclasses are refused before any of their code runs ---


class _EqualToAnything(int):
    """An int whose equality answers yes to everything, so an `isinstance` check admits it as a generation."""

    def __eq__(self, other: object) -> bool:
        _GETTER_CALLS.append("int-eq")
        return True

    def __ne__(self, other: object) -> bool:
        _GETTER_CALLS.append("int-ne")
        return False

    def __hash__(self) -> int:
        return 0


class _RecordingReadout(EvidenceReadout):
    def __getattribute__(self, name: str) -> Any:
        if not name.startswith("__"):
            _GETTER_CALLS.append(name)
        return super().__getattribute__(name)


class _CountingItems(tuple):  # type: ignore[type-arg]
    def __iter__(self) -> Any:
        _GETTER_CALLS.append("iter")
        return super().__iter__()


class _RecordingItem(EvidenceItem):
    def __getattribute__(self, name: str) -> Any:
        if not name.startswith("__"):
            _GETTER_CALLS.append(name)
        return super().__getattribute__(name)


class _RecordingAccepted(AcceptedCompletion):
    def __getattribute__(self, name: str) -> Any:
        if not name.startswith("__"):
            _GETTER_CALLS.append(name)
        return super().__getattribute__(name)


def _as_subclass(cls: type, base: object) -> Any:
    fields = {field.name: getattr(base, field.name) for field in dataclasses.fields(base)}  # type: ignore[arg-type]
    return cls(**fields)


def test_an_int_subclass_fencing_generation_is_refused_by_type_before_its_equality_runs() -> None:
    _GETTER_CALLS.clear()
    readout = _proven(fencing_generation=_EqualToAnything(999))

    assert _refusal(readout) == REFUSED_MALFORMED
    assert _GETTER_CALLS == []


def test_an_int_subclass_settlement_generation_is_refused_before_any_comparison() -> None:
    _GETTER_CALLS.clear()
    with pytest.raises(CompletionRefused) as raised:
        _decide(_proven(), generation=_EqualToAnything(GENERATION))  # type: ignore[arg-type]
    assert raised.value.reason == REFUSED_MALFORMED
    assert _GETTER_CALLS == []


def test_an_evidence_readout_subclass_is_refused_before_any_getter_runs() -> None:
    hostile = _as_subclass(_RecordingReadout, _proven())
    _GETTER_CALLS.clear()

    assert _refusal(hostile) == REFUSED_MALFORMED
    assert _GETTER_CALLS == []


def test_a_tuple_subclass_of_items_is_refused_before_it_is_iterated() -> None:
    base = _proven()
    hostile = replace(base, items=_CountingItems(base.items))
    _GETTER_CALLS.clear()

    assert _refusal(hostile) == REFUSED_MALFORMED
    assert _GETTER_CALLS == []


def test_an_evidence_item_subclass_is_refused_before_its_stateful_getters_are_read() -> None:
    base = _proven()
    hostile_item = _as_subclass(_RecordingItem, base.items[0])
    _GETTER_CALLS.clear()

    assert _refusal(replace(base, items=(hostile_item, base.items[1]))) == REFUSED_MALFORMED
    assert _GETTER_CALLS == []


def test_an_accepted_completion_subclass_is_refused_before_its_getters_are_read() -> None:
    hostile = _as_subclass(_RecordingAccepted, accepted_for(RUN))
    _GETTER_CALLS.clear()

    assert _refusal(_proven(), accepted=hostile) == REFUSED_MALFORMED
    assert _GETTER_CALLS == []


def test_a_gate_that_returns_an_accepted_subclass_is_refused_and_writes_nothing(owned: m1.Owned) -> None:
    hostile = _as_subclass(_RecordingAccepted, accepted_for(RUN))
    _GETTER_CALLS.clear()

    with pytest.raises(CompletionRefused) as raised:
        _settle(owned.connection, CompletionGate(reader=ScriptedReader(), accepted=lambda _run: hostile))  # type: ignore[arg-type,return-value]
    assert raised.value.reason == REFUSED_MALFORMED
    assert _GETTER_CALLS == []
    assert _count(owned) == 0


# --- Settlement seam: absent authority fails closed, and the reader sees only the fence ----------


def _settle(
    connection: sqlite3.Connection,
    gate_: CompletionGate | None,
    *,
    run: str = RUN,
    application_attempt_number: int = APPLICATION_ATTEMPT,
) -> None:
    settle_completion(
        connection,
        gate_,
        workspace_id=WORKSPACE_ID,
        run_id=run,
        job_id=JOB,
        run_step_id=STEP,
        runtime_attempt_id=ATTEMPT,
        application_attempt_number=application_attempt_number,
        fencing_generation=GENERATION,
        decided_at_us=DECIDED_US,
        service_instance_id="svc-settle-helper",
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
        def read(self, **_kwargs: object) -> EvidenceReadout:
            raise CompletionEvidenceUnavailable("evidence store is down")

    with pytest.raises(CompletionRefused) as raised:
        _settle(owned.connection, CompletionGate(reader=Unreachable(), accepted=accepted_for))  # type: ignore[arg-type]
    assert raised.value.reason == REFUSED_UNAVAILABLE


def test_the_reader_is_handed_the_identifiers_and_the_fence_and_never_the_connection(
    owned: m1.Owned,
) -> None:
    claim = _claim(owned, "run-boundary")
    reader = ScriptedReader()

    _scheduler(owned, CompletionGate(reader=reader, accepted=accepted_for)).complete(
        claim, result_kind="runtime_completion", result={"ok": True}
    )

    assert reader.calls == [(WORKSPACE_ID, claim.run_id, owned.generation)]
    assert list(inspect.signature(IndependentEvidenceReader.read).parameters) == [
        "self",
        "workspace_id",
        "run_id",
        "fencing_generation",
    ]


class _Raising:
    """A reader that fails in a way the boundary does not name as unavailable."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def read(self, **_kwargs: object) -> EvidenceReadout:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError("the reader failed in an unexpected way"), id="unexpected"),
        pytest.param(sqlite3.OperationalError("no such table: evidence_rows"), id="missing-table"),
    ],
)
def test_an_unexpected_reader_failure_propagates_and_leaves_the_claim_open(
    owned: m1.Owned, error: BaseException
) -> None:
    claim = _claim(owned, "run-unexpected")

    with pytest.raises(type(error)):
        _scheduler(owned, CompletionGate(reader=_Raising(error), accepted=accepted_for)).complete(  # type: ignore[arg-type]
            claim, result_kind="runtime_completion", result={"ok": True}
        )
    assert _state(owned, claim) == OPEN_STATE
    assert not owned.connection.in_transaction


# --- Scheduler: provider success cannot bypass, refusal rolls back, claim stays retryable ------


def _scheduler(owned: m1.Owned, completion: CompletionGate | None) -> RuntimeScheduler:
    return RuntimeScheduler(
        owned.connection,
        owned.identity,
        WORKSPACE_ID,
        owned.generation,
        _clock(),
        completion=completion,
    )


def _workflow_scheduler(owned: m1.Owned, completion: CompletionGate | None) -> RuntimeScheduler:
    return RuntimeScheduler(
        owned.connection,
        owned.identity,
        WORKSPACE_ID,
        owned.generation,
        _clock(),
        completion=completion,
        requires_completion=True,
    )


def _clock() -> m1.FakeClock:
    return m1.FakeClock(wall=datetime.fromtimestamp((BASE_US + 1_000) / 1_000_000, UTC))


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


def _ledger(owned: m1.Owned) -> tuple[Any, ...]:
    """Every table a claim or a settlement writes, counted and with each job's state."""
    connection = owned.connection
    counts = tuple(
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "omnivia_job_attempts",
            "omnivia_job_events",
            "omnivia_job_terminal_observations",
            "omnivia_runtime_attempts",
            "omnivia_runtime_run_step_states",
            "omnivia_runtime_events",
            "omnivia_runtime_attempt_outcomes",
            TABLE,
        )
    )
    states = tuple(
        connection.execute("SELECT job_id, state FROM omnivia_durable_jobs ORDER BY job_id").fetchall()
    )
    return (counts, states)


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
    refusing = gate(reader_answering(lambda readout: replace(readout, complete=False)))

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


def test_a_workflow_scheduler_without_a_gate_refuses_to_claim_before_any_write(
    owned: m1.Owned,
) -> None:
    rt106._seed_run(owned, job_id="job-refused", run_id="run-refused", step_id="step-refused")
    before = _ledger(owned)

    with pytest.raises(CompletionRefused) as raised:
        _workflow_scheduler(owned, None).claim_next()

    assert raised.value.reason == REFUSED_NO_GATE
    assert str(raised.value) == "workflow scheduling requires a configured completion gate (no_completion_gate)"
    assert _ledger(owned) == before
    assert not owned.connection.in_transaction


def test_the_unconfigured_workflow_refusal_survives_a_restart_and_still_writes_nothing(
    owned: m1.Owned,
) -> None:
    rt106._seed_run(owned, job_id="job-restart", run_id="run-restart", step_id="step-restart")
    before = _ledger(owned)
    path = owned.path
    owned.connection.close()

    restarted = m1.take_ownership(path)
    try:
        with pytest.raises(CompletionRefused) as raised:
            _workflow_scheduler(restarted, None).claim_next()
        assert raised.value.reason == REFUSED_NO_GATE
        assert _ledger(restarted) == before
        # The configured process still claims the same job afterwards: nothing was stranded.
        assert _workflow_scheduler(restarted, gate()).claim_next() is not None
    finally:
        restarted.connection.close()


def test_a_workflow_scheduler_without_a_gate_refuses_to_settle_a_claim_before_any_write(
    owned: m1.Owned,
) -> None:
    rt106._seed_run(owned, job_id="job-held", run_id="run-held", step_id="step-held")
    claim = _workflow_scheduler(owned, gate()).claim_next()
    assert claim is not None
    before = _ledger(owned)

    with pytest.raises(CompletionRefused) as raised:
        _workflow_scheduler(owned, None).complete(claim, result_kind="runtime_completion", result={"ok": True})

    assert raised.value.reason == REFUSED_NO_GATE
    assert _ledger(owned) == before


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
    assert stored.decision.application_attempt_number == claim.application_attempt_number
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


# --- Closure: one order, one pairing, atomic --------------------------------------------------------


def _seed(owned: m1.Owned, run: str, *, steps: int = 1) -> None:
    """A real run with `steps` steps, admitted and waiting for a claim, as the scheduler reads it."""
    rt106._seed_run(owned, job_id=f"job-{run}", run_id=run, step_id=f"step-{run}-1")
    for ordinal in range(2, steps + 1):
        append_run_step(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            run_id=run,
            run_step_id=f"step-{run}-{ordinal}",
            ordinal=ordinal,
            step_kind="plan",
            created_at_us=BASE_US,
        )


def _claim(owned: m1.Owned, run: str, *, steps: int = 1) -> RuntimeClaim:
    _seed(owned, run, steps=steps)
    claim = _scheduler(owned, None).claim_next()
    assert claim is not None and claim.run_id == run
    return claim


def _close(
    owned: m1.Owned, claim: RuntimeClaim, *, attempt: str | None, step: str | None
) -> None:
    """Close the claimed attempt and/or step at the decision instant, the way the scheduler does."""
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        writer = transaction_local_writer(owned.connection, workspace_id=WORKSPACE_ID)
        if attempt is not None:
            writer.finish_attempt(
                attempt_id=claim.runtime_attempt_id, status=attempt, finished_at_us=DECIDED_US
            )
        if step is not None:
            writer.record_step_status(
                run_step_id=claim.run_step_id, status=step, observed_at_us=DECIDED_US
            )


def _next_sequence(owned: m1.Owned, run: str) -> int:
    row = owned.connection.execute(
        "SELECT COALESCE(MAX(sequence), -1) + 1 FROM omnivia_runtime_events "
        "WHERE workspace_id = ? AND run_id = ?",
        (WORKSPACE_ID, run),
    ).fetchone()
    return int(row[0])


def _decision(owned: m1.Owned, claim: RuntimeClaim) -> CompletionDecision:
    """The honest decision for a claim, built the way settlement builds it."""
    accepted = accepted_for(claim.run_id)
    return decide_completion(
        accepted,
        proven_readout(accepted, generation=owned.generation, workspace_id=WORKSPACE_ID),
        workspace_id=WORKSPACE_ID,
        job_id=claim.job_id,
        run_step_id=claim.run_step_id,
        runtime_attempt_id=claim.runtime_attempt_id,
        application_attempt_number=claim.application_attempt_number,
        fencing_generation=owned.generation,
        settled_sequence=_next_sequence(owned, claim.run_id),
    )


def _record(owned: m1.Owned, decision: CompletionDecision, *, decided_at_us: int = DECIDED_US) -> Any:
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as fenced:
        return record_decision(
            fenced,
            decision=decision,
            decided_at_us=decided_at_us,
            service_instance_id=owned.identity.service_instance_id,
        )


def _count(owned: m1.Owned) -> int:
    return int(owned.connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])


def _events(owned: m1.Owned, run: str) -> int:
    return int(
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_runtime_events WHERE run_id = ? AND run_status = 'succeeded'",
            (run,),
        ).fetchone()[0]
    )


FAILED_ERROR = {"code": "internal_recoverable", "message": "closed for the test", "retry_class": "retryable"}


def _terminal(owned: m1.Owned, claim: RuntimeClaim, *, state: str = "succeeded") -> None:
    """Terminalize the claimed application job the way the scheduler does, inside a fence.

    A `failed` closure carries an error, as its row's CHECK requires; the others carry none.
    """
    error = FAILED_ERROR if state == "failed" else None
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        _terminalize_application_job(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            job_id=claim.job_id,
            fencing_generation=owned.generation,
            clock=_clock(),
            state=state,
            result_kind="runtime_completion" if state == "succeeded" else None,
            result={"ok": True} if state == "succeeded" else None,
            error=error,
            _transaction_open=True,
        )


_RAW_INSERT = (
    f"INSERT INTO {TABLE} (workspace_id, decision_digest, run_id, job_id, run_step_id, "
    "runtime_attempt_id, application_attempt_number, closure_state, settled_sequence, decision, "
    "decision_body, decided_under_generation, decided_at_us, service_instance_id) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, 'succeeded', ?, 'accepted', ?, ?, ?, ?)"
)


def _settle_raw(
    owned: m1.Owned,
    claim: RuntimeClaim,
    *,
    body_text: str,
    digest: str,
    decision_run: str | None = None,
    decision_job: str | None = None,
    decision_application_attempt: int | None = None,
) -> None:
    """Write a raw decision, its succeeded event and the job's terminal observation in one fenced transaction.

    That is the scheduler's complete closure, so a row that is refused here is refused for the fact a
    test states, and a row that lands is closed and can be read back. The row's columns come from the
    claim unless overridden, so a test can break exactly one lineage fact. The event names the digest
    it is paired with.
    """
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
                digest,
                decision_run or claim.run_id,
                decision_job or claim.job_id,
                claim.run_step_id,
                claim.runtime_attempt_id,
                claim.application_attempt_number
                if decision_application_attempt is None
                else decision_application_attempt,
                _next_sequence(owned, claim.run_id),
                body_text,
                owned.generation,
                DECIDED_US,
                owned.identity.service_instance_id,
            ),
        )
        transaction_local_writer(fenced, workspace_id=WORKSPACE_ID).append_run_event(
            run_id=claim.run_id,
            runtime_event_id=f"evt-raw-{claim.run_id}",
            occurred_at_us=DECIDED_US,
            event_kind=SETTLED,
            run_status="succeeded",
            run_step_id=claim.run_step_id,
            message="raw settlement",
            details={
                "completion_decision_digest": digest,
                "workspace_id": WORKSPACE_ID,
                "run_id": claim.run_id,
                "job_id": claim.job_id,
                "run_step_id": claim.run_step_id,
                "runtime_attempt_id": claim.runtime_attempt_id,
                "runtime_attempt_number": claim.runtime_attempt_number,
                "application_attempt_number": claim.application_attempt_number,
                "service_instance_id": owned.identity.service_instance_id,
                "fencing_generation": owned.generation,
            },
        )
        _terminalize_application_job(
            fenced,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            job_id=claim.job_id,
            fencing_generation=owned.generation,
            clock=_clock(),
            state="succeeded",
            result_kind="runtime_completion",
            result={"ok": True},
            _transaction_open=True,
        )


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_final_settlement_is_the_only_way_a_decision_is_stored_and_it_is_replayed_exactly(
    owned: m1.Owned,
) -> None:
    claim = _claim(owned, "run-replay")
    _scheduler(owned, gate()).complete(claim, result_kind="runtime_completion", result={"ok": True})
    stored = read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id)
    assert stored is not None

    replayed = _record(owned, stored.decision, decided_at_us=BASE_US + 5)

    assert replayed.decision_digest == stored.decision_digest
    assert replayed.decided_at_us == stored.decided_at_us
    assert _count(owned) == 1


def test_a_different_decision_for_the_same_run_conflicts_and_writes_nothing(owned: m1.Owned) -> None:
    claim = _claim(owned, "run-conflict")
    _scheduler(owned, gate()).complete(claim, result_kind="runtime_completion", result={"ok": True})
    stored = read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id)
    assert stored is not None
    changed = replace(
        stored.decision,
        evidence=(
            replace(stored.decision.evidence[0], reviewed_by="another-reviewer"),
            *stored.decision.evidence[1:],
        ),
    )

    with pytest.raises(CompletionConflict):
        _record(owned, changed)

    assert _count(owned) == 1
    after = read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id)
    assert after is not None and after.decision_digest == stored.decision_digest


def test_a_stale_writer_cannot_record_a_decision_after_takeover(owned: m1.Owned) -> None:
    claim = _claim(owned, "run-stale")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)
    decision = _decision(owned, claim)
    rt106._takeover(owned)

    with pytest.raises(StaleGeneration):
        _record(owned, decision)
    assert _count(owned) == 0


def test_a_decision_outside_its_closed_shape_is_refused_before_storage(owned: m1.Owned) -> None:
    claim = _claim(owned, "run-shape")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)
    with pytest.raises(CompletionDecisionInvalid):
        _record(owned, _decision(owned, claim), decided_at_us=0)
    assert _count(owned) == 0


def test_a_decision_alone_fails_at_commit_cleanly_and_leaves_nothing(owned: m1.Owned) -> None:
    """A decision with fully valid lineage is not closed by its succeeded event, so COMMIT refuses it."""
    claim = _claim(owned, "run-orphan")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _record(owned, _decision(owned, claim))

    assert _count(owned) == 0
    assert not owned.connection.in_transaction


def test_a_decision_and_its_event_without_the_terminal_observation_fail_at_commit(
    owned: m1.Owned,
) -> None:
    claim = _claim(owned, "run-no-observation")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)
    decision = _decision(owned, claim)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as fenced:
        record_decision(
            fenced,
            decision=decision,
            decided_at_us=DECIDED_US,
            service_instance_id=owned.identity.service_instance_id,
        )
        transaction_local_writer(fenced, workspace_id=WORKSPACE_ID).append_run_event(
            run_id=claim.run_id,
            runtime_event_id=f"evt-no-observation-{claim.run_id}",
            occurred_at_us=DECIDED_US,
            event_kind=SETTLED,
            run_status="succeeded",
            run_step_id=claim.run_step_id,
            message="settled without its job",
            details={
                "completion_decision_digest": decision.decision_digest,
                **_detail_lineage(owned, claim, decision),
            },
        )

    assert _count(owned) == 0
    assert _events(owned, claim.run_id) == 0
    assert _state(owned, claim) == CLOSED_STATE


def test_a_terminal_observation_without_its_decision_is_refused_by_the_database(
    owned: m1.Owned,
) -> None:
    claim = _claim(owned, "run-observation-alone")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)

    with pytest.raises(sqlite3.DatabaseError, match="closed by its completion decision"):
        _terminal(owned, claim)
    assert _state(owned, claim) == CLOSED_STATE


def test_a_terminal_observation_before_its_event_is_refused_by_the_database(owned: m1.Owned) -> None:
    claim = _claim(owned, "run-observation-early")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)
    decision = _decision(owned, claim)

    with pytest.raises(sqlite3.DatabaseError, match="closed by its completion decision"), fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as fenced:
        record_decision(
            fenced,
            decision=decision,
            decided_at_us=DECIDED_US,
            service_instance_id=owned.identity.service_instance_id,
        )
        _terminalize_application_job(
            fenced,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            job_id=claim.job_id,
            fencing_generation=owned.generation,
            clock=_clock(),
            state="succeeded",
            result_kind="runtime_completion",
            result={"ok": True},
            _transaction_open=True,
        )

    assert _count(owned) == 0
    assert _state(owned, claim) == CLOSED_STATE


def _detail_lineage(owned: m1.Owned, claim: RuntimeClaim, decision: CompletionDecision) -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "run_id": claim.run_id,
        "job_id": decision.job_id,
        "run_step_id": decision.run_step_id,
        "runtime_attempt_id": decision.runtime_attempt_id,
        "runtime_attempt_number": claim.runtime_attempt_number,
        "application_attempt_number": decision.application_attempt_number,
        "service_instance_id": owned.identity.service_instance_id,
        "fencing_generation": decision.decided_under_generation,
    }


def test_the_final_success_is_closed_in_one_order_and_replays_exactly_after_closure(
    owned: m1.Owned,
) -> None:
    claim = _claim(owned, "run-closed")
    _scheduler(owned, gate()).complete(claim, result_kind="runtime_completion", result={"ok": True})
    stored = read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id)
    assert stored is not None
    assert _state(owned, claim) == SETTLED_STATE

    replayed = _record(owned, stored.decision, decided_at_us=BASE_US + 99)

    assert replayed.decided_at_us == stored.decided_at_us
    assert _count(owned) == 1
    assert owned.connection.execute("PRAGMA foreign_key_check").fetchall() == []


_INJECTION_SEAMS = ["after-decision", "after-event", "during-job-terminalization"]


@pytest.mark.parametrize("seam", _INJECTION_SEAMS)
def test_an_injected_failure_at_any_closure_step_rolls_everything_back_and_the_claim_stays_retryable(
    owned: m1.Owned, monkeypatch: pytest.MonkeyPatch, seam: str
) -> None:
    claim = _claim(owned, f"run-inject-{seam}")

    def fail(message: str) -> RuntimeError:
        return RuntimeError(f"injected {message}")

    if seam == "after-decision":
        original_settle = scheduler_module.settle_completion

        def settle_then_fail(*args: Any, **kwargs: Any) -> Any:
            original_settle(*args, **kwargs)
            raise fail(seam)

        monkeypatch.setattr(scheduler_module, "settle_completion", settle_then_fail)
    elif seam == "after-event":
        original_append = RuntimeScheduler._append_event

        def append_then_fail(self: RuntimeScheduler, *args: Any, **kwargs: Any) -> None:
            original_append(self, *args, **kwargs)
            raise fail(seam)

        monkeypatch.setattr(RuntimeScheduler, "_append_event", append_then_fail)
    else:
        original_terminalize = scheduler_module._terminalize_application_job

        def terminalize_then_fail(*args: Any, **kwargs: Any) -> Any:
            original_terminalize(*args, **kwargs)
            raise fail(seam)

        monkeypatch.setattr(scheduler_module, "_terminalize_application_job", terminalize_then_fail)

    with pytest.raises(RuntimeError, match="injected"):
        _scheduler(owned, gate()).complete(claim, result_kind="runtime_completion", result={"ok": True})
    monkeypatch.undo()

    assert _state(owned, claim) == OPEN_STATE
    assert _count(owned) == 0
    assert _events(owned, claim.run_id) == 0
    assert read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id) is None
    assert not owned.connection.in_transaction
    assert _scheduler(owned, gate()).complete(
        claim, result_kind="runtime_completion", result={"ok": True}
    ) is None
    assert _state(owned, claim) == SETTLED_STATE


def test_the_database_refuses_a_second_authority_for_a_run_already_settled(owned: m1.Owned) -> None:
    claim = _claim(owned, "run-second-authority")
    _scheduler(owned, gate()).complete(claim, result_kind="runtime_completion", result={"ok": True})

    with pytest.raises(sqlite3.DatabaseError, match="admits no workflow completion record"), fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as fenced:
        fenced.execute(
            "INSERT INTO omnivia_workflow_run_completions "
            "(workspace_id, run_id, outcome, decided_at_us, audit_ref) VALUES (?, ?, ?, ?, ?)",
            (WORKSPACE_ID, claim.run_id, "succeeded", BASE_US, "audit-second"),
        )


# --- Closure lineage: the decision must name the exact current claim ---------------------------

_LINEAGE_CASES = [
    pytest.param("missing-run", id="missing-run"),
    pytest.param("cross-run-job", id="cross-run-job"),
    pytest.param("cross-run-step", id="cross-run-step"),
    pytest.param("cross-run-attempt", id="cross-run-attempt"),
    pytest.param("missing-attempt", id="missing-attempt"),
    pytest.param("missing-application-attempt", id="missing-application-attempt"),
    pytest.param("non-final-step", id="non-final-step"),
    pytest.param("non-success-attempt", id="non-success-attempt"),
    pytest.param("non-success-step", id="non-success-step"),
    pytest.param("step-still-running", id="step-still-running"),
    pytest.param("stale-generation", id="stale-generation"),
    pytest.param("terminalized-job", id="terminalized-job"),
    pytest.param("digest-mismatch", id="digest-mismatch"),
    pytest.param("body-job-differs-from-column", id="body-job-differs-from-column"),
    pytest.param("body-application-attempt-differs", id="body-application-attempt-differs"),
]


@pytest.mark.parametrize("case", _LINEAGE_CASES)
def test_the_database_refuses_a_decision_whose_lineage_is_not_the_exact_current_settlement(
    owned: m1.Owned, case: str
) -> None:
    """Each case breaks one invariant on a direct store write. The database names it, and nothing lands."""
    run = "run-adv"
    claim = _claim(owned, run, steps=2 if case == "non-final-step" else 1)
    if case == "cross-run-job":
        _seed(owned, "run-other-job")
    if case == "cross-run-step":
        _seed(owned, "run-other-step")
    if case == "cross-run-attempt":
        other = _claim(owned, "run-other-attempt")
        _close(owned, other, attempt=SUCCEEDED, step=SUCCEEDED)
    closures = {
        "non-success-attempt": ("uncertain", SUCCEEDED),
        "non-success-step": (SUCCEEDED, "failed"),
        "step-still-running": (SUCCEEDED, None),
    }
    attempt, step = closures.get(case, (SUCCEEDED, SUCCEEDED))
    _close(owned, claim, attempt=attempt, step=step)
    if case == "terminalized-job":
        _terminal(owned, claim, state="failed")
    decision = _decision(owned, claim)

    if case == "missing-run":
        decision = replace(decision, run_id="run-missing")
    elif case == "cross-run-job":
        decision = replace(decision, job_id="job-run-other-job")
    elif case == "cross-run-step":
        decision = replace(decision, run_step_id="step-run-other-step-1")
    elif case == "cross-run-attempt":
        decision = replace(decision, runtime_attempt_id=other.runtime_attempt_id)
    elif case == "missing-attempt":
        decision = replace(decision, runtime_attempt_id="attempt-missing")
    elif case == "missing-application-attempt":
        decision = replace(decision, application_attempt_number=claim.application_attempt_number + 1)
    elif case == "stale-generation":
        decision = replace(decision, decided_under_generation=owned.generation - 1)

    if case == "digest-mismatch":
        with pytest.raises(sqlite3.DatabaseError, match="must name the digest of its own body"):
            _settle_raw(
                owned,
                claim,
                body_text=to_canonical_json(decision.to_body()),
                digest=digest_for("not-this-body"),
            )
    elif case == "body-job-differs-from-column":
        with pytest.raises(sqlite3.DatabaseError, match="agree with its lineage columns"):
            _settle_raw(
                owned,
                claim,
                body_text=to_canonical_json(decision.to_body()),
                digest=decision.decision_digest,
                decision_job="job-run-other-job",
            )
    elif case == "body-application-attempt-differs":
        with pytest.raises(sqlite3.DatabaseError, match="agree with its lineage columns"):
            _settle_raw(
                owned,
                claim,
                body_text=to_canonical_json(decision.to_body()),
                digest=decision.decision_digest,
                decision_application_attempt=claim.application_attempt_number + 1,
            )
    else:
        with pytest.raises(sqlite3.DatabaseError):
            _record(owned, decision)

    assert _count(owned) == 0
    assert read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id) is None


def test_a_two_step_run_that_finished_on_step_two_refuses_a_decision_for_the_old_step(
    owned: m1.Owned,
) -> None:
    first = _claim(owned, "run-old-step", steps=2)
    second = _scheduler(owned, None).complete(first, result_kind="runtime_completion", result={"ok": True})
    assert second is not None
    _close(owned, second, attempt=SUCCEEDED, step=SUCCEEDED)
    old = _decision(owned, first)

    with pytest.raises(sqlite3.DatabaseError):
        _record(owned, old)
    assert _count(owned) == 0


def test_an_older_application_attempt_cannot_settle_while_a_newer_one_runs(owned: m1.Owned) -> None:
    claim = _claim(owned, "run-older-attempt")
    assert claim.application_attempt_number == 1
    successor = rt106._takeover(owned)
    recovered = _scheduler(successor, None).recover_stranded()
    assert [job.requeued for job in recovered] == [True]
    newer = _scheduler(successor, None).claim_next()
    assert newer is not None and newer.application_attempt_number == 2
    _close(successor, newer, attempt=SUCCEEDED, step=SUCCEEDED)
    stale = replace(_decision(successor, newer), application_attempt_number=1)

    with pytest.raises(sqlite3.DatabaseError, match="latest event is its running step|latest running application attempt"):
        _record(successor, stale)
    assert _count(successor) == 0


def test_a_succeeded_event_without_its_decision_is_refused_by_the_database(owned: m1.Owned) -> None:
    claim = _claim(owned, "run-bare-event")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)

    with pytest.raises(sqlite3.DatabaseError, match="must carry the completion decision"), fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as fenced:
        transaction_local_writer(fenced, workspace_id=WORKSPACE_ID).append_run_event(
            run_id=claim.run_id,
            runtime_event_id="evt-bare-success",
            occurred_at_us=DECIDED_US,
            event_kind=SETTLED,
            run_status="succeeded",
            run_step_id=claim.run_step_id,
            details={"completion_decision_digest": _decision(owned, claim).decision_digest},
        )
    assert _count(owned) == 0


_EVENT_SUBSTITUTIONS = [
    pytest.param({"event_kind": "run_completed"}, id="kind"),
    pytest.param({"occurred_at_us": DECIDED_US + 1}, id="time"),
    pytest.param({"run_step_id": "other-step"}, id="step"),
    pytest.param({"details": {"completion_decision_digest": digest_for("other")}}, id="digest"),
    pytest.param({"details": {"job_id": "job-other"}}, id="job"),
    pytest.param({"details": {"runtime_attempt_id": "attempt-other"}}, id="runtime-attempt"),
    pytest.param({"details": {"application_attempt_number": 2}}, id="application-attempt"),
    pytest.param({"details": {"runtime_attempt_number": 2}}, id="runtime-attempt-number"),
    pytest.param({"details": {"runtime_attempt_number": True}}, id="runtime-attempt-number-boolean"),
    pytest.param({"details": {"runtime_attempt_number": 1.0}}, id="runtime-attempt-number-real"),
    pytest.param({"details": {"service_instance_id": "svc-other"}}, id="service-instance"),
    pytest.param({"details": {"service_instance_id": 1}}, id="service-instance-number"),
    pytest.param({"details": {"fencing_generation": GENERATION + 1}}, id="generation"),
    pytest.param({"details": {"fencing_generation": True}}, id="generation-boolean-reads-as-one"),
    pytest.param({"details": {"application_attempt_number": True}}, id="application-attempt-boolean-reads-as-one"),
    pytest.param({"details": {"run_id": "run-other"}}, id="run"),
    pytest.param({"details": {"run_step_id": "step-other-json"}}, id="json-run-step"),
    pytest.param({"details": {"completion_decision_digest": None}}, id="digest-absent"),
]


@pytest.mark.parametrize("substitution", _EVENT_SUBSTITUTIONS)
def test_a_succeeded_event_that_substitutes_any_settled_fact_is_refused_with_its_decision_present(
    owned: m1.Owned, substitution: dict[str, Any]
) -> None:
    claim = _claim(owned, "run-substitute", steps=2)
    first = _scheduler(owned, None).complete(claim, result_kind="runtime_completion", result={"ok": True})
    assert first is not None
    _close(owned, first, attempt=SUCCEEDED, step=SUCCEEDED)
    decision = _decision(owned, first)
    # `other-step` names the first step of the same run, which is a real step the event may not carry.
    other_step = claim.run_step_id
    overrides = dict(substitution)
    details = overrides.pop("details", None)
    if overrides.get("run_step_id") == "other-step":
        overrides["run_step_id"] = other_step

    event_details: Any = {
        "completion_decision_digest": decision.decision_digest,
        **_detail_lineage(owned, first, decision),
    }
    if isinstance(details, dict):
        event_details.update(details)
    elif details is not None:
        event_details = details

    expected_error = (
        "must be run_succeeded"
        if overrides.get("event_kind") == "run_completed"
        else "must carry the completion decision"
    )
    with pytest.raises(sqlite3.DatabaseError, match=expected_error), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as fenced:
        record_decision(
            fenced,
            decision=decision,
            decided_at_us=DECIDED_US,
            service_instance_id=owned.identity.service_instance_id,
        )
        transaction_local_writer(fenced, workspace_id=WORKSPACE_ID).append_run_event(
            run_id=first.run_id,
            runtime_event_id=f"evt-substitute-{first.run_id}",
            occurred_at_us=overrides.get("occurred_at_us", DECIDED_US),
            event_kind=overrides.get("event_kind", SETTLED),
            run_status="succeeded",
            run_step_id=overrides.get("run_step_id", first.run_step_id),
            message="substituted settlement",
            details=event_details,
        )
    assert _count(owned) == 0
    assert _events(owned, first.run_id) == 0


def test_a_decision_sequence_admits_only_its_succeeded_event(owned: m1.Owned) -> None:
    claim = _claim(owned, "run-reserved")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)
    decision = _decision(owned, claim)

    with pytest.raises(sqlite3.DatabaseError, match="reserved for its succeeded event"), fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as fenced:
        record_decision(
            fenced,
            decision=decision,
            decided_at_us=DECIDED_US,
            service_instance_id=owned.identity.service_instance_id,
        )
        transaction_local_writer(fenced, workspace_id=WORKSPACE_ID).append_run_event(
            run_id=claim.run_id,
            runtime_event_id="evt-reserved-failed",
            occurred_at_us=DECIDED_US,
            event_kind="run_failed",
            run_status="failed",
            run_step_id=claim.run_step_id,
            details={"fencing_generation": owned.generation},
        )
    assert _count(owned) == 0


def test_a_stored_row_whose_closure_is_not_the_succeeded_closure_is_refused_on_read(
    owned: m1.Owned,
) -> None:
    """Out-of-band corruption: a row whose closure reads `failed` is never projected as a success."""
    from omnivia_core_runtime.storage import completion_decisions as stored_rows

    claim = _claim(owned, "run-closure-corrupt")
    _scheduler(owned, gate()).complete(claim, result_kind="runtime_completion", result={"ok": True})
    row = owned.connection.execute(
        f"SELECT {', '.join(stored_rows._COLUMNS)} FROM {TABLE} WHERE workspace_id = ? AND run_id = ?",
        (WORKSPACE_ID, claim.run_id),
    ).fetchone()
    intact = list(row)
    corrupt = list(row)
    corrupt[stored_rows._COLUMNS.index("closure_state")] = "failed"

    assert stored_rows._record(tuple(intact)).decision.run_id == claim.run_id
    with pytest.raises(CompletionDecisionInvalid, match="malformed"):
        stored_rows._record(tuple(corrupt))


def test_a_body_that_is_not_canonical_or_does_not_match_its_digest_is_refused_on_read(
    owned: m1.Owned,
) -> None:
    claim = _claim(owned, "run-pretty")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)
    decision = _decision(owned, claim)
    pretty = json.dumps(json.loads(to_canonical_json(decision.to_body())), indent=2)
    _settle_raw(owned, claim, body_text=pretty, digest=_sha(pretty))

    with pytest.raises(CompletionDecisionInvalid, match="does not verify its digest"):
        read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id)


_MUTATIONS: dict[str, tuple[Callable[[dict[str, Any]], None], str]] = {
    # Lineage and identity facts the database checks against the columns: refused on write.
    "body-run-differs-from-row": (lambda body: body.update(run_id="run-other"), "db"),
    "missing-key-job_id": (lambda body: body.pop("job_id"), "db"),
    "decision-not-accepted": (lambda body: body.update(decision="rejected"), "db"),
    "bool-as-generation": (lambda body: body.update(decided_under_generation=True), "db"),
    # Shape facts the database does not check: stored, then refused on read.
    "unknown-top-level-key": (lambda body: body.update(extra=1), "read"),
    "unknown-evidence-key": (lambda body: body["evidence"][0].update(extra="x"), "read"),
    "bool-as-application-attempt": (
        lambda body: body.update(application_attempt_number=True),
        "db",
    ),
    "string-where-criteria-array": (lambda body: body.update(proven_criteria=CRITERIA[0]), "read"),
    "string-where-evidence-array": (lambda body: body.update(evidence="artefact-verified"), "read"),
    "number-where-criteria-array": (lambda body: body.update(proven_criteria=7), "read"),
    "duplicate-criteria": (lambda body: body.update(proven_criteria=[CRITERIA[0], CRITERIA[0]]), "read"),
    "unsorted-criteria": (lambda body: body.update(proven_criteria=list(reversed(CRITERIA))), "read"),
    "unproven-criteria-present": (lambda body: body.update(unproven_criteria=[CRITERIA[1]]), "read"),
    "string-where-unproven-array": (lambda body: body.update(unproven_criteria="tests-pass"), "read"),
    "evidence-is-not-a-list-of-objects": (lambda body: body.update(evidence=[CRITERIA[0]]), "read"),
}


@pytest.mark.parametrize("mutation", sorted(_MUTATIONS), ids=sorted(_MUTATIONS))
def test_a_stored_body_outside_its_closed_shape_is_refused(owned: m1.Owned, mutation: str) -> None:
    claim = _claim(owned, "run-shape-body")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)
    body = json.loads(to_canonical_json(_decision(owned, claim).to_body()))
    change, refused_by = _MUTATIONS[mutation]
    change(body)
    text = json.dumps(body)

    if refused_by == "db":
        with pytest.raises(sqlite3.DatabaseError):
            _settle_raw(owned, claim, body_text=text, digest=_sha(text))
        assert _count(owned) == 0
    else:
        _settle_raw(owned, claim, body_text=text, digest=_sha(text))
        with pytest.raises(CompletionDecisionInvalid):
            read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id)


def test_a_stored_digest_column_that_does_not_name_its_body_is_refused_by_the_database(
    owned: m1.Owned,
) -> None:
    claim = _claim(owned, "run-digest-column")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)
    decision = _decision(owned, claim)

    with pytest.raises(sqlite3.DatabaseError, match="must name the digest of its own body"):
        _settle_raw(
            owned,
            claim,
            body_text=to_canonical_json(decision.to_body()),
            digest=digest_for("not-this-body"),
        )
    assert _count(owned) == 0


def test_the_schema_refuses_an_unguarded_write_a_foreign_binding_and_any_update_or_delete(
    owned: m1.Owned,
) -> None:
    claim = _claim(owned, "run-schema")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)
    decision = _decision(owned, claim)
    body = to_canonical_json(decision.to_body())

    # The connection's authorizer refuses a raw write before the table's guard trigger runs.
    with pytest.raises(sqlite3.DatabaseError, match="unguarded INSERT|not authorized"):
        owned.connection.execute(
            _RAW_INSERT,
            (
                WORKSPACE_ID,
                decision.decision_digest,
                claim.run_id,
                claim.job_id,
                claim.run_step_id,
                claim.runtime_attempt_id,
                claim.application_attempt_number,
                decision.settled_sequence,
                body,
                owned.generation,
                DECIDED_US,
                owned.identity.service_instance_id,
            ),
        )

    with pytest.raises(sqlite3.DatabaseError, match="must bind the open workspace"), fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as fenced:
        record_decision(
            fenced,
            decision=replace(decision, workspace_id=OTHER_WORKSPACE_ID),
            decided_at_us=DECIDED_US,
            service_instance_id=owned.identity.service_instance_id,
        )

    with pytest.raises(sqlite3.DatabaseError, match="current fencing generation"), fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as fenced:
        record_decision(
            fenced,
            decision=replace(decision, decided_under_generation=owned.generation + 1),
            decided_at_us=DECIDED_US,
            service_instance_id=owned.identity.service_instance_id,
        )

    assert _count(owned) == 0
    # A complete settlement is the only way a row exists, so the update and delete targets are real.
    claim_done = _claim(owned, "run-schema-done")
    _scheduler(owned, gate()).complete(claim_done, result_kind="runtime_completion", result={"ok": True})
    assert _count(owned) == 1
    for statement in (f"UPDATE {TABLE} SET decision_body = decision_body", f"DELETE FROM {TABLE}"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"), fenced_transaction(
                owned.connection,
                owned.identity,
                workspace_id=WORKSPACE_ID,
                fencing_generation=owned.generation,
            ) as fenced:
            fenced.execute(statement)
    assert _count(owned) == 1


def test_read_back_returns_the_settled_decision_with_its_lineage(owned: m1.Owned) -> None:
    claim = _claim(owned, "run-readback")
    _scheduler(owned, gate()).complete(claim, result_kind="runtime_completion", result={"ok": True})

    stored = read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id)

    assert stored is not None
    assert stored.decision.job_id == claim.job_id
    assert stored.decision.run_step_id == claim.run_step_id
    assert stored.decision.runtime_attempt_id == claim.runtime_attempt_id
    assert stored.decision.application_attempt_number == claim.application_attempt_number
    assert stored.decision.settled_sequence == _next_sequence(owned, claim.run_id) - 1


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_a_failed_or_cancelled_observation_cannot_stand_in_for_the_succeeded_closure(
    owned: m1.Owned, state: str
) -> None:
    """The decision's deferred key names the succeeded closure exactly, so another closure of the same attempt fails.

    A failed closure reaches the key and fails it at COMMIT. A cancelled closure is refused earlier, by
    the 0015 guard that admits a cancelled observation only under an accepted cancellation control, so
    it cannot stand in either way.
    """
    claim = _claim(owned, f"run-stand-in-{state}")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)
    decision = _decision(owned, claim)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY" if state == "failed" else "cancellation control"), fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as fenced:
        record_decision(
            fenced,
            decision=decision,
            decided_at_us=DECIDED_US,
            service_instance_id=owned.identity.service_instance_id,
        )
        transaction_local_writer(fenced, workspace_id=WORKSPACE_ID).append_run_event(
            run_id=claim.run_id,
            runtime_event_id=f"evt-stand-in-{state}-{claim.run_id}",
            occurred_at_us=DECIDED_US,
            event_kind=SETTLED,
            run_status="succeeded",
            run_step_id=claim.run_step_id,
            message="settled, then closed otherwise",
            details={
                "completion_decision_digest": decision.decision_digest,
                **_detail_lineage(owned, claim, decision),
            },
        )
        _terminalize_application_job(
            fenced,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            job_id=claim.job_id,
            fencing_generation=owned.generation,
            clock=_clock(),
            state=state,
            error=FAILED_ERROR if state == "failed" else None,
            _transaction_open=True,
        )

    assert _count(owned) == 0
    assert _events(owned, claim.run_id) == 0
    assert _state(owned, claim) == CLOSED_STATE


def test_a_failed_application_attempt_cannot_be_followed_by_a_bare_succeeded_event(owned: m1.Owned) -> None:
    """Terminalizing the attempt first does not open a route: the event is still scheduler-owned and undecided."""
    claim = _claim(owned, "run-terminal-first")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)
    _terminal(owned, claim, state="failed")

    with pytest.raises(sqlite3.DatabaseError, match="must carry the completion decision"), fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as fenced:
        transaction_local_writer(fenced, workspace_id=WORKSPACE_ID).append_run_event(
            run_id=claim.run_id,
            runtime_event_id=f"evt-terminal-first-{claim.run_id}",
            occurred_at_us=DECIDED_US,
            event_kind=SETTLED,
            run_status="succeeded",
            run_step_id=claim.run_step_id,
            message="succeeded after the attempt was terminalized",
            details={"completion_decision_digest": digest_for("bare")},
        )
    assert _events(owned, claim.run_id) == 0


def test_a_succeeded_observation_cannot_precede_a_decision_that_then_names_its_event(owned: m1.Owned) -> None:
    """Terminalizing succeeded first is refused at the observation, before any decision or event can follow it."""
    claim = _claim(owned, "run-observation-first")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)
    decision = _decision(owned, claim)

    with pytest.raises(sqlite3.DatabaseError, match="closed by its completion decision"), fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as fenced:
        _terminalize_application_job(
            fenced,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            job_id=claim.job_id,
            fencing_generation=owned.generation,
            clock=_clock(),
            state="succeeded",
            result_kind="runtime_completion",
            result={"ok": True},
            _transaction_open=True,
        )
        record_decision(
            fenced,
            decision=decision,
            decided_at_us=DECIDED_US,
            service_instance_id=owned.identity.service_instance_id,
        )
    assert _count(owned) == 0
    assert _state(owned, claim) == CLOSED_STATE


@pytest.mark.parametrize(
    "substitution",
    [
        pytest.param({"fencing_generation": True}, id="generation-true"),
        pytest.param({"application_attempt_number": True}, id="application-attempt-true"),
    ],
)
def test_a_boolean_that_json_reads_as_one_is_refused_by_the_observation_guard(
    owned: m1.Owned, substitution: dict[str, Any]
) -> None:
    """JSON1 reads `true` as the integer 1, so a numeric equality alone would accept it. The integer type check refuses it."""
    claim = _claim(owned, "run-bool-observation")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)
    decision = _decision(owned, claim)
    details = {
        "completion_decision_digest": decision.decision_digest,
        **_detail_lineage(owned, claim, decision),
    }
    details.update(substitution)
    # The fence the event is written under is the real one, so only the JSON value is the lie.
    with pytest.raises(sqlite3.DatabaseError, match="must carry the completion decision"), fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as fenced:
        record_decision(
            fenced,
            decision=decision,
            decided_at_us=DECIDED_US,
            service_instance_id=owned.identity.service_instance_id,
        )
        transaction_local_writer(fenced, workspace_id=WORKSPACE_ID).append_run_event(
            run_id=claim.run_id,
            runtime_event_id=f"evt-bool-{claim.run_id}",
            occurred_at_us=DECIDED_US,
            event_kind=SETTLED,
            run_status="succeeded",
            run_step_id=claim.run_step_id,
            message="boolean lineage",
            details=details,
        )
    assert _count(owned) == 0
    assert _events(owned, claim.run_id) == 0


@pytest.mark.parametrize("field", ["application_attempt_number", "settled_sequence", "decided_under_generation"])
@pytest.mark.parametrize("kind", ["true", "real"])
def test_a_body_integer_that_is_boolean_or_real_closes_nothing(
    owned: m1.Owned, field: str, kind: str
) -> None:
    """JSON true and an integral real equal the integer under SQL equality; the integer type check refuses both on write."""
    claim = _claim(owned, f"run-json-{field}-{kind}")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)
    body = json.loads(to_canonical_json(_decision(owned, claim).to_body()))
    body[field] = True if kind == "true" else float(body[field])
    text = json.dumps(body)

    with pytest.raises(sqlite3.DatabaseError, match="agree with its lineage columns"):
        _settle_raw(owned, claim, body_text=text, digest=_sha(text))
    assert _count(owned) == 0
    assert _events(owned, claim.run_id) == 0
    assert _state(owned, claim) == CLOSED_STATE


def test_a_settled_event_whose_instant_is_altered_out_of_band_is_refused_on_read(owned: m1.Owned) -> None:
    """The decided time is outside the digest, so an out-of-band change to the event is caught on read and projection."""
    import sqlite3 as raw_sqlite

    claim = _claim(owned, "run-time-corrupt")
    _scheduler(owned, gate()).complete(claim, result_kind="runtime_completion", result={"ok": True})
    stored = read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id)
    assert stored is not None
    path = owned.path
    owned.connection.close()

    raw = raw_sqlite.connect(path)
    try:
        raw.execute("DROP TRIGGER IF EXISTS omnivia_guard_runtime_events_update")
        raw.execute(
            "UPDATE omnivia_runtime_events SET occurred_at_us = occurred_at_us + 1 "
            "WHERE workspace_id = ? AND run_id = ? AND run_status = 'succeeded'",
            (WORKSPACE_ID, claim.run_id),
        )
        raw.commit()
    finally:
        raw.close()

    reopened = m1.take_ownership(path)
    try:
        with pytest.raises(CompletionDecisionInvalid, match="does not match its succeeded event"):
            read_decision(reopened.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id)
    finally:
        reopened.connection.close()


def test_record_decision_refuses_to_replay_a_stored_row_whose_time_no_longer_matches_its_event(
    owned: m1.Owned,
) -> None:
    """An exact replay returns only a row whose settling event agrees with it; an out-of-band time change is refused."""
    import sqlite3 as raw_sqlite

    claim = _claim(owned, "run-replay-corrupt")
    _scheduler(owned, gate()).complete(claim, result_kind="runtime_completion", result={"ok": True})
    stored = read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id)
    assert stored is not None
    path = owned.path
    owned.connection.close()

    raw = raw_sqlite.connect(path)
    try:
        raw.execute("DROP TRIGGER IF EXISTS omnivia_guard_runtime_events_update")
        raw.execute(
            "UPDATE omnivia_runtime_events SET occurred_at_us = occurred_at_us + 1 "
            "WHERE workspace_id = ? AND run_id = ? AND run_status = 'succeeded'",
            (WORKSPACE_ID, claim.run_id),
        )
        raw.commit()
    finally:
        raw.close()

    reopened = m1.take_ownership(path)
    try:
        with pytest.raises(CompletionDecisionInvalid, match="does not match its succeeded event"), fenced_transaction(
            reopened.connection,
            reopened.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=reopened.generation,
        ) as fenced:
            record_decision(
                fenced,
                decision=stored.decision,
                decided_at_us=stored.decided_at_us,
                service_instance_id=reopened.identity.service_instance_id,
            )
    finally:
        reopened.connection.close()


# Out-of-band edits to the settling event's details. Migration 0066 pins these at write time, and the
# event row's own CHECKs bind `details_json` to its digest and length. The edits below rewrite all three
# together, so the table's CHECKs hold, and the read and the exact replay must still refuse them.


def _with(**changes: object) -> Callable[[str], str]:
    return lambda text: to_canonical_json({**json.loads(text), **changes})


def _without(key: str) -> Callable[[str], str]:
    return lambda text: to_canonical_json({k: v for k, v in json.loads(text).items() if k != key})


def _shadowed_duplicate(text: str) -> str:
    # A first `run_id` that a parser keeps only once: the last value wins, so every field still reads right.
    return text.replace('{"', '{"run_id":"run-shadowed",', 1)


_EVENT_DETAIL_CORRUPTIONS = [
    pytest.param(_with(completion_decision_digest=digest_for("other")), id="digest"),
    pytest.param(_with(workspace_id="ws-completion-other-0001"), id="workspace"),
    pytest.param(_with(run_id="run-other"), id="run"),
    pytest.param(_with(job_id="job-other"), id="job"),
    pytest.param(_with(run_step_id="step-other"), id="run-step"),
    pytest.param(_with(runtime_attempt_id="attempt-other"), id="runtime-attempt"),
    pytest.param(_with(application_attempt_number=True), id="application-attempt-boolean"),
    pytest.param(_with(application_attempt_number="1"), id="application-attempt-text"),
    pytest.param(_with(fencing_generation=True), id="generation-boolean"),
    pytest.param(_with(fencing_generation=float(GENERATION)), id="generation-float"),
    pytest.param(_with(runtime_attempt_number="1"), id="attempt-number-text"),
    pytest.param(_with(runtime_attempt_number=2), id="attempt-number-other"),
    pytest.param(_with(runtime_attempt_number=True), id="attempt-number-boolean"),
    pytest.param(_with(service_instance_id="svc-other"), id="service-instance-other"),
    pytest.param(_with(service_instance_id=1), id="service-instance-number"),
    pytest.param(_with(unexpected="extra"), id="extra-key"),
    pytest.param(_without("completion_decision_digest"), id="digest-absent"),
    pytest.param(_shadowed_duplicate, id="duplicate-key"),
    pytest.param(lambda text: text[:-1], id="truncated"),
    pytest.param(lambda text: f"[{text}]", id="array"),
]


@pytest.mark.parametrize("corrupt", _EVENT_DETAIL_CORRUPTIONS)
def test_a_settled_event_whose_details_are_altered_out_of_band_is_refused_by_read_and_exact_replay(
    owned: m1.Owned, corrupt: Callable[[str], str]
) -> None:
    import sqlite3 as raw_sqlite

    claim = _claim(owned, "run-details-corrupt")
    _scheduler(owned, gate()).complete(claim, result_kind="runtime_completion", result={"ok": True})
    stored = read_decision(owned.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id)
    assert stored is not None
    (details,) = owned.connection.execute(
        "SELECT details_json FROM omnivia_runtime_events WHERE workspace_id = ? AND run_id = ? "
        "AND run_status = 'succeeded'",
        (WORKSPACE_ID, claim.run_id),
    ).fetchone()
    altered = corrupt(details)
    altered_bytes = altered.encode("utf-8")
    path = owned.path
    owned.connection.close()

    raw = raw_sqlite.connect(path)
    try:
        raw.execute("DROP TRIGGER IF EXISTS omnivia_guard_runtime_events_update")
        raw.execute(
            "UPDATE omnivia_runtime_events SET details_json = ?, details_digest = ?, details_byte_length = ? "
            "WHERE workspace_id = ? AND run_id = ? AND run_status = 'succeeded'",
            (
                altered,
                f"sha256:{hashlib.sha256(altered_bytes).hexdigest()}",
                len(altered_bytes),
                WORKSPACE_ID,
                claim.run_id,
            ),
        )
        raw.commit()
    finally:
        raw.close()

    reopened = m1.take_ownership(path)
    try:
        with pytest.raises(CompletionDecisionInvalid, match="does not match its succeeded event"):
            read_decision(reopened.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id)
        with pytest.raises(
            CompletionDecisionInvalid, match="does not match its succeeded event"
        ), fenced_transaction(
            reopened.connection,
            reopened.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=reopened.generation,
        ) as fenced:
            record_decision(
                fenced,
                decision=stored.decision,
                decided_at_us=stored.decided_at_us,
                service_instance_id=reopened.identity.service_instance_id,
            )
    finally:
        reopened.connection.close()


def _succeeded_details(owned: m1.Owned, run: str) -> dict[str, Any]:
    (text,) = owned.connection.execute(
        "SELECT details_json FROM omnivia_runtime_events WHERE workspace_id = ? AND run_id = ? "
        "AND run_status = 'succeeded'",
        (WORKSPACE_ID, run),
    ).fetchone()
    return json.loads(text)  # type: ignore[no-any-return]


def test_a_final_completion_on_the_second_runtime_attempt_states_and_reads_back_that_number(
    owned: m1.Owned,
) -> None:
    _claim(owned, "run-attempt-two")
    successor = rt106._takeover(owned)
    assert [job.requeued for job in _scheduler(successor, None).recover_stranded()] == [True]
    newer = _scheduler(successor, None).claim_next()
    assert newer is not None and newer.runtime_attempt_number == 2

    _scheduler(successor, gate()).complete(newer, result_kind="runtime_completion", result={"ok": True})

    stored = read_decision(successor.connection, workspace_id=WORKSPACE_ID, run_id=newer.run_id)
    assert stored is not None
    assert stored.service_instance_id == successor.identity.service_instance_id
    details = _succeeded_details(successor, newer.run_id)
    assert details["runtime_attempt_number"] == 2
    assert details["service_instance_id"] == successor.identity.service_instance_id


def test_a_settled_decision_reads_and_replays_after_a_different_service_instance_takes_over(
    owned: m1.Owned,
) -> None:
    claim = _claim(owned, "run-historical")
    _scheduler(owned, gate()).complete(claim, result_kind="runtime_completion", result={"ok": True})
    successor = rt106._takeover(owned)
    assert successor.identity.service_instance_id != owned.identity.service_instance_id

    stored = read_decision(successor.connection, workspace_id=WORKSPACE_ID, run_id=claim.run_id)

    assert stored is not None
    assert stored.service_instance_id == owned.identity.service_instance_id
    replayed = _record(successor, stored.decision, decided_at_us=BASE_US + 7)
    assert replayed.service_instance_id == owned.identity.service_instance_id
    assert replayed.decided_at_us == stored.decided_at_us
    assert _count(successor) == 1


def test_the_database_refuses_a_decision_stored_under_another_service_instance(
    owned: m1.Owned,
) -> None:
    claim = _claim(owned, "run-foreign-service")
    _close(owned, claim, attempt=SUCCEEDED, step=SUCCEEDED)
    decision = _decision(owned, claim)

    with pytest.raises(sqlite3.DatabaseError, match="claimed by the current writer"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as fenced:
        record_decision(
            fenced, decision=decision, decided_at_us=DECIDED_US, service_instance_id="svc-other"
        )
    assert _count(owned) == 0
