"""RT-203 acceptance for the Approval and CapabilityGrant repositories.

The migration tests hold migration 0022 to what SQL can enforce. These hold
`storage/agent_runtime.py`'s three new writers and their readers to what SQL cannot.

*A decision is made once, on the request that was actually made.* An approval is a
request and, later, a decision, appended as separate facts and read back as one record.
`record_approval_decision` compares every immutable request fact with the one already
stored before it issues a statement, so a decision naming a different wait, role,
assignee, deadline or instant inserts nothing at all -- and a second decision, of either
outcome, is refused here and structurally impossible in 0022.

*A comment is recorded once.* The accepted contract lets a pending approval carry a
comment and lets a decision add one later, so both round-trip; replacing one that is
already recorded does not, and leaves the stored text exactly as it was.

*Discovery is not authority, and neither is a superseded decision.* A grant must name
the run's latest pinned `PolicySnapshot` and must be for a capability that policy
actually granted -- a capability the workspace merely offers is refused. A grant already
issued stays readable after the policy narrows underneath it, validated against the
historical policy it names rather than the revision in force now.

*The grant record is the bytes.* Each grant is stored as the complete canonical v1 wire
document, addressed by the SHA-256 of exactly those bytes. Every read recomputes the
digest and length, requires canonical bytes, decodes through the generated contract and
checks the columns the row is indexed by against the document itself, so a file edited
outside this database fails closed rather than returning something that merely parses.

*The exact action is bound by `Wait.resume_digest`, which already exists.* RT-203 adds
no second approval digest and changes no contract: a resolution quoting any other digest
is refused by the existing `ResolveWait` semantics before policy is consulted, so a
recorded approval cannot be spent on a state it was not granted for. That is proved here
against RT-107's real seam rather than restated.

No public wire surface is touched: the contract version is asserted unchanged.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt102_agent_runtime_repository as r102
import test_rt107_runtime_waits as rt107
import test_rt202_policy_budget_snapshot_repository as r202
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.service.runtime_waits import WaitResolutionConflict
from omnivia_core_runtime.storage.agent_runtime import (
    StoredCapabilityGrant,
    close_wait,
    issue_capability_grant,
    open_wait,
    read_approval,
    read_capability_grant,
    read_run,
    read_run_approvals,
    read_run_capability_grants,
    read_run_waits,
    record_approval_decision,
    request_approval,
    runtime_writer,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    Approval,
    CapabilityGrant,
    ContractSemanticError,
    PolicySnapshot,
    to_canonical_json,
    validate_approval,
    validate_capability_grant,
)

WORKSPACE_ID = m18.WORKSPACE_ID
OTHER_WORKSPACE_ID = m18.OTHER_WORKSPACE_ID
BASE_US = m18.BASE_US
JOB_ID = m18.JOB_ID
RUN_ID = m18.RUN_ID
STEP_ID = m18.STEP_ID
WAIT_ID = m18.WAIT_ID
DIGEST = m18.DIGEST
MS = r202.MS

APPROVALS = "omnivia_runtime_approvals"
DECISIONS = "omnivia_runtime_approval_decisions"
COMMENTS = "omnivia_runtime_approval_comments"
GRANTS = "omnivia_runtime_capability_grants"
TABLES = (APPROVALS, DECISIONS, COMMENTS, GRANTS)

APPROVAL_ID = "apr-0001"
GRANT_ID = "cap-0001"
POLICY_ID = r202.POLICY_ID

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def at(offset_ms: int = 0) -> str:
    """The run's admission instant plus milliseconds, spelled the way a read renders it.

    An approval is materialised from microsecond columns rather than replayed from
    stored bytes, so a read renders the canonical spelling of the instant it holds --
    trailing `.000` omitted. Derived from `BASE_US` rather than written out, because
    0022 refuses an approval requested before the wait it belongs to.
    """
    moment = _EPOCH + timedelta(microseconds=BASE_US + offset_ms * MS)
    if moment.microsecond == 0:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"

#: RT-107's own fixtures, re-exported so this file can drive the real `ResolveWait`
#: seam. The exact-action binding RT-203 relies on is `Wait.resume_digest`, which that
#: seam already checks; proving it here against a stub would prove nothing about it.
admitted = rt107.admitted
running = rt107.running


def approval(**overrides: Any) -> Approval:
    """One pending approval request on the run's approval wait."""
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "approval_id": APPROVAL_ID,
        "run_id": RUN_ID,
        "wait_id": WAIT_ID,
        "requested_at": at(),
        "approver_role": "runtime_approver",
    }
    values.update(overrides)
    return Approval(**values)


def decided(**overrides: Any) -> Approval:
    """The same approval, complete with the one decision it receives."""
    values: dict[str, Any] = {
        "decision": "approved",
        "decided_at": at(1),
        "decided_by": "principal-approver",
        "audit_reference": m18.audit_ref_for(JOB_ID),
    }
    values.update(overrides)
    return approval(**values)


