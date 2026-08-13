"""Tests for the import/install alignment check.

Loads ``scripts/check-import-install-alignment.py`` as a module (its filename is
not a valid Python identifier, so it cannot be imported normally) and exercises
it against the real repository *and* against a reconstruction of the defect it
exists to catch.

The reconstruction carries a **real** workflow, copied verbatim, so the install
list under test is the one CI actually uses rather than a restatement of it.

It reconstructed the M2 defect against ``phase2-platform.yml`` until packet
section 17b.2 added the client to that install list; 17b.3 deletes those tests
rather than replacing them, because the condition can no longer arise there. The
detection is unchanged and still proven -- now against
``core-performance-report.yml``, the one real workflow left whose collected tree
can import a local distribution its own job does not install.
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
PERFORMANCE_WORKFLOW = ".github/workflows/core-performance-report.yml"
PHASE2_TESTS = "packages/omnivia-core-runtime/tests/phase2"
PHASE2_WINDOWS_PIPE_TESTS = (
    "packages/omnivia-core-runtime/tests/phase3/protocol/"
    "test_windows_named_pipe.py"
)
BENCHMARK_TESTS = "benchmarks/tests"

# The M2 defect, in the shape it actually shipped: not a module-level import
# anyone would spot in the header, but one buried in a function body. pytest
# resolves it at collection time regardless, taking down every case in the file.
FUNCTION_LOCAL_IMPORT = """
def test_a_benchmark_reaches_for_the_runtime() -> None:
    from omnivia_core_runtime.ownership import discovery

    assert discovery is not None
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
    _write(tmp_path / PHASE2_WINDOWS_PIPE_TESTS, "")
    return tmp_path


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
    assert phase2.test_paths == (PHASE2_TESTS, PHASE2_WINDOWS_PIPE_TESTS)
    assert "packages/omnivia-core-runtime" in phase2.install_targets
    # Read out of the workflow rather than written down here. This used to assert
    # the client's *absence* -- the gap the check existed for. Packet section
    # 17b.2 closes it, so the fact is now the opposite one, and asserting it here
    # is what keeps this file honest about which workflow installs what.
    assert "packages/omnivia-core-client" in phase2.install_targets

    acceptance = jobs[ACCEPTANCE_WORKFLOW]
    assert "packages/omnivia-core-client" in acceptance.install_targets
    assert PHASE2_TESTS not in acceptance.test_paths
    assert "packages/omnivia-core-runtime/tests" in acceptance.test_paths

@pytest.fixture
def performance_root(tmp_path: Path) -> Path:
    """A miniature checkout carrying the real `core-performance-report.yml`.

    That job installs the root distribution and `omnivia-memory` only, while
    collecting `benchmarks/tests` -- the one real install gap left to prove the
    detection against.
    """
    _write(
        tmp_path / PERFORMANCE_WORKFLOW,
        (REPO_ROOT / PERFORMANCE_WORKFLOW).read_text(encoding="utf-8"),
    )
    _distribution(tmp_path, ".", "omnivia_core")
    _distribution(tmp_path, "services/omnivia-memory", "omnivia_memory")
    for name in ("runtime", "cli", "mcp", "client"):
        _distribution(tmp_path, f"packages/omnivia-core-{name}", f"omnivia_core_{name}")
    _write(tmp_path / BENCHMARK_TESTS / "conftest.py", "")
    return tmp_path


def test_an_import_the_collecting_job_does_not_install_is_caught(
    performance_root: Path,
) -> None:
    """The detection, still live, on a real workflow with a real install gap.

    Three properties, one fixture: the finding is raised; it is raised for a
    *function-local* import, the shape the original defect had and the reason
    this check walks the AST rather than reading the header; and a module outside
    the collected path is not judged.
    """
    _write(performance_root / BENCHMARK_TESTS / "test_speed.py", FUNCTION_LOCAL_IMPORT)

    findings = alignment.run_checks(performance_root)

    assert len(findings) == 1
    finding = findings[0]
    assert f"{BENCHMARK_TESTS}/test_speed.py:3" in finding
    assert "omnivia_core_runtime" in finding
    assert "packages/omnivia-core-runtime" in finding
    assert "core-performance-report.yml [performance-report]" in finding

    # Path precision: the same import outside the collected tree is not this
    # job's problem, so the finding count does not move.
    _write(performance_root / "packages/omnivia-core-runtime/tests/test_x.py",
           FUNCTION_LOCAL_IMPORT)
    assert len(alignment.run_checks(performance_root)) == 1


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
