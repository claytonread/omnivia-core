"""Tests for the packaged-resource accessors (ADR-038).

``omnivia_core.contracts.v1.resources`` reads through ``importlib.resources``
against the wheel's force-included copy (``pyproject.toml``), which only
exists once the package is actually built and installed -- not when running
against this checked-out source tree. These tests substitute the module's two
lookup seams (``_schemas_root`` / ``_fixtures_root``) with a plain directory
holding a real copy of the canonical files, so the accessor logic itself
(enumeration, text/JSON reads, manifest parsing) is exercised without
building a wheel. The end-to-end packaged path is verified separately by
``scripts/check-package-builds.sh`` against a real isolated wheel install.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from omnivia_core.contracts.v1 import resources

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CANONICAL_SCHEMA_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
CANONICAL_FIXTURES_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "fixtures"


@pytest.fixture()
def packaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the resource accessors at a temporary copy of the canonical files."""
    schema_dir = tmp_path / "schemas"
    fixtures_dir = tmp_path / "fixtures"
    shutil.copytree(CANONICAL_SCHEMA_DIR, schema_dir)
    shutil.copytree(CANONICAL_FIXTURES_DIR, fixtures_dir)
    monkeypatch.setattr(resources, "_schemas_root", lambda: schema_dir)
    monkeypatch.setattr(resources, "_fixtures_root", lambda: fixtures_dir)
    return tmp_path


def test_list_schema_names_matches_canonical_source(packaged: Path) -> None:
    expected = tuple(sorted(path.name.removesuffix(".schema.json") for path in CANONICAL_SCHEMA_DIR.glob("*.schema.json")))
    assert resources.list_schema_names() == expected
    assert len(expected) == 17


def test_read_schema_text_matches_canonical_source(packaged: Path) -> None:
    for name in resources.list_schema_names():
        expected = (CANONICAL_SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8")
        assert resources.read_schema_text(name) == expected


def test_read_schema_parses_json(packaged: Path) -> None:
    document = resources.read_schema("common")
    assert document["$id"] == "https://contracts.omnivia.dev/application/v1/common.schema.json"


def test_read_schema_rejects_non_object_root(packaged: Path, tmp_path: Path) -> None:
    (tmp_path / "schemas" / "not-an-object.schema.json").write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(TypeError, match="expected a JSON object"):
        resources.read_schema("not-an-object")


def test_list_fixture_files_matches_canonical_source(packaged: Path) -> None:
    expected = tuple(sorted(path.name for path in CANONICAL_FIXTURES_DIR.glob("*.json")))
    assert resources.list_fixture_files() == expected
    assert "manifest.json" in expected


def test_read_fixture_text_matches_canonical_source(packaged: Path) -> None:
    for file_name in resources.list_fixture_files():
        expected = (CANONICAL_FIXTURES_DIR / file_name).read_text(encoding="utf-8")
        assert resources.read_fixture_text(file_name) == expected


def test_read_fixture_parses_json(packaged: Path) -> None:
    document = resources.read_fixture("compatible-negotiation.json")
    assert document["metadata"]["request_id"] == "req-0001"


def test_read_fixture_manifest_matches_canonical_source(packaged: Path) -> None:
    expected = json.loads((CANONICAL_FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert resources.read_fixture_manifest() == expected


def test_read_fixture_manifest_rejects_non_object_root(packaged: Path, tmp_path: Path) -> None:
    (tmp_path / "fixtures" / "manifest.json").write_text("[]\n", encoding="utf-8")
    with pytest.raises(TypeError, match="expected a JSON object"):
        resources.read_fixture_manifest()


# --------------------------------------------------------------------------
# Name validation: only an exact packaged name is ever read, so a caller
# string can never resolve to a path outside the resource directories.
# --------------------------------------------------------------------------

# Names that are not verbatim packaged entries: traversal, separators, absolute
# paths, encoded and lookalike spellings, and the suffix the accessors append
# themselves. None may reach the filesystem.
REJECTED_NAMES: tuple[str, ...] = (
    "",
    ".",
    "..",
    "../common",
    "../../pyproject",
    "..%2Fcommon",
    "%2e%2e%2fcommon",
    "subdir/common",
    "subdir\\common",
    "/etc/passwd",
    "/absolute/common",
    "~/common",
    "common ",
    " common",
    "Common",
    "common.schema.json",
    "common\x00",
    "no-such-schema",
)


@pytest.mark.parametrize("name", REJECTED_NAMES)
def test_read_schema_text_rejects_anything_but_an_exact_packaged_name(
    name: str, packaged: Path
) -> None:
    assert name not in resources.list_schema_names()
    with pytest.raises(ValueError, match="unknown schema name"):
        resources.read_schema_text(name)


@pytest.mark.parametrize("name", REJECTED_NAMES)
def test_read_schema_rejects_anything_but_an_exact_packaged_name(
    name: str, packaged: Path
) -> None:
    with pytest.raises(ValueError, match="unknown schema name"):
        resources.read_schema(name)


@pytest.mark.parametrize(
    "file_name",
    (
        "",
        ".",
        "..",
        "../manifest.json",
        "../../pyproject.toml",
        "..%2Fmanifest.json",
        "%2e%2e%2fmanifest.json",
        "subdir/manifest.json",
        "subdir\\manifest.json",
        "/etc/passwd",
        "/absolute/manifest.json",
        "~/manifest.json",
        "manifest.json ",
        "Manifest.json",
        "manifest",
        "manifest.json\x00",
        "no-such-fixture.json",
    ),
)
def test_read_fixture_text_rejects_anything_but_an_exact_packaged_file_name(
    file_name: str, packaged: Path
) -> None:
    assert file_name not in resources.list_fixture_files()
    with pytest.raises(ValueError, match="unknown fixture file name"):
        resources.read_fixture_text(file_name)
    with pytest.raises(ValueError, match="unknown fixture file name"):
        resources.read_fixture(file_name)


def test_rejected_names_never_read_outside_the_resource_directory(
    packaged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected name must fail before any filesystem access, not merely fail to
    resolve: a sibling file that a traversal *would* have reached stays unread.
    """
    (tmp_path / "outside.schema.json").write_text('{"leaked": true}\n', encoding="utf-8")
    (tmp_path / "outside.json").write_text('{"leaked": true}\n', encoding="utf-8")

    opened: list[str] = []
    original_read_text = Path.read_text

    def _record(self: Path, *args: object, **kwargs: object) -> str:
        opened.append(str(self))
        return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _record)

    for name in ("../outside", "..", "/etc/passwd"):
        with pytest.raises(ValueError):
            resources.read_schema_text(name)
    for file_name in ("../outside.json", "..", "/etc/passwd"):
        with pytest.raises(ValueError):
            resources.read_fixture_text(file_name)

    assert opened == []


def test_every_listed_name_is_accepted(packaged: Path) -> None:
    for name in resources.list_schema_names():
        assert resources.read_schema_text(name)
    for file_name in resources.list_fixture_files():
        assert resources.read_fixture_text(file_name)
