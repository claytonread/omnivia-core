"""Dependency and transitional-import drift check for Core.

Two kinds of drift matter before the migration:

1. **Outward drift.** Core must stay public-safe and free of private Platform or
   Dev implementation dependencies. Every third-party import is therefore
   matched against a narrow allowlist with a stated reason, and a small set of
   module prefixes is banned outright so the failure message says *why*.

2. **Inward drift.** Core still ships transitional runtime code (persistence,
   ingestion, search, workspace, memory, graph). The contract layer importing
   the runtime layer is exactly what the migration has to unpick, so each such
   edge is enumerated with the symbols it pulls and the reason it exists. A new
   edge fails the check until someone declares it deliberately.

Imports are read with :mod:`ast` rather than by importing modules, so the check
never executes Core code and cannot be fooled by runtime patching.
"""

from __future__ import annotations

import ast
import copy
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from baseline import (
    BASELINE_FORMAT_VERSION,
    BASELINE_TASK_ID,
    CORE_PACKAGE,
    CORE_SRC,
    INVENTORY_DIR,
    REPO_ROOT,
)
from baseline.determinism import (
    diff_json,
    format_differences,
    load_json,
    write_artifact,
)

DEPENDENCIES_PATH = INVENTORY_DIR / "dependencies.json"
CORE_PYPROJECT = REPO_ROOT / "services" / "omnivia-memory" / "pyproject.toml"

#: Third-party modules Core may import, each with the reason it is tolerated.
#: Empty entries are not allowed: an allowlist without a reason is not a review.
ALLOWED_THIRD_PARTY: dict[str, str] = {
    "fitz": (
        "PyMuPDF, imported lazily inside PDFExtractor.extract and degraded to a "
        "failure result when absent. Not a declared dependency."
    ),
    "docx": (
        "python-docx, imported lazily inside DOCXExtractor.extract and degraded to a "
        "failure result when absent. Not a declared dependency."
    ),
}

#: Module prefixes Core must never import, with the boundary each one breaks.
BANNED_IMPORT_PREFIXES: dict[str, str] = {
    "omnivia_platform": "Core must not depend on private Platform implementation code.",
    "omnivia_dev": "Core must not depend on private Dev implementation code.",
    "omnivia_cloud": "Core must not depend on private Cloud implementation code.",
    "mcp": "Core does not own MCP serving; that surface belongs to Dev.",
    "fastapi": "Core does not own an HTTP runtime; that surface belongs to Platform.",
    "starlette": "Core does not own an HTTP runtime; that surface belongs to Platform.",
    "uvicorn": "Core does not own an HTTP runtime; that surface belongs to Platform.",
    "click": "Core does not own a product CLI; that surface belongs to Dev.",
    "typer": "Core does not own a product CLI; that surface belongs to Dev.",
}

#: Modules that carry transitional runtime behaviour. Everything else in the
#: package is contract-layer. Matching is on dotted-segment boundaries so
#: ``omnivia_memory.memory_graph`` is not swallowed by ``omnivia_memory.memory``.
RUNTIME_MODULE_PREFIXES: tuple[str, ...] = (
    f"{CORE_PACKAGE}.control_plane.registry",
    f"{CORE_PACKAGE}.graph",
    f"{CORE_PACKAGE}.ingestion",
    f"{CORE_PACKAGE}.memory",
    f"{CORE_PACKAGE}.memory_graph.ingestion_adapter",
    f"{CORE_PACKAGE}.memory_graph.store",
    f"{CORE_PACKAGE}.persistence",
    f"{CORE_PACKAGE}.search",
    f"{CORE_PACKAGE}.workspace",
)

#: ``omnivia_core`` is not a generic third-party import: it is the canonical
#: package the compatibility-facade leaves now route to (see
#: ``tests/canonical_migration/_leaves.py``'s ``FACADE_CANONICAL_TO_LEGACY`` and
#: ``baseline.inventory.FACADE_ROUTES``). It is verified narrowly and exactly by
#: ``_facade_dependency_problems`` below rather than added to
#: ``ALLOWED_THIRD_PARTY``, which is reserved for genuine third-party tolerances.
FACADE_COMPATIBILITY_PACKAGE = "omnivia_core"

#: The exact dependency range ``services/omnivia-memory/pyproject.toml`` must
#: declare for the compatibility distribution to depend on Core.
FACADE_COMPATIBILITY_DEPENDENCY = "omnivia-core>=0.1.0,<0.2.0"

