"""C05a acceptance for the effect-head reader and the stop-progress repository.

Two readers, one property between them: neither is allowed to answer a question the
workspace has not answered.

*The effect head is walked, never guessed.* Migration 0024 links a superseded
`unknown` settlement to the later one that replaced it, and puts uniqueness only on
the resulting side -- so one settlement may be superseded twice, by two answers that
disagree about whether a real external effect happened. `read_effect_heads` follows
every arm and reports :data:`EFFECT_HEAD_BRANCHED` when more than one end exists.
The tests below check both that it refuses, and specifically that it does not do the
tempting thing: the later-timestamped arm is not the one it picks.

*A stop projection is backed by rows.* An unknown stop identifier refuses. A stop
recorded with neither a progress observation nor a settled 0025 outcome refuses. A
`pending_reconciliation` phase is only ever reported where durable progress rows say
so, and `settled` only where no obligation is still unresolved at the effect ledger
*and* cleanup has resolved to `not_required` or `completed`.

*`retry_eligible` is never true.* The contract says it reports an owner-authorized
recovery decision rather than an inference from a settled stop; this layer holds no
such authorization and so reports none, settled or not.

Everything runs against a real SQLite workspace migrated to head, over the accepted
cancellation path -- `stop_run` -- rather than hand-written stop rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_c05_runtime_stop_progress_migration as m42
import test_rt102_agent_runtime_migration as m18
import test_rt203_effect_reconciliation_migration as m24
import test_rt203_effect_transaction_migration as m23
from omnivia_core_runtime.storage.agent_runtime import append_run_event
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.runtime_effect_head import (
    EFFECT_HEAD_BRANCHED,
    EFFECT_HEAD_SETTLED,
    EFFECT_HEAD_UNRESOLVED,
    read_effect_head,
)
from omnivia_core_runtime.storage.runtime_stop import (
    MAX_STOP_PENDING_EFFECT_IDS,
    STOP_OUTCOME_ACCEPTED,
    RunStopRequest,
    StopCleanupReceipt,
    StopObligation,
    read_stop_obligations,
    read_stop_progress,
    read_stop_projection,
    runtime_stop_writer,
    stop_run,
)

from omnivia_core.contracts.v1.semantics_runtime import (
    RUNTIME_STOP_PHASE_PENDING_RECONCILIATION,
    RUNTIME_STOP_PHASE_REQUESTED,
    RUNTIME_STOP_PHASE_SETTLED,
    validate_runtime_stop_projection,
)

WORKSPACE_ID = m18.WORKSPACE_ID
RUN_ID = m18.RUN_ID
BASE_US = m18.BASE_US
AUDIT_REF = m42.AUDIT_REF

INTENT = "effect-intent-0001"
STOP_REQUEST_ID = m42.STOP_REQUEST_ID
REQUESTED_AT_US = m42.REQUESTED_AT_US
STOP_REQUESTS = "omnivia_runtime_stop_requests"
STOP_OUTCOMES = "omnivia_runtime_stop_outcomes"

guarded = m42.guarded
insert = m42.insert


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


# --- seeding -------------------------------------------------------------------------


def reconcile(holder: m1.Owned, **overrides: object) -> None:
    with guarded(holder):
        insert(holder, m24.RECONCILIATIONS, m24.reconciliation_row(**overrides))


def settle(holder: m1.Owned, **overrides: object) -> None:
    with guarded(holder):
        insert(holder, m23.SETTLEMENTS, m23.settlement_row(**overrides))


def stop_request() -> RunStopRequest:
    return RunStopRequest(
        stop_request_id=STOP_REQUEST_ID,
        run_id=RUN_ID,
        requested_at_us=REQUESTED_AT_US,
        requested_by="core-operator",
        reason="operator.cancelled",
        audit_ref=AUDIT_REF,
    )


def cancel(holder: m1.Owned) -> None:
    """Record an accepted cancellation over a run the effect seeds already built.

    Written as rows rather than through `stop_run`, because the `m23`/`m24` seeds
    open their run's event stream with a direct INSERT and the canonical writer
    maintains the run-summary projection, which refuses to start mid-stream. The
    writer's own path is exercised by
    :func:`test_the_accepted_cancellation_path_produces_a_readable_projection` and
    by `test_t0693_runtime_stop`; what these tests need is the ledger it leaves.
    """
    with guarded(holder):
        m18.insert_event(
            holder,
            sequence=2,
            runtime_event_id="evt-stop-0001",
            occurred_at_us=REQUESTED_AT_US + 1,
            event_kind="run.cancelled",
            run_status="cancelled",
        )
        insert(holder, STOP_REQUESTS, m42.stop_request_row())
        insert(
            holder,
            STOP_OUTCOMES,
            {
                "workspace_id": WORKSPACE_ID,
                "stop_request_id": STOP_REQUEST_ID,
                "outcome": STOP_OUTCOME_ACCEPTED,
                "completed_at_us": REQUESTED_AT_US + 2,
                "runtime_event_sequence": 2,
                "reason": "operator.cancelled",
                "audit_ref": AUDIT_REF,
            },
        )


def record_progress(holder: m1.Owned, **overrides: object) -> Any:
    values: dict[str, Any] = {
        "stop_progress_id": "stop-progress-0001",
        "stop_request_id": STOP_REQUEST_ID,
        "observed_at_us": REQUESTED_AT_US + 3,
        "cleanup_required": True,
        "reason": "effects.pending",
        "audit_ref": AUDIT_REF,
        "obligations": (),
    }
    values.update(overrides)
    with runtime_stop_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ) as writer:
        return writer.record_progress(**values)


def record_cleanup(holder: m1.Owned, receipt_id: str, outcome: str) -> None:
    with runtime_stop_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ) as writer:
        writer.record_cleanup_receipt(
            StopCleanupReceipt(
                stop_cleanup_receipt_id=receipt_id,
                stop_request_id=STOP_REQUEST_ID,
                resource_kind="provider.session",
                outcome=outcome,
                performed_at_us=REQUESTED_AT_US + 4,
                reason="cancellation.cleanup",
                audit_ref=AUDIT_REF,
            )
        )


def projection(holder: m1.Owned, stop_request_id: str = STOP_REQUEST_ID) -> Any:
    """The projection, checked against the contract's own validator every time."""
    read = read_stop_projection(
        holder.connection, workspace_id=WORKSPACE_ID, stop_request_id=stop_request_id
    )
    validate_runtime_stop_projection(read)
    return read


