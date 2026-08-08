#!/usr/bin/env python3
"""Verify that a bare ``pytest`` run collects every test file in this checkout.

The defect this exists for
--------------------------
``testpaths`` used to read ``["services", "tests"]``. Everything under
``packages/`` -- the runtime, client, CLI and MCP distributions -- was outside
it, so a bare ``pytest -q`` collected 9358 of 13955 cases and reported a green
full-repository run. The acceptance workflow was unaffected because it names its
paths explicitly, and naming paths disables ``testpaths`` entirely, so CI and a
local run disagreed with nothing to reveal it. Lanes repeatedly quoted results
from a run that was silently skipping thousands of tests.

Why this check is structural rather than a count
------------------------------------------------
The obvious guard is a floor: fail if fewer than N tests are collected. A
literal N rots the day someone adds a test, and a floor that only ratchets
upwards still passes a run that dropped an entire tree while another tree grew
past the difference. Neither describes the actual invariant.

The invariant is: **every test file on disk is reachable from a bare run.** That
is checked directly here -- discover ``test_*.py``/``*_test.py`` on disk, ask
pytest what a bare run collects, and require the first set to be contained in the
second. It needs no number, so there is nothing to bump and nothing to rot; a
tree added outside ``testpaths`` fails on the day it lands, whatever the totals
do. It reproduces the historical defect exactly: with the old ``testpaths`` every
file under ``packages/`` is on disk and uncollected, and every one is named.

A zero-collection run is reported separately. It is the same failure in the
limit, but it usually means a broken configuration rather than a narrow one, and
saying so is more useful than listing all 134 files.

Deliberately not checked here: whether the *workflow* names every tree. That is
already held by ``tests/test_core_acceptance_workflow.py``, which discovers the
distributions under ``packages/`` from the tree and asserts each one's ``tests``
directory appears in the broad pytest command. This check is the other half --
the local invocation, which is what a developer actually trusts.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Never walked when looking for test files: version control, virtual
# environments, installed dependencies and tool caches. A `test_*.py` vendored
# inside any of them is not this repository's test and pytest does not collect it
# either -- `.venv` alone carries thousands.
SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "build",
        "dist",
        ".eggs",
    }
)

# pytest's default `python_files`. Both patterns are matched so a file named the
# less common way is not silently exempt from this check.
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py")

# Top-level trees deliberately outside every collection root, and therefore
# outside this check. There is exactly one, and widening this set is a decision
# rather than a convenience.
#
# `conformance/` holds the V06-4 hosted TLS suite. It dials a real provisioned
# host, so a bare run must not execute it -- the repository's own documented
# local command would be permanently red on any machine without that host, which
# is precisely the pressure that produces a `skipif`. And it must not skip when
# the host is absent either: the owner's rule (non-loopback TLS packet 10a.5) is
# that the hosted suite records zero skipped tests, and that an unavailable host
# retains NO-GO rather than closing V06-4. Keeping the tree out of `testpaths` is
# what keeps "not run" and "passed" different states without a marker.
#
# The invariant this check exists for is not weakened, it is completed. "Every
# test file is reachable from a bare run" becomes "every test file is reachable
# from a bare run, or is named by a workflow that runs it", and the second half
# is held by `tests/test_core_acceptance_workflow.py`, which requires every
# directory under `conformance/` containing test modules to be named by
# `.github/workflows/core-tls-conformance.yml`'s pytest invocation. A tree added
# here and named by nothing still fails -- just in the other file.
DISPATCH_ONLY_TREES = frozenset({"conformance"})

NOTHING_COLLECTED = (
    "a bare `pytest` run collects no tests at all; check `testpaths` in pyproject.toml"
)


class CollectionFailed(Exception):
    """pytest could not complete a bare collection."""


def discover_test_files(root: Path = REPO_ROOT) -> set[str]:
    """Every test file a bare run is expected to reach, as paths from ``root``.

    ``DISPATCH_ONLY_TREES`` is applied at the top level only, so a `conformance`
    directory nested inside a collected tree is still discovered -- the exemption
    is for the one named repository tree, not for the name.
    """
    found: set[str] = set()
    stack = [root]
    while stack:
        directory = stack.pop()
        for entry in directory.iterdir():
            if entry.is_dir():
                exempt = (
                    entry.parent == root and entry.name in DISPATCH_ONLY_TREES
                )
                if entry.name not in SKIPPED_DIRECTORIES and not exempt:
                    stack.append(entry)
                continue
            if any(entry.match(pattern) for pattern in TEST_FILE_PATTERNS):
                found.add(entry.relative_to(root).as_posix())
    return found


def collect_test_files(
    root: Path = REPO_ROOT, extra_arguments: Sequence[str] = ()
) -> set[str]:
    """The files a bare ``pytest`` run collects at least one test from.

    No path arguments are passed: passing any path makes pytest ignore
    ``testpaths`` altogether, which would test something other than the
    configuration under test. ``extra_arguments`` exists so a test can replay a
    different ``testpaths`` through ``-o`` and prove this check fails on it.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            *extra_arguments,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    # Exit code 5 is "no tests collected", which is a finding rather than a
    # broken harness; `run_checks` reports it from the empty result. Anything
    # else non-zero means collection itself failed and the comparison below would
    # be meaningless, so it is raised rather than reported as missing files.
    if completed.returncode not in (0, 5):
        raise CollectionFailed(
            f"`pytest --collect-only` exited {completed.returncode}\n"
            f"{completed.stdout}\n{completed.stderr}"
        )

    collected: set[str] = set()
    for line in completed.stdout.splitlines():
        node_id, separator, _ = line.partition("::")
        if separator and not node_id.startswith(" "):
            collected.add(node_id)
    return collected


def compare(on_disk: set[str], collected: set[str]) -> list[str]:
    """Findings for a discovered/collected pair. Empty means clean."""
    if not collected:
        return [NOTHING_COLLECTED]
    missing = sorted(on_disk - collected)
    if not missing:
        return []
    findings = [
        f"{path}: exists but a bare `pytest` run collects nothing from it"
        for path in missing
    ]
    findings.append(
        f"{len(missing)} test file(s) are unreachable from a bare run. Either add "
        f"the tree to `testpaths` in pyproject.toml, or delete the file if it "
        f"holds no tests."
    )
    return findings


def run_checks(
    root: Path = REPO_ROOT, extra_arguments: Sequence[str] = ()
) -> list[str]:
    return compare(discover_test_files(root), collect_test_files(root, extra_arguments))


def main() -> int:
    try:
        findings = run_checks()
    except CollectionFailed as error:
        print("OmniVia Core test collection check FAILED:\n", file=sys.stderr)
        print(f"  {error}", file=sys.stderr)
        return 1
    if findings:
        print("OmniVia Core test collection check FAILED:\n", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    print("OmniVia Core test collection check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
