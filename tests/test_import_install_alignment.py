"""Tests for the import/install alignment check.

Loads ``scripts/check-import-install-alignment.py`` as a module (its filename is
not a valid Python identifier, so it cannot be imported normally) and exercises
it against the real repository *and* against a reconstruction of the defect it
exists to catch.

The reconstruction carries the **real** ``phase2-platform.yml``, copied
verbatim, so the install list under test is the one CI actually uses rather than
a restatement of it. Its falsifier is the same tree with the client added to
that workflow's install line: the finding has to disappear, which is what
distinguishes a check that reads the install list from one that simply bans an
import.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check-import-install-alignment.py"

PHASE2_WORKFLOW = ".github/workflows/phase2-platform.yml"
ACCEPTANCE_WORKFLOW = ".github/workflows/core-acceptance.yml"
PHASE2_TESTS = "packages/omnivia-core-runtime/tests/phase2"
PHASE3_TESTS = "packages/omnivia-core-runtime/tests/phase3"

# The M2 defect, in the shape it actually shipped: not a module-level import
# anyone would spot in the header, but one buried in a function body. pytest
# resolves it at collection time regardless, taking down every case in the file.
FUNCTION_LOCAL_IMPORT = """
def test_the_real_client_accepts_the_real_descriptor() -> None:
    from omnivia_core_client.discovery import discover_service

    assert discover_service is not None
"""

MODULE_LEVEL_IMPORT = """
from omnivia_core_client.discovery import discover_service

assert discover_service is not None
"""


def _load_alignment_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_import_install_alignment", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses introspects sys.modules[cls.__module__], so the module must
    # be registered there before exec_module runs its class bodies.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


alignment = _load_alignment_module()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _distribution(root: Path, target: str, package: str) -> None:
    _write(root / target / "pyproject.toml", "[project]\nname = 'stub'\n")
    _write(root / target / "src" / package / "__init__.py", "")


@pytest.fixture
def phase2_root(tmp_path: Path) -> Path:
    """A miniature checkout carrying the real `phase2-platform.yml`."""
    _write(
        tmp_path / PHASE2_WORKFLOW,
        (REPO_ROOT / PHASE2_WORKFLOW).read_text(encoding="utf-8"),
    )
    _distribution(tmp_path, ".", "omnivia_core")
    _distribution(tmp_path, "services/omnivia-memory", "omnivia_memory")
    for name in ("runtime", "cli", "mcp", "client"):
        _distribution(tmp_path, f"packages/omnivia-core-{name}", f"omnivia_core_{name}")
    # The collected path has to exist, or the workflow's pytest argument is a
    # missing path rather than a tree to scan.
    _write(tmp_path / PHASE2_TESTS / "conftest.py", "")
    return tmp_path


def _install_the_client(root: Path) -> None:
    """Add the client to the copied workflow's install list, and nothing else."""
    path = root / PHASE2_WORKFLOW
    text = path.read_text(encoding="utf-8")
    anchor = "python -m pip install -e packages/omnivia-core-mcp"
    assert anchor in text
    updated = text.replace(
        anchor,
        f"{anchor}\n          python -m pip install -e packages/omnivia-core-client",
    )
    path.write_text(updated, encoding="utf-8")


def test_the_repository_is_aligned() -> None:
    assert alignment.run_checks() == []


def test_every_local_distribution_is_derived_from_the_tree() -> None:
    provided = {
        distribution.install_target: sorted(distribution.import_packages)
        for distribution in alignment.local_distributions(REPO_ROOT)
    }
    assert provided == {
        ".": ["omnivia_core"],
        "services/omnivia-memory": ["omnivia_memory"],
        "packages/omnivia-core-cli": ["omnivia_core_cli"],
        "packages/omnivia-core-client": ["omnivia_core_client"],
        "packages/omnivia-core-mcp": ["omnivia_core_mcp"],
        "packages/omnivia-core-runtime": ["omnivia_core_runtime"],
    }


def test_the_real_workflows_parse_into_their_install_lists_and_test_paths() -> None:
    jobs = {job.workflow: job for job in alignment.workflow_jobs(REPO_ROOT)}

    phase2 = jobs[PHASE2_WORKFLOW]
    assert phase2.name == "phase2-platform"
    assert phase2.test_paths == (PHASE2_TESTS,)
    assert "packages/omnivia-core-runtime" in phase2.install_targets
    # The gap this whole check exists for, read out of the workflow rather than
    # written down here.
    assert "packages/omnivia-core-client" not in phase2.install_targets

    acceptance = jobs[ACCEPTANCE_WORKFLOW]
    assert "packages/omnivia-core-client" in acceptance.install_targets
    assert PHASE2_TESTS not in acceptance.test_paths
    assert "packages/omnivia-core-runtime/tests" in acceptance.test_paths


def test_a_function_local_client_import_under_phase2_is_caught(
    phase2_root: Path,
) -> None:
    _write(phase2_root / PHASE2_TESTS / "test_publication.py", FUNCTION_LOCAL_IMPORT)

    findings = alignment.run_checks(phase2_root)

    assert len(findings) == 1
    finding = findings[0]
    assert f"{PHASE2_TESTS}/test_publication.py:3" in finding
    assert "omnivia_core_client" in finding
    assert "packages/omnivia-core-client" in finding
    assert "phase2-platform.yml [phase2-platform]" in finding


def test_the_same_import_is_clean_once_the_workflow_installs_the_client(
    phase2_root: Path,
) -> None:
    """The falsifier: the finding must come from the install list, not the name."""
    _write(phase2_root / PHASE2_TESTS / "test_publication.py", FUNCTION_LOCAL_IMPORT)
    assert alignment.run_checks(phase2_root) != []

    _install_the_client(phase2_root)

    assert alignment.run_checks(phase2_root) == []


def test_a_module_level_client_import_under_phase2_is_caught(
    phase2_root: Path,
) -> None:
    _write(phase2_root / PHASE2_TESTS / "test_publication.py", MODULE_LEVEL_IMPORT)

    findings = alignment.run_checks(phase2_root)

    assert len(findings) == 1
    assert f"{PHASE2_TESTS}/test_publication.py:2" in findings[0]


def test_a_module_outside_the_collected_path_is_not_judged(phase2_root: Path) -> None:
    """Path precision: phase2 names `tests/phase2`, so `tests/phase3` is not its
    problem. That is the arrangement the real repository relies on to keep the
    end-to-end client proof collectable under acceptance and not under phase2."""
    _write(phase2_root / PHASE3_TESTS / "test_client_discovery.py", MODULE_LEVEL_IMPORT)

    assert alignment.run_checks(phase2_root) == []


def test_an_installed_distribution_may_be_imported(phase2_root: Path) -> None:
    _write(
        phase2_root / PHASE2_TESTS / "test_publication.py",
        "from omnivia_core_runtime.ownership import discovery\n\nassert discovery\n",
    )

    assert alignment.run_checks(phase2_root) == []


def test_a_test_path_the_workflow_names_but_the_tree_lacks_is_reported(
    phase2_root: Path,
) -> None:
    shutil.rmtree(phase2_root / PHASE2_TESTS)

    findings = alignment.run_checks(phase2_root)

    assert len(findings) == 1
    assert PHASE2_TESTS in findings[0]
    assert "does not exist" in findings[0]


def test_a_third_party_import_is_out_of_scope(phase2_root: Path) -> None:
    """Stated bound: only local distributions are judged."""
    _write(phase2_root / PHASE2_TESTS / "test_publication.py", "import fitz\n")

    assert alignment.run_checks(phase2_root) == []