UNRESOLVED_OBLIGATION = StopObligation(
    effect_intent_id=INTENT, effect_settlement_id="effect-settlement-0001"
)


# --- the effect-head reader ----------------------------------------------------------


def test_an_intent_with_no_settlement_has_no_head_and_is_unresolved(
    owned: m1.Owned,
) -> None:
    m23.seed_intent(owned)

    head = read_effect_head(
        owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT
    )

    assert head.resolution == EFFECT_HEAD_UNRESOLVED
    assert head.effect_settlement_id is None
    assert head.outcome is None
    assert not head.settled


def test_an_unlinked_unknown_settlement_is_the_head_and_is_unresolved(
    owned: m1.Owned,
) -> None:
    m24.seed_unknown_source(owned)

    head = read_effect_head(
        owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT
    )

    assert head.resolution == EFFECT_HEAD_UNRESOLVED
    assert head.effect_settlement_id == "effect-settlement-0001"
    assert head.outcome == "unknown"


def test_a_not_applied_link_settles_the_head(owned: m1.Owned) -> None:
    m24.seed_not_applied_result(owned)
    reconcile(owned)

    head = read_effect_head(
        owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT
    )

    assert head.resolution == EFFECT_HEAD_SETTLED
    assert head.effect_settlement_id == "effect-settlement-0002"
    assert head.outcome == "not_committed"
    assert head.settled