#: The exact, sorted set of legacy modules that import ``omnivia_core``. A
#: module added to or removed from this set without a matching update here must
#: fail, not silently pass through a widened importer allowance.
#:
#: Every entry but one is a converted facade *leaf*. The exception is the package
#: root itself: the converted compatibility root imports each of its 182
#: advertised names, and ``__version__``, straight from the approved canonical
#: owner or barrel that publishes it, which makes it an exact ``omnivia_core``
#: importer like any leaf.
#: It is the only barrel-or-root entry, and deliberately named rather than
#: matched by a prefix.
FACADE_COMPATIBILITY_IMPORTERS: tuple[str, ...] = tuple(
    sorted(
        (
            CORE_PACKAGE,
            f"{CORE_PACKAGE}._shared.validation",
            f"{CORE_PACKAGE}.app_manifest.models",
            f"{CORE_PACKAGE}.app_manifest.validation",
            f"{CORE_PACKAGE}.app_shell_bridge.models",
            f"{CORE_PACKAGE}.app_shell_bridge.validation",
            f"{CORE_PACKAGE}.component_contract.models",
            f"{CORE_PACKAGE}.component_contract.validation",
            f"{CORE_PACKAGE}.control_plane.imports",
            f"{CORE_PACKAGE}.control_plane.models",
            f"{CORE_PACKAGE}.control_plane.validation",
            f"{CORE_PACKAGE}.graph.models",
            f"{CORE_PACKAGE}.graph.search_models",
            f"{CORE_PACKAGE}.ingestion.models",
            f"{CORE_PACKAGE}.ingestion.watcher.models",
            f"{CORE_PACKAGE}.knowledge.models",
            f"{CORE_PACKAGE}.knowledge.normalize",
            f"{CORE_PACKAGE}.knowledge.validation",
            f"{CORE_PACKAGE}.lifecycle.models",
            f"{CORE_PACKAGE}.lifecycle.rules",
            f"{CORE_PACKAGE}.memory.models",
            f"{CORE_PACKAGE}.memory_graph.assembly",
            f"{CORE_PACKAGE}.memory_graph.fixtures",
            f"{CORE_PACKAGE}.memory_graph.models",
            f"{CORE_PACKAGE}.memory_graph.validation",
            f"{CORE_PACKAGE}.module_manifest.models",
            f"{CORE_PACKAGE}.module_manifest.validation",
            f"{CORE_PACKAGE}.provenance.models",
            f"{CORE_PACKAGE}.run_ledger.models",
            f"{CORE_PACKAGE}.run_ledger.validation",
            f"{CORE_PACKAGE}.workspace.models",
        )
    )
)

#: The canonical package this scan does *not* otherwise walk. Used only by
#: ``_facade_stdlib_import_problems`` below, to prove a declared stdlib-import
#: move really landed on the paired canonical module.
FACADE_CANONICAL_SRC = REPO_ROOT / "src"

#: Standard-library top-level modules the frozen Phase 0 baseline recorded for the
#: legacy tree whose *only* importer in that tree was a leaf that has since become
#: a compatibility facade. Keys are the stdlib module; values are the exact
#: converted legacy leaf whose canonical counterpart performs that import now.
#:
#: This is not a tolerance for a lost dependency. The import did not disappear: it
#: moved across the package boundary with the implementation, and this scan only
#: walks the legacy tree. ``unicodedata`` is the first and only such module --
#: ``omnivia_memory.knowledge.normalize`` was the single place the legacy tree
#: imported it, and ``omnivia_core.knowledge.normalize`` imports it now.
#: Every other converted leaf's stdlib imports (``hashlib``, ``json``, ``re``,
#: ``datetime`` and the rest) are still imported by unconverted legacy modules, so
#: no other entry exists or is needed.
#:
#: Each entry is proved by ``_facade_stdlib_import_problems`` -- the named legacy
#: leaf must be a declared facade importer, the module must really be absent from
#: the live legacy tree, it must really have been in the frozen baseline, and the
#: paired canonical module must really import it -- before it may be normalized
#: away. There is no general "stdlib imports may shrink" allowance: an
#: undeclared removal still fails the inventory diff.
FACADE_STDLIB_IMPORT_MOVES: dict[str, str] = {
    "unicodedata": f"{CORE_PACKAGE}.knowledge.normalize",
}

