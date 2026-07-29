"""omnivia_core.knowledge must be an explicit portable barrel: exact ordered
``__all__`` parity with the legacy knowledge package, every exported name
bound to its canonical owner leaf (models/normalize/validation) and nothing
else, a star import exposing exactly what ``__all__`` advertises, and a
fresh-process import that never touches ``omnivia_memory`` or a runtime
module.

``test_parity.py`` already proves each owner leaf (``knowledge.models``,
``.normalize``, ``.validation``) is an exact port of its legacy counterpart.
This module is only concerned with the barrel's own re-export contract, which
none of those per-leaf gates observe.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = REPO_ROOT / "src"
PUBLIC_EXPORTS_INVENTORY = REPO_ROOT / "baseline" / "inventories" / "public-exports.json"
#: The interpreter running this suite -- see test_fresh_process_imports.py
#: for why this must not be a hardcoded ``.venv/bin/python``.
PYTHON = sys.executable

with PUBLIC_EXPORTS_INVENTORY.open(encoding="utf-8") as inventory_file:
    FROZEN_MODULES = json.load(inventory_file)["modules"]

FROZEN_KNOWLEDGE_ALL: tuple[str, ...] = tuple(
    FROZEN_MODULES["omnivia_memory.knowledge"]["all"]
)

LEGACY_TO_CANONICAL_OWNER: dict[str, str] = {
    "omnivia_memory._shared.validation": "omnivia_core._shared.validation",
    "omnivia_memory.knowledge.models": "omnivia_core.knowledge.models",
    "omnivia_memory.knowledge.normalize": "omnivia_core.knowledge.normalize",
    "omnivia_memory.knowledge.validation": "omnivia_core.knowledge.validation",
}

# The frozen inventory records public definitions for classes, functions, and
# typed constants. These three builtin frozensets are deliberately recorded by
# the stricter migration inventory instead, so pin their owner explicitly.
MODEL_CONSTANT_EXPORTS = {
    "BUILTIN_GRAPH_NODE_KINDS",
    "BUILTIN_GRAPH_RELATIONS",
    "BUILTIN_OBJECT_KINDS",
}


def _expected_owner_by_export() -> dict[str, str]:
    owners: dict[str, str] = {}
    for legacy_owner, canonical_owner in LEGACY_TO_CANONICAL_OWNER.items():
        for name in FROZEN_MODULES[legacy_owner]["defines"]:
            if name in FROZEN_KNOWLEDGE_ALL:
                assert name not in owners, f"multiple frozen owners found for {name}"
                owners[name] = canonical_owner
    owners.update(dict.fromkeys(MODEL_CONSTANT_EXPORTS, "omnivia_core.knowledge.models"))
    assert set(owners) == set(FROZEN_KNOWLEDGE_ALL), (
        "frozen owner map does not cover the complete knowledge barrel: "
        f"missing={sorted(set(FROZEN_KNOWLEDGE_ALL) - set(owners))}, "
        f"extra={sorted(set(owners) - set(FROZEN_KNOWLEDGE_ALL))}"
    )
    return owners


EXPECTED_OWNER_BY_EXPORT = _expected_owner_by_export()
EXPECTED_CANONICAL_MODULE_CLOSURE = {
    "omnivia_core",
    "omnivia_core._shared",
    "omnivia_core._shared.validation",
    "omnivia_core.knowledge",
    "omnivia_core.knowledge.models",
    "omnivia_core.knowledge.normalize",
    "omnivia_core.knowledge.validation",
}


def test_knowledge_barrel_all_matches_legacy_order_exactly() -> None:
    canonical = importlib.import_module("omnivia_core.knowledge")
    legacy = importlib.import_module("omnivia_memory.knowledge")
    assert tuple(canonical.__all__) == FROZEN_KNOWLEDGE_ALL, (
        "omnivia_core.knowledge.__all__ drifted from the frozen Phase 0 inventory:\n"
        f"canonical: {canonical.__all__}\nfrozen: {list(FROZEN_KNOWLEDGE_ALL)}"
    )
    assert tuple(legacy.__all__) == FROZEN_KNOWLEDGE_ALL, (
        "live omnivia_memory.knowledge.__all__ drifted from the frozen Phase 0 inventory:\n"
        f"legacy: {legacy.__all__}\nfrozen: {list(FROZEN_KNOWLEDGE_ALL)}"
    )


def test_knowledge_barrel_all_has_no_duplicates() -> None:
    canonical = importlib.import_module("omnivia_core.knowledge")
    assert len(canonical.__all__) == len(set(canonical.__all__)), (
        f"omnivia_core.knowledge.__all__ contains duplicates: {canonical.__all__}"
    )


def test_knowledge_barrel_bindings_match_their_canonical_owner_leaf() -> None:
    """Every advertised name is identical to its exact frozen owner binding."""
    canonical = importlib.import_module("omnivia_core.knowledge")

    for name in canonical.__all__:
        owner_name = EXPECTED_OWNER_BY_EXPORT[name]
        owner_module = importlib.import_module(owner_name)
        assert getattr(canonical, name) is getattr(owner_module, name), (
            f"omnivia_core.knowledge.{name} is not identical to "
            f"its frozen owner {owner_name}.{name}"
        )


def test_knowledge_barrel_star_import_exposes_exactly_advertised_names() -> None:
    namespace: dict[str, object] = {}
    exec("from omnivia_core.knowledge import *", namespace)  # noqa: S102
    exported = {name for name in namespace if name != "__builtins__"}
    assert exported == set(FROZEN_KNOWLEDGE_ALL), (
        f"star import of omnivia_core.knowledge exposed {sorted(exported)}, "
        f"expected exactly {sorted(FROZEN_KNOWLEDGE_ALL)}"
    )


def _isolated_barrel_import_script(import_order: tuple[str, ...]) -> str:
    return "\n".join(
        [
            "import sys",
            "import pathlib",
            "import importlib",
            f"sys.path.insert(0, {str(CORE_SRC)!r})",
            f"for module_name in {import_order!r}:",
            "    importlib.import_module(module_name)",
            f"core_src = pathlib.Path({str(CORE_SRC)!r}).resolve()",
            "outside = []",
            "for name, module in sorted(sys.modules.items()):",
            '    if name != "omnivia_core" and not name.startswith("omnivia_core."):',
            "        continue",
            '    path = getattr(module, "__file__", None)',
            "    if path is None:",
            "        continue",
            "    resolved = pathlib.Path(path).resolve()",
            "    try:",
            "        resolved.relative_to(core_src)",
            "    except ValueError:",
            '        outside.append(f"{name} -> {resolved}")',
            "if outside:",
            '    raise SystemExit("omnivia_core modules loaded from outside src: " + ", ".join(outside))',
            f"expected_core = {EXPECTED_CANONICAL_MODULE_CLOSURE!r}",
            'loaded_core = {name for name in sys.modules if name == "omnivia_core" or name.startswith("omnivia_core.")}',
            "if loaded_core != expected_core:",
            '    raise SystemExit(f"unexpected canonical module closure: {sorted(loaded_core)}")',
            'forbidden = {"omnivia_memory", "sqlalchemy", "sqlite3", "fastapi", "starlette", "httpx", "requests", "omnivia_core_runtime", "omnivia_core_mcp", "omnivia_core_cli", "omnivia_platform", "omnivia_dev", "omnivia_cloud"}',
            'leaked = sorted(forbidden & {m.split(".")[0] for m in sys.modules})',
            "if leaked:",
            '    raise SystemExit(f"forbidden modules loaded: {leaked}")',
            'print("OK")',
        ]
    )


@pytest.mark.parametrize(
    "import_order",
    (
        ("omnivia_core.knowledge",),
        (
            "omnivia_core._shared.validation",
            "omnivia_core.knowledge.models",
            "omnivia_core.knowledge.normalize",
            "omnivia_core.knowledge.validation",
            "omnivia_core.knowledge",
        ),
        (
            "omnivia_core.knowledge.validation",
            "omnivia_core.knowledge.normalize",
            "omnivia_core.knowledge.models",
            "omnivia_core._shared.validation",
            "omnivia_core.knowledge",
        ),
    ),
    ids=("barrel-first", "owner-order", "reverse-owner-order"),
)
def test_knowledge_barrel_imports_in_isolation_from_src_alone(
    import_order: tuple[str, ...],
) -> None:
    assert PYTHON, "sys.executable must name the interpreter running this suite"

    result = subprocess.run(
        [PYTHON, "-I", "-S", "-c", _isolated_barrel_import_script(import_order)],
        cwd=REPO_ROOT,
        env={},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"isolated import of omnivia_core.knowledge failed\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip() == "OK"