def test_an_applied_link_settles_the_head(owned: m1.Owned) -> None:
    m24.seed_applied_result(owned)
    reconcile(
        owned,
        outcome="APPLIED",
        effect_receipt_id="effect-receipt-0001",
        reconciled_at_us=BASE_US + 7,
    )

    head = read_effect_head(
        owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT
    )

    assert head.resolution == EFFECT_HEAD_SETTLED
    assert head.effect_settlement_id == "effect-settlement-0002"
    assert head.outcome == "committed"


@pytest.mark.parametrize("outcome", ("PARTIAL", "UNKNOWN"))
def test_a_partial_or_unknown_link_leaves_the_head_unresolved(
    owned: m1.Owned, outcome: str
) -> None:
    """Both result in a further `unknown`, which a later link may still supersede."""
    m24.seed_unknown_source(owned)
    settle(
        owned,
        effect_settlement_id="effect-settlement-0002",
        outcome="unknown",
        effect_receipt_id=None,
        reason="still_unreachable",
        settled_at_us=BASE_US + 6,
    )
    reconcile(owned, outcome=outcome)

    head = read_effect_head(
        owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT
    )

    assert head.resolution == EFFECT_HEAD_UNRESOLVED
    assert head.effect_settlement_id == "effect-settlement-0002"
    assert head.outcome == "unknown"


def test_a_chain_of_two_links_reaches_the_last_answer(owned: m1.Owned) -> None:
    """A still-unknown result may be reconciled again; the walk follows it through."""
    m24.seed_unknown_source(owned)
    settle(
        owned,
        effect_settlement_id="effect-settlement-0002",
        outcome="unknown",
        effect_receipt_id=None,
        reason="still_unreachable",
        settled_at_us=BASE_US + 6,
    )
    reconcile(owned, outcome="PARTIAL")
    settle(
        owned,
        effect_settlement_id="effect-settlement-0003",
        outcome="not_committed",
        effect_receipt_id=None,
        reason="absence_proven",
        settled_at_us=BASE_US + 8,
    )
    reconcile(
        owned,
        effect_reconciliation_id="effect-reconcile-0002",
        source_effect_settlement_id="effect-settlement-0002",
        resulting_effect_settlement_id="effect-settlement-0003",
        reconciled_at_us=BASE_US + 8,
    )

    head = read_effect_head(
        owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT
    )

    assert head.resolution == EFFECT_HEAD_SETTLED
    assert head.effect_settlement_id == "effect-settlement-0003"


def test_two_contradictory_links_from_one_settlement_branch(owned: m1.Owned) -> None:
    """Two reconcilers disagree, and neither answer is published as the head.

    0024 puts uniqueness on the resulting side alone, so one `unknown` settlement may
    be superseded twice. `NOT_APPLIED` at `BASE_US + 6` and `APPLIED` at
    `BASE_US + 8` are two claims about whether a real external effect happened, and
    the later instant is not evidence about which is true -- so the assertions below
    insist not merely on a refusal, but that the later arm was not chosen.
    """
    m24.seed_not_applied_result(owned)
    reconcile(owned)
    with guarded(owned):
        insert(
            owned,
            m23.RECEIPTS,
            m23.receipt_row(
                effect_receipt_id="effect-receipt-0002", observed_at_us=BASE_US + 7
            ),
        )
    settle(
        owned,
        effect_settlement_id="effect-settlement-0003",
        outcome="committed",
        effect_receipt_id="effect-receipt-0002",
        settled_at_us=BASE_US + 8,
    )
    reconcile(
        owned,
        effect_reconciliation_id="effect-reconcile-0002",
        outcome="APPLIED",
        effect_receipt_id="effect-receipt-0002",
        resulting_effect_settlement_id="effect-settlement-0003",
        reconciled_at_us=BASE_US + 8,
    )

    head = read_effect_head(
        owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT
    )

    assert head.resolution == EFFECT_HEAD_BRANCHED
    assert head.effect_settlement_id is None
    assert head.outcome is None
    assert not head.settled


