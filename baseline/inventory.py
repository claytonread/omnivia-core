"""Machine-checkable inventory of Core's public Python exports.

The upcoming package migration renames and moves modules. That is only safe if
we can prove, mechanically, what the public surface looked like beforehand. This
module walks the installed ``omnivia_memory`` package and records:

- the root ``__all__`` and the compatibility symbols that are importable from the
  root but deliberately excluded from ``__all__``;
- for every root export, which module actually defines it and which subpackages
  re-export it, so a move is visible even when the name is unchanged;
- for every module, its ``__all__`` and the public symbols it defines, with
  enough detail (enum members, dataclass fields, callable signatures) to catch a
  silent contract change.

Nothing here imports Core's runtime for effect: the inventory is pure
introspection.
"""

from __future__ import annotations

import dataclasses
import enum
import inspect
import pkgutil
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
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

PUBLIC_EXPORTS_PATH = INVENTORY_DIR / "public-exports.json"

_OBJECT_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")


class InventoryError(RuntimeError):
    """Raised when the public export inventory cannot be built or verified."""


def ensure_core_importable() -> ModuleType:
    """Import Core, preferring this checkout over any stale editable install.

    The benchmark runner already guards against importing ``omnivia_memory``
    from a shadow install; the baseline needs the same guarantee, because a
    baseline captured from the wrong tree is worse than no baseline.
    """
    if str(CORE_SRC) not in sys.path:
        sys.path.insert(0, str(CORE_SRC))
    module = __import__(CORE_PACKAGE)
    resolved = Path(module.__file__ or "").resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError as exc:  # pragma: no cover - environment guard
        raise InventoryError(
            f"{CORE_PACKAGE} resolved to {resolved}, which is outside this repository "
            f"({REPO_ROOT}). Baseline artifacts must describe this checkout. "
            "Uninstall the shadowing package or run with "
            "PYTHONPATH=services/omnivia-memory/src."
        ) from exc
    return module


def iter_core_modules(root: ModuleType) -> Iterator[ModuleType]:
    """Yield ``omnivia_memory`` and every importable submodule, sorted by name."""
    names = [root.__name__]
    for info in pkgutil.walk_packages(root.__path__, prefix=f"{root.__name__}."):
        names.append(info.name)
    for name in sorted(set(names)):
        yield __import__(name, fromlist=["__name__"])


def build_public_export_inventory() -> dict[str, Any]:
    """Return the full public export inventory for the current checkout."""
    root = ensure_core_importable()
    modules = list(iter_core_modules(root))
    module_by_name = {module.__name__: module for module in modules}

    root_all = sorted(getattr(root, "__all__", []))
    compatibility = _compatibility_exports(root, frozenset(root_all))

    bindings: dict[str, Any] = {}
    for name in [*root_all, *compatibility]:
        if name.startswith("__"):
            continue
        value = getattr(root, name)
        bindings[name] = {
            "defined_in": _defining_module(value),
            "exported_by": _exporting_modules(value, name, module_by_name),
        }

    module_entries: dict[str, Any] = {}
    for module in modules:
        module_entries[module.__name__] = {
            "all": sorted(getattr(module, "__all__", [])) if hasattr(module, "__all__") else None,
            "defines": _module_definitions(module),
        }

    return {
        "format_version": BASELINE_FORMAT_VERSION,
        "task": BASELINE_TASK_ID,
        "package": CORE_PACKAGE,
        "package_version": getattr(root, "__version__", None),
        "package_source": _repo_relative(root.__file__),
        "root": {
            "all": root_all,
            "compatibility_exports": compatibility,
            "bindings": bindings,
        },
        "modules": module_entries,
    }


def write_public_export_inventory() -> Path:
    """Regenerate the tracked public export inventory."""
    write_artifact(PUBLIC_EXPORTS_PATH, build_public_export_inventory())
    return PUBLIC_EXPORTS_PATH


def verify_public_export_inventory() -> list[str]:
    """Return precise drift messages, or an empty list when the surface matches."""
    expected = load_json(PUBLIC_EXPORTS_PATH)
    actual = build_public_export_inventory()
    differences = diff_json(expected, actual)
    if not differences:
        return []
    return [
        "Public Python export inventory drifted from the frozen Phase 0 baseline.",
        f"Artifact: {PUBLIC_EXPORTS_PATH.relative_to(REPO_ROOT)}",
        format_differences(differences),
        (
            "If the change is intended, regenerate with `python -m baseline capture` "
            "and record the reason in the review note."
        ),
    ]


