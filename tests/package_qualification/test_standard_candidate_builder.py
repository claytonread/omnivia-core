"""Static and artifact-level guards for the V06-7 candidate builder."""

from __future__ import annotations

import ast
import importlib.util
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILDER = REPO_ROOT / "scripts" / "build-standard-candidate.py"
LIFECYCLE = REPO_ROOT / "scripts" / "run-standard-lifecycle.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "phase2-platform.yml"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("standard_candidate_builder", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _constants() -> set[str]:
    tree = ast.parse(BUILDER.read_text(encoding="utf-8"), filename=str(BUILDER))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_builder_defines_the_exact_standard_profile_and_candidate_evidence() -> None:
    constants = _constants()
    assert {
        "omnivia-core",
        "omnivia-core-runtime",
        "omnivia-core-client",
        "omnivia-core-cli",
        "omnivia-core-mcp",
        "release-manifest.json",
        "checksums-sha256.txt",
        "sbom.spdx.json",
        "third-party-licenses.json",
        "NOTICE.txt",
        "build-provenance.json",
        "signature-verification.json",
        "compatibility-matrix.json",
        "qualification-result.json",
        "standard-lifecycle-result.json",
        "unsigned",
        "--no-index",
        "--only-binary=:all:",
    } <= constants


def test_builder_imports_no_omnivia_package() -> None:
    tree = ast.parse(BUILDER.read_text(encoding="utf-8"), filename=str(BUILDER))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any(name.startswith("omnivia") for name in imports)


def test_installed_lifecycle_covers_upgrade_rollback_and_recovery() -> None:
    constants = {
        node.value
        for node in ast.walk(
            ast.parse(LIFECYCLE.read_text(encoding="utf-8"), filename=str(LIFECYCLE))
        )
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {
        "phase0_to_workspace_format_1",
        "verified_backup_before_migration",
        "corrupt_source_refused",
        "older_core_writable_access_refused",
        "silent_schema_downgrade",
        "verified_pre_upgrade_backup_restored",
        "unknown_format_refused",
        "private_paths_recorded",
        "secrets_recorded",
    } <= constants


def test_candidate_matrix_binds_all_required_compatibility_versions() -> None:
    constants = _constants()
    assert {
        "first_party_distributions",
        "core",
        "client",
        "workspace_format",
        "workspace_contract",
        "protocol",
        "api",
    } <= constants


def test_wheel_inspection_reads_identity_digest_and_embedded_license(
    tmp_path: Path,
) -> None:
    builder = _module()
    wheel = tmp_path / "sample_pkg-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "sample_pkg-1.2.3.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: sample-pkg\n"
            "Version: 1.2.3\n"
            "Requires-Python: >=3.11\n"
            "License-Expression: Apache-2.0\n"
            "Requires-Dist: dependency>=1\n\n",
        )
        archive.writestr(
            "sample_pkg-1.2.3.dist-info/licenses/LICENSE", "sample license\n"
        )

    package = builder.inspect_wheel(wheel)

    assert package.name == "sample-pkg"
    assert package.normalized_name == "sample-pkg"
    assert package.version == "1.2.3"
    assert package.requires_python == ">=3.11"
    assert package.requires_dist == ("dependency>=1",)
    assert package.declared_license == "Apache-2.0"
    assert package.license_members == (
        "sample_pkg-1.2.3.dist-info/licenses/LICENSE",
    )
    assert len(package.sha256) == 64


def test_checksum_index_covers_every_other_candidate_file_and_detects_tampering(
    tmp_path: Path,
) -> None:
    builder = _module()
    (tmp_path / "metadata").mkdir()
    (tmp_path / "wheels").mkdir()
    (tmp_path / "metadata" / "manifest.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "wheels" / "one.whl").write_bytes(b"wheel")

    builder.write_checksums(tmp_path)
    checksum = tmp_path / builder.CHECKSUMS_FILE
    lines = checksum.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert not any(builder.CHECKSUMS_FILE in line for line in lines)
    builder.verify_checksums(tmp_path)

    (tmp_path / "wheels" / "one.whl").write_bytes(b"tampered")
    with pytest.raises(builder.CandidateError, match="checksum verification failed"):
        builder.verify_checksums(tmp_path)


def test_builder_has_no_skip_or_xfail_path() -> None:
    tree = ast.parse(BUILDER.read_text(encoding="utf-8"), filename=str(BUILDER))
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not {"skip", "skipif", "xfail", "importorskip"} & attributes


def test_existing_required_matrix_runs_and_retains_the_candidate() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert workflow.count(
        "python scripts/build-standard-candidate.py --output standard-candidate"
    ) == 1
    assert "os: [ubuntu-latest, macos-latest, windows-latest]" in workflow
    assert "name: Phase 2 platform (${{ matrix.os }})" in workflow
    assert "uses: actions/upload-artifact@v6" in workflow
    assert "if-no-files-found: error" in workflow
