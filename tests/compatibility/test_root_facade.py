"""Identity and export coverage for the converted ``omnivia_memory`` package root.

The route registry's ``root_facade`` source gate
(``baseline.facade_manifest.root_facade_defects``) reads the root's *source* and
holds it to a frozen owner-by-owner import table. This module is the independent
half: it imports the packages and checks the objects, so a source that satisfies
the gate but does not actually resolve to the canonical identities still fails.

Nothing here reads the root's own import block to decide what to expect. The
advertised names come from ``baseline.facade_manifest.ROOT_FACADE_ALL``, and the
canonical owner of each one is derived from the *leaf* route map in
``baseline.inventory.FACADE_ROUTES`` -- the leaves, not the barrels the root
imports from -- so the comparison is against where the object is really defined
rather than against where the root happens to fetch it. That derivation fails
closed: a name the leaf routes do not cover, or cover ambiguously without an
explicit collision override here, is an error rather than a skipped check.
"""

from __future__ import annotations

import ast
import collections
import importlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from baseline.facade_manifest import (
    CANONICAL_ROOT_ALL,
    ROOT_FACADE_ALL,
    ROOT_FACADE_HIDDEN_BINDINGS,
    ROOT_FACADE_HIDDEN_CANONICAL_BINDINGS,
    ROOT_FACADE_HIDDEN_RUNTIME_BINDINGS,
    ROOT_FACADE_VERSION_BINDING,
    ROOT_FACADE_VERSION_MODULE,
    canonical_root_source_path,
)
from baseline.inventory import FACADE_ROUTES

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = REPO_ROOT / "src"
MEMORY_SRC = REPO_ROOT / "services" / "omnivia-memory" / "src"
PYTHON = sys.executable

LEGACY_ROOT = "omnivia_memory"
CANONICAL_ROOT = "omnivia_core"

#: The six advertised names more than one domain publishes under the same
#: spelling, each pinned to the owner the frozen root contract routes it to, plus
#: the plausible wrong owners a "first exporter wins" rule could have picked.
#:
#: These are declared, never derived. Five of the six are genuinely ambiguous in
#: the leaf route map (``test_root_owner_map_covers_every_advertised_name``
#: asserts exactly which), so a derivation would have to break the tie somehow,
#: and any tie-break that happens to agree with the current import order would
#: stop being a check. ``SourceType`` is unambiguous in the route map today and is
#: pinned anyway: it travels with ``Source``, and ingestion acquiring a
#: same-named enum later must fail here rather than silently make the pair
#: derivable-but-wrong.
ROOT_COLLISION_OWNERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "LifecycleState": (
        "omnivia_core.control_plane.models",
        ("omnivia_core.lifecycle.models", "omnivia_core.memory.models"),
    ),
    "ProvenanceRequirement": (
        "omnivia_core.component_contract.models",
        ("omnivia_core.app_manifest.models",),
    ),
    # ``omnivia_core.memory.models`` is *not* listed as a wrong owner here: it
    # re-exports this same provenance record rather than defining a rival one, so
    # it could not be the wrong answer. Ingestion's ``Source`` is a different
    # class entirely and is the real alternative.
    "Source": ("omnivia_core.provenance.models", ("omnivia_core.ingestion.models",)),
    "SourceRef": (
        "omnivia_core.knowledge.models",
        ("omnivia_core.memory_graph.models",),
    ),
    "SourceType": ("omnivia_core.provenance.models", ()),
    # The memory graph re-exports the *same* shared record, so it is not a wrong
    # owner for this name. The four domain-local ``ValidationResult`` classes are.
    "ValidationResult": (
        "omnivia_core._shared.validation",
        (
            "omnivia_core.app_manifest.models",
            "omnivia_core.app_shell_bridge.models",
            "omnivia_core.component_contract.models",
            "omnivia_core.control_plane.models",
        ),
    ),
}

#: The two non-advertised canonical inputs and the module that owns each.
HIDDEN_CANONICAL_OWNERS: dict[str, str] = {
    "MemoryCreate": "omnivia_core.memory.models",
    "MemoryUpdate": "omnivia_core.memory.models",
}

#: The two non-advertised bindings that stay legacy-owned, and the runtime module
#: that owns each. Both modules are declared ``runtime_only`` in the registry, so
#: neither has a canonical counterpart to have come from.
HIDDEN_RUNTIME_OWNERS: dict[str, str] = {
    "Database": "omnivia_memory.persistence.database",
    "MemoryService": "omnivia_memory.memory.service",
}

