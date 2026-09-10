"""Tests for the packaged trusted-runtime v1 resource accessors.

``omnivia_core.runtime_contract.v1.resources`` reads through ``importlib.resources``
against the wheel's force-included copy of ``contracts/runtime/v1/{schemas,fixtures}``
(``pyproject.toml``), which only exists once the package is built and installed.
These tests substitute the module's two lookup seams with a plain directory holding a
real copy of the canonical files, so the accessor logic is exercised without building
a wheel. That the wheel actually carries the corpus is asserted against a real built
wheel by ``scripts/check-package-builds.sh``.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

from omnivia_core.runtime_contract.v1 import resources

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_SCHEMA_DIR = REPO_ROOT / "contracts" / "runtime" / "v1" / "schemas"
CANONICAL_FIXTURES_DIR = REPO_ROOT / "contracts" / "runtime" / "v1" / "fixtures"


@pytest.fixture()
def packaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the resource accessors at a temporary copy of the canonical files."""
    shutil.copytree(CANONICAL_SCHEMA_DIR, tmp_path / "schemas")
    shutil.copytree(CANONICAL_FIXTURES_DIR, tmp_path / "fixtures")
    monkeypatch.setattr(resources, "_schemas_root", lambda: tmp_path / "schemas")
    monkeypatch.setattr(resources, "_fixtures_root", lambda: tmp_path / "fixtures")
    return tmp_path


def test_the_single_canonical_schema_is_listed(packaged: Path) -> None:
    assert resources.list_schema_names() == ("trusted-runtime-v1",)


def test_schema_text_matches_the_canonical_source(packaged: Path) -> None:
    expected = (CANONICAL_SCHEMA_DIR / "trusted-runtime-v1.schema.json").read_text(
        encoding="utf-8"
    )
    assert resources.read_schema_text("trusted-runtime-v1") == expected


def test_the_schema_publishes_a_stable_identity(packaged: Path) -> None:
    document = resources.read_schema("trusted-runtime-v1")
    assert document["$id"] == "https://contracts.omnivia.dev/runtime/v1/trusted-runtime.schema.json"
    assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_every_packaged_case_is_listed_in_its_category(packaged: Path) -> None:
    paths = resources.list_case_paths()
    assert resources.VECTORS_NAME in paths
    assert {path.split("/", 1)[0] for path in paths} == {"valid", "invalid", "vectors"}
    assert paths == tuple(sorted(paths))


def test_a_case_reads_back_the_bytes_on_disk(packaged: Path) -> None:
    name = next(path for path in resources.list_case_paths() if path.startswith("valid/"))
    category, _, leaf = name.partition("/")
    expected = (CANONICAL_FIXTURES_DIR / category / leaf).read_text(encoding="utf-8")
    assert resources.read_case_text(name) == expected
    assert resources.read_case(name)["expected"]["outcome"] == "verified"


def test_the_vectors_document_is_reachable_by_its_own_accessor(packaged: Path) -> None:
    vectors = resources.read_vectors()
    assert vectors["vectors_version"] == "1.0"
    assert vectors["canonicalisation"] and vectors["signatures"]


def test_an_unknown_name_is_refused_before_any_filesystem_access(packaged: Path) -> None:
    with pytest.raises(ValueError, match="unknown schema name"):
        resources.read_schema_text("trusted-runtime-v2")
    with pytest.raises(ValueError, match="unknown case path"):
        resources.read_case_text("valid/../../../etc/passwd")
    with pytest.raises(ValueError, match="unknown case path"):
        resources.read_case_text("/etc/passwd")


def test_reading_the_corpus_needs_no_cryptography(packaged: Path) -> None:
    """The public contract package is standard-library only, and that bound matters here.

    A consumer reading the corpus to drive its own verifier must not be forced to
    install a signature library to *read* it -- and Core's root distribution declares
    no dependencies at all, so an import of one here would not even resolve.
    """
    tree = ast.parse(Path(resources.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".", 1)[0])
    assert imported == {"__future__", "json", "importlib", "typing"}, sorted(imported)
