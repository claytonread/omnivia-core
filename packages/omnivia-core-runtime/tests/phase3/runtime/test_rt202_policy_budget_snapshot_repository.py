"""RT-202 acceptance for the PolicySnapshot and BudgetSnapshot repositories.

The migration tests hold migration 0021 to what SQL can enforce. These hold
`storage/agent_runtime.py`'s two new writers and their readers to what SQL cannot.

*An accepted decision cannot be widened.* This is the acceptance criterion, and it is
what most of this file is about. A policy that re-grants a capability, a budget that
raises a ceiling -- cost, tokens or wall clock, including dropping a finite wall clock
for an unbounded one -- a revision or instant that goes backwards, or consumption that
falls, is refused by the accepted contract's own progression validators before a
statement is issued, so the previous decision is still there afterwards and the refused
one is nowhere. A legal narrowing is a new immutable row, never an edit to the old one.

*The record is the bytes.* Each snapshot is stored as the complete canonical v1 wire
document; the content address is the SHA-256 of exactly those bytes and the length is
their exact length. Every read recomputes both, requires canonical bytes, decodes
through the generated contract, validates the semantics and checks the columns the row
is indexed by against the document itself -- so a file edited outside this database
fails closed with a `StorageError` rather than returning something that merely parses.

*They enter their own fenced transaction, and compose under one.* Each standalone
writer is called on a bare owned connection and either commits under current authority
or leaves the database exactly as it found it; both also compose under a single
`runtime_writer` fence, because a run is admitted with a policy and a budget together.

*They read a workspace, never a record's claim about one.* Every read takes the
caller's workspace and filters on it, and a run's history has one deterministic order.

No public wire surface is touched: the contract version is asserted unchanged.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt102_agent_runtime_repository as r102
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.storage.agent_runtime import (
    StoredBudgetSnapshot,
    StoredPolicySnapshot,
    append_budget_snapshot,
    append_policy_snapshot,
    read_budget_snapshot,
    read_policy_snapshot,
    read_run,
    read_run_budget_snapshots,
    read_run_policy_snapshots,
    runtime_writer,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    BudgetSnapshot,
    ContractSemanticError,
    PolicySnapshot,
    to_canonical_json,
    validate_budget_snapshot,
    validate_policy_snapshot,
)

WORKSPACE_ID = m18.WORKSPACE_ID
OTHER_WORKSPACE_ID = m18.OTHER_WORKSPACE_ID
BASE_US = m18.BASE_US
JOB_ID = m18.JOB_ID
RUN_ID = m18.RUN_ID
MS = r102.MS

POLICY_TABLE = "omnivia_runtime_policy_snapshots"
BUDGET_TABLE = "omnivia_runtime_budget_snapshots"
TABLES = (POLICY_TABLE, BUDGET_TABLE)

POLICY_ID = "policy-0001"
BUDGET_ID = "budget-0001"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def pinned_at(offset_ms: int = 0) -> str:
    """The run's admission instant, plus milliseconds, as a canonical UTC timestamp.

    Derived from `BASE_US` rather than written out, because 0021 refuses a snapshot
    pinned before the run it belongs to, and a hand-typed year would silently drift from
    whatever instant `m18` admits its run at.
    """
    moment = _EPOCH + timedelta(microseconds=BASE_US + offset_ms * MS)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def policy(**overrides: Any) -> PolicySnapshot:
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "policy_snapshot_id": POLICY_ID,
        "run_id": RUN_ID,
        "revision": 1,
        "pinned_at": pinned_at(),
        "granted_capabilities": ("memory.read", "memory.write"),
        "discovered_capabilities": ("memory.export", "memory.read", "memory.write"),
        "decision_reason": "workspace_default",
        "audit_reference": m18.audit_ref_for(JOB_ID),
    }
    values.update(overrides)
    return PolicySnapshot(**values)


def budget(**overrides: Any) -> BudgetSnapshot:
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "budget_snapshot_id": BUDGET_ID,
        "run_id": RUN_ID,
        "revision": 1,
        "pinned_at": pinned_at(),
        "max_cost_units": 1000,
        "consumed_cost_units": 12,
        "max_token_units": 200_000,
        "consumed_token_units": 4096,
        "max_wall_clock_ms": 600_000,
    }
    values.update(overrides)
    return BudgetSnapshot(**values)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def add_policy(holder: m1.Owned, snapshot: PolicySnapshot | None = None) -> None:
    append_policy_snapshot(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        snapshot=snapshot or policy(),
    )


def add_budget(holder: m1.Owned, snapshot: BudgetSnapshot | None = None) -> None:
    append_budget_snapshot(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        snapshot=snapshot or budget(),
    )


def row_counts(holder: m1.Owned) -> dict[str, int]:
    return {
        table: int(
            holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
        for table in TABLES
    }


def digest_of(document: str) -> str:
    return f"sha256:{sha256(document.encode('utf-8')).hexdigest()}"


# --- round trip and content address -------------------------------------------------


def test_both_snapshots_round_trip_as_the_exact_contract_records(
    owned: m1.Owned,
) -> None:
    """Held to `semantics_runtime`'s validators, not to this file's idea of the shapes."""
    r102.admit(owned)
    add_policy(owned)
    add_budget(owned)

    stored_policy = read_policy_snapshot(
        owned.connection, workspace_id=WORKSPACE_ID, policy_snapshot_id=POLICY_ID
    )
    assert isinstance(stored_policy, StoredPolicySnapshot)
    assert stored_policy.snapshot == policy()
    validate_policy_snapshot(
        stored_policy.snapshot, run_id=RUN_ID, workspace_id=WORKSPACE_ID
    )
    assert stored_policy.snapshot.granted_capabilities == ("memory.read", "memory.write")
    assert stored_policy.snapshot.discovered_capabilities == (
        "memory.export",
        "memory.read",
        "memory.write",
    )

    stored_budget = read_budget_snapshot(
        owned.connection, workspace_id=WORKSPACE_ID, budget_snapshot_id=BUDGET_ID
    )
    assert isinstance(stored_budget, StoredBudgetSnapshot)
    assert stored_budget.snapshot == budget()
    validate_budget_snapshot(
        stored_budget.snapshot, run_id=RUN_ID, workspace_id=WORKSPACE_ID
    )
    assert stored_budget.snapshot.max_wall_clock_ms == 600_000


def test_the_content_address_is_the_digest_of_the_canonical_wire_document(
    owned: m1.Owned,
) -> None:
    """Hash-addressed: the address is SHA-256 of exactly the bytes that were stored."""
    r102.admit(owned)
    add_policy(owned)
    add_budget(owned)
    for record, stored in (
        (
            policy(),
            read_policy_snapshot(
                owned.connection, workspace_id=WORKSPACE_ID, policy_snapshot_id=POLICY_ID
            ),
        ),
        (
            budget(),
            read_budget_snapshot(
                owned.connection, workspace_id=WORKSPACE_ID, budget_snapshot_id=BUDGET_ID
            ),
        ),
    ):
        document = to_canonical_json(record.to_wire())
        assert stored is not None
        assert stored.content_address == digest_of(document)
        assert stored.content_length_bytes == len(document.encode("utf-8"))
        # The address is the document's, not the identifier's: nothing here derives one
        # from the other, and a snapshot id is never a digest.
        assert stored.content_address != record.to_wire()["run_id"]

    stored_row = owned.connection.execute(
        f"SELECT snapshot_json, snapshot_digest, snapshot_byte_length FROM {POLICY_TABLE}"
    ).fetchone()
    assert stored_row[0] == to_canonical_json(policy().to_wire())
    assert stored_row[1] == digest_of(str(stored_row[0]))
    assert stored_row[2] == len(str(stored_row[0]).encode("utf-8"))


def test_the_largest_policy_the_contract_accepts_is_storable(owned: m1.Owned) -> None:
    """A column that refused a legal record would be a bound storage has no right to set.

    The accepted contract allows 256 granted and 256 discovered capability identifiers of
    up to 128 characters each, which is a document well past the 64 KiB an event detail
    gets, so 0021 bounds its own at twice that and this is the record that proves it.
    """
    r102.admit(owned)
    identifiers = tuple(
        f"c{index:03d}." + "a" * (128 - len(f"c{index:03d}.")) for index in range(512)
    )
    largest = policy(
        granted_capabilities=identifiers[:256],
        discovered_capabilities=identifiers[256:],
    )
    document = to_canonical_json(largest.to_wire())
    assert len(document.encode("utf-8")) > 65_536
    add_policy(owned, largest)
    stored = read_policy_snapshot(
        owned.connection, workspace_id=WORKSPACE_ID, policy_snapshot_id=POLICY_ID
    )
    assert stored is not None
    assert stored.snapshot == largest
    assert stored.content_length_bytes == len(document.encode("utf-8"))


def test_a_missing_snapshot_reads_as_absent_rather_than_empty(owned: m1.Owned) -> None:
    r102.admit(owned)
    assert (
        read_policy_snapshot(
            owned.connection, workspace_id=WORKSPACE_ID, policy_snapshot_id="policy-none"
        )
        is None
    )
    assert (
        read_budget_snapshot(
            owned.connection, workspace_id=WORKSPACE_ID, budget_snapshot_id="budget-none"
        )
        is None
    )
    assert (
        read_run_policy_snapshots(
            owned.connection, workspace_id=WORKSPACE_ID, run_id="run-absent"
        )
        == ()
    )
    assert (
        read_run_budget_snapshots(
            owned.connection, workspace_id=WORKSPACE_ID, run_id="run-absent"
        )
        == ()
    )


def test_no_reader_answers_for_a_workspace_it_was_not_asked_about(
    owned: m1.Owned,
) -> None:
    """The workspace arrives as an argument, never out of the record being read."""
    r102.admit(owned)
    add_policy(owned)
    add_budget(owned)
    assert (
        read_policy_snapshot(
            owned.connection,
            workspace_id=OTHER_WORKSPACE_ID,
            policy_snapshot_id=POLICY_ID,
        )
        is None
    )
    assert (
        read_budget_snapshot(
            owned.connection,
            workspace_id=OTHER_WORKSPACE_ID,
            budget_snapshot_id=BUDGET_ID,
        )
        is None
    )
    assert (
        read_run_policy_snapshots(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, run_id=RUN_ID
        )
        == ()
    )
    assert (
        read_run_budget_snapshots(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, run_id=RUN_ID
        )
        == ()
    )


def test_a_snapshot_naming_another_workspace_is_refused_before_the_write(
    owned: m1.Owned,
) -> None:
    """The writer's workspace decides, so a record cannot smuggle in its own."""
    r102.admit(owned)
    before = row_counts(owned)
    with pytest.raises(ContractSemanticError, match="is not this workspace"):
        add_policy(owned, policy(workspace_id=OTHER_WORKSPACE_ID))
    with pytest.raises(ContractSemanticError, match="is not this workspace"):
        add_budget(owned, budget(workspace_id=OTHER_WORKSPACE_ID))
    assert row_counts(owned) == before