ADVERTISED_PORTABLE = tuple(
    name for name in ROOT_FACADE_ALL if name != ROOT_FACADE_VERSION_BINDING
)


def _derived_owners() -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """Invert the leaf route map into ``name -> owner``, keeping the ambiguities.

    Returns the names exactly one leaf route owns, and the names more than one
    does. Nothing is resolved here: a tie is reported, not broken.
    """
    owners: dict[str, set[str]] = collections.defaultdict(set)
    for symbols in FACADE_ROUTES.values():
        for name, canonical_module in symbols.items():
            owners[name].add(canonical_module)
    unique = {
        name: next(iter(modules))
        for name, modules in owners.items()
        if len(modules) == 1
    }
    ambiguous = {
        name: tuple(sorted(modules))
        for name, modules in owners.items()
        if len(modules) > 1
    }
    return unique, ambiguous


def _root_owner_map() -> dict[str, str]:
    """``advertised name -> canonical owner`` for all 182 portable names.

    Built from the leaf routes, with the six collisions taken from
    ``ROOT_COLLISION_OWNERS`` rather than from the route map. Fails closed: a
    missing name, or an ambiguous one with no declared override, raises.
    """
    unique, ambiguous = _derived_owners()
    mapping: dict[str, str] = {}
    unresolved: list[str] = []
    for name in ADVERTISED_PORTABLE:
        override = ROOT_COLLISION_OWNERS.get(name)
        if override is not None:
            mapping[name] = override[0]
            continue
        if name in ambiguous:
            unresolved.append(f"{name} (ambiguous: {list(ambiguous[name])})")
            continue
        if name not in unique:
            unresolved.append(f"{name} (no leaf route owns it)")
            continue
        mapping[name] = unique[name]
    if unresolved:
        raise AssertionError(
            "cannot resolve a canonical owner for every advertised root name: "
            + "; ".join(sorted(unresolved))
        )
    return mapping


ROOT_OWNERS = _root_owner_map()


def _import(module: str) -> ModuleType:
    return importlib.import_module(module)


def _root() -> ModuleType:
    return _import(LEGACY_ROOT)


