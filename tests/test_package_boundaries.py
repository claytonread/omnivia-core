"""Tests for the OmniVia Core package boundary checks (ADR-036, T-0628).

Loads ``scripts/check-package-boundaries.py`` as a module (its filename is
not a valid Python identifier, so it cannot be imported normally) and
exercises its individual checks against the real repository layout.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check-package-boundaries.py"


def _load_boundary_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_package_boundaries", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses introspects sys.modules[cls.__module__], so the module must
    # be registered there before exec_module runs its class bodies.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


boundaries = _load_boundary_module()


def test_no_boundary_violations() -> None:
    assert boundaries.run_checks() == []


def test_distribution_and_import_names_are_unique() -> None:
    assert boundaries.check_unique_names() == []


def test_py_typed_markers_present() -> None:
    assert boundaries.check_py_typed_markers() == []


def test_wheel_package_selection_matches_import_package() -> None:
    assert boundaries.check_wheel_package_selection() == []


def test_versions_and_python_requirement() -> None:
    assert boundaries.check_versions_and_python_requirement() == []


def test_build_system_is_pinned_hatchling() -> None:
    assert boundaries.check_build_system() == []


def test_no_direct_reference_dependencies() -> None:
    assert boundaries.check_no_direct_reference_dependencies() == []


def test_core_has_no_sibling_or_legacy_dependency() -> None:
    assert boundaries.check_core_has_no_sibling_or_legacy_dependency() == []


def test_core_has_no_sibling_or_legacy_import() -> None:
    assert boundaries.check_core_has_no_sibling_or_legacy_import() == []


def test_no_apps_or_pro_dependency_or_import() -> None:
    assert boundaries.check_no_apps_or_pro_dependency_or_import() == []


def test_apps_and_pro_import_packages_are_exactly_those_two() -> None:
    """Pinned by name, not just count, so dropping or renaming either fails here
    rather than only showing up as a silently narrower live check."""
    assert boundaries.APPS_AND_PRO_IMPORT_PACKAGES == frozenset({"omnivia_apps", "omnivia_pro"})


def test_apps_or_pro_check_detects_a_forbidden_dependency_and_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A distribution that imports Apps or Pro, or depends on either, must be
    reported -- proving the check can actually fail, the same way the client's
    sibling check is proved against a deliberately broken copy."""
    src_root = tmp_path / "src" / "omnivia_core_probe"
    src_root.mkdir(parents=True)
    (src_root / "adapter.py").write_text(
        "import omnivia_apps\nimport omnivia_pro\n", encoding="utf-8"
    )
    manifest_path = tmp_path / "pyproject.toml"
    manifest_path.write_text(
        "[project]\n"
        'name = "omnivia-core-probe"\n'
        "dependencies = [\n"
        '  "omnivia-apps>=0.1.0",\n'
        '  "Omnivia_Pro>=0.1.0",\n'
        "]\n",
        encoding="utf-8",
    )

    broken = boundaries.PackageSpec(
        distribution_name="omnivia-core-probe",
        import_package="omnivia_core_probe",
        manifest_path=manifest_path,
        src_root=tmp_path / "src",
    )
    monkeypatch.setattr(boundaries, "ALL_PACKAGES", (broken,))

    findings = boundaries.check_no_apps_or_pro_dependency_or_import()
    # One finding per forbidden dependency (matching
    # `check_core_has_no_sibling_or_legacy_dependency`'s idiom, not the single
    # combined finding `check_client_depends_only_on_core` produces), plus one
    # for the import line naming both.
    assert len(findings) == 3
    apps_finding, pro_finding, import_finding = findings
    assert "omnivia-apps>=0.1.0" in apps_finding
    assert "Omnivia_Pro>=0.1.0" in pro_finding
    assert "omnivia_apps" in import_finding
    assert "omnivia_pro" in import_finding


def test_siblings_depend_on_core() -> None:
    assert boundaries.check_siblings_depend_on_core() == []


def test_mcp_and_cli_do_not_depend_on_runtime() -> None:
    assert boundaries.check_mcp_and_cli_do_not_depend_on_runtime() == []


def test_client_depends_only_on_core() -> None:
    assert boundaries.check_client_depends_only_on_core() == []


# --------------------------------------------------------------------------
# Enumeration of the topology itself. The checks above all iterate over
# `ALL_PACKAGES` / `SIBLINGS`, so a distribution that is never added to those
# tuples is not checked at all and every assertion above still passes. These
# pin the membership directly, and derive it from the tree so a future
# `packages/` distribution fails here rather than being silently unchecked.
# --------------------------------------------------------------------------

EXPECTED_DISTRIBUTIONS = (
    "omnivia-core",
    "omnivia-core-runtime",
    "omnivia-core-mcp",
    "omnivia-core-cli",
    "omnivia-core-client",
)