#: The compatibility root's runtime edges *after* conversion: the exact modules it
#: may still reach into the legacy runtime for, and the exact symbols it may take
#: from each. This is the whole of the root's legacy-owned half.
FACADE_ROOT_RUNTIME_BINDINGS: dict[str, tuple[str, ...]] = {
    f"{CORE_PACKAGE}.memory.service": ("MemoryService",),
    f"{CORE_PACKAGE}.persistence": ("Database",),
}

#: The same edges as the *frozen* Phase 0 baseline recorded them, before the root
#: was converted. Restated here rather than read out of the artifact, so the
#: normalization below replaces one exactly-known state with another exactly-known
#: state instead of overwriting whatever it happens to find.
FACADE_ROOT_FROZEN_RUNTIME_BINDINGS: dict[str, tuple[str, ...]] = {
    f"{CORE_PACKAGE}.memory": ("MemoryCreate", "MemoryService", "MemoryUpdate"),
    f"{CORE_PACKAGE}.persistence": ("Database",),
}

#: The root bindings that left the legacy runtime edge above for a canonical
#: owner, and the canonical module each one must now come from. These two are the
#: entire difference between the frozen and converted edge sets: the root used to
#: take them from ``omnivia_memory.memory`` alongside ``MemoryService``, and now
#: takes them from Core, which is why the frozen edge cannot simply be kept.
FACADE_ROOT_CANONICALIZED_BINDINGS: dict[str, str] = {
    "MemoryCreate": f"{FACADE_COMPATIBILITY_PACKAGE}.memory.models",
    "MemoryUpdate": f"{FACADE_COMPATIBILITY_PACKAGE}.memory.models",
}

#: Why each contract -> runtime edge is tolerated for now. An observed edge with
#: no entry here fails the check; an entry here with no observed edge is stale.
TRANSITIONAL_IMPORT_REASONS: dict[tuple[str, str], str] = {
    (CORE_PACKAGE, f"{CORE_PACKAGE}.memory.service"): (
        "Documented compatibility re-export: downstream callers import MemoryService "
        "from the package root. Kept out of __all__. The converted root reaches the "
        "runtime-owned service module directly -- MemoryCreate and MemoryUpdate now "
        "come from canonical omnivia_core.memory.models instead, so this edge carries "
        "only the object Core deliberately does not own."
    ),
    (CORE_PACKAGE, f"{CORE_PACKAGE}.persistence"): (
        "Documented compatibility re-export: downstream callers import Database from "
        "the package root. Kept out of __all__."
    ),
    (f"{CORE_PACKAGE}.memory_graph", f"{CORE_PACKAGE}.memory_graph.store"): (
        "The memory_graph package re-exports its durable JSON store alongside the "
        "display contracts. The store performs filesystem writes and belongs with the "
        "runtime once ownership moves."
    ),
    (f"{CORE_PACKAGE}.memory_graph", f"{CORE_PACKAGE}.memory_graph.ingestion_adapter"): (
        "The memory_graph package re-exports the adapter that turns ingestion records "
        "into memory graph records. It depends on the ingestion runtime and belongs "
        "with it once ownership moves."
    ),
}


@dataclass(frozen=True)
class ImportEdge:
    """One import statement, reduced to importer and imported module."""

    importer: str
    imported: str
    symbols: tuple[str, ...]


class DependencyError(RuntimeError):
    """Raised when the dependency scan cannot be completed."""


def module_name_for(path: Path) -> str:
    """Return the dotted module name for a source file under ``CORE_SRC``."""
    relative = path.relative_to(CORE_SRC).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def iter_source_files() -> list[Path]:
    """Return every Core source file, in a stable order."""
    return sorted(CORE_SRC.rglob("*.py"))