def _run_isolated(script: str, *, expect_stdout: str = "OK") -> subprocess.CompletedProcess[str]:
    """Run ``script`` in a fresh interpreter with this checkout's two source roots.

    ``-I`` drops ``PYTHONPATH``, the user site directory and the cwd, so only the
    paths the script inserts itself, plus the interpreter's own site-packages,
    are on ``sys.path``: no test-session import state leaks in. Site-packages is
    deliberately *not* disabled -- the root reaches
    ``omnivia_memory.persistence``, whose SQLAlchemy dependency has to resolve.
    """
    result = subprocess.run(
        [PYTHON, "-I", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"isolated subprocess failed (exit {result.returncode}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert result.stdout.strip() == expect_stdout, result.stdout
    return result


_PATH_PREAMBLE = (
    "import sys\n"
    f"sys.path.insert(0, {str(MEMORY_SRC)!r})\n"
    f"sys.path.insert(0, {str(CORE_SRC)!r})\n"
)


# ---------------------------------------------------------------------------
# The owner map itself.
# ---------------------------------------------------------------------------


def test_root_owner_map_covers_every_advertised_name_and_fails_closed() -> None:
    """The derivation is only trustworthy if it cannot silently skip a name.

    All 182 advertised portable names resolve to exactly one owner; the five
    genuinely ambiguous ones in the leaf route map are exactly the five the
    collision table declares an override for; and a name with no route at all
    raises rather than being dropped.
    """
    unique, ambiguous = _derived_owners()
    assert set(ambiguous) & set(ADVERTISED_PORTABLE) == {
        "LifecycleState",
        "ProvenanceRequirement",
        "Source",
        "SourceRef",
        "ValidationResult",
    }
    # ``SourceType`` is pinned but not currently ambiguous, so its override must
    # agree with the single owner the routes do give it -- otherwise the table and
    # the route map would be quietly contradicting each other.
    assert unique["SourceType"] == ROOT_COLLISION_OWNERS["SourceType"][0]
    assert ROOT_COLLISION_OWNERS["SourceType"][1] == ()

    assert set(ROOT_OWNERS) == set(ADVERTISED_PORTABLE)
    assert len(ROOT_OWNERS) == 182
    for name, owner in ROOT_OWNERS.items():
        assert owner.startswith("omnivia_core"), (name, owner)

    with pytest.raises(AssertionError, match="no leaf route owns it"):
        _resolve_one("NotARootName")


def _resolve_one(name: str) -> str:
    """The owner-resolution step for a single name, so its failure is testable."""
    unique, ambiguous = _derived_owners()
    if name in ROOT_COLLISION_OWNERS:
        return ROOT_COLLISION_OWNERS[name][0]
    if name in ambiguous:
        raise AssertionError(f"{name} (ambiguous: {list(ambiguous[name])})")
    if name not in unique:
        raise AssertionError(f"{name} (no leaf route owns it)")
    return unique[name]


def test_collision_overrides_are_exactly_six_and_name_real_alternatives() -> None:
    """The override table stays a closed set of six, and every "wrong owner" it
    names is a module that really does publish that name -- otherwise the
    distinctness assertions below would be comparing against nothing."""
    assert len(ROOT_COLLISION_OWNERS) == 6
    assert set(ROOT_COLLISION_OWNERS) == {
        "LifecycleState",
        "ProvenanceRequirement",
        "Source",
        "SourceRef",
        "SourceType",
        "ValidationResult",
    }
    for name, (owner, wrong_owners) in sorted(ROOT_COLLISION_OWNERS.items()):
        assert hasattr(_import(owner), name), (name, owner)
        for wrong in wrong_owners:
            assert hasattr(_import(wrong), name), (name, wrong)
            assert wrong != owner


# ---------------------------------------------------------------------------
# The advertised surface.
# ---------------------------------------------------------------------------


def test_root_all_is_the_frozen_ordered_contract() -> None:
    """183 entries, in the frozen order, with no duplicate: 182 portable contracts
    plus ``__version__``. The list object itself is compared, so a reordering fails
    here as well as at the source gate."""
    root = _root()
    assert list(root.__all__) == list(ROOT_FACADE_ALL)
    assert len(ROOT_FACADE_ALL) == 183
    assert len(set(ROOT_FACADE_ALL)) == 183
    assert len(ADVERTISED_PORTABLE) == 182
    assert ROOT_FACADE_VERSION_BINDING in ROOT_FACADE_ALL


def test_root_star_surface_is_exactly_all() -> None:
    """``from omnivia_memory import *`` binds the frozen set and nothing else, so
    the advertised contract is what a wildcard consumer actually receives."""
    namespace: dict[str, object] = {}
    exec("from omnivia_memory import *", namespace)  # noqa: S102
    namespace.pop("__builtins__", None)
    assert set(namespace) == set(ROOT_FACADE_ALL)
    assert set(namespace).isdisjoint(ROOT_FACADE_HIDDEN_BINDINGS)


@pytest.mark.parametrize("name", ADVERTISED_PORTABLE, ids=ADVERTISED_PORTABLE)
def test_every_advertised_name_is_the_exact_canonical_object(name: str) -> None:
    """One assertion per advertised name: ``omnivia_memory.<name>`` *is* the object
    bound at its canonical owner. Not equal, not a subclass, not a lookalike --
    the same object, so a consumer's ``isinstance`` and identity checks keep
    working across the two import paths."""
    root = _root()
    owner_module = _import(ROOT_OWNERS[name])
    assert hasattr(root, name), f"{LEGACY_ROOT}.{name} is missing"
    assert hasattr(owner_module, name), f"{ROOT_OWNERS[name]}.{name} is missing"
    assert getattr(root, name) is getattr(owner_module, name), (
        f"{LEGACY_ROOT}.{name} is not the exact object bound at "
        f"{ROOT_OWNERS[name]}.{name}"
    )


def test_advertised_names_are_covered_exactly_once() -> None:
    """The per-name test above is parametrized from the frozen tuple, so this pins
    that the parametrization is the whole contract and covers each name once."""
    assert len(ADVERTISED_PORTABLE) == len(set(ADVERTISED_PORTABLE)) == 182
    assert set(ADVERTISED_PORTABLE) | {ROOT_FACADE_VERSION_BINDING} == set(
        ROOT_FACADE_ALL
    )


# ---------------------------------------------------------------------------
# The version binding.
# ---------------------------------------------------------------------------


def test_version_is_imported_from_the_canonical_root_not_copied() -> None:
    """Equality always, identity where Python guarantees it, and -- because a
    second literal assignment would satisfy both for as long as the two strings
    happened to match -- the source proves the root has no version of its own."""
    root = _root()
    canonical = _import(ROOT_FACADE_VERSION_MODULE)
    assert root.__version__ == canonical.__version__
    # Both names are bound to the same ``str`` object: the root imports it, so
    # there is only one object to be bound to. A copied literal would be a
    # different (if equal) object unless the interpreter happened to intern it,
    # which is exactly why the source check below is here too.
    assert root.__version__ is canonical.__version__

    tree = ast.parse(_root_source(), filename=str(_root_path()))
    assignments = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]
    assert assignments == ["__all__"], (
        f"the root assigns {assignments}; it must import its version, not restate it"
    )
    version_imports = [
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and any(alias.name == ROOT_FACADE_VERSION_BINDING for alias in node.names)
    ]
    assert version_imports == [ROOT_FACADE_VERSION_MODULE]


def _root_path() -> Path:
    return MEMORY_SRC / LEGACY_ROOT / "__init__.py"


def _root_source() -> str:
    return _root_path().read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The four non-advertised compatibility bindings.
# ---------------------------------------------------------------------------


def test_the_four_hidden_bindings_are_importable_and_unadvertised() -> None:
    """All four resolve through the root, none is in ``__all__``, and none arrives
    through a wildcard import. That is the whole arrangement: importable for
    callers that ask by name, absent from the advertised contract."""
    root = _root()
    assert ROOT_FACADE_HIDDEN_BINDINGS == (
        "Database",
        "MemoryCreate",
        "MemoryService",
        "MemoryUpdate",
    )
    for name in ROOT_FACADE_HIDDEN_BINDINGS:
        assert hasattr(root, name), f"{LEGACY_ROOT}.{name} is missing"
        assert name not in root.__all__, f"{name} must stay out of __all__"
        assert name not in ROOT_FACADE_ALL


def test_hidden_canonical_bindings_are_the_exact_core_objects() -> None:
    """``MemoryCreate`` and ``MemoryUpdate`` are canonical Core inputs, so they get
    the same identity guarantee the advertised names do -- they are simply not
    advertised."""
    root = _root()
    assert set(HIDDEN_CANONICAL_OWNERS) == set(ROOT_FACADE_HIDDEN_CANONICAL_BINDINGS)
    for name, owner in sorted(HIDDEN_CANONICAL_OWNERS.items()):
        canonical = _import(owner)
        assert getattr(root, name) is getattr(canonical, name)
        assert getattr(root, name).__module__ == owner


def test_hidden_runtime_bindings_stay_the_exact_legacy_objects() -> None:
    """``Database`` and ``MemoryService`` are deliberately *not* canonical. Each is
    the exact object its legacy runtime module owns, and Core has no counterpart
    for either -- converting the root moved no runtime code."""
    root = _root()
    assert set(HIDDEN_RUNTIME_OWNERS) == set(ROOT_FACADE_HIDDEN_RUNTIME_BINDINGS)
    for name, owner in sorted(HIDDEN_RUNTIME_OWNERS.items()):
        legacy_owner = _import(owner)
        assert getattr(root, name) is getattr(legacy_owner, name)
        assert getattr(root, name).__module__ == owner

        canonical_twin = owner.replace(LEGACY_ROOT, CANONICAL_ROOT, 1)
        with pytest.raises(ImportError):
            importlib.import_module(canonical_twin)
        # And the canonical package that *would* hold it either does not exist at
        # all (``omnivia_core.persistence``) or exists without the name
        # (``omnivia_core.memory``). Either way there is no canonical object for
        # the root to have taken instead.
        parent_name = canonical_twin.rpartition(".")[0]
        try:
            canonical_parent: ModuleType | None = importlib.import_module(parent_name)
        except ImportError:
            canonical_parent = None
        if canonical_parent is not None:
            assert not hasattr(canonical_parent, name), (name, parent_name)


def test_no_fifth_hidden_public_binding_exists() -> None:
    """The root's complete public namespace is exactly ``__all__`` plus the four.

    Modules and private names are excluded, as the baseline export inventory
    excludes them. Anything else -- an ``annotations`` future feature, a stray
    helper, a fifth compatibility re-export -- appears here.
    """
    root = _root()
    public = {
        name
        for name, value in vars(root).items()
        if not name.startswith("_") and not isinstance(value, ModuleType)
    }
    advertised = {name for name in ROOT_FACADE_ALL if not name.startswith("_")}
    extra = sorted(public - advertised - set(ROOT_FACADE_HIDDEN_BINDINGS))
    assert extra == [], f"the root has undeclared public bindings: {extra}"
    hidden = sorted(public - advertised)
    assert hidden == list(ROOT_FACADE_HIDDEN_BINDINGS)
    assert len(hidden) == 4

    # And no dynamic widening: every one of those names is a real binding in the
    # module namespace, not something a ``__getattr__`` synthesised on demand.
    assert "__getattr__" not in vars(root)
    assert "__dir__" not in vars(root)


# ---------------------------------------------------------------------------
# The six collisions, as objects.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", sorted(ROOT_COLLISION_OWNERS), ids=sorted(ROOT_COLLISION_OWNERS)
)
def test_collision_object_is_the_named_owner_and_not_a_plausible_wrong_one(
    name: str,
) -> None:
    """A wrong collision owner still imports and is still a class of the right
    shape, so identity against the *intended* owner is not enough on its own: this
    also asserts the root's object is not the same as any plausible alternative
    domain's same-named type."""
    root = _root()
    owner, wrong_owners = ROOT_COLLISION_OWNERS[name]
    bound = getattr(root, name)
    assert bound is getattr(_import(owner), name), (
        f"{LEGACY_ROOT}.{name} is not {owner}.{name}"
    )
    for wrong in wrong_owners:
        assert bound is not getattr(_import(wrong), name), (
            f"{LEGACY_ROOT}.{name} resolved to {wrong}.{name}, the wrong domain's "
            f"same-named type"
        )


