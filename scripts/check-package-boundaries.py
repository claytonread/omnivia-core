#!/usr/bin/env python3
"""Verify the OmniVia Core package topology (ADR-036).

Parses each distribution's ``pyproject.toml`` (via ``tomllib``) and its
Python source tree (via the ``ast`` module) to check the compile-time
dependency direction:

                          omnivia-core
             ^          ^          ^          ^
             |          |          |          |
omnivia-core-runtime  omnivia-core-mcp  omnivia-core-cli  omnivia-core-client

Checks performed:

- distribution names and top-level import package names are unique;
- ``omnivia-core`` has no sibling or legacy dependency or import, and
  ``omnivia-core-client`` counts as a sibling like any other;
- ``omnivia-core-runtime``, ``omnivia-core-mcp``, ``omnivia-core-cli``, and
  ``omnivia-core-client`` each declare a dependency on ``omnivia-core``;
- ``omnivia-core-mcp`` and ``omnivia-core-cli`` do not depend on or import
  ``omnivia_core_runtime``;
- ``omnivia-core-client`` declares exactly one dependency -- the accepted
  ``omnivia-core`` range -- and imports no runtime, MCP, CLI, or legacy
  package;
- every package's Hatchling wheel target selects its own ``src`` package;
- every package has a ``py.typed`` marker;
- every package's build backend is ``hatchling.build`` and its build
  requirement is exactly the accepted bounded Hatchling pin;
- no package declares a direct URL/path/VCS (PEP 508 direct reference)
  dependency.

This script has no third-party dependencies; it only uses the standard
library so it can run before any package is installed.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class PackageSpec:
    """A single distribution in the OmniVia Core package topology."""

    distribution_name: str
    import_package: str
    manifest_path: Path
    src_root: Path


CORE = PackageSpec(
    distribution_name="omnivia-core",
    import_package="omnivia_core",
    manifest_path=REPO_ROOT / "pyproject.toml",
    src_root=REPO_ROOT / "src",
)
RUNTIME = PackageSpec(
    distribution_name="omnivia-core-runtime",
    import_package="omnivia_core_runtime",
    manifest_path=REPO_ROOT / "packages" / "omnivia-core-runtime" / "pyproject.toml",
    src_root=REPO_ROOT / "packages" / "omnivia-core-runtime" / "src",
)
MCP = PackageSpec(
    distribution_name="omnivia-core-mcp",
    import_package="omnivia_core_mcp",
    manifest_path=REPO_ROOT / "packages" / "omnivia-core-mcp" / "pyproject.toml",
    src_root=REPO_ROOT / "packages" / "omnivia-core-mcp" / "src",
)
CLI = PackageSpec(
    distribution_name="omnivia-core-cli",
    import_package="omnivia_core_cli",
    manifest_path=REPO_ROOT / "packages" / "omnivia-core-cli" / "pyproject.toml",
    src_root=REPO_ROOT / "packages" / "omnivia-core-cli" / "src",
)
CLIENT = PackageSpec(
    distribution_name="omnivia-core-client",
    import_package="omnivia_core_client",
    manifest_path=REPO_ROOT / "packages" / "omnivia-core-client" / "pyproject.toml",
    src_root=REPO_ROOT / "packages" / "omnivia-core-client" / "src",
)

ALL_PACKAGES: tuple[PackageSpec, ...] = (CORE, RUNTIME, MCP, CLI, CLIENT)
SIBLINGS: tuple[PackageSpec, ...] = (RUNTIME, MCP, CLI, CLIENT)

# The siblings ``omnivia-core-client`` must stay clear of. It is the shared
# protocol foundation every one of them may build on, so the dependency edge
# only ever points the other way.
CLIENT_FORBIDDEN_SIBLINGS: tuple[PackageSpec, ...] = (RUNTIME, MCP, CLI)

LEGACY_IMPORT_PACKAGES: frozenset[str] = frozenset(
    {"omnivia_memory", "omnivia_platform", "omnivia_dev", "omnivia_cloud"}
)
REQUIRED_CORE_DEPENDENCY = "omnivia-core>=0.1.0,<0.2.0"

EXPECTED_BUILD_BACKEND = "hatchling.build"
ACCEPTED_BUILD_REQUIRES: tuple[str, ...] = ("hatchling>=1.26,<2",)

_DEP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")

# PEP 508 direct references take the form ``name [extras] @ <URI reference>``.
# A URI reference is either an absolute URL with a scheme (``scheme://...``,
# including VCS forms like ``git+https://...``) or a local path (absolute, or
# relative via ``./``/``../``/``~``). Matching on the parsed URI target (not on
# spacing-sensitive substrings) also catches compact forms such as
# ``name@../local`` with no surrounding whitespace.
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_LOCAL_PATH_PREFIXES = ("/", "./", "../", "~")


def normalize_distribution_name(name: str) -> str:
    """Normalize a distribution name per PEP 503 for comparison."""
    return re.sub(r"[-_.]+", "-", name).lower()


def dependency_name(dependency: str) -> str:
    match = _DEP_NAME_RE.match(dependency.strip())
    if match is None:
        raise ValueError(f"Could not parse a dependency name from {dependency!r}")
    return normalize_distribution_name(match.group(0))


def get_str(mapping: dict[str, Any], *keys: str) -> str:
    value: Any = mapping
    for key in keys[:-1]:
        if not isinstance(value, dict):
            raise TypeError(f"Expected a table at {'.'.join(keys)!r}")
        value = value.get(key)
    if not isinstance(value, dict):
        raise TypeError(f"Expected a table at {'.'.join(keys)!r}")
    value = value.get(keys[-1])
    if not isinstance(value, str):
        raise TypeError(f"Expected a string at {'.'.join(keys)!r}")
    return value


def get_str_list(mapping: dict[str, Any], *keys: str) -> list[str]:
    value: Any = mapping
    for key in keys[:-1]:
        if not isinstance(value, dict):
            return []
        value = value.get(key)
    if not isinstance(value, dict):
        return []
    value = value.get(keys[-1])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"Expected a list of strings at {'.'.join(keys)!r}")
    return value


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def collect_top_level_imports(src_root: Path) -> dict[Path, set[str]]:
    """Map each ``.py`` file under ``src_root`` to its top-level import names."""
    imports_by_file: dict[Path, set[str]] = {}
    for py_file in sorted(src_root.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
        imports_by_file[py_file] = names
    return imports_by_file


def check_unique_names() -> list[str]:
    findings: list[str] = []

    distribution_names = [pkg.distribution_name for pkg in ALL_PACKAGES]
    if len(set(distribution_names)) != len(distribution_names):
        findings.append(f"Distribution names are not unique: {distribution_names}")

    import_names = [pkg.import_package for pkg in ALL_PACKAGES]
    if len(set(import_names)) != len(import_names):
        findings.append(f"Import package names are not unique: {import_names}")

    for pkg in ALL_PACKAGES:
        manifest = load_manifest(pkg.manifest_path)
        declared_name = normalize_distribution_name(get_str(manifest, "project", "name"))
        expected_name = normalize_distribution_name(pkg.distribution_name)
        if declared_name != expected_name:
            findings.append(
                f"{pkg.manifest_path}: project.name={declared_name!r} does not match "
                f"expected {expected_name!r}"
            )

    return findings


def check_py_typed_markers() -> list[str]:
    findings: list[str] = []
    for pkg in ALL_PACKAGES:
        marker = pkg.src_root / pkg.import_package / "py.typed"
        if not marker.is_file():
            findings.append(f"Missing py.typed marker: {marker}")
    return findings


def check_wheel_package_selection() -> list[str]:
    findings: list[str] = []
    for pkg in ALL_PACKAGES:
        manifest = load_manifest(pkg.manifest_path)
        packages = get_str_list(manifest, "tool", "hatch", "build", "targets", "wheel", "packages")
        expected = f"src/{pkg.import_package}"
        if packages != [expected]:
            findings.append(
                f"{pkg.manifest_path}: tool.hatch.build.targets.wheel.packages="
                f"{packages!r}, expected [{expected!r}]"
            )
    return findings


def check_versions_and_python_requirement() -> list[str]:
    findings: list[str] = []
    for pkg in ALL_PACKAGES:
        manifest = load_manifest(pkg.manifest_path)
        version = get_str(manifest, "project", "version")
        if version != "0.1.0":
            findings.append(f"{pkg.manifest_path}: project.version={version!r}, expected '0.1.0'")
        requires_python = get_str(manifest, "project", "requires-python")
        if requires_python != ">=3.11":
            findings.append(
                f"{pkg.manifest_path}: project.requires-python={requires_python!r}, "
                "expected '>=3.11'"
            )
    return findings


def check_core_has_no_sibling_or_legacy_dependency() -> list[str]:
    findings: list[str] = []
    manifest = load_manifest(CORE.manifest_path)
    dependencies = get_str_list(manifest, "project", "dependencies")
    sibling_names = {normalize_distribution_name(pkg.distribution_name) for pkg in SIBLINGS}
    for dep in dependencies:
        name = dependency_name(dep)
        if name in sibling_names or name.replace("-", "_") in LEGACY_IMPORT_PACKAGES:
            findings.append(f"{CORE.manifest_path}: forbidden core dependency {dep!r}")
    return findings


def check_core_has_no_sibling_or_legacy_import() -> list[str]:
    findings: list[str] = []
    forbidden = {pkg.import_package for pkg in SIBLINGS} | LEGACY_IMPORT_PACKAGES
    for py_file, names in collect_top_level_imports(CORE.src_root).items():
        hit = names & forbidden
        if hit:
            findings.append(f"{py_file}: forbidden import(s) {sorted(hit)}")
    return findings


def _dependency_is_direct_reference(dependency: str) -> bool:
    """Return True if ``dependency`` is a PEP 508 direct URL/path/VCS reference."""
    _, sep, remainder = dependency.strip().partition("@")
    if not sep:
        return False
    # Drop a trailing environment marker (`; python_version < "3.12"`), then
    # isolate the URI reference token itself.
    remainder = remainder.split(";", 1)[0].strip()
    tokens = remainder.split()
    if not tokens:
        return False
    target = tokens[0]
    if _URI_SCHEME_RE.match(target):
        return True
    return target.startswith(_LOCAL_PATH_PREFIXES)


def check_build_system() -> list[str]:
    findings: list[str] = []
    for pkg in ALL_PACKAGES:
        manifest = load_manifest(pkg.manifest_path)
        backend = get_str(manifest, "build-system", "build-backend")
        if backend != EXPECTED_BUILD_BACKEND:
            findings.append(
                f"{pkg.manifest_path}: build-system.build-backend={backend!r}, "
                f"expected {EXPECTED_BUILD_BACKEND!r}"
            )

        requires = get_str_list(manifest, "build-system", "requires")
        if requires != list(ACCEPTED_BUILD_REQUIRES):
            findings.append(
                f"{pkg.manifest_path}: build-system.requires={requires!r}, "
                f"expected {list(ACCEPTED_BUILD_REQUIRES)!r}"
            )
    return findings


def check_no_direct_reference_dependencies() -> list[str]:
    findings: list[str] = []
    for pkg in ALL_PACKAGES:
        manifest = load_manifest(pkg.manifest_path)
        dependencies = get_str_list(manifest, "project", "dependencies")
        for dep in dependencies:
            if _dependency_is_direct_reference(dep):
                findings.append(
                    f"{pkg.manifest_path}: direct-reference dependency not allowed: {dep!r}"
                )
    return findings


def check_siblings_depend_on_core() -> list[str]:
    findings: list[str] = []
    for pkg in SIBLINGS:
        manifest = load_manifest(pkg.manifest_path)
        dependencies = get_str_list(manifest, "project", "dependencies")

        core_deps = [dep for dep in dependencies if dependency_name(dep) == "omnivia-core"]
        if not core_deps:
            findings.append(f"{pkg.manifest_path}: does not declare a dependency on omnivia-core")
            continue

        if REQUIRED_CORE_DEPENDENCY not in dependencies:
            findings.append(
                f"{pkg.manifest_path}: expected dependency {REQUIRED_CORE_DEPENDENCY!r}, "
                f"found {core_deps!r}"
            )

    return findings


def check_mcp_and_cli_do_not_depend_on_runtime() -> list[str]:
    findings: list[str] = []
    for pkg in (MCP, CLI):
        manifest = load_manifest(pkg.manifest_path)
        dependencies = get_str_list(manifest, "project", "dependencies")
        for dep in dependencies:
            if dependency_name(dep) == normalize_distribution_name(RUNTIME.distribution_name):
                findings.append(f"{pkg.manifest_path}: forbidden dependency on {dep!r}")

        for py_file, names in collect_top_level_imports(pkg.src_root).items():
            if RUNTIME.import_package in names:
                findings.append(f"{py_file}: forbidden import of {RUNTIME.import_package!r}")

    return findings


def check_client_depends_only_on_core() -> list[str]:
    """``omnivia-core-client`` sits on the public contracts and on nothing else.

    Its manifest must declare that one edge and only that one: the dependency
    list has to be exactly the accepted ``omnivia-core`` range. Enumerating the
    forbidden distributions instead would pass an unrelated third-party
    dependency, which is the edge that would quietly make the shared protocol
    foundation un-vendorable. The source tree must not ``import`` the runtime,
    MCP, CLI, or legacy packages either.
    """
    findings: list[str] = []
    manifest = load_manifest(CLIENT.manifest_path)
    dependencies = get_str_list(manifest, "project", "dependencies")

    if dependencies != [REQUIRED_CORE_DEPENDENCY]:
        findings.append(
            f"{CLIENT.manifest_path}: project.dependencies={dependencies!r}, expected "
            f"exactly {[REQUIRED_CORE_DEPENDENCY]!r}"
        )

    forbidden_imports = {
        pkg.import_package for pkg in CLIENT_FORBIDDEN_SIBLINGS
    } | LEGACY_IMPORT_PACKAGES
    for py_file, names in collect_top_level_imports(CLIENT.src_root).items():
        hit = names & forbidden_imports
        if hit:
            findings.append(f"{py_file}: forbidden import(s) {sorted(hit)}")

    return findings


def run_checks() -> list[str]:
    findings: list[str] = []
    findings += check_unique_names()
    findings += check_py_typed_markers()
    findings += check_wheel_package_selection()
    findings += check_versions_and_python_requirement()
    findings += check_build_system()
    findings += check_no_direct_reference_dependencies()
    findings += check_core_has_no_sibling_or_legacy_dependency()
    findings += check_core_has_no_sibling_or_legacy_import()
    findings += check_siblings_depend_on_core()
    findings += check_mcp_and_cli_do_not_depend_on_runtime()
    findings += check_client_depends_only_on_core()
    return findings


def main() -> int:
    findings = run_checks()
    if findings:
        print("OmniVia Core package boundary check FAILED:\n", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    print("OmniVia Core package boundary check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
