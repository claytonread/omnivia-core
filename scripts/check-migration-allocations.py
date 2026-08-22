#!/usr/bin/env python3
"""Hold the T-0660 migration number allocation and fail closed on collision or drift.

Migration numbers are a single global sequence in one directory, and several
lanes are queued behind the same next number. Two lanes that each picked "the
next free one" independently produce two files called `0021_*.sql`, which is not
a merge conflict -- both files exist, `load_migrations()` finds duplicate
versions, and the collision is discovered by a workspace that cannot migrate.

`contracts/migrations/v1/allocations.json` is the authority that assigns each
number ahead of time. This script is the gate that keeps the repository and that
authority in agreement, and it is deliberately narrow: it checks *this*
repository's canonical authority file against *this* repository's migration
directory. It takes no path argument, because a checker that accepts its own
oracle from the command line proves whatever it is handed.

What it enforces:

* the authority is well-formed -- exact field sets, no extras, correct types;
* numbers run consecutively from the accepted predecessor with no gap, no
  duplicate and no reordering, and each entry links to the number before it;
* a filename names its own number (`0021_*.sql` for migration 21) and no
  filename is allocated twice;
* a `candidate` entry pins a 64-character content hash and a 40-character
  introducing commit, and the file on disk exists with exactly that content;
* a `reserved` entry pins neither, and its file is *absent* -- a reserved
  number is an allocation, not a file, until the authority advances it;
* `accepted_commit` is null everywhere, because no entry has been accepted yet;
* every `.sql` in the migration directory numbered above the accepted
  predecessor is allocated here.

What it deliberately does not do: it does not restate the allocation table. The
authority is the source of truth for which lane owns which number, and
`tests/test_migration_allocations.py` pins that exact table -- owners included --
as an independently reviewed second copy, the same way that file pins the
acceptance workflow's lint scopes.

Standard library only, so it runs before anything is installed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTHORITY = REPO_ROOT / "contracts" / "migrations" / "v1" / "allocations.json"
MIGRATION_DIR = (
    REPO_ROOT
    / "packages"
    / "omnivia-core-runtime"
    / "src"
    / "omnivia_core_runtime"
    / "storage"
    / "migration_files"
)

SCHEMA_VERSION = 1
CANDIDATE = "candidate"
RESERVED = "reserved"

TOP_LEVEL_FIELDS = (
    "schema_version",
    "decision",
    "frozen_source_head",
    "reserved_rule",
    "accepted_predecessor",
    "allocations",
)
PREDECESSOR_FIELDS = ("number", "filename")
ALLOCATION_FIELDS = (
    "number",
    "filename",
    "owner",
    "repository",
    "state",
    "predecessor",
    "sha256",
    "introduced_commit",
    "accepted_commit",
)

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _is_int(value: object) -> bool:
    """True for a JSON integer. `bool` is excluded: `True == 1` in Python."""
    return isinstance(value, int) and not isinstance(value, bool)


def _exact_fields(
    value: object, required: tuple[str, ...], label: str, findings: list[str]
) -> dict[str, Any] | None:
    """The object at `value` with exactly `required` as its keys, or None."""
    if not isinstance(value, dict):
        findings.append(f"{label}: expected an object, got {type(value).__name__}")
        return None
    missing = sorted(set(required) - set(value))
    extra = sorted(set(value) - set(required))
    if missing or extra:
        findings.append(f"{label}: field drift, missing={missing}, extra={extra}")
        return None
    return value


def _text_field(entry: Mapping[str, Any], name: str, label: str, findings: list[str]) -> None:
    value = entry[name]
    if not isinstance(value, str) or not value.strip():
        findings.append(f"{label}: {name} must be a non-empty string, got {value!r}")


def _check_naming(filename: str, number: int, label: str, findings: list[str]) -> None:
    """A filename must be `NNNN_name.sql` and `NNNN` must be its own number."""
    prefix, separator, slug = filename[: -len(".sql")].partition("_")
    if not separator or not slug or len(prefix) != 4 or not prefix.isdigit():
        findings.append(f"{label}: filename {filename!r} is not of the form NNNN_name.sql")
    elif int(prefix) != number:
        findings.append(f"{label}: filename {filename!r} does not name migration {number:04d}")


def _check_state(
    entry: Mapping[str, Any],
    filename: str,
    label: str,
    present: Mapping[str, str],
    findings: list[str],
) -> None:
    """The state/commit/hash combination, and the file's presence on disk."""
    state = entry["state"]
    digest = entry["sha256"]
    introduced = entry["introduced_commit"]

    if entry["accepted_commit"] is not None:
        findings.append(
            f"{label}: accepted_commit must be null until the allocation is accepted, "
            f"got {entry['accepted_commit']!r}"
        )

    if state == CANDIDATE:
        pinned = isinstance(digest, str) and _DIGEST.match(digest) is not None
        if not pinned:
            findings.append(f"{label}: a candidate must pin a 64-character sha256, got {digest!r}")
        if not isinstance(introduced, str) or not _COMMIT.match(introduced):
            findings.append(
                f"{label}: a candidate must pin a 40-character introducing commit, "
                f"got {introduced!r}"
            )
        if filename not in present:
            findings.append(f"{label}: candidate file is missing from the migration directory")
        elif pinned and present[filename] != digest:
            findings.append(
                f"{label}: {filename} content drifted -- the authority pins "
                f"{str(digest)[:12]}…, the file hashes to {present[filename][:12]}…"
            )
    elif state == RESERVED:
        if digest is not None or introduced is not None:
            findings.append(
                f"{label}: a reserved allocation must pin neither sha256 nor "
                f"introduced_commit, got {digest!r} and {introduced!r}"
            )
        if filename in present:
            findings.append(
                f"{label}: reserved migration {filename} already exists; advance this "
                "allocation's state deliberately before the file appears"
            )
    else:
        findings.append(f"{label}: state must be {CANDIDATE!r} or {RESERVED!r}, got {state!r}")


