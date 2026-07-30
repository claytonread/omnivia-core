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
    DEPENDENCIES_PATH,
    FACADE_COMPATIBILITY_DEPENDENCY,
    FACADE_COMPATIBILITY_IMPORTERS,
    FACADE_COMPATIBILITY_PACKAGE,
    FACADE_ROOT_CANONICALIZED_BINDINGS,
    FACADE_ROOT_FROZEN_RUNTIME_BINDINGS,
    FACADE_ROOT_RUNTIME_BINDINGS,
    FACADE_STDLIB_IMPORT_MOVES,
    TRANSITIONAL_IMPORT_REASONS,
    ImportEdge,
    _canonical_module_imports_stdlib,
    _facade_dependency_problems,
    _facade_root_runtime_problems,
    _facade_stdlib_import_problems,
    _normalize_expected_dependencies,
    _root_canonical_from_imports,
    build_dependency_inventory,
    collect_import_edges,
    is_runtime_module,
    verify_dependency_inventory,
)
from baseline.determinism import load_json


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


# --------------------------------------------------------------------------
# Compatibility-facade stdlib-import moves: converting a leaf can remove the
# legacy tree's *only* import of a standard-library module, because the import
# moves to the canonical owner and this scan walks the legacy tree alone. Each
# such module is declared by name and proved before it may be normalized away.
# --------------------------------------------------------------------------

#: The declared moves, restated here rather than read off the declaration, so the
#: tests below fail if the declaration itself grows, shrinks, or is repointed.
_EXPECTED_STDLIB_MOVES = {"unicodedata": f"{CORE_PACKAGE}.knowledge.normalize"}


def test_stdlib_import_moves_declaration_is_exactly_the_knowledge_normalizer() -> None:
    """One module, one leaf. A second entry appearing without its own coverage
    would let a genuinely dropped stdlib dependency be normalized away silently."""
    assert FACADE_STDLIB_IMPORT_MOVES == _EXPECTED_STDLIB_MOVES
    for legacy_module in FACADE_STDLIB_IMPORT_MOVES.values():
        assert legacy_module in FACADE_COMPATIBILITY_IMPORTERS


def test_facade_stdlib_import_problems_accepts_the_declared_move() -> None:
    assert _facade_stdlib_import_problems(build_dependency_inventory()) == []


def test_facade_stdlib_import_problems_rejects_a_non_facade_leaf() -> None:
    """A stdlib import may only go missing because a *converted* leaf's
    implementation moved. Attributing it to an unconverted module would excuse a
    real deletion."""
    problems = _facade_stdlib_import_problems(
        build_dependency_inventory(),
        moves={"unicodedata": f"{CORE_PACKAGE}.persistence.database"},
    )
    assert any(
        "is not a declared compatibility-facade importer" in problem
        for problem in problems
    ), problems


def test_facade_stdlib_import_problems_rejects_a_module_the_baseline_never_had() -> None:
    """Declaring a move for a module Phase 0 never recorded is fabricated
    provenance: there is nothing to normalize away."""
    problems = _facade_stdlib_import_problems(
        build_dependency_inventory(),
        moves={"zoneinfo": f"{CORE_PACKAGE}.knowledge.normalize"},
    )
    assert any(
        "the frozen baseline never recorded it" in problem for problem in problems
    ), problems


def test_facade_stdlib_import_problems_rejects_a_stale_declaration() -> None:
    """A module still imported somewhere in the legacy tree needs no allowance.
    Keeping the entry would mask a *future* removal of that same module."""
    problems = _facade_stdlib_import_problems(
        build_dependency_inventory(),
        moves={"re": f"{CORE_PACKAGE}.knowledge.normalize"},
    )
    assert any(
        "still imported somewhere in the legacy tree" in problem
        for problem in problems
    ), problems


def test_facade_stdlib_import_problems_rejects_an_import_that_did_not_move() -> None:
    """The paired canonical module must really perform the import. Naming a
    canonical leaf that does not is how a dropped dependency would slip through --
    ``knowledge.models`` is the plausible wrong answer, being the normalizer's own
    sibling."""
    problems = _facade_stdlib_import_problems(
        build_dependency_inventory(),
        moves={"unicodedata": f"{CORE_PACKAGE}.knowledge.models"},
        importers=(*FACADE_COMPATIBILITY_IMPORTERS,),
    )
    assert any(
        "does not import 'unicodedata'" in problem and "rather than moved" in problem
        for problem in problems
    ), problems


