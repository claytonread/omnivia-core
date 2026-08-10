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

The one exemption
-----------------
``conformance/`` is the single top-level tree deliberately outside a bare run.
Its suites dial a provisioned external host over TLS and are run only by
``.github/workflows/core-tls-conformance.yml`` on manual dispatch, so collecting
them locally would fail for want of a host rather than for want of a working
build. The exemption is exactly that one directory at the repository root -- not
a basename, a glob, a marker or an environment switch -- so a ``conformance``
directory anywhere else in the tree, and every other tree, stays fully
accounted for.

Exempting a tree is only safe while something else runs it, and that half is
held by ``tests/test_core_acceptance_workflow.py``: it reads
``DISPATCH_ONLY_TREES`` from this module, requires it to be exactly the
``conformance`` tree, and requires every test-bearing directory inside that tree
to be named by the dispatch workflow's one invocation. "Outside a bare run"
therefore cannot quietly become "run by nothing".

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

# Top-level trees run only by a dispatched workflow, never by a bare run. See
# "The one exemption" above. Matched against `REPO_ROOT` itself rather than
# against whatever `root` a caller passes, so this is a statement about this
# repository's layout and not a name that excuses itself wherever it appears.
DISPATCH_ONLY_TREES = frozenset({"conformance"})

# pytest's default `python_files`. Both patterns are matched so a file named the
# less common way is not silently exempt from this check.
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py")

NOTHING_COLLECTED = (
    "a bare `pytest` run collects no tests at all; check `testpaths` in pyproject.toml"
)


class CollectionFailed(Exception):
    """pytest could not complete a bare collection."""


def discover_test_files(root: Path = REPO_ROOT) -> set[str]:
    """Every test file in the checkout, as slash-separated paths from ``root``.

    A ``DISPATCH_ONLY_TREES`` entry is skipped only as a direct child of
    ``REPO_ROOT``. A caller-provided root -- a temporary directory in a test --
    that happens to contain a directory of the same name is still accounted for
    in full, and so is a same-named directory nested anywhere in this repository.
    """
    found: set[str] = set()
    stack = [root]
    while stack:
        directory = stack.pop()
        for entry in directory.iterdir():
            if entry.is_dir():
                dispatch_only = entry.parent == REPO_ROOT and entry.name in DISPATCH_ONLY_TREES
                if entry.name not in SKIPPED_DIRECTORIES and not dispatch_only:
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