def collect_import_edges() -> list[ImportEdge]:
    """Parse every Core source file and return its import edges."""
    edges: list[ImportEdge] = []
    for path in iter_source_files():
        importer = module_name_for(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - a broken tree fails earlier
            raise DependencyError(f"{path}: cannot parse ({exc})") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    edges.append(ImportEdge(importer, alias.name, ()))
            elif isinstance(node, ast.ImportFrom):
                edges.extend(_from_import_edges(node, importer))
    return edges


def _from_import_edges(node: ast.ImportFrom, importer: str) -> list[ImportEdge]:
    """Turn one ``from ... import ...`` into the edges it really creates.

    ``from . import store`` and ``from omnivia_memory.memory_graph import store``
    both import a *module*, not a name, so they are recorded as edges to that
    module. Getting this wrong would hide a contract -> runtime edge behind its
    parent package.
    """
    base = _resolve_from_import(node, importer)
    if base is None:
        return []
    edges: list[ImportEdge] = []
    plain: list[str] = []
    for alias in node.names:
        candidate = f"{base}.{alias.name}"
        if _is_core_module(candidate):
            edges.append(ImportEdge(importer, candidate, (alias.name,)))
        else:
            plain.append(alias.name)
    if plain:
        edges.append(ImportEdge(importer, base, tuple(sorted(plain))))
    return edges


def build_dependency_inventory() -> dict[str, Any]:
    """Return the canonical dependency inventory for the current checkout."""
    edges = collect_import_edges()
    modules = sorted({edge.importer for edge in edges})

    third_party: dict[str, set[str]] = {}
    stdlib: set[str] = set()
    for edge in edges:
        top = edge.imported.split(".")[0]
        if edge.imported == CORE_PACKAGE or edge.imported.startswith(f"{CORE_PACKAGE}."):
            continue
        if top == "__future__" or top in sys.stdlib_module_names:
            stdlib.add(top)
            continue
        third_party.setdefault(top, set()).add(edge.importer)

    declared, optional = _declared_dependencies()
    declared_names = {_distribution_to_module(item) for item in declared}

    return {
        "format_version": BASELINE_FORMAT_VERSION,
        "task": BASELINE_TASK_ID,
        "package": CORE_PACKAGE,
        "module_count": len(modules),
        "declared_dependencies": declared,
        "declared_optional_dependencies": optional,
        "declared_but_unimported": sorted(declared_names - set(third_party)),
        "third_party_imports": [
            {
                "module": name,
                "imported_by": sorted(third_party[name]),
                "reason": ALLOWED_THIRD_PARTY.get(name),
            }
            for name in sorted(third_party)
        ],
        "stdlib_imports": sorted(stdlib),
        "layers": {
            "runtime_prefixes": list(RUNTIME_MODULE_PREFIXES),
            "runtime_modules": [name for name in modules if is_runtime_module(name)],
            "contract_modules": [name for name in modules if not is_runtime_module(name)],
        },
        "transitional_imports": [
            {
                "importer": edge.importer,
                "imported": edge.imported,
                "symbols": list(edge.symbols),
                "reason": TRANSITIONAL_IMPORT_REASONS.get((edge.importer, edge.imported)),
            }
            for edge in _transitional_edges(edges)
        ],
    }


def write_dependency_inventory() -> Path:
    """Regenerate the tracked dependency inventory."""
    write_artifact(DEPENDENCIES_PATH, build_dependency_inventory())
    return DEPENDENCIES_PATH


def _facade_dependency_problems(
    actual: dict[str, Any],
    *,
    package: str = FACADE_COMPATIBILITY_PACKAGE,
    dependency: str = FACADE_COMPATIBILITY_DEPENDENCY,
    importers: tuple[str, ...] = FACADE_COMPATIBILITY_IMPORTERS,
) -> list[str]:
    """Verify the compatibility-facade dependency on ``package`` exactly.

    Requires: ``package`` is imported by exactly ``importers`` (no fewer, no
    more), and ``dependency`` is declared in ``services/omnivia-memory/pyproject.toml``.
    A stale, missing, wrong-range, or wrong-importer state fails here with a
    named reason rather than only via the generic inventory diff.
    """
    problems: list[str] = []
    matches = [entry for entry in actual["third_party_imports"] if entry["module"] == package]
    if not matches:
        problems.append(
            f"expected {package!r} to be imported by the compatibility-facade leaves "
            f"({', '.join(importers)}); found no such import"
        )
    else:
        observed_importers = tuple(sorted(matches[0]["imported_by"]))
        if observed_importers != importers:
            problems.append(
                f"{package!r} importer set drifted: expected {list(importers)}, "
                f"found {list(observed_importers)}"
            )
    if dependency not in actual["declared_dependencies"]:
        problems.append(
            f"services/omnivia-memory/pyproject.toml must declare {dependency!r} as a "
            "dependency of the compatibility distribution"
        )
    return problems


def _canonical_module_imports_stdlib(canonical_module: str, module: str) -> bool:
    """Whether ``canonical_module``'s source imports the stdlib ``module``.

    Reads exactly one file with :mod:`ast`, and matches on the imported module's
    first dotted segment so ``import unicodedata`` counts and a name that merely
    starts with the same letters does not. Nothing is imported for effect.
    """
    path = FACADE_CANONICAL_SRC.joinpath(*canonical_module.split(".")).with_suffix(".py")
    if not path.is_file():
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):  # pragma: no cover - a broken tree fails earlier
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == module for alias in node.names):
                return True
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
            and node.module.split(".")[0] == module
        ):
            return True
    return False