def grant(**overrides: Any) -> CapabilityGrant:
    """One capability the policy in `r202.policy()` actually grants."""
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "capability_grant_id": GRANT_ID,
        "run_id": RUN_ID,
        "capability_id": "memory.read",
        "policy_snapshot_id": POLICY_ID,
        "granted_at": at(),
        "scopes": ("memory:read",),
        "purpose": "runtime.execute",
    }
    values.update(overrides)
    return CapabilityGrant(**values)


def narrowed_policy(**overrides: Any) -> PolicySnapshot:
    """Revision 2, granting strictly less than revision 1 did."""
    values: dict[str, Any] = {
        "policy_snapshot_id": "policy-0002",
        "revision": 2,
        "pinned_at": at(1),
        "granted_capabilities": ("memory.read",),
    }
    values.update(overrides)
    return r202.policy(**values)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def add_wait(
    holder: m1.Owned,
    *,
    wait_id: str = WAIT_ID,
    run_step_id: str = STEP_ID,
    kind: str = "approval",
    created_at_us: int = BASE_US,
    expires_at_us: int | None = None,
) -> None:
    open_wait(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        wait_id=wait_id,
        run_id=RUN_ID,
        run_step_id=run_step_id,
        kind=kind,
        created_at_us=created_at_us,
        resume_digest=DIGEST,
        expires_at_us=expires_at_us,
    )


def waiting(holder: m1.Owned, **wait: Any) -> None:
    """One admitted run, one step, and one pending approval wait to decide on."""
    r102.admit(holder)
    r102.add_step(holder)
    add_wait(holder, **wait)


def ask(holder: m1.Owned, record: Approval | None = None) -> None:
    request_approval(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        approval=record or approval(),
    )


def decide(holder: m1.Owned, record: Approval | None = None) -> None:
    record_approval_decision(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        approval=record or decided(),
    )


def issue(holder: m1.Owned, record: CapabilityGrant | None = None) -> None:
    issue_capability_grant(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        grant=record or grant(),
    )


