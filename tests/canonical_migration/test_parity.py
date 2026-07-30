"""Canonical Slice A leaves must match their legacy contract, symbol-for-symbol.

Four layers of comparison run here, from loosest to strictest:

- ``baseline.inventory.describe_symbol`` -- the same introspection the frozen
  Phase 0 export baseline uses -- catches a symbol moving, disappearing, or
  changing shape (enum members, dataclass field *names*, frozen-ness).
- ``_strict`` (this package) -- exact source-level fidelity on top of that:
  dataclass field *types* in declaration order (so ``typing.List[str]`` and
  ``list[str]`` compare as different), generated ``__init__``/``__eq__``/
  ``__hash__``/``__repr__``/``__post_init__`` signatures, dataclass
  init/repr/eq/order/frozen/kw-only params, module-level constants whose
  runtime ``__module__`` does not resolve back to the defining module
  (builtin ``frozenset`` instances, ``types.UnionType`` aliases), alias
  identity (``IngestSource is Source``), and ``__all__`` presence/content.
- *Namespace* parity -- every non-underscore, non-module name bound at module
  scope, whether defined there or merely imported into it. The two layers
  above filter to symbols *defined* in the module, so they are blind to a
  port that swaps an import (``from datetime import UTC`` for ``timezone``)
  or hoists a deliberately function-local import to module scope. This layer
  is not.
- *Normalized AST* parity -- ``ast.dump`` of the canonical module against
  ``ast.dump`` of the legacy module with only its ``omnivia_memory`` package
  references rewritten to ``omnivia_core`` (and, for any leaf listed in
  ``SANCTIONED_IMPORT_REWRITES``, that leaf's named, exact import rewrites
  applied first -- no leaf needs one at present). Nothing else is normalized,
  so this pins the whole
  module: function and method *bodies*, constant values, enum member aliases,
  ``field(default_factory=...)`` expressions, the delegation each
  ``to_dict``/``from_dict`` performs, docstring text, and import placement
  (module-scope vs. function-local). Comments and formatting are the only
  things it cannot see, which is what makes a ``# type: ignore[...]`` needed
  by this repo's stricter mypy run the one legitimate way to differ from the
  legacy source.

  A sanctioned import rewrite is narrower than the package rename: it is an
  exact, named, per-leaf source substitution (an owner-routed split of one
  legacy import into its canonical owner leaves), not a fuzzy normalization.
  ``SANCTIONED_IMPORT_REWRITES`` enumerates each rule by the exact legacy
  module it applies to and the exact old/new source fragments, and every
  rule must match its leaf's legacy source exactly once
  (``test_sanctioned_import_rewrites_apply_to_a_known_leaf_exactly_once``
  rejects a stale or unused rule -- which is what emptied the map: every leaf
  that ever needed a rule has since become a facade).

A subset of once-duplicated leaves (``_shared.validation``,
``app_manifest.models``, ``app_manifest.validation``,
``app_shell_bridge.models``, ``app_shell_bridge.validation``,
``component_contract.models``, ``component_contract.validation``,
``control_plane.imports``, ``control_plane.models``,
``control_plane.validation``, ``knowledge.models``, ``knowledge.normalize``,
``knowledge.validation``, ``lifecycle.models``, ``lifecycle.rules``,
``module_manifest.models``, ``module_manifest.validation``,
``provenance.models``, ``memory.models``, ``run_ledger.models``, and
``run_ledger.validation``) has since been converted
into thin compatibility facades:
the legacy leaf now imports its supported symbols directly from the canonical
owner (``legacy.Foo is canonical.Foo``) instead of holding a duplicated,
source-parity copy. Those leaves are no longer source-identical to their
legacy counterpart by design, so they are held out of
``CANONICAL_TO_LEGACY`` and the parametrized gates below -- listed instead in
``FACADE_CANONICAL_TO_LEGACY`` -- and verified for exact symbol identity, not
source sameness, by ``tests/compatibility/test_facade_foundation.py``.

Neither the structural nor the AST layer normalizes annotation spellings
away: a canonical leaf that "modernizes" a legacy ``typing.List``/
``typing.Optional`` annotation, or a legacy ``datetime.now(timezone.utc)``
call, fails here -- by design.

``omnivia_core.graph.search_models`` is the one deliberate exception to the
AST gate, and it gets a purpose-built version of it rather than a waiver:
see ``test_graph_search_models_canonicalizes_only_query_and_result_records``.
"""

