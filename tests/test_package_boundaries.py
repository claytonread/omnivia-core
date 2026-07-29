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


def test_siblings_depend_on_core() -> None:
    assert boundaries.check_siblings_depend_on_core() == []


def test_mcp_and_cli_do_not_depend_on_runtime() -> None:
    assert boundaries.check_mcp_and_cli_do_not_depend_on_runtime() == []


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
    the two canonical directories (ADR-038) onto the importable resource path.
    """
    manifest = boundaries.load_manifest(boundaries.CORE.manifest_path)
    force_include = manifest["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include == {
        "contracts/application/v1/schemas": "omnivia_core/contracts/v1/resources/schemas",
        "contracts/application/v1/fixtures": "omnivia_core/contracts/v1/resources/fixtures",
    }
    for source in force_include:
        directory = REPO_ROOT / source
        assert directory.is_dir(), source
        non_json = sorted(path.name for path in directory.iterdir() if path.suffix != ".json")
        assert non_json == [], f"{source} holds non-JSON file(s) that would be packaged: {non_json}"
