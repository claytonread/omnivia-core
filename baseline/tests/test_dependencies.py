"""Tests for the dependency and transitional-import drift check."""

from __future__ import annotations

import ast
import copy
import textwrap

import pytest

from baseline import CORE_PACKAGE
from baseline.dependencies import (
    ALLOWED_THIRD_PARTY,
    BANNED_IMPORT_PREFIXES,
    FACADE_COMPATIBILITY_DEPENDENCY,
    FACADE_COMPATIBILITY_IMPORTERS,
    FACADE_COMPATIBILITY_PACKAGE,
    TRANSITIONAL_IMPORT_REASONS,
    ImportEdge,
    _facade_dependency_problems,
    _normalize_expected_dependencies,
    build_dependency_inventory,
    collect_import_edges,
    is_runtime_module,
    verify_dependency_inventory,
)


def test_dependency_inventory_matches_the_frozen_baseline() -> None:
    assert verify_dependency_inventory() == []


def test_core_imports_no_unallowlisted_third_party_module() -> None:
    """Every third-party import must be allowlisted, except the compatibility-facade
    package: that one is a verified first-party route, checked narrowly and exactly by
    ``_facade_dependency_problems`` instead of the generic reason-only allowlist."""
    inventory = build_dependency_inventory()

    for entry in inventory["third_party_imports"]:
        if entry["module"] == FACADE_COMPATIBILITY_PACKAGE:
            continue
        assert entry["module"] in ALLOWED_THIRD_PARTY, entry
        assert entry["reason"], entry["module"]


def test_core_imports_no_private_repo_or_surface_module() -> None:
    """Core stays public-safe: no Platform, Dev, Cloud, MCP, HTTP, or CLI imports."""
    for edge in collect_import_edges():
        for prefix in BANNED_IMPORT_PREFIXES:
            assert not (
                edge.imported == prefix or edge.imported.startswith(f"{prefix}.")
            ), f"{edge.importer} imports {edge.imported}"


def test_every_transitional_import_is_allowlisted_with_a_reason() -> None:
    inventory = build_dependency_inventory()

    assert inventory["transitional_imports"], "the transitional edges should be recorded"
    for entry in inventory["transitional_imports"]:
        assert entry["reason"], f"{entry['importer']} -> {entry['imported']}"


def test_the_allowlist_stays_narrow() -> None:
    """Every allowlist entry must correspond to a real edge."""
    observed = {
        (entry["importer"], entry["imported"])
        for entry in build_dependency_inventory()["transitional_imports"]
    }

    assert set(TRANSITIONAL_IMPORT_REASONS) == observed


def test_layer_split_does_not_swallow_memory_graph() -> None:
    """`omnivia_memory.memory` must not capture `omnivia_memory.memory_graph`."""
    assert is_runtime_module(f"{CORE_PACKAGE}.memory.service")
    assert not is_runtime_module(f"{CORE_PACKAGE}.memory_graph.models")
    assert is_runtime_module(f"{CORE_PACKAGE}.memory_graph.store")


def test_declared_but_unimported_dependencies_are_recorded() -> None:
    """A declared dependency nothing imports is drift worth freezing, not hiding."""
    inventory = build_dependency_inventory()

    assert "sqlalchemy" in inventory["declared_but_unimported"]
    assert "sqlalchemy" not in {
        entry["module"] for entry in inventory["third_party_imports"]
    }


@pytest.mark.parametrize(
    "source, importer, expected",
    [
        # `from . import models` imports a module, not a name, so the edge must
        # point at the module. Recording the parent package instead would hide a
        # contract -> runtime edge.
        ("from . import models", f"{CORE_PACKAGE}.knowledge", f"{CORE_PACKAGE}.knowledge.models"),
        (
            "from .models import Thing",
            f"{CORE_PACKAGE}.knowledge.validation",
            f"{CORE_PACKAGE}.knowledge.models",
        ),
        (
            "from ..persistence import Database",
            f"{CORE_PACKAGE}.workspace.service",
            f"{CORE_PACKAGE}.persistence",
        ),
        (
            f"from {CORE_PACKAGE}.memory_graph import store",
            f"{CORE_PACKAGE}.knowledge.validation",
            f"{CORE_PACKAGE}.memory_graph.store",
        ),
        ("from typing import Any", f"{CORE_PACKAGE}.knowledge.models", "typing"),
    ],
)
def test_from_imports_resolve_to_the_module_they_really_touch(
    source, importer, expected
) -> None:
    from baseline.dependencies import _from_import_edges

    node = ast.parse(textwrap.dedent(source)).body[0]
    edges = _from_import_edges(node, importer)

    assert [edge.imported for edge in edges] == [expected]


def test_case_insensitive_filesystems_do_not_invent_modules() -> None:
    """`from .persistence import Database` must not resolve to a `Database` module."""
    from baseline.dependencies import _from_import_edges

    node = ast.parse("from .persistence import Database").body[0]
    edges = _from_import_edges(node, CORE_PACKAGE)

    assert [edge.imported for edge in edges] == [f"{CORE_PACKAGE}.persistence"]


