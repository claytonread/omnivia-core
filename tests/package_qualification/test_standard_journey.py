"""Static guards for the isolated V06-7 Standard-profile journey."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
JOURNEY = REPO_ROOT / "scripts" / "run-standard-journey.py"


def _tree() -> ast.Module:
    return ast.parse(JOURNEY.read_text(encoding="utf-8"), filename=str(JOURNEY))


def _constants() -> set[str]:
    return {
        node.value
        for node in ast.walk(_tree())
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_journey_invokes_only_the_three_installed_product_executables() -> None:
    constants = _constants()
    assert {"omnivia-core-service", "omnivia", "omnivia-core-mcp"} <= constants
    imports = {
        alias.name
        for node in ast.walk(_tree())
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any(name.startswith("omnivia") for name in imports)


def test_journey_covers_initialization_governance_mcp_and_recovery() -> None:
    constants = _constants()
    assert {
        "--init",
        "--capture-source",
        "--source-id",
        "memory",
        "create",
        "governance",
        "propose",
        "approve",
        "knowledge_search",
        "context_pack_build",
        "managed-local crash recovery",
    } <= constants


def test_journey_has_no_skip_or_xfail_path() -> None:
    attributes = {
        node.attr for node in ast.walk(_tree()) if isinstance(node, ast.Attribute)
    }
    assert not {"skip", "skipif", "xfail", "importorskip"} & attributes