from __future__ import annotations

import ast
import difflib
import importlib
from pathlib import Path
from types import ModuleType
from typing import Any

import _strict
import pytest
from _leaves import (
    CANONICAL_LEAF_MODULES,
    CANONICAL_TO_LEGACY,
    FACADE_CANONICAL_TO_LEGACY,
    SEARCH_MODELS_EXPECTED_MISSING_FROM_CANONICAL,
)

from baseline.inventory import describe_symbol as loose_describe_symbol

LEGACY_PACKAGE = "omnivia_memory"
CANONICAL_PACKAGE = "omnivia_core"

#: Sanctioned, exact, per-leaf import rewrites applied to a leaf's legacy
#: source -- before the generic ``omnivia_memory`` -> ``omnivia_core`` rename
#: below -- so the AST comparison stays an equality check rather than a
#: fuzzy one. Keyed by the exact ``(canonical, legacy)`` module pair; each
#: rule is an (old, new) source-fragment pair.
#:
#: No leaf needs a rule at present, and the map is deliberately kept (rather than
#: deleted) because the mechanism -- and the staleness gate over it -- still has
#: to exist for the batches still to come.
#:
#: ``run_ledger.validation`` and ``control_plane.validation`` each used to carry
#: one: the former split a combined legacy knowledge-barrel import across the
#: canonical shared-validation and knowledge-validation owner leaves, the latter
#: rerouted its compatibility helper from the legacy knowledge barrel directly to
#: the canonical knowledge-validation owner leaf. Both legacy leaves are now
#: compatibility facades, so they have left ``CANONICAL_TO_LEGACY`` and there is
#: no legacy source left to rewrite: identity, not source sameness, is what
#: ``tests/compatibility/test_facade_foundation.py`` now proves for them. Keeping
#: either rule would fail
#: ``test_sanctioned_import_rewrites_apply_to_a_known_leaf_exactly_once`` as
#: stale, which is exactly that gate working.
SANCTIONED_IMPORT_REWRITES: dict[
    tuple[str, str], tuple[tuple[str, str], ...]
] = {}


def _public_definitions(module: ModuleType, *, strict: bool) -> dict[str, Any]:
    """Public symbols *defined* in ``module`` (not merely imported into it)."""

    describe = _strict.describe_symbol if strict else loose_describe_symbol
    definitions: dict[str, Any] = {}
    for name, value in vars(module).items():
        if name.startswith("_") or isinstance(value, ModuleType):
            continue
        if getattr(value, "__module__", None) != module.__name__:
            continue
        definitions[name] = describe(value)
    return definitions


def _public_namespace(module: ModuleType) -> set[str]:
    """Every non-underscore, non-module name bound at ``module`` scope.

    Unlike ``_public_definitions`` this deliberately does *not* filter on
    ``__module__``, so imported names count too. That is the point: a port
    that swaps ``from datetime import timezone`` for ``from datetime import
    UTC``, or that hoists a function-local ``LifecycleRules`` import up to
    module scope, changes nothing ``_public_definitions`` can see but does
    change what the module publishes to ``from ... import *`` and to any
    caller reaching through the module object.

    Submodules are excluded because a package's own submodules get bound on
    the parent as an import side effect, which says nothing about the port.
    """
    return {
        name
        for name, value in vars(module).items()
        if not name.startswith("_") and not isinstance(value, ModuleType)
    }


def _module_source(module: ModuleType) -> str:
    path = getattr(module, "__file__", None)
    assert path is not None, f"{module.__name__} has no source file to compare"
    return Path(path).read_text(encoding="utf-8")


