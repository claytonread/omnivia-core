"""Tests for the packaged Core update v1 resource accessors.

``omnivia_core.update_contract.v1.resources`` reads through ``importlib.resources``
against the wheel's force-included copy of ``contracts/update/v1/{schemas,fixtures}``
(``pyproject.toml``), which only exists once the package is built and installed.
These tests substitute the module's two lookup seams with a plain directory
holding a real copy of the canonical files, so the accessor logic is exercised
without building a wheel. That the wheel actually carries the corpus is asserted
against a real built wheel by ``scripts/check-package-builds.sh``.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

from omnivia_core.update_contract.v1 import resources

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_SCHEMA_DIR = REPO_ROOT / "contracts" / "update" / "v1" / "schemas"
CANONICAL_FIXTURES_DIR = REPO_ROOT / "contracts" / "update" / "v1" / "fixtures"


@pytest.fixture()
def packaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the resource accessors at a temporary copy of the canonical files."""
    shutil.copytree(CANONICAL_SCHEMA_DIR, tmp_path / "schemas")
    shutil.copytree(CANONICAL_FIXTURES_DIR, tmp_path / "fixtures")
    monkeypatch.setattr(resources, "_schemas_root", lambda: tmp_path / "schemas")
    monkeypatch.setattr(resources, "_fixtures_root", lambda: tmp_path / "fixtures")
    return tmp_path


def test_the_single_canonical_schema_is_listed(packaged: Path) -> None:
    assert resources.list_schema_names() == ("core-update-v1",)


def test_schema_text_matches_the_canonical_source(packaged: Path) -> None:
    expected = (CANONICAL_SCHEMA_DIR / "core-update-v1.schema.json").read_text(
        encoding="utf-8"
    )
    assert resources.read_schema_text("core-update-v1") == expected


def test_the_schema_publishes_a_stable_identity(packaged: Path) -> None:
    document = resources.read_schema("core-update-v1")
    assert document["$id"] == (
        "https://contracts.omnivia.dev/update/v1/core-update.schema.json"
    )
    assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_every_packaged_case_is_listed_under_its_subject(packaged: Path) -> None:
    paths = resources.list_case_paths()
    assert resources.VECTORS_NAME in paths
    assert {path.split("/", 1)[0] for path in paths} == {
        *resources.CASE_CATEGORIES,
        "vectors",
    }
    assert paths == tuple(sorted(paths))


@pytest.mark.parametrize("category", resources.CASE_CATEGORIES)
def test_one_subject_can_be_listed_on_its_own(packaged: Path, category: str) -> None:
    """A consumer verifying channel documents has no use for the archive cases."""
    scoped = resources.list_case_paths(category)
    assert scoped
    assert all(path.startswith(f"{category}/") for path in scoped)
    assert set(scoped) < set(resources.list_case_paths())


def test_the_categories_partition_the_corpus(packaged: Path) -> None:
    """Every case is in exactly one subject, and the vectors are in none of them."""
    scoped = [
        path
        for category in resources.CASE_CATEGORIES
        for path in resources.list_case_paths(category)
    ]
    assert len(scoped) == len(set(scoped))
    assert set(scoped) | {resources.VECTORS_NAME} == set(resources.list_case_paths())


def test_every_case_parses_and_carries_its_own_verdict(packaged: Path) -> None:
    """The corpus needs no companion expectations table, so there is not one."""
    for category in resources.CASE_CATEGORIES:
        for path in resources.list_case_paths(category):
            document = resources.read_case(path)
            assert document["case_id"] == Path(path).stem
            assert document["expected"]["outcome"]


def test_the_vectors_carry_a_version_and_the_signature_domain(packaged: Path) -> None:
    vectors = resources.read_vectors()
    assert vectors["vectors_version"] == "1.0"
    assert vectors["signature_domain"].startswith("omnivia.")
    for section in ("canonicalisation", "base64url", "signing_input", "signatures"):
        assert vectors[section], section


@pytest.mark.parametrize(
    "name",
    [
        "..",
        "../../../etc/passwd",
        "/etc/passwd",
        "core-update-v1.schema.json",
        "no-such-schema",
        "",
    ],
)
def test_a_name_that_is_not_a_packaged_schema_is_refused(
    packaged: Path, name: str
) -> None:
    """Exact membership is the whole check, which is why it needs no traversal rules."""
    with pytest.raises(ValueError):
        resources.read_schema_text(name)


@pytest.mark.parametrize(
    "name",
    [
        "..",
        "envelopes/../../../etc/passwd",
        "/etc/passwd",
        "envelopes",
        "vectors",
        "envelopes/no-such-case.json",
        "",
    ],
)
def test_a_path_that_is_not_a_packaged_case_is_refused(
    packaged: Path, name: str
) -> None:
    with pytest.raises(ValueError):
        resources.read_case_text(name)


def test_an_unknown_category_is_refused(packaged: Path) -> None:
    """``vectors`` is a real directory and is deliberately not a case category."""
    for name in ("vectors", "valid", "..", ""):
        with pytest.raises(ValueError):
            resources.list_case_paths(name)


def test_a_document_root_that_is_not_an_object_is_a_type_error(
    packaged: Path,
) -> None:
    """A corpus file that stopped being a document is not silently handed over."""
    (packaged / "fixtures" / "envelopes" / "wrong-product.json").write_text(
        "[]", encoding="utf-8"
    )
    with pytest.raises(TypeError):
        resources.read_case("envelopes/wrong-product.json")


def test_the_accessors_reach_for_nothing_but_the_standard_library() -> None:
    """Reading the published corpus must never require verifying a signature.

    A consumer in another language reads these files to learn the rules; a Python
    consumer must be able to read them with no cryptography library installed and
    no part of the implementation imported. Asserted by parsing the module rather
    than by importing it, so the claim covers lazy imports too.
    """
    source = Path(resources.__file__)
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots == {"__future__", "importlib", "json", "typing"}, sorted(roots)
