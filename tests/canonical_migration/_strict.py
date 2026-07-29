"""Source-level-fidelity parity descriptors for Slice A canonical leaves.

``baseline.inventory.describe_symbol`` is built to answer "did a public
symbol move or disappear" for the frozen Phase 0 export baseline. It
deliberately normalizes away the details this module exists to catch:
dataclass field *types* (only field *names* are recorded), generated
``__init__``/``__eq__``/``__hash__``/``__repr__`` signatures (dunders are
skipped as "private"), module-level constants and type aliases whose
runtime ``__module__`` does not resolve back to the defining module (e.g.
builtin ``frozenset`` instances, ``types.UnionType`` aliases), and
``__all__`` presence.

Everything here is intentionally exact rather than normalized: comparing
``str(field.type)`` (or ``repr()`` of a signature parameter's annotation)
preserves ``typing.List[str]`` vs ``list[str]`` and ``typing.Optional[X]``
vs ``X | None`` as *different* values, which is the whole point -- Slice A
must port the legacy source's exact annotation spellings, not a
"modernized" equivalent.
"""

from __future__ import annotations

import dataclasses
import enum
import inspect
import re
from types import ModuleType
from typing import Any

_OBJECT_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")
#: Cross-module type references (``entrypoint: omnivia_core.module_manifest
#: .models.Entrypoint`` vs ``...omnivia_memory...``) are an *expected*
#: difference -- Slice A rewrites internal imports from the legacy package to
#: the canonical one -- not annotation drift. Normalized away so the
#: comparison stays sensitive to the spellings that *do* matter
#: (``typing.List[str]`` vs ``list[str]``, ``typing.Optional[X]`` vs
#: ``X | None``), which never contain a package name.
_PACKAGE_PREFIX = re.compile(r"\bomnivia_(core|memory)\b")


def _clean(text: str) -> str:
    """Blank out memory addresses and the legacy/canonical package name so
    descriptions are stable across runs and comparable across packages."""
    return _PACKAGE_PREFIX.sub("omnivia_pkg", _OBJECT_ADDRESS.sub("0xXXXX", text))


def _signature_of(func: Any) -> str:
    target = func
    if isinstance(func, (classmethod, staticmethod)):
        target = func.__func__
    elif isinstance(func, property):
        target = func.fget
    if target is None:
        return "(...)"
    try:
        return _clean(str(inspect.signature(target)))
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return "(...)"


def _own_callables(cls: type) -> dict[str, str]:
    """Every function/classmethod/staticmethod/property defined directly on
    ``cls`` (not inherited), including dunders -- ``__init__``, ``__eq__``,
    ``__repr__``, ``__post_init__`` -- since those carry the generated
    dataclass contract this module exists to pin down."""
    out: dict[str, str] = {}
    for name, member in vars(cls).items():
        if member is None:
            # dataclass(eq=True, frozen=False) sets __hash__ = None explicitly.
            if name == "__hash__":
                out[name] = "None"
            continue
        if isinstance(member, (classmethod, staticmethod)):
            kind = "classmethod" if isinstance(member, classmethod) else "staticmethod"
            out[name] = f"{kind}{_signature_of(member)}"
        elif isinstance(member, property):
            out[name] = f"property{_signature_of(member)}"
        elif inspect.isfunction(member):
            out[name] = f"function{_signature_of(member)}"
    return out


def _factory_kind(factory: Any) -> str | None:
    """Presence + a light identity description of a dataclass default_factory.

    Builtins (``list``/``dict``/``set``/``tuple``/``frozenset``) are compared
    by name. Anything else (typically a lambda constructing a nested model)
    is compared by the qualname of the type it produces, which catches a
    factory that silently starts returning a different shape without
    requiring byte-identical lambda source (an unreasonable bar across two
    independently-loaded modules).
    """
    if factory is dataclasses.MISSING:
        return None
    if factory in (list, dict, set, tuple, frozenset):
        return f"builtin:{factory.__name__}"
    try:
        produced = factory()
    except Exception:  # noqa: BLE001 -- pragma: no cover - defensive, no known factory raises
        return "custom:<call-failed>"
    return f"custom:{type(produced).__qualname__}"


def _default_repr(default: Any) -> str:
    if default is dataclasses.MISSING:
        return "<no-default>"
    return _clean(repr(default))