def test_canonical_import_probe_matches_on_dotted_segments_only() -> None:
    """The probe must not accept a near miss. ``unicodedata`` is imported by the
    canonical normalizer; ``unicode`` and ``data`` are not, and neither is a
    module the file merely mentions in prose."""
    canonical = "omnivia_core.knowledge.normalize"
    assert _canonical_module_imports_stdlib(canonical, "unicodedata")
    assert _canonical_module_imports_stdlib(canonical, "re")
    assert not _canonical_module_imports_stdlib(canonical, "unicode")
    assert not _canonical_module_imports_stdlib(canonical, "data")
    assert not _canonical_module_imports_stdlib(canonical, "unicodedatax")
    assert not _canonical_module_imports_stdlib("omnivia_core.no_such_module", "re")


def test_normalize_drops_exactly_the_declared_stdlib_modules() -> None:
    """Removal is by exact key. A module whose name merely contains a declared
    one, and every module not declared at all, must survive."""
    expected = {
        "declared_dependencies": [],
        "third_party_imports": [],
        "stdlib_imports": [
            "re",
            "unicodedata",
            "unicodedatax",
            "xunicodedata",
            "unicode",
            "uuid",
        ],
    }

    normalized = _normalize_expected_dependencies(expected)

    assert normalized["stdlib_imports"] == [
        "re",
        "unicodedatax",
        "xunicodedata",
        "unicode",
        "uuid",
    ]


def test_normalize_leaves_stdlib_imports_alone_when_no_move_is_declared() -> None:
    frozen = load_json(DEPENDENCIES_PATH)

    normalized = _normalize_expected_dependencies(frozen, stdlib_moves={})

    assert normalized["stdlib_imports"] == frozen["stdlib_imports"]
    assert "unicodedata" in normalized["stdlib_imports"]


def test_verify_fails_closed_on_an_unverified_stdlib_import_move(monkeypatch) -> None:
    """The stdlib-move check runs *before* normalization and returns outright, so
    an unverified move can never be applied to the frozen baseline."""
    monkeypatch.setattr(
        "baseline.dependencies._facade_stdlib_import_problems",
        lambda *_args, **_kwargs: ["synthetic stdlib-move defect"],
    )

    def _must_not_run(*_args, **_kwargs):  # pragma: no cover - asserted unreachable
        raise AssertionError("normalization ran despite an unverified stdlib move")

    monkeypatch.setattr(
        "baseline.dependencies._normalize_expected_dependencies", _must_not_run
    )

    problems = verify_dependency_inventory()

    assert any(
        "stdlib-import move verification failed" in problem for problem in problems
    ), problems
    assert any("synthetic stdlib-move defect" in problem for problem in problems)


def test_verify_fails_closed_on_an_undeclared_stdlib_import_removal(monkeypatch) -> None:
    """There is no general "stdlib imports may shrink" allowance: dropping a
    module that has no declared move must still fail the inventory diff."""
    real_build = build_dependency_inventory

    def _tampered():
        inventory = dict(real_build())
        inventory["stdlib_imports"] = [
            module for module in inventory["stdlib_imports"] if module != "uuid"
        ]
        return inventory

    monkeypatch.setattr("baseline.dependencies.build_dependency_inventory", _tampered)

    problems = verify_dependency_inventory()

    assert any("uuid" in problem for problem in problems), problems


# --------------------------------------------------------------------------
# The converted compatibility root.
#
# Two things changed here, and only two. The root became an exact
# ``omnivia_core`` importer, so it joined ``FACADE_COMPATIBILITY_IMPORTERS``. And
# its transitional edge *moved*: it used to take ``MemoryCreate``,
# ``MemoryService`` and ``MemoryUpdate`` together from ``omnivia_memory.memory``,
# and now takes the two inputs from canonical Core while reaching
# ``omnivia_memory.memory.service`` for the one object Core deliberately does not
# own. That is a genuine frozen-vs-live difference in ``transitional_imports``
# that no pre-existing normalization covered, so it is declared as two
# exactly-known states and pinned in both directions below.
# --------------------------------------------------------------------------

#: The importer-set delta this batch is allowed to cause: the package root, and
#: nothing else. Restated rather than derived so a widened importer allowance --
#: another barrel, or a prefix match -- fails here.
_EXPECTED_IMPORTER_DELTA = frozenset({CORE_PACKAGE})

#: The 30 converted facade leaves, as the set that was there before the root
#: joined. Together with the delta above this pins the whole importer tuple
#: without restating 31 module paths.
_LEAF_IMPORTERS = frozenset(FACADE_COMPATIBILITY_IMPORTERS) - _EXPECTED_IMPORTER_DELTA

