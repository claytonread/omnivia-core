"""Tests for the canonical Agent Runtime records and their semantics (RT-101).

Every invariant `semantics_runtime` claims to enforce is proved here by its failure mode: a
valid record, then the smallest mutation that breaks the rule, then the refusal. A rule with
no failing case in this file is a rule this repository does not actually enforce, whatever
its docstring says.

The valid inputs are the checked-in conformance fixtures rather than dataclasses built by
hand, so the shapes under test are the same shapes the contract publishes, and a fixture that
drifts fails here as well as in `scripts/check-application-contracts.py`.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.contracts.v1 import semantics_runtime as runtime
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    OPERATION_CATALOGUE,
    Approval,
    Attempt,
    BudgetSnapshot,
    ExternalReference,
    JobControl,
    PolicySnapshot,
    ResolveWait,
    Run,
    Wait,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "fixtures"

WORKSPACE = "ws-runtime-1"
RUN_ID = "run-0001"
DIGEST = "sha256:" + "a" * 64


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _run_document() -> dict[str, Any]:
    return _fixture("runtime-run-replay.json")


def _run(**overrides: Any) -> Run:
    return Run.from_wire({**_run_document(), **overrides})


def _replace(document: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """A deep copy of `document` with one dotted/indexed path replaced.

    Mutating one field of a known-good fixture is what makes each refusal below attributable:
    the only difference between the passing case and the failing one is the field the rule is
    about.
    """
    copied = json.loads(json.dumps(document))
    node: Any = copied
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[int(part)] if part.isdigit() else node[part]
    last = parts[-1]
    if value is _DELETE:
        del node[int(last) if last.isdigit() else last]
    else:
        node[int(last) if last.isdigit() else last] = value
    return copied


_DELETE = object()


# --------------------------------------------------------------------------
# The published vocabulary is Core-owned and distinct from the job family
# --------------------------------------------------------------------------


def test_the_run_status_vocabulary_is_distinct_from_jobstate_and_the_control_plane() -> None:
    """RunStatus is a new Core-owned vocabulary, not a promotion of an existing one.

    `JobState` cannot express waiting and the control-plane run status is not durable, so the
    canonical vocabulary has to say things neither of them can: that a run is durably
    suspended, that it ended partially complete, and that an effect is unreconciled.
    """
    assert set(runtime.RUN_STATUSES) >= {
        runtime.RUN_STATUS_WAITING,
        runtime.RUN_STATUS_PARTIALLY_COMPLETED,
        runtime.RUN_STATUS_UNCERTAIN,
    }
    job_states = {"queued", "claimed", "succeeded", "failed", "cancelled"}
    assert set(runtime.RUN_STATUSES) != job_states
    # Neither vocabulary contains the other: the scheduler says things the runtime does not
    # ("claimed" is a lease, not a run state), and the runtime says things it cannot.
    assert job_states - set(runtime.RUN_STATUSES) == {"queued", "claimed"}
    assert set(runtime.RUN_STATUSES) - job_states


def test_the_published_lookup_tables_are_immutable() -> None:
    """A caller cannot edit the rules it is being judged by, in this process or any other."""
    with pytest.raises(TypeError):
        runtime.RUN_STATUS_TRANSITIONS["succeeded"] = frozenset({"running"})  # type: ignore[index]
    with pytest.raises(TypeError):
        runtime.WAIT_RESOLUTION_FOR_KIND["timer"] = "approval_decision"  # type: ignore[index]
    assert runtime.RUN_STATUS_TRANSITIONS["succeeded"] == frozenset()
    assert runtime.WAIT_RESOLUTION_FOR_KIND["timer"] == "timer_expiry"


def test_uncertain_is_not_a_terminal_run_status() -> None:
    """Uncertainty is an open question, not an outcome, so it never closes a run."""
    assert runtime.RUN_STATUS_UNCERTAIN not in runtime.RUN_TERMINAL_STATUSES
    assert set(runtime.RUN_TERMINAL_STATUSES) == {
        runtime.RUN_STATUS_SUCCEEDED,
        runtime.RUN_STATUS_PARTIALLY_COMPLETED,
        runtime.RUN_STATUS_FAILED,
        runtime.RUN_STATUS_CANCELLED,
    }


#: Every closed Runtime vocabulary, paired with the semantics constant that restates it. The
#: pairing is discovered from the schema rather than listed, so a vocabulary added to
#: `runtime.schema.json` with no Python counterpart fails here instead of silently leaving the
#: validators reasoning about a value domain the contract no longer has.
_VOCABULARY_CONSTANTS = {
    "RuntimeSourceKind": "RUNTIME_SOURCE_KINDS",
    "RunStatus": "RUN_STATUSES",
    "RunStepStatus": "RUN_STEP_STATUSES",
    "AttemptStatus": "ATTEMPT_STATUSES",
    "WaitKind": "WAIT_KINDS",
    "WaitStatus": "WAIT_STATUSES",
    "WaitResolution": "WAIT_RESOLUTIONS",
    "ApprovalDecision": "APPROVAL_DECISIONS",
    "EffectOutcome": "EFFECT_OUTCOMES",
    "CleanupOutcome": "CLEANUP_OUTCOMES",
    "RunDefinitionKind": "RUN_DEFINITION_KINDS",
}


def _schema_enums() -> dict[str, list[str]]:
    document = json.loads(
        (
            REPO_ROOT / "contracts" / "application" / "v1" / "schemas" / "runtime.schema.json"
        ).read_text(encoding="utf-8")
    )
    return {name: body["enum"] for name, body in document["$defs"].items() if body.get("enum")}


def test_every_closed_runtime_vocabulary_has_a_semantics_counterpart() -> None:
    """The schema is the authority on each vocabulary; the semantics constant restates it.

    Two copies of one value domain drift silently unless something compares them: a status
    added to the schema and not to `RUN_STATUSES` would decode, validate structurally, and
    then be treated as unknown by every fail-safe reader -- a value the contract publishes and
    the library refuses to act on.
    """
    enums = _schema_enums()
    assert set(enums) == set(_VOCABULARY_CONSTANTS)
    for name, values in enums.items():
        constant = getattr(runtime, _VOCABULARY_CONSTANTS[name])
        assert set(constant) == set(values), name
        assert len(constant) == len(values), name


def test_resolve_wait_is_not_a_job_control_and_publishes_no_operation() -> None:
    """ResolveWait sits outside the frozen job family, in every way it could not.

    It is not a `JobControl` member, it names no job, `job.retry` is still the single recovery
    operation, and neither `job.resume` nor any `runtime.*` operation exists.
    """
    assert set(JobControl.__dataclass_fields__) == {"cancellation", "recovery"}
    assert not any("job" in field for field in ResolveWait.__dataclass_fields__)
    operations = {entry.name for entry in OPERATION_CATALOGUE}
    assert len(operations) == 20
    assert "job.retry" in operations
    assert "job.resume" not in operations
    assert not any(name.startswith("runtime.") for name in operations)


# --------------------------------------------------------------------------
# Unknown open values fail safe
# --------------------------------------------------------------------------


def test_an_unknown_run_status_decodes_unchanged() -> None:
    document = _fixture("runtime-unknown-run-status.json")
    assert Run.from_wire(document).status == document["status"] == "quiesced_experimental"


@pytest.mark.parametrize(
    "reader",
    (
        runtime.is_known_run_status,
        runtime.is_terminal_run_status,
        runtime.is_successful_run_status,
        runtime.is_waiting_run_status,
        runtime.permits_new_effect,
    ),
    ids=lambda reader: reader.__name__,
)
def test_every_run_status_reader_fails_safe_on_an_unknown_value(reader: Any) -> None:
    """No decision is ever taken from a status this build cannot read."""
    for value in ("quiesced_experimental", "", None, 7, True):
        assert reader(value) is False


def test_an_unknown_status_run_is_structurally_valid_but_may_not_be_treated_as_finished() -> None:
    """The whole tolerant/strict split, in one record.

    Validation preserves the unknown status rather than rejecting it -- refusing would break
    a compatible minor release that added one -- and the function a caller uses to act on a
    finished run refuses it instead of guessing.
    """
    run = Run.from_wire(_fixture("runtime-unknown-run-status.json"))
    runtime.validate_run(run, workspace_id=WORKSPACE)
    with pytest.raises(ContractSemanticError, match="unrecognized status"):
        runtime.validate_terminal_run(run, workspace_id=WORKSPACE)


def test_a_valid_terminal_run_is_accepted_by_the_same_rule() -> None:
    """The positive control: the refusal above is about the unknown status, not the shape."""
    runtime.validate_terminal_run(_run(), workspace_id=WORKSPACE)


def test_an_unknown_status_is_never_judged_as_a_transition() -> None:
    """A step out of, or into, an unseen status is unjudged rather than guessed at."""
    runtime.validate_run_status_transition("succeeded", "quiesced_experimental")
    runtime.validate_run_status_transition("quiesced_experimental", "running")
    with pytest.raises(ContractSemanticError, match="may not move"):
        runtime.validate_run_status_transition("succeeded", "running")


def test_an_unknown_correlation_source_is_never_authority() -> None:
    for value in ("provider_of_the_week", "", None, 7):
        assert runtime.is_authoritative_source(value) is False
    assert runtime.is_authoritative_source("runtime") is True
    assert runtime.is_authoritative_source("agent_lane_ledger") is False


# --------------------------------------------------------------------------
# Workspace-scoped, source-qualified identity and correlation
# --------------------------------------------------------------------------


def test_the_conformance_run_validates_in_its_own_workspace() -> None:
    runtime.validate_run(_run(), workspace_id=WORKSPACE)


def test_a_run_is_refused_against_another_workspace() -> None:
    with pytest.raises(ContractSemanticError, match="is not this workspace"):
        runtime.validate_run(_run(), workspace_id="ws-other")


@pytest.mark.parametrize(
    "path",
    (
        "policy.workspace_id",
        "budget.workspace_id",
        "capability_grants.0.workspace_id",
        "steps.0.workspace_id",
        "steps.0.attempts.0.workspace_id",
        "effect_intents.0.workspace_id",
        "effect_receipts.0.workspace_id",
        "effect_settlements.0.workspace_id",
        "events.0.workspace_id",
        "evidence.0.workspace_id",
        "cleanup_receipts.0.workspace_id",
    ),
)
def test_every_record_in_a_run_restates_the_run_workspace(path: str) -> None:
    """One record in a foreign workspace makes the whole history unreadable, so it is refused.

    Restating the workspace on every record is only worth anything if a disagreement is an
    error; otherwise a history could be assembled from records issued in two workspaces and
    nothing would say so.
    """
    document = _replace(_run_document(), path, "ws-elsewhere")
    with pytest.raises(ContractSemanticError, match="is not this workspace"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


@pytest.mark.parametrize(
    "path",
    (
        "policy.run_id",
        "budget.run_id",
        "capability_grants.0.run_id",
        "steps.0.run_id",
        "steps.0.attempts.0.run_id",
        "effect_intents.0.run_id",
        "effect_receipts.0.run_id",
        "events.0.run_id",
        "evidence.0.run_id",
    ),
)
def test_every_record_in_a_run_restates_the_run_id(path: str) -> None:
    document = _replace(_run_document(), path, "run-9999")
    with pytest.raises(ContractSemanticError, match="is not this run"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_a_correlation_states_the_domain_its_identifier_belongs_to() -> None:
    """The agent-lane ledger collision Gate 0 names, made unjoinable.

    The fixture carries a correlation whose `source_id` is spelled like a run id from the
    agent-lane ledger. It is recorded, and it is qualified, so nothing can read it as this
    run's identity.
    """
    run = _run()
    kinds = {reference.source_kind for reference in run.correlations}
    assert kinds == {"application_job", "agent_lane_ledger"}
    assert not any(runtime.is_authoritative_source(kind) for kind in kinds)


def test_a_run_may_not_carry_a_second_authoritative_spelling_of_itself() -> None:
    document = _replace(_run_document(), "correlations.0.source_kind", "runtime")
    with pytest.raises(ContractSemanticError, match="does not correlate to itself"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_a_correlation_is_scoped_to_the_workspace_it_is_read_in() -> None:
    reference = ExternalReference.from_wire(
        {"source_kind": "external_log", "source_id": "log-1", "workspace_id": "ws-elsewhere"}
    )
    with pytest.raises(ContractSemanticError, match="is not this workspace"):
        runtime.validate_external_reference(reference, workspace_id=WORKSPACE)


# --------------------------------------------------------------------------
# Immutable, contiguous history
# --------------------------------------------------------------------------


def test_step_ordinals_are_contiguous() -> None:
    document = _replace(_run_document(), "steps.0.ordinal", 2)
    with pytest.raises(ContractSemanticError, match="breaks the contiguous 1..N sequence"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_attempt_numbers_are_contiguous() -> None:
    document = _replace(_run_document(), "steps.0.attempts.0.attempt_number", 2)
    with pytest.raises(ContractSemanticError, match="breaks the contiguous 1..N sequence"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_event_sequences_are_contiguous_from_zero() -> None:
    document = _replace(_run_document(), "events.1.sequence", 5)
    with pytest.raises(ContractSemanticError, match="breaks the contiguous 0..N stream"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_event_instants_never_regress() -> None:
    document = _replace(_run_document(), "events.1.occurred_at", "2026-08-22T08:59:00Z")
    with pytest.raises(ContractSemanticError, match="occurred_at regresses"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_an_event_stream_may_not_state_an_illegal_transition() -> None:
    document = _replace(_run_document(), "events.0.run_status", "succeeded")
    with pytest.raises(ContractSemanticError, match="may not move from 'succeeded'"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_no_attempt_may_follow_a_succeeded_attempt() -> None:
    """A succeeded attempt is final: a history continuing past one describes work already done."""
    first = _run_document()["steps"][0]["attempts"][0]
    second = {
        **first,
        "attempt_id": "attempt-0002",
        "attempt_number": 2,
        "started_at": "2026-08-22T09:00:10Z",
        "finished_at": "2026-08-22T09:00:11Z",
    }
    with pytest.raises(ContractSemanticError, match="may follow a 'succeeded' attempt"):
        runtime.validate_attempt_history(
            [Attempt.from_wire(first), Attempt.from_wire(second)],
            run_id=RUN_ID,
            run_step_id="step-0001",
            workspace_id=WORKSPACE,
        )


def test_a_failed_attempt_may_be_followed_by_another() -> None:
    """The positive control for the rule above: a history continues after a failure."""
    first = {
        **_run_document()["steps"][0]["attempts"][0],
        "status": "failed",
        "failure": {"code": "internal_recoverable", "message": "transient", "retry_class": "retryable"},
    }
    second = {
        **_run_document()["steps"][0]["attempts"][0],
        "attempt_id": "attempt-0002",
        "attempt_number": 2,
        "started_at": "2026-08-22T09:00:10Z",
        "finished_at": "2026-08-22T09:00:11Z",
    }
    runtime.validate_attempt_history(
        [Attempt.from_wire(first), Attempt.from_wire(second)],
        run_id=RUN_ID,
        run_step_id="step-0001",
        workspace_id=WORKSPACE,
    )


def test_attempts_of_one_step_never_overlap() -> None:
    first = {**_run_document()["steps"][0]["attempts"][0], "status": "cancelled"}
    second = {
        **first,
        "attempt_id": "attempt-0002",
        "attempt_number": 2,
        "started_at": "2026-08-22T09:00:06Z",
        "finished_at": "2026-08-22T09:00:11Z",
    }
    with pytest.raises(ContractSemanticError, match="never overlap"):
        runtime.validate_attempt_history(
            [Attempt.from_wire(first), Attempt.from_wire(second)],
            run_id=RUN_ID,
            run_step_id="step-0001",
            workspace_id=WORKSPACE,
        )


def test_a_running_attempt_states_no_finish_and_a_finished_one_must() -> None:
    running = _replace(_run_document(), "steps.0.attempts.0.status", "running")
    with pytest.raises(ContractSemanticError, match="a running attempt has not finished"):
        runtime.validate_run(Run.from_wire(running), workspace_id=WORKSPACE)
    undated = _replace(_run_document(), "steps.0.attempts.0.finished_at", _DELETE)
    with pytest.raises(ContractSemanticError, match="must state finished_at"):
        runtime.validate_run(Run.from_wire(undated), workspace_id=WORKSPACE)


def test_a_record_identifier_never_repeats_within_a_run() -> None:
    document = _run_document()
    document["cleanup_receipts"].append(dict(document["cleanup_receipts"][0]))
    with pytest.raises(ContractSemanticError, match="appears more than once"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


# --------------------------------------------------------------------------
# Uncertainty is not failure
# --------------------------------------------------------------------------


def test_an_uncertain_attempt_carries_no_failure() -> None:
    document = _replace(_run_document(), "steps.0.attempts.0.status", "uncertain")
    document = _replace(
        document,
        "steps.0.attempts.0.failure",
        {"code": "internal_recoverable", "message": "unclear", "retry_class": "retryable"},
    )
    with pytest.raises(ContractSemanticError, match="uncertainty is not failure"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_a_run_may_not_close_over_an_unreconciled_effect() -> None:
    """An `unknown` settlement keeps the run open, whatever terminal status it claims."""
    document = _replace(_run_document(), "effect_settlements.0.outcome", "unknown")
    document = _replace(document, "effect_settlements.0.effect_receipt_id", _DELETE)
    with pytest.raises(ContractSemanticError, match="uncertainty is not failure"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_an_uncertain_run_holding_an_unreconciled_effect_is_valid() -> None:
    """The positive control: the refusal is about closing, not about the unknown outcome."""
    document = _replace(_run_document(), "effect_settlements.0.outcome", "unknown")
    document = _replace(document, "effect_settlements.0.effect_receipt_id", _DELETE)
    document = _replace(document, "status", "uncertain")
    document = _replace(document, "events.2.run_status", "uncertain")
    document = _replace(document, "finished_at", _DELETE)
    runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


# --------------------------------------------------------------------------
# No effect before intent
# --------------------------------------------------------------------------


def test_an_effect_receipt_with_no_matching_intent_is_refused() -> None:
    run = Run.from_wire(_fixture("runtime-effect-receipt-without-intent.json"))
    assert run.effect_intents == ()
    assert run.effect_receipts and run.effect_settlements
    with pytest.raises(ContractSemanticError, match="no effect receipt without an effect intent"):
        runtime.validate_run(run, workspace_id=WORKSPACE)


def test_an_effect_settlement_with_no_matching_intent_is_refused() -> None:
    """Checked directly, because `validate_run` reaches the orphaned receipt first."""
    run = Run.from_wire(_fixture("runtime-effect-receipt-without-intent.json"))
    with pytest.raises(ContractSemanticError, match="no effect settlement without an effect intent"):
        runtime.validate_effect_settlement(
            run.effect_settlements[0],
            run_id=RUN_ID,
            workspace_id=WORKSPACE,
            intents=(),
            receipts=run.effect_receipts,
        )


def test_an_effect_is_never_observed_before_it_was_intended() -> None:
    document = _replace(_run_document(), "effect_receipts.0.observed_at", "2026-08-22T09:00:05Z")
    with pytest.raises(ContractSemanticError, match="observed before it was intended"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_a_committed_settlement_must_name_the_receipt_that_proves_it() -> None:
    document = _replace(_run_document(), "effect_settlements.0.effect_receipt_id", _DELETE)
    with pytest.raises(ContractSemanticError, match="must name the receipt that proves it"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_a_settlement_may_not_point_at_another_effects_receipt() -> None:
    document = _run_document()
    document["effect_intents"].append(
        {**document["effect_intents"][0], "effect_intent_id": "intent-0002"}
    )
    document["effect_receipts"].append(
        {
            **document["effect_receipts"][0],
            "effect_receipt_id": "receipt-0002",
            "effect_intent_id": "intent-0002",
        }
    )
    document = _replace(document, "effect_settlements.0.effect_receipt_id", "receipt-0002")
    with pytest.raises(ContractSemanticError, match="is evidence for"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_only_a_committed_settlement_names_a_receipt() -> None:
    document = _replace(_run_document(), "effect_settlements.0.outcome", "not_committed")
    with pytest.raises(ContractSemanticError, match="names no receipt"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_an_intents_context_may_not_carry_one_intent_id_twice() -> None:
    """Both effect validators are direct entry points, so they check the context they are handed.

    The two intents differ only in `declared_at`, and one of them is later than the receipt was
    observed. Resolving the duplicate id to whichever came last would therefore make "was this
    effect observed before it was intended" a question list order answers, so the ambiguity is
    refused in either ordering instead.
    """
    run = _run()
    intent = run.effect_intents[0]
    later = dataclasses.replace(intent, declared_at="2026-08-22T09:30:00Z")
    scope: dict[str, Any] = {"run_id": RUN_ID, "workspace_id": WORKSPACE}
    runtime.validate_effect_receipt(run.effect_receipts[0], **scope, intents=(intent,))
    for ordering in ((intent, later), (later, intent)):
        with pytest.raises(ContractSemanticError, match="appears more than once"):
            runtime.validate_effect_receipt(run.effect_receipts[0], **scope, intents=ordering)
        with pytest.raises(ContractSemanticError, match="appears more than once"):
            runtime.validate_effect_settlement(
                run.effect_settlements[0],
                **scope,
                intents=ordering,
                receipts=run.effect_receipts,
            )


def test_a_receipts_context_may_not_carry_one_receipt_id_twice() -> None:
    """Same rule for the receipts a settlement rests on, in either ordering.

    The shadow receipt carries the same id but is evidence for another effect, so list order
    would otherwise decide whether the committed settlement is proved or refused.
    """
    run = _run()
    receipt = run.effect_receipts[0]
    shadow = dataclasses.replace(receipt, effect_intent_id="intent-9999")
    scope: dict[str, Any] = {"run_id": RUN_ID, "workspace_id": WORKSPACE}
    runtime.validate_effect_settlement(
        run.effect_settlements[0], **scope, intents=run.effect_intents, receipts=(receipt,)
    )
    for ordering in ((receipt, shadow), (shadow, receipt)):
        with pytest.raises(ContractSemanticError, match="appears more than once"):
            runtime.validate_effect_settlement(
                run.effect_settlements[0],
                **scope,
                intents=run.effect_intents,
                receipts=ordering,
            )


# --------------------------------------------------------------------------
# Stable logical idempotency
# --------------------------------------------------------------------------


def test_the_same_logical_key_over_the_same_definition_is_a_replay() -> None:
    run = _run()
    assert runtime.classify_run_replay(run, run) == "replay"


def test_the_same_logical_key_over_a_different_definition_conflicts() -> None:
    document = _replace(_run_document(), "definition.definition_version", "2.0.0")
    assert runtime.classify_run_replay(_run(), Run.from_wire(document)) == "idempotency_conflict"


def test_the_same_logical_key_under_a_different_run_id_conflicts() -> None:
    """A replay is the same run, not a second one wearing the same key."""
    document = _replace(_run_document(), "run_id", "run-0002")
    assert runtime.classify_run_replay(_run(), Run.from_wire(document)) == "idempotency_conflict"


def test_a_different_logical_key_is_distinct_work() -> None:
    document = _replace(_run_document(), "logical_key", "onboarding-2026-08-23-acme")
    assert runtime.classify_run_replay(_run(), Run.from_wire(document)) == "distinct"


def test_one_effect_redelivered_over_the_same_digest_is_a_replay() -> None:
    intent = _run().effect_intents[0]
    assert runtime.classify_effect_replay(intent, intent) == "replay"


def test_one_idempotency_key_over_two_request_digests_conflicts() -> None:
    document = _replace(_run_document(), "effect_intents.0.request_digest", DIGEST)
    diverged = Run.from_wire(document).effect_intents[0]
    assert (
        runtime.classify_effect_replay(_run().effect_intents[0], diverged)
        == "idempotency_conflict"
    )


# --------------------------------------------------------------------------
# Monotonic policy, and discovery is not authority
# --------------------------------------------------------------------------


def test_a_grant_naming_a_merely_discovered_capability_is_refused() -> None:
    """The fixture's policy offers `memory.export` and grants only `memory.write`."""
    document = _replace(_run_document(), "capability_grants.0.capability_id", "memory.export")
    with pytest.raises(ContractSemanticError, match="discovery is not authority"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_a_grant_naming_a_capability_the_policy_never_mentions_is_refused() -> None:
    document = _replace(_run_document(), "capability_grants.0.capability_id", "billing.charge")
    with pytest.raises(ContractSemanticError, match="is not granted by the pinned policy"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_an_effect_may_not_act_through_a_grant_for_another_capability() -> None:
    document = _run_document()
    document["policy"]["granted_capabilities"].append("memory.read")
    document["capability_grants"].append(
        {
            **document["capability_grants"][0],
            "capability_grant_id": "grant-0002",
            "capability_id": "memory.read",
        }
    )
    document = _replace(document, "effect_intents.0.capability_grant_id", "grant-0002")
    with pytest.raises(ContractSemanticError, match="grants 'memory.read', not 'memory.write'"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_a_grants_context_may_not_carry_one_grant_id_twice() -> None:
    """`validate_effect_intent` is a direct entry point, so it checks the context it is handed.

    Two grants sharing an id make "the grant this effect acts through" a question list order
    answers. Resolving it to whichever came last would let a narrower grant be shadowed by a
    later duplicate, so the ambiguity is refused instead.
    """
    run = _run()
    grant = run.capability_grants[0]
    scope: dict[str, Any] = {"run_id": RUN_ID, "workspace_id": WORKSPACE}
    runtime.validate_effect_intent(run.effect_intents[0], **scope, grants=(grant,))
    with pytest.raises(ContractSemanticError, match="appears more than once"):
        runtime.validate_effect_intent(run.effect_intents[0], **scope, grants=(grant, grant))


def test_an_effect_may_not_name_a_grant_this_run_never_held() -> None:
    document = _replace(_run_document(), "effect_intents.0.capability_grant_id", "grant-9999")
    with pytest.raises(ContractSemanticError, match="names no grant issued to this run"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def _policy(**overrides: Any) -> PolicySnapshot:
    return PolicySnapshot.from_wire({**_run_document()["policy"], **overrides})


def test_policy_revisions_move_forward_only() -> None:
    with pytest.raises(ContractSemanticError, match="does not advance"):
        runtime.validate_policy_snapshot_progression(
            _policy(revision=2), _policy(revision=2, policy_snapshot_id="policy-0002")
        )


def test_a_later_policy_revision_may_narrow_but_never_widen_its_grants() -> None:
    first = _policy()
    narrowed = _policy(revision=2, policy_snapshot_id="policy-0002", granted_capabilities=[])
    runtime.validate_policy_snapshot_progression(first, narrowed)
    widened = _policy(
        revision=2,
        policy_snapshot_id="policy-0002",
        granted_capabilities=["memory.write", "memory.export"],
    )
    with pytest.raises(ContractSemanticError, match="may narrow but never widen"):
        runtime.validate_policy_snapshot_progression(first, widened)


@pytest.mark.parametrize(
    "granted",
    (5, [["memory.write"]], ("memory.write", "memory.write")),
    ids=("a-number", "a-nested-list", "a-repeat"),
)
def test_a_hand_built_policy_snapshot_is_validated_before_any_set_arithmetic(granted: Any) -> None:
    """`validate_policy_snapshot_progression` is a direct entry point, so nothing decoded it.

    A snapshot built by hand can carry anything at all in `granted_capabilities`, and set
    arithmetic over it would raise a raw `TypeError` rather than a contract error -- so both
    snapshots are validated in full first, in whichever position the hostile one occupies.
    """
    hostile = dataclasses.replace(
        _policy(revision=2, policy_snapshot_id="policy-0002"), granted_capabilities=granted
    )
    with pytest.raises(ContractSemanticError):
        runtime.validate_policy_snapshot_progression(_policy(), hostile)
    with pytest.raises(ContractSemanticError):
        runtime.validate_policy_snapshot_progression(
            hostile, _policy(revision=3, policy_snapshot_id="policy-0003")
        )


def test_a_policy_progression_compares_two_revisions_of_one_run() -> None:
    with pytest.raises(ContractSemanticError, match="is not this run"):
        runtime.validate_policy_snapshot_progression(
            _policy(),
            _policy(revision=2, policy_snapshot_id="policy-0002", run_id="run-0002"),
        )


def test_a_policy_progression_may_not_cross_workspaces() -> None:
    with pytest.raises(ContractSemanticError, match="is not this workspace"):
        runtime.validate_policy_snapshot_progression(
            _policy(),
            _policy(revision=2, policy_snapshot_id="policy-0002", workspace_id="ws-runtime-2"),
        )


def test_a_policy_revision_may_not_regress_in_time() -> None:
    with pytest.raises(ContractSemanticError, match="pinned_at regresses"):
        runtime.validate_policy_snapshot_progression(
            _policy(),
            _policy(revision=2, policy_snapshot_id="policy-0002", pinned_at="2026-08-22T08:00:00Z"),
        )


def _budget(**overrides: Any) -> BudgetSnapshot:
    return BudgetSnapshot.from_wire({**_run_document()["budget"], **overrides})


def test_consumption_never_exceeds_its_own_ceiling() -> None:
    document = _replace(_run_document(), "budget.consumed_cost_units", 1001)
    with pytest.raises(ContractSemanticError, match="exceeds max_cost_units"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_a_pinned_ceiling_may_narrow_but_never_widen() -> None:
    first = _budget()
    runtime.validate_budget_snapshot_progression(
        first, _budget(revision=2, budget_snapshot_id="budget-0002", max_cost_units=500)
    )
    with pytest.raises(ContractSemanticError, match="may narrow but never widen"):
        runtime.validate_budget_snapshot_progression(
            first, _budget(revision=2, budget_snapshot_id="budget-0002", max_cost_units=5000)
        )


def test_a_budget_progression_validates_both_snapshots_in_full() -> None:
    """A ceiling comparison over a snapshot that never passed its own rules proves nothing."""
    with pytest.raises(ContractSemanticError, match="exceeds max_cost_units"):
        runtime.validate_budget_snapshot_progression(
            _budget(),
            _budget(revision=2, budget_snapshot_id="budget-0002", consumed_cost_units=100_000),
        )


def test_a_budget_progression_compares_two_revisions_of_one_run() -> None:
    with pytest.raises(ContractSemanticError, match="is not this run"):
        runtime.validate_budget_snapshot_progression(
            _budget(),
            _budget(revision=2, budget_snapshot_id="budget-0002", run_id="run-0002"),
        )


def test_a_budget_progression_may_not_cross_workspaces() -> None:
    """The same run id in another workspace is another run, not a later revision of this one."""
    with pytest.raises(ContractSemanticError, match="is not this workspace"):
        runtime.validate_budget_snapshot_progression(
            _budget(),
            _budget(revision=2, budget_snapshot_id="budget-0002", workspace_id="ws-runtime-2"),
        )


def test_consumption_never_decreases() -> None:
    with pytest.raises(ContractSemanticError, match="consumption never decreases"):
        runtime.validate_budget_snapshot_progression(
            _budget(),
            _budget(revision=2, budget_snapshot_id="budget-0002", consumed_cost_units=0),
        )


# --------------------------------------------------------------------------
# Cancellation preserves evidence, cleanup is observable, logs are subordinate
# --------------------------------------------------------------------------


def test_the_cancelled_conformance_run_keeps_its_evidence() -> None:
    run = Run.from_wire(_fixture("runtime-run-cancelled-evidence.json"))
    runtime.validate_run(run, workspace_id=WORKSPACE)
    assert run.status == runtime.RUN_STATUS_CANCELLED
    assert run.evidence and all(item.retained for item in run.evidence)
    assert run.cleanup_receipts


def test_a_cancelled_run_may_not_discard_its_evidence() -> None:
    document = _fixture("runtime-run-cancelled-evidence.json")
    document = _replace(document, "evidence.0.retained", False)
    with pytest.raises(ContractSemanticError, match="cancellation discarded evidence"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_a_cancelled_run_that_executed_keeps_something_to_show_for_it() -> None:
    document = _replace(_fixture("runtime-run-cancelled-evidence.json"), "evidence", [])
    with pytest.raises(ContractSemanticError, match="retains no evidence"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_a_terminal_run_states_its_cleanup() -> None:
    document = _replace(_run_document(), "cleanup_receipts", [])
    with pytest.raises(ContractSemanticError, match="cleanup is observable, not assumed"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_a_failed_cleanup_is_recorded_rather_than_left_unsaid() -> None:
    """Cleanup is observable, not successful: the receipt is written for the attempt."""
    document = _replace(_run_document(), "cleanup_receipts.0.outcome", "failed")
    document = _replace(document, "cleanup_receipts.0.reason", "resource_locked")
    runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_a_non_terminal_run_has_not_finished() -> None:
    document = _replace(_run_document(), "status", "running")
    document = _replace(document, "events.2.run_status", "running")
    with pytest.raises(ContractSemanticError, match="has not finished"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_evidence_from_an_external_log_may_not_claim_authority() -> None:
    """External logs corroborate the runtime's own record; they never replace it."""
    document = _replace(_run_document(), "evidence.1.authoritative", True)
    with pytest.raises(ContractSemanticError, match="is subordinate and may not be authoritative"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_evidence_recorded_by_the_runtime_itself_may_be_authoritative() -> None:
    run = _run()
    authoritative = [item for item in run.evidence if item.authoritative]
    assert authoritative and all(
        item.source.source_kind == runtime.RUNTIME_AUTHORITATIVE_SOURCE_KIND
        for item in authoritative
    )


def test_evidence_may_not_name_an_artifact_this_run_never_produced() -> None:
    document = _replace(_run_document(), "evidence.0.artifact_id", "artifact-9999")
    with pytest.raises(ContractSemanticError, match="names no artifact of this run"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


# --------------------------------------------------------------------------
# An optional step reference is still a reference that has to resolve
# --------------------------------------------------------------------------


def _artifact_document(**overrides: Any) -> dict[str, Any]:
    """One well-formed artifact of the conformance run. The fixture records none."""
    return {
        "workspace_id": WORKSPACE,
        "artifact_id": "artifact-0001",
        "run_id": RUN_ID,
        "artifact_kind": "transcript",
        "media_type": "application/json",
        "content_checksum": DIGEST,
        "content_length_bytes": 12,
        "produced_at": "2026-08-22T09:00:05Z",
        **overrides,
    }


def test_an_event_may_not_attribute_itself_to_a_step_this_run_never_ran() -> None:
    document = _replace(_run_document(), "events.1.run_step_id", "step-9999")
    with pytest.raises(ContractSemanticError, match="names no step of this run"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_an_artifact_may_not_attribute_itself_to_a_step_this_run_never_ran() -> None:
    """The positive control first: naming the real step, and naming no step, both stand."""
    document = _run_document()
    document["artifacts"] = [
        _artifact_document(run_step_id="step-0001"),
        _artifact_document(artifact_id="artifact-0002"),
    ]
    runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)
    document = _replace(document, "artifacts.0.run_step_id", "step-9999")
    with pytest.raises(ContractSemanticError, match="names no step of this run"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_evidence_may_not_attribute_itself_to_a_step_this_run_never_ran() -> None:
    document = _replace(_run_document(), "evidence.0.run_step_id", "step-9999")
    with pytest.raises(ContractSemanticError, match="names no step of this run"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


# --------------------------------------------------------------------------
# Waits, approvals and ResolveWait
# --------------------------------------------------------------------------


def _wait(**overrides: Any) -> Wait:
    return Wait.from_wire(
        {
            "workspace_id": WORKSPACE,
            "wait_id": "wait-0001",
            "run_id": RUN_ID,
            "run_step_id": "step-0002",
            "kind": "approval",
            "status": "pending",
            "created_at": "2026-08-22T09:01:00Z",
            "resume_digest": _fixture("runtime-resolve-wait.json")["resume_digest"],
            **overrides,
        }
    )


def _command(**overrides: Any) -> ResolveWait:
    """The conformance command, with overrides applied. `None` removes an optional field.

    The wire has no null: an absent optional field is absent, and the generated decoder
    refuses an explicit null, so a test that wants "no approval_id" has to omit the key.
    """
    document = {**_fixture("runtime-resolve-wait.json"), **overrides}
    return ResolveWait.from_wire({k: v for k, v in document.items() if v is not None})


def _approval(**overrides: Any) -> Approval:
    return Approval.from_wire(
        {
            "workspace_id": WORKSPACE,
            "approval_id": "approval-0001",
            "run_id": RUN_ID,
            "wait_id": "wait-0001",
            "requested_at": "2026-08-22T09:01:00Z",
            "approver_role": "compliance-reviewer",
            **overrides,
        }
    )


def _decided_approval(**overrides: Any) -> Approval:
    """The approval the conformance command carries: a complete, recorded `approved`.

    The command quotes `approval-0001`; this is the record that decision was actually
    written to, and it is what makes quoting the id mean anything.
    """
    return _approval(
        **{
            "decision": "approved",
            "decided_at": "2026-08-22T09:04:00Z",
            "decided_by": "reviewer-7",
            "audit_reference": "audit-approval-0001",
            **overrides,
        }
    )


def test_the_conformance_resolve_wait_resolves_its_wait() -> None:
    runtime.validate_resolve_wait(
        _command(),
        wait=_wait(),
        approval=_decided_approval(),
        run_status=runtime.RUN_STATUS_WAITING,
    )


def test_the_trusted_context_a_resolution_is_judged_against_may_not_be_omitted() -> None:
    """A context argument a caller can leave out is a rule a caller can skip."""
    with pytest.raises(TypeError):
        runtime.validate_resolve_wait(  # type: ignore[call-arg]
            _command(), wait=_wait(), run_status=runtime.RUN_STATUS_WAITING
        )


def test_a_resolve_wait_is_refused_against_a_run_that_is_not_waiting() -> None:
    for status in ("running", "succeeded", "quiesced_experimental"):
        with pytest.raises(ContractSemanticError, match="is not waiting"):
            runtime.validate_resolve_wait(
                _command(),
                wait=_wait(),
                approval=_decided_approval(),
                run_status=status,
            )


def test_a_resolution_must_match_the_wait_kind() -> None:
    with pytest.raises(ContractSemanticError, match="does not resolve a 'timer' wait"):
        runtime.validate_resolve_wait(
            _command(),
            wait=_wait(kind="timer"),
            approval=_decided_approval(),
            run_status=runtime.RUN_STATUS_WAITING,
        )


def test_cancellation_releases_a_wait_of_any_kind() -> None:
    command = _command(resolution="cancelled", approval_id=None, reason="run_cancelled")
    for kind in runtime.WAIT_KINDS:
        runtime.validate_resolve_wait(
            command, wait=_wait(kind=kind), approval=None, run_status=runtime.RUN_STATUS_WAITING
        )


def test_a_resolution_carrying_no_decision_may_not_be_handed_one() -> None:
    """Supplying an approval beside a cancellation is a contradiction, not surplus context."""
    command = _command(resolution="cancelled", approval_id=None, reason="run_cancelled")
    with pytest.raises(ContractSemanticError, match="carries no approval"):
        runtime.validate_resolve_wait(
            command,
            wait=_wait(),
            approval=_decided_approval(),
            run_status=runtime.RUN_STATUS_WAITING,
        )


def test_a_rejected_decision_also_resolves_its_wait() -> None:
    """Both recorded decisions terminate the approval, so both resolve the wait it holds.

    A rejection authorizes nothing -- no grant, no effect -- but resolving a wait is not
    authorizing anything: the run resumes to act on the refusal.
    """
    runtime.validate_resolve_wait(
        _command(),
        wait=_wait(),
        approval=_decided_approval(decision="rejected"),
        run_status=runtime.RUN_STATUS_WAITING,
    )


def test_an_approval_decision_is_judged_against_the_approval_actually_recorded() -> None:
    """The command quotes an approval id; the recorded decision is what resolves the wait.

    Without the record in hand the id is a claim the command makes about itself, so each way
    it can fail to be a decision somebody made -- no decision at all, a different approval --
    is a refusal.
    """
    for approval, expected in (
        (_approval(), "records no decision"),
        (_decided_approval(approval_id="approval-0002"), "is not this recorded approval"),
    ):
        with pytest.raises(ContractSemanticError, match=expected):
            runtime.validate_resolve_wait(
                _command(),
                wait=_wait(),
                approval=approval,
                run_status=runtime.RUN_STATUS_WAITING,
            )


def test_an_approval_decided_for_another_wait_or_run_may_not_resolve_this_one() -> None:
    """An approval is authority for the wait it was requested on, and for no other."""
    with pytest.raises(ContractSemanticError, match="is not this wait"):
        runtime.validate_resolve_wait(
            _command(),
            wait=_wait(),
            approval=_decided_approval(wait_id="wait-0002"),
            run_status=runtime.RUN_STATUS_WAITING,
        )
    with pytest.raises(ContractSemanticError, match="is not this run"):
        runtime.validate_resolve_wait(
            _command(),
            wait=_wait(),
            approval=_decided_approval(run_id="run-0002"),
            run_status=runtime.RUN_STATUS_WAITING,
        )
    with pytest.raises(ContractSemanticError, match="is not this workspace"):
        runtime.validate_resolve_wait(
            _command(),
            wait=_wait(),
            approval=_decided_approval(workspace_id="ws-other"),
            run_status=runtime.RUN_STATUS_WAITING,
        )


def test_a_resolution_must_quote_the_digest_the_wait_published() -> None:
    with pytest.raises(ContractSemanticError, match="resume_digest does not match"):
        runtime.validate_resolve_wait(
            _command(),
            wait=_wait(resume_digest=DIGEST),
            approval=_decided_approval(),
            run_status=runtime.RUN_STATUS_WAITING,
        )


def test_a_wait_is_resolved_exactly_once() -> None:
    resolved = _wait(status="resolved", resolved_at="2026-08-22T09:04:00Z", approval_id="approval-0001")
    with pytest.raises(ContractSemanticError, match="already stopped being pending"):
        runtime.validate_resolve_wait(
            _command(),
            wait=resolved,
            approval=_decided_approval(),
            run_status=runtime.RUN_STATUS_WAITING,
        )


def test_only_an_approval_decision_names_an_approval() -> None:
    with pytest.raises(ContractSemanticError, match="names no approval"):
        runtime.validate_resolve_wait_shape(_command(resolution="timer_expiry"))
    with pytest.raises(ContractSemanticError, match="must name the approval carrying it"):
        runtime.validate_resolve_wait_shape(_command(approval_id=None))


def test_a_resolve_wait_may_not_be_directed_at_another_run_or_workspace() -> None:
    with pytest.raises(ContractSemanticError, match="is not this run"):
        runtime.validate_resolve_wait(
            _command(run_id="run-0002"),
            wait=_wait(),
            approval=_decided_approval(),
            run_status=runtime.RUN_STATUS_WAITING,
        )
    with pytest.raises(ContractSemanticError, match="is not this wait's workspace"):
        runtime.validate_resolve_wait(
            _command(workspace_id="ws-other"),
            wait=_wait(),
            approval=_decided_approval(),
            run_status=runtime.RUN_STATUS_WAITING,
        )


def test_an_unknown_resolution_is_refused_rather_than_guessed_at() -> None:
    with pytest.raises(ContractSemanticError, match="is not a known WaitResolution"):
        runtime.validate_resolve_wait_shape(_command(resolution="resume_from_checkpoint"))


def test_decode_resolve_wait_tolerates_an_unknown_field() -> None:
    """A newer peer's extra field decodes and is ignored, like every other v1 payload."""
    document = {**_fixture("runtime-resolve-wait.json"), "x_added_by_a_later_version": True}
    assert runtime.decode_resolve_wait(document) == _command()


def test_a_waiting_step_names_the_wait_holding_it() -> None:
    document = _replace(_run_document(), "steps.0.status", "waiting")
    with pytest.raises(ContractSemanticError, match="must name the wait holding it"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_a_step_may_not_name_a_wait_this_run_never_entered() -> None:
    document = _replace(_run_document(), "steps.0.status", "waiting")
    document = _replace(document, "steps.0.wait_id", "wait-9999")
    with pytest.raises(ContractSemanticError, match="names no wait of this run"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def _run_suspended_on_a_wait(**overrides: Any) -> dict[str, Any]:
    """The conformance run with its first step suspended on one durable wait.

    The checked-in fixtures record no wait, so the step/wait pairing rules need a run that
    has one; building it from the fixture keeps every other field the conformance shape.
    """
    document = _replace(_run_document(), "steps.0.status", "waiting")
    document = _replace(document, "steps.0.wait_id", "wait-0001")
    document["waits"] = [
        {
            "workspace_id": WORKSPACE,
            "wait_id": "wait-0001",
            "run_id": RUN_ID,
            "run_step_id": "step-0001",
            "kind": "approval",
            "status": "pending",
            "created_at": "2026-08-22T09:01:00Z",
            "resume_digest": _fixture("runtime-resolve-wait.json")["resume_digest"],
            **overrides,
        }
    ]
    return document


def _with_a_second_step(document: dict[str, Any]) -> dict[str, Any]:
    """`document` with an ordinary second step, whose one attempt is its own."""
    first = {key: value for key, value in document["steps"][0].items() if key != "wait_id"}
    document["steps"].append(
        {
            **first,
            "run_step_id": "step-0002",
            "ordinal": 2,
            "status": "succeeded",
            "attempts": [
                {**first["attempts"][0], "attempt_id": "attempt-0002", "run_step_id": "step-0002"}
            ],
        }
    )
    return document


def test_a_step_and_the_wait_holding_it_name_each_other() -> None:
    """The positive control, then the cross-link that independent membership would allow."""
    runtime.validate_run(Run.from_wire(_run_suspended_on_a_wait()), workspace_id=WORKSPACE)
    crossed = _with_a_second_step(_run_suspended_on_a_wait(run_step_id="step-0002"))
    with pytest.raises(ContractSemanticError, match="suspends step 'step-0002', not this one"):
        runtime.validate_run(Run.from_wire(crossed), workspace_id=WORKSPACE)


def test_a_wait_may_not_suspend_a_step_that_is_not_suspended_on_it() -> None:
    """The other half of the pair: the step exists, and it is holding no wait at all."""
    document = _run_suspended_on_a_wait()
    document = _replace(document, "steps.0.wait_id", _DELETE)
    document = _replace(document, "steps.0.status", "succeeded")
    with pytest.raises(ContractSemanticError, match="not by this wait"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def _approval_document(approval_id: str, wait_id: str) -> dict[str, Any]:
    """A complete, recorded `approved` decision, requested on `wait_id`."""
    return {
        "workspace_id": WORKSPACE,
        "approval_id": approval_id,
        "run_id": RUN_ID,
        "wait_id": wait_id,
        "requested_at": "2026-08-22T09:01:00Z",
        "approver_role": "compliance-reviewer",
        "decision": "approved",
        "decided_at": "2026-08-22T09:04:00Z",
        "decided_by": "reviewer-7",
        "audit_reference": f"audit-{approval_id}",
    }


def _run_with_two_resolved_waits(first_names: str, second_names: str) -> dict[str, Any]:
    """A run whose two steps each rest on their own resolved approval wait.

    Two of everything is the smallest shape in which a wait can name an approval that exists,
    belongs to this run, and was nonetheless requested on the *other* wait -- which is exactly
    what membership alone cannot tell apart.
    """
    document = _with_a_second_step(_run_suspended_on_a_wait())
    document = _replace(document, "steps.1.status", "waiting")
    document = _replace(document, "steps.1.wait_id", "wait-0002")
    first = {
        **document["waits"][0],
        "status": "resolved",
        "resolved_at": "2026-08-22T09:04:00Z",
        "approval_id": first_names,
    }
    document["waits"] = [
        first,
        {**first, "wait_id": "wait-0002", "run_step_id": "step-0002", "approval_id": second_names},
    ]
    document["approvals"] = [
        _approval_document("approval-0001", "wait-0001"),
        _approval_document("approval-0002", "wait-0002"),
    ]
    return document


def test_a_wait_and_the_approval_it_names_are_one_pairing_read_from_both_ends() -> None:
    """The positive control, then the cross-link that mere membership would allow.

    Both approvals exist, both are decided, and both were requested on a wait of this run --
    so checking only that the named approval is *somewhere* in the run accepts a run in which
    each wait resumes on the decision made about the other one.
    """
    aligned = _run_with_two_resolved_waits("approval-0001", "approval-0002")
    runtime.validate_run(Run.from_wire(aligned), workspace_id=WORKSPACE)
    crossed = _run_with_two_resolved_waits("approval-0002", "approval-0001")
    with pytest.raises(ContractSemanticError, match="was requested on wait 'wait-0002'"):
        runtime.validate_run(Run.from_wire(crossed), workspace_id=WORKSPACE)


def test_an_approval_is_requested_on_a_wait_that_is_waiting_for_one() -> None:
    """A timer expires and a signal arrives; neither is a decision anybody was asked for.

    The wait is otherwise impeccable -- resolved, of a declared kind, and naming no approval,
    so `validate_wait` passes it -- and the approval is a complete recorded decision on a wait
    of this run. Only reading the pairing from the approval's end catches it.
    """
    document = _run_with_two_resolved_waits("approval-0001", "approval-0002")
    document["waits"][1] = {
        key: value for key, value in document["waits"][1].items() if key != "approval_id"
    }
    document["waits"][1]["kind"] = "timer"
    with pytest.raises(ContractSemanticError, match="is a 'timer' wait, not an approval wait"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_a_wait_may_not_name_an_approval_this_run_never_recorded() -> None:
    document = _run_with_two_resolved_waits("approval-9999", "approval-0002")
    with pytest.raises(ContractSemanticError, match="names no approval of this run"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_an_effect_is_attempted_within_the_step_that_declared_it() -> None:
    """An attempt of another step is not this step's attempt, however real it is."""
    document = _with_a_second_step(_run_document())
    document = _replace(document, "effect_intents.0.attempt_id", "attempt-0002")
    with pytest.raises(ContractSemanticError, match="belongs to step 'step-0002'"):
        runtime.validate_run(Run.from_wire(document), workspace_id=WORKSPACE)


def test_a_pending_wait_names_no_approval() -> None:
    """An approval on a pending wait is the decision the wait is still waiting for."""
    with pytest.raises(ContractSemanticError, match="a pending wait names no approval"):
        runtime.validate_wait(
            _wait(approval_id="approval-0001"), run_id=RUN_ID, workspace_id=WORKSPACE
        )


def test_a_pending_approval_is_a_request_with_no_decision() -> None:
    runtime.validate_approval(_approval(), run_id=RUN_ID, workspace_id=WORKSPACE)


def test_a_recorded_decision_is_complete_or_absent() -> None:
    """A decision nobody can attribute is not an approval, so the four fields stand together."""
    with pytest.raises(ContractSemanticError, match="missing"):
        runtime.validate_approval(
            _approval(decision="approved"), run_id=RUN_ID, workspace_id=WORKSPACE
        )
    runtime.validate_approval(
        _approval(
            decision="approved",
            decided_at="2026-08-22T09:04:00Z",
            decided_by="reviewer-7",
            audit_reference="audit-approval-0001",
        ),
        run_id=RUN_ID,
        workspace_id=WORKSPACE,
    )


def test_a_decision_never_predates_its_own_request() -> None:
    with pytest.raises(ContractSemanticError, match="decided_at precedes requested_at"):
        runtime.validate_approval(
            _approval(
                decision="rejected",
                decided_at="2026-08-22T08:00:00Z",
                decided_by="reviewer-7",
                audit_reference="audit-approval-0001",
            ),
            run_id=RUN_ID,
            workspace_id=WORKSPACE,
        )


# --------------------------------------------------------------------------
# Direct entry points never raise a raw TypeError
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    (
        lambda: runtime.validate_run(None, workspace_id=WORKSPACE),
        lambda: runtime.validate_run({"run_id": "x"}, workspace_id=WORKSPACE),
        lambda: runtime.validate_run(_run(), workspace_id=None),
        lambda: runtime.validate_terminal_run(7, workspace_id=WORKSPACE),
        lambda: runtime.validate_run_status_transition(None, "running"),
        lambda: runtime.validate_attempt(None, run_id=RUN_ID, run_step_id="s", workspace_id=WORKSPACE),
        lambda: runtime.validate_attempt_history(
            "not-a-sequence", run_id=RUN_ID, run_step_id="s", workspace_id=WORKSPACE
        ),
        lambda: runtime.validate_run_step({}, run_id=RUN_ID, workspace_id=WORKSPACE),
        lambda: runtime.validate_wait(object(), run_id=RUN_ID, workspace_id=WORKSPACE),
        lambda: runtime.validate_approval(None, run_id=RUN_ID, workspace_id=WORKSPACE),
        lambda: runtime.validate_policy_snapshot(None, run_id=RUN_ID, workspace_id=WORKSPACE),
        lambda: runtime.validate_policy_snapshot_progression(None, None),
        lambda: runtime.validate_budget_snapshot(None, run_id=RUN_ID, workspace_id=WORKSPACE),
        lambda: runtime.validate_budget_snapshot_progression(None, None),
        lambda: runtime.validate_capability_grant(
            None, run_id=RUN_ID, workspace_id=WORKSPACE, policy=None
        ),
        lambda: runtime.validate_effect_intent(
            None, run_id=RUN_ID, workspace_id=WORKSPACE, grants=()
        ),
        lambda: runtime.validate_effect_receipt(
            None, run_id=RUN_ID, workspace_id=WORKSPACE, intents=()
        ),
        lambda: runtime.validate_effect_settlement(
            None, run_id=RUN_ID, workspace_id=WORKSPACE, intents=(), receipts=()
        ),
        lambda: runtime.validate_runtime_event_stream(3, run_id=RUN_ID, workspace_id=WORKSPACE),
        lambda: runtime.validate_artifact(None, run_id=RUN_ID, workspace_id=WORKSPACE),
        lambda: runtime.validate_evidence_item(None, run_id=RUN_ID, workspace_id=WORKSPACE),
        lambda: runtime.validate_cleanup_receipt(None, run_id=RUN_ID, workspace_id=WORKSPACE),
        lambda: runtime.validate_external_reference(None, workspace_id=WORKSPACE),
        lambda: runtime.validate_resolve_wait_shape(None),
        lambda: runtime.validate_resolve_wait(
            _command(), wait=None, approval=None, run_status="waiting"
        ),
        lambda: runtime.validate_resolve_wait(
            _command(), wait=_wait(), approval=object(), run_status="waiting"
        ),
        lambda: runtime.classify_run_replay(None, None),
        lambda: runtime.classify_effect_replay(None, None),
    ),
    ids=lambda call: "case",
)
def test_a_misused_entry_point_raises_a_contract_semantic_error(call: Any) -> None:
    """No public function may leak a raw `TypeError`/`AttributeError` to a direct caller."""
    with pytest.raises(ContractSemanticError):
        call()
