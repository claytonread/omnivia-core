"""OmniVia Core Phase 0 baseline freeze.

This package captures a reproducible behavioural and data baseline for Core
*before* the approved package, contract, and workspace-ownership migrations.
It adds no runtime behaviour: every module here only observes existing Core
code, writes deterministic artifacts, and fails loudly when the observed
surface drifts from the frozen one.

Contents:

- ``determinism`` - normalisation and redaction so artifacts are byte-identical
  across machines and carry no machine-specific absolute paths.
- ``inventory`` - the public Python export inventory and its drift check.
- ``surfaces`` - declared HTTP / MCP / CLI surface inventories for the parts of
  the baseline slice that Core cannot execute, with an explicit evidence guard.
- ``scenarios`` - golden fixtures captured by running real Core code paths.
- ``dependencies`` - third-party dependency and transitional-import drift check.
- ``legacy_db`` - legacy ``memories.db`` backup, read-only capture, checksum,
  restore, and rollback helpers that operate only on generated fixtures.

Artifacts live in ``baseline/inventories`` and ``baseline/fixtures`` and are
regenerated with ``python -m baseline capture``; ``python -m baseline verify``
checks the working tree against them.
"""

from __future__ import annotations

from pathlib import Path

#: Version of the baseline artifact format. Bump when the on-disk shape of a
#: tracked artifact changes so stale artifacts fail verification loudly.
BASELINE_FORMAT_VERSION = "1"

#: Identifier of the task that froze this baseline.
BASELINE_TASK_ID = "T-0627"

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
INVENTORY_DIR = PACKAGE_ROOT / "inventories"
FIXTURE_DIR = PACKAGE_ROOT / "fixtures"

#: Import root for the Core package under baseline.
CORE_SRC = REPO_ROOT / "services" / "omnivia-memory" / "src"
CORE_PACKAGE = "omnivia_memory"

__all__ = [
    "BASELINE_FORMAT_VERSION",
    "BASELINE_TASK_ID",
    "CORE_PACKAGE",
    "CORE_SRC",
    "FIXTURE_DIR",
    "INVENTORY_DIR",
    "PACKAGE_ROOT",
    "REPO_ROOT",
]
