"""The omnivia-memory facade foundation: seven leaves plus four barrels.

Phase 1 ported portable leaves from ``services/omnivia-memory`` into
``src/omnivia_core`` as source-parity copies (see
``tests/canonical_migration``). This slice retires that duplication leaf by
leaf: ``_shared.validation``, ``lifecycle.models``, ``lifecycle.rules``,
``provenance.models``, ``memory.models``, ``app_shell_bridge.models``, and
``app_shell_bridge.validation`` in ``omnivia_memory`` are no longer copies --
they are thin compatibility facades whose supported symbols are the *exact*
canonical objects (``omnivia_memory.X.Symbol is omnivia_core.X.Symbol``), not
structurally equal lookalikes. The four barrels above them (``_shared``,
``lifecycle``, ``provenance``, ``app_shell_bridge``) already delegated to
their sibling leaves and needed no source change, but their re-exported
objects are now canonical too as a result -- the app-shell barrel becomes
identity-preserving purely transitively, through its two converted leaves.

This module is the dedicated verification for that transition, independent
of the ``tests/canonical_migration`` source-parity gates (which now exclude
these seven leaves via ``FACADE_CANONICAL_TO_LEGACY`` -- see
``tests/canonical_migration/_leaves.py`` and
``tests/canonical_migration/test_parity.py``).
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = REPO_ROOT / "src"


def _load_leaves_manifest():
    """Load ``tests/canonical_migration/_leaves.py`` by file path.

    That directory has no ``__init__.py`` (it is imported as a bare, unpackaged
    module by ``tests/canonical_migration/test_parity.py`` itself), so it is
    not reliably reachable via a dotted ``tests.canonical_migration`` import --
    whether that resolves depends on which other directories a given pytest
    invocation has already added to ``sys.path`` as import roots. Loading the
    file directly sidesteps that path/collection-order dependency entirely.
    """
    path = REPO_ROOT / "tests" / "canonical_migration" / "_leaves.py"
    spec = importlib.util.spec_from_file_location("_facade_foundation_leaves_manifest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FACADE_CANONICAL_TO_LEGACY = _load_leaves_manifest().FACADE_CANONICAL_TO_LEGACY
MEMORY_SRC = REPO_ROOT / "services" / "omnivia-memory" / "src"
PYTHON = sys.executable

#: The legacy leaves converted into facades, and -- for each supported symbol
#: bound at that leaf's module scope -- the canonical module that owns the
#: exact object it must route to. Declared independently
#: of ``FACADE_CANONICAL_TO_LEGACY`` (rather than derived from it) so this
#: test also catches that manifest silently drifting, per
#: ``test_facade_canonical_to_legacy_manifest_matches_expected_seven_pairs``.
#:
#: Each leaf's set covers its full historical module-scope namespace, not
#: just its contract names: these leaves never declared ``__all__``
#: (``test_leaf_wrapper_has_no_all`` below), so every non-private binding a
#: caller could import before this slice -- including the "incidental"
#: names an ``import``/``from ... import`` statement leaves behind, such as
#: ``Any``, ``annotations``, ``Enum``, or a plain module binding like
#: ``uuid`` -- is still part of the public surface today and must still
#: resolve to the exact same object.
LEAF_SYMBOL_SOURCES: dict[str, dict[str, str]] = {
    "omnivia_memory._shared.validation": {
        "SENSITIVE_KEYS": "omnivia_core._shared.validation",
        "ValidationResult": "omnivia_core._shared.validation",
        "scan_sensitive_fields": "omnivia_core._shared.validation",
        "validate_iso_timestamp": "omnivia_core._shared.validation",
        "validate_optional_iso_timestamp": "omnivia_core._shared.validation",
        "Any": "omnivia_core._shared.validation",
        "annotations": "omnivia_core._shared.validation",
        "dataclass": "omnivia_core._shared.validation",
        "datetime": "omnivia_core._shared.validation",
        "field": "omnivia_core._shared.validation",
    },
    "omnivia_memory.app_shell_bridge.models": {
        "AppShellBodyDescriptor": "omnivia_core.app_shell_bridge.models",
        "AppShellHostContext": "omnivia_core.app_shell_bridge.models",
        "AppShellRuntimeState": "omnivia_core.app_shell_bridge.models",
        "AppShellSource": "omnivia_core.app_shell_bridge.models",
        "Enum": "omnivia_core.app_shell_bridge.models",
        "List": "omnivia_core.app_shell_bridge.models",
        # Not the ``_shared.validation`` primitive of the same name: the App
        # Shell bridge historically defined its own ``ValidationResult``
        # dataclass, and this leaf must keep routing to that one. See
        # ``test_app_shell_validation_result_keeps_its_historical_collision_owner``.
        "ValidationResult": "omnivia_core.app_shell_bridge.models",
        "dataclass": "omnivia_core.app_shell_bridge.models",
        "field": "omnivia_core.app_shell_bridge.models",
    },
    "omnivia_memory.app_shell_bridge.validation": {
        "Any": "omnivia_core.app_shell_bridge.validation",
        "AppShellBridgeValidationError": "omnivia_core.app_shell_bridge.validation",
        "Dict": "omnivia_core.app_shell_bridge.validation",
        "List": "omnivia_core.app_shell_bridge.validation",
        "TYPE_CHECKING": "omnivia_core.app_shell_bridge.validation",
        "validate_app_shell_body_descriptor": "omnivia_core.app_shell_bridge.validation",
        "validate_app_shell_host_context": "omnivia_core.app_shell_bridge.validation",
    },
    "omnivia_memory.lifecycle.models": {
        "LifecycleState": "omnivia_core.lifecycle.models",
        "Enum": "omnivia_core.lifecycle.models",
        "annotations": "omnivia_core.lifecycle.models",
    },
    "omnivia_memory.lifecycle.rules": {
        "CreatedBy": "omnivia_core.lifecycle.rules",
        "LifecycleRules": "omnivia_core.lifecycle.rules",
        "LifecycleState": "omnivia_core.lifecycle.models",
        "Enum": "omnivia_core.lifecycle.rules",
        "annotations": "omnivia_core.lifecycle.rules",
    },
    "omnivia_memory.provenance.models": {
        "Source": "omnivia_core.provenance.models",
        "SourceType": "omnivia_core.provenance.models",
        "Any": "omnivia_core.provenance.models",
        "Enum": "omnivia_core.provenance.models",
        "annotations": "omnivia_core.provenance.models",
    },
    "omnivia_memory.memory.models": {
        "Memory": "omnivia_core.memory.models",
        "MemoryCreate": "omnivia_core.memory.models",
        "MemoryUpdate": "omnivia_core.memory.models",
        "LifecycleState": "omnivia_core.lifecycle.models",
        "CreatedBy": "omnivia_core.lifecycle.rules",
        "Source": "omnivia_core.provenance.models",
        "Any": "omnivia_core.memory.models",
        "annotations": "omnivia_core.memory.models",
        "dataclass": "omnivia_core.memory.models",
        "datetime": "omnivia_core.memory.models",
        "field": "omnivia_core.memory.models",
        "timezone": "omnivia_core.memory.models",
        "uuid": "omnivia_core.memory.models",
    },
}

#: Each leaf's entire body must be exactly one ``from <module> import (...)``
#: statement, sourced from this single module. This is stricter than
#: ``LEAF_SYMBOL_SOURCES`` (which records, per symbol, whichever canonical
#: module *owns* the exact object, for the identity check) -- a leaf may
#: legitimately route a name's identity check to a sibling canonical module
#: (``lifecycle.rules.LifecycleState`` owns the same object as
#: ``lifecycle.models.LifecycleState``) while still importing that name from
#: only one place. See ``_assert_leaf_is_exact_route_facade``.
LEAF_IMPORT_SOURCE: dict[str, str] = {
    "omnivia_memory._shared.validation": "omnivia_core._shared.validation",
    "omnivia_memory.app_shell_bridge.models": "omnivia_core.app_shell_bridge.models",
    "omnivia_memory.app_shell_bridge.validation": "omnivia_core.app_shell_bridge.validation",
    "omnivia_memory.lifecycle.models": "omnivia_core.lifecycle.models",
    "omnivia_memory.lifecycle.rules": "omnivia_core.lifecycle.rules",
    "omnivia_memory.provenance.models": "omnivia_core.provenance.models",
    "omnivia_memory.memory.models": "omnivia_core.memory.models",
}

#: The barrels above the converted leaves, all source-unchanged, and the exact
#: ordered ``__all__`` each must keep (architecture decision: preserve the
#: existing ordered literal list). Keyed by the shared package-relative suffix
#: so both trees (``omnivia_core.<suffix>`` / ``omnivia_memory.<suffix>``) can
#: be checked from one entry.
BARREL_ALL_ORDER: dict[str, list[str]] = {
    "_shared": [
        "SENSITIVE_KEYS",
        "ValidationResult",
        "scan_sensitive_fields",
        "validate_iso_timestamp",
        "validate_optional_iso_timestamp",
    ],
    "app_shell_bridge": [
        "AppShellRuntimeState",
        "AppShellSource",
        "ValidationResult",
        "AppShellHostContext",
        "AppShellBodyDescriptor",
        "AppShellBridgeValidationError",
        "validate_app_shell_host_context",
        "validate_app_shell_body_descriptor",
    ],
    "lifecycle": ["LifecycleState", "LifecycleRules", "CreatedBy"],
    "provenance": ["Source", "SourceType"],
}

#: The barrels whose legacy source is a pure *absolute*-import re-export body,
#: checkable by ``_assert_pure_facade_module``. ``app_shell_bridge`` is
#: deliberately absent: its legacy barrel re-exports through the historical
#: ``from .models import ...`` / ``from .validation import ...`` relative form
#: and stays source-unchanged in this slice, so it gets its own stricter,
#: shape-exact gate (``test_app_shell_barrel_source_is_unchanged_relative_reexport``)
#: rather than a relaxed version of the shared one.
ABSOLUTE_IMPORT_BARRELS: tuple[str, ...] = ("_shared", "lifecycle", "provenance")

#: The exact, ordered relative re-export shape the unchanged legacy app-shell
#: barrel must still have: ``(relative module, imported names in source order)``.
APP_SHELL_BARREL_RELATIVE_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "models",
        (
            "AppShellRuntimeState",
            "AppShellSource",
            "ValidationResult",
            "AppShellHostContext",
            "AppShellBodyDescriptor",
        ),
    ),
    (
        "validation",
        (
            "AppShellBridgeValidationError",
            "validate_app_shell_host_context",
            "validate_app_shell_body_descriptor",
        ),
    ),
)

#: Independently declared expectation for the manifest the migration-test
#: suite now uses to exclude these seven leaves from its source-parity gates.
EXPECTED_FACADE_CANONICAL_TO_LEGACY: dict[str, str] = {
    "omnivia_core._shared.validation": "omnivia_memory._shared.validation",
    "omnivia_core.app_shell_bridge.models": "omnivia_memory.app_shell_bridge.models",
    "omnivia_core.app_shell_bridge.validation": "omnivia_memory.app_shell_bridge.validation",
    "omnivia_core.lifecycle.models": "omnivia_memory.lifecycle.models",
    "omnivia_core.lifecycle.rules": "omnivia_memory.lifecycle.rules",
    "omnivia_core.provenance.models": "omnivia_memory.provenance.models",
    "omnivia_core.memory.models": "omnivia_memory.memory.models",
}


def _leaf_symbol_cases() -> list[tuple[str, str, str]]:
    return [
        (legacy_module, symbol, canonical_module)
        for legacy_module, symbols in LEAF_SYMBOL_SOURCES.items()
        for symbol, canonical_module in symbols.items()
    ]


@pytest.mark.parametrize(
    "legacy_module,symbol,canonical_module",
    _leaf_symbol_cases(),
    ids=[f"{m}.{s}" for m, s, _ in _leaf_symbol_cases()],
)
def test_leaf_symbol_is_exact_canonical_object(
    legacy_module: str, symbol: str, canonical_module: str
) -> None:
    legacy = importlib.import_module(legacy_module)
    canonical = importlib.import_module(canonical_module)
    assert getattr(legacy, symbol) is getattr(canonical, symbol), (
        f"{legacy_module}.{symbol} is not the exact same object as "
        f"{canonical_module}.{symbol}"
    )


@pytest.mark.parametrize("barrel,expected_all", sorted(BARREL_ALL_ORDER.items()))
def test_barrel_all_is_exact_ordered_literal_in_both_trees(
    barrel: str, expected_all: list[str]
) -> None:
    legacy = importlib.import_module(f"omnivia_memory.{barrel}")
    canonical = importlib.import_module(f"omnivia_core.{barrel}")
    assert legacy.__all__ == expected_all
    assert canonical.__all__ == expected_all


@pytest.mark.parametrize("barrel,expected_all", sorted(BARREL_ALL_ORDER.items()))
def test_barrel_symbols_are_exact_canonical_objects(
    barrel: str, expected_all: list[str]
) -> None:
    legacy = importlib.import_module(f"omnivia_memory.{barrel}")
    canonical = importlib.import_module(f"omnivia_core.{barrel}")
    for name in expected_all:
        assert getattr(legacy, name) is getattr(canonical, name), (
            f"omnivia_memory.{barrel}.{name} is not the exact same object as "
            f"omnivia_core.{barrel}.{name}"
        )


@pytest.mark.parametrize("leaf_name", sorted(LEAF_SYMBOL_SOURCES))
def test_leaf_wrapper_has_no_all(leaf_name: str) -> None:
    module = importlib.import_module(leaf_name)
    assert not hasattr(module, "__all__"), (
        f"{leaf_name} must not define __all__ -- it never did before becoming a facade"
    )


def _module_body_after_docstring(module_name: str) -> list[ast.stmt]:
    module = importlib.import_module(module_name)
    path = getattr(module, "__file__", None)
    assert path is not None, f"{module_name} has no source file to inspect"
    tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=module_name)
    body = list(tree.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def _assert_pure_facade_module(module_name: str, *, allow_all: bool) -> None:
    """Every statement is a docstring (stripped above), a ``from __future__
    import ...``, a plain absolute ``from omnivia_(core|memory) import ...``
    with no wildcard and no renaming alias, or -- only where ``allow_all`` --
    a single literal ``__all__ = [...]`` assignment of string constants.
    Anything else (a def, a class, a bare ``import``, a conditional, an
    ``__getattr__``, a ``sys.modules`` write) fails the module.
    """
    for node in _module_body_after_docstring(module_name):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{module_name}: relative import is not allowed: {ast.dump(node)}"
            assert node.module is not None, f"{module_name}: bare relative import"
            root = node.module.split(".")[0]
            assert node.module == "__future__" or root in {"omnivia_core", "omnivia_memory"}, (
                f"{module_name}: unexpected import source {node.module!r}"
            )
            for alias in node.names:
                assert alias.name != "*", f"{module_name}: star import is not allowed"
                assert alias.asname is None, (
                    f"{module_name}: {alias.name!r} uses a rename/dynamic alias, not a plain import"
                )
            continue
        if allow_all and isinstance(node, ast.Assign):
            assert len(node.targets) == 1, f"{module_name}: unexpected multi-target assignment"
            target = node.targets[0]
            assert isinstance(target, ast.Name) and target.id == "__all__", (
                f"{module_name}: unexpected assignment target {ast.dump(target)}"
            )
            assert isinstance(node.value, ast.List), f"{module_name}: __all__ is not a literal list"
            for elt in node.value.elts:
                assert isinstance(elt, ast.Constant) and isinstance(elt.value, str), (
                    f"{module_name}: __all__ contains a non-literal-string element"
                )
            continue
        raise AssertionError(f"{module_name}: disallowed statement {ast.dump(node)}")


def _assert_leaf_is_exact_route_facade(leaf_name: str) -> None:
    """A converted leaf's entire body (after its docstring) must be exactly one
    ``from <canonical route> import (<exact expected name set>)`` statement:
    no defs, no class, no bare ``import``, no conditional, no ``__getattr__``
    or ``sys.modules`` write, no second import statement, and -- within that
    one statement -- no wildcard, no relative import, no rename/alias, no
    name outside the exact expected set for this leaf. This is what makes
    the facade a pure route rather than a proxy: every name it can produce
    is decided at import time, from exactly one source, by the interpreter's
    own import machinery.
    """
    body = _module_body_after_docstring(leaf_name)
    assert len(body) == 1, (
        f"{leaf_name}: expected exactly one statement (a single import), found "
        f"{len(body)}: {[ast.dump(node) for node in body]}"
    )
    (node,) = body
    assert isinstance(node, ast.ImportFrom), (
        f"{leaf_name}: expected a single `from ... import ...` statement, found "
        f"{ast.dump(node)}"
    )
    assert node.level == 0, f"{leaf_name}: relative import is not allowed"
    expected_source = LEAF_IMPORT_SOURCE[leaf_name]
    assert node.module == expected_source, (
        f"{leaf_name}: imports from {node.module!r}, expected exactly {expected_source!r}"
    )
    names: set[str] = set()
    for alias in node.names:
        assert alias.name != "*", f"{leaf_name}: star import is not allowed"
        assert alias.asname is None, (
            f"{leaf_name}: {alias.name!r} uses a rename/dynamic alias, not a plain import"
        )
        names.add(alias.name)
    expected_names = set(LEAF_SYMBOL_SOURCES[leaf_name])
    assert names == expected_names, (
        f"{leaf_name}: imports {sorted(names)} from {expected_source!r}, expected exactly "
        f"{sorted(expected_names)}"
    )


@pytest.mark.parametrize("leaf_name", sorted(LEAF_SYMBOL_SOURCES))
def test_leaf_wrapper_ast_is_pure_facade(leaf_name: str) -> None:
    _assert_leaf_is_exact_route_facade(leaf_name)


@pytest.mark.parametrize("barrel", sorted(f"omnivia_memory.{b}" for b in ABSOLUTE_IMPORT_BARRELS))
def test_barrel_ast_is_pure_facade(barrel: str) -> None:
    _assert_pure_facade_module(barrel, allow_all=True)


def test_absolute_import_barrels_cover_every_barrel_but_the_app_shell_exception() -> None:
    """``ABSOLUTE_IMPORT_BARRELS`` may hold out exactly one barrel -- the
    app-shell one, which keeps its historical relative re-export form and is
    covered by its own stricter gate below. The held-out set must not quietly
    grow to a second barrel."""
    held_out = set(BARREL_ALL_ORDER) - set(ABSOLUTE_IMPORT_BARRELS)
    assert held_out == {"app_shell_bridge"}, (
        "app_shell_bridge is the only barrel that may be held out of the shared "
        f"absolute-import AST gate; found {sorted(held_out)}"
    )


def test_app_shell_barrel_source_is_unchanged_relative_reexport() -> None:
    """The legacy app-shell barrel is *source-unchanged* by this slice: it
    becomes identity-preserving transitively, through its two converted leaves,
    not by being rewritten itself. Pin its exact historical shape -- two
    relative ``from .<leaf> import (...)`` statements in source order with
    their exact ordered name lists, then the ``__all__`` literal -- so a
    future edit that reroutes the barrel directly at ``omnivia_core``, adds a
    ``__getattr__``, or reorders its re-exports fails here.
    """
    body = _module_body_after_docstring("omnivia_memory.app_shell_bridge")
    assert len(body) == len(APP_SHELL_BARREL_RELATIVE_IMPORTS) + 1, (
        "omnivia_memory.app_shell_bridge: expected exactly "
        f"{len(APP_SHELL_BARREL_RELATIVE_IMPORTS)} relative imports plus __all__, found "
        f"{[ast.dump(node) for node in body]}"
    )
    for node, (module, names) in zip(body, APP_SHELL_BARREL_RELATIVE_IMPORTS, strict=False):
        assert isinstance(node, ast.ImportFrom), f"expected an import, found {ast.dump(node)}"
        assert node.level == 1, f"omnivia_memory.app_shell_bridge: {module} import is not relative"
        assert node.module == module
        assert tuple(alias.name for alias in node.names) == names
        for alias in node.names:
            assert alias.name != "*", "star import is not allowed"
            assert alias.asname is None, f"{alias.name!r} uses a rename/dynamic alias"

    all_node = body[-1]
    assert isinstance(all_node, ast.Assign), f"expected __all__, found {ast.dump(all_node)}"
    (target,) = all_node.targets
    assert isinstance(target, ast.Name) and target.id == "__all__"
    assert isinstance(all_node.value, ast.List)
    assert [
        elt.value for elt in all_node.value.elts if isinstance(elt, ast.Constant)
    ] == BARREL_ALL_ORDER["app_shell_bridge"]


def test_app_shell_validation_result_keeps_its_historical_collision_owner() -> None:
    """``ValidationResult`` is a name collision across five independent
    domains. The App Shell bridge's own dataclass is the one this leaf
    historically exposed, so routing it to the shared primitive (or to any
    other domain's same-named result type) would be a silent contract swap
    that every "is the exact canonical object" check above would still pass.
    Pin the owner, and pin that it is *not* the others.
    """
    legacy_leaf = importlib.import_module("omnivia_memory.app_shell_bridge.models")
    legacy_barrel = importlib.import_module("omnivia_memory.app_shell_bridge")
    canonical_leaf = importlib.import_module("omnivia_core.app_shell_bridge.models")

    assert legacy_leaf.ValidationResult is canonical_leaf.ValidationResult
    assert legacy_barrel.ValidationResult is canonical_leaf.ValidationResult

    for other_module in (
        "omnivia_core._shared.validation",
        "omnivia_memory._shared.validation",
        "omnivia_core.app_manifest.models",
        "omnivia_memory.app_manifest.models",
        "omnivia_core.component_contract.models",
        "omnivia_memory.component_contract.models",
        "omnivia_core.control_plane.models",
        "omnivia_memory.control_plane.models",
    ):
        other = importlib.import_module(other_module)
        assert legacy_leaf.ValidationResult is not other.ValidationResult, (
            "omnivia_memory.app_shell_bridge.models.ValidationResult must stay the App "
            f"Shell bridge's own dataclass, not {other_module}.ValidationResult"
        )


def test_facade_canonical_to_legacy_manifest_matches_expected_seven_pairs() -> None:
    """``FACADE_CANONICAL_TO_LEGACY`` (imported from the migration-test
    manifest) must be exactly these seven pairs -- neither manifest may drift
    (grow, shrink, or repoint) without this dedicated test noticing, since
    that shared constant is also what excludes these leaves from the
    canonical_migration source-parity gates."""
    assert FACADE_CANONICAL_TO_LEGACY == EXPECTED_FACADE_CANONICAL_TO_LEGACY
    assert set(LEAF_SYMBOL_SOURCES) == set(EXPECTED_FACADE_CANONICAL_TO_LEGACY.values())


def _run_isolated(script: str) -> None:
    result = subprocess.run(
        [PYTHON, "-I", "-S", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"isolated subprocess failed (exit {result.returncode}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_canonical_core_imports_independently_of_omnivia_memory() -> None:
    """Every canonical owner behind this slice's facades must import cleanly
    with only ``src`` on ``sys.path`` -- ``services/omnivia-memory/src`` is
    never added, so an accidental ``import omnivia_memory`` anywhere in the
    canonical chain would surface as a hard failure here, not a silent pass."""
    canonical_modules = sorted(EXPECTED_FACADE_CANONICAL_TO_LEGACY)
    script = "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {str(CORE_SRC)!r})",
            *(f"import {module}" for module in canonical_modules),
            "assert 'omnivia_memory' not in sys.modules",
        ]
    )
    _run_isolated(script)


def _fresh_process_identity_script(*, canonical_first: bool) -> str:
    canonical_modules = sorted(EXPECTED_FACADE_CANONICAL_TO_LEGACY)
    legacy_modules = sorted(LEAF_SYMBOL_SOURCES)
    first, second = (
        (canonical_modules, legacy_modules) if canonical_first else (legacy_modules, canonical_modules)
    )
    lines = [
        "import sys",
        f"sys.path.insert(0, {str(MEMORY_SRC)!r})",
        f"sys.path.insert(0, {str(CORE_SRC)!r})",
        *(f"import {module}" for module in first),
        *(f"import {module}" for module in second),
    ]
    for legacy_module, symbols in LEAF_SYMBOL_SOURCES.items():
        for symbol, canonical_module in symbols.items():
            lines.append(
                f"assert {legacy_module}.{symbol} is {canonical_module}.{symbol}, "
                f"'{legacy_module}.{symbol} is not {canonical_module}.{symbol} "
                f"(canonical_first={canonical_first})'"
            )
    # The colliding ``ValidationResult`` owners must stay distinct in a fresh
    # process too: every positive identity assertion above would still pass if
    # the two routes had collapsed onto one object.
    lines.append(
        "assert omnivia_memory.app_shell_bridge.models.ValidationResult "
        "is not omnivia_core._shared.validation.ValidationResult, "
        "'the app-shell and shared ValidationResult owners collapsed into one object "
        f"(canonical_first={canonical_first})'"
    )
    return "\n".join(lines)


@pytest.mark.parametrize(
    "canonical_first", [True, False], ids=["canonical-first", "facade-first"]
)
def test_fresh_process_import_order_preserves_identity(canonical_first: bool) -> None:
    _run_isolated(_fresh_process_identity_script(canonical_first=canonical_first))
