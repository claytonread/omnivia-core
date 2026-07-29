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

#: Why each contract -> runtime edge is tolerated for now. An observed edge with
#: no entry here fails the check; an entry here with no observed edge is stale.
TRANSITIONAL_IMPORT_REASONS: dict[tuple[str, str], str] = {
    (CORE_PACKAGE, f"{CORE_PACKAGE}.memory"): (
        "Documented compatibility re-export: downstream callers import MemoryService, "
        "MemoryCreate, and MemoryUpdate from the package root. Kept out of __all__."
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


def verify_dependency_inventory() -> list[str]:
    """Check the import graph against the frozen inventory and the allowlists."""
    problems: list[str] = []
    actual = build_dependency_inventory()

    for entry in actual["third_party_imports"]:
        module = entry["module"]
        if module not in ALLOWED_THIRD_PARTY:
            problems.append(
                f"third-party import {module!r} is not allowlisted (imported by "
                f"{', '.join(entry['imported_by'])}). Core must stay free of "
                "implementation dependencies; add a narrow entry with a reason to "
                "ALLOWED_THIRD_PARTY only if the dependency is genuinely required."
            )

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
    differences = diff_json(expected, actual)
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