def check(text: str, present: Mapping[str, str]) -> list[str]:
    """Every way `text` and the migration directory `present` describes disagree.

    `present` maps each `.sql` filename in the migration directory to the SHA-256
    of its bytes. Pure, so the mutation suite can exercise every failure class
    without writing to the repository.
    """
    findings: list[str] = []

    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        return [f"authority is not valid JSON: {error}"]

    root = _exact_fields(document, TOP_LEVEL_FIELDS, "authority", findings)
    if root is None:
        return findings

    if not _is_int(root["schema_version"]) or root["schema_version"] != SCHEMA_VERSION:
        findings.append(
            f"authority: schema_version={root['schema_version']!r}, expected {SCHEMA_VERSION}"
        )
    _text_field(root, "decision", "authority", findings)
    _text_field(root, "reserved_rule", "authority", findings)
    head = root["frozen_source_head"]
    if not isinstance(head, str) or not _COMMIT.match(head):
        findings.append(
            f"authority: frozen_source_head must be a 40-character commit id, got {head!r}"
        )

    predecessor = _exact_fields(
        root["accepted_predecessor"], PREDECESSOR_FIELDS, "accepted_predecessor", findings
    )
    entries = root["allocations"]
    if not isinstance(entries, list) or not entries:
        findings.append("authority: allocations must be a non-empty list")
        return findings
    if predecessor is None:
        return findings

    base = predecessor["number"]
    if not _is_int(base):
        findings.append(f"accepted_predecessor: number must be an integer, got {base!r}")
        return findings
    _text_field(predecessor, "filename", "accepted_predecessor", findings)
    if isinstance(predecessor["filename"], str) and predecessor["filename"] not in present:
        findings.append(
            f"accepted_predecessor: {predecessor['filename']!r} is not in the migration "
            "directory, so this allocation is anchored to a migration that does not exist"
        )

    expected = base + 1
    allocated: dict[str, int] = {}

    for index, raw in enumerate(entries):
        label = f"allocations[{index}]"
        entry = _exact_fields(raw, ALLOCATION_FIELDS, label, findings)
        if entry is None:
            continue

        number = entry["number"]
        if not _is_int(number):
            findings.append(f"{label}: number must be an integer, got {number!r}")
            continue
        if number != expected:
            findings.append(
                f"{label}: expected migration {expected}, got {number} -- the sequence has "
                "a gap, a duplicate or a reordering"
            )
        expected = number + 1

        if entry["predecessor"] != number - 1:
            findings.append(
                f"{label}: migration {number} must follow {number - 1}, "
                f"but declares predecessor {entry['predecessor']!r}"
            )

        _text_field(entry, "owner", label, findings)
        _text_field(entry, "repository", label, findings)

        filename = entry["filename"]
        if not isinstance(filename, str) or not filename.endswith(".sql"):
            findings.append(f"{label}: filename must be a `.sql` name, got {filename!r}")
            continue
        if filename in allocated:
            findings.append(
                f"{label}: filename {filename!r} is already allocated to "
                f"migration {allocated[filename]}"
            )
        allocated[filename] = number

        _check_naming(filename, number, label, findings)
        _check_state(entry, filename, label, present, findings)

    for filename in sorted(present):
        prefix = filename.split("_", 1)[0]
        if not prefix.isdigit():
            findings.append(f"{filename}: migration file has no ordered numeric prefix")
            continue
        if int(prefix) > base and filename not in allocated:
            findings.append(
                f"{filename}: migration {int(prefix)} is beyond the accepted predecessor "
                f"{base} and is allocated to nobody"
            )

    return findings


def migration_files(directory: Path = MIGRATION_DIR) -> dict[str, str]:
    """Each `.sql` filename in `directory` mapped to the SHA-256 of its bytes."""
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.glob("*.sql"))
    }


def main() -> int:
    if not AUTHORITY.is_file():
        print(f"Migration allocation check FAILED: missing authority {AUTHORITY}", file=sys.stderr)
        return 1
    if not MIGRATION_DIR.is_dir():
        print(
            f"Migration allocation check FAILED: missing migration directory {MIGRATION_DIR}",
            file=sys.stderr,
        )
        return 1

    findings = check(AUTHORITY.read_text(encoding="utf-8"), migration_files())
    if findings:
        print("Migration allocation check FAILED:\n", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        print(f"\nAuthority: {AUTHORITY.relative_to(REPO_ROOT)}", file=sys.stderr)
        return 1
    print("Migration allocation check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