# --- narrowing appends, widening does not ---------------------------------------------


def test_a_narrower_successor_is_a_new_immutable_record_beside_the_first(
    owned: m1.Owned,
) -> None:
    """The accepted decision survives its own narrowing: two rows, two addresses."""
    r102.admit(owned)
    add_policy(owned)
    add_budget(owned)
    narrowed_policy = policy(
        policy_snapshot_id="policy-0002",
        revision=2,
        pinned_at=pinned_at(1),
        granted_capabilities=("memory.read",),
    )
    narrowed_budget = budget(
        budget_snapshot_id="budget-0002",
        revision=2,
        pinned_at=pinned_at(1),
        max_cost_units=500,
        consumed_cost_units=40,
        max_wall_clock_ms=300_000,
    )
    add_policy(owned, narrowed_policy)
    add_budget(owned, narrowed_budget)

    policies = read_run_policy_snapshots(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    budgets = read_run_budget_snapshots(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert [entry.snapshot for entry in policies] == [policy(), narrowed_policy]
    assert [entry.snapshot for entry in budgets] == [budget(), narrowed_budget]
    assert policies[0].content_address != policies[1].content_address
    assert budgets[0].content_address != budgets[1].content_address
    # The first decision is still readable, byte for byte, by its own identifier.
    first = read_policy_snapshot(
        owned.connection, workspace_id=WORKSPACE_ID, policy_snapshot_id=POLICY_ID
    )
    assert first is not None and first.snapshot == policy()


def test_absent_to_finite_wall_clock_is_a_narrowing_and_appends(owned: m1.Owned) -> None:
    """Absent means unbounded, so pinning a finite ceiling on top of it narrows."""
    r102.admit(owned)
    unbounded = budget(max_wall_clock_ms=None)
    add_budget(owned, unbounded)
    bounded = budget(
        budget_snapshot_id="budget-0002",
        revision=2,
        pinned_at=pinned_at(1),
        max_wall_clock_ms=1_000,
    )
    add_budget(owned, bounded)
    assert [
        entry.snapshot
        for entry in read_run_budget_snapshots(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
    ] == [unbounded, bounded]


WIDENED_POLICIES = {
    "regrants a capability": policy(
        policy_snapshot_id="policy-0002",
        revision=2,
        pinned_at=pinned_at(1),
        granted_capabilities=("memory.export", "memory.read", "memory.write"),
    ),
    "revision regresses": policy(
        policy_snapshot_id="policy-0002", revision=1, pinned_at=pinned_at(1)
    ),
    "instant regresses": policy(
        policy_snapshot_id="policy-0002", revision=2, pinned_at=pinned_at(-1)
    ),
}

WIDENED_BUDGETS = {
    "raises the cost ceiling": budget(
        budget_snapshot_id="budget-0002",
        revision=2,
        pinned_at=pinned_at(1),
        max_cost_units=5_000,
    ),
    "raises the token ceiling": budget(
        budget_snapshot_id="budget-0002",
        revision=2,
        pinned_at=pinned_at(1),
        max_token_units=500_000,
    ),
    "raises the wall clock ceiling": budget(
        budget_snapshot_id="budget-0002",
        revision=2,
        pinned_at=pinned_at(1),
        max_wall_clock_ms=1_200_000,
    ),
    "drops the wall clock ceiling": budget(
        budget_snapshot_id="budget-0002",
        revision=2,
        pinned_at=pinned_at(1),
        max_wall_clock_ms=None,
    ),
    "revision regresses": budget(
        budget_snapshot_id="budget-0002", revision=1, pinned_at=pinned_at(1)
    ),
    "consumption regresses": budget(
        budget_snapshot_id="budget-0002",
        revision=2,
        pinned_at=pinned_at(1),
        consumed_token_units=0,
    ),
}


@pytest.mark.parametrize("case", sorted(WIDENED_POLICIES))
def test_a_widening_policy_leaves_the_accepted_decision_and_inserts_nothing(
    owned: m1.Owned, case: str
) -> None:
    r102.admit(owned)
    add_policy(owned)
    before = row_counts(owned)
    with pytest.raises(ContractSemanticError):
        add_policy(owned, WIDENED_POLICIES[case])
    assert row_counts(owned) == before
    remaining = read_run_policy_snapshots(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert [entry.snapshot for entry in remaining] == [policy()]


@pytest.mark.parametrize("case", sorted(WIDENED_BUDGETS))
def test_a_widening_budget_leaves_the_accepted_decision_and_inserts_nothing(
    owned: m1.Owned, case: str
) -> None:
    r102.admit(owned)
    add_budget(owned)
    before = row_counts(owned)
    with pytest.raises(ContractSemanticError):
        add_budget(owned, WIDENED_BUDGETS[case])
    assert row_counts(owned) == before
    remaining = read_run_budget_snapshots(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert [entry.snapshot for entry in remaining] == [budget()]


def test_a_budget_revision_may_not_be_pinned_before_an_earlier_one(
    owned: m1.Owned,
) -> None:
    """The accepted budget contract's own progression validator states this rule.

    A history whose later revision was pinned earlier than the one it succeeds is not a
    history anybody can read in order, whatever the two ceilings say, so it is refused
    before a statement is issued and nothing is inserted. 0021's own instant guard
    restates the same rule in SQL as defence in depth, proved directly against the
    schema by the migration tests.
    """
    r102.admit(owned)
    add_budget(owned, budget(pinned_at=pinned_at(2)))
    before = row_counts(owned)
    with pytest.raises(ContractSemanticError, match="pinned_at regresses"):
        add_budget(
            owned,
            budget(
                budget_snapshot_id="budget-0002", revision=2, pinned_at=pinned_at(1)
            ),
        )
    assert row_counts(owned) == before


def test_a_second_decision_at_the_same_revision_cannot_replace_the_first(
    owned: m1.Owned,
) -> None:
    """A replacement is not a successor, under a new identifier or the same one.

    Refused here by the accepted revision rule before a statement is issued; the unique
    per-run revision that backs it up in SQL is proved by the migration tests.
    """
    r102.admit(owned)
    add_policy(owned)
    before = row_counts(owned)
    with pytest.raises(ContractSemanticError, match="does not advance"):
        add_policy(owned, policy(policy_snapshot_id="policy-0002"))
    with pytest.raises(ContractSemanticError, match="does not advance"):
        add_policy(owned, policy(granted_capabilities=("memory.read",)))
    assert row_counts(owned) == before
    stored = read_run_policy_snapshots(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert [entry.snapshot for entry in stored] == [policy()]


def test_a_broader_decision_for_another_run_is_a_different_immutable_snapshot(
    owned: m1.Owned,
) -> None:
    """Monotonicity is per run: a second run's own admission is not the first's widening."""
    r102.admit(owned)
    add_policy(owned, policy(granted_capabilities=("memory.read",)))
    second = r102.admit(owned, r102.admission(run_id="run-0002", job_id="job-run-0002", event_id="evt-0002"))
    broader = policy(
        policy_snapshot_id="policy-0002",
        run_id=second.run_id,
        granted_capabilities=("memory.export", "memory.read", "memory.write"),
        audit_reference=m18.audit_ref_for(second.job_id),
    )
    add_policy(owned, broader)

    first = read_policy_snapshot(
        owned.connection, workspace_id=WORKSPACE_ID, policy_snapshot_id=POLICY_ID
    )
    other = read_policy_snapshot(
        owned.connection, workspace_id=WORKSPACE_ID, policy_snapshot_id="policy-0002"
    )
    assert first is not None and other is not None
    assert first.snapshot.granted_capabilities == ("memory.read",)
    assert other.snapshot == broader
    assert first.content_address != other.content_address
    assert [
        entry.snapshot
        for entry in read_run_policy_snapshots(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
    ] == [first.snapshot]


# --- the run aggregate stays honest ----------------------------------------------------


def test_read_run_reports_the_latest_decisions_and_none_where_there_are_none(
    owned: m1.Owned,
) -> None:
    """Optional because a run really can have neither, not to make a type pass."""
    r102.admit(owned)
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.policy is None
    assert snapshot.budget is None

    add_policy(owned)
    add_budget(owned)
    add_policy(
        owned,
        policy(
            policy_snapshot_id="policy-0002",
            revision=2,
            pinned_at=pinned_at(1),
            granted_capabilities=("memory.read",),
        ),
    )
    add_budget(
        owned,
        budget(
            budget_snapshot_id="budget-0002",
            revision=2,
            pinned_at=pinned_at(1),
            max_cost_units=500,
        ),
    )
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.policy is not None and snapshot.budget is not None
    assert snapshot.policy.revision == 2
    assert snapshot.policy.granted_capabilities == ("memory.read",)
    assert snapshot.budget.revision == 2
    assert snapshot.budget.max_cost_units == 500
    validate_policy_snapshot(snapshot.policy, run_id=RUN_ID, workspace_id=WORKSPACE_ID)
    validate_budget_snapshot(snapshot.budget, run_id=RUN_ID, workspace_id=WORKSPACE_ID)


# --- corruption fails closed -----------------------------------------------------------


def stored_policy_row(holder: m1.Owned) -> tuple[Any, ...]:
    row = holder.connection.execute(
        f"SELECT snapshot_json, snapshot_digest, snapshot_byte_length FROM {POLICY_TABLE}"
    ).fetchone()
    assert row is not None
    return tuple(row)


def corrupt(holder: m1.Owned, *statements: str) -> sqlite3.Connection:
    """One file edited outside this database's own guards, which is the case reads exist for."""
    return r102.tamper(
        holder, "DROP TRIGGER omnivia_guard_runtime_policy_snapshots_update", *statements
    )


def test_tampered_bytes_are_refused_rather_than_returned(owned: m1.Owned) -> None:
    r102.admit(owned)
    add_policy(owned)
    document = str(stored_policy_row(owned)[0])
    # Same length, so the byte-length CHECK still holds and only the digest can tell.
    edited = document.replace("workspace_default", "workspace_defaulX")
    assert len(edited) == len(document) and edited != document
    connection = corrupt(
        owned, f"UPDATE {POLICY_TABLE} SET snapshot_json = '{edited}'"
    )
    try:
        with pytest.raises(StorageError, match="does not match its recorded digest"):
            read_policy_snapshot(
                connection, workspace_id=WORKSPACE_ID, policy_snapshot_id=POLICY_ID
            )
    finally:
        connection.close()


def test_a_tampered_digest_is_refused_rather_than_believed(owned: m1.Owned) -> None:
    r102.admit(owned)
    add_policy(owned)
    connection = corrupt(
        owned,
        f"UPDATE {POLICY_TABLE} SET snapshot_digest = 'sha256:{'e' * 64}'",
    )
    try:
        with pytest.raises(StorageError, match="does not match its recorded digest"):
            read_policy_snapshot(
                connection, workspace_id=WORKSPACE_ID, policy_snapshot_id=POLICY_ID
            )
    finally:
        connection.close()


def test_a_tampered_byte_length_is_refused_rather_than_ignored(owned: m1.Owned) -> None:
    """The CHECK holds this only for writes through this database; a read proves it again."""
    r102.admit(owned)
    add_policy(owned)
    connection = corrupt(
        owned,
        "PRAGMA ignore_check_constraints = ON",
        f"UPDATE {POLICY_TABLE} SET snapshot_byte_length = snapshot_byte_length + 1",
    )
    try:
        with pytest.raises(StorageError, match="does not match its recorded byte length"):
            read_policy_snapshot(
                connection, workspace_id=WORKSPACE_ID, policy_snapshot_id=POLICY_ID
            )
    finally:
        connection.close()


def test_non_canonical_bytes_are_refused_even_when_they_decode(owned: m1.Owned) -> None:
    """A document whose digest nobody else can reproduce is not this document."""
    r102.admit(owned)
    add_policy(owned)
    spaced = to_canonical_json(policy().to_wire()).replace(",", ", ")
    connection = corrupt(
        owned,
        f"UPDATE {POLICY_TABLE} SET snapshot_json = '{spaced}', "
        f"snapshot_digest = '{digest_of(spaced)}', "
        f"snapshot_byte_length = {len(spaced.encode('utf-8'))}",
    )
    try:
        with pytest.raises(StorageError, match="is not canonical JSON"):
            read_policy_snapshot(
                connection, workspace_id=WORKSPACE_ID, policy_snapshot_id=POLICY_ID
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "column,value",
    (
        ("revision", "7"),
        ("pinned_at_us", str(BASE_US + 999 * MS)),
        ("policy_snapshot_id", "'policy-9999'"),
        ("run_id", "'run-0002'"),
    ),
)
def test_a_selector_that_contradicts_the_document_fails_closed(
    owned: m1.Owned, column: str, value: str
) -> None:
    """The indexed columns are copies of the document, and a copy that drifted is a lie."""
    r102.admit(owned)
    r102.admit(owned, r102.admission(run_id="run-0002", job_id="job-run-0002", event_id="evt-0002"))
    add_policy(owned)
    connection = corrupt(
        owned, f"UPDATE {POLICY_TABLE} SET {column} = {value}"
    )
    identifier = "policy-9999" if column == "policy_snapshot_id" else POLICY_ID
    try:
        with pytest.raises(
            StorageError, match="disagrees with the columns it is indexed by"
        ):
            read_policy_snapshot(
                connection, workspace_id=WORKSPACE_ID, policy_snapshot_id=identifier
            )
    finally:
        connection.close()


def test_a_stored_document_that_is_not_a_valid_snapshot_is_refused(
    owned: m1.Owned,
) -> None:
    r102.admit(owned)
    add_policy(owned)
    document = to_canonical_json({"workspace_id": WORKSPACE_ID, "run_id": RUN_ID})
    connection = corrupt(
        owned,
        f"UPDATE {POLICY_TABLE} SET snapshot_json = '{document}', "
        f"snapshot_digest = '{digest_of(document)}', "
        f"snapshot_byte_length = {len(document.encode('utf-8'))}",
    )
    try:
        with pytest.raises(StorageError, match="is not a valid PolicySnapshot"):
            read_policy_snapshot(
                connection, workspace_id=WORKSPACE_ID, policy_snapshot_id=POLICY_ID
            )
    finally:
        connection.close()


# --- fencing and transaction composition -----------------------------------------------


STANDALONE_WRITERS = {
    "append_policy_snapshot": add_policy,
    "append_budget_snapshot": add_budget,
}


@pytest.mark.parametrize("name", sorted(STANDALONE_WRITERS))
def test_every_standalone_writer_is_refused_by_a_stale_fence(
    owned: m1.Owned, name: str
) -> None:
    """A writer that forgot its fence is a writer nothing checks."""
    r102.admit(owned)
    write = STANDALONE_WRITERS[name]
    before = row_counts(owned)
    stale = replace(owned, generation=owned.generation + 1)
    with pytest.raises(StaleGeneration):
        write(stale)
    assert row_counts(owned) == before
    # The same call under current authority commits, so the refusal was the fence.
    write(owned)
    assert row_counts(owned) != before


def test_a_policy_and_a_budget_commit_together_under_one_fence(owned: m1.Owned) -> None:
    """The seam RT-104 needs: a run is admitted with both decisions, or with neither."""
    r102.admit(owned)
    with runtime_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        writer.append_policy_snapshot(policy())
        writer.append_budget_snapshot(budget())
    assert row_counts(owned) == dict.fromkeys(TABLES, 1)
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.policy == policy()
    assert snapshot.budget == budget()


def test_a_composition_that_fails_later_rolls_back_what_succeeded_earlier(
    owned: m1.Owned,
) -> None:
    """One fence, one outcome: the policy does not survive the budget's refusal."""
    r102.admit(owned)
    before = row_counts(owned)
    with (
        pytest.raises(ContractSemanticError, match="exceeds max_cost_units"),
        runtime_writer(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as writer,
    ):
        writer.append_policy_snapshot(policy())
        writer.append_budget_snapshot(budget(consumed_cost_units=100_000))
    assert row_counts(owned) == before
    assert (
        read_policy_snapshot(
            owned.connection, workspace_id=WORKSPACE_ID, policy_snapshot_id=POLICY_ID
        )
        is None
    )


def test_a_snapshot_for_a_run_this_workspace_does_not_hold_is_refused(
    owned: m1.Owned,
) -> None:
    """The composite foreign key, not a rule this module writes down a second time."""
    r102.admit(owned)
    before = row_counts(owned)
    with pytest.raises(sqlite3.IntegrityError):
        add_policy(owned, policy(run_id="run-nowhere"))
    assert row_counts(owned) == before


# --- no wire drift ---------------------------------------------------------------------


def test_this_lane_moved_no_public_contract() -> None:
    """RT-202 is persistence. The frozen application surface is untouched."""
    assert CONTRACT_VERSION == "1.3"