def test_a_new_transitional_edge_is_reported(monkeypatch) -> None:
    """An undeclared contract -> runtime import must fail with its own name."""
    invented = ImportEdge(
        importer=f"{CORE_PACKAGE}.knowledge.validation",
        imported=f"{CORE_PACKAGE}.persistence.database",
        symbols=("Database",),
    )
    monkeypatch.setattr(
        "baseline.dependencies.collect_import_edges",
        lambda: [*collect_import_edges(), invented],
    )

    problems = verify_dependency_inventory()

    assert any(
        "knowledge.validation" in problem and "not allowlisted" in problem
        for problem in problems
    )


# --------------------------------------------------------------------------
# Compatibility-facade dependency normalization: the frozen Phase 0 baseline
# never declared omnivia_core as a dependency, so verify_dependency_inventory
# only accepts that delta after these checks pass, and only exactly as they
# describe it. Each case below asserts a specific way that delta could be
# wrong still fails closed.
# --------------------------------------------------------------------------


def test_facade_dependency_problems_is_empty_for_the_real_checkout() -> None:
    assert _facade_dependency_problems(build_dependency_inventory()) == []


def test_facade_dependency_problems_reports_a_missing_import() -> None:
    actual = build_dependency_inventory()
    actual = {
        **actual,
        "third_party_imports": [
            entry
            for entry in actual["third_party_imports"]
            if entry["module"] != FACADE_COMPATIBILITY_PACKAGE
        ],
    }

    problems = _facade_dependency_problems(actual)

    assert any("found no such import" in problem for problem in problems)


def test_facade_dependency_problems_reports_a_wrong_importer_set() -> None:
    actual = build_dependency_inventory()
    third_party = [dict(entry) for entry in actual["third_party_imports"]]
    for entry in third_party:
        if entry["module"] == FACADE_COMPATIBILITY_PACKAGE:
            entry["imported_by"] = [*entry["imported_by"], "omnivia_memory.some_other_module"]
    actual = {**actual, "third_party_imports": third_party}

    problems = _facade_dependency_problems(actual)

    assert any("importer set drifted" in problem for problem in problems)


def test_facade_dependency_problems_reports_a_missing_importer() -> None:
    actual = build_dependency_inventory()
    third_party = [dict(entry) for entry in actual["third_party_imports"]]
    for entry in third_party:
        if entry["module"] == FACADE_COMPATIBILITY_PACKAGE:
            entry["imported_by"] = entry["imported_by"][1:]
    actual = {**actual, "third_party_imports": third_party}

    problems = _facade_dependency_problems(actual)

    assert any("importer set drifted" in problem for problem in problems)


def test_facade_dependency_problems_reports_a_missing_declared_range() -> None:
    actual = build_dependency_inventory()
    actual = {
        **actual,
        "declared_dependencies": [
            dep for dep in actual["declared_dependencies"] if dep != FACADE_COMPATIBILITY_DEPENDENCY
        ],
    }

    problems = _facade_dependency_problems(actual)

    assert any("must declare" in problem for problem in problems)


def test_facade_dependency_problems_reports_a_wrong_declared_range() -> None:
    actual = build_dependency_inventory()
    actual = {
        **actual,
        "declared_dependencies": [
            "omnivia-core>=0.2.0,<0.3.0" if dep == FACADE_COMPATIBILITY_DEPENDENCY else dep
            for dep in actual["declared_dependencies"]
        ],
    }

    problems = _facade_dependency_problems(actual)

    assert any("must declare" in problem for problem in problems)


def test_normalize_expected_dependencies_does_not_mutate_its_argument() -> None:
    expected = {"declared_dependencies": ["sqlalchemy>=2.0.0"], "third_party_imports": []}
    before = copy.deepcopy(expected)

    _normalize_expected_dependencies(expected)

    assert expected == before


def test_normalize_expected_dependencies_adds_exactly_the_sanctioned_entry() -> None:
    expected = {"declared_dependencies": ["sqlalchemy>=2.0.0"], "third_party_imports": []}

    normalized = _normalize_expected_dependencies(expected)

    assert normalized["declared_dependencies"] == sorted(
        ["sqlalchemy>=2.0.0", FACADE_COMPATIBILITY_DEPENDENCY]
    )
    assert normalized["third_party_imports"] == [
        {
            "module": FACADE_COMPATIBILITY_PACKAGE,
            "imported_by": list(FACADE_COMPATIBILITY_IMPORTERS),
            "reason": None,
        }
    ]


def test_verify_dependency_inventory_fails_closed_on_unrelated_third_party_drift(
    monkeypatch,
) -> None:
    """Normalizing the sanctioned omnivia_core delta must not swallow an unrelated,
    unallowlisted third-party import that shows up alongside it."""
    real_build = build_dependency_inventory

    def _tampered():
        inventory = real_build()
        inventory = dict(inventory)
        inventory["third_party_imports"] = [
            *inventory["third_party_imports"],
            {"module": "requests", "imported_by": [f"{CORE_PACKAGE}.knowledge.models"], "reason": None},
        ]
        return inventory

    monkeypatch.setattr("baseline.dependencies.build_dependency_inventory", _tampered)

    problems = verify_dependency_inventory()

    assert any("requests" in problem and "not allowlisted" in problem for problem in problems)