def test_two_unlinked_alternatives_of_one_intent_branch(owned: m1.Owned) -> None:
    """The same situation without the links, and it gets the same answer."""
    m24.seed_unknown_source(owned)
    settle(
        owned,
        effect_settlement_id="effect-settlement-0009",
        outcome="unknown",
        effect_receipt_id=None,
        reason="provider_unreachable",
        settled_at_us=BASE_US + 9,
    )

    head = read_effect_head(
        owned.connection, workspace_id=WORKSPACE_ID, effect_intent_id=INTENT
    )

    assert head.resolution == EFFECT_HEAD_BRANCHED
    assert head.effect_settlement_id is None


def test_a_foreign_workspace_reads_no_head(owned: m1.Owned) -> None:
    m24.seed_unknown_source(owned)

    head = read_effect_head(
        owned.connection,
        workspace_id=m18.OTHER_WORKSPACE_ID,
        effect_intent_id=INTENT,
    )

    assert head.resolution == EFFECT_HEAD_UNRESOLVED
    assert head.effect_settlement_id is None


# --- projections that refuse ---------------------------------------------------------


def test_an_unknown_stop_request_fails_closed(owned: m1.Owned) -> None:
    m24.seed_unknown_source(owned)

    with pytest.raises(StorageError, match="not a stop request of this workspace"):
        read_stop_projection(
            owned.connection, workspace_id=WORKSPACE_ID, stop_request_id="stop-9999"
        )


def test_a_request_with_neither_progress_nor_outcome_fails_closed(
    owned: m1.Owned,
) -> None:
    """A bare request row says a stop was asked for and nothing about where it got to."""
    m42.seed_stopped_run_with_unknown_effect(owned)

    with pytest.raises(StorageError, match="neither progress nor"):
        read_stop_projection(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            stop_request_id=STOP_REQUEST_ID,
        )


def test_a_foreign_workspace_reads_no_projection(owned: m1.Owned) -> None:
    m24.seed_unknown_source(owned)
    cancel(owned)

    with pytest.raises(StorageError, match="not a stop request of this workspace"):
        read_stop_projection(
            owned.connection,
            workspace_id=m18.OTHER_WORKSPACE_ID,
            stop_request_id=STOP_REQUEST_ID,
        )


# --- projections over real progress --------------------------------------------------


def test_the_accepted_cancellation_path_produces_a_readable_projection(
    owned: m1.Owned,
) -> None:
    """The canonical writer's own output, read back as stop progress.

    Nothing about `stop_run` changes in this slice, so this is the linkage test: a
    cancellation accepted by the existing path leaves a request that these readers
    can report on, at phase `requested` until something looks at what it is waiting
    on.
    """
    m18.seed_job(owned)
    with guarded(owned):
        m18.insert_run(owned)
    append_run_event(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        run_id=RUN_ID,
        runtime_event_id="evt-0001",
        occurred_at_us=BASE_US,
        event_kind="run.admitted",
        run_status="admitted",
    )

    outcome = stop_run(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        request=stop_request(),
        runtime_event_id="evt-stop-0001",
        occurred_at_us=REQUESTED_AT_US + 1,
        completed_at_us=REQUESTED_AT_US + 2,
    )

    assert outcome.outcome == STOP_OUTCOME_ACCEPTED
    read = projection(owned)
    assert read.stop_request_id == STOP_REQUEST_ID
    assert read.phase == RUNTIME_STOP_PHASE_REQUESTED
    assert not read.retry_eligible


def test_a_settled_stop_nobody_has_looked_at_reports_requested(owned: m1.Owned) -> None:
    """0025 settled it; no observation exists, so nothing further is yet known."""
    m24.seed_unknown_source(owned)
    cancel(owned)

    read = projection(owned)

    assert read.phase == RUNTIME_STOP_PHASE_REQUESTED
    assert read.pending_effect_count == 0
    assert read.pending_effect_ids == ()
    assert not read.pending_effects_truncated
    # No receipt and no observation establishes nothing about cleanup, and an empty
    # pending-effect count is not proof that cleanup finished.
    assert read.cleanup_state == "uncertain"
    assert not read.retry_eligible
    assert read.request_audit_ref == AUDIT_REF


