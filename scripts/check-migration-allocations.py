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
  introducing commit, the file on disk exists with exactly that content, and
  `accepted_commit` is null;
* an `accepted` entry pins the same content hash and introducing commit as a
  candidate, plus a 40-character `accepted_commit`, and the file on disk
  exists with exactly that content;
* a `reserved` entry pins neither a hash, an introducing commit nor an
  accepted commit, and its file is *absent* -- a reserved number is an
  allocation, not a file, until the authority advances it;
* every `.sql` in the migration directory numbered above the accepted
  predecessor is allocated here, and no two `.sql` files claim the same number
  at *any* point in the sequence, including at or below the predecessor;
* the commit pins are real: `frozen_source_head` is a commit this checkout
  descends from, each candidate's or accepted entry's `introduced_commit` is a
  commit that contains that exact migration pathname with the pinned content,
  and for an accepted entry the `introduced_commit` is an ancestor of the
  `accepted_commit`, the `accepted_commit` is an ancestor of the checked head,
  and the migration blob at the `accepted_commit` also matches the pinned
  content.

Content is hashed the way the runtime hashes it -- the UTF-8 *text* of the file,
not its raw bytes. `Migration.checksum` digests
`path.read_text(encoding="utf-8").encode("utf-8")`, and text mode translates CRLF
and CR to LF, so a byte-level digest here would call every pinned hash wrong on a
CRLF checkout while the runtime's own checksums stayed right.

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
import subprocess
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
# The migration directory as Git names it: `<commit>:<path>` takes a
# repository-relative, forward-slashed path on every platform.
MIGRATION_PATH = MIGRATION_DIR.relative_to(REPO_ROOT).as_posix()

SCHEMA_VERSION = 1
CANDIDATE = "candidate"
RESERVED = "reserved"
ACCEPTED = "accepted"

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


class GuardError(RuntimeError):
    """A fact the guard needs -- a file's text, a Git answer -- could not be read.

    Raised rather than absorbed: a guard that quietly skips the check it could not
    perform reports "passed" for a proof it never attempted. `main()` turns this
    into a finding and a non-zero exit.
    """