# ---------------------------------------------------------------------------
# Fresh-process import orders.
# ---------------------------------------------------------------------------


#: The two loop bodies the generated script reuses, kept as named constants so
#: the script is assembled from whole lines rather than from string fragments.
_SAME_OBJECT_LINE = (
    "    assert getattr(root, name) is"
    " getattr(importlib.import_module(owner), name), (name, owner)"
)
_DIFFERENT_OBJECT_LINE = (
    "        assert getattr(root, name) is not"
    " getattr(importlib.import_module(module), name), (name, module)"
)


def _collision_triples() -> list[tuple[str, str, tuple[str, ...]]]:
    """``(name, owner, wrong owners)`` for the six collisions, sorted by name."""
    return sorted(
        (name, owner, wrong)
        for name, (owner, wrong) in ROOT_COLLISION_OWNERS.items()
    )


def _identity_script(*, canonical_first: bool) -> str:
    """A script that imports in one order and then re-checks every identity."""
    owner_modules = sorted(set(ROOT_OWNERS.values()))
    collision_modules = sorted(
        {module for _owner, wrong in ROOT_COLLISION_OWNERS.values() for module in wrong}
        | {owner for owner, _wrong in ROOT_COLLISION_OWNERS.values()}
        | set(HIDDEN_CANONICAL_OWNERS.values())
    )
    canonical_lines = [
        f"import {module}" for module in sorted(set(owner_modules + collision_modules))
    ]
    legacy_lines = [f"import {LEGACY_ROOT}"]
    first, second = (
        (canonical_lines, legacy_lines)
        if canonical_first
        else (legacy_lines, canonical_lines)
    )
    lines = [
        _PATH_PREAMBLE.rstrip("\n"),
        "import importlib",
        *first,
        *second,
        *(f"import {module}" for module in sorted(HIDDEN_RUNTIME_OWNERS.values())),
        f"root = importlib.import_module({LEGACY_ROOT!r})",
        f"assert list(root.__all__) == {list(ROOT_FACADE_ALL)!r}",
        f"for name, owner in {sorted(ROOT_OWNERS.items())!r}:",
        _SAME_OBJECT_LINE,
        f"for name, owner in {sorted(HIDDEN_CANONICAL_OWNERS.items())!r}:",
        _SAME_OBJECT_LINE,
        f"for name, owner in {sorted(HIDDEN_RUNTIME_OWNERS.items())!r}:",
        _SAME_OBJECT_LINE,
        f"for name, owner, wrong in {_collision_triples()!r}:",
        "    for module in wrong:",
        _DIFFERENT_OBJECT_LINE,
        f"canonical = importlib.import_module({CANONICAL_ROOT!r})",
        "assert root.__version__ is canonical.__version__",
        "print('OK')",
    ]
    return "\n".join(lines)