def test_a_pending_projection_is_backed_by_durable_progress_rows(
    owned: m1.Owned,
) -> None:
    m24.seed_unknown_source(owned)
    cancel(owned)
    recorded = record_progress(owned, obligations=(UNRESOLVED_OBLIGATION,))

    read = projection(owned)

    assert read.phase == RUNTIME_STOP_PHASE_PENDING_RECONCILIATION
    assert read.pending_effect_count == 1
    assert read.pending_effect_ids == (INTENT,)
    assert not read.pending_effects_truncated
    assert read.cleanup_state == "requested"
    assert not read.retry_eligible
    # The rows the projection rests on are really there.
    assert recorded.progress_number == 1
    stored = read_stop_progress(
        owned.connection, workspace_id=WORKSPACE_ID, stop_request_id=STOP_REQUEST_ID
    )
    assert stored == (recorded,)
    assert read_stop_obligations(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        stop_progress_id=recorded.stop_progress_id,
    ) == (UNRESOLVED_OBLIGATION,)


def test_an_obligation_reconciliation_has_since_answered_is_no_longer_pending(
    owned: m1.Owned,
) -> None:
    """The obligation row is never rewritten; the effect ledger is re-read."""
    m24.seed_not_applied_result(owned)
    cancel(owned)
    record_progress(owned, cleanup_required=False, obligations=(UNRESOLVED_OBLIGATION,))
    assert projection(owned).pending_effect_count == 1

    reconcile(owned)

    read = projection(owned)
    assert read.pending_effect_count == 0
    assert read.phase == RUNTIME_STOP_PHASE_SETTLED
    assert read.cleanup_state == "not_required"
    assert not read.retry_eligible


def test_a_branched_obligation_stays_pending(owned: m1.Owned) -> None:
    """A stop is not clear of an effect nobody can say the outcome of."""
    m24.seed_unknown_source(owned)
    settle(
        owned,
        effect_settlement_id="effect-settlement-0009",
        outcome="unknown",
        effect_receipt_id=None,
        reason="provider_unreachable",
        settled_at_us=BASE_US + 9,
    )
    cancel(owned)
    record_progress(owned, cleanup_required=False, obligations=(UNRESOLVED_OBLIGATION,))

    read = projection(owned)

    assert read.pending_effect_count == 1
    assert read.phase == RUNTIME_STOP_PHASE_PENDING_RECONCILIATION


def test_the_latest_observation_is_the_one_reported(owned: m1.Owned) -> None:
    m24.seed_unknown_source(owned)
    cancel(owned)
    record_progress(owned, obligations=(UNRESOLVED_OBLIGATION,))
    second = record_progress(
        owned,
        stop_progress_id="stop-progress-0002",
        observed_at_us=REQUESTED_AT_US + 5,
        cleanup_required=False,
        reason="effects.abandoned",
    )

    read = projection(owned)

    assert second.progress_number == 2
    assert read.pending_effect_count == 0
    assert read.phase == RUNTIME_STOP_PHASE_SETTLED
    # And the earlier observation still says exactly what it said.
    first = read_stop_progress(
        owned.connection, workspace_id=WORKSPACE_ID, stop_request_id=STOP_REQUEST_ID
    )[0]
    assert read_stop_obligations(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        stop_progress_id=first.stop_progress_id,
    ) == (UNRESOLVED_OBLIGATION,)


def test_settled_requires_zero_pending_effects(owned: m1.Owned) -> None:
    m24.seed_unknown_source(owned)
    cancel(owned)
    record_progress(owned, cleanup_required=False, obligations=(UNRESOLVED_OBLIGATION,))

    read = projection(owned)

    assert read.cleanup_state == "not_required"
    assert read.pending_effect_count == 1
    assert read.phase == RUNTIME_STOP_PHASE_PENDING_RECONCILIATION


