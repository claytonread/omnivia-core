"""Acceptance coverage for Phase 2 retention, legal hold, and restore verification."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "phase3" / "runtime"))

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
from omnivia_core_runtime.storage.semantic_evidence import (
    read_evidence_item,
    verify_evidence_observation_digests,
)
from omnivia_core_runtime.storage.semantic_retention import (
    ALL_DELETION_STORAGE_CLASSES,
    DeletionPlanState,
    EvidenceLegalHold,
    EvidenceLegalHoldRelease,
    RetentionPolicy,
    active_legal_holds,
    build_deletion_plan,
    build_deletion_receipt,
    semantic_retention_writer,
    verify_retention_digests,
)
from test_application_audit_idempotency_migration import (  # type: ignore[import-not-found]
    bootstrap_and_migrate,
    take_ownership,
)
from test_phase2_governance_repository import (  # type: ignore[import-not-found]
    WORKSPACE_ID,
    instant,
    register_evidence,
)


@pytest.fixture
def owned(tmp_path: Path):
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = take_ownership(path, workspace_id=WORKSPACE_ID)
    register_evidence(holder)
    yield holder
    try:
        holder.connection.close()
    except sqlite3.ProgrammingError:
        pass


def writer(holder, *, generation: int | None = None):
    return semantic_retention_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation if generation is None else generation,
    )


def policy() -> RetentionPolicy:
    return RetentionPolicy(WORKSPACE_ID, "retention-v1", 2, instant(1))


def test_retention_plan_obeys_due_date_override_and_legal_hold(owned) -> None:
    evidence = read_evidence_item(owned.connection, WORKSPACE_ID, "ev-1")
    assert evidence is not None
    with writer(owned) as write:
        write.append_policy(policy())

    early = build_deletion_plan(
        plan_id="plan-early",
        evidence=evidence,
        policy=policy(),
        requested_at=instant(60),
        reason_code="scheduled",
        active_legal_hold=False,
    )
    assert early.state is DeletionPlanState.BLOCKED_NOT_DUE
    with pytest.raises(StorageError, match="only a ready"):
        build_deletion_receipt(
            receipt_id="receipt-early", plan=early, completed_at=instant(61)
        )

    with pytest.raises(StorageError, match="only shorten"):
        build_deletion_plan(
            plan_id="plan-looser",
            evidence=evidence,
            policy=policy(),
            requested_at=instant(300_000),
            reason_code="scheduled",
            active_legal_hold=False,
            retention_override_days=3,
        )

    hold = EvidenceLegalHold(WORKSPACE_ID, "hold-1", "ev-1", "litigation", instant(70))
    with writer(owned) as write:
        write.place_legal_hold(hold)
    assert active_legal_holds(owned.connection, WORKSPACE_ID, "ev-1") == (hold,)
    blocked = build_deletion_plan(
        plan_id="plan-held",
        evidence=evidence,
        policy=policy(),
        requested_at=instant(300_000),
        reason_code="scheduled",
        active_legal_hold=True,
        retention_override_days=1,
    )
    assert blocked.state is DeletionPlanState.BLOCKED_LEGAL_HOLD

    release = EvidenceLegalHoldRelease(
        WORKSPACE_ID, "release-1", "hold-1", "principal-1", instant(300_001)
    )
    with writer(owned) as write:
        write.release_legal_hold(release)
    assert active_legal_holds(owned.connection, WORKSPACE_ID, "ev-1") == ()

    ready = build_deletion_plan(
        plan_id="plan-ready",
        evidence=evidence,
        policy=policy(),
        requested_at=instant(300_002),
        reason_code="scheduled",
        active_legal_hold=False,
        retention_override_days=1,
    )
    assert ready.state is DeletionPlanState.READY
    assert (
        tuple(target.storage_class for target in ready.targets)
        == ALL_DELETION_STORAGE_CLASSES
    )
    assert {target.target_ref for target in ready.targets} == {"ev-1"}
    receipt = build_deletion_receipt(
        receipt_id="receipt-1", plan=ready, completed_at=instant(300_003)
    )
    with writer(owned) as write:
        write.append_plan(early)
        write.append_plan(blocked)
        write.append_plan(ready)
        write.append_receipt(receipt)
    verify_retention_digests(owned.connection, WORKSPACE_ID)

    documents = " ".join(
        str(value)
        for row in owned.connection.execute(
            "SELECT target_ref,target_digest FROM omnivia_semantic_evidence_deletion_targets"
        ).fetchall()
        for value in row
    )
    assert "blob://" not in documents
    assert "https://" not in documents


def test_retention_tables_are_append_only_and_fenced(owned) -> None:
    with pytest.raises(sqlite3.DatabaseError, match="authorized|unguarded INSERT"):
        owned.connection.execute(
            "INSERT INTO omnivia_semantic_retention_policies VALUES (?,?,?,?,?,?,?)",
            (WORKSPACE_ID, "raw", 1, 1, "second", "stated", "sha256:" + "a" * 64),
        )
    with writer(owned) as write:
        write.append_policy(policy())
    with pytest.raises(sqlite3.DatabaseError, match="authorized|append-only"):
        owned.connection.execute(
            "UPDATE omnivia_semantic_retention_policies SET default_retention_days=3"
        )
    with pytest.raises(sqlite3.DatabaseError, match="authorized|append-only"):
        owned.connection.execute("DELETE FROM omnivia_semantic_retention_policies")
    with pytest.raises(StaleGeneration), writer(owned, generation=owned.generation + 1):
        pass


def test_backup_restore_reverifies_content_free_retention_records(
    owned, tmp_path: Path
) -> None:
    evidence = read_evidence_item(owned.connection, WORKSPACE_ID, "ev-1")
    assert evidence is not None
    ready = build_deletion_plan(
        plan_id="plan-ready",
        evidence=evidence,
        policy=policy(),
        requested_at=instant(300_000),
        reason_code="scheduled",
        active_legal_hold=False,
    )
    with writer(owned) as write:
        write.append_policy(policy())
        write.append_plan(ready)
        write.append_receipt(
            build_deletion_receipt(
                receipt_id="receipt-1", plan=ready, completed_at=instant(300_001)
            )
        )
    owned.connection.close()

    installation = InstallationLayout(root=tmp_path / "installation-state")
    installation.create(WORKSPACE_ID)
    backup = create_verified_backup(
        owned.path,
        installation,
        workspace_id=WORKSPACE_ID,
        attempt_id=new_attempt_id(),
    )
    restored_path = restore_backup(backup.path, tmp_path / "restored.sqlite")
    restored = open_database(restored_path, OpenMode.READ_ONLY)
    try:
        verify_evidence_observation_digests(restored, WORKSPACE_ID)
        verify_retention_digests(restored, WORKSPACE_ID)
        assert restored.execute(
            "SELECT COUNT(*) FROM omnivia_semantic_evidence_deletion_targets"
        ).fetchone()[0] == len(ALL_DELETION_STORAGE_CLASSES)
    finally:
        restored.close()
