"""Command line entry point for the Phase 0 baseline freeze.

``capture`` regenerates every tracked artifact; ``verify`` checks the working
tree against them and exits non-zero with the exact drift. ``verify-external``
validates a capture artifact produced by Platform or Dev, which is how the
recorded evidence gaps are closed.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from baseline import ARTIFACT_ROOT_ENV, FIXTURE_DIR, INVENTORY_DIR, REPO_ROOT
from baseline.dependencies import (
    verify_dependency_inventory,
    write_dependency_inventory,
)
from baseline.inventory import (
    ensure_core_importable,
    verify_public_export_inventory,
    write_public_export_inventory,
)
from baseline.legacy_db import (
    verify_legacy_fixture_inventory,
    write_legacy_fixture_inventory,
)
from baseline.scenarios import verify_fixtures, write_fixtures
from baseline.storage import (
    verify_storage_schema_inventory,
    write_storage_schema_inventory,
)
from baseline.surfaces import (
    EVIDENCE_GAPS,
    SURFACES,
    require_declared_slices,
    verify_external_capture,
    verify_surface_inventories,
    write_surface_inventories,
)

#: Internal orchestration signal. The transactional capture sets this on the one
#: child process that generates the candidate baseline, so that child writes
#: straight into its (throwaway) artifact root instead of recursing into another
#: transaction. It is never something a user sets: redirecting the artifact root
#: alone still gets the safe, transactional path.
WRITE_ONLY_ENV = "BASELINE_CAPTURE_WRITE_ONLY"

CHECKS: tuple[tuple[str, Callable[[], list[str]]], ...] = (
    ("public exports", verify_public_export_inventory),
    ("external surfaces", verify_surface_inventories),
    ("storage schema", verify_storage_schema_inventory),
    ("golden fixtures", verify_fixtures),
    ("dependencies", verify_dependency_inventory),
    ("legacy database fixture", verify_legacy_fixture_inventory),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m baseline",
        description="Capture and verify the OmniVia Core Phase 0 baseline (T-0627).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("capture", help="regenerate every tracked baseline artifact")
    subparsers.add_parser("verify", help="check the working tree against the frozen baseline")
    subparsers.add_parser("list-gaps", help="print the recorded evidence gaps")

    external = subparsers.add_parser(
        "verify-external",
        help="validate a capture artifact produced by Platform or Dev",
    )
    external.add_argument("--surface", required=True, choices=sorted(SURFACES))
    external.add_argument("--artifact", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_core_importable()
    require_declared_slices()

    if args.command == "capture":
        return _capture()
    if args.command == "verify":
        return _verify()
    if args.command == "list-gaps":
        return _list_gaps()
    return _verify_external(args.surface, args.artifact)


def _capture() -> int:
    """Regenerate the baseline transactionally, or unsafely when redirected.

    When ``BASELINE_CAPTURE_WRITE_ONLY`` is set we are the candidate-generation
    child of a transactional capture: the artifact modules already point at a
    throwaway root, so writing straight to it is correct and safe. Otherwise this
    is a user-facing ``capture`` and it must be transactional -- generate and
    verify a candidate first, and replace the accepted baseline only when green.
    """
    if os.environ.get(WRITE_ONLY_ENV):
        return _write_all_artifacts()
    return _capture_transactional()


def _write_all_artifacts() -> int:
    """Regenerate every tracked artifact in place. Never call this on the
    accepted baseline directly; it is the write half of a transaction."""
    written: list[Path] = [
        write_public_export_inventory(),
        *write_surface_inventories(),
        write_storage_schema_inventory(),
        write_dependency_inventory(),
        write_legacy_fixture_inventory(),
        *write_fixtures(),
    ]
    for path in written:
        print(f"wrote {path.relative_to(INVENTORY_DIR.parent)}")
    print(f"\n{len(written)} baseline artifact(s) regenerated.")
    return 0


def _capture_transactional() -> int:
    """Regenerate the baseline as an all-or-nothing transaction.

    A full capture is not a fixed point of ``verify`` while transitional
    facade-move evidence is active: regenerating the frozen baseline destroys the
    "before" side those checks compare against. So a naive capture overwrites a
    valid baseline and then fails the merge gate. This makes capture safe by
    construction: the accepted baseline is only ever read until a freshly
    generated candidate has passed the complete verification, and it is replaced
    with a single atomic ``os.replace`` per artifact -- never a half-written tree.
    """
    print("--- step 1/5: verify the accepted baseline against the current tree ---")
    if _verify() != 0:
        print(
            "\nRefusing to capture: the accepted baseline is not green against this "
            "tree (see the drift above). Capture must never overwrite a baseline "
            "that is already failing; fix the drift first."
        )
        return 1

    # The candidate lives beside the accepted artifacts so the final promotion is
    # a same-filesystem, atomic os.replace. ``INVENTORY_DIR.parent`` is the
    # artifact root -- the package directory normally, or the redirected root
    # under test -- so this holds in both cases.
    artifact_root = INVENTORY_DIR.parent
    staging = Path(tempfile.mkdtemp(prefix=".baseline-capture-", dir=artifact_root))
    try:
        candidate_root = staging / "candidate"
        shutil.copytree(INVENTORY_DIR, candidate_root / "inventories")
        shutil.copytree(FIXTURE_DIR, candidate_root / "fixtures")

        print("\n--- step 2/5: generate the candidate baseline into a temporary location ---")
        generated = _run_baseline_child(["capture"], candidate_root, write_only=True)
        sys.stdout.write(generated.stdout)
        sys.stderr.write(generated.stderr)
        if generated.returncode != 0:
            print("\nRefusing to capture: candidate generation failed; accepted baseline unchanged.")
            return 1

        print("\n--- step 3/5: candidate diff against the accepted baseline ---")
        print(_diff_artifact_trees(artifact_root, candidate_root))

        print("\n--- step 4/5: verify the candidate baseline ---")
        verified = _run_baseline_child(["verify"], candidate_root)
        sys.stdout.write(verified.stdout)
        sys.stderr.write(verified.stderr)
        if verified.returncode != 0:
            print(
                "\nRefusing to capture: the candidate baseline failed verification "
                "(see the failed slices above). The accepted baseline is left "
                "byte-for-byte unchanged."
            )
            return 1

        print("\n--- step 5/5: promote the verified candidate ---")
        _promote_atomically(candidate_root, artifact_root)
        print("Accepted baseline replaced with the verified candidate.")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _run_baseline_child(
    args: list[str], candidate_root: Path, *, write_only: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m baseline`` with the artifact root redirected at ``candidate_root``."""
    env = dict(os.environ)
    env[ARTIFACT_ROOT_ENV] = str(candidate_root)
    env.pop(WRITE_ONLY_ENV, None)
    if write_only:
        env[WRITE_ONLY_ENV] = "1"
    return subprocess.run(
        [sys.executable, "-m", "baseline", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _artifact_rel_files(root: Path) -> set[str]:
    """Relative paths of every artifact under ``root``'s inventories and fixtures."""
    found: set[str] = set()
    for subdir in ("inventories", "fixtures"):
        base = root / subdir
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file():
                found.add(str(path.relative_to(root)))
    return found


def _diff_artifact_trees(accepted_root: Path, candidate_root: Path, *, max_lines: int = 200) -> str:
    """Return a bounded, human-readable diff between the accepted and candidate trees."""
    accepted_files = _artifact_rel_files(accepted_root)
    candidate_files = _artifact_rel_files(candidate_root)
    lines: list[str] = []

    for rel in sorted(candidate_files - accepted_files):
        lines.append(f"added: {rel}")
    for rel in sorted(accepted_files - candidate_files):
        lines.append(f"removed: {rel}")

    for rel in sorted(accepted_files & candidate_files):
        accepted_text = (accepted_root / rel).read_text(encoding="utf-8")
        candidate_text = (candidate_root / rel).read_text(encoding="utf-8")
        if accepted_text == candidate_text:
            continue
        lines.append(f"changed: {rel}")
        diff = difflib.unified_diff(
            accepted_text.splitlines(),
            candidate_text.splitlines(),
            fromfile=f"accepted/{rel}",
            tofile=f"candidate/{rel}",
            lineterm="",
        )
        for entry in diff:
            if len(lines) >= max_lines:
                lines.append("... (diff truncated)")
                break
            lines.append(f"  {entry}")
        if len(lines) >= max_lines:
            break

    if not lines:
        return "No changes: the candidate is identical to the accepted baseline."
    return "\n".join(lines)


def _promote_atomically(candidate_root: Path, accepted_root: Path) -> None:
    """Replace the accepted artifacts with the candidate's, one atomic move each.

    Signals are ignored across the move loop so a Ctrl-C cannot interleave a
    partial promotion. Each move is an atomic same-filesystem ``os.replace``.
    """
    # ponytail: SIG_IGN only guards graceful signals in the main thread; SIGKILL
    # or a mid-loop power loss can still leave a mixed set. The loop is a handful
    # of microsecond moves after the candidate is already proven green, so this
    # is the pragmatic ceiling; a single-directory swap would need it fully
    # atomic.
    previous: dict[int, object] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError):  # not the main thread
            previous[sig] = signal.signal(sig, signal.SIG_IGN)
    try:
        accepted_files = _artifact_rel_files(accepted_root)
        candidate_files = _artifact_rel_files(candidate_root)
        for rel in sorted(candidate_files):
            destination = accepted_root / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(candidate_root / rel, destination)
        for rel in sorted(accepted_files - candidate_files):
            (accepted_root / rel).unlink()
    finally:
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError):
                signal.signal(sig, handler)


