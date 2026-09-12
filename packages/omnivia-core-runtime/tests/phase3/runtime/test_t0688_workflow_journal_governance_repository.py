"""T-0688 IP-07 acceptance for parity, journal integrity, quarantine and retention.

The 0035 migration tests hold the schema to what SQL can enforce, and the bundle
repository tests hold the transition writer to what SQL cannot. These hold the four
judgement records the same way.

*Parity compares the bytes, and only where there are two writers.* The bundle-derived
digest is recomputed from the stored bundle every time, never taken from a caller. `R0`
has no bundle writer to disagree with and `R2` is no longer dual-write, so both refuse.
One report per bundle, replayable exactly and never rewritten, and a promotion is decided
over a declared population -- so a missing report blocks it exactly as a diverged one
does, and declaring nothing proves nothing.

*Integrity is verified against what the bundles say should be there.* A row that is gone
is a `sequence_gap` at the sequence it is gone from -- first, middle or last alike -- and
a row that is there and cannot be believed is an `integrity_failure` at its own. `R0`
observes and never quarantines; `R1` and `R2` quarantine in the same fenced transaction
as the report, and a quarantined Run refuses to resume under `RT_JOURNAL_QUARANTINED`
without one public Run fact moving, and without a single row being repaired or removed.

*A release is a decision, not a repair.* It names an actor and a reason, it is fenced, it
appends, and there is nothing to release when nothing is held. One Run's release never
answers for another's.

*A retention boundary records; it never deletes.* A boundary that names a removed range
must say so, the posture it leaves cannot be quietly restored by a later boundary, and
the Audit record it cites has to already exist in this workspace -- which 0035's own
trigger, not this module, is what says.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt102_agent_runtime_repository as r102
import test_t0688_workflow_runtime_hardening_repository as ip06
import test_t0688_workflow_transition_bundle_repository as ip07
import test_workflow_runs_migration as m27
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.storage.backup import (
    InstallationLayout,
    create_verified_backup,
    new_attempt_id,
    restore_backup,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    StorageError,
    open_database,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.workflow_runtime_hardening import (
    JournalGovernanceRefused,
    StoredRuntimeDefinitionBinding,
    TransitionBundleRefused,
    evaluate_journal_resume,
    evaluate_parity_promotion,
    journal_genesis_link,
    read_journal_integrity_report,
    read_journal_quarantine,
    read_journal_retention_posture,
    read_runtime_journal_events,
    read_transition_parity_report,
    record_retention_boundary,
    record_transition_parity_report,
    release_journal_quarantine,
    verify_journal_integrity,
)

from omnivia_core.contracts.v1.canonical_json import canonicalize

WORKSPACE_ID = ip06.WORKSPACE_ID
OTHER_WORKSPACE_ID = m18.OTHER_WORKSPACE_ID
RUN_ID = ip06.RUN_ID
OTHER_RUN_ID = "run-ip07g-other"
OTHER_JOB_ID = "job-ip07g-other"
OTHER_BINDING_ID = "binding-ip07g-other"

BUNDLES = ip07.BUNDLES
JOURNAL = ip07.JOURNAL
PARITY = "omnivia_workflow_transition_parity_reports"
INTEGRITY = "omnivia_workflow_journal_integrity_reports"
QUARANTINE = "omnivia_workflow_journal_quarantine_events"
RETENTION = "omnivia_workflow_journal_retention_boundaries"
AUDIT = "omnivia_application_audit_events"

GOVERNANCE_TABLES = (PARITY, INTEGRITY, QUARANTINE, RETENTION)

#: Three transitions, so a missing first, middle and final journal row are three
#: different sequences rather than three names for the same one.
CHAIN = 3

PARITY_ID = "parity-ip07g-one"
REPORT_ID = "integrity-ip07g-one"
SECOND_REPORT_ID = "integrity-ip07g-two"
BOUNDARY_ID = "boundary-ip07g-one"
RETENTION_AUDIT = "audit-ip07g-retention"

ACTOR = "core-operator"
REASON = "operator_release"

#: A well-formed digest no bundle in this suite derives.
FOREIGN_DIGEST = "sha256:" + "a" * 64

PARITY_AT = ip06.instant(ip07.RECORDED_BASE_US + 100)
VERIFIED_AT = ip06.instant(ip07.RECORDED_BASE_US + 200)
RELEASED_AT = ip06.instant(ip07.RECORDED_BASE_US + 300)
BOUNDARY_AT = ip06.instant(ip07.RECORDED_BASE_US + 400)


# --- the workspace under test ---------------------------------------------------------


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


@pytest.fixture
def bound(owned: m1.Owned) -> StoredRuntimeDefinitionBinding:
    """ip06's seeded plan and run, with the verified binding transitions apply against."""
    ip06.prepare(owned)
    return ip06.admit(owned)


def chain_link(sequence: int) -> str:
    """The one link the event at `sequence` may name: genesis, then its predecessor."""
    if sequence == 0:
        return journal_genesis_link(RUN_ID)
    return ip06.digest_of(canonicalize(chain_event(sequence - 1)))


def chain_event(sequence: int) -> dict[str, Any]:
    return ip07.journal_event(sequence, chain_link(sequence))


def chain_bundle(sequence: int) -> dict[str, Any]:
    return ip07.bundle(sequence, event=chain_event(sequence))


