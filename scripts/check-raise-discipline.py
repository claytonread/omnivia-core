#!/usr/bin/env python3
"""Verify the raise-outside-except convention on the unauthenticated seams.

``raise X from None`` inside an ``except`` block reads like suppression and is
not. It clears ``__cause__`` and sets ``__suppress_context__`` so a *rendered*
traceback stays quiet, but ``__context__`` still references the exception being
handled. Anything that logs, serializes, diffs or asserts on the error a caller
catches -- ``error.__context__`` is one attribute access away -- recovers the
original and whatever text it quoted. That has produced credential- and
payload-disclosure defects in three separate lanes, most recently where
``ipaddress.ip_address``'s ``ValueError`` quoted a caller-supplied host.

The convention that actually works, and that this repository already states in
prose and asserts in tests: set a flag or a sentinel inside the handler, let the
handler end, and raise **after** it. With no exception being handled there is
nothing to chain, so ``__context__`` is genuinely ``None``. See
``service/probes.py``'s ``_provided`` and ``service/transport.py``'s frame
decode for the established shape.

**No existing lint rule covers this.** ``ruff``'s ``B904`` and ``TRY200`` are
the only rules that touch exception chaining and both push the *opposite* way:
B904 asks for ``raise ... from err`` or ``raise ... from None`` inside an
``except``, which is satisfied by the exact construct forbidden here. Nothing in
the ``TRY``, ``BLE`` or ``B`` families mentions ``__context__``.

Scope
-----
Two trees, and deliberately not the whole repository:

- ``packages/omnivia-core-runtime/src/omnivia_core_runtime/service``
- ``packages/omnivia-core-client/src/omnivia_core_client``

These are the seams where an exception's text can reach a caller this process
has not authenticated. ``service/`` answers probes and dispatches OVC1 requests
before and around authentication; ``omnivia_core_client`` is the peer that turns
service bytes and discovery state into exceptions handed to application code.
Both already carry the convention in their module prose and assert
``__context__ is None`` in their own suites, so the rule codifies a decision
that tree has already made rather than imposing a new one.

Deliberately outside: ``src/omnivia_core`` is a contract *library* whose
exceptions are meant to quote what they rejected -- that is precisely why the
seams above translate them -- and ``services/omnivia-memory``, ``baseline`` and
``scripts`` are not on the unauthenticated path. A whole-repository rule would
fire on all of them for no disclosure gain, and a rule that fires on unrelated
code gets disabled, which is worse than no rule.

What is flagged, inside an ``except`` handler:

- ``raise X from None`` -- the defect: it claims suppression it does not do;
- ``raise X`` with no ``from`` -- the same ``__context__`` leak, undisguised.

What is not flagged: a bare ``raise`` (a re-raise carries no new exception), and
``raise X from err`` (explicit, visible chaining -- an author who wrote that
chose it, and the choice is a review question rather than a silent mistake).
``raise`` statements inside a function or class defined within the handler are
skipped: those run when the object is called, not necessarily while the handler
is on the stack.

Accepted sites
--------------
``ACCEPTED_SITES`` is a per-file ratchet, not a mute button. It records the
sites that predate this check with the count accepted for each file, so a new
one raises the count and fails, and repairing one lowers the count and *also*
fails until the entry is corrected. The debt is enumerated rather than
invisible, and it cannot rot in either direction. Fixing these needs a lane with
authority over runtime and client source; this check has none.
"""

from __future__ import annotations

import ast
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRECTORIES = frozenset({"__pycache__"})

SCOPED_TREES: tuple[str, ...] = (
    "packages/omnivia-core-runtime/src/omnivia_core_runtime/service",
    "packages/omnivia-core-client/src/omnivia_core_client",
)

RUNTIME_SERVICE = "packages/omnivia-core-runtime/src/omnivia_core_runtime/service"
CLIENT = "packages/omnivia-core-client/src/omnivia_core_client"