@pytest.mark.parametrize("canonical_first", [False, True], ids=["root-first", "canonical-first"])
def test_fresh_process_import_orders_preserve_every_identity(
    canonical_first: bool,
) -> None:
    """Import order must not matter. Both directions are checked in a fresh
    interpreter, because a facade that only holds when its canonical owners are
    already in ``sys.modules`` -- or only when they are not -- would pass the
    in-process checks above on whatever order the test session happened to
    produce."""
    _run_isolated(_identity_script(canonical_first=canonical_first))


# ---------------------------------------------------------------------------
# Silence at import time.
# ---------------------------------------------------------------------------


_SILENCE_SCRIPT = (
    _PATH_PREAMBLE
    + '''
import contextlib
import io
import logging
import warnings

captured: list[logging.LogRecord] = []


class _Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        captured.append(record)


handler = _Capture()
root_logger = logging.getLogger()
root_logger.addHandler(handler)
root_logger.setLevel(logging.DEBUG)

out, err = io.StringIO(), io.StringIO()
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        import omnivia_memory

# Only warnings raised from inside the compatibility distribution count: a
# third-party dependency's own deprecation notice is not this package's doing.
ours = [
    item
    for item in caught
    if "omnivia_memory" in str(item.filename) or "omnivia_core" in str(item.filename)
]
assert not ours, [str(item.message) for item in ours]
assert not captured, [record.getMessage() for record in captured]
assert out.getvalue() == "", out.getvalue()
assert err.getvalue() == "", err.getvalue()
assert omnivia_memory.__version__
print("OK")
'''
)