def normalized_digest(data: bytes, label: str) -> str:
    """SHA-256 of `data` as the runtime hashes it: UTF-8 text with LF newlines.

    `Migration.checksum` digests `read_text(encoding="utf-8").encode("utf-8")`,
    and text mode translates CRLF and CR to LF. Matching that here is what keeps
    the pinned hashes correct on a CRLF checkout; on an LF checkout it is the
    same value a raw-byte digest produced.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GuardError(f"{label}: not valid UTF-8 ({error})") from error
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _file_number(filename: str) -> int | None:
    """The migration number `NNNN_name.sql` names, or None if it names none."""
    if not filename.endswith(".sql"):
        return None
    prefix, separator, slug = filename[: -len(".sql")].partition("_")
    if not separator or not slug or len(prefix) != 4:
        return None
    if not (prefix.isascii() and prefix.isdigit()):
        return None
    return int(prefix)


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
    named = _file_number(filename)
    if named is None:
        findings.append(f"{label}: filename {filename!r} is not of the form NNNN_name.sql")
    elif named != number:
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
    accepted = entry["accepted_commit"]

    if state == ACCEPTED:
        pinned = isinstance(digest, str) and _DIGEST.match(digest) is not None
        if not pinned:
            findings.append(
                f"{label}: an accepted allocation must pin a 64-character sha256, got {digest!r}"
            )
        if not isinstance(introduced, str) or not _COMMIT.match(introduced):
            findings.append(
                f"{label}: an accepted allocation must pin a 40-character introducing commit, "
                f"got {introduced!r}"
            )
        if not isinstance(accepted, str) or not _COMMIT.match(accepted):
            findings.append(
                f"{label}: an accepted allocation must pin a 40-character accepted commit, "
                f"got {accepted!r}"
            )
        if filename not in present:
            findings.append(f"{label}: accepted file is missing from the migration directory")
        elif pinned and present[filename] != digest:
            findings.append(
                f"{label}: {filename} content drifted -- the authority pins "
                f"{str(digest)[:12]}…, the file hashes to {present[filename][:12]}…"
            )
    elif state == CANDIDATE:
        if accepted is not None:
            findings.append(
                f"{label}: accepted_commit must be null until the allocation is accepted, "
                f"got {accepted!r}"
            )
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
        if accepted is not None:
            findings.append(
                f"{label}: accepted_commit must be null until the allocation is accepted, "
                f"got {accepted!r}"
            )
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
        findings.append(
            f"{label}: state must be {CANDIDATE!r}, {RESERVED!r} or {ACCEPTED!r}, got {state!r}"
        )


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
    if isinstance(predecessor["filename"], str):
        # The anchor is held to the same naming rule as every allocation. Without
        # it only the filename's *existence* was proved, so raising `number` to 20
        # while leaving `0017_connector_sync_state.sql` in place moved the base of
        # the whole check: allocations below the new base are simply dropped, and
        # the orphan scan below stops asking who owns 0018-0020.
        _check_naming(predecessor["filename"], base, "accepted_predecessor", findings)
        if predecessor["filename"] not in present:
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

    # Two independent facts about the directory itself. The collision scan covers
    # every number, not only those above the predecessor: `load_migrations()`
    # orders the whole directory, so a second `0009_*.sql` breaks migration just
    # as thoroughly as a second `0021_*.sql` and the allocation table -- which
    # starts above the predecessor -- would never mention it.
    claimants: dict[int, list[str]] = {}
    for filename in sorted(present):
        number = _file_number(filename)
        if number is None:
            findings.append(
                f"{filename}: migration file has no ordered numeric prefix; "
                "a migration must be named NNNN_name.sql"
            )
            continue
        claimants.setdefault(number, []).append(filename)
        if number > base and filename not in allocated:
            findings.append(
                f"{filename}: migration {number} is beyond the accepted predecessor "
                f"{base} and is allocated to nobody"
            )

    for number, names in sorted(claimants.items()):
        if len(names) > 1:
            findings.append(
                f"migration {number:04d} is claimed by more than one file: {', '.join(names)}"
            )

    return findings


def migration_files(directory: Path = MIGRATION_DIR) -> dict[str, str]:
    """Each `.sql` filename in `directory` mapped to its normalized text digest."""
    present: dict[str, str] = {}
    for path in sorted(directory.glob("*.sql")):
        try:
            data = path.read_bytes()
        except OSError as error:
            raise GuardError(f"{path.name}: unreadable ({error})") from error
        present[path.name] = normalized_digest(data, path.name)
    return present


def _authority_text() -> str:
    """The authority's UTF-8 text, or a `GuardError` naming why it could not be read."""
    try:
        data = AUTHORITY.read_bytes()
    except OSError as error:
        raise GuardError(f"authority is unreadable ({error})") from error
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GuardError(f"authority is not valid UTF-8 ({error})") from error


def _git(arguments: list[str], cwd: Path) -> tuple[int, bytes]:
    """Run `git` with an argument array -- no shell -- from the repository root."""
    try:
        completed = subprocess.run(
            ["git", *arguments], cwd=cwd, capture_output=True, check=False
        )
    except OSError as error:
        raise GuardError(f"git could not be run: {error}") from error
    return completed.returncode, completed.stdout


def _is_commit(commit: str, cwd: Path) -> bool:
    code, _ = _git(["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"], cwd)
    return code == 0


