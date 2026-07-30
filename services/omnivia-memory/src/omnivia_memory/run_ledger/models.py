"""Compatibility facade for run-ledger contract models.

Deprecated: import these from ``omnivia_core.run_ledger.models`` instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# and cross-leaf-imported bindings (`Enum`, `Mapping`, `annotations`, `cast`,
# `dataclass`, `field`, and `ContractVersion`, which the canonical run-ledger
# models leaf imports from the knowledge leaf) -- names this leaf's historical
# namespace still has to resolve. No other error code is suppressed.
from omnivia_core.run_ledger.models import (  # type: ignore[attr-defined]
    RUN_LEDGER_CONTRACT_VERSION,
    RUN_LEDGER_PATH_ENV,
    ContractVersion,
    Enum,
    EvidenceFileRef,
    Mapping,
    RunLedgerEntry,
    RunLedgerProvenance,
    RunLedgerStatus,
    annotations,
    cast,
    dataclass,
    field,
)