def test_importing_the_root_emits_no_warning_log_or_output() -> None:
    """ADR-036 requires the deprecation notice to live in package metadata and
    guidance, never in runtime behaviour. Importing the root must therefore be
    silent: no ``warnings.warn``, no logging record, nothing on stdout or stderr.

    Checked in a fresh interpreter so a warning already triggered (and therefore
    deduplicated) by the test session cannot hide one.
    """
    result = _run_isolated(_SILENCE_SCRIPT)
    assert result.stderr == "", result.stderr


def test_root_source_contains_no_runtime_notice() -> None:
    """The source side of the same requirement, so a notice cannot be added behind
    a conditional the import above happens not to take."""
    tree = ast.parse(_root_source(), filename=str(_root_path()))
    reached = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert reached.isdisjoint({"warnings", "logging", "sys"})
    calls = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert calls == [], f"the root calls {calls} at import time"


# ---------------------------------------------------------------------------
# The canonical root stays version-only.
# ---------------------------------------------------------------------------


def test_canonical_root_is_version_only_in_the_source_checkout() -> None:
    """Why the legacy root cannot be a plain ``direct_facade``, checked rather than
    asserted in prose: ``omnivia_core`` advertises exactly one name, so there is
    nothing for a single re-export to have re-exported.

    The isolated-Core-wheel half of this claim is in
    ``tests/compatibility/test_facade_wheel_install.py``.
    """
    canonical = _import(CANONICAL_ROOT)
    assert list(canonical.__all__) == list(CANONICAL_ROOT_ALL) == ["__version__"]
    public = {
        name
        for name, value in vars(canonical).items()
        if not name.startswith("_") and not isinstance(value, ModuleType)
    }
    # ``annotations`` is the ``from __future__ import annotations`` feature object,
    # not an exported contract -- and it is exactly why the *legacy* root has no
    # future import of its own: that binding would land in its public namespace and
    # break the 187-binding partition. Nothing else may be here.
    assert public == {"annotations"}, (
        f"the canonical root gained public bindings: "
        f"{sorted(public - {'annotations'})}"
    )
    assert type(canonical.annotations).__module__ == "__future__"

    tree = ast.parse(
        canonical_root_source_path(REPO_ROOT).read_text(encoding="utf-8"),
        filename=str(canonical_root_source_path(REPO_ROOT)),
    )
    assigned = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]
    assert sorted(assigned) == ["__all__", "__version__"]
    # It advertises none of the legacy root's 182 contracts, so the two roots are
    # not two spellings of one surface.
    for name in ADVERTISED_PORTABLE:
        assert not hasattr(canonical, name), (
            f"the canonical root now exposes {name}; the legacy root's owner table "
            f"would need revisiting"
        )
