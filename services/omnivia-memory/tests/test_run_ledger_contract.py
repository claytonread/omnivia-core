"""Tests for the run-ledger contract."""

from omnivia_memory import (
    RUN_LEDGER_CONTRACT_VERSION,
    RUN_LEDGER_PATH_ENV,
    EvidenceFileRef,
    RunLedgerEntry,
    RunLedgerProvenance,
    RunLedgerStatus,
    validate_run_ledger_entry,
)
from omnivia_memory.run_ledger import (
    RUN_LEDGER_CONTRACT_VERSION as SUBMODULE_RUN_LEDGER_CONTRACT_VERSION,
)
from omnivia_memory.run_ledger import RUN_LEDGER_PATH_ENV as SUBMODULE_RUN_LEDGER_PATH_ENV
from omnivia_memory.run_ledger import (
    EvidenceFileRef as SubmoduleEvidenceFileRef,
)
from omnivia_memory.run_ledger import RunLedgerEntry as SubmoduleRunLedgerEntry
from omnivia_memory.run_ledger import (
    RunLedgerProvenance as SubmoduleRunLedgerProvenance,
)
from omnivia_memory.run_ledger import RunLedgerStatus as SubmoduleRunLedgerStatus


def test_run_ledger_entry_round_trips_through_dicts() -> None:
    entry = RunLedgerEntry(
        run_id="run-001",
        task_id="T-0103",
        target_repo="omnivia-core",
        lane_id="L2",
        status=RunLedgerStatus.RUNNING,
        started_at="2026-06-10T11:00:00Z",
        updated_at="2026-06-10T11:05:00Z",
        evidence_file_refs=[
            EvidenceFileRef(
                path="artifacts/runs/run-001/summary.md",
                kind="summary",
                description="Run summary",
            )
        ],
        provenance=RunLedgerProvenance(
            producer="omnivia-pm",
            source_ref="runs/run-001.json",
            producer_version="1.0.0",
        ),
    )

    restored = RunLedgerEntry.from_dict(entry.to_dict())
    result = validate_run_ledger_entry(restored)

    assert restored == entry
    assert result.valid
    assert result.warnings == []
    assert str(restored.contract_version) == "1.0"


def test_terminal_entries_require_completed_at_and_valid_nested_fields() -> None:
    entry = RunLedgerEntry(
        run_id="run-002",
        task_id="T-0104",
        target_repo="omnivia-dev",
        lane_id="L2",
        status=RunLedgerStatus.SUCCEEDED,
        started_at="2026-06-10T11:00:00Z",
        updated_at="2026-06-10T11:07:00Z",
        evidence_file_refs=[EvidenceFileRef(path="", kind="")],
        provenance=RunLedgerProvenance(producer=""),
        completed_at=None,
    )

    result = validate_run_ledger_entry(entry)

    assert not result.valid
    assert "completed_at is required for terminal run statuses" in result.errors
    assert "evidence_file_refs[0].path is required" in result.errors
    assert "evidence_file_refs[0].kind is required" in result.errors
    assert "provenance.producer is required" in result.errors


def test_package_root_exports_run_ledger_contracts() -> None:
    assert RUN_LEDGER_CONTRACT_VERSION is SUBMODULE_RUN_LEDGER_CONTRACT_VERSION
    assert RUN_LEDGER_PATH_ENV == SUBMODULE_RUN_LEDGER_PATH_ENV
    assert EvidenceFileRef is SubmoduleEvidenceFileRef
    assert RunLedgerEntry is SubmoduleRunLedgerEntry
    assert RunLedgerProvenance is SubmoduleRunLedgerProvenance
    assert RunLedgerStatus is SubmoduleRunLedgerStatus