def _verify() -> int:
    failed = 0
    for name, check in CHECKS:
        problems = check()
        if problems:
            failed += 1
            print(f"FAIL {name}")
            for problem in problems:
                for line in str(problem).splitlines():
                    print(f"     {line}")
        else:
            print(f"ok   {name}")
    if failed:
        print(f"\n{failed} baseline check(s) failed.")
        return 1
    print(f"\nAll {len(CHECKS)} baseline checks passed.")
    return 0


def _list_gaps() -> int:
    for gap in sorted(EVIDENCE_GAPS, key=lambda item: item.id):
        print(f"{gap.id} [{gap.scope}] [{gap.status}] {gap.title}")
        print(f"  owner:  {gap.owner_repo}")
        print(f"  was:    {gap.why_open}")
        if gap.status == "closed":
            print(f"  closed: {gap.closed_by}")
            if gap.residual_gap_id:
                print(f"  left:   remainder tracked by {gap.residual_gap_id}")
        else:
            print(f"  closes: {gap.closes_when}")
        print()
    return 0


def _verify_external(surface: str, artifact: Path) -> int:
    problems = verify_external_capture(surface, artifact)
    if problems:
        print(f"FAIL external capture for {surface}")
        for problem in problems:
            print(f"     {problem}")
        return 1
    print(f"ok   external capture for {surface} matches the declared baseline slice")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via __main__
    sys.exit(main())