def row_counts(holder: m1.Owned) -> dict[str, int]:
    return {
        table: int(
            holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
        for table in TABLES
    }


# --- the approval round trip -----------------------------------------------------


def test_a_requested_approval_reads_back_pending(owned: m1.Owned) -> None:
    """Pending is every decision field absent, which is a state and not a gap."""
    waiting(owned)
    ask(owned)
    stored = read_approval(
        owned.connection, workspace_id=WORKSPACE_ID, approval_id=APPROVAL_ID
    )
    assert stored == approval()
    assert stored is not None
    assert (
        stored.decision,
        stored.decided_at,
        stored.decided_by,
        stored.audit_reference,
    ) == (None, None, None, None)
    validate_approval(stored, run_id=RUN_ID, workspace_id=WORKSPACE_ID)
    assert row_counts(owned)[DECISIONS] == 0


def test_the_full_request_half_round_trips_including_its_optional_fields(
    owned: m1.Owned,
) -> None:
    waiting(owned)
    full = approval(
        assigned_to="principal-approver",
        escalated_to="principal-escalation",
        expires_at=at(10),
        comment="please review the export step",
    )
    ask(owned, full)
    assert (
        read_approval(
            owned.connection, workspace_id=WORKSPACE_ID, approval_id=APPROVAL_ID
        )
        == full
    )


@pytest.mark.parametrize("outcome", ("approved", "rejected"))
def test_a_decided_approval_reads_back_whole(owned: m1.Owned, outcome: str) -> None:
    """A rejection is as recordable as an approval; neither is the absence of the other."""
    waiting(owned)
    ask(owned)
    answer = decided(decision=outcome)
    decide(owned, answer)
    stored = read_approval(
        owned.connection, workspace_id=WORKSPACE_ID, approval_id=APPROVAL_ID
    )
    assert stored == answer
    assert stored is not None
    assert stored.decision == outcome
    assert stored.decided_by == "principal-approver"
    assert stored.audit_reference == m18.audit_ref_for(JOB_ID)
    validate_approval(stored, run_id=RUN_ID, workspace_id=WORKSPACE_ID)
    assert row_counts(owned)[APPROVALS] == 1
    assert row_counts(owned)[DECISIONS] == 1


def test_run_approvals_read_in_one_deterministic_order(owned: m1.Owned) -> None:
    waiting(owned)
    r102.add_step(owned, run_step_id="step-0002", ordinal=2)
    add_wait(owned, wait_id="wait-0002", run_step_id="step-0002")
    second = approval(
        approval_id="apr-0002", wait_id="wait-0002", requested_at=at(2)
    )
    ask(owned, second)
    ask(owned)
    assert read_run_approvals(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    ) == (approval(), second)


def test_a_missing_approval_reads_as_absent_rather_than_empty(owned: m1.Owned) -> None:
    waiting(owned)
    assert (
        read_approval(
            owned.connection, workspace_id=WORKSPACE_ID, approval_id="apr-none"
        )
        is None
    )
    assert (
        read_run_approvals(
            owned.connection, workspace_id=WORKSPACE_ID, run_id="run-absent"
        )
        == ()
    )


# --- the decision is made once, on the request that was made ------------------------


def test_a_second_decision_is_refused_and_leaves_the_first_untouched(
    owned: m1.Owned,
) -> None:
    """Refused here, and structurally impossible in 0022; the migration tests hold that."""
    waiting(owned)
    ask(owned)
    decide(owned)
    before = row_counts(owned)
    with pytest.raises(StorageError, match="already decided"):
        decide(owned, decided(decision="rejected", decided_at=at(2)))
    with pytest.raises(StorageError, match="already decided"):
        decide(owned)
    assert row_counts(owned) == before
    stored = read_approval(
        owned.connection, workspace_id=WORKSPACE_ID, approval_id=APPROVAL_ID
    )
    assert stored is not None and stored.decision == "approved"


def test_a_decision_for_an_approval_nobody_requested_inserts_nothing(
    owned: m1.Owned,
) -> None:
    waiting(owned)
    before = row_counts(owned)
    with pytest.raises(StorageError, match="was never requested"):
        decide(owned)
    assert row_counts(owned) == before


CONTRADICTED_REQUESTS = {
    "another wait": {"wait_id": "wait-0002"},
    "another role": {"approver_role": "runtime_reviewer"},
    "another run": {"run_id": "run-0002"},
    # The decision moves with the request it claims, so what is refused below is the
    # rewritten request rather than a decision that predates its own request.
    "another instant": {"requested_at": at(3), "decided_at": at(4)},
    "an assignee it never had": {"assigned_to": "principal-someone"},
    "an escalation it never had": {"escalated_to": "principal-escalation"},
    "a deadline it never had": {"expires_at": at(30)},
}


@pytest.mark.parametrize("case", sorted(CONTRADICTED_REQUESTS))
def test_a_decision_that_rewrites_its_own_request_inserts_nothing(
    owned: m1.Owned, case: str
) -> None:
    """The request half is immutable, so a decision may not restate it differently."""
    waiting(owned)
    ask(owned)
    before = row_counts(owned)
    with pytest.raises(StorageError, match="disagrees with the request"):
        decide(owned, decided(**CONTRADICTED_REQUESTS[case]))
    assert row_counts(owned) == before
    stored = read_approval(
        owned.connection, workspace_id=WORKSPACE_ID, approval_id=APPROVAL_ID
    )
    assert stored == approval()


def test_the_same_instant_spelled_differently_is_the_same_request(
    owned: m1.Owned,
) -> None:
    """The request stores an instant, not a spelling, so `.000Z` restates it exactly.

    A caller that wrote the request in one canonical spelling and hands the decision
    back in the other is not rewriting anything, and refusing it would be this module
    comparing text where it means to compare time.
    """
    waiting(owned)
    ask(owned, approval(requested_at=at(), expires_at=at(10)))
    respelled = r202.pinned_at()
    assert respelled != at() and respelled.startswith(at().removesuffix("Z"))
    decide(
        owned,
        decided(requested_at=respelled, expires_at=r202.pinned_at(10)),
    )
    stored = read_approval(
        owned.connection, workspace_id=WORKSPACE_ID, approval_id=APPROVAL_ID
    )
    assert stored == decided(expires_at=at(10))


def test_a_partial_decision_is_refused_before_any_statement(owned: m1.Owned) -> None:
    """All four fields stand or fall together: a decision nobody can attribute is none."""
    waiting(owned)
    ask(owned)
    before = row_counts(owned)
    with pytest.raises(ContractSemanticError, match="missing"):
        decide(owned, approval(decision="approved"))
    with pytest.raises(StorageError, match="a partial one is not a decision"):
        decide(owned, approval())
    assert row_counts(owned) == before


def test_a_decided_approval_cannot_be_recorded_as_a_request(owned: m1.Owned) -> None:
    """The two halves are two appends; a request that arrives decided is neither."""
    waiting(owned)
    before = row_counts(owned)
    with pytest.raises(StorageError, match="carries no decision"):
        ask(owned, decided())
    assert row_counts(owned) == before


def test_a_decision_after_the_approval_deadline_has_expired_not_been_decided(
    owned: m1.Owned,
) -> None:
    waiting(owned)
    ask(owned, approval(expires_at=at(1)))
    before = row_counts(owned)
    with pytest.raises(sqlite3.IntegrityError, match="past its deadline"):
        decide(owned, decided(expires_at=at(1), decided_at=at(2)))
    assert row_counts(owned) == before


def test_a_decision_after_the_wait_deadline_has_expired_not_been_decided(
    owned: m1.Owned,
) -> None:
    """The wait's own deadline binds the decision too: it is the thing being waited on."""
    waiting(owned, expires_at_us=BASE_US + MS)
    ask(owned)
    before = row_counts(owned)
    with pytest.raises(sqlite3.IntegrityError, match="past its wait deadline"):
        decide(owned, decided(decided_at=at(2)))
    assert row_counts(owned) == before
    decide(owned, decided(decided_at=at(1)))
    assert row_counts(owned)[DECISIONS] == 1


def test_a_deadline_that_precedes_its_own_request_is_refused(owned: m1.Owned) -> None:
    waiting(owned)
    before = row_counts(owned)
    with pytest.raises(ContractSemanticError, match="expires_at precedes requested_at"):
        ask(owned, approval(requested_at=at(5), expires_at=at(1)))
    assert row_counts(owned) == before


def test_neither_half_lands_on_a_wait_that_is_no_longer_pending(
    owned: m1.Owned,
) -> None:
    """An approval decides a wait that is still waiting for it, or it decides nothing."""
    waiting(owned)
    ask(owned)
    close_wait(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        wait_id=WAIT_ID,
        status="cancelled",
        resolved_at_us=BASE_US + MS,
        resolution_reason="run_cancelled",
    )
    before = row_counts(owned)
    with pytest.raises(sqlite3.IntegrityError, match="still pending"):
        decide(owned, decided(decided_at=at(1)))
    with pytest.raises(sqlite3.IntegrityError, match="still pending"):
        ask(owned, approval(approval_id="apr-0002"))
    assert row_counts(owned) == before


def test_an_approval_is_requested_only_on_an_approval_wait_of_its_own_run(
    owned: m1.Owned,
) -> None:
    waiting(owned, kind="timer")
    before = row_counts(owned)
    with pytest.raises(sqlite3.IntegrityError, match="approval wait of its own run"):
        ask(owned)
    assert row_counts(owned) == before


# --- the comment is recorded once ---------------------------------------------------


def test_a_pending_comment_survives_the_decision_that_follows_it(
    owned: m1.Owned,
) -> None:
    waiting(owned)
    ask(owned, approval(comment="please review the export step"))
    decide(owned, decided(comment="please review the export step"))
    stored = read_approval(
        owned.connection, workspace_id=WORKSPACE_ID, approval_id=APPROVAL_ID
    )
    assert stored is not None
    assert stored.comment == "please review the export step"
    assert stored.decision == "approved"
    assert row_counts(owned)[COMMENTS] == 1


def test_a_decision_may_add_the_comment_the_request_did_not_carry(
    owned: m1.Owned,
) -> None:
    waiting(owned)
    ask(owned)
    assert row_counts(owned)[COMMENTS] == 0
    decide(owned, decided(comment="approved with the narrower scope"))
    stored = read_approval(
        owned.connection, workspace_id=WORKSPACE_ID, approval_id=APPROVAL_ID
    )
    assert stored is not None
    assert stored.comment == "approved with the narrower scope"
    assert row_counts(owned)[COMMENTS] == 1


@pytest.mark.parametrize("replacement", ("a different note", None))
def test_a_recorded_comment_is_never_replaced(
    owned: m1.Owned, replacement: str | None
) -> None:
    """Dropping the text is replacing it with nothing, and is refused the same way."""
    waiting(owned)
    ask(owned, approval(comment="the original note"))
    before = row_counts(owned)
    with pytest.raises(StorageError, match="never replaced"):
        decide(owned, decided(comment=replacement))
    assert row_counts(owned) == before
    stored = read_approval(
        owned.connection, workspace_id=WORKSPACE_ID, approval_id=APPROVAL_ID
    )
    assert stored is not None
    assert stored.comment == "the original note"
    assert stored.decision is None


def test_the_longest_comment_the_contract_accepts_round_trips(owned: m1.Owned) -> None:
    """A column that refused a legal record would be a bound storage has no right to set."""
    waiting(owned)
    longest = "n" * 2048
    ask(owned, approval(comment=longest))
    stored = read_approval(
        owned.connection, workspace_id=WORKSPACE_ID, approval_id=APPROVAL_ID
    )
    assert stored is not None and stored.comment == longest
    with pytest.raises(ContractSemanticError, match="maximum length"):
        ask(owned, approval(approval_id="apr-0002", comment="n" * 2049))


# --- the exact action stays bound to the wait's own resume digest ---------------------


def rt107_pending() -> Approval:
    return replace(
        rt107.decided_approval(),
        decision=None,
        decided_at=None,
        decided_by=None,
        audit_reference=None,
    )


def test_a_changed_resume_digest_cannot_spend_the_recorded_approval(
    running: m1.Owned,
) -> None:
    """The binding is `Wait.resume_digest`, already stored and already checked.

    RT-203 adds no second approval digest and touches no contract, so the proof is that
    the accepted `ResolveWait` semantics refuse a quoted digest that is not the one the
    wait published -- before the policy seam is even consulted -- and that the durably
    recorded decision is still sitting there, unspent, afterwards.
    """
    rt107.open_wait(running, kind="approval")
    answer = rt107.decided_approval()
    request_approval(
        running.connection,
        running.identity,
        workspace_id=rt107.WORKSPACE_ID,
        fencing_generation=running.generation,
        approval=rt107_pending(),
    )
    record_approval_decision(
        running.connection,
        running.identity,
        workspace_id=rt107.WORKSPACE_ID,
        fencing_generation=running.generation,
        approval=answer,
    )
    stored = read_approval(
        running.connection,
        workspace_id=rt107.WORKSPACE_ID,
        approval_id=answer.approval_id,
    )
    assert stored == answer

    policy = rt107.RecordingPolicy(approval=stored)
    with pytest.raises(WaitResolutionConflict, match="resume_digest"):
        rt107.resolve_wait(
            running,
            policy=policy,
            command=rt107.resolution(
                resolution_kind="approval_decision",
                reason="approved",
                approval_id=answer.approval_id,
                resume_digest="sha256:" + "f" * 64,
            ),
        )
    assert policy.calls == 0
    waits = read_run_waits(
        running.connection, workspace_id=rt107.WORKSPACE_ID, run_id=rt107.RUN_ID
    )
    assert waits[0].status == "pending" and waits[0].approval_id is None
    assert (
        read_approval(
            running.connection,
            workspace_id=rt107.WORKSPACE_ID,
            approval_id=answer.approval_id,
        )
        == answer
    )

    # The same resolution, quoting the digest the wait actually published, spends it.
    rt107.resolve_wait(
        running,
        policy=rt107.RecordingPolicy(approval=stored),
        key="rt203-resolve-0001",
        command=rt107.resolution(
            resolution_kind="approval_decision",
            reason="approved",
            approval_id=answer.approval_id,
            resume_digest=waits[0].resume_digest,
        ),
    )
    resolved = read_run_waits(
        running.connection, workspace_id=rt107.WORKSPACE_ID, run_id=rt107.RUN_ID
    )
    assert resolved[0].status == "resolved"
    assert resolved[0].approval_id == answer.approval_id


# --- grants are backed by the policy that issued them --------------------------------


def test_a_grant_round_trips_as_the_exact_contract_record(owned: m1.Owned) -> None:
    waiting(owned)
    r202.add_policy(owned)
    issue(owned)
    stored = read_capability_grant(
        owned.connection, workspace_id=WORKSPACE_ID, capability_grant_id=GRANT_ID
    )
    assert isinstance(stored, StoredCapabilityGrant)
    assert stored.grant == grant()
    assert stored.grant.scopes == ("memory:read",)
    assert stored.grant.purpose == "runtime.execute"
    validate_capability_grant(
        stored.grant,
        run_id=RUN_ID,
        workspace_id=WORKSPACE_ID,
        policy=r202.policy(),
    )


def test_the_content_address_is_the_digest_of_the_canonical_wire_document(
    owned: m1.Owned,
) -> None:
    waiting(owned)
    r202.add_policy(owned)
    issue(owned)
    document = to_canonical_json(grant().to_wire())
    stored = read_capability_grant(
        owned.connection, workspace_id=WORKSPACE_ID, capability_grant_id=GRANT_ID
    )
    assert stored is not None
    assert stored.content_address == r202.digest_of(document)
    assert stored.content_length_bytes == len(document.encode("utf-8"))
    # The address is the document's, not the identifier's: a grant id is never a digest.
    assert stored.content_address != GRANT_ID
    row = owned.connection.execute(
        f"SELECT grant_json, grant_digest, grant_byte_length FROM {GRANTS}"
    ).fetchone()
    assert row[0] == document
    assert row[1] == r202.digest_of(str(row[0]))
    assert row[2] == len(str(row[0]).encode("utf-8"))


def test_the_largest_grant_the_contract_accepts_is_storable(owned: m1.Owned) -> None:
    """64 scopes of 128 characters, with every bounded field at its own maximum."""
    waiting(owned)
    r202.add_policy(owned)
    largest = grant(
        scopes=tuple(f"s{index:03d}" + "_" * 124 for index in range(64)),
        purpose="p" * 128,
        expires_at=at(600_000),
    )
    issue(owned, largest)
    stored = read_capability_grant(
        owned.connection, workspace_id=WORKSPACE_ID, capability_grant_id=GRANT_ID
    )
    assert stored is not None and stored.grant == largest
    assert stored.content_length_bytes == len(
        to_canonical_json(largest.to_wire()).encode("utf-8")
    )


def test_a_capability_the_workspace_merely_offers_is_not_a_grant(
    owned: m1.Owned,
) -> None:
    """`memory.export` is discovered and not granted, and discovery is not authority."""
    waiting(owned)
    r202.add_policy(owned)
    before = row_counts(owned)
    with pytest.raises(ContractSemanticError, match="discovery is not authority"):
        issue(owned, grant(capability_id="memory.export"))
    with pytest.raises(ContractSemanticError, match="not granted by the pinned policy"):
        issue(owned, grant(capability_id="memory.unknown"))
    assert row_counts(owned) == before


def test_a_grant_must_name_the_policy_in_force_when_it_is_issued(
    owned: m1.Owned,
) -> None:
    """A superseded decision cannot back a new grant, or a narrowing could be walked back."""
    waiting(owned)
    r202.add_policy(owned)
    r202.add_policy(owned, narrowed_policy())
    before = row_counts(owned)
    with pytest.raises(StorageError, match="policy in force when it is issued"):
        issue(owned, grant(granted_at=at(1)))
    assert row_counts(owned) == before
    issue(
        owned,
        grant(policy_snapshot_id="policy-0002", granted_at=at(1)),
    )
    stored = read_capability_grant(
        owned.connection, workspace_id=WORKSPACE_ID, capability_grant_id=GRANT_ID
    )
    assert stored is not None
    assert stored.grant.policy_snapshot_id == "policy-0002"


def test_a_run_with_no_pinned_policy_has_nothing_to_back_a_grant(
    owned: m1.Owned,
) -> None:
    waiting(owned)
    before = row_counts(owned)
    with pytest.raises(StorageError, match="no pinned policy"):
        issue(owned)
    assert row_counts(owned) == before


def test_a_grant_stays_readable_after_the_policy_narrows_underneath_it(
    owned: m1.Owned,
) -> None:
    """Validated against the policy it names, not the revision in force now.

    A grant issued under revision 1 is still the grant that was issued once revision 2
    has narrowed the capability out; re-checking it against a decision made afterwards
    would make a legal history unreadable, which is the opposite of the point.
    """
    waiting(owned)
    r202.add_policy(owned)
    issue(owned, grant(capability_id="memory.write"))
    r202.add_policy(owned, narrowed_policy())
    stored = read_capability_grant(
        owned.connection, workspace_id=WORKSPACE_ID, capability_grant_id=GRANT_ID
    )
    assert stored is not None
    assert stored.grant == grant(capability_id="memory.write")
    assert "memory.write" not in narrowed_policy().granted_capabilities
    assert len(
        read_run_capability_grants(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
    ) == 1


def test_run_grants_read_in_one_deterministic_order(owned: m1.Owned) -> None:
    waiting(owned)
    r202.add_policy(owned)
    issue(owned, grant(capability_grant_id="cap-0002", granted_at=at(2)))
    issue(owned, grant())
    assert [
        issued.grant.capability_grant_id
        for issued in read_run_capability_grants(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
    ] == [GRANT_ID, "cap-0002"]


def test_a_missing_grant_reads_as_absent_rather_than_empty(owned: m1.Owned) -> None:
    waiting(owned)
    assert (
        read_capability_grant(
            owned.connection, workspace_id=WORKSPACE_ID, capability_grant_id="cap-none"
        )
        is None
    )
    assert (
        read_run_capability_grants(
            owned.connection, workspace_id=WORKSPACE_ID, run_id="run-absent"
        )
        == ()
    )


# --- a workspace is an argument, never a record's claim about one ---------------------


def test_no_reader_answers_for_a_workspace_it_was_not_asked_about(
    owned: m1.Owned,
) -> None:
    waiting(owned)
    r202.add_policy(owned)
    ask(owned)
    decide(owned)
    issue(owned)
    for reader, key in (
        (read_approval, {"approval_id": APPROVAL_ID}),
        (read_capability_grant, {"capability_grant_id": GRANT_ID}),
    ):
        assert reader(owned.connection, workspace_id=OTHER_WORKSPACE_ID, **key) is None
    assert (
        read_run_approvals(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, run_id=RUN_ID
        )
        == ()
    )
    assert (
        read_run_capability_grants(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, run_id=RUN_ID
        )
        == ()
    )


def test_a_record_naming_another_workspace_is_refused_before_the_write(
    owned: m1.Owned,
) -> None:
    """The writer's workspace decides, so a record cannot smuggle in its own."""
    waiting(owned)
    r202.add_policy(owned)
    before = row_counts(owned)
    with pytest.raises(ContractSemanticError, match="is not this workspace"):
        ask(owned, approval(workspace_id=OTHER_WORKSPACE_ID))
    with pytest.raises(ContractSemanticError, match="is not this workspace"):
        issue(owned, grant(workspace_id=OTHER_WORKSPACE_ID))
    ask(owned)
    with pytest.raises(ContractSemanticError, match="is not this workspace"):
        decide(owned, decided(workspace_id=OTHER_WORKSPACE_ID))
    assert row_counts(owned) == dict(before, **{APPROVALS: 1})


# --- the run aggregate stays honest --------------------------------------------------


def test_read_run_reports_approvals_and_grants_without_inventing_the_rest(
    owned: m1.Owned,
) -> None:
    """Still a `RunSnapshot`: the effect family has no store, so it is not reported."""
    waiting(owned)
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.approvals == ()
    assert snapshot.capability_grants == ()

    r202.add_policy(owned)
    ask(owned)
    decide(owned)
    issue(owned)
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.approvals == (decided(),)
    assert snapshot.capability_grants == (grant(),)
    assert not hasattr(snapshot, "effects")
    validate_approval(
        snapshot.approvals[0], run_id=RUN_ID, workspace_id=WORKSPACE_ID
    )
    assert snapshot.policy is not None
    validate_capability_grant(
        snapshot.capability_grants[0],
        run_id=RUN_ID,
        workspace_id=WORKSPACE_ID,
        policy=snapshot.policy,
    )


# --- corruption fails closed ----------------------------------------------------------


def corrupt(holder: m1.Owned, *statements: str) -> sqlite3.Connection:
    """One file edited outside this database's own guards, which is the case reads exist for."""
    return r102.tamper(
        holder, f"DROP TRIGGER omnivia_guard_{GRANTS.removeprefix('omnivia_')}_update",
        *statements,
    )


def seeded_grant(holder: m1.Owned) -> str:
    waiting(holder)
    r202.add_policy(holder)
    issue(holder)
    return to_canonical_json(grant().to_wire())


def test_tampered_grant_bytes_are_refused_rather_than_returned(owned: m1.Owned) -> None:
    document = seeded_grant(owned)
    # Same length, so the byte-length CHECK still holds and only the digest can tell.
    edited = document.replace("runtime.execute", "runtime.executX")
    assert len(edited) == len(document) and edited != document
    connection = corrupt(owned, f"UPDATE {GRANTS} SET grant_json = '{edited}'")
    try:
        with pytest.raises(StorageError, match="does not match its recorded digest"):
            read_capability_grant(
                connection, workspace_id=WORKSPACE_ID, capability_grant_id=GRANT_ID
            )
    finally:
        connection.close()


def test_a_tampered_grant_byte_length_is_refused_rather_than_ignored(
    owned: m1.Owned,
) -> None:
    """The CHECK holds this only for writes through this database; a read proves it again."""
    seeded_grant(owned)
    connection = corrupt(
        owned,
        "PRAGMA ignore_check_constraints = ON",
        f"UPDATE {GRANTS} SET grant_byte_length = grant_byte_length + 1",
    )
    try:
        with pytest.raises(StorageError, match="does not match its recorded byte length"):
            read_capability_grant(
                connection, workspace_id=WORKSPACE_ID, capability_grant_id=GRANT_ID
            )
    finally:
        connection.close()


def test_non_canonical_grant_bytes_are_refused_even_when_they_decode(
    owned: m1.Owned,
) -> None:
    """A document whose digest nobody else can reproduce is not this document."""
    document = seeded_grant(owned)
    spaced = document.replace(",", ", ")
    connection = corrupt(
        owned,
        f"UPDATE {GRANTS} SET grant_json = '{spaced}', "
        f"grant_digest = '{r202.digest_of(spaced)}', "
        f"grant_byte_length = {len(spaced.encode('utf-8'))}",
    )
    try:
        with pytest.raises(StorageError, match="is not canonical JSON"):
            read_capability_grant(
                connection, workspace_id=WORKSPACE_ID, capability_grant_id=GRANT_ID
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "column,value",
    (
        ("capability_grant_id", "'cap-9999'"),
        ("run_id", "'run-0002'"),
        ("policy_snapshot_id", "'policy-0002'"),
        ("granted_at_us", str(BASE_US + 999 * MS)),
    ),
)
def test_a_grant_selector_that_contradicts_the_document_fails_closed(
    owned: m1.Owned, column: str, value: str
) -> None:
    """The indexed columns are copies of the document, and a copy that drifted is a lie."""
    seeded_grant(owned)
    r102.admit(
        owned,
        r102.admission(run_id="run-0002", job_id="job-run-0002", event_id="evt-0002"),
    )
    r202.add_policy(owned, narrowed_policy())
    connection = corrupt(owned, f"UPDATE {GRANTS} SET {column} = {value}")
    identifier = "cap-9999" if column == "capability_grant_id" else GRANT_ID
    try:
        with pytest.raises(
            StorageError, match="disagrees with the columns it is indexed by"
        ):
            read_capability_grant(
                connection, workspace_id=WORKSPACE_ID, capability_grant_id=identifier
            )
    finally:
        connection.close()


def test_a_stored_document_that_is_not_a_valid_grant_is_refused(
    owned: m1.Owned,
) -> None:
    seeded_grant(owned)
    document = to_canonical_json({"workspace_id": WORKSPACE_ID, "run_id": RUN_ID})
    connection = corrupt(
        owned,
        f"UPDATE {GRANTS} SET grant_json = '{document}', "
        f"grant_digest = '{r202.digest_of(document)}', "
        f"grant_byte_length = {len(document.encode('utf-8'))}",
    )
    try:
        with pytest.raises(StorageError, match="is not a valid CapabilityGrant"):
            read_capability_grant(
                connection, workspace_id=WORKSPACE_ID, capability_grant_id=GRANT_ID
            )
    finally:
        connection.close()


def test_a_stored_grant_whose_policy_no_longer_backs_it_fails_closed(
    owned: m1.Owned,
) -> None:
    """A row whose document names a capability its own policy never granted is corrupt."""
    seeded_grant(owned)
    forged = to_canonical_json(grant(capability_id="memory.export").to_wire())
    connection = corrupt(
        owned,
        f"UPDATE {GRANTS} SET grant_json = '{forged}', "
        f"grant_digest = '{r202.digest_of(forged)}', "
        f"grant_byte_length = {len(forged.encode('utf-8'))}",
    )
    try:
        with pytest.raises(StorageError, match="is not a valid CapabilityGrant"):
            read_capability_grant(
                connection, workspace_id=WORKSPACE_ID, capability_grant_id=GRANT_ID
            )
    finally:
        connection.close()


# --- fencing and transaction composition ----------------------------------------------


STANDALONE_WRITERS = {
    "issue_capability_grant": issue,
    "record_approval_decision": decide,
    "request_approval": ask,
}


@pytest.mark.parametrize("name", sorted(STANDALONE_WRITERS))
def test_every_standalone_writer_is_refused_by_a_stale_fence(
    owned: m1.Owned, name: str
) -> None:
    """A writer that forgot its fence is a writer nothing checks."""
    waiting(owned)
    r202.add_policy(owned)
    if name == "record_approval_decision":
        ask(owned)
    write = STANDALONE_WRITERS[name]
    before = row_counts(owned)
    stale = replace(owned, generation=owned.generation + 1)
    with pytest.raises(StaleGeneration):
        write(stale)
    assert row_counts(owned) == before
    # The same call under current authority commits, so the refusal was the fence.
    write(owned)
    assert row_counts(owned) != before


def test_a_request_a_decision_and_a_grant_commit_together_under_one_fence(
    owned: m1.Owned,
) -> None:
    waiting(owned)
    r202.add_policy(owned)
    with runtime_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        writer.request_approval(approval())
        writer.record_approval_decision(decided())
        writer.issue_capability_grant(grant())
    assert row_counts(owned) == {APPROVALS: 1, DECISIONS: 1, COMMENTS: 0, GRANTS: 1}


def test_a_composition_that_fails_later_rolls_back_what_succeeded_earlier(
    owned: m1.Owned,
) -> None:
    """One fence, one outcome: the request does not survive the grant's refusal."""
    waiting(owned)
    r202.add_policy(owned)
    before = row_counts(owned)
    with (
        pytest.raises(ContractSemanticError, match="discovery is not authority"),
        runtime_writer(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as writer,
    ):
        writer.request_approval(approval())
        writer.issue_capability_grant(grant(capability_id="memory.export"))
    assert row_counts(owned) == before
    assert (
        read_approval(
            owned.connection, workspace_id=WORKSPACE_ID, approval_id=APPROVAL_ID
        )
        is None
    )


def test_a_record_for_a_run_this_workspace_does_not_hold_is_refused(
    owned: m1.Owned,
) -> None:
    """The composite foreign keys, not rules this module writes down a second time."""
    waiting(owned)
    r202.add_policy(owned)
    before = row_counts(owned)
    with pytest.raises(sqlite3.IntegrityError):
        ask(owned, approval(run_id="run-nowhere"))
    assert row_counts(owned) == before


# --- no wire drift ---------------------------------------------------------------------


def test_this_lane_moved_no_public_contract() -> None:
    """RT-203 is persistence. The frozen application surface is untouched."""
    assert CONTRACT_VERSION == "1.3"