def _canonicalize_legacy_source(
    source: str, *, canonical_name: str = "", legacy_name: str = ""
) -> str:
    """Rewrite *only* the legacy package name (and, for a leaf with a
    sanctioned import rewrite, that leaf's single named split), leaving
    everything else alone.

    Slice A's default sanctioned edit to a ported module is retargeting
    internal imports from ``omnivia_memory`` to ``omnivia_core``. A leaf
    listed in ``SANCTIONED_IMPORT_REWRITES`` additionally gets its one named,
    exact, owner-routed import split applied first -- never a fuzzy
    normalization. Rewriting exactly these things (and nothing else) before
    parsing is what lets the AST comparison below stay an equality check.
    """
    for old, new in SANCTIONED_IMPORT_REWRITES.get(
        (canonical_name, legacy_name), ()
    ):
        occurrences = source.count(old)
        assert occurrences == 1, (
            f"sanctioned import rewrite for {legacy_name} expects its old "
            f"fragment to occur exactly once in the legacy source, found "
            f"{occurrences}; update the rule or investigate the leaf drift"
        )
        source = source.replace(old, new, 1)
    return source.replace(LEGACY_PACKAGE, CANONICAL_PACKAGE)


def _dump(tree: ast.AST) -> str:
    return ast.dump(tree, indent=2)


def _ast_mismatch_message(canonical_name: str, legacy_name: str, expected: str, actual: str) -> str:
    diff = difflib.unified_diff(
        expected.splitlines(),
        actual.splitlines(),
        fromfile=f"{legacy_name} (package name canonicalized)",
        tofile=canonical_name,
        lineterm="",
        n=3,
    )
    return (
        f"{canonical_name} is not an exact port of {legacy_name}.\n"
        "Only the omnivia_memory -> omnivia_core package rename, this leaf's "
        "sanctioned owner-route import rewrite (if any, see "
        "SANCTIONED_IMPORT_REWRITES), comments and formatting may differ; "
        "every other AST node must match.\n" + "\n".join(list(diff)[:200])
    )


def _extra_constants(module: ModuleType, module_label: str, canonical_name: str) -> dict[str, Any]:
    """Constants listed in ``_strict.EXTRA_MODULE_CONSTANTS`` for this leaf.

    These have a runtime ``__module__`` that does not resolve back to the
    defining module (builtin ``frozenset`` instances, ``types.UnionType``
    aliases), so ``_public_definitions``'s module-identity filter cannot see
    them; they are enumerated explicitly instead. ``EXTRA_MODULE_CONSTANTS``
    is keyed by the canonical module name only, so both the canonical and
    legacy module are looked up under ``canonical_name``.
    """
    extras: dict[str, Any] = {}
    for name in _strict.EXTRA_MODULE_CONSTANTS.get(canonical_name, ()):
        assert hasattr(module, name), f"{module_label} is missing expected constant {name!r}"
        extras[name] = _strict.describe_symbol(getattr(module, name))
    return extras