def _dataclass_fields(cls: type) -> list[dict[str, Any]]:
    """Fields in declaration order, with exact type spelling and shape."""
    fields = []
    for f in dataclasses.fields(cls):
        fields.append(
            {
                "name": f.name,
                "type": _clean(str(f.type)),
                "default": _default_repr(f.default),
                "default_factory": _factory_kind(f.default_factory),
                "init": f.init,
                "repr": f.repr,
                "compare": f.compare,
                "hash": f.hash,
                "kw_only": getattr(f, "kw_only", dataclasses.MISSING) is True,
            }
        )
    return fields


def _enum_base(cls: type) -> str:
    for base in cls.__mro__[1:]:
        if base is enum.Enum:
            continue
        if base in (str, int):
            return base.__name__
    return "Enum"


def _own_annotations(obj: Any) -> dict[str, str]:
    raw = vars(obj).get("__annotations__", {})
    return {name: _clean(str(value)) for name, value in raw.items()}


def describe_class(cls: type) -> dict[str, Any]:
    info: dict[str, Any] = {
        "own_annotations": _own_annotations(cls),
        "bases": [_clean(f"{b.__module__}.{b.__qualname__}") for b in cls.__bases__],
    }
    if issubclass(cls, enum.Enum):
        info["kind"] = "enum"
        info["enum_base"] = _enum_base(cls)
        info["members"] = [f"{member.name}={member.value!r}" for member in cls]
        return info
    if dataclasses.is_dataclass(cls):
        info["kind"] = "dataclass"
        info["dataclass_params"] = _clean(str(cls.__dataclass_params__))  # type: ignore[attr-defined]
        info["fields"] = _dataclass_fields(cls)
    else:
        info["kind"] = "class"
    try:
        info["class_signature"] = _clean(str(inspect.signature(cls)))
    except (TypeError, ValueError):  # pragma: no cover
        info["class_signature"] = "(...)"
    info["callables"] = _own_callables(cls)
    return info


def describe_function(func: Any) -> dict[str, Any]:
    return {"kind": "function", "signature": _signature_of(func)}


def describe_constant(value: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {f.name: _clean(repr(getattr(value, f.name))) for f in dataclasses.fields(value)}
        return {
            "kind": "constant_dataclass_instance",
            "type": _clean(type(value).__qualname__),
            "fields": fields,
        }
    if isinstance(value, (set, frozenset)):
        kind = "frozenset" if isinstance(value, frozenset) else "set"
        return {"kind": kind, "values": sorted(_clean(repr(item)) for item in value)}
    return {"kind": "constant", "type": _clean(type(value).__name__), "repr": _clean(repr(value))}


def describe_symbol(value: Any) -> dict[str, Any]:
    """Strict counterpart to ``baseline.inventory.describe_symbol``."""
    if inspect.isclass(value):
        return describe_class(value)
    if inspect.isfunction(value) or inspect.isbuiltin(value):
        return describe_function(value)
    return describe_constant(value)


def module_annotations(module: ModuleType) -> dict[str, str]:
    raw = vars(module).get("__annotations__", {})
    return {name: _clean(str(value)) for name, value in raw.items()}


#: Module-level constants whose runtime ``__module__`` does not resolve back
#: to the defining module (builtin ``frozenset`` instances have no
#: meaningful ``__module__``; ``types.UnionType`` aliases resolve to
#: ``"types"``), so they are invisible to a definition filter built around
#: ``__module__`` identity and must be listed explicitly.
EXTRA_MODULE_CONSTANTS: dict[str, tuple[str, ...]] = {
    "omnivia_core._shared.validation": ("SENSITIVE_KEYS",),
    "omnivia_core.knowledge.models": (
        "BUILTIN_OBJECT_KINDS",
        "BUILTIN_GRAPH_NODE_KINDS",
        "BUILTIN_GRAPH_RELATIONS",
    ),
    "omnivia_core.control_plane.models": (
        "CONTROL_PLANE_CONTRACT_VERSION",
        "CONTROL_PLANE_SCHEMA_VERSION",
    ),
    "omnivia_core.memory_graph.models": ("Confidence",),
    "omnivia_core.run_ledger.models": (
        "RUN_LEDGER_CONTRACT_VERSION",
        "RUN_LEDGER_PATH_ENV",
    ),
    "omnivia_core.run_ledger.validation": ("TERMINAL_RUN_STATUSES",),
}

#: (module, alias_name, target_name): the alias must be the *exact same
#: object* as the target, not merely an equal one -- catches a canonical
#: port that accidentally redefines the alias as a lookalike class instead
#: of assigning it.
ALIAS_IDENTITY: tuple[tuple[str, str, str], ...] = (
    ("omnivia_core.ingestion.models", "IngestSource", "Source"),
)
