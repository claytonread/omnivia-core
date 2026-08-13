"""Permanent shape checks for the cross-platform V06-6 executable gate."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "scripts" / "check-v06-6-executables.py"


def _tree() -> ast.Module:
    return ast.parse(CHECKER.read_text(encoding="utf-8"), filename=str(CHECKER))


def _string_constants() -> set[str]:
    return {
        node.value
        for node in ast.walk(_tree())
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_gate_names_both_installed_console_executables() -> None:
    constants = _string_constants()
    assert "omnivia" in constants
    assert "omnivia-core-mcp" in constants


def test_gate_has_no_skip_or_xfail_control_flow() -> None:
    calls = [
        node.func.attr
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "skip" not in calls
    assert "xfail" not in calls


def test_gate_installs_wheels_without_an_index() -> None:
    constants = _string_constants()
    assert "--no-index" in constants
    assert "--only-binary=:all:" in constants
    assert "omnivia-core-cli" in constants
    assert "omnivia-core-mcp" in constants