def _assert_leaf_matches(canonical_name: str, legacy_name: str) -> None:
    canonical = importlib.import_module(canonical_name)
    legacy = importlib.import_module(legacy_name)

    canonical_namespace = _public_namespace(canonical)
    legacy_namespace = _public_namespace(legacy)
    assert canonical_namespace == legacy_namespace, (
        f"{canonical_name} module namespace drifted from {legacy_name}:\n"
        f"only in canonical: {sorted(canonical_namespace - legacy_namespace)}\n"
        f"only in legacy: {sorted(legacy_namespace - canonical_namespace)}"
    )

    canonical_defs = _public_definitions(canonical, strict=True)
    legacy_defs = _public_definitions(legacy, strict=True)

    assert set(canonical_defs) == set(legacy_defs), (
        f"{canonical_name} public definitions {sorted(canonical_defs)} do not match "
        f"{legacy_name} {sorted(legacy_defs)}"
    )
    for name in canonical_defs:
        assert canonical_defs[name] == legacy_defs[name], (
            f"{canonical_name}.{name} contract drifted from {legacy_name}.{name}:\n"
            f"canonical: {canonical_defs[name]}\nlegacy: {legacy_defs[name]}"
        )

    canonical_extras = _extra_constants(canonical, canonical_name, canonical_name)
    legacy_extras = _extra_constants(legacy, legacy_name, canonical_name)
    assert canonical_extras == legacy_extras, (
        f"{canonical_name} extra module constants drifted from {legacy_name}:\n"
        f"canonical: {canonical_extras}\nlegacy: {legacy_extras}"
    )

    canonical_annotations = _strict.module_annotations(canonical)
    legacy_annotations = _strict.module_annotations(legacy)
    assert canonical_annotations == legacy_annotations, (
        f"{canonical_name} module-level annotations {canonical_annotations} do not match "
        f"{legacy_name} {legacy_annotations}"
    )

    canonical_all = getattr(canonical, "__all__", None)
    legacy_all = getattr(legacy, "__all__", None)
    assert canonical_all == legacy_all, (
        f"{canonical_name}.__all__ ({canonical_all!r}) does not match {legacy_name}.__all__ ({legacy_all!r})"
    )


@pytest.mark.parametrize(
    "canonical_name,legacy_name", sorted(CANONICAL_TO_LEGACY.items()), ids=lambda v: v
)
def test_canonical_leaf_matches_legacy_contract(canonical_name: str, legacy_name: str) -> None:
    _assert_leaf_matches(canonical_name, legacy_name)


@pytest.mark.parametrize(
    "canonical_name,legacy_name", sorted(CANONICAL_TO_LEGACY.items()), ids=lambda v: v
)
def test_canonical_leaf_is_an_exact_ast_port(canonical_name: str, legacy_name: str) -> None:
    """Every mapped leaf must be its legacy module, package rename aside.

    The structural layers above compare the *shape* of public symbols. They
    cannot see a body that was rewritten, a default that was recomputed a
    different way, an enum alias that was dropped, or an import that moved
    scope -- all of which are behavior. This compares the parsed module
    outright, so those changes cannot land unnoticed.
    """
    canonical_source = _module_source(importlib.import_module(canonical_name))
    legacy_source = _canonicalize_legacy_source(
        _module_source(importlib.import_module(legacy_name)),
        canonical_name=canonical_name,
        legacy_name=legacy_name,
    )

    canonical_tree = _dump(ast.parse(canonical_source, filename=canonical_name))
    legacy_tree = _dump(ast.parse(legacy_source, filename=legacy_name))

    assert canonical_tree == legacy_tree, _ast_mismatch_message(
        canonical_name, legacy_name, legacy_tree, canonical_tree
    )


def test_ast_gate_covers_every_leaf_but_the_one_documented_exception() -> None:
    """The gates above are parametrized over ``CANONICAL_TO_LEGACY``, so a
    leaf added to that manifest is covered automatically. Assert here that
    the manifests leave nothing else uncovered: every leaf is either a
    copied-leaf pair in ``CANONICAL_TO_LEGACY`` (covered by the generic AST
    gate above) or a facade-leaf pair in ``FACADE_CANONICAL_TO_LEGACY``
    (covered by tests/compatibility/test_facade_foundation.py instead), with
    ``search_models`` as the sole documented exception -- held out of both
    maps and covered by its own stricter test rather than waived -- so the
    exception cannot quietly grow to a second module.
    """
    # ``omnivia_core._shared`` is a re-export barrel with no definitions of
    # its own; it is covered by test_shared_barrel_matches_legacy_all_and_bindings.
    held_out = {
        name
        for name in CANONICAL_LEAF_MODULES
        if name != "omnivia_core._shared"
        and name not in CANONICAL_TO_LEGACY
        and name not in FACADE_CANONICAL_TO_LEGACY
    }
    assert held_out == {"omnivia_core.graph.search_models"}, (
        "graph.search_models is the only leaf that may be held out of both the "
        f"copied-leaf and facade-leaf maps; found {sorted(held_out)}"
    )


