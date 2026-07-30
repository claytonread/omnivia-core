"""Compatibility facade for run-ledger validation.

Deprecated: import ``TERMINAL_RUN_STATUSES`` / ``validate_evidence_file_ref`` /
``validate_run_ledger_entry`` / ``validate_run_ledger_provenance`` from
``omnivia_core.run_ledger.validation`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# and cross-leaf-imported bindings (`annotations`, `datetime`, the four
# run-ledger contract classes and the version constant this leaf's sibling
# `models` owns, and `ValidationResult` /
# `check_contract_version_compatibility`, which the canonical validation module
# imports from the shared-validation and knowledge-validation leaves) -- names
# this leaf's historical namespace still has to resolve. No other error code is
# suppressed.
from omnivia_core.run_ledger.validation import (  # type: ignore[attr-defined]
    RUN_LEDGER_CONTRACT_VERSION,
    TERMINAL_RUN_STATUSES,
    EvidenceFileRef,
    RunLedgerEntry,
    RunLedgerProvenance,
    RunLedgerStatus,
    ValidationResult,
    annotations,
    check_contract_version_compatibility,
    datetime,
    validate_evidence_file_ref,
    validate_run_ledger_entry,
    validate_run_ledger_provenance,
)