@pytest.fixture
def journalled(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> StoredRuntimeDefinitionBinding:
    """That run, with a three-link journal every verification here is measured against."""
    for sequence in range(CHAIN):
        ip07.apply(owned, bound, chain_bundle(sequence), ip07.payload(sequence))
    return bound


def bundle_ids(count: int = CHAIN) -> tuple[str, ...]:
    return tuple(f"bundle-ip07-{sequence}" for sequence in range(count))


def bundle_digest(sequence: int = 0) -> str:
    """The address of the bundle's stored bytes, which parity is measured against."""
    return ip06.digest_of(canonicalize(chain_bundle(sequence)))


def seed_other_run(holder: m1.Owned) -> StoredRuntimeDefinitionBinding:
    """A second bound Run in this workspace, with its own binding and first transition."""
    m27.seed_runtime_run(holder, run_id=OTHER_RUN_ID, job_id=OTHER_JOB_ID)
    binding = ip06.admit(
        holder,
        ip06.admission(
            run_id=OTHER_RUN_ID, binding=ip06.binding(bindingId=OTHER_BINDING_ID)
        ),
    )
    event = {
        "eventId": "event-ip07g-other-0",
        "runId": OTHER_RUN_ID,
        "sequence": 0,
        "previousIntegrityLink": journal_genesis_link(OTHER_RUN_ID),
        "eventKind": ip07.EVENT_KIND,
        "recordedAt": ip06.instant(ip07.RECORDED_BASE_US),
        "payloadDigest": ip06.digest_of(canonicalize(other_payload())),
    }
    ip07.apply(
        holder,
        binding,
        ip07.bundle(
            0, event=event, bundleId="bundle-ip07g-other-0", runId=OTHER_RUN_ID
        ),
        other_payload(),
    )
    return binding


def other_payload() -> dict[str, Any]:
    return {"runId": OTHER_RUN_ID, "sequence": 0, "outcome": "advanced"}


def audit_record(holder: m1.Owned, audit_ref: str = RETENTION_AUDIT) -> None:
    with m27.guarded(holder):
        m27.audit(holder, audit_ref)


def counts(connection: sqlite3.Connection) -> tuple[int, ...]:
    return tuple(
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in GOVERNANCE_TABLES
    )


def durable_history(connection: sqlite3.Connection) -> tuple[list[Any], ...]:
    """Everything a retention boundary must never have removed."""
    return tuple(
        connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3").fetchall()
        for table in (JOURNAL, BUNDLES, m27.EVIDENCE, AUDIT)
    )


def public_run_state(connection: sqlite3.Connection) -> tuple[list[Any], ...]:
    """Everything a reader of the public Workflow Run sees, as rows."""
    return tuple(
        connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
        for table in (m27.RUNS, m27.COMPLETIONS, m27.OBSERVATIONS)
    )


def reopen(holder: m1.Owned, *statements: str) -> m1.Owned:
    """Edit the file outside this database's own guards, then take the workspace back.

    `ip06.corrupt` hands back a bare connection, which is what a verified *read* has to
    fail closed on. A verification pass writes, and a bare connection holds neither the
    service writer function nor the lease those writes are guarded by -- so the edit is
    made, the file is closed, and ownership is taken again as a service would take it
    after a crash. The guard the edit dropped stays dropped; nothing below depends on it.
    """
    connection = r102.tamper(holder, *statements)
    connection.close()
    return m1.take_ownership(holder.path)


def drop(table: str, operation: str) -> str:
    return f"DROP TRIGGER omnivia_guard_{table.removeprefix('omnivia_')}_{operation}"


# --- calling conventions --------------------------------------------------------------


def parity(
    holder: m1.Owned,
    *,
    bundle_id: str = "bundle-ip07-0",
    writer_digest: str | None = None,
    report_id: str = PARITY_ID,
    stage: str = "R1",
    run_id: str = RUN_ID,
    generation: int | None = None,
) -> Any:
    return record_transition_parity_report(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation if generation is None else generation,
        report_id=report_id,
        run_id=run_id,
        bundle_id=bundle_id,
        existing_writer_digest=(
            writer_digest
            if writer_digest is not None
            else bundle_digest(int(bundle_id.rsplit("-", 1)[1]))
        ),
        rollout_stage=stage,
        recorded_at=PARITY_AT,
    )


def verify(
    holder: m1.Owned,
    *,
    stage: str = "R1",
    report_id: str = REPORT_ID,
    run_id: str = RUN_ID,
    verified_at: str = VERIFIED_AT,
    generation: int | None = None,
) -> Any:
    return verify_journal_integrity(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation if generation is None else generation,
        report_id=report_id,
        run_id=run_id,
        rollout_stage=stage,
        verified_at=verified_at,
    )


def release(
    holder: m1.Owned,
    *,
    run_id: str = RUN_ID,
    actor: str = ACTOR,
    reason: str = REASON,
    recorded_at: str = RELEASED_AT,
    decision_id: str | None = None,
    generation: int | None = None,
) -> Any:
    return release_journal_quarantine(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation if generation is None else generation,
        run_id=run_id,
        deciding_actor=actor,
        reason=reason,
        recorded_at=recorded_at,
        decision_id=decision_id,
    )


def boundary(
    holder: m1.Owned,
    *,
    boundary_id: str = BOUNDARY_ID,
    run_id: str = RUN_ID,
    first: int | None = None,
    last: int | None = None,
    resumable_after: bool = True,
    audit_ref: str = RETENTION_AUDIT,
    recorded_at: str = BOUNDARY_AT,
    generation: int | None = None,
) -> Any:
    return record_retention_boundary(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation if generation is None else generation,
        boundary_id=boundary_id,
        run_id=run_id,
        first_removed_sequence=first,
        last_removed_sequence=last,
        resumable_after=resumable_after,
        policy_ref="policy-journal-90d",
        evidence_ref="evidence-ip07g",
        audit_ref=audit_ref,
        recorded_at=recorded_at,
    )


def quarantine_of(holder: m1.Owned, run_id: str = RUN_ID) -> Any:
    return read_journal_quarantine(
        holder.connection, workspace_id=WORKSPACE_ID, run_id=run_id
    )


def resume_of(holder: m1.Owned, run_id: str = RUN_ID) -> Any:
    return evaluate_journal_resume(
        holder.connection, workspace_id=WORKSPACE_ID, run_id=run_id
    )


# --- parity: what the two writers produced --------------------------------------------


def test_a_parity_report_is_the_stored_bundle_against_the_writer_that_named_it(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """`match` is the recomputed address of the stored bundle, not a stated digest."""
    derived = bundle_digest()
    stored = parity(owned, writer_digest=derived)
    body = canonicalize(
        {
            "reportId": PARITY_ID,
            "runId": RUN_ID,
            "bundleId": "bundle-ip07-0",
            "rolloutStage": "R1",
            "existingWriterDigest": derived,
            "bundleDerivedDigest": derived,
            "status": "match",
            "recordedAt": PARITY_AT,
        }
    )
    assert stored.status == "match"
    assert stored.bundle_derived_digest == derived
    assert stored.content_address == ip06.digest_of(body)
    assert stored.content_length_bytes == len(body.encode("utf-8"))

    read = read_transition_parity_report(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        bundle_id="bundle-ip07-0",
    )
    assert read == stored
    assert read.report == dict(stored.report)  # type: ignore[union-attr]


def test_a_writer_digest_the_bundle_does_not_address_is_recorded_as_diverged(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    stored = parity(owned, writer_digest=FOREIGN_DIGEST)
    assert stored.status == "diverged"
    assert stored.existing_writer_digest == FOREIGN_DIGEST
    assert stored.bundle_derived_digest == bundle_digest()
    assert (
        evaluate_parity_promotion(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            declared_bundle_ids=["bundle-ip07-0"],
        ).eligible
        is False
    )


@pytest.mark.parametrize("stage", ["R0", "R2", "R3", "r1", ""])
def test_only_the_dual_write_stage_records_a_parity_report(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding, stage: str
) -> None:
    """`R0` runs one writer and `R2` runs the other; neither has a parity to report."""
    with pytest.raises(JournalGovernanceRefused) as refusal:
        parity(owned, stage=stage)
    assert refusal.value.diagnostic == "RT_PARITY_STAGE_NOT_DUAL_WRITE"
    assert counts(owned.connection) == (0, 0, 0, 0)


def test_a_bundle_this_run_never_recorded_carries_no_parity(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    with pytest.raises(JournalGovernanceRefused) as refusal:
        parity(owned, bundle_id="bundle-ip07-9")
    assert refusal.value.diagnostic == "RT_PARITY_BUNDLE_MISSING"
    assert counts(owned.connection) == (0, 0, 0, 0)


def test_the_same_parity_report_replays_and_appends_nothing(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    first = parity(owned)
    again = parity(owned)
    assert again == first
    assert counts(owned.connection)[0] == 1


@pytest.mark.parametrize(
    "overrides",
    [{"report_id": "parity-ip07g-two"}, {"writer_digest": FOREIGN_DIGEST}],
    ids=["another identity", "another comparison"],
)
def test_a_second_parity_judgement_of_one_bundle_refuses(
    owned: m1.Owned,
    journalled: StoredRuntimeDefinitionBinding,
    overrides: dict[str, Any],
) -> None:
    first = parity(owned)
    with pytest.raises(JournalGovernanceRefused) as refusal:
        parity(owned, **overrides)
    assert refusal.value.diagnostic == "RT_PARITY_REPORT_CONFLICT"
    assert counts(owned.connection)[0] == 1
    assert (
        read_transition_parity_report(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            bundle_id="bundle-ip07-0",
        )
        == first
    )


def test_stale_authority_records_no_parity_report(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    with pytest.raises(StaleGeneration):
        parity(owned, generation=owned.generation + 1)
    assert counts(owned.connection) == (0, 0, 0, 0)


# --- parity: promotion over a declared population -------------------------------------


def report_every_bundle(holder: m1.Owned) -> None:
    for sequence, bundle_id in enumerate(bundle_ids()):
        parity(holder, bundle_id=bundle_id, report_id=f"parity-ip07g-{sequence}")


def test_a_declared_population_all_matching_is_eligible_for_promotion(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    report_every_bundle(owned)
    eligibility = evaluate_parity_promotion(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        declared_bundle_ids=bundle_ids(),
    )
    assert eligibility.eligible is True
    assert eligibility.matched_bundle_ids == bundle_ids()
    assert eligibility.diverged_bundle_ids == ()
    assert eligibility.unreported_bundle_ids == ()


def test_a_declared_bundle_with_no_report_blocks_promotion(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """Silence is not parity: the bundle nobody reported on is the one that blocks."""
    parity(owned, bundle_id="bundle-ip07-0", report_id="parity-ip07g-0")
    eligibility = evaluate_parity_promotion(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        declared_bundle_ids=bundle_ids(),
    )
    assert eligibility.eligible is False
    assert eligibility.matched_bundle_ids == ("bundle-ip07-0",)
    assert eligibility.unreported_bundle_ids == ("bundle-ip07-1", "bundle-ip07-2")


def test_one_diverged_bundle_blocks_a_population_that_otherwise_matches(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    parity(owned, bundle_id="bundle-ip07-0", report_id="parity-ip07g-0")
    parity(
        owned,
        bundle_id="bundle-ip07-1",
        report_id="parity-ip07g-1",
        writer_digest=FOREIGN_DIGEST,
    )
    parity(owned, bundle_id="bundle-ip07-2", report_id="parity-ip07g-2")
    eligibility = evaluate_parity_promotion(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        declared_bundle_ids=bundle_ids(),
    )
    assert eligibility.eligible is False
    assert eligibility.diverged_bundle_ids == ("bundle-ip07-1",)


def test_an_empty_population_never_establishes_a_parity_pass(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """Declaring nothing is the one way to have no divergences and no evidence."""
    report_every_bundle(owned)
    eligibility = evaluate_parity_promotion(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        declared_bundle_ids=[],
    )
    assert eligibility.eligible is False
    assert eligibility.declared_bundle_ids == ()


def test_no_parity_record_answers_for_a_workspace_or_run_it_was_not_asked_about(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    report_every_bundle(owned)
    assert (
        read_transition_parity_report(
            owned.connection,
            workspace_id=OTHER_WORKSPACE_ID,
            run_id=RUN_ID,
            bundle_id="bundle-ip07-0",
        )
        is None
    )
    for workspace, run in ((OTHER_WORKSPACE_ID, RUN_ID), (WORKSPACE_ID, "run-absent")):
        eligibility = evaluate_parity_promotion(
            owned.connection,
            workspace_id=workspace,
            run_id=run,
            declared_bundle_ids=bundle_ids(),
        )
        assert eligibility.eligible is False
        assert eligibility.unreported_bundle_ids == bundle_ids()


# --- parity: corruption ---------------------------------------------------------------


def parity_edits(derived: str) -> dict[str, tuple[str, tuple[Any, ...], str, str]]:
    document = canonicalize(
        {
            "reportId": PARITY_ID,
            "runId": RUN_ID,
            "bundleId": "bundle-ip07-0",
            "rolloutStage": "R1",
            "existingWriterDigest": derived,
            "bundleDerivedDigest": derived,
            "status": "match",
            "recordedAt": PARITY_AT,
        }
    )
    spaced = document.replace(",", ", ")
    restated = document.replace(derived, FOREIGN_DIGEST)
    return {
        "bytes": (
            f"UPDATE {PARITY} SET report_json = ?",
            (document.replace("match", "matcH"),),
            "does not match its recorded digest",
            PARITY,
        ),
        "digest": (
            f"UPDATE {PARITY} SET report_digest = ?",
            ("sha256:" + "e" * 64,),
            "does not match its recorded digest",
            PARITY,
        ),
        "byte length": (
            f"UPDATE {PARITY} SET report_byte_length = report_byte_length + 1",
            (),
            "does not match its recorded byte length",
            PARITY,
        ),
        "canonical form": (
            (
                f"UPDATE {PARITY} SET report_json = ?, report_digest = ?, "
                "report_byte_length = ?"
            ),
            (spaced, ip06.digest_of(spaced), len(spaced.encode("utf-8"))),
            "is not canonical JSON",
            PARITY,
        ),
        "indexed status": (
            f"UPDATE {PARITY} SET status = 'diverged'",
            (),
            "disagrees with the columns it is indexed by",
            PARITY,
        ),
        "indexed instant": (
            f"UPDATE {PARITY} SET recorded_at_us = recorded_at_us + 1",
            (),
            "disagrees with the columns it is indexed by",
            PARITY,
        ),
        "restated comparison": (
            (
                f"UPDATE {PARITY} SET report_json = ?, report_digest = ?, "
                "report_byte_length = ?, existing_writer_digest = ?, "
                "bundle_derived_digest = ?"
            ),
            (
                restated,
                ip06.digest_of(restated),
                len(restated.encode("utf-8")),
                FOREIGN_DIGEST,
                FOREIGN_DIGEST,
            ),
            "disagrees with the bundle it was derived from",
            PARITY,
        ),
        "the bundle it reports on": (
            f"UPDATE {BUNDLES} SET bundle_json = ? WHERE bundle_id = 'bundle-ip07-0'",
            (canonicalize(chain_bundle(0)).replace("advanced", "advanceD"),),
            "a stored transition bundle does not match its recorded digest",
            BUNDLES,
        ),
    }


@pytest.mark.parametrize("case", sorted(parity_edits("sha256:" + "0" * 64)))
def test_a_tampered_parity_report_fails_closed(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding, case: str
) -> None:
    parity(owned)
    statement, parameters, expected, table = parity_edits(bundle_digest())[case]
    connection = ip06.corrupt(owned, statement, *parameters, table=table)
    try:
        with pytest.raises(StorageError, match=expected):
            read_transition_parity_report(
                connection,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                bundle_id="bundle-ip07-0",
            )
    finally:
        connection.close()


# --- parity: an offline editor that restates the whole address ------------------------


def restated(table: str, document: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    """One report document replaced whole, with its stored address recomputed for it.

    The digest and the byte length are the report's address, and an editor working on
    the file offline can restate both for whatever it wrote. What survives that is the
    shape of the document itself, which is what these cases are.
    """
    body = canonicalize(document)
    return (
        f"UPDATE {table} SET report_json = ?, report_digest = ?, report_byte_length = ?",
        (body, ip06.digest_of(body), len(body.encode("utf-8"))),
    )


def parity_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "reportId": PARITY_ID,
        "runId": RUN_ID,
        "bundleId": "bundle-ip07-0",
        "rolloutStage": "R1",
        "existingWriterDigest": bundle_digest(),
        "bundleDerivedDigest": bundle_digest(),
        "status": "match",
        "recordedAt": PARITY_AT,
    }
    document.update(overrides)
    return document


def parity_shape_edits() -> dict[str, tuple[dict[str, Any], str]]:
    dropped = parity_document()
    del dropped["rolloutStage"]
    return {
        "a member added": (
            parity_document(promotedBy="core-operator"),
            "does not hold exactly the members it is stored with",
        ),
        "a member removed": (
            dropped,
            "does not hold exactly the members it is stored with",
        ),
        "a member of another type": (
            parity_document(status=1),
            "states 'status' as a value of another type",
        ),
        "a stage no parity is recorded at": (
            parity_document(rolloutStage="R2"),
            "names a stage no parity is ever recorded at",
        ),
    }


@pytest.mark.parametrize("case", sorted(parity_shape_edits()))
def test_a_parity_report_edited_into_another_shape_fails_closed(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding, case: str
) -> None:
    """The address proves the bytes; only the shape proves this module wrote them."""
    parity(owned)
    document, expected = parity_shape_edits()[case]
    statement, parameters = restated(PARITY, document)
    connection = ip06.corrupt(owned, statement, *parameters, table=PARITY)
    try:
        with pytest.raises(StorageError, match=expected):
            read_transition_parity_report(
                connection,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                bundle_id="bundle-ip07-0",
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "statement,expected",
    [
        (
            f"UPDATE {PARITY} SET existing_writer_digest = 'sha256:zz'",
            "not spelled as this schema stores it",
        ),
        (
            f"UPDATE {PARITY} SET report_id = 'parity ip07g one'",
            "not spelled as this schema stores it",
        ),
        (
            f"UPDATE {PARITY} SET recorded_at_us = 0",
            "not a number this schema stores",
        ),
    ],
    ids=["an unspellable digest", "an unspellable identifier", "an unstorable instant"],
)
def test_a_parity_scalar_the_schema_would_have_refused_fails_closed(
    owned: m1.Owned,
    journalled: StoredRuntimeDefinitionBinding,
    statement: str,
    expected: str,
) -> None:
    """`ignore_check_constraints` writes what the CHECK would have refused; reads do not."""
    parity(owned)
    connection = ip06.corrupt(owned, statement, table=PARITY)
    try:
        with pytest.raises(StorageError, match=expected):
            read_transition_parity_report(
                connection,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                bundle_id="bundle-ip07-0",
            )
    finally:
        connection.close()


def test_a_parity_report_that_cannot_be_read_blocks_promotion_rather_than_passing_it(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    parity(owned)
    connection = ip06.corrupt(
        owned, f"UPDATE {PARITY} SET status = 'diverged'", table=PARITY
    )
    try:
        with pytest.raises(StorageError):
            evaluate_parity_promotion(
                connection,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                declared_bundle_ids=["bundle-ip07-0"],
            )
    finally:
        connection.close()


# --- integrity: an aligned journal -----------------------------------------------------


@pytest.mark.parametrize("stage", ["R0", "R1", "R2"])
def test_an_aligned_journal_verifies_at_every_stage_and_quarantines_nothing(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding, stage: str
) -> None:
    verification = verify(owned, stage=stage)
    assert verification.disposition == "recorded"
    assert verification.outcome == "verified"
    assert verification.diagnostic is None
    assert verification.first_affected_sequence is None
    assert verification.observed_head == CHAIN - 1
    assert verification.quarantined is False

    stored = read_journal_integrity_report(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID, report_id=REPORT_ID
    )
    assert stored is not None
    assert stored.outcome == "verified"
    assert stored.rollout_stage == stage
    assert stored.observed_head == CHAIN - 1
    assert stored.content_address == verification.content_address
    assert stored.report == {
        "reportId": REPORT_ID,
        "runId": RUN_ID,
        "rolloutStage": stage,
        "outcome": "verified",
        "observedHead": CHAIN - 1,
        "verifiedAt": VERIFIED_AT,
    }
    assert counts(owned.connection) == (0, 1, 0, 0)
    assert resume_of(owned).resumable is True


def test_an_empty_run_verifies_with_no_head_at_all(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    """A Run that has produced nothing has nothing missing, and says so with `-1`."""
    verification = verify(owned)
    assert (verification.outcome, verification.observed_head) == ("verified", -1)
    assert verification.quarantined is False
    assert (
        read_runtime_journal_events(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        == ()
    )


def test_the_first_event_is_verified_against_the_derived_genesis_link(
    owned: m1.Owned, bound: StoredRuntimeDefinitionBinding
) -> None:
    """One transition, and the link it must name is this Run's and no other Run's."""
    ip07.apply(owned, bound)
    events = read_runtime_journal_events(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert events[0].event["previousIntegrityLink"] == journal_genesis_link(RUN_ID)
    assert verify(owned).outcome == "verified"


def test_a_first_event_anchored_to_another_runs_genesis_is_an_integrity_failure(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    forged = canonicalize(ip07.journal_event(0, journal_genesis_link(OTHER_RUN_ID)))
    holder = reopen(
        owned,
        "PRAGMA ignore_check_constraints = ON",
        drop(JOURNAL, "update"),
        f"UPDATE {JOURNAL} SET event_json = '{forged}', "
        f"event_digest = '{ip06.digest_of(forged)}', "
        f"event_byte_length = {len(forged.encode('utf-8'))}, "
        f"previous_link_digest = '{journal_genesis_link(OTHER_RUN_ID)}' "
        "WHERE sequence = 0",
    )
    try:
        verification = verify(holder, stage="R0")
        assert verification.outcome == "integrity_failure"
        assert verification.first_affected_sequence == 0
        assert verification.diagnostic == "RT_JOURNAL_INTEGRITY_FAILURE"
    finally:
        holder.connection.close()


# --- integrity: a row that is gone -----------------------------------------------------


@pytest.mark.parametrize("missing", [0, 1, 2], ids=["first", "middle", "final"])
def test_a_missing_journal_row_is_a_sequence_gap_at_its_own_sequence(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding, missing: int
) -> None:
    """The bundles say three events exist. Which one is gone is the finding."""
    holder = reopen(
        owned,
        drop(JOURNAL, "delete"),
        f"DELETE FROM {JOURNAL} WHERE sequence = {missing}",
    )
    try:
        verification = verify(holder, stage="R0")
        assert verification.outcome == "sequence_gap"
        assert verification.diagnostic == "RT_JOURNAL_SEQUENCE_GAP"
        assert verification.first_affected_sequence == missing
        assert verification.observed_head == (
            CHAIN - 2 if missing == CHAIN - 1 else CHAIN - 1
        )
    finally:
        holder.connection.close()


@pytest.mark.parametrize("stage", ["R1", "R2"])
def test_a_journal_emptied_under_surviving_bundles_is_quarantined_and_held(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding, stage: str
) -> None:
    """The worst journal fault is the one that must not be the unenforceable one.

    A Run whose bundles all survive and whose journal is gone entirely has no event a
    disposition could name. 0035 lets a `sequence_gap` quarantine hold without one, so
    an enforcing stage records the finding and the hold in the same transaction and the
    Run stops -- rather than the fault that removed everything being the only one a
    verification pass could observe and not act on.
    """
    holder = reopen(owned, drop(JOURNAL, "delete"), f"DELETE FROM {JOURNAL}")
    try:
        verification = verify(holder, stage=stage)
        assert (verification.outcome, verification.first_affected_sequence) == (
            "sequence_gap",
            0,
        )
        assert verification.observed_head == -1
        assert verification.quarantined is True
        assert counts(holder.connection) == (0, 1, 1, 0)

        held = quarantine_of(holder)
        assert held.held is True
        assert held.event_id is None
        assert held.integrity_report_id == REPORT_ID
        assert resume_of(holder).diagnostic == "RT_JOURNAL_QUARANTINED"
        assert resume_of(holder).resumable is False
    finally:
        holder.connection.close()


def test_r0_records_an_emptied_journal_and_holds_nothing(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """Observe-only stays observe-only, even for the fault that removed everything."""
    holder = reopen(owned, drop(JOURNAL, "delete"), f"DELETE FROM {JOURNAL}")
    try:
        observed = verify(holder, stage="R0")
        assert (observed.outcome, observed.first_affected_sequence) == (
            "sequence_gap",
            0,
        )
        assert observed.quarantined is False
        assert counts(holder.connection) == (0, 1, 0, 0)
        assert resume_of(holder).resumable is True
    finally:
        holder.connection.close()


def test_a_citationless_quarantine_is_released_without_inventing_an_event(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """A release carries the absent citation forward; 0035 refuses any other value."""
    holder = reopen(owned, drop(JOURNAL, "delete"), f"DELETE FROM {JOURNAL}")
    try:
        verify(holder, stage="R1")
        released = release(holder)
        assert released.held is False
        assert released.event_id is None
        assert released.deciding_actor == ACTOR
        assert resume_of(holder).resumable is True
        assert counts(holder.connection) == (0, 1, 2, 0)
    finally:
        holder.connection.close()


def test_a_missing_bundle_under_a_surviving_event_is_an_integrity_failure(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """The pair is the record; an event whose bundle is gone is not half of one."""
    holder = reopen(
        owned,
        drop(BUNDLES, "delete"),
        f"DELETE FROM {BUNDLES} WHERE bundle_id = 'bundle-ip07-1'",
    )
    try:
        verification = verify(holder, stage="R0")
        assert verification.outcome == "integrity_failure"
        assert verification.first_affected_sequence == 1
        assert verification.observed_head == CHAIN - 1
    finally:
        holder.connection.close()


# --- integrity: a row that cannot be believed ------------------------------------------


def corrupted_journal_edits() -> dict[str, str]:
    document = canonicalize(chain_event(1))
    body = canonicalize(ip07.payload(1))
    forged = canonicalize(ip07.journal_event(1, ip07.WRONG_LINK))
    return {
        "event bytes": (
            f"UPDATE {JOURNAL} SET event_json = "
            f"'{document.replace('advanced', 'advanceD')}' WHERE sequence = 1"
        ),
        "event digest": (
            f"UPDATE {JOURNAL} SET event_digest = 'sha256:{'e' * 64}' "
            "WHERE sequence = 1"
        ),
        "payload bytes": (
            f"UPDATE {JOURNAL} SET event_payload_json = "
            f"'{body.replace('advanced', 'advanceD')}' WHERE sequence = 1"
        ),
        "payload digest": (
            f"UPDATE {JOURNAL} SET event_payload_digest = 'sha256:{'b' * 64}', "
            f"payload_digest = 'sha256:{'b' * 64}' WHERE sequence = 1"
        ),
        "integrity link": (
            f"UPDATE {JOURNAL} SET event_json = '{forged}', "
            f"event_digest = '{ip06.digest_of(forged)}', "
            f"event_byte_length = {len(forged.encode('utf-8'))}, "
            f"previous_link_digest = '{ip07.WRONG_LINK}' WHERE sequence = 1"
        ),
    }


@pytest.mark.parametrize("case", sorted(corrupted_journal_edits()))
def test_a_row_that_cannot_be_believed_is_an_integrity_failure_at_its_sequence(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding, case: str
) -> None:
    holder = reopen(
        owned,
        "PRAGMA ignore_check_constraints = ON",
        drop(JOURNAL, "update"),
        corrupted_journal_edits()[case],
    )
    try:
        verification = verify(holder, stage="R0")
        assert verification.outcome == "integrity_failure"
        assert verification.diagnostic == "RT_JOURNAL_INTEGRITY_FAILURE"
        assert verification.first_affected_sequence == 1
        assert verification.observed_head == CHAIN - 1
    finally:
        holder.connection.close()


# --- integrity: recording the pass ------------------------------------------------------


def test_the_same_verification_replays_without_recording_a_second_report(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    first = verify(owned)
    again = verify(owned)
    assert again.disposition == "replayed"
    assert (again.outcome, again.content_address) == (
        first.outcome,
        first.content_address,
    )
    assert counts(owned.connection) == (0, 1, 0, 0)


def test_one_report_identifier_over_a_different_verification_refuses(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    verify(owned)
    with pytest.raises(JournalGovernanceRefused) as refusal:
        verify(owned, verified_at=ip06.instant(ip07.RECORDED_BASE_US + 999))
    assert refusal.value.diagnostic == "RT_JOURNAL_REPORT_CONFLICT"
    assert counts(owned.connection) == (0, 1, 0, 0)


def test_a_stage_this_schema_does_not_record_is_refused(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    with pytest.raises(StorageError, match="is not a rollout stage"):
        verify(owned, stage="R3")
    assert counts(owned.connection) == (0, 0, 0, 0)


def test_stale_authority_records_no_verification(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    with pytest.raises(StaleGeneration):
        verify(owned, generation=owned.generation + 1)
    assert counts(owned.connection) == (0, 0, 0, 0)


def test_no_integrity_report_answers_for_a_workspace_it_was_not_asked_about(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    verify(owned)
    assert (
        read_journal_integrity_report(
            owned.connection,
            workspace_id=OTHER_WORKSPACE_ID,
            run_id=RUN_ID,
            report_id=REPORT_ID,
        )
        is None
    )
    assert (
        read_journal_integrity_report(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            run_id=OTHER_RUN_ID,
            report_id=REPORT_ID,
        )
        is None
    )


def integrity_edits() -> dict[str, tuple[str, str]]:
    return {
        "bytes": (
            (
                f"UPDATE {INTEGRITY} SET report_json = "
                "replace(report_json, 'verified', 'verifieD')"
            ),
            "does not match its recorded digest",
        ),
        "byte length": (
            f"UPDATE {INTEGRITY} SET report_byte_length = report_byte_length + 1",
            "does not match its recorded byte length",
        ),
        "outcome": (
            f"UPDATE {INTEGRITY} SET outcome = 'sequence_gap'",
            "does not pair its outcome with its diagnostic",
        ),
        "diagnostic": (
            f"UPDATE {INTEGRITY} SET diagnostic = 'RT_JOURNAL_SEQUENCE_GAP'",
            "does not pair its outcome with its diagnostic",
        ),
        "indexed stage": (
            f"UPDATE {INTEGRITY} SET rollout_stage = 'R2'",
            "disagrees with the columns it is indexed by",
        ),
        "indexed head": (
            f"UPDATE {INTEGRITY} SET observed_head = 1",
            "disagrees with the columns it is indexed by",
        ),
        "indexed instant": (
            f"UPDATE {INTEGRITY} SET verified_at_us = verified_at_us + 1",
            "disagrees with the columns it is indexed by",
        ),
    }


@pytest.mark.parametrize("case", sorted(integrity_edits()))
def test_a_tampered_integrity_report_fails_closed(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding, case: str
) -> None:
    verify(owned, stage="R0")
    statement, expected = integrity_edits()[case]
    connection = ip06.corrupt(owned, statement, table=INTEGRITY)
    try:
        with pytest.raises(StorageError, match=expected):
            read_journal_integrity_report(
                connection,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                report_id=REPORT_ID,
            )
    finally:
        connection.close()


def test_a_report_that_outlived_the_journal_it_verified_fails_closed(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """A `verified` head of two, over a journal that now reaches zero, proves nothing."""
    verify(owned, stage="R0")
    connection = ip06.corrupt(
        owned,
        f"DELETE FROM {JOURNAL} WHERE sequence > 0",
        table=JOURNAL,
        operation="delete",
    )
    try:
        with pytest.raises(
            StorageError, match="a journal head this run no longer holds"
        ):
            read_journal_integrity_report(
                connection,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                report_id=REPORT_ID,
            )
    finally:
        connection.close()


# --- integrity: a report edited into another shape --------------------------------------


def integrity_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "reportId": REPORT_ID,
        "runId": RUN_ID,
        "rolloutStage": "R0",
        "outcome": "verified",
        "observedHead": CHAIN - 1,
        "verifiedAt": VERIFIED_AT,
    }
    document.update(overrides)
    return document


def integrity_shape_edits() -> dict[str, tuple[dict[str, Any], str]]:
    dropped = integrity_document()
    del dropped["observedHead"]
    return {
        "a member added": (
            integrity_document(quarantinedBy="core-operator"),
            "does not hold exactly the members it is stored with",
        ),
        "a member removed": (
            dropped,
            "does not hold exactly the members it is stored with",
        ),
        "a finding's members on a verified pass": (
            integrity_document(
                firstAffectedSequence=1, diagnostic="RT_JOURNAL_SEQUENCE_GAP"
            ),
            "does not hold exactly the members it is stored with",
        ),
        "a member of another type": (
            integrity_document(observedHead="2"),
            "states 'observedHead' as a value of another type",
        ),
        "a head stated as a posture": (
            integrity_document(observedHead=True),
            "states 'observedHead' as a value of another type",
        ),
    }


@pytest.mark.parametrize("case", sorted(integrity_shape_edits()))
def test_an_integrity_report_edited_into_another_shape_fails_closed(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding, case: str
) -> None:
    verify(owned, stage="R0")
    document, expected = integrity_shape_edits()[case]
    statement, parameters = restated(INTEGRITY, document)
    connection = ip06.corrupt(owned, statement, *parameters, table=INTEGRITY)
    try:
        with pytest.raises(StorageError, match=expected):
            read_journal_integrity_report(
                connection,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                report_id=REPORT_ID,
            )
    finally:
        connection.close()


def test_a_finding_report_missing_the_members_its_outcome_requires_fails_closed(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """A finding states both optional members; a document stating neither is not one."""
    holder = reopen(
        owned, drop(JOURNAL, "delete"), f"DELETE FROM {JOURNAL} WHERE sequence = 1"
    )
    verify(holder, stage="R0")
    statement, parameters = restated(
        INTEGRITY,
        integrity_document(outcome="sequence_gap", observedHead=CHAIN - 1),
    )
    connection = ip06.corrupt(holder, statement, *parameters, table=INTEGRITY)
    try:
        with pytest.raises(
            StorageError, match="does not hold exactly the members it is stored with"
        ):
            read_journal_integrity_report(
                connection,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                report_id=REPORT_ID,
            )
    finally:
        connection.close()


# --- integrity: a report is evidence about a prefix, re-checked against it --------------


def test_a_verified_report_survives_valid_later_appends(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """A pass over a prefix is not invalidated by history appended honestly after it."""
    verify(owned, stage="R0")
    ip07.apply(owned, journalled, chain_bundle(CHAIN), ip07.payload(CHAIN))
    report = read_journal_integrity_report(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID, report_id=REPORT_ID
    )
    assert report is not None
    assert report.outcome == "verified"
    assert report.observed_head == CHAIN - 1


def test_corruption_after_a_verified_head_leaves_the_older_prefix_report_readable(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """The report says what it verified; a fault beyond that is not its business."""
    verify(owned, stage="R0", report_id="integrity-ip07g-prefix")
    ip07.apply(owned, journalled, chain_bundle(CHAIN), ip07.payload(CHAIN))
    holder = reopen(
        owned,
        drop(JOURNAL, "delete"),
        f"DELETE FROM {JOURNAL} WHERE sequence = {CHAIN}",
    )
    try:
        report = read_journal_integrity_report(
            holder.connection,
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            report_id="integrity-ip07g-prefix",
        )
        assert report is not None
        assert report.outcome == "verified"
    finally:
        holder.connection.close()


def test_corruption_inside_a_verified_prefix_invalidates_the_report(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """A `verified` pass over rows since edited away is not evidence for them."""
    verify(owned, stage="R0")
    connection = ip06.corrupt(
        owned,
        f"UPDATE {JOURNAL} SET event_json = ? WHERE sequence = 1",
        canonicalize(chain_event(1)).replace("advanced", "advanceD"),
        table=JOURNAL,
    )
    try:
        with pytest.raises(
            StorageError, match="verified a prefix this run no longer shows"
        ):
            read_journal_integrity_report(
                connection,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                report_id=REPORT_ID,
            )
    finally:
        connection.close()


def test_a_finding_that_no_longer_matches_the_live_journal_fails_closed(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """A finding restated onto another sequence is not the finding that was recorded."""
    holder = reopen(
        owned, drop(JOURNAL, "delete"), f"DELETE FROM {JOURNAL} WHERE sequence = 1"
    )
    verify(holder, stage="R0")
    moved = integrity_document(
        outcome="sequence_gap",
        firstAffectedSequence=2,
        diagnostic="RT_JOURNAL_SEQUENCE_GAP",
        observedHead=CHAIN - 1,
    )
    statement, parameters = restated(INTEGRITY, moved)
    connection = ip06.corrupt(
        holder,
        f"{statement}, first_affected_sequence = 2",
        *parameters,
        table=INTEGRITY,
    )
    try:
        with pytest.raises(
            StorageError, match="no longer states what this run's journal shows"
        ):
            read_journal_integrity_report(
                connection,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                report_id=REPORT_ID,
            )
    finally:
        connection.close()


# --- quarantine: observe at R0, enforce at R1 and R2 -----------------------------------


def test_r0_records_the_finding_and_stops_nothing(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """Observe-only means observe only: a report, no disposition, and a resumable Run."""
    holder = reopen(
        owned, drop(JOURNAL, "delete"), f"DELETE FROM {JOURNAL} WHERE sequence = 1"
    )
    try:
        verification = verify(holder, stage="R0")
        assert verification.outcome == "sequence_gap"
        assert verification.quarantined is False
        assert counts(holder.connection) == (0, 1, 0, 0)
        assert quarantine_of(holder).held is False
        assert resume_of(holder).resumable is True
    finally:
        holder.connection.close()


@pytest.mark.parametrize("stage", ["R1", "R2"])
def test_an_enforcing_stage_quarantines_the_finding_it_recorded(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding, stage: str
) -> None:
    holder = reopen(
        owned, drop(JOURNAL, "delete"), f"DELETE FROM {JOURNAL} WHERE sequence = 1"
    )
    try:
        before = public_run_state(holder.connection)
        verification = verify(holder, stage=stage)
        assert verification.quarantined is True
        assert counts(holder.connection) == (0, 1, 1, 0)

        current = quarantine_of(holder)
        assert current.held is True
        assert current.diagnostic == "RT_JOURNAL_QUARANTINED"
        assert current.integrity_report_id == REPORT_ID
        assert current.disposition_sequence == 0
        assert current.deciding_actor is None and current.reason is None
        # The fault is that sequence one is gone, so there is no event at it to cite;
        # 0035 requires one of this run's own events, and the highest surviving is it.
        assert current.event_id == f"event-ip07-{CHAIN - 1}"

        eligibility = resume_of(holder)
        assert eligibility.resumable is False
        assert eligibility.diagnostic == "RT_JOURNAL_QUARANTINED"
        assert eligibility.integrity_report_id == REPORT_ID
        assert public_run_state(holder.connection) == before
    finally:
        holder.connection.close()


def test_a_quarantine_removes_folds_and_renumbers_nothing(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """What survives a finding is exactly what was there when it was found."""
    holder = reopen(
        owned, drop(JOURNAL, "delete"), f"DELETE FROM {JOURNAL} WHERE sequence = 1"
    )
    try:
        before = durable_history(holder.connection)
        verify(holder, stage="R2")
        assert durable_history(holder.connection) == before
    finally:
        holder.connection.close()


def test_an_enforcing_stage_that_finds_nothing_quarantines_nothing(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    verification = verify(owned, stage="R2")
    assert (verification.outcome, verification.quarantined) == ("verified", False)
    assert counts(owned.connection) == (0, 1, 0, 0)


def test_a_replayed_finding_appends_no_second_disposition(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    holder = reopen(
        owned, drop(JOURNAL, "delete"), f"DELETE FROM {JOURNAL} WHERE sequence = 1"
    )
    try:
        verify(holder, stage="R1")
        again = verify(holder, stage="R1")
        assert again.disposition == "replayed"
        assert again.quarantined is True
        assert counts(holder.connection) == (0, 1, 1, 0)
    finally:
        holder.connection.close()


# --- quarantine: release ---------------------------------------------------------------


@pytest.fixture
def quarantined(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> Iterator[m1.Owned]:
    """That run, with its middle journal row gone and the quarantine `R1` appended."""
    holder = reopen(
        owned, drop(JOURNAL, "delete"), f"DELETE FROM {JOURNAL} WHERE sequence = 1"
    )
    verify(holder, stage="R1")
    yield holder
    holder.connection.close()


def test_a_release_is_attributable_fenced_and_appended(quarantined: m1.Owned) -> None:
    before = public_run_state(quarantined.connection)
    released = release(quarantined)
    assert released.held is False
    assert released.deciding_actor == ACTOR
    assert released.reason == REASON
    assert released.diagnostic is None
    assert released.disposition_sequence == 1
    assert counts(quarantined.connection) == (0, 1, 2, 0)
    assert resume_of(quarantined).resumable is True
    assert public_run_state(quarantined.connection) == before
    assert quarantined.connection.execute(
        f"SELECT action FROM {QUARANTINE} ORDER BY disposition_sequence"
    ).fetchall() == [("quarantined",), ("released",)]


def test_the_same_release_again_appends_no_second_disposition(
    quarantined: m1.Owned,
) -> None:
    first = release(quarantined)
    again = release(quarantined)
    assert again == first
    assert counts(quarantined.connection)[2] == 2


def test_a_retried_release_over_stacked_holds_discharges_no_second_hold(
    quarantined: m1.Owned,
) -> None:
    """The regression for the non-idempotent release.

    Stacked holds are where a retry stops being harmless. With two outstanding, the Run
    is *still held* after the first release, so a redelivered request -- same actor, same
    reason, same instant -- read as a fresh decision and popped the second hold,
    discharging a finding whoever decided the first one never saw.

    An exact retry now replays the one result it already produced and appends nothing.
    The Run stays held, and the hold it stays held under is the one nobody has answered.
    """
    verify(quarantined, report_id=SECOND_REPORT_ID)
    assert counts(quarantined.connection) == (0, 2, 2, 0)

    still_held = release(quarantined)
    assert (still_held.held, still_held.integrity_report_id) == (True, REPORT_ID)

    for _ in range(3):
        replayed = release(quarantined)
        assert replayed == still_held

    assert counts(quarantined.connection) == (0, 2, 3, 0)
    assert quarantined.connection.execute(
        f"SELECT action FROM {QUARANTINE} ORDER BY disposition_sequence"
    ).fetchall() == [("quarantined",), ("quarantined",), ("released",)]
    assert quarantine_of(quarantined).held is True
    assert resume_of(quarantined).resumable is False

    # Answering the second finding is a second decision, and it says so.
    freed = release(quarantined, decision_id="decision-second-finding")
    assert freed.held is False
    assert (freed.deciding_actor, freed.reason) == (ACTOR, REASON)
    assert counts(quarantined.connection) == (0, 2, 4, 0)
    assert resume_of(quarantined).resumable is True


def test_a_recorded_decision_identity_may_not_stand_on_other_terms(
    quarantined: m1.Owned,
) -> None:
    """Two decisions cannot share one identity: the second is a conflict, not a retry."""
    release(quarantined, decision_id="decision-one")

    with pytest.raises(JournalGovernanceRefused) as refusal:
        release(quarantined, decision_id="decision-one", actor="core-auditor")
    assert refusal.value.diagnostic == "RT_JOURNAL_RELEASE_CONFLICT"
    assert counts(quarantined.connection)[2] == 2

    with pytest.raises(JournalGovernanceRefused) as reason_refusal:
        release(quarantined, decision_id="decision-one", reason="other_reason")
    assert reason_refusal.value.diagnostic == "RT_JOURNAL_RELEASE_CONFLICT"
    assert counts(quarantined.connection)[2] == 2


def test_one_release_discharges_the_latest_hold_and_leaves_the_rest_standing(
    quarantined: m1.Owned,
) -> None:
    """Two findings are two decisions, and a release is a decision about one of them.

    A verification pass that finds a second fault holds this Run again under a second
    integrity report. Reading only the latest disposition would let one person's release
    -- made about the fault they were shown -- discharge a hold nobody ever answered;
    citing the latest disposition would then hand the released hold back as the reason
    this Run is still held. The identity in the projection is the report and the event of
    the hold actually outstanding, at every step.
    """
    surviving = chain_event(CHAIN - 1)["eventId"]
    first_hold = quarantine_of(quarantined)
    assert (first_hold.integrity_report_id, first_hold.event_id) == (
        REPORT_ID,
        surviving,
    )

    second = verify(quarantined, report_id=SECOND_REPORT_ID)
    assert second.quarantined is True
    assert counts(quarantined.connection) == (0, 2, 2, 0)
    second_hold = quarantine_of(quarantined)
    assert (second_hold.held, second_hold.integrity_report_id) == (
        True,
        SECOND_REPORT_ID,
    )

    still_held = release(quarantined)
    assert still_held.held is True
    assert still_held.integrity_report_id == REPORT_ID
    assert still_held.event_id == surviving
    assert still_held.diagnostic == "RT_JOURNAL_QUARANTINED"
    assert still_held.deciding_actor is None
    assert still_held.reason is None
    assert still_held.disposition_sequence == 2
    assert still_held == quarantine_of(quarantined)
    assert resume_of(quarantined).resumable is False
    assert resume_of(quarantined).diagnostic == "RT_JOURNAL_QUARANTINED"

    freed = release(quarantined, actor="core-auditor")
    assert freed.held is False
    assert freed.deciding_actor == "core-auditor"
    assert freed.integrity_report_id is None
    assert freed.disposition_sequence == 3
    assert freed == quarantine_of(quarantined)
    assert resume_of(quarantined).resumable is True
    assert quarantined.connection.execute(
        f"SELECT action, event_id, deciding_actor FROM {QUARANTINE} "
        "ORDER BY disposition_sequence"
    ).fetchall() == [
        ("quarantined", surviving, None),
        ("quarantined", surviving, None),
        ("released", surviving, ACTOR),
        ("released", surviving, "core-auditor"),
    ]


def test_a_release_past_the_last_of_a_stack_of_holds_refuses(
    quarantined: m1.Owned,
) -> None:
    """As many decisions as findings, and the one after that has nothing left to answer."""
    verify(quarantined, report_id=SECOND_REPORT_ID)
    release(quarantined)
    release(quarantined, actor="core-auditor")
    assert counts(quarantined.connection)[2] == 4

    with pytest.raises(JournalGovernanceRefused) as refusal:
        release(quarantined, actor="core-reviewer")
    assert refusal.value.diagnostic == "RT_JOURNAL_NOT_QUARANTINED"
    assert counts(quarantined.connection)[2] == 4
    assert quarantine_of(quarantined).held is False


def quarantine_insert(sequence: int, action: str, event_id: str | None) -> str:
    """One disposition, spelled for a file edited past 0035's own trigger."""
    decided = (
        f"'{REPORT_ID}', 'RT_JOURNAL_QUARANTINED', NULL, NULL, NULL"
        if action == "quarantined"
        else f"NULL, NULL, '{ACTOR}', '{REASON}', 'decision-ip07g-{sequence}'"
    )
    return (
        f"INSERT INTO {QUARANTINE} (workspace_id, run_id, disposition_sequence, "
        "event_id, action, integrity_report_id, diagnostic, deciding_actor, reason, "
        f"decision_id, recorded_at_us) VALUES ('{WORKSPACE_ID}', '{RUN_ID}', {sequence}, "
        f"{'NULL' if event_id is None else repr(event_id)}, '{action}', {decided}, "
        f"{ip07.RECORDED_BASE_US + 300 + sequence})"
    )


def test_a_release_that_discharges_no_hold_fails_closed(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """0035 refuses to write one, and a file edited outside it is not believed either."""
    holder = reopen(
        owned, drop(QUARANTINE, "insert"), quarantine_insert(0, "released", None)
    )
    try:
        with pytest.raises(StorageError, match="releases a hold it does not hold"):
            quarantine_of(holder)
        with pytest.raises(StorageError, match="releases a hold it does not hold"):
            resume_of(holder)
    finally:
        holder.connection.close()


def test_a_release_that_discharges_a_hold_it_does_not_cite_fails_closed(
    quarantined: m1.Owned,
) -> None:
    """Holds come off newest first, so a release naming an older one is not believed.

    0035's trigger refuses to write this history; a file edited outside it holds a
    release that reads as answering the first finding while the second stands unanswered,
    which is a decision nobody made about a fault nobody saw.
    """
    held = chain_event(CHAIN - 1)["eventId"]
    older = chain_event(0)["eventId"]
    holder = reopen(
        quarantined,
        drop(QUARANTINE, "insert"),
        quarantine_insert(1, "quarantined", older),
        quarantine_insert(2, "released", held),
    )
    try:
        with pytest.raises(StorageError, match="does not cite the hold it discharges"):
            quarantine_of(holder)
        with pytest.raises(StorageError, match="does not cite the hold it discharges"):
            resume_of(holder)
    finally:
        holder.connection.close()


def test_a_release_with_nothing_held_refuses(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    with pytest.raises(JournalGovernanceRefused) as refusal:
        release(owned)
    assert refusal.value.diagnostic == "RT_JOURNAL_NOT_QUARANTINED"
    assert counts(owned.connection) == (0, 0, 0, 0)


def test_a_release_after_a_release_by_someone_else_refuses(
    quarantined: m1.Owned,
) -> None:
    """Idempotency is the same decision restated, not a second decision on a free Run."""
    release(quarantined)
    with pytest.raises(JournalGovernanceRefused) as refusal:
        release(quarantined, actor="core-auditor")
    assert refusal.value.diagnostic == "RT_JOURNAL_NOT_QUARANTINED"
    assert counts(quarantined.connection)[2] == 2


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"actor": ""}, "names both an actor and a reason"),
        ({"reason": ""}, "names both an actor and a reason"),
        ({"reason": "Operator Release"}, "the workspace schema refused"),
        ({"actor": "core operator"}, "the workspace schema refused"),
    ],
    ids=["no actor", "no reason", "unschematic reason", "unschematic actor"],
)
def test_a_release_without_a_schema_valid_actor_and_reason_refuses(
    quarantined: m1.Owned, overrides: dict[str, Any], expected: str
) -> None:
    with pytest.raises(StorageError, match=expected):
        release(quarantined, **overrides)
    assert counts(quarantined.connection)[2] == 1
    assert quarantine_of(quarantined).held is True


def test_stale_authority_releases_nothing(quarantined: m1.Owned) -> None:
    with pytest.raises(StaleGeneration):
        release(quarantined, generation=quarantined.generation + 1)
    assert counts(quarantined.connection)[2] == 1
    assert quarantine_of(quarantined).held is True


def test_one_runs_quarantine_neither_blocks_nor_is_released_by_another(
    quarantined: m1.Owned,
) -> None:
    """Same workspace, different Run: the projection and the release are both scoped."""
    seed_other_run(quarantined)
    assert quarantine_of(quarantined, OTHER_RUN_ID).held is False
    assert resume_of(quarantined, OTHER_RUN_ID).resumable is True

    with pytest.raises(JournalGovernanceRefused) as refusal:
        release(quarantined, run_id=OTHER_RUN_ID)
    assert refusal.value.diagnostic == "RT_JOURNAL_NOT_QUARANTINED"
    assert quarantine_of(quarantined).held is True
    assert resume_of(quarantined).diagnostic == "RT_JOURNAL_QUARANTINED"


def test_no_quarantine_answers_for_a_workspace_it_was_not_asked_about(
    quarantined: m1.Owned,
) -> None:
    assert (
        read_journal_quarantine(
            quarantined.connection, workspace_id=OTHER_WORKSPACE_ID, run_id=RUN_ID
        ).held
        is False
    )
    assert (
        evaluate_journal_resume(
            quarantined.connection, workspace_id=OTHER_WORKSPACE_ID, run_id=RUN_ID
        ).resumable
        is True
    )


# --- quarantine: corruption ------------------------------------------------------------


def quarantine_edits() -> dict[str, tuple[str, tuple[Any, ...], str, str, str]]:
    return {
        "unknown action": (
            f"UPDATE {QUARANTINE} SET action = 'archived'",
            (),
            "names an unknown action",
            QUARANTINE,
            "update",
        ),
        "held without its report": (
            f"UPDATE {QUARANTINE} SET integrity_report_id = NULL",
            (),
            "not in the form its action requires",
            QUARANTINE,
            "update",
        ),
        "held naming an actor": (
            f"UPDATE {QUARANTINE} SET deciding_actor = ?",
            (ACTOR,),
            "not in the form its action requires",
            QUARANTINE,
            "update",
        ),
        "a disposition sequence that skips": (
            f"UPDATE {QUARANTINE} SET disposition_sequence = 4",
            (),
            "is not contiguous from zero",
            QUARANTINE,
            "update",
        ),
        # The `quarantined` fixture reached its finding by removing a journal row, so
        # that guard is already gone from this file and there is none left to drop. The
        # disposition cites this run's highest surviving event, which the gap at one
        # leaves at sequence two.
        "the event it holds": (
            f"DELETE FROM {JOURNAL} WHERE sequence = {CHAIN - 1}",
            (),
            "names an event its run no longer holds",
            None,
            "delete",
        ),
        "the report it cites": (
            f"DELETE FROM {INTEGRITY}",
            (),
            "cites an integrity report that is no longer there",
            INTEGRITY,
            "delete",
        ),
        "the finding it cites": (
            (
                f"UPDATE {INTEGRITY} SET outcome = 'verified', "
                "first_affected_sequence = NULL, diagnostic = NULL"
            ),
            (),
            "disagrees with the columns it is indexed by",
            INTEGRITY,
            "update",
        ),
    }


@pytest.mark.parametrize("case", sorted(quarantine_edits()))
def test_a_tampered_quarantine_history_fails_closed(
    quarantined: m1.Owned, case: str
) -> None:
    statement, parameters, expected, table, operation = quarantine_edits()[case]
    connection = (
        r102.tamper(quarantined, statement)
        if table is None
        else ip06.corrupt(
            quarantined, statement, *parameters, table=table, operation=operation
        )
    )
    try:
        for reader in (read_journal_quarantine, evaluate_journal_resume):
            with pytest.raises(StorageError, match=expected):
                reader(connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    finally:
        connection.close()


def released_history_edits() -> dict[str, tuple[str, tuple[Any, ...], str, str, str]]:
    """Edits to the *earlier* held disposition, made after the release on top of it."""
    verified = restated(
        INTEGRITY,
        integrity_document(rolloutStage="R1", observedHead=CHAIN - 1),
    )
    return {
        "the report the earlier hold cited": (
            f"DELETE FROM {INTEGRITY}",
            (),
            "cites an integrity report that is no longer there",
            INTEGRITY,
            "delete",
        ),
        "the finding the earlier hold cited": (
            (
                f"{verified[0]}, outcome = 'verified', "
                "first_affected_sequence = NULL, diagnostic = NULL"
            ),
            verified[1],
            "verified a prefix this run no longer shows",
            INTEGRITY,
            "update",
        ),
        "the form of the earlier hold": (
            f"UPDATE {QUARANTINE} SET deciding_actor = ? WHERE disposition_sequence = 0",
            (ACTOR,),
            "not in the form its action requires",
            QUARANTINE,
            "update",
        ),
        "the instant of the earlier hold": (
            f"UPDATE {QUARANTINE} SET recorded_at_us = 0 WHERE disposition_sequence = 0",
            (),
            "not a number this schema stores",
            QUARANTINE,
            "update",
        ),
    }


@pytest.mark.parametrize("case", sorted(released_history_edits()))
def test_a_release_does_not_hide_tampering_of_the_hold_beneath_it(
    quarantined: m1.Owned, case: str
) -> None:
    """The latest disposition decides; it does not excuse the history it sits on.

    A reader that verified only the newest row would read a released Run as free while
    the quarantine underneath it, or the report that quarantine stands on, had been
    edited into something nobody recorded.
    """
    release(quarantined)
    statement, parameters, expected, table, operation = released_history_edits()[case]
    connection = ip06.corrupt(
        quarantined, statement, *parameters, table=table, operation=operation
    )
    try:
        for reader in (read_journal_quarantine, evaluate_journal_resume):
            with pytest.raises(StorageError, match=expected):
                reader(connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    finally:
        connection.close()


# --- retention: recorded, never enacted -------------------------------------------------


def test_a_boundary_that_removes_nothing_may_leave_a_run_resumable(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    audit_record(owned)
    before = durable_history(owned.connection)
    posture = boundary(owned)
    assert posture.resumable is True
    assert posture.blocking_boundary_id is None
    assert len(posture.boundaries) == 1
    assert posture.boundaries[0].first_removed_sequence is None
    assert posture.boundaries[0].audit_ref == RETENTION_AUDIT
    assert resume_of(owned).resumable is True
    assert durable_history(owned.connection) == before


def test_a_boundary_that_names_a_range_records_it_and_removes_nothing(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    audit_record(owned)
    before = durable_history(owned.connection)
    posture = boundary(owned, first=0, last=1, resumable_after=False)
    assert posture.resumable is False
    assert posture.blocking_boundary_id == BOUNDARY_ID
    assert (
        posture.boundaries[0].first_removed_sequence,
        posture.boundaries[0].last_removed_sequence,
    ) == (0, 1)
    assert durable_history(owned.connection) == before
    assert (
        len(
            read_runtime_journal_events(
                owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            )
        )
        == CHAIN
    )


def test_a_removal_that_claims_to_leave_a_run_resumable_refuses(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    audit_record(owned)
    before = public_run_state(owned.connection)
    with pytest.raises(JournalGovernanceRefused) as refusal:
        boundary(owned, first=0, last=1, resumable_after=True)
    assert refusal.value.diagnostic == "RT_RETENTION_RANGE_STILL_RESUMABLE"
    assert counts(owned.connection) == (0, 0, 0, 0)
    assert public_run_state(owned.connection) == before


def test_a_run_rendered_non_resumable_refuses_to_resume_on_its_own_diagnostic(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """Separate from quarantine: nothing is held, and the Run still may not resume."""
    audit_record(owned)
    boundary(owned, first=0, last=0, resumable_after=False)
    eligibility = resume_of(owned)
    assert eligibility.resumable is False
    assert eligibility.diagnostic == "RT_JOURNAL_RETENTION_BOUNDARY"
    assert eligibility.boundary_id == BOUNDARY_ID
    assert eligibility.integrity_report_id is None
    assert quarantine_of(owned).held is False


def test_a_later_boundary_cannot_restore_a_resumability_already_ended(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    audit_record(owned)
    boundary(owned, first=0, last=0, resumable_after=False)
    posture = boundary(
        owned,
        boundary_id="boundary-ip07g-two",
        resumable_after=True,
        recorded_at=ip06.instant(ip07.RECORDED_BASE_US + 500),
    )
    assert posture.resumable is False
    assert posture.blocking_boundary_id == BOUNDARY_ID
    assert len(posture.boundaries) == 2
    assert resume_of(owned).diagnostic == "RT_JOURNAL_RETENTION_BOUNDARY"


def test_a_boundary_naming_an_audit_record_this_workspace_does_not_hold_refuses(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    """0035's own trigger says so; this module has no Audit writer to satisfy it."""
    with pytest.raises(
        StorageError, match="audit reference must belong to its workspace"
    ):
        boundary(owned)
    assert counts(owned.connection) == (0, 0, 0, 0)
    assert (
        read_journal_retention_posture(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        ).boundaries
        == ()
    )


def test_stale_authority_records_no_boundary(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    audit_record(owned)
    with pytest.raises(StaleGeneration):
        boundary(owned, generation=owned.generation + 1)
    assert counts(owned.connection) == (0, 0, 0, 0)


def test_no_retention_posture_answers_for_a_workspace_or_run_it_was_not_asked_about(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> None:
    audit_record(owned)
    boundary(owned, first=0, last=0, resumable_after=False)
    seed_other_run(owned)
    for workspace, run in ((OTHER_WORKSPACE_ID, RUN_ID), (WORKSPACE_ID, OTHER_RUN_ID)):
        posture = read_journal_retention_posture(
            owned.connection, workspace_id=workspace, run_id=run
        )
        assert posture.boundaries == ()
        assert posture.resumable is True
    assert (
        evaluate_journal_resume(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=OTHER_RUN_ID
        ).resumable
        is True
    )


def retention_edits() -> dict[str, tuple[str, str, str, str]]:
    return {
        "a range that claims resumability": (
            f"UPDATE {RETENTION} SET resumable_after = 1",
            "does not state a removable range",
            RETENTION,
            "update",
        ),
        "a reversed range": (
            f"UPDATE {RETENTION} SET first_removed_sequence = 2",
            "does not state a removable range",
            RETENTION,
            "update",
        ),
        "half a range": (
            f"UPDATE {RETENTION} SET last_removed_sequence = NULL",
            "does not state a removable range",
            RETENTION,
            "update",
        ),
        "an unknown resumability": (
            f"UPDATE {RETENTION} SET resumable_after = 2",
            "does not state whether its run may resume",
            RETENTION,
            "update",
        ),
        "the audit record it names": (
            f"DELETE FROM {AUDIT}",
            "names an audit record its workspace does not hold",
            AUDIT,
            "delete",
        ),
    }


@pytest.mark.parametrize("case", sorted(retention_edits()))
def test_a_tampered_retention_boundary_fails_closed(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding, case: str
) -> None:
    audit_record(owned)
    boundary(owned, first=0, last=1, resumable_after=False)
    statement, expected, table, operation = retention_edits()[case]
    connection = ip06.corrupt(owned, statement, table=table, operation=operation)
    try:
        for reader in (read_journal_retention_posture, evaluate_journal_resume):
            with pytest.raises(StorageError, match=expected):
                reader(connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "statement,expected",
    [
        (
            f"UPDATE {RETENTION} SET boundary_id = 'boundary ip07g one'",
            "not spelled as this schema stores it",
        ),
        (
            f"UPDATE {RETENTION} SET policy_ref = ''",
            "not spelled as this schema stores it",
        ),
        (
            f"UPDATE {RETENTION} SET audit_ref = 'audit/ip07g'",
            "not spelled as this schema stores it",
        ),
        (
            f"UPDATE {RETENTION} SET first_removed_sequence = -1",
            "not a number this schema stores",
        ),
        (
            f"UPDATE {RETENTION} SET recorded_at_us = 0",
            "not a number this schema stores",
        ),
    ],
    ids=[
        "an unspellable identifier",
        "an empty reference",
        "an unspellable audit reference",
        "a negative sequence",
        "an unstorable instant",
    ],
)
def test_a_retention_scalar_the_schema_would_have_refused_fails_closed(
    owned: m1.Owned,
    journalled: StoredRuntimeDefinitionBinding,
    statement: str,
    expected: str,
) -> None:
    """The CHECK is the copy that runs on write; the read cannot assume it ran."""
    audit_record(owned)
    boundary(owned, first=0, last=1, resumable_after=False)
    connection = ip06.corrupt(owned, statement, table=RETENTION)
    try:
        for reader in (read_journal_retention_posture, evaluate_journal_resume):
            with pytest.raises(StorageError, match=expected):
                reader(connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    finally:
        connection.close()


# --- durability -------------------------------------------------------------------------


def test_a_verified_backup_restores_every_judgement_that_still_reads(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding, tmp_path: Path
) -> None:
    audit_record(owned)
    parity(owned)
    verify(owned, stage="R0")
    boundary(owned, first=0, last=1, resumable_after=False)
    recorded = (
        read_transition_parity_report(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            bundle_id="bundle-ip07-0",
        ),
        read_journal_integrity_report(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            report_id=REPORT_ID,
        ),
        read_journal_retention_posture(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        ),
        resume_of(owned),
    )
    owned.connection.close()

    installation = InstallationLayout(root=tmp_path / "installation-state")
    backup = create_verified_backup(
        owned.path,
        installation,
        workspace_id=WORKSPACE_ID,
        attempt_id=new_attempt_id(),
    )
    assert backup.verified

    restored = tmp_path / "restored.sqlite"
    restore_backup(backup.path, restored)
    connection = open_database(restored, OpenMode.EPHEMERAL)
    try:
        assert (
            read_transition_parity_report(
                connection,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                bundle_id="bundle-ip07-0",
            ),
            read_journal_integrity_report(
                connection,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                report_id=REPORT_ID,
            ),
            read_journal_retention_posture(
                connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            ),
            evaluate_journal_resume(
                connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
            ),
        ) == recorded
    finally:
        connection.close()


# --- T-0691: a held quarantine refuses the write, not only the read ---------------------


@pytest.fixture
def held(
    owned: m1.Owned, journalled: StoredRuntimeDefinitionBinding
) -> Iterator[tuple[m1.Owned, StoredRuntimeDefinitionBinding]]:
    """A quarantined run whose chain head still links, so a write can actually be tried.

    The fault is at sequence one and the head is sequence two, so the next transition
    would link cleanly and nothing but the quarantine stands between a caller and the
    chain. That is what makes every refusal below a fact about the hold rather than an
    accident of the fault that caused it -- and what lets the released case go on to
    write for real instead of merely failing differently.
    """
    holder = reopen(
        owned,
        "PRAGMA ignore_check_constraints = ON",
        drop(JOURNAL, "update"),
        corrupted_journal_edits()["event digest"],
    )
    verify(holder, stage="R1")
    assert quarantine_of(holder).held is True
    yield holder, journalled
    holder.connection.close()


def next_transition(
    holder: m1.Owned, binding: StoredRuntimeDefinitionBinding
) -> Any:
    """The transition this run would take next if nothing were holding it."""
    return ip07.apply(
        holder, binding, chain_bundle(CHAIN), ip07.payload(CHAIN), stage="R2"
    )


def durable_state(holder: m1.Owned) -> tuple[Any, ...]:
    """Everything a refused write must have left exactly where it was."""
    return (
        ip07.counts(holder.connection),
        counts(holder.connection),
        durable_history(holder.connection),
        public_run_state(holder.connection),
    )


def test_a_held_quarantine_refuses_the_transition_that_would_extend_its_chain(
    held: tuple[m1.Owned, StoredRuntimeDefinitionBinding],
) -> None:
    """The finding this pins: the hold used to stop only the reader.

    `evaluate_journal_resume` reported `RT_JOURNAL_QUARANTINED` and a caller that never
    asked applied the next bundle anyway, extending a chain nobody had been able to
    verify. The refusal now happens on the write, carries the same diagnostic, and
    leaves the bundle table, the journal, the governance records and the public Run
    exactly as they were.
    """
    holder, binding = held
    before = durable_state(holder)

    with pytest.raises(TransitionBundleRefused) as refusal:
        next_transition(holder, binding)

    assert refusal.value.diagnostic == "RT_JOURNAL_QUARANTINED"
    assert durable_state(holder) == before
    assert resume_of(holder).diagnostic == "RT_JOURNAL_QUARANTINED"


def test_a_replay_of_a_recorded_bundle_is_refused_while_the_hold_stands(
    held: tuple[m1.Owned, StoredRuntimeDefinitionBinding],
) -> None:
    """Confirming a replay means reading the chain the hold says nobody can verify."""
    holder, binding = held
    before = durable_state(holder)

    with pytest.raises(TransitionBundleRefused) as refusal:
        ip07.apply(holder, binding, chain_bundle(0), ip07.payload(0), stage="R2")

    assert refusal.value.diagnostic == "RT_JOURNAL_QUARANTINED"
    assert durable_state(holder) == before


def test_writes_resume_only_once_the_whole_quarantine_stack_is_released(
    held: tuple[m1.Owned, StoredRuntimeDefinitionBinding],
) -> None:
    """One decision discharges one hold, so a partial release is still a held run."""
    holder, binding = held
    assert verify(holder, report_id=SECOND_REPORT_ID).quarantined is True
    before = durable_state(holder)

    release(holder)
    assert quarantine_of(holder).held is True
    with pytest.raises(TransitionBundleRefused) as still_held:
        next_transition(holder, binding)
    assert still_held.value.diagnostic == "RT_JOURNAL_QUARANTINED"
    assert ip07.counts(holder.connection) == before[0]

    release(holder, actor="core-auditor", recorded_at=BOUNDARY_AT)
    assert quarantine_of(holder).held is False

    outcome = next_transition(holder, binding)

    assert outcome.disposition == "applied"
    assert ip07.counts(holder.connection) == (CHAIN + 1, CHAIN + 1)
    assert resume_of(holder).resumable is True


def test_a_superseded_writer_never_reaches_a_quarantined_run_at_all(
    held: tuple[m1.Owned, StoredRuntimeDefinitionBinding],
) -> None:
    """A real takeover, and the transition the superseded generation would have made.

    The generation is the one this workspace actually left behind when the next service
    took it, not an invented number. Two facts stand between that writer and the chain
    and the test proves both: the fence refuses the superseded generation, and the hold
    is still standing for the service that took over.
    """
    holder, binding = held
    superseded = holder.generation
    successor = reopen(holder)
    try:
        assert successor.generation > superseded
        before = durable_state(successor)

        with pytest.raises(StaleGeneration):
            ip07.apply(
                successor,
                binding,
                chain_bundle(CHAIN),
                ip07.payload(CHAIN),
                generation=superseded,
            )
        assert durable_state(successor) == before

        with pytest.raises(TransitionBundleRefused) as refusal:
            next_transition(successor, binding)
        assert refusal.value.diagnostic == "RT_JOURNAL_QUARANTINED"
        assert durable_state(successor) == before
    finally:
        successor.connection.close()


def test_one_runs_quarantine_never_blocks_another_runs_transition(
    held: tuple[m1.Owned, StoredRuntimeDefinitionBinding],
) -> None:
    """The refusal is run-scoped, like every other fact about a quarantine.

    `seed_other_run` binds a second Run in this same workspace and applies its first
    transition for real, so a guard that read the workspace rather than the Run would
    abort here rather than pass.
    """
    holder, _binding = held

    seed_other_run(holder)

    assert quarantine_of(holder, OTHER_RUN_ID).held is False
    assert resume_of(holder, OTHER_RUN_ID).resumable is True
    assert quarantine_of(holder).held is True
    assert resume_of(holder).diagnostic == "RT_JOURNAL_QUARANTINED"
