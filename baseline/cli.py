"""Command line entry point for the Phase 0 baseline freeze.

``capture`` regenerates every tracked artifact; ``verify`` checks the working
tree against them and exits non-zero with the exact drift. ``verify-external``
validates a capture artifact produced by Platform or Dev, which is how the
recorded evidence gaps are closed.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from baseline import REPO_ROOT
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
    written: list[Path] = [
        write_public_export_inventory(),
        *write_surface_inventories(),
        write_storage_schema_inventory(),
        write_dependency_inventory(),
        write_legacy_fixture_inventory(),
        *write_fixtures(),
    ]
    for path in written:
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    print(f"\n{len(written)} baseline artifact(s) regenerated.")
    return 0


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