def _facade_stdlib_import_problems(
    actual: dict[str, Any],
    *,
    moves: dict[str, str] = FACADE_STDLIB_IMPORT_MOVES,
    importers: tuple[str, ...] = FACADE_COMPATIBILITY_IMPORTERS,
    canonical_package: str = FACADE_COMPATIBILITY_PACKAGE,
    frozen: dict[str, Any] | None = None,
) -> list[str]:
    """Verify every declared stdlib-import move before it may be normalized away.

    Each entry must (a) name a legacy leaf that really is a declared
    compatibility-facade importer, so the removal is attributable to a sanctioned
    conversion and not to a deleted feature; (b) name a module the frozen baseline
    really recorded; (c) name a module that really is gone from the live legacy
    tree, so a stale entry cannot keep masking a module that came back; and (d)
    name a paired canonical module that really performs the import now, so the
    import is proved to have *moved* rather than been dropped.
    """
    if frozen is None:
        frozen = load_json(DEPENDENCIES_PATH)
    frozen_stdlib = set(frozen.get("stdlib_imports", []))
    actual_stdlib = set(actual.get("stdlib_imports", []))
    problems: list[str] = []
    for module, legacy_module in sorted(moves.items()):
        where = f"stdlib import {module!r} via {legacy_module}"
        if legacy_module not in importers:
            problems.append(
                f"{where}: {legacy_module} is not a declared compatibility-facade "
                "importer, so its stdlib import cannot have moved with a conversion"
            )
            continue
        if module not in frozen_stdlib:
            problems.append(f"{where}: the frozen baseline never recorded it")
            continue
        if module in actual_stdlib:
            problems.append(
                f"{where}: still imported somewhere in the legacy tree, so the "
                "declaration is stale and must be removed"
            )
            continue
        canonical_module = legacy_module.replace(CORE_PACKAGE, canonical_package, 1)
        if not _canonical_module_imports_stdlib(canonical_module, module):
            problems.append(
                f"{where}: {canonical_module} does not import {module!r}, so the "
                "import was dropped rather than moved"
            )
    return problems