_EXPECTED_ROOT_RUNTIME_BINDINGS = {
    f"{CORE_PACKAGE}.memory.service": ("MemoryService",),
    f"{CORE_PACKAGE}.persistence": ("Database",),
}
_EXPECTED_ROOT_FROZEN_RUNTIME_BINDINGS = {
    f"{CORE_PACKAGE}.memory": ("MemoryCreate", "MemoryService", "MemoryUpdate"),
    f"{CORE_PACKAGE}.persistence": ("Database",),
}
_EXPECTED_ROOT_CANONICALIZED_BINDINGS = {
    "MemoryCreate": f"{FACADE_COMPATIBILITY_PACKAGE}.memory.models",
    "MemoryUpdate": f"{FACADE_COMPATIBILITY_PACKAGE}.memory.models",
}


def test_the_package_root_is_the_only_non_leaf_facade_importer() -> None:
    """The importer set is 31 entries: the 30 converted leaves plus the root.

    Pinned as an exact delta rather than as "contains the root", so adding another
    barrel-or-root importer has to be a deliberate edit here. Every leaf entry is
    a dotted submodule; the root is the one bare package name.
    """
    assert frozenset(FACADE_COMPATIBILITY_IMPORTERS) - _LEAF_IMPORTERS == (
        _EXPECTED_IMPORTER_DELTA
    )
    assert CORE_PACKAGE in FACADE_COMPATIBILITY_IMPORTERS
    assert len(FACADE_COMPATIBILITY_IMPORTERS) == 31
    assert len(_LEAF_IMPORTERS) == 30
    assert list(FACADE_COMPATIBILITY_IMPORTERS) == sorted(
        FACADE_COMPATIBILITY_IMPORTERS
    )
    bare = [name for name in FACADE_COMPATIBILITY_IMPORTERS if "." not in name]
    assert bare == [CORE_PACKAGE]
    for name in _LEAF_IMPORTERS:
        assert name.startswith(f"{CORE_PACKAGE}.")


def test_facade_dependency_problems_rejects_dropping_the_root_importer() -> None:
    """The importer set is compared exactly. With the root converted, omitting it
    from the declaration must fail rather than be tolerated as a subset."""
    problems = _facade_dependency_problems(
        build_dependency_inventory(),
        importers=tuple(sorted(_LEAF_IMPORTERS)),
    )
    assert any("importer set drifted" in problem for problem in problems), problems


def test_root_runtime_binding_declarations_are_exact() -> None:
    """The three declarations, restated here so a widened one fails this test
    instead of licensing itself. Two runtime edges, four symbols across the frozen
    pair, and exactly two symbols that moved to canonical Core."""
    assert FACADE_ROOT_RUNTIME_BINDINGS == _EXPECTED_ROOT_RUNTIME_BINDINGS
    assert FACADE_ROOT_FROZEN_RUNTIME_BINDINGS == (
        _EXPECTED_ROOT_FROZEN_RUNTIME_BINDINGS
    )
    assert FACADE_ROOT_CANONICALIZED_BINDINGS == (
        _EXPECTED_ROOT_CANONICALIZED_BINDINGS
    )

    # The converted edges reach runtime modules only, and every symbol they carry
    # was already carried by the frozen pair: nothing new is introduced as a "move".
    frozen_symbols = {
        symbol
        for symbols in FACADE_ROOT_FROZEN_RUNTIME_BINDINGS.values()
        for symbol in symbols
    }
    for module, symbols in FACADE_ROOT_RUNTIME_BINDINGS.items():
        assert is_runtime_module(module), module
        assert set(symbols) <= frozen_symbols

    # The two canonicalized bindings are exactly the frozen symbols that are no
    # longer taken from the legacy runtime.
    converted_symbols = {
        symbol for symbols in FACADE_ROOT_RUNTIME_BINDINGS.values() for symbol in symbols
    }
    assert frozen_symbols - converted_symbols == set(
        FACADE_ROOT_CANONICALIZED_BINDINGS
    )
    for canonical_module in FACADE_ROOT_CANONICALIZED_BINDINGS.values():
        assert canonical_module.startswith(f"{FACADE_COMPATIBILITY_PACKAGE}.")


