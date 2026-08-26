"""Tests for the packaged Chat Runtime Contract v1 resource accessors.

``omnivia_core.chat_contract.v1.resources`` reads through
``importlib.resources`` against the wheel's force-included copy of
``contracts/chat/v1/{schemas,fixtures}`` (``pyproject.toml``), which only
exists once the package is built and installed. These tests substitute the
module's two lookup seams with a plain directory holding a real copy of the
canonical files, so the accessor logic is exercised without building a wheel.
The end-to-end packaged path is verified separately against a real isolated
wheel install in ``tests/chat_contract/test_wheel_resources.py``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from omnivia_core.chat_contract.v1 import resources

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CANONICAL_SCHEMA_DIR = REPO_ROOT / "contracts" / "chat" / "v1" / "schemas"
CANONICAL_FIXTURES_DIR = REPO_ROOT / "contracts" / "chat" / "v1" / "fixtures"


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


def test_all_thirteen_canonical_schemas_are_listed(packaged: Path) -> None:
    expected = sorted(p.name.removesuffix(".schema.json") for p in CANONICAL_SCHEMA_DIR.glob("*.schema.json"))
    assert len(expected) == 13
    assert resources.list_schema_names() == tuple(expected)


def test_schema_text_matches_the_canonical_source(packaged: Path) -> None:
    expected = (CANONICAL_SCHEMA_DIR / "common.schema.json").read_text(encoding="utf-8")
    assert resources.read_schema_text("common") == expected


def test_read_schema_parses_the_document(packaged: Path) -> None:
    document = resources.read_schema("common")
    assert document["$id"] == "https://contracts.omnivia.dev/chat/v1/common.schema.json"


def test_an_unknown_schema_name_is_refused_before_any_filesystem_access(packaged: Path) -> None:
    with pytest.raises(ValueError, match="unknown schema name"):
        resources.read_schema_text("not-a-real-schema")


def test_a_path_traversal_schema_name_is_refused(packaged: Path) -> None:
    with pytest.raises(ValueError):
        resources.read_schema_text("../../../etc/passwd")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def test_exactly_the_hundred_fifty_eight_governed_fixtures_are_listed(packaged: Path) -> None:
    assert len(resources.list_fixture_paths()) == 158


def test_fixture_paths_are_category_prefixed_and_sorted(packaged: Path) -> None:
    paths = resources.list_fixture_paths()
    assert paths == tuple(sorted(paths))
    categories = {path.split("/", 1)[0] for path in paths}
    assert categories == {"valid", "invalid", "traces"}


def test_fixture_text_matches_the_canonical_source(packaged: Path) -> None:
    relative = "valid/message-first-submission-success.json"
    expected = (CANONICAL_FIXTURES_DIR / relative).read_text(encoding="utf-8")
    assert resources.read_fixture_text(relative) == expected


def test_read_fixture_parses_the_document(packaged: Path) -> None:
    document = resources.read_fixture("valid/message-first-submission-success.json")
    assert document["role"] == "user"


def test_an_unknown_fixture_path_is_refused_before_any_filesystem_access(packaged: Path) -> None:
    with pytest.raises(ValueError, match="unknown fixture path"):
        resources.read_fixture_text("valid/not-packaged.json")


def test_a_path_traversal_fixture_path_is_refused(packaged: Path) -> None:
    with pytest.raises(ValueError):
        resources.read_fixture_text("../schemas/common.schema.json")


def test_the_fixture_manifest_is_not_listed_as_a_fixture(packaged: Path) -> None:
    assert all(not path.endswith("FIXTURE-MANIFEST.json") for path in resources.list_fixture_paths())


def test_read_fixture_manifest_matches_the_canonical_source(packaged: Path) -> None:
    expected = json.loads((CANONICAL_FIXTURES_DIR / "FIXTURE-MANIFEST.json").read_text(encoding="utf-8"))
    assert resources.read_fixture_manifest() == expected
