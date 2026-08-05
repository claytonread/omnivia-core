"""Tests for the bare-run collection-completeness check.

Loads ``scripts/check-test-collection.py`` by path (its filename is not a valid
Python identifier, so it cannot be imported normally) -- the same shape
``tests/test_raise_discipline.py`` uses.

The second test is the one that matters. It does not assert the check's rule in
prose: it re-runs pytest with the ``testpaths`` the repository actually shipped
(``services`` and ``tests``), and requires the check to name every file under
``packages/`` that the run misses. If the check ever stopped detecting that, this
fails.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check-test-collection.py"

# The `testpaths` value this repository shipped before the widening: everything
# under `packages/`, `baseline/tests` and `benchmarks/tests` was outside it.
HISTORICAL_TESTPATHS = ("-o", "testpaths=services tests")


def _load_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_test_collection", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check() -> ModuleType:
    return _load_check()


def test_the_bare_run_reaches_every_test_file_in_the_checkout(check: ModuleType) -> None:
    """The live guard: `pytest` with no path arguments must collect from every
    test file on disk.

    This is what fails if `testpaths` is narrowed again, or if a distribution
    grows a `tests` tree that nothing adds to it.
    """
    assert check.run_checks(REPO_ROOT) == []


def test_the_historical_testpaths_is_reported_as_unreachable(check: ModuleType) -> None:
    """Replay the real defect and require the check to catch it.

    With `testpaths = ["services", "tests"]` the runtime and client suites are on
    disk and collected by nothing. Every one of those files must be named
    individually -- a check that only noticed the total had dropped would report
    a smaller, less actionable failure, and a ratcheting count floor would not
    have noticed at all if another tree had grown past the difference.
    """
    on_disk = check.discover_test_files(REPO_ROOT)
    collected = check.collect_test_files(REPO_ROOT, HISTORICAL_TESTPATHS)

    # The replay has to actually be narrower, or the rest proves nothing.
    assert collected, "the replayed run collected nothing; the -o override is wrong"
    assert collected < on_disk

    findings = check.compare(on_disk, collected)
    package_tests = {path for path in on_disk if path.startswith("packages/")}
    assert package_tests, "no tests under packages/; this replay no longer means anything"
    for path in sorted(package_tests):
        assert any(finding.startswith(f"{path}:") for finding in findings), path


def test_zero_collection_is_reported_as_its_own_failure(check: ModuleType) -> None:
    """The limit case gets its own message rather than a list of every file."""
    findings = check.compare({"tests/test_a.py", "packages/p/tests/test_b.py"}, set())
    assert findings == [
        "a bare `pytest` run collects no tests at all; check `testpaths` in pyproject.toml"
    ]


def test_a_fully_collected_pair_reports_nothing(check: ModuleType) -> None:
    """The defect-free base for the two rejections above. Collecting *more* than
    is on disk is not a finding: pytest reports node ids for parametrised cases
    and conftest-injected items that never correspond to a discovered file."""
    on_disk = {"tests/test_a.py"}
    assert check.compare(on_disk, on_disk) == []
    assert check.compare(on_disk, on_disk | {"tests/test_generated.py"}) == []


def test_vendored_and_cached_trees_are_not_discovered(
    check: ModuleType, tmp_path: Path
) -> None:
    """A `test_*.py` inside `.venv` or `node_modules` is a dependency's test, not
    this repository's, and pytest does not collect it either. Discovering them
    would make the check fail permanently on any developer machine."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_real.py").write_text("", encoding="utf-8")
    for vendored in (".venv/lib/x", "node_modules/pkg", "build/lib", "__pycache__"):
        directory = tmp_path / vendored
        directory.mkdir(parents=True)
        (directory / "test_vendored.py").write_text("", encoding="utf-8")

    assert check.discover_test_files(tmp_path) == {"tests/test_real.py"}


def test_both_default_pytest_filename_patterns_are_discovered(
    check: ModuleType, tmp_path: Path
) -> None:
    """`python_files` defaults to `test_*.py *_test.py`. A file named the second
    way must not be exempt from the check by accident."""
    (tmp_path / "test_leading.py").write_text("", encoding="utf-8")
    (tmp_path / "trailing_test.py").write_text("", encoding="utf-8")
    (tmp_path / "helpers.py").write_text("", encoding="utf-8")

    assert check.discover_test_files(tmp_path) == {"test_leading.py", "trailing_test.py"}