def test_transitional_import_reasons_cover_the_converted_root_edges() -> None:
    """Both converted root edges carry a stated reason, the superseded
    ``omnivia_memory.memory`` key is gone, and the reason on the moved edge says
    why the two inputs are no longer on it."""
    keys = {
        imported for importer, imported in TRANSITIONAL_IMPORT_REASONS if importer == CORE_PACKAGE
    }
    assert keys == set(FACADE_ROOT_RUNTIME_BINDINGS)
    assert (CORE_PACKAGE, f"{CORE_PACKAGE}.memory") not in TRANSITIONAL_IMPORT_REASONS
    for imported in FACADE_ROOT_RUNTIME_BINDINGS:
        assert TRANSITIONAL_IMPORT_REASONS[(CORE_PACKAGE, imported)].strip()
    service_reason = TRANSITIONAL_IMPORT_REASONS[
        (CORE_PACKAGE, f"{CORE_PACKAGE}.memory.service")
    ]
    assert "MemoryCreate" in service_reason and "MemoryUpdate" in service_reason


def test_facade_root_runtime_problems_accepts_the_declared_move() -> None:
    assert _facade_root_runtime_problems(build_dependency_inventory()) == []


def test_facade_root_runtime_problems_rejects_a_stale_frozen_declaration() -> None:
    """The declaration names the state it was written for. Pointed at a frozen
    baseline it does not describe, it must refuse rather than overwrite whatever it
    finds."""
    problems = _facade_root_runtime_problems(
        build_dependency_inventory(),
        frozen_bindings={f"{CORE_PACKAGE}.memory": ("MemoryService",)},
    )
    assert any(
        "the frozen baseline records root runtime edges" in problem
        for problem in problems
    ), problems


def test_facade_root_runtime_problems_rejects_an_extra_live_runtime_edge() -> None:
    """A third runtime edge out of the root -- or a fifth symbol on an existing one
    -- must fail rather than ride in behind this allowance."""
    inventory = copy.deepcopy(build_dependency_inventory())
    inventory["transitional_imports"] = [
        *inventory["transitional_imports"],
        {
            "importer": CORE_PACKAGE,
            "imported": f"{CORE_PACKAGE}.search.service",
            "symbols": ["SearchService"],
            "reason": None,
        },
    ]
    problems = _facade_root_runtime_problems(inventory)
    assert any(
        "the checkout has root runtime edges" in problem for problem in problems
    ), problems


def test_facade_root_runtime_problems_rejects_a_non_runtime_owner() -> None:
    """A declared root runtime edge must point at a module the layering rules
    really call runtime; a contract module could not be a legacy-owned exception."""
    problems = _facade_root_runtime_problems(
        build_dependency_inventory(),
        converted={f"{CORE_PACKAGE}.knowledge": ("MemoryService",)},
    )
    assert any("the checkout has root runtime edges" in problem for problem in problems)

    # ...and when the live edges *are* the declared ones, the runtime-module check
    # is what rejects a contract owner.
    inventory = copy.deepcopy(build_dependency_inventory())
    inventory["transitional_imports"] = [
        entry
        for entry in inventory["transitional_imports"]
        if not (
            entry["importer"] == CORE_PACKAGE
            and entry["imported"] == f"{CORE_PACKAGE}.memory.service"
        )
    ] + [
        {
            "importer": CORE_PACKAGE,
            "imported": f"{CORE_PACKAGE}.knowledge",
            "symbols": ["MemoryService"],
            "reason": None,
        }
    ]
    problems = _facade_root_runtime_problems(
        inventory,
        converted={
            f"{CORE_PACKAGE}.knowledge": ("MemoryService",),
            f"{CORE_PACKAGE}.persistence": ("Database",),
        },
    )
    assert any("is not a runtime module" in problem for problem in problems), problems


def test_facade_root_runtime_problems_rejects_a_binding_that_did_not_move() -> None:
    """A canonicalized binding has to be proved *moved*, not merely gone: the root's
    source must really import it from the declared canonical owner. Naming a
    plausible wrong owner -- the memory barrel rather than its models leaf -- fails.
    """
    problems = _facade_root_runtime_problems(
        build_dependency_inventory(),
        canonicalized={"MemoryCreate": f"{FACADE_COMPATIBILITY_PACKAGE}.memory"},
    )
    assert any(
        "does not import it from" in problem and "rather than moved" in problem
        for problem in problems
    ), problems


def test_facade_root_runtime_problems_rejects_a_stale_canonicalized_binding() -> None:
    """An entry kept after the binding came back to the legacy edge would keep
    excusing it. It must be reported as stale instead."""
    problems = _facade_root_runtime_problems(
        build_dependency_inventory(),
        canonicalized={"MemoryService": f"{FACADE_COMPATIBILITY_PACKAGE}.memory.models"},
    )
    assert any(
        "still taken from a legacy runtime module" in problem for problem in problems
    ), problems


