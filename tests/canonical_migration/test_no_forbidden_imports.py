"""The canonical tree may import only the standard library and ``omnivia_core``.

Read with :mod:`ast` rather than by importing modules, so the check never
executes Core code and only sees what is actually written -- not whatever
happens to already be on ``sys.path``.

The dynamic-import detection (``importlib.import_module``, ``builtins.
__import__``, under any statically resolvable alias or ``getattr`` spelling,
failing closed on a non-literal target) is *reused, not reimplemented*, from
``scripts/check-application-contracts.py``'s already-tested visitor -- the
same pattern ``tests/contracts/test_check_application_contracts.py`` uses to
exercise that script, since its filename is not a valid module identifier.
Reusing it here means a future fix to that visitor's logic covers both
boundaries at once, and this test can never silently drift into a weaker
copy of it.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
CORE_SRC = SRC_ROOT / "omnivia_core"
CHECKER_SCRIPT = REPO_ROOT / "scripts" / "check-application-contracts.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_application_contracts", CHECKER_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()

#: Package prefixes this boundary explicitly names as forbidden, with why.
#: The actual gate below is the stricter allow-list required by the task
#: (stdlib + ``omnivia_core`` only, see ``_forbidden_module_reference``) --
#: this dict exists so a violation of one of these specific, named
#: boundaries is reported with a precise reason, and so each one is
#: independently exercised by ``test_named_prefix_is_rejected`` below rather
#: than only being covered incidentally by the general rule.
#:
#: ``omnivia_core_runtime``, ``omnivia_core_mcp`` and ``omnivia_core_cli`` are
#: Core's own separate distributions, published from ``packages/`` in this
#: repo -- they are not Dev-owned, and naming them here is not a statement
#: about which Module owns them. They are forbidden because the dependency
#: only ever runs the other way: those distributions depend on the
#: ``omnivia_core`` library, so an import back from the library would invert
#: the layering and make the standalone-installable core package unusable
#: without them.
NAMED_FORBIDDEN_PREFIXES: dict[str, str] = {
    "omnivia_memory": "Canonical Core must not depend on the legacy runtime package.",
    "omnivia_core_runtime": (
        "omnivia-core-runtime is a separate Core distribution that depends on this "
        "library; the library must not depend back on it."
    ),
    "omnivia_core_mcp": (
        "omnivia-core-mcp is a separate Core distribution that depends on this "
        "library; the library must not depend back on it."
    ),
    "omnivia_core_cli": (
        "omnivia-core-cli is a separate Core distribution that depends on this "
        "library; the library must not depend back on it."
    ),
    "sqlalchemy": "Canonical Core must stay persistence-framework free.",
    "omnivia_platform": "Core must not depend on private Platform implementation code.",
    "omnivia_apps": "Core must not depend on private Apps implementation code.",
    "omnivia_dev": "Core must not depend on private Dev implementation code.",
    "omnivia_pro": "Core must not depend on private Pro (paid Module) implementation code.",
    "omnivia_cloud": "Core must not depend on private Cloud (paid Module) implementation code.",
}

#: import package -> the sibling Core distribution in ``packages/`` that
#: provides it. Keeps the reasons above honest: these are Core's own
#: separate distributions, not another Module's surface.
SIBLING_CORE_DISTRIBUTIONS: dict[str, str] = {
    "omnivia_core_runtime": "omnivia-core-runtime",
    "omnivia_core_mcp": "omnivia-core-mcp",
    "omnivia_core_cli": "omnivia-core-cli",
}


def _forbidden_module_reference(module: str, stdlib: frozenset[str]) -> str | None:
    """Return why ``module`` is forbidden, or ``None`` if it is allowed.

    Canonical Core may import the standard library and ``omnivia_core``
    (any submodule) -- nothing else. This is an allow-list, not merely a
    deny-list of the explicitly named prefixes: an import of some other,
    unnamed non-stdlib package is just as forbidden, and is reported with a
    generic reason instead of a named one.
    """
    top = module.split(".")[0]
    if top == "omnivia_core":
        return None
    if top in stdlib:
        return None
    named_reason = NAMED_FORBIDDEN_PREFIXES.get(top)
    if named_reason:
        return f"{named_reason} (imports {module!r})"
    return f"forbidden import {module!r}: canonical Core may only import the standard library and omnivia_core"


def _check_file_imports(py_file: Path, stdlib: frozenset[str]) -> list[str]:
    """Report every forbidden-import boundary violation in one file.

    Mirrors ``check-application-contracts.py``'s ``_check_file_imports``
    exactly, with only the allow-list swapped for this package's own
    (``omnivia_core`` entirely, vs. that script's ``omnivia_core.contracts``
    only): a forbidden absolute import, a relative import that resolves
    outside ``omnivia_core``, or a constant-string dynamic import
    (``__import__`` / ``importlib.import_module``, under any statically
    resolvable alias or literal ``getattr`` spelling) that escapes it. A
    dynamic import whose target cannot be proven statically -- a computed
    string, an ambiguous rebinding -- fails closed rather than being
    assumed safe.
    """
    findings: list[str] = []
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    references = checker._resolve_references(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                reason = _forbidden_module_reference(alias.name, stdlib)
                if reason:
                    findings.append(f"{py_file}: {reason}")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    reason = _forbidden_module_reference(node.module, stdlib)
                    if reason:
                        findings.append(f"{py_file}: {reason}")
                continue
            resolved = checker._resolve_relative_import(py_file, node.level, node.module)
            if resolved is None:
                dots = "." * node.level
                findings.append(
                    f"{py_file}: relative import {dots}{node.module or ''} escapes above omnivia_core"
                )
                continue
            reason = _forbidden_module_reference(resolved, stdlib)
            if reason:
                findings.append(f"{py_file}: forbidden relative import resolving to {resolved!r}")
        elif isinstance(node, ast.Call) and checker._is_dynamic_import_call(node, references):
            first_arg = node.args[0] if node.args else None
            if not (isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str)):
                findings.append(
                    f"{py_file}: dynamic import with a non-literal argument cannot be "
                    "verified to stay within stdlib + omnivia_core"
                )
                continue
            reason = _forbidden_module_reference(first_arg.value, stdlib)
            if reason:
                findings.append(f"{py_file}: dynamic import escape, {reason}")
    return findings


def test_canonical_tree_has_no_forbidden_imports() -> None:
    stdlib = frozenset(sys.stdlib_module_names) | {"__future__"}
    problems: list[str] = []
    for path in sorted(CORE_SRC.rglob("*.py")):
        problems.extend(_check_file_imports(path, stdlib))
    assert not problems, "\n".join(problems)


def test_named_forbidden_prefixes_are_actually_covered_by_the_allow_list() -> None:
    """Each explicitly named prefix really is rejected by the general rule,
    not just documented -- catches ``NAMED_FORBIDDEN_PREFIXES`` drifting out
    of sync with the allow-list it exists to annotate."""
    stdlib = frozenset(sys.stdlib_module_names) | {"__future__"}
    for prefix in NAMED_FORBIDDEN_PREFIXES:
        assert _forbidden_module_reference(prefix, stdlib) is not None, (
            f"{prefix!r} is listed as forbidden but the allow-list would accept it"
        )
        assert _forbidden_module_reference(f"{prefix}.submodule", stdlib) is not None


def test_named_core_distributions_are_real_sibling_packages_in_this_repo() -> None:
    """The reasons above claim ``omnivia_core_runtime``/``_mcp``/``_cli`` are
    separate *Core* distributions in this repo, forbidden because they depend
    on this library rather than because another Module owns them. Check that
    claim against ``packages/`` so the explanation cannot rot into a wrong
    one -- if these ever stop being Core's own distributions, the reasons
    need rewriting, not just the paths."""
    for import_package, distribution in SIBLING_CORE_DISTRIBUTIONS.items():
        package_root = REPO_ROOT / "packages" / distribution
        assert (package_root / "pyproject.toml").is_file(), (
            f"{distribution} is described as a sibling Core distribution but "
            f"{package_root}/pyproject.toml does not exist"
        )
        assert (package_root / "src" / import_package).is_dir(), (
            f"{distribution} is described as providing the {import_package!r} "
            f"import package, but {package_root}/src/{import_package} does not exist"
        )
        assert import_package in NAMED_FORBIDDEN_PREFIXES


def _findings_for_source(tmp_path: Path, source: str) -> list[str]:
    py_file = tmp_path / "probe.py"
    py_file.write_text(source, encoding="utf-8")
    stdlib = frozenset(sys.stdlib_module_names) | {"__future__"}
    return _check_file_imports(py_file, stdlib)


def test_direct_import_of_forbidden_package_is_rejected(tmp_path: Path) -> None:
    assert _findings_for_source(tmp_path, "import omnivia_memory\n")


def test_direct_from_import_of_forbidden_package_is_rejected(tmp_path: Path) -> None:
    assert _findings_for_source(tmp_path, "from sqlalchemy import Column\n")


def test_aliased_import_of_forbidden_package_is_rejected(tmp_path: Path) -> None:
    assert _findings_for_source(tmp_path, "import omnivia_memory as legacy\n")


def test_importlib_import_module_of_forbidden_package_is_rejected(tmp_path: Path) -> None:
    assert _findings_for_source(tmp_path, "import importlib\nimportlib.import_module('omnivia_memory')\n")


def test_importlib_aliased_module_is_rejected(tmp_path: Path) -> None:
    assert _findings_for_source(
        tmp_path, "import importlib as il\nil.import_module('omnivia_memory.lifecycle')\n"
    )


def test_builtins_dunder_import_of_forbidden_package_is_rejected(tmp_path: Path) -> None:
    assert _findings_for_source(
        tmp_path, "import builtins\nbuiltins.__import__('omnivia_platform')\n"
    )


def test_chained_assignment_alias_of_importer_is_rejected(tmp_path: Path) -> None:
    assert _findings_for_source(
        tmp_path,
        "import importlib\n"
        "first = importlib.import_module\n"
        "load = first\n"
        "load('omnivia_memory')\n",
    )


def test_getattr_literal_spelling_of_importer_is_rejected(tmp_path: Path) -> None:
    assert _findings_for_source(
        tmp_path,
        "import importlib\n"
        "getattr(importlib, 'import_module')('omnivia_memory')\n",
    )


def test_bare_import_module_name_is_treated_as_dynamic_import(tmp_path: Path) -> None:
    """``import_module(...)`` spelled exactly like the escape hatch, with no
    visible binding in this file, fails closed rather than being assumed
    harmless."""
    assert _findings_for_source(tmp_path, "import_module('omnivia_memory')\n")


def test_dynamic_import_with_nonliteral_target_fails_closed(tmp_path: Path) -> None:
    assert _findings_for_source(
        tmp_path,
        "import importlib\n"
        "name = 'omnivia_' + 'memory'\n"
        "importlib.import_module(name)\n",
    )


def test_dynamic_import_of_stdlib_is_allowed(tmp_path: Path) -> None:
    assert _findings_for_source(tmp_path, "import importlib\nimportlib.import_module('dataclasses')\n") == []


def test_dynamic_import_of_omnivia_core_is_allowed(tmp_path: Path) -> None:
    assert (
        _findings_for_source(
            tmp_path, "import importlib\nimportlib.import_module('omnivia_core.lifecycle.models')\n"
        )
        == []
    )


def test_ordinary_stdlib_and_omnivia_core_imports_are_allowed(tmp_path: Path) -> None:
    assert (
        _findings_for_source(
            tmp_path,
            "from __future__ import annotations\n"
            "import dataclasses\n"
            "from enum import Enum\n"
            "from omnivia_core.lifecycle.models import LifecycleState\n",
        )
        == []
    )