def test_sanctioned_import_rewrites_apply_to_a_known_leaf_exactly_once() -> None:
    """Every configured rewrite rule must target a currently-mapped legacy
    leaf, and its old fragment must occur in that leaf's live source exactly
    once -- guards against a rule surviving after its leaf is dropped from
    ``CANONICAL_TO_LEGACY`` or after the leaf's source drifts out from under
    it (rewritten, reworded, or removed)."""
    mapped_legacy_names = set(CANONICAL_TO_LEGACY.values())
    for (canonical_name, legacy_name), rules in SANCTIONED_IMPORT_REWRITES.items():
        assert legacy_name in mapped_legacy_names, (
            f"stale sanctioned import rewrite: {legacy_name} is not (or no longer) "
            "a mapped legacy leaf in CANONICAL_TO_LEGACY"
        )
        assert CANONICAL_TO_LEGACY.get(canonical_name) == legacy_name, (
            f"stale sanctioned import rewrite: {canonical_name} does not map "
            f"exactly to {legacy_name}"
        )
        assert rules, f"sanctioned import rewrite for {legacy_name} has no rules configured"
        source = _module_source(importlib.import_module(legacy_name))
        for old, _new in rules:
            occurrences = source.count(old)
            assert occurrences == 1, (
                f"sanctioned import rewrite old fragment for {legacy_name} must occur "
                f"exactly once in the legacy source, found {occurrences}"
            )


@pytest.mark.parametrize(
    "module_name,alias_name,target_name", _strict.ALIAS_IDENTITY, ids=lambda v: v
)
def test_alias_identity(module_name: str, alias_name: str, target_name: str) -> None:
    """A ported alias must be the *same object* as its target, in both trees.

    Guards against a canonical port that redefines the alias as a
    structurally-equal lookalike instead of assigning it -- something the
    structural comparisons above cannot detect, since an equal-but-distinct
    object would still pass them.
    """
    module = importlib.import_module(module_name)
    alias = getattr(module, alias_name)
    target = getattr(module, target_name)
    assert alias is target, (
        f"{module_name}.{alias_name} must be the exact same object as "
        f"{module_name}.{target_name} (identity alias), not a lookalike"
    )

    legacy_module_name = module_name.replace(CANONICAL_PACKAGE, LEGACY_PACKAGE, 1)
    legacy_module = importlib.import_module(legacy_module_name)
    legacy_alias = getattr(legacy_module, alias_name)
    legacy_target = getattr(legacy_module, target_name)
    assert legacy_alias is legacy_target, (
        f"{legacy_module_name}.{alias_name} is not an identity alias for "
        f"{legacy_module_name}.{target_name} -- ALIAS_IDENTITY expectation is stale"
    )


def _search_models_ast_without_scoring_helpers(source: str) -> tuple[str, str]:
    """Split ``search_models`` source into (module docstring, body dump).

    The scoring helpers are removed from the *legacy* body so the remaining
    statements can be compared as an exact port. The module docstring is
    returned separately because the canonical module deliberately extends
    it with a paragraph documenting that exclusion -- the only prose Slice A
    is allowed to add anywhere, and checked on its own terms below.
    """
    module = ast.parse(source)
    docstring = ast.get_docstring(module, clean=False) or ""
    body = [
        node
        for node in module.body
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in SEARCH_MODELS_EXPECTED_MISSING_FROM_CANONICAL
        )
    ]
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return docstring, _dump(ast.Module(body=body, type_ignores=[]))