def test_facade_root_runtime_problems_requires_the_root_to_be_a_facade_importer() -> None:
    """The move is only attributable to the sanctioned conversion if the root really
    is a declared ``omnivia_core`` importer."""
    problems = _facade_root_runtime_problems(
        build_dependency_inventory(),
        importers=tuple(sorted(_LEAF_IMPORTERS)),
    )
    assert problems == [
        (f"compatibility root {CORE_PACKAGE!r}: not a declared compatibility-facade "
        "importer, so its runtime edges cannot have moved with the root conversion")
    ]


def test_root_canonical_from_imports_reads_the_source_not_the_module() -> None:
    """The proof reads the file with ``ast``, so a runtime-patched root cannot
    satisfy it, and it records only canonical from-imports."""
    found = _root_canonical_from_imports()
    assert found[f"{FACADE_COMPATIBILITY_PACKAGE}.memory.models"] == (
        "MemoryCreate",
        "MemoryUpdate",
    )
    assert f"{FACADE_COMPATIBILITY_PACKAGE}.provenance" in found
    for module in found:
        assert module.startswith(FACADE_COMPATIBILITY_PACKAGE)
    # The legacy runtime owners are deliberately absent: this helper reports the
    # canonical half only.
    assert not any(module.startswith(f"{CORE_PACKAGE}.") for module in found)


def test_normalize_expected_dependencies_replaces_only_the_root_edges() -> None:
    """The normalization touches transitional edges whose importer *is* the package
    root, and nothing else. Every other edge in the artifact still has to match."""
    other = {
        "importer": f"{CORE_PACKAGE}.memory_graph",
        "imported": f"{CORE_PACKAGE}.memory_graph.store",
        "symbols": ["MemoryGraphStore"],
        "reason": "kept",
    }
    expected = {
        "declared_dependencies": [],
        "third_party_imports": [],
        "transitional_imports": [
            {
                "importer": CORE_PACKAGE,
                "imported": f"{CORE_PACKAGE}.memory",
                "symbols": ["MemoryCreate", "MemoryService", "MemoryUpdate"],
                "reason": "superseded",
            },
            {
                "importer": CORE_PACKAGE,
                "imported": f"{CORE_PACKAGE}.persistence",
                "symbols": ["Database"],
                "reason": "kept",
            },
            other,
        ],
    }
    before = copy.deepcopy(expected)

    normalized = _normalize_expected_dependencies(expected)

    assert expected == before, "the frozen artifact must not be mutated in place"
    assert [
        (entry["importer"], entry["imported"], tuple(entry["symbols"]))
        for entry in normalized["transitional_imports"]
    ] == [
        (CORE_PACKAGE, f"{CORE_PACKAGE}.memory.service", ("MemoryService",)),
        (CORE_PACKAGE, f"{CORE_PACKAGE}.persistence", ("Database",)),
        (f"{CORE_PACKAGE}.memory_graph", f"{CORE_PACKAGE}.memory_graph.store", ("MemoryGraphStore",)),
    ]
    # The untouched edge keeps its own reason; the replaced ones take theirs from
    # the allowlist rather than from the superseded artifact text.
    assert normalized["transitional_imports"][2] == other
    assert normalized["transitional_imports"][0]["reason"] == (
        TRANSITIONAL_IMPORT_REASONS[(CORE_PACKAGE, f"{CORE_PACKAGE}.memory.service")]
    )


def test_verify_dependency_inventory_refuses_to_normalize_an_unverified_root(
    monkeypatch,
) -> None:
    """The normalization is gated on the proof. With the proof failing, the frozen
    baseline is left alone and the failure says so by name."""
    real_build = build_dependency_inventory

    def _tampered():
        inventory = copy.deepcopy(real_build())
        inventory["transitional_imports"] = [
            *inventory["transitional_imports"],
            {
                "importer": CORE_PACKAGE,
                "imported": f"{CORE_PACKAGE}.search.service",
                "symbols": ["SearchService"],
                "reason": None,
            },
        ]
        return inventory

    monkeypatch.setattr("baseline.dependencies.build_dependency_inventory", _tampered)

    problems = verify_dependency_inventory()

    assert any(
        "Compatibility root runtime-edge verification failed" in problem
        for problem in problems
    ), problems
