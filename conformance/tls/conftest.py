"""Session wiring for the hosted TLS conformance suite: evidence, and no skips.

Why this file exists at all
---------------------------
Two things in the packet cannot be expressed inside a test function.

**The zero-skip rule.** The owner's amendment (packet section 10a.5) is that "the
hosted suite must record zero skipped tests. An unavailable host or skipped run
retains NO-GO and does not close V06-4." A test cannot assert that *it* did not
skip, and a test that asserts the session's skip count has to run after every
other test, which is an ordering promise pytest does not make. So the rule is
enforced here, in `pytest_sessionfinish`, where the counts are final: a session
that recorded any skip, xfail or xpass fails, whatever the individual tests did.
That makes the rule mechanical rather than a convention a later edit can quietly
break -- adding a `skipif` to this tree does not produce a green run with a
smaller number in it, it produces a red one.

**The evidence artifact.** Packet section 6.5 requires a machine-readable record
preserved with the run, and the last two items in it -- collected and passed test
counts, and an explicit `skipped == 0` -- are session facts. The tests fill in
:data:`EVIDENCE` through the `evidence` fixture as they establish each fact; this
hook adds the counts and writes the file.

Where the file goes, and why not here
--------------------------------------
`OMNIVIA_TLS_CONFORMANCE_EVIDENCE` names an absolute path, and the workflow points
it at the runner's temporary directory. Nothing is written into the work tree.
That is packet section 6.7's compensating rule and owner resolution 005 R005-05:
generated keys, certificates and bundles land only in test-owned temporary
directories. The generated certificate material follows the same rule by using
pytest's `tmp_path_factory`; this artifact is the one output that has to outlive
the run, so it is the one that takes an explicit path from the environment.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

#: Facts the tests establish, written out by `pytest_sessionfinish`. Populated
#: through the `evidence` fixture rather than imported, so a test that never runs
#: contributes nothing rather than contributing a stale default.
EVIDENCE: dict[str, Any] = {}

#: Outcome tallies, counted from reports rather than from `session.items`, so a
#: test that skipped inside a fixture is counted as skipped rather than as
#: collected-and-ignored.
_OUTCOMES: Counter[str] = Counter()

EVIDENCE_PATH_VARIABLE = "OMNIVIA_TLS_CONFORMANCE_EVIDENCE"

#: Reported as a failure of the run rather than as a property of a test. `xfail`
#: and `xpass` are here with `skipped` because all three are ways a case stops
#: being a pass without being a failure, which is exactly what the owner's rule
#: forbids this suite from recording.
_FORBIDDEN_OUTCOMES = ("skipped", "xfailed", "xpassed")


@pytest.fixture(scope="session")
def evidence() -> dict[str, Any]:
    """The section 6.5 record, accumulated across the session."""
    return EVIDENCE


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Tally every phase, so a fixture-stage error is not lost.

    A setup-stage failure is the shape this suite takes when the host is absent:
    the session fixture that dials it raises, and every dependent test is reported
    as an *error* at setup. That is neither a pass nor a skip, which is the whole
    point -- an unavailable host produces a red run with zero skips, not a quiet
    one.
    """
    if report.when == "call" or report.outcome == "failed":
        _OUTCOMES[report.outcome] += 1
    if report.when == "setup" and report.outcome == "skipped":
        _OUTCOMES["skipped"] += 1
    if hasattr(report, "wasxfail"):
        _OUTCOMES["xpassed" if report.outcome == "passed" else "xfailed"] += 1


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Write the evidence record, then fail the run if anything was skipped."""
    del exitstatus
    counts = {
        "collected": session.testscollected,
        "passed": _OUTCOMES["passed"],
        "failed": _OUTCOMES["failed"],
        "skipped": _OUTCOMES["skipped"],
        "xfailed": _OUTCOMES["xfailed"],
        "xpassed": _OUTCOMES["xpassed"],
    }
    EVIDENCE["counts"] = counts
    EVIDENCE["zero_skip_rule_satisfied"] = all(
        counts[outcome] == 0 for outcome in _FORBIDDEN_OUTCOMES
    )

    destination = os.environ.get(EVIDENCE_PATH_VARIABLE, "").strip()
    if destination:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(EVIDENCE, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )

    if not EVIDENCE["zero_skip_rule_satisfied"]:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        offending = {
            outcome: counts[outcome]
            for outcome in _FORBIDDEN_OUTCOMES
            if counts[outcome]
        }
        raise pytest.UsageError(
            "the hosted TLS conformance suite recorded a non-passing, non-failing "
            f"outcome: {offending}. The owner's rule (packet 10a.5) is that this "
            "suite records zero skipped tests; a skipped run retains NO-GO and does "
            "not close V06-4. There is no third outcome."
        )