def test_every_first_class_distribution_is_enumerated() -> None:
    assert sorted(pkg.distribution_name for pkg in boundaries.ALL_PACKAGES) == sorted(
        EXPECTED_DISTRIBUTIONS
    )
    assert len(boundaries.ALL_PACKAGES) == 5


def test_every_distribution_under_packages_is_checked() -> None:
    """Discovered from the tree, not restated: adding a distribution under
    `packages/` without enumerating it in the checker fails here."""
    on_disk = sorted(
        path.parent.name
        for path in (REPO_ROOT / "packages").glob("*/pyproject.toml")
    )
    enumerated = sorted(pkg.distribution_name for pkg in boundaries.SIBLINGS)
    assert on_disk == enumerated


def test_core_is_the_only_non_sibling() -> None:
    """Core treats every `packages/` distribution -- the client included -- as a
    sibling, which is what makes the two Core-purity checks cover it."""
    assert boundaries.CORE not in boundaries.SIBLINGS
    assert boundaries.CLIENT in boundaries.SIBLINGS
    assert set(boundaries.ALL_PACKAGES) - set(boundaries.SIBLINGS) == {boundaries.CORE}


def test_client_declares_exactly_the_accepted_core_range() -> None:
    """The one dependency edge the client is allowed, pinned to the accepted range."""
    manifest = boundaries.load_manifest(boundaries.CLIENT.manifest_path)
    assert manifest["project"]["dependencies"] == [boundaries.REQUIRED_CORE_DEPENDENCY]


def test_client_forbidden_siblings_are_the_other_three_packages() -> None:
    """The client sits under runtime, MCP and CLI, never beside or above them."""
    assert sorted(pkg.distribution_name for pkg in boundaries.CLIENT_FORBIDDEN_SIBLINGS) == [
        "omnivia-core-cli",
        "omnivia-core-mcp",
        "omnivia-core-runtime",
    ]
    assert boundaries.CLIENT not in boundaries.CLIENT_FORBIDDEN_SIBLINGS


def test_client_check_detects_a_forbidden_dependency_and_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client check against a deliberately broken copy of the distribution:
    a manifest edge onto a sibling, an unrelated third-party dependency, and a
    source import of a sibling must all be reported, so a green result above
    means the check can fail. The third-party entry is the case an enumeration
    of forbidden siblings would let through -- the dependency list is required
    to be exactly the accepted core range, so any extra entry fails."""
    src_root = tmp_path / "src" / "omnivia_core_client"
    src_root.mkdir(parents=True)
    (src_root / "transport.py").write_text(
        "import omnivia_core_runtime\nimport omnivia_memory\n", encoding="utf-8"
    )
    manifest_path = tmp_path / "pyproject.toml"
    manifest_path.write_text(
        "[project]\n"
        'name = "omnivia-core-client"\n'
        "dependencies = [\n"
        '  "omnivia-core>=0.1.0,<0.2.0",\n'
        '  "omnivia-core-cli>=0.1.0",\n'
        '  "httpx>=0.27,<1",\n'
        "]\n",
        encoding="utf-8",
    )

    broken = boundaries.PackageSpec(
        distribution_name="omnivia-core-client",
        import_package="omnivia_core_client",
        manifest_path=manifest_path,
        src_root=tmp_path / "src",
    )
    monkeypatch.setattr(boundaries, "CLIENT", broken)

    findings = boundaries.check_client_depends_only_on_core()
    assert len(findings) == 2
    dependency_finding, import_finding = findings
    assert "omnivia-core-cli>=0.1.0" in dependency_finding
    assert "httpx>=0.27,<1" in dependency_finding
    assert "omnivia_core_runtime" in import_finding
    assert "omnivia_memory" in import_finding


def test_client_check_detects_a_third_party_dependency_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An otherwise clean client that grows one unrelated third-party dependency
    still fails: no sibling, no legacy package, no forbidden import, just an
    extra entry in a list that must hold the accepted core range alone."""
    src_root = tmp_path / "src" / "omnivia_core_client"
    src_root.mkdir(parents=True)
    (src_root / "transport.py").write_text("import omnivia_core\n", encoding="utf-8")
    manifest_path = tmp_path / "pyproject.toml"
    manifest_path.write_text(
        "[project]\n"
        'name = "omnivia-core-client"\n'
        f'dependencies = ["{boundaries.REQUIRED_CORE_DEPENDENCY}", "httpx>=0.27,<1"]\n',
        encoding="utf-8",
    )

    broken = boundaries.PackageSpec(
        distribution_name="omnivia-core-client",
        import_package="omnivia_core_client",
        manifest_path=manifest_path,
        src_root=tmp_path / "src",
    )
    monkeypatch.setattr(boundaries, "CLIENT", broken)

    findings = boundaries.check_client_depends_only_on_core()
    assert len(findings) == 1
    assert "httpx>=0.27,<1" in findings[0]


