"""Import-provenance guard for the client test suite.

A git worktree does **not** isolate Python imports. The shared virtualenv holds
an editable install of ``omnivia_core`` pointing at whichever checkout was
installed from, so a run inside a worktree silently tests another checkout's
source unless ``PYTHONPATH`` shadows it -- and a byte-for-byte framing suite
that passes against the wrong tree is worse than one that fails, because it
certifies bytes nobody produced here.

Rather than leave that as a note in a document nobody reads at the right
moment, this fails the run immediately and says what to set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

#: Repository root of *this* checkout: tests/ -> omnivia-core-client/ -> packages/ -> root
REPO_ROOT = Path(__file__).resolve().parents[3]

#: Packages whose source must come from this checkout for a run to mean anything.
#: Deliberately only these two: this distribution depends on the public contracts
#: and on nothing else, and a sibling appearing here at all would be the failure
#: `test_package_isolation` exists to catch.
GUARDED_PACKAGES = ("omnivia_core", "omnivia_core_client")


def _provenance() -> list[str]:
    """Packages resolving from outside this checkout, with where they came from."""
    import importlib

    wrong: list[str] = []
    for name in GUARDED_PACKAGES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            # An absent package is a different problem, and the failing import will
            # report it far more clearly than this guard could.
            continue
        origin = getattr(module, "__file__", None)
        if origin is None:
            continue
        resolved = Path(origin).resolve()
        if REPO_ROOT not in resolved.parents:
            wrong.append(f"{name} -> {resolved}")
    return wrong


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to run against another checkout's source."""
    wrong = _provenance()
    if not wrong:
        return

    expected = ":".join(
        str(REPO_ROOT / part) for part in ("packages/omnivia-core-client/src", "src")
    )
    raise pytest.UsageError(
        "these tests are importing source from outside this checkout, so their "
        "results would describe a different tree:\n  "
        + "\n  ".join(wrong)
        + f"\n\nthis checkout: {REPO_ROOT}"
        + "\nthe venv most likely holds an editable install pointing elsewhere; "
        "PYTHONPATH must shadow it:\n"
        f"\n  export PYTHONPATH={expected}\n"
    )