# path -> (accepted count, why it is accepted today)
#
# Empty, and that is the point. This started with nine entries recording the
# sites that predated the check. They were repaired in the lane that landed
# beside it, and the ratchet is what proved the repair complete: it failed
# until every entry was removed, because a count that no longer matches the
# tree is stale in either direction.
#
# Add an entry only to record a site that genuinely cannot be repaired yet,
# with the reason. An entry added to silence a finding is the failure this
# structure exists to prevent.
ACCEPTED_SITES: dict[str, tuple[int, str]] = {}


@dataclass(frozen=True)
class Site:
    """One `raise` inside an `except` that leaves `__context__` set."""

    path: Path
    lineno: int
    kind: str


def python_files(base: Path) -> list[Path]:
    """Every `.py` file at or under `base`."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRECTORIES]
        found.extend(
            Path(dirpath) / name for name in sorted(filenames) if name.endswith(".py")
        )
    return sorted(found)


def _raise_statements(node: ast.AST) -> Iterator[ast.Raise]:
    """Every `raise` that runs while the enclosing handler is active.

    Nested function, lambda and class bodies are not descended into: a `raise`
    there executes when that object is called, which is not necessarily while
    this handler is on the stack.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(
            child,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef),
        ):
            continue
        if isinstance(child, ast.Raise):
            yield child
        yield from _raise_statements(child)


def _kind(raised: ast.Raise) -> str | None:
    """What is wrong with this `raise`, or `None` if nothing is."""
    if raised.exc is None:
        return None  # bare re-raise: no new exception, nothing to chain
    if raised.cause is None:
        return "raise without `from` inside `except`"
    if isinstance(raised.cause, ast.Constant) and raised.cause.value is None:
        return "`raise ... from None` inside `except`"
    return None  # `from err`: explicit chaining, a review question not a leak


def scan_source(source: str, path: Path) -> list[Site]:
    """Every context-leaking raise in one module, ordered by line."""
    tree = ast.parse(source, filename=str(path))
    sites: dict[int, Site] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for raised in _raise_statements(node):
            kind = _kind(raised)
            # Keyed by line: a `raise` inside a handler nested in another
            # handler is reached twice and is still one site.
            if kind is not None:
                sites[raised.lineno] = Site(path, raised.lineno, kind)
    return [sites[lineno] for lineno in sorted(sites)]


def run_checks(root: Path = REPO_ROOT) -> list[str]:
    """Findings across the scoped trees. Empty means clean."""
    findings: list[str] = []
    seen: set[str] = set()
    for tree in SCOPED_TREES:
        base = root / tree
        if not base.is_dir():
            findings.append(f"{tree}: scoped tree is missing")
            continue
        for path in python_files(base):
            sites = scan_source(path.read_text(encoding="utf-8"), path)
            relative = path.relative_to(root).as_posix()
            accepted = ACCEPTED_SITES.get(relative, (0, ""))[0]
            if sites:
                seen.add(relative)
            if len(sites) > accepted:
                detail = ", ".join(
                    f"line {site.lineno} ({site.kind})" for site in sites
                )
                findings.append(
                    f"{relative}: {len(sites)} raise(s) inside `except` leave "
                    f"`__context__` set, accepted {accepted} -- {detail}. Set a "
                    f"flag in the handler and raise after it exits."
                )
            elif len(sites) < accepted:
                findings.append(
                    f"{relative}: ACCEPTED_SITES allows {accepted} but {len(sites)} "
                    f"remain -- lower the entry to match."
                )
    for relative, (accepted, _reason) in sorted(ACCEPTED_SITES.items()):
        if relative not in seen:
            findings.append(
                f"{relative}: ACCEPTED_SITES allows {accepted} but the file has "
                f"none, or is no longer in a scoped tree -- remove the entry."
            )
    return findings


def main() -> int:
    findings = run_checks()
    if findings:
        print("OmniVia Core raise discipline check FAILED:\n", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    print("OmniVia Core raise discipline check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
