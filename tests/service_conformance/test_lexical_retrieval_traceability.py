"""Permanent invariants for the lexical-retrieval traceability fixture.

``tests/fixtures/service_conformance/lexical-retrieval-traceability-v1.json`` maps
each applicable lexical-retrieval clause to the tests that show it, or marks it
``pending`` with the reason nothing does. This module does not run that evidence;
it holds the fixture to it: every named test must exist as a top-level function in
the named file and carry no skip or xfail marker, a covered clause names evidence
and a pending one names none, and the pending set is exact, so closing a gap or
opening one has to change this file.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE = json.loads(
    (
        REPO_ROOT
        / "tests"
        / "fixtures"
        / "service_conformance"
        / "lexical-retrieval-traceability-v1.json"
    ).read_text(encoding="utf-8")
)
OPERATIONS = json.loads(
    (
        REPO_ROOT
        / "tests"
        / "fixtures"
        / "service_conformance"
        / "operation-traceability-v1.json"
    ).read_text(encoding="utf-8")
)["operations"]
RUNTIME = (
    REPO_ROOT / "packages" / "omnivia-core-runtime" / "src" / "omnivia_core_runtime"
)
LEXICAL_SOURCES = (
    RUNTIME / "storage" / "projections" / "fts.py",
    RUNTIME / "storage" / "retrieval.py",
    RUNTIME / "service" / "handlers" / "evidence.py",
)
CLAUSE_IDS = [f"LEX-{ordinal:02d}" for ordinal in range(1, 8)]
#: Exact: closing a gap moves a clause out of this set and opening one moves it in.
PENDING: set[str] = set()
CLAUSE_KEYS = {"clause_id", "clause", "state", "evidence"}
REFUSED_MARKERS = {"skip", "skipif", "xfail"}


def _clauses() -> list[dict[str, Any]]:
    return list(FIXTURE["clauses"])


def _references(clause: dict[str, Any]) -> list[str]:
    return [*clause["evidence"], *clause.get("partial_evidence", [])]


def _function(reference: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    path, _, name = reference.partition("::")
    assert name, f"{reference}: expected <path>::<function>"
    source = REPO_ROOT / path
    assert source.is_file(), f"{reference}: no such file"
    for node in ast.parse(source.read_text(encoding="utf-8")).body:
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == name
        ):
            return node
    raise AssertionError(f"{reference}: no top-level function of that name")


def test_the_fixture_is_the_declared_format_and_holds_the_clauses_in_order() -> None:
    assert FIXTURE["format"] == "omnivia.lexical-retrieval-traceability.v1"
    assert set(FIXTURE["states"]) == {"covered", "pending"}
    assert [clause["clause_id"] for clause in _clauses()] == CLAUSE_IDS


def test_every_clause_is_covered_or_pending_with_what_that_state_requires() -> None:
    for clause in _clauses():
        identifier = clause["clause_id"]
        assert CLAUSE_KEYS <= set(clause), identifier
        assert clause["state"] in FIXTURE["states"], identifier
        if clause["state"] == "covered":
            assert clause["evidence"], f"{identifier}: covered with no evidence"
            assert clause["covers"].strip(), identifier
            assert "pending_reason" not in clause, identifier
        else:
            assert clause["evidence"] == [], (
                f"{identifier}: pending must not claim evidence"
            )
            assert clause["pending_reason"].strip(), identifier


def test_the_pending_set_is_exactly_the_known_gaps() -> None:
    pending = {c["clause_id"] for c in _clauses() if c["state"] == "pending"}
    assert pending == PENDING


def test_every_named_test_exists_and_is_not_skipped() -> None:
    seen: set[str] = set()
    for clause in _clauses():
        for reference in _references(clause):
            function = _function(reference)
            markers = {
                ast.unparse(decorator).split("(")[0].rsplit(".", 1)[-1]
                for decorator in function.decorator_list
            }
            assert not markers & REFUSED_MARKERS, f"{reference}: {sorted(markers)}"
            seen.add(reference)
    assert seen


def test_evidence_erase_is_the_one_erasing_operation_and_lex_01_cites_its_tests() -> (
    None
):
    """LEX-01 rests on `evidence.erase`: the operation exists, writes, and is covered."""
    erase = [
        op["contract"]
        for op in OPERATIONS
        if op["contract"]["name"] == "evidence.erase"
    ]
    assert len(erase) == 1
    assert erase[0]["scope"]["side_effect"] == "update"
    assert erase[0]["scope"]["required_scopes"] == ["memory:write"]
    assert erase[0]["idempotency"]["required"] is True
    assert erase[0]["audit"]["audited"] is True
    families = ("evidence.", "document.", "knowledge.", "memory.", "context_pack.")
    erasing = re.compile(r"erase|purge|delete|forget|remove|retention")
    names = [op["contract"]["name"] for op in OPERATIONS]
    assert [n for n in names if n.startswith(families) and erasing.search(n)] == [
        "evidence.erase"
    ]
    lex_01 = next(c for c in _clauses() if c["clause_id"] == "LEX-01")
    assert lex_01["state"] == "covered"
    assert lex_01["evidence"]
    assert all(
        "test_evidence_erase_vertical.py::test_lex_01_" in reference
        for reference in lex_01["evidence"]
    )


def test_the_lexical_path_holds_no_cache() -> None:
    """The reason LEX-06 can rest on per-connection storage and workspace-scoped reads."""
    for source in LEXICAL_SOURCES:
        text = source.read_text(encoding="utf-8")
        assert not re.search(r"cache", text, re.IGNORECASE), source.name