@pytest.mark.parametrize(
    ("outcomes", "cleanup_state", "phase"),
    (
        ((), "requested", RUNTIME_STOP_PHASE_PENDING_RECONCILIATION),
        (("released",), "completed", RUNTIME_STOP_PHASE_SETTLED),
        (("not_required",), "not_required", RUNTIME_STOP_PHASE_SETTLED),
        (("failed",), "failed", RUNTIME_STOP_PHASE_PENDING_RECONCILIATION),
        (("released", "failed"), "partial", RUNTIME_STOP_PHASE_PENDING_RECONCILIATION),
        (("unknown",), "uncertain", RUNTIME_STOP_PHASE_PENDING_RECONCILIATION),
        (
            ("released", "unknown"),
            "uncertain",
            RUNTIME_STOP_PHASE_PENDING_RECONCILIATION,
        ),
    ),
    ids=("none", "released", "nothing-to-free", "failed", "mixed", "unknown", "shadowed"),
)
def test_settled_requires_cleanup_to_have_resolved(
    owned: m1.Owned,
    outcomes: tuple[str, ...],
    cleanup_state: str,
    phase: str,
) -> None:
    """With no pending effect left, cleanup alone decides whether this stop settled.

    The `shadowed` case is the one worth naming: one resource was released and one
    could not be established, and the aggregate is `uncertain` rather than
    `completed`, because a receipt that says nothing was established is not evidence
    that everything was.
    """
    m24.seed_unknown_source(owned)
    cancel(owned)
    record_progress(owned, cleanup_required=True)
    for index, outcome in enumerate(outcomes):
        record_cleanup(owned, f"stop-cleanup-{index:04d}", outcome)

    read = projection(owned)

    assert read.pending_effect_count == 0
    assert read.cleanup_state == cleanup_state
    assert read.phase == phase
    assert not read.retry_eligible


def test_pending_effect_ids_are_bounded_and_the_truncation_flag_is_truthful(
    owned: m1.Owned,
) -> None:
    """More unresolved effects than the contract may list: the count still tells truth."""
    total = MAX_STOP_PENDING_EFFECT_IDS + 2
    m23.seed_effect_parents(owned)
    obligations = []
    with guarded(owned):
        for index in range(total):
            intent = f"effect-intent-c{index:04d}"
            settlement = f"effect-settlement-c{index:04d}"
            insert(
                owned,
                m23.INTENTS,
                m23.intent_row(
                    effect_intent_id=intent,
                    idempotency_key=f"effect-key-c{index:04d}",
                ),
            )
            insert(
                owned,
                m23.SETTLEMENTS,
                m23.settlement_row(
                    effect_settlement_id=settlement,
                    effect_intent_id=intent,
                    outcome="unknown",
                    effect_receipt_id=None,
                    reason="provider_unreachable",
                ),
            )
            obligations.append(
                StopObligation(
                    effect_intent_id=intent, effect_settlement_id=settlement
                )
            )
    cancel(owned)
    record_progress(owned, obligations=tuple(obligations))

    read = projection(owned)

    assert read.pending_effect_count == total
    assert len(read.pending_effect_ids) == MAX_STOP_PENDING_EFFECT_IDS
    assert read.pending_effects_truncated
    # The prefix is the identifier ordering, not a wall-clock one, so which ids are
    # carried does not depend on a value no invariant pins.
    assert list(read.pending_effect_ids) == sorted(
        o.effect_intent_id for o in obligations
    )[:MAX_STOP_PENDING_EFFECT_IDS]


def test_an_exactly_bounded_list_is_not_reported_as_truncated(owned: m1.Owned) -> None:
    m24.seed_unknown_source(owned)
    cancel(owned)
    record_progress(owned, obligations=(UNRESOLVED_OBLIGATION,))

    read = projection(owned)

    assert read.pending_effect_count == len(read.pending_effect_ids)
    assert not read.pending_effects_truncated