def check_history(text: str, head: str = "HEAD", cwd: Path = REPO_ROOT) -> list[str]:
    """Every way the authority's commit pins disagree with this repository's history.

    `check()` proves the pins are well-formed 40-character strings, which is a
    claim about syntax and nothing else: a pin can be perfectly shaped and name a
    commit that does not exist, does not lead to this checkout, or never carried
    the migration it is cited for. This is the seam that asks Git, kept out of
    `check()` so the mutation suite stays pure, offline and free of repository
    state; `main()` calls both.

    Malformed entries, and entries that are neither `candidate` nor `accepted`,
    are left alone here -- `check()` reports those, and repeating them would
    double every finding.
    """
    findings: list[str] = []
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return findings
    if not isinstance(document, dict):
        return findings

    if not _is_commit(head, cwd):
        return [f"history: {head} does not resolve to a commit in {cwd}"]

    frozen = document.get("frozen_source_head")
    if isinstance(frozen, str) and _COMMIT.match(frozen):
        if not _is_commit(frozen, cwd):
            findings.append(
                f"authority: frozen_source_head {frozen} is not a commit in this repository"
            )
        else:
            # Needs the full history. The acceptance job checks out with
            # `fetch-depth: 0` (pinned by `test_checkout_fetches_full_history`),
            # so a shallow clone would fail here rather than pass weakly.
            ancestry, _ = _git(["merge-base", "--is-ancestor", frozen, head], cwd)
            if ancestry != 0:
                findings.append(
                    f"authority: frozen_source_head {frozen} is not an ancestor of {head}, "
                    "so this checkout does not descend from the frozen source"
                )

    entries = document.get("allocations")
    if not isinstance(entries, list):
        return findings

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("state") not in (CANDIDATE, ACCEPTED):
            continue
        commit = entry.get("introduced_commit")
        filename = entry.get("filename")
        digest = entry.get("sha256")
        if not (isinstance(commit, str) and _COMMIT.match(commit)):
            continue
        if not isinstance(filename, str) or not isinstance(digest, str):
            continue
        if not _DIGEST.match(digest):
            continue

        label = f"allocations[{index}]"
        if not _is_commit(commit, cwd):
            findings.append(
                f"{label}: introduced_commit {commit} is not a commit in this repository"
            )
            continue

        path = f"{MIGRATION_PATH}/{filename}"
        code, blob = _git(["cat-file", "blob", f"{commit}:{path}"], cwd)
        if code != 0:
            findings.append(f"{label}: commit {commit} does not contain {path}")
            continue

        actual = normalized_digest(blob, f"{label}: {path} at commit {commit}")
        if actual != digest:
            findings.append(
                f"{label}: {path} at commit {commit} hashes to {actual[:12]}…, "
                f"the authority pins {digest[:12]}…"
            )
            continue

        if entry.get("state") != ACCEPTED:
            continue

        accepted_commit = entry.get("accepted_commit")
        if not (isinstance(accepted_commit, str) and _COMMIT.match(accepted_commit)):
            continue

        if not _is_commit(accepted_commit, cwd):
            findings.append(
                f"{label}: accepted_commit {accepted_commit} is not a commit in this repository"
            )
            continue

        ancestry, _ = _git(["merge-base", "--is-ancestor", commit, accepted_commit], cwd)
        if ancestry != 0:
            findings.append(
                f"{label}: introduced_commit {commit} is not an ancestor of "
                f"accepted_commit {accepted_commit}"
            )
            continue

        ancestry, _ = _git(["merge-base", "--is-ancestor", accepted_commit, head], cwd)
        if ancestry != 0:
            findings.append(
                f"{label}: accepted_commit {accepted_commit} is not an ancestor of {head}, "
                "so this checkout does not descend from the accepted lineage"
            )
            continue

        code, accepted_blob = _git(["cat-file", "blob", f"{accepted_commit}:{path}"], cwd)
        if code != 0:
            findings.append(f"{label}: accepted_commit {accepted_commit} does not contain {path}")
            continue

        accepted_actual = normalized_digest(
            accepted_blob, f"{label}: {path} at accepted_commit {accepted_commit}"
        )
        if accepted_actual != digest:
            findings.append(
                f"{label}: {path} at accepted_commit {accepted_commit} hashes to "
                f"{accepted_actual[:12]}…, the authority pins {digest[:12]}…"
            )

    return findings


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

    try:
        text = _authority_text()
        findings = check(text, migration_files()) + check_history(text)
    except GuardError as error:
        findings = [str(error)]

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