def test_dependency_name_parses_pinned_version_range() -> None:
    assert boundaries.dependency_name("omnivia-core>=0.1.0,<0.2.0") == "omnivia-core"


def test_dependency_name_normalizes_underscores_and_case() -> None:
    assert boundaries.dependency_name("Omnivia_Core_Runtime==0.1.0") == "omnivia-core-runtime"


def test_direct_reference_detects_file_url() -> None:
    assert boundaries._dependency_is_direct_reference("name @ file:///abs/path") is True


def test_direct_reference_detects_compact_relative_path() -> None:
    assert boundaries._dependency_is_direct_reference("name@../local") is True


def test_direct_reference_detects_absolute_path() -> None:
    assert boundaries._dependency_is_direct_reference("name @ /abs/path") is True


def test_direct_reference_detects_home_relative_path() -> None:
    assert boundaries._dependency_is_direct_reference("name @ ~/path") is True


def test_direct_reference_detects_vcs_url() -> None:
    assert (
        boundaries._dependency_is_direct_reference("name @ git+https://example.com/repo.git@main")
        is True
    )


def test_direct_reference_detects_url_with_marker() -> None:
    assert (
        boundaries._dependency_is_direct_reference(
            'name @ https://example.com/pkg.whl ; python_version < "3.12"'
        )
        is True
    )


def test_direct_reference_allows_ordinary_version_specifiers() -> None:
    assert boundaries._dependency_is_direct_reference("name>=1.0,<2.0") is False
    assert boundaries._dependency_is_direct_reference("name==1.2.3") is False
    assert boundaries._dependency_is_direct_reference("name[extra]>=1.0") is False


def test_collect_top_level_imports_detects_forbidden_import(tmp_path: Path) -> None:
    package_dir = tmp_path / "some_pkg"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text(
        "from omnivia_core_runtime import thing\n", encoding="utf-8"
    )

    imports_by_file = boundaries.collect_top_level_imports(tmp_path)
    (only_file,) = imports_by_file
    assert imports_by_file[only_file] == {"omnivia_core_runtime"}


def test_main_returns_zero_on_clean_tree() -> None:
    assert boundaries.main() == 0


# --------------------------------------------------------------------------
# Source-level counterparts of the isolated-wheel assertions in
# scripts/check-package-builds.sh, which needs a real build to run.
# --------------------------------------------------------------------------


def test_core_declares_no_runtime_dependencies_at_all() -> None:
    """Core ships standard-library only, so its wheel METADATA must carry no
    Requires-Dist. That is decided here, by the manifest declaring no dependencies --
    ``check-package-builds.sh`` asserts the built wheel actually agrees.
    """
    manifest = boundaries.load_manifest(boundaries.CORE.manifest_path)
    assert manifest["project"].get("dependencies", []) == []
    assert manifest["project"].get("optional-dependencies", {}) == {}


def test_core_wheel_force_includes_exactly_the_canonical_contract_resources() -> None:
    """The wheel's packaged resource set is exact because the force-include maps exactly
    the four canonical directories onto their importable resource paths: the two
    Application Contract v1 directories (ADR-038) and the two Host Contract v1
    directories (DOC-004 section AA).
    """
    manifest = boundaries.load_manifest(boundaries.CORE.manifest_path)
    force_include = manifest["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include == {
        "contracts/application/v1/schemas": "omnivia_core/contracts/v1/resources/schemas",
        "contracts/application/v1/fixtures": "omnivia_core/contracts/v1/resources/fixtures",
        "contracts/host/v1/schemas": "omnivia_core/host_contract/v1/resources/schemas",
        "contracts/host/v1/fixtures": "omnivia_core/host_contract/v1/resources/fixtures",
    }
    for source in force_include:
        directory = REPO_ROOT / source
        assert directory.is_dir(), source
        # Host Contract fixtures keep the governed category directories, so this
        # walks the tree rather than one level: anything that is not a JSON file
        # anywhere under a force-included root would still be packaged.
        non_json = sorted(
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file() and path.suffix != ".json"
        )
        assert non_json == [], f"{source} holds non-JSON file(s) that would be packaged: {non_json}"


def test_core_wheel_packages_the_approved_host_contract_resource_count() -> None:
    """One canonical schema and twenty governed fixtures, no more and no fewer."""
    schemas = REPO_ROOT / "contracts" / "host" / "v1" / "schemas"
    fixtures = REPO_ROOT / "contracts" / "host" / "v1" / "fixtures"
    assert sorted(path.name for path in schemas.rglob("*.json")) == [
        "host-contract-v1.schema.json"
    ]
    assert len(list(fixtures.rglob("*.json"))) == 20