def _root_transitional_edges(entries: Iterable[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    """The ``imported -> symbols`` map of every transitional edge out of the root."""
    return {
        entry["imported"]: tuple(entry["symbols"])
        for entry in entries
        if entry["importer"] == CORE_PACKAGE
    }


def _facade_root_runtime_problems(
    actual: dict[str, Any],
    *,
    converted: dict[str, tuple[str, ...]] = FACADE_ROOT_RUNTIME_BINDINGS,
    frozen_bindings: dict[str, tuple[str, ...]] = FACADE_ROOT_FROZEN_RUNTIME_BINDINGS,
    canonicalized: dict[str, str] = FACADE_ROOT_CANONICALIZED_BINDINGS,
    importers: tuple[str, ...] = FACADE_COMPATIBILITY_IMPORTERS,
    frozen: dict[str, Any] | None = None,
) -> list[str]:
    """Verify the compatibility root's runtime-edge delta before normalizing it.

    Converting the root is the one change in this migration that *moves* a frozen
    transitional import rather than only adding or removing one: the root used to
    take ``MemoryCreate``, ``MemoryService`` and ``MemoryUpdate`` together from
    ``omnivia_memory.memory``, and now takes the two inputs from canonical Core and
    reaches ``omnivia_memory.memory.service`` for the one object Core deliberately
    does not own. That is a genuine frozen-vs-live difference in
    ``transitional_imports`` that no existing normalization covers, so it is
    declared as two exactly-known states and proved here before either is applied.

    Each of the following must hold, or the frozen baseline is left alone:

    * the root is a declared ``omnivia_core`` importer, so the move is attributable
      to the sanctioned conversion rather than to an unrelated edit;
    * the frozen artifact's root edges are *exactly*
      ``FACADE_ROOT_FROZEN_RUNTIME_BINDINGS``, so a stale declaration cannot
      overwrite a state it was not written for;
    * the live root edges are *exactly* ``FACADE_ROOT_RUNTIME_BINDINGS``, so the
      root cannot acquire a third runtime edge, or a fifth runtime symbol, behind
      this allowance;
    * every declared runtime module really is a runtime module by
      ``RUNTIME_MODULE_PREFIXES``, and every declared symbol really is one the
      frozen edge set carried, so nothing new is being introduced as a "move";
    * each canonicalized binding is really gone from the root's runtime edges and
      really is imported by the root from its declared canonical owner, so the
      import is proved to have *moved* rather than been dropped.
    """
    if frozen is None:
        frozen = load_json(DEPENDENCIES_PATH)
    problems: list[str] = []
    where = f"compatibility root {CORE_PACKAGE!r}"

    if CORE_PACKAGE not in importers:
        problems.append(
            f"{where}: not a declared compatibility-facade importer, so its runtime "
            "edges cannot have moved with the root conversion"
        )
        return problems

    frozen_edges = _root_transitional_edges(frozen.get("transitional_imports", []))
    if frozen_edges != frozen_bindings:
        problems.append(
            f"{where}: the frozen baseline records root runtime edges "
            f"{ {k: list(v) for k, v in sorted(frozen_edges.items())} }, not the "
            f"declared { {k: list(v) for k, v in sorted(frozen_bindings.items())} }"
        )
        return problems

    live_edges = _root_transitional_edges(actual.get("transitional_imports", []))
    if live_edges != converted:
        problems.append(
            f"{where}: the checkout has root runtime edges "
            f"{ {k: list(v) for k, v in sorted(live_edges.items())} }, not the "
            f"declared { {k: list(v) for k, v in sorted(converted.items())} }"
        )
        return problems

    frozen_symbols = {symbol for symbols in frozen_bindings.values() for symbol in symbols}
    for module, symbols in sorted(converted.items()):
        if not is_runtime_module(module):
            problems.append(
                f"{where}: {module} is not a runtime module, so it is not a legacy "
                "runtime owner the root may keep reaching"
            )
        for symbol in symbols:
            if symbol not in frozen_symbols:
                problems.append(
                    f"{where}: {module}.{symbol} was not one of the frozen root runtime "
                    "symbols, so it is a new edge rather than a moved one"
                )

    root_from_imports = _root_canonical_from_imports()
    for symbol, canonical_module in sorted(canonicalized.items()):
        binding = f"{where}: canonicalized binding {symbol!r}"
        if symbol not in frozen_symbols:
            problems.append(f"{binding}: the frozen baseline never carried it")
            continue
        if any(symbol in symbols for symbols in converted.values()):
            problems.append(
                f"{binding}: still taken from a legacy runtime module, so the "
                "declaration is stale and must be removed"
            )
            continue
        if symbol not in root_from_imports.get(canonical_module, ()):
            problems.append(
                f"{binding}: {CORE_PACKAGE}'s root does not import it from "
                f"{canonical_module}, so it was dropped rather than moved"
            )
    return problems


def _root_canonical_from_imports() -> dict[str, tuple[str, ...]]:
    """``module -> names`` for every canonical from-import in the legacy root's source.

    Read with :mod:`ast` from the file, like the rest of this scan: nothing is
    imported for effect, so a runtime-patched root cannot satisfy the proof above.
    """
    path = CORE_SRC / CORE_PACKAGE / "__init__.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):  # pragma: no cover - a broken tree fails earlier
        return {}
    found: dict[str, list[str]] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
            and _matches_prefix(node.module, FACADE_COMPATIBILITY_PACKAGE)
        ):
            found.setdefault(node.module, []).extend(
                alias.name for alias in node.names if alias.asname is None
            )
    return {module: tuple(names) for module, names in found.items()}


def _normalize_expected_dependencies(
    expected: dict[str, Any],
    *,
    package: str = FACADE_COMPATIBILITY_PACKAGE,
    dependency: str = FACADE_COMPATIBILITY_DEPENDENCY,
    importers: tuple[str, ...] = FACADE_COMPATIBILITY_IMPORTERS,
    stdlib_moves: dict[str, str] = FACADE_STDLIB_IMPORT_MOVES,
    root_runtime_bindings: dict[str, tuple[str, ...]] = FACADE_ROOT_RUNTIME_BINDINGS,
    reasons: dict[tuple[str, str], str] = TRANSITIONAL_IMPORT_REASONS,
) -> dict[str, Any]:
    """Return a copy of ``expected`` with only the sanctioned facade dependency delta applied.

    Four deltas, all caused directly by the facade conversions: the declared
    ``omnivia-core`` dependency, that package's exact importer set, the exact
    stdlib modules named by ``stdlib_moves`` -- each of which is dropped from the
    expected list only after ``_facade_stdlib_import_problems`` has proved the
    import moved to the paired canonical module -- and the compatibility root's
    own transitional edges, replaced whole by ``root_runtime_bindings`` only after
    ``_facade_root_runtime_problems`` has proved both the frozen and the live state
    exactly. Removal is by exact key, never by prefix or substring, and only edges
    whose importer *is* the package root are touched: every other transitional
    edge in the artifact is left alone and still has to match.

    The frozen artifact on disk is never touched; this operates on an
    in-memory copy so every other dependency difference still fails the
    comparison in ``verify_dependency_inventory``.
    """
    normalized = copy.deepcopy(expected)
    normalized["declared_dependencies"] = sorted({*normalized["declared_dependencies"], dependency})
    third_party = [entry for entry in normalized["third_party_imports"] if entry["module"] != package]
    third_party.append({"module": package, "imported_by": list(importers), "reason": None})
    normalized["third_party_imports"] = sorted(third_party, key=lambda entry: entry["module"])
    if "transitional_imports" in normalized:
        others = [
            entry
            for entry in normalized["transitional_imports"]
            if entry["importer"] != CORE_PACKAGE
        ]
        normalized["transitional_imports"] = sorted(
            [
                *others,
                *(
                    {
                        "importer": CORE_PACKAGE,
                        "imported": imported,
                        "symbols": list(symbols),
                        "reason": reasons.get((CORE_PACKAGE, imported)),
                    }
                    for imported, symbols in root_runtime_bindings.items()
                ),
            ],
            key=lambda entry: (entry["importer"], entry["imported"]),
        )
    if "stdlib_imports" in normalized:
        normalized["stdlib_imports"] = [
            module
            for module in normalized["stdlib_imports"]
            if module not in stdlib_moves
        ]
    return normalized


def verify_dependency_inventory() -> list[str]:
    """Check the import graph against the frozen inventory and the allowlists."""
    problems: list[str] = []
    actual = build_dependency_inventory()

    for entry in actual["third_party_imports"]:
        module = entry["module"]
        if module == FACADE_COMPATIBILITY_PACKAGE:
            continue
        if module not in ALLOWED_THIRD_PARTY:
            problems.append(
                f"third-party import {module!r} is not allowlisted (imported by "
                f"{', '.join(entry['imported_by'])}). Core must stay free of "
                "implementation dependencies; add a narrow entry with a reason to "
                "ALLOWED_THIRD_PARTY only if the dependency is genuinely required."
            )

    problems.extend(_facade_dependency_problems(actual))

    for edge in collect_import_edges():
        for prefix, why in sorted(BANNED_IMPORT_PREFIXES.items()):
            if _matches_prefix(edge.imported, prefix):
                problems.append(f"{edge.importer} imports {edge.imported!r}: {why}")

    for entry in actual["transitional_imports"]:
        if not entry["reason"]:
            problems.append(
                f"transitional import {entry['importer']} -> {entry['imported']} is not "
                "allowlisted. The contract layer importing the runtime layer must be "
                "declared in TRANSITIONAL_IMPORT_REASONS with the reason it exists."
            )

    observed = {(entry["importer"], entry["imported"]) for entry in actual["transitional_imports"]}
    for stale in sorted(set(TRANSITIONAL_IMPORT_REASONS) - observed):
        problems.append(
            f"transitional import allowlist entry {stale[0]} -> {stale[1]} no longer "
            "matches a real import; remove it so the allowlist stays narrow."
        )

    expected = load_json(DEPENDENCIES_PATH)

    stdlib_problems = _facade_stdlib_import_problems(actual, frozen=expected)
    if stdlib_problems:
        return [
            *problems,
            (
                "Compatibility-facade stdlib-import move verification failed for one "
                "or more entries in baseline.dependencies' "
                "FACADE_STDLIB_IMPORT_MOVES; refusing to normalize the frozen Phase 0 "
                "baseline for an unverified move."
            ),
            format_differences(stdlib_problems),
        ]

    root_problems = _facade_root_runtime_problems(actual, frozen=expected)
    if root_problems:
        return [
            *problems,
            (
                "Compatibility root runtime-edge verification failed against "
                "baseline.dependencies' FACADE_ROOT_RUNTIME_BINDINGS / "
                "FACADE_ROOT_FROZEN_RUNTIME_BINDINGS / "
                "FACADE_ROOT_CANONICALIZED_BINDINGS; refusing to normalize the frozen "
                "Phase 0 baseline for an unverified root conversion."
            ),
            format_differences(root_problems),
        ]

    normalized_expected = _normalize_expected_dependencies(expected)
    differences = diff_json(normalized_expected, actual)
    if differences:
        problems.append(
            "Dependency inventory drifted from the frozen Phase 0 baseline "
            f"({DEPENDENCIES_PATH.relative_to(REPO_ROOT)}):\n"
            f"{format_differences(differences)}"
        )
    return problems


def is_runtime_module(module: str) -> bool:
    """Whether a Core module carries transitional runtime behaviour."""
    return any(_matches_prefix(module, prefix) for prefix in RUNTIME_MODULE_PREFIXES)


def _transitional_edges(edges: Iterable[ImportEdge]) -> list[ImportEdge]:
    """Contract-layer modules importing runtime-layer modules, deduplicated."""
    collected: dict[tuple[str, str], set[str]] = {}
    for edge in edges:
        if not (edge.imported == CORE_PACKAGE or edge.imported.startswith(f"{CORE_PACKAGE}.")):
            continue
        if is_runtime_module(edge.importer) or not is_runtime_module(edge.imported):
            continue
        collected.setdefault((edge.importer, edge.imported), set()).update(edge.symbols)
    return [
        ImportEdge(importer, imported, tuple(sorted(symbols)))
        for (importer, imported), symbols in sorted(collected.items())
    ]


def _resolve_from_import(node: ast.ImportFrom, importer: str) -> str | None:
    """Resolve ``from . import x`` and ``from .mod import x`` to a dotted name."""
    if node.level == 0:
        return node.module
    # A package's __init__ resolves relative imports against itself; a module
    # resolves them against its parent package.
    package = importer if _is_package(importer) else importer.rpartition(".")[0]
    parts = package.split(".") if package else []
    for _ in range(node.level - 1):
        if not parts:
            return None
        parts.pop()
    if node.module:
        parts.append(node.module)
    return ".".join(parts) or None


def _is_package(module: str) -> bool:
    return (CORE_SRC / Path(*module.split("."))).is_dir()


def _is_core_module(module: str) -> bool:
    """Whether a dotted name is a module or package inside this Core checkout.

    The parent directory is listed rather than probed with ``exists()`` because
    macOS filesystems are case-insensitive, and ``persistence/Database`` would
    otherwise look like a module when only ``persistence/database.py`` exists.
    """
    if not (module == CORE_PACKAGE or module.startswith(f"{CORE_PACKAGE}.")):
        return False
    parts = module.split(".")
    parent = CORE_SRC / Path(*parts[:-1])
    if not parent.is_dir():
        return False
    names = {entry.name for entry in parent.iterdir()}
    return parts[-1] in names or f"{parts[-1]}.py" in names


def _matches_prefix(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(f"{prefix}.")


def _declared_dependencies() -> tuple[list[str], dict[str, list[str]]]:
    data = tomllib.loads(CORE_PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project", {})
    optional = {
        name: sorted(values)
        for name, values in sorted(project.get("optional-dependencies", {}).items())
    }
    return sorted(project.get("dependencies", [])), optional


def _distribution_to_module(requirement: str) -> str:
    """Reduce a requirement string to the module name it is expected to provide."""
    name = requirement.split(";")[0]
    for separator in ("[", "=", ">", "<", "!", "~", " "):
        name = name.split(separator)[0]
    return name.strip().replace("-", "_").lower()
