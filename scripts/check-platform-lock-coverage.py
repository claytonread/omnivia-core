#!/usr/bin/env python3
"""Fail unless this platform's two-process lock case actually ran here.

A matrix row that skips its own platform's case is worse than no row: it reports
green while proving nothing, and the check that is supposed to close the gap
becomes the evidence that it is closed. `msvcrt` byte-range locking has exactly
one dedicated case, so on Windows this script is the difference between "Windows
locking works" and "a Windows runner executed some tests".

Each platform has one case that must run and one that must skip. Both halves are
asserted: a POSIX case silently running on Windows would mean the marker stopped
selecting, which is the same defect seen from the other side.

The verdict comes from pytest's JUnit XML, not from its console prose. Matching the
literal string `"1 passed"` made the gate's answer depend on a human-readable
summary line that pytest is free to reword, and gave no way to tell "ran and
passed" from "was selected and skipped" beyond that wording.
"""

from __future__ import annotations

import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree

SUITE = Path("packages/omnivia-core-runtime/tests/phase2/test_filesystem_locking.py")

IS_WINDOWS = platform.system() == "Windows"
REQUIRED = (
    "test_fl02_windows_two_process_exclusion"
    if IS_WINDOWS
    else "test_fl01_posix_two_process_exclusion"
)
#: The other platform's case, which must be selected away rather than run here.
FOREIGN = (
    "test_fl01_posix_two_process_exclusion"
    if IS_WINDOWS
    else "test_fl02_windows_two_process_exclusion"
)


def _run(report: Path, selector: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(SUITE),
            "-q",
            "-k",
            selector,
            f"--junit-xml={report}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _outcomes(report: Path) -> dict[str, str]:
    """Test name to outcome, read from the machine-readable report."""
    if not report.is_file():
        return {}
    root = ElementTree.parse(report).getroot()
    outcomes: dict[str, str] = {}
    for case in root.iter("testcase"):
        name = case.get("name") or ""
        outcome = "passed"
        for child in case:
            if child.tag in {"skipped", "failure", "error"}:
                outcome = child.tag
                break
        outcomes[name] = outcome
    return outcomes


def main() -> int:
    if not SUITE.is_file():
        print(f"FAIL: {SUITE} is missing; run this from the repository root")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "required.xml"
        completed = _run(report, REQUIRED)
        outcomes = _outcomes(report)

        outcome = outcomes.get(REQUIRED)
        if outcome != "passed":
            print(
                f"FAIL: {REQUIRED} did not run and pass on {platform.system()} "
                f"(outcome: {outcome or 'not collected'}).\n"
                "This platform's lock semantics are unproven, which is exactly what "
                "this matrix row exists to disprove.\n\n"
                f"{completed.stdout}{completed.stderr}"
            )
            return 1

        # The other platform's case must be skipped here, not silently running.
        foreign_report = Path(tmp) / "foreign.xml"
        _run(foreign_report, FOREIGN)
        foreign_outcome = _outcomes(foreign_report).get(FOREIGN)
        if foreign_outcome != "skipped":
            print(
                f"FAIL: {FOREIGN} was expected to skip on {platform.system()} but its "
                f"outcome was {foreign_outcome or 'not collected'}. The platform "
                "markers are no longer selecting, so neither row proves what it claims."
            )
            return 1

    print(
        f"OK: {REQUIRED} ran and passed on {platform.system()}; "
        f"{FOREIGN} skipped as expected"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