def test_graph_search_models_canonicalizes_only_query_and_result_records() -> None:
    """omnivia_core.graph.search_models == legacy search_models minus the
    relevance-scoring helpers, which stay runtime-owned (see _leaves.py).

    This is the one leaf held out of the generic AST gate, so it carries a
    purpose-built equivalent: the legacy scoring helpers are removed from the
    legacy tree, the module docstring is compared separately (the canonical
    module extends it to document the exclusion), and everything that
    remains must still be an exact AST port.
    """

    canonical = importlib.import_module("omnivia_core.graph.search_models")
    legacy = importlib.import_module("omnivia_memory.graph.search_models")

    canonical_namespace = _public_namespace(canonical)
    legacy_namespace = _public_namespace(legacy) - SEARCH_MODELS_EXPECTED_MISSING_FROM_CANONICAL
    assert canonical_namespace == legacy_namespace, (
        "omnivia_core.graph.search_models namespace drifted from legacy minus the "
        f"scoring helpers:\nonly in canonical: {sorted(canonical_namespace - legacy_namespace)}\n"
        f"only in legacy: {sorted(legacy_namespace - canonical_namespace)}"
    )

    canonical_defs = _public_definitions(canonical, strict=True)
    legacy_defs = _public_definitions(legacy, strict=True)
    for missing in SEARCH_MODELS_EXPECTED_MISSING_FROM_CANONICAL:
        legacy_defs.pop(missing, None)

    assert set(canonical_defs) == set(legacy_defs), (
        f"omnivia_core.graph.search_models public definitions {sorted(canonical_defs)} do not "
        f"match expected {sorted(legacy_defs)} (legacy search_models minus "
        f"{sorted(SEARCH_MODELS_EXPECTED_MISSING_FROM_CANONICAL)})"
    )
    for name in canonical_defs:
        assert canonical_defs[name] == legacy_defs[name], (
            f"omnivia_core.graph.search_models.{name} contract drifted:\n"
            f"canonical: {canonical_defs[name]}\nlegacy: {legacy_defs[name]}"
        )

    canonical_doc, canonical_body = _search_models_ast_without_scoring_helpers(
        _module_source(canonical)
    )
    legacy_doc, legacy_body = _search_models_ast_without_scoring_helpers(
        _canonicalize_legacy_source(_module_source(legacy))
    )
    assert canonical_body == legacy_body, _ast_mismatch_message(
        "omnivia_core.graph.search_models",
        "omnivia_memory.graph.search_models (minus scoring helpers)",
        legacy_body,
        canonical_body,
    )

    # The only sanctioned prose divergence: the canonical docstring keeps the
    # legacy text verbatim and appends a note naming every excluded helper.
    assert canonical_doc.startswith(legacy_doc.rstrip() + "\n"), (
        "omnivia_core.graph.search_models must keep the legacy module docstring "
        "verbatim and only append to it"
    )
    for banned in SEARCH_MODELS_EXPECTED_MISSING_FROM_CANONICAL:
        assert banned in canonical_doc, (
            f"the canonical search_models docstring must name {banned!r} as "
            "deliberately left runtime-owned"
        )

    # The scoring helpers are runtime-owned and must never enter Core.
    for banned in SEARCH_MODELS_EXPECTED_MISSING_FROM_CANONICAL:
        assert not hasattr(canonical, banned), (
            f"omnivia_core.graph.search_models must not define {banned!r}; "
            "relevance scoring stays runtime-owned"
        )


def test_shared_barrel_matches_legacy_all_and_bindings() -> None:
    """omnivia_core._shared re-exports the same names as omnivia_memory._shared,
    bound to the exact same canonical objects.

    ``omnivia_memory._shared.validation`` is now a compatibility facade over
    ``omnivia_core._shared.validation`` (see ``FACADE_CANONICAL_TO_LEGACY``),
    so both barrels re-export the identical underlying symbols -- this is an
    object-identity check, not a structural lookalike comparison.
    """

    canonical = importlib.import_module("omnivia_core._shared")
    legacy = importlib.import_module("omnivia_memory._shared")

    assert sorted(canonical.__all__) == sorted(legacy.__all__)
    for name in canonical.__all__:
        canonical_value = getattr(canonical, name)
        legacy_value = getattr(legacy, name)
        assert canonical_value is legacy_value, (
            f"omnivia_core._shared.{name} is not the exact same object as "
            f"omnivia_memory._shared.{name}"
        )