def _compatibility_exports(root: ModuleType, root_all: frozenset[str]) -> list[str]:
    """Public root attributes that are importable but excluded from ``__all__``.

    Core's root docstring calls these out explicitly: runtime-heavy symbols stay
    importable for downstream callers while the advertised API stays
    contract-only. They are part of the baseline because the migration must not
    break them by accident.
    """
    names = []
    for name, value in vars(root).items():
        if name.startswith("_") or name in root_all or isinstance(value, ModuleType):
            continue
        names.append(name)
    return sorted(names)


def _defining_module(value: Any) -> str | None:
    module = getattr(value, "__module__", None)
    return module if isinstance(module, str) else None


def _exporting_modules(
    value: Any,
    name: str,
    module_by_name: dict[str, ModuleType],
) -> list[str]:
    """Modules whose ``__all__`` advertises this exact object under this name."""
    exporters = []
    for module_name, module in module_by_name.items():
        if module_name == CORE_PACKAGE:
            continue
        if name not in getattr(module, "__all__", []):
            continue
        if getattr(module, name, None) is value:
            exporters.append(module_name)
    return sorted(exporters)


def _module_definitions(module: ModuleType) -> dict[str, Any]:
    """Detail for every public symbol that is *defined* in this module."""
    definitions: dict[str, Any] = {}
    for name, value in vars(module).items():
        if name.startswith("_") or isinstance(value, ModuleType):
            continue
        if _defining_module(value) != module.__name__:
            # Imported into the module rather than defined by it. Recording it
            # here would make every re-export look like a definition and hide
            # real moves during the migration.
            continue
        definitions[name] = describe_symbol(value)
    return dict(sorted(definitions.items()))


def describe_symbol(value: Any) -> dict[str, Any]:
    """Return a stable, contract-level description of a public symbol."""
    if inspect.isclass(value):
        return _describe_class(value)
    if inspect.isfunction(value) or inspect.isbuiltin(value):
        return {"kind": "function", "signature": _signature(value)}
    return _describe_constant(value)


def _describe_class(value: type) -> dict[str, Any]:
    if issubclass(value, enum.Enum):
        return {
            "kind": "enum",
            "base": _enum_base(value),
            "members": [f"{member.name}={member.value!r}" for member in value],
        }
    if issubclass(value, BaseException):
        return {"kind": "exception", "bases": _base_names(value)}
    if dataclasses.is_dataclass(value):
        params = getattr(value, "__dataclass_params__", None)
        return {
            "kind": "dataclass",
            "frozen": bool(getattr(params, "frozen", False)),
            "fields": [field.name for field in dataclasses.fields(value)],
            "methods": _public_methods(value),
        }
    if _is_typed_dict(value):
        return {"kind": "typed_dict", "fields": sorted(getattr(value, "__annotations__", {}))}
    return {"kind": "class", "bases": _base_names(value), "methods": _public_methods(value)}


def _describe_constant(value: Any) -> dict[str, Any]:
    return {"kind": "constant", "type": type(value).__name__, "value": _render_value(value)}


def _render_value(value: Any) -> Any:
    """Render a constant's value in a form that is stable across runs."""
    if isinstance(value, enum.Enum):
        return f"{type(value).__name__}.{value.name}"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (set, frozenset)):
        return sorted(str(_render_value(item)) for item in value)
    if isinstance(value, (list, tuple)):
        return [_render_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _render_value(value[key]) for key in sorted(value, key=str)}
    return f"<{type(value).__name__}>"


def _enum_base(value: type) -> str:
    for base in value.__mro__[1:]:
        if base is enum.Enum:
            continue
        if base in (str, int):
            return base.__name__
    return "Enum"


def _base_names(value: type) -> list[str]:
    return [f"{base.__module__}.{base.__qualname__}" for base in value.__bases__]


def _public_methods(value: type) -> list[str]:
    methods = []
    for name, member in vars(value).items():
        if name.startswith("_"):
            continue
        if inspect.isfunction(member) or isinstance(member, (classmethod, staticmethod, property)):
            methods.append(f"{name}{_signature(member)}")
    return sorted(methods)


def _signature(value: Any) -> str:
    target = value
    if isinstance(value, (classmethod, staticmethod)):
        target = value.__func__
    elif isinstance(value, property):
        target = value.fget
    if target is None:
        return "(...)"
    try:
        rendered = str(inspect.signature(target))
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return "(...)"
    # Default values that repr as ``<object at 0x...>`` would change every run.
    return _OBJECT_ADDRESS.sub("0xXXXX", rendered)


def _is_typed_dict(value: type) -> bool:
    return hasattr(value, "__required_keys__") and hasattr(value, "__annotations__")


def _repo_relative(path: str | None) -> str | None:
    if path is None:
        return None
    return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
