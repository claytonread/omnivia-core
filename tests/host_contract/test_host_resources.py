"""Tests for the packaged Host Contract v1 resource accessors.

``omnivia_core.host_contract.v1.resources`` reads through
``importlib.resources`` against the wheel's force-included copy of
``contracts/host/v1/{schemas,fixtures}`` (``pyproject.toml``), which only
exists once the package is built and installed. These tests substitute the
module's two lookup seams with a plain directory holding a real copy of the
canonical files, so the accessor logic is exercised without building a wheel.
The end-to-end packaged path is verified separately against a real isolated
wheel install in ``tests/host_contract/test_host_wheel_resources.py``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

import pytest

from omnivia_core.host_contract.v1 import resources

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CANONICAL_SCHEMA_DIR = REPO_ROOT / "contracts" / "host" / "v1" / "schemas"
CANONICAL_FIXTURES_DIR = REPO_ROOT / "contracts" / "host" / "v1" / "fixtures"


@pytest.fixture()
def packaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the resource accessors at a temporary copy of the canonical files."""
    shutil.copytree(CANONICAL_SCHEMA_DIR, tmp_path / "schemas")
    shutil.copytree(CANONICAL_FIXTURES_DIR, tmp_path / "fixtures")
    monkeypatch.setattr(resources, "_schemas_root", lambda: tmp_path / "schemas")
    monkeypatch.setattr(resources, "_fixtures_root", lambda: tmp_path / "fixtures")
    return tmp_path


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


def test_the_single_canonical_schema_is_listed(packaged: Path) -> None:
    assert resources.list_schema_names() == ("host-contract-v1",)


def test_schema_text_matches_the_canonical_source(packaged: Path) -> None:
    expected = (CANONICAL_SCHEMA_DIR / "host-contract-v1.schema.json").read_text(encoding="utf-8")
    assert resources.read_schema_text("host-contract-v1") == expected


def test_schema_parses_to_the_governed_identity(packaged: Path) -> None:
    document = resources.read_schema("host-contract-v1")
    assert document["$id"] == "urn:omnivia:host-contract:1.0.0"
    assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_an_unknown_schema_name_is_refused_before_any_filesystem_access(packaged: Path) -> None:
    with pytest.raises(ValueError, match="unknown schema name"):
        resources.read_schema_text("host-contract-v2")


@pytest.mark.parametrize(
    "name",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "host-contract-v1/../host-contract-v1",
        "host-contract-v1.schema.json",
        ".",
    ],
)
def test_a_traversal_shaped_schema_name_is_refused(packaged: Path, name: str) -> None:
    with pytest.raises(ValueError, match="unknown schema name"):
        resources.read_schema_text(name)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def test_every_governed_fixture_is_listed_by_its_categorised_path(packaged: Path) -> None:
    listed = resources.list_fixture_paths()
    assert len(listed) == 20
    assert listed[0] == "degradation/optional-capability-missing.json"
    assert "valid/shell-context.json" in listed
    assert "invalid/unknown-namespace.json" in listed
    assert listed == tuple(sorted(listed))


def test_fixture_text_matches_the_canonical_source(packaged: Path) -> None:
    for relative in resources.list_fixture_paths():
        expected = (CANONICAL_FIXTURES_DIR / relative).read_text(encoding="utf-8")
        assert resources.read_fixture_text(relative) == expected


def test_fixture_parses(packaged: Path) -> None:
    assert resources.read_fixture("valid/host-request.json")["namespace"] == "workspace"


def test_an_unknown_fixture_path_is_refused(packaged: Path) -> None:
    with pytest.raises(ValueError, match="unknown fixture path"):
        resources.read_fixture_text("valid/does-not-exist.json")


@pytest.mark.parametrize(
    "name",
    ["../schemas/host-contract-v1.schema.json", "/etc/passwd", "valid/../valid/host-request.json"],
)
def test_a_traversal_shaped_fixture_path_is_refused(packaged: Path, name: str) -> None:
    with pytest.raises(ValueError, match="unknown fixture path"):
        resources.read_fixture_text(name)


# --------------------------------------------------------------------------
# Governed fixture expectations
# --------------------------------------------------------------------------


def test_fixture_expectations_cover_every_packaged_fixture(packaged: Path) -> None:
    assert set(resources.FIXTURE_EXPECTATIONS) == set(resources.list_fixture_paths())


def test_fixture_expectations_transcribe_the_governed_table() -> None:
    expectations = resources.FIXTURE_EXPECTATIONS
    assert expectations["valid/host-request.json"] == "schema_valid"
    assert expectations["denial/permission-denied.json"] == "schema_valid_display_safe"
    assert expectations["degradation/optional-capability-missing.json"] == "schema_valid"
    assert expectations["invalid/unknown-namespace.json"] == "rejected"
    assert expectations["invalid/result-envelope-conflict.json"] == "rejected"
    assert expectations["invalid/environment-binding-inline-secret.json"] == "rejected"
    assert expectations["invalid/development-profile-cloud-target.json"] == "rejected"
    assert (
        expectations["invalid/forged-shell-context-request.json"]
        == "envelope_accepted_operation_denied"
    )
    assert expectations["migration/v0-display-context-to-v1.json"] == "migration_plan_only"


def test_every_expectation_is_one_of_the_governed_outcomes() -> None:
    assert set(resources.FIXTURE_EXPECTATIONS.values()) <= set(resources.FIXTURE_OUTCOMES)


def test_fixture_expectations_are_immutable() -> None:
    table = cast(dict[str, str], resources.FIXTURE_EXPECTATIONS)
    with pytest.raises(TypeError):
        table["valid/host-request.json"] = "rejected"
