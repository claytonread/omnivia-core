"""RT-103 acceptance for the Artifact, EvidenceItem and CleanupReceipt repositories.

The migration tests hold migration 0019 to what SQL can enforce. These hold
`storage/agent_runtime.py`'s three new writers and their readers to what SQL cannot.

*They enter their own fenced transaction.* Each of the three standalone writers is
called on a bare owned connection -- no surrounding transaction -- and either commits
under current authority or leaves the database exactly as it found it. The same three
writes also compose under one `runtime_writer` fence, because RT-104 has to be able to
record an artifact, the evidence for it and the run's cleanup in one transaction, and
three functions that each open their own could not be assembled into that.

*They return the accepted contract records.* `Artifact`, `EvidenceItem` and
`CleanupReceipt` come back as the generated types, held to `semantics_runtime`'s own
validators rather than to assertions written here, and an `ExternalReference`
round-trips field for field rather than as a document that happens to re-parse.

*A missing blob degrades, it does not corrupt.* `read_blob_availability` reports an
absent `omnivia_blob_objects` row as unavailable, and the artifact's own metadata and
its parent run stay readable either way. Nothing in these reads requires a catalogue
row to exist, and nothing fabricates bytes when one does not.

*They read a workspace, never a record's claim about one.* Every read takes the
caller's workspace and filters on it, and every collection has one deterministic order.

No public wire surface is touched: the contract version is asserted unchanged, and the
closed sets storage enforces are asserted to be the contract's own.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt102_agent_runtime_repository as r102
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.storage.agent_runtime import (
    BlobAvailability,
    append_artifact,
    append_cleanup_receipt,
    append_evidence_item,
    read_artifact,
    read_blob_availability,
    read_cleanup_receipt,
    read_evidence_item,
    read_run,
    read_run_artifacts,
    read_run_cleanup_receipts,
    read_run_evidence,
    runtime_writer,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

from omnivia_core.contracts.v1 import (
    CLEANUP_OUTCOMES,
    CONTRACT_VERSION,
    RUNTIME_AUTHORITATIVE_SOURCE_KIND,
    RUNTIME_SOURCE_KINDS,
    Artifact,
    CleanupReceipt,
    EvidenceItem,
    ExternalReference,
    validate_artifact,
    validate_cleanup_receipt,
    validate_evidence_item,
)

WORKSPACE_ID = m18.WORKSPACE_ID
OTHER_WORKSPACE_ID = m18.OTHER_WORKSPACE_ID
BASE_US = m18.BASE_US
JOB_ID = m18.JOB_ID
RUN_ID = m18.RUN_ID
STEP_ID = m18.STEP_ID

#: One millisecond, for the same reason RT-102's repository tests carry one: the stored
#: microsecond is rendered at millisecond precision, so two instants that must read back
#: differently have to be at least this far apart.
MS = r102.MS

ARTIFACT_ID = "art-0001"
EVIDENCE_ID = "evd-0001"
CLEANUP_ID = "cln-0001"

ARTIFACT_DIGEST = "sha256:" + "1" * 64
EVIDENCE_DIGEST = "sha256:" + "2" * 64

SOURCE = ExternalReference(
    source_kind=RUNTIME_AUTHORITATIVE_SOURCE_KIND,
    source_id="runtime:step-0001:tool-call-3",
    workspace_id=WORKSPACE_ID,
)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def add_artifact(holder: m1.Owned, **overrides: object) -> None:
    arguments: dict[str, object] = {
        "artifact_id": ARTIFACT_ID,
        "run_id": RUN_ID,
        "artifact_kind": "final_output",
        "media_type": "application/json",
        "content_checksum": ARTIFACT_DIGEST,
        "content_length_bytes": 128,
        "produced_at_us": BASE_US + MS,
    }
    arguments.update(overrides)
    append_artifact(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        **arguments,
    )


def add_evidence(holder: m1.Owned, **overrides: object) -> None:
    arguments: dict[str, object] = {
        "evidence_item_id": EVIDENCE_ID,
        "run_id": RUN_ID,
        "evidence_kind": "tool_call",
        "source": SOURCE,
        "content_checksum": EVIDENCE_DIGEST,
        "captured_at_us": BASE_US + MS,
        "authoritative": True,
        "retained": True,
    }
    arguments.update(overrides)
    append_evidence_item(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        **arguments,
    )


def add_cleanup_receipt(holder: m1.Owned, **overrides: object) -> None:
    arguments: dict[str, object] = {
        "cleanup_receipt_id": CLEANUP_ID,
        "run_id": RUN_ID,
        "resource_kind": "sandbox",
        "outcome": "released",
        "reason": "run_completed",
        "performed_at_us": BASE_US + 2 * MS,
        "audit_reference": m18.audit_ref_for(JOB_ID),
    }
    arguments.update(overrides)
    append_cleanup_receipt(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        **arguments,
    )


def catalogue_blob(holder: m1.Owned, *, digest: str, length: int) -> None:
    """One row in the existing verified-identity catalogue migration 0008 owns."""
    with m18.guarded(holder):
        holder.connection.execute(
            "INSERT INTO omnivia_blob_objects (workspace_id, content_digest, "
            "content_length_bytes, created_at_us, verified_at_us) VALUES (?, ?, ?, ?, ?)",
            (WORKSPACE_ID, digest, length, BASE_US, BASE_US),
        )


# --- round trip -------------------------------------------------------------------


def test_the_three_records_round_trip_as_contract_records(owned: m1.Owned) -> None:
    """Held to `semantics_runtime`'s validators, not to this file's idea of the shapes."""
    r102.admit(owned)
    r102.add_step(owned)
    add_artifact(owned, run_step_id=STEP_ID)
    add_evidence(
        owned,
        run_step_id=STEP_ID,
        artifact_id=ARTIFACT_ID,
        content_checksum=ARTIFACT_DIGEST,
    )
    add_cleanup_receipt(owned)

    artifact = read_artifact(
        owned.connection, workspace_id=WORKSPACE_ID, artifact_id=ARTIFACT_ID
    )
    assert isinstance(artifact, Artifact)
    validate_artifact(artifact, run_id=RUN_ID, workspace_id=WORKSPACE_ID)
    assert artifact.run_step_id == STEP_ID
    assert artifact.artifact_kind == "final_output"
    assert artifact.media_type == "application/json"
    assert artifact.content_checksum == ARTIFACT_DIGEST
    assert artifact.content_length_bytes == 128

    item = read_evidence_item(
        owned.connection, workspace_id=WORKSPACE_ID, evidence_item_id=EVIDENCE_ID
    )
    assert isinstance(item, EvidenceItem)
    validate_evidence_item(item, run_id=RUN_ID, workspace_id=WORKSPACE_ID)
    assert item.source == SOURCE
    assert item.source.source_kind == SOURCE.source_kind
    assert item.source.source_id == SOURCE.source_id
    assert item.source.workspace_id == SOURCE.workspace_id
    assert item.artifact_id == ARTIFACT_ID
    assert item.content_checksum == artifact.content_checksum
    assert item.authoritative is True and item.retained is True

    receipt = read_cleanup_receipt(
        owned.connection, workspace_id=WORKSPACE_ID, cleanup_receipt_id=CLEANUP_ID
    )
    assert isinstance(receipt, CleanupReceipt)
    validate_cleanup_receipt(receipt, run_id=RUN_ID, workspace_id=WORKSPACE_ID)
    assert receipt.outcome == "released"
    assert receipt.audit_reference == m18.audit_ref_for(JOB_ID)


def test_a_run_snapshot_carries_the_three_new_collections(owned: m1.Owned) -> None:
    """`read_run` reports what 0019 stores, and still reports nothing it does not."""
    r102.admit(owned)
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.artifacts == ()
    assert snapshot.evidence == ()
    assert snapshot.cleanup_receipts == ()

    add_artifact(owned)
    add_evidence(owned)
    add_cleanup_receipt(owned)
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert [record.artifact_id for record in snapshot.artifacts] == [ARTIFACT_ID]
    assert [record.evidence_item_id for record in snapshot.evidence] == [EVIDENCE_ID]
    assert [
        record.cleanup_receipt_id for record in snapshot.cleanup_receipts
    ] == [CLEANUP_ID]
    for record in snapshot.artifacts:
        validate_artifact(record, run_id=RUN_ID, workspace_id=WORKSPACE_ID)
    for item in snapshot.evidence:
        validate_evidence_item(item, run_id=RUN_ID, workspace_id=WORKSPACE_ID)
    for receipt in snapshot.cleanup_receipts:
        validate_cleanup_receipt(receipt, run_id=RUN_ID, workspace_id=WORKSPACE_ID)


def test_every_accepted_source_kind_and_cleanup_outcome_is_storable(
    owned: m1.Owned,
) -> None:
    """The closed sets in SQL are the contract's own, not a narrower copy of them.

    The migration tests prove storage is no *wider* than the accepted vocabulary. This
    is the other half: a kind or outcome the contract accepts and storage silently
    refuses would be a record that cannot be written down at all.
    """
    r102.admit(owned)
    for index, source_kind in enumerate(RUNTIME_SOURCE_KINDS):
        add_evidence(
            owned,
            evidence_item_id=f"evd-kind-{index}",
            source=ExternalReference(
                source_kind=source_kind,
                source_id=f"src-{index}",
                workspace_id=WORKSPACE_ID,
            ),
            authoritative=source_kind == RUNTIME_AUTHORITATIVE_SOURCE_KIND,
        )
    stored = read_run_evidence(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert {item.source.source_kind for item in stored} == set(RUNTIME_SOURCE_KINDS)

    for index, outcome in enumerate(CLEANUP_OUTCOMES):
        add_cleanup_receipt(
            owned, cleanup_receipt_id=f"cln-outcome-{index}", outcome=outcome
        )
    receipts = read_run_cleanup_receipts(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert {receipt.outcome for receipt in receipts} == set(CLEANUP_OUTCOMES)


# --- blob availability ---------------------------------------------------------------


def test_a_missing_blob_reads_as_unavailable_and_hides_nothing(owned: m1.Owned) -> None:
    """The acceptance criterion: an absent blob degrades, it does not erase the record."""
    r102.admit(owned)
    add_artifact(owned)
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_blob_objects WHERE content_digest = ?",
            (ARTIFACT_DIGEST,),
        ).fetchone()[0]
        == 0
    )
    availability = read_blob_availability(
        owned.connection, workspace_id=WORKSPACE_ID, content_checksum=ARTIFACT_DIGEST
    )
    assert availability == BlobAvailability(available=False)
    assert availability.content_length_bytes is None

    artifact = read_artifact(
        owned.connection, workspace_id=WORKSPACE_ID, artifact_id=ARTIFACT_ID
    )
    assert artifact is not None
    assert artifact.content_checksum == ARTIFACT_DIGEST
    assert artifact.content_length_bytes == 128
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.artifacts == (artifact,)


def test_a_catalogued_blob_reads_as_available_with_its_verified_length(
    owned: m1.Owned,
) -> None:
    r102.admit(owned)
    add_artifact(owned)
    catalogue_blob(owned, digest=ARTIFACT_DIGEST, length=128)
    assert read_blob_availability(
        owned.connection, workspace_id=WORKSPACE_ID, content_checksum=ARTIFACT_DIGEST
    ) == BlobAvailability(available=True, content_length_bytes=128)


def test_blob_availability_answers_only_for_the_workspace_it_was_asked_about(
    owned: m1.Owned,
) -> None:
    r102.admit(owned)
    add_artifact(owned)
    catalogue_blob(owned, digest=ARTIFACT_DIGEST, length=128)
    assert (
        read_blob_availability(
            owned.connection,
            workspace_id=OTHER_WORKSPACE_ID,
            content_checksum=ARTIFACT_DIGEST,
        ).available
        is False
    )


# --- ordering, absence and foreign workspaces -------------------------------------


def test_each_collection_reads_back_in_one_deterministic_order(owned: m1.Owned) -> None:
    """Time first, identifier second -- so a tie still has exactly one order."""
    r102.admit(owned)
    for index, (identifier, produced) in enumerate(
        (("art-c", 2), ("art-a", 1), ("art-b", 1))
    ):
        add_artifact(
            owned,
            artifact_id=identifier,
            content_checksum=f"sha256:{index:064x}",
            produced_at_us=BASE_US + produced * MS,
        )
    for identifier, captured in (("evd-c", 2), ("evd-a", 1), ("evd-b", 1)):
        add_evidence(
            owned, evidence_item_id=identifier, captured_at_us=BASE_US + captured * MS
        )
    for identifier, performed in (("cln-c", 2), ("cln-a", 1), ("cln-b", 1)):
        add_cleanup_receipt(
            owned,
            cleanup_receipt_id=identifier,
            performed_at_us=BASE_US + performed * MS,
        )

    artifacts = read_run_artifacts(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    evidence = read_run_evidence(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    receipts = read_run_cleanup_receipts(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert [record.artifact_id for record in artifacts] == ["art-a", "art-b", "art-c"]
    assert [item.evidence_item_id for item in evidence] == ["evd-a", "evd-b", "evd-c"]
    assert [receipt.cleanup_receipt_id for receipt in receipts] == [
        "cln-a",
        "cln-b",
        "cln-c",
    ]
    assert artifacts == read_run_artifacts(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.artifacts == artifacts
    assert snapshot.evidence == evidence
    assert snapshot.cleanup_receipts == receipts


def test_a_record_that_does_not_exist_reads_as_absent_rather_than_empty(
    owned: m1.Owned,
) -> None:
    r102.admit(owned)
    assert (
        read_artifact(
            owned.connection, workspace_id=WORKSPACE_ID, artifact_id="art-absent"
        )
        is None
    )
    assert (
        read_evidence_item(
            owned.connection, workspace_id=WORKSPACE_ID, evidence_item_id="evd-absent"
        )
        is None
    )
    assert (
        read_cleanup_receipt(
            owned.connection, workspace_id=WORKSPACE_ID, cleanup_receipt_id="cln-absent"
        )
        is None
    )
    for reader in (read_run_artifacts, read_run_evidence, read_run_cleanup_receipts):
        assert (
            reader(owned.connection, workspace_id=WORKSPACE_ID, run_id="run-absent")
            == ()
        )


def test_no_reader_answers_for_a_workspace_it_was_not_asked_about(
    owned: m1.Owned,
) -> None:
    """The workspace arrives as an argument, never out of the record being read."""
    r102.admit(owned)
    add_artifact(owned)
    add_evidence(owned)
    add_cleanup_receipt(owned)
    assert (
        read_artifact(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, artifact_id=ARTIFACT_ID
        )
        is None
    )
    assert (
        read_evidence_item(
            owned.connection,
            workspace_id=OTHER_WORKSPACE_ID,
            evidence_item_id=EVIDENCE_ID,
        )
        is None
    )
    assert (
        read_cleanup_receipt(
            owned.connection,
            workspace_id=OTHER_WORKSPACE_ID,
            cleanup_receipt_id=CLEANUP_ID,
        )
        is None
    )
    for reader in (read_run_artifacts, read_run_evidence, read_run_cleanup_receipts):
        assert (
            reader(owned.connection, workspace_id=OTHER_WORKSPACE_ID, run_id=RUN_ID)
            == ()
        )
    assert (
        read_run(owned.connection, workspace_id=OTHER_WORKSPACE_ID, run_id=RUN_ID)
        is None
    )


# --- fencing and transaction composition ---------------------------------------------


METADATA_TABLES = (
    "omnivia_runtime_artifacts",
    "omnivia_runtime_evidence",
    "omnivia_runtime_cleanup_receipts",
)


def metadata_row_counts(holder: m1.Owned) -> dict[str, int]:
    return {
        table: int(
            holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
        for table in METADATA_TABLES
    }


STANDALONE_WRITERS = {
    "append_artifact": add_artifact,
    "append_evidence_item": add_evidence,
    "append_cleanup_receipt": add_cleanup_receipt,
}


@pytest.mark.parametrize("name", sorted(STANDALONE_WRITERS))
def test_every_standalone_writer_is_refused_by_a_stale_fence(
    owned: m1.Owned, name: str
) -> None:
    """A writer that forgot its fence is a writer nothing checks."""
    r102.admit(owned)
    write = STANDALONE_WRITERS[name]
    before = metadata_row_counts(owned)
    stale = replace(owned, generation=owned.generation + 1)
    with pytest.raises(StaleGeneration):
        write(stale)
    assert metadata_row_counts(owned) == before
    # The same call under current authority commits, so the refusal was the fence
    # rather than the row.
    write(owned)
    assert metadata_row_counts(owned) != before


def test_the_three_writes_compose_under_one_fence(owned: m1.Owned) -> None:
    """The seam RT-104 needs: an artifact, its evidence and the cleanup, one commit."""
    r102.admit(owned)
    with runtime_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        writer.append_artifact(
            artifact_id=ARTIFACT_ID,
            run_id=RUN_ID,
            artifact_kind="final_output",
            media_type="application/json",
            content_checksum=ARTIFACT_DIGEST,
            content_length_bytes=128,
            produced_at_us=BASE_US + MS,
        )
        writer.append_evidence_item(
            evidence_item_id=EVIDENCE_ID,
            run_id=RUN_ID,
            evidence_kind="tool_call",
            source=SOURCE,
            content_checksum=ARTIFACT_DIGEST,
            artifact_id=ARTIFACT_ID,
            captured_at_us=BASE_US + MS,
            authoritative=True,
            retained=True,
        )
        writer.append_cleanup_receipt(
            cleanup_receipt_id=CLEANUP_ID,
            run_id=RUN_ID,
            resource_kind="sandbox",
            outcome="released",
            reason="run_completed",
            performed_at_us=BASE_US + 2 * MS,
            audit_reference=m18.audit_ref_for(JOB_ID),
        )
    assert metadata_row_counts(owned) == dict.fromkeys(METADATA_TABLES, 1)
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.evidence[0].artifact_id == snapshot.artifacts[0].artifact_id


def test_a_composition_that_fails_later_rolls_back_what_succeeded_earlier(
    owned: m1.Owned,
) -> None:
    """One fence, one outcome: the artifact does not survive the evidence's refusal."""
    r102.admit(owned)
    before = metadata_row_counts(owned)
    with (
        pytest.raises(sqlite3.IntegrityError, match="agree with its checksum"),
        runtime_writer(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as writer,
    ):
        writer.append_artifact(
            artifact_id=ARTIFACT_ID,
            run_id=RUN_ID,
            artifact_kind="final_output",
            media_type="application/json",
            content_checksum=ARTIFACT_DIGEST,
            content_length_bytes=128,
            produced_at_us=BASE_US + MS,
        )
        writer.append_evidence_item(
            evidence_item_id=EVIDENCE_ID,
            run_id=RUN_ID,
            evidence_kind="tool_call",
            source=SOURCE,
            content_checksum=EVIDENCE_DIGEST,
            artifact_id=ARTIFACT_ID,
            captured_at_us=BASE_US + MS,
            authoritative=True,
            retained=True,
        )
    assert metadata_row_counts(owned) == before
    assert (
        read_artifact(
            owned.connection, workspace_id=WORKSPACE_ID, artifact_id=ARTIFACT_ID
        )
        is None
    )


# --- no wire drift ---------------------------------------------------------------------


def test_this_lane_moved_no_public_contract() -> None:
    """RT-103 is persistence. The frozen application surface is untouched."""
    assert CONTRACT_VERSION == "1.3"
