"""Validation helpers for the run-ledger contract."""

from __future__ import annotations

from datetime import datetime

from omnivia_memory.knowledge import (
    ValidationResult,
    check_contract_version_compatibility,
)
from omnivia_memory.run_ledger.models import (
    RUN_LEDGER_CONTRACT_VERSION,
    EvidenceFileRef,
    RunLedgerEntry,
    RunLedgerProvenance,
    RunLedgerStatus,
)

TERMINAL_RUN_STATUSES = frozenset(
    {
        RunLedgerStatus.SUCCEEDED,
        RunLedgerStatus.FAILED,
        RunLedgerStatus.BLOCKED,
        RunLedgerStatus.CANCELLED,
    }
)


def validate_run_ledger_entry(entry: RunLedgerEntry) -> ValidationResult:
    """Validate a run-ledger entry."""

    errors: list[str] = []
    warnings: list[str] = []

    version_result = check_contract_version_compatibility(
        entry.contract_version,
        RUN_LEDGER_CONTRACT_VERSION,
    )
    errors.extend(version_result.errors)
    warnings.extend(version_result.warnings)

    _require("run_id", entry.run_id, errors)
    _require("task_id", entry.task_id, errors)
    _require("target_repo", entry.target_repo, errors)
    _require("lane_id", entry.lane_id, errors)
    _validate_status(entry.status, errors)
    _validate_iso("started_at", entry.started_at, errors)
    _validate_iso("updated_at", entry.updated_at, errors)
    _validate_optional_iso("completed_at", entry.completed_at, errors)

    if entry.status in TERMINAL_RUN_STATUSES and entry.completed_at is None:
        errors.append("completed_at is required for terminal run statuses")
    if not entry.evidence_file_refs and entry.status in TERMINAL_RUN_STATUSES:
        warnings.append("terminal run has no evidence_file_refs")

    for index, evidence_file_ref in enumerate(entry.evidence_file_refs):
        _append_prefixed(
            validate_evidence_file_ref(evidence_file_ref),
            f"evidence_file_refs[{index}]",
            errors,
            warnings,
        )

    _append_prefixed(
        validate_run_ledger_provenance(entry.provenance),
        "provenance",
        errors,
        warnings,
    )

    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_evidence_file_ref(evidence_file_ref: EvidenceFileRef) -> ValidationResult:
    """Validate a run evidence file reference."""

    errors: list[str] = []
    warnings: list[str] = []
    _require("path", evidence_file_ref.path, errors)
    _require("kind", evidence_file_ref.kind, errors)
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def validate_run_ledger_provenance(
    provenance: RunLedgerProvenance,
) -> ValidationResult:
    """Validate run-ledger provenance metadata."""

    errors: list[str] = []
    warnings: list[str] = []
    _require("producer", provenance.producer, errors)
    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def _append_prefixed(
    result: ValidationResult,
    prefix: str,
    errors: list[str],
    warnings: list[str],
) -> None:
    errors.extend(f"{prefix}.{error}" for error in result.errors)
    warnings.extend(f"{prefix}.{warning}" for warning in result.warnings)


def _require(field_name: str, value: object, errors: list[str]) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        errors.append(f"{field_name} is required")


def _validate_status(value: RunLedgerStatus | object, errors: list[str]) -> None:
    if not isinstance(value, RunLedgerStatus):
        errors.append("status must be a RunLedgerStatus")


def _validate_iso(field_name: str, value: str, errors: list[str]) -> None:
    _require(field_name, value, errors)
    if value:
        _validate_optional_iso(field_name, value, errors)


def _validate_optional_iso(field_name: str, value: str | None, errors: list[str]) -> None:
    if value is None:
        return
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{field_name} must be an ISO timestamp")
