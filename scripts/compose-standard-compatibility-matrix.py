#!/usr/bin/env python3
"""Compose the three per-platform Standard candidates into one qualification matrix.

Each platform candidate (built by ``scripts/build-standard-candidate.py``) is
evidence for exactly one row: it was built and qualified on one OS, and it says
so through its own checksum-verified metadata.  This script takes three such
candidate directories -- one Linux, one macOS, one Windows, in any order -- and
composes them into a single deterministic JSON document that states what all
three rows agree on: the same clean source revision, the same compatibility
versions, the same five first-party distribution names and versions.  Each
row's first-party wheel SHA-256 values are recorded but never compared across
rows: platform-distinct bytes for the same first-party distribution are
expected (compiled artifacts, embedded path separators, etc.), not evidence
of a broken build.

Every candidate is read defensively.  Its checksum index is parsed and
verified byte-for-byte before any metadata inside it is trusted; every
release-manifest wheel is bound back to those verified bytes by path, digest
and size; the candidate key is recomputed from the manifest rather than
cross-compared between files that an attacker would edit together; and every
cross-row requirement (revision, versions, first-party wheel name/version
agreement with the row's own versions mapping, host identity, runner
selector, signing state) is checked explicitly.  Nothing here re-derives or
re-signs anything: the composed matrix is exactly as unsigned and exactly as
release-ineligible as its three inputs, and fails closed if any input claims
otherwise.

    scripts/compose-standard-compatibility-matrix.py \\
        CANDIDATE_DIR CANDIDATE_DIR CANDIDATE_DIR --output MATRIX.json [--force]

The three candidate directories may be given in any order; each row is placed
by the OS identity recorded inside it, never by directory name or argument
position.  The output is refused if it would land inside any candidate
directory, so composing can never overwrite the evidence it reads.  Standard
library only, entirely offline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

CHECKSUMS_FILE: Final = "checksums-sha256.txt"
METADATA_DIRECTORY: Final = "metadata"
BUILD_PROVENANCE_FILE: Final = "build-provenance.json"
COMPATIBILITY_MATRIX_FILE: Final = "compatibility-matrix.json"
QUALIFICATION_RESULT_FILE: Final = "qualification-result.json"
RELEASE_MANIFEST_FILE: Final = "release-manifest.json"
SIGNATURE_VERIFICATION_FILE: Final = "signature-verification.json"
OUTPUT_FORMAT: Final = "omnivia.standard-compatibility-matrix-composed.v1"
HEX_DIGITS: Final = frozenset("0123456789abcdef")

#: Duplicated from ``scripts/build-standard-candidate.py`` on purpose: this
#: script imports no OmniVia package and does not import the builder, so a
#: selector renamed on one side and not the other would silently stop being
#: checked, which is the whole value of keeping the copy.  The same reasoning
#: applies to every other builder-side literal pinned below: format markers,
#: the Standard profile, the matrix Python and the row statuses.
MATRIX_RUNNERS: Final = {
    "linux": "ubuntu-latest",
    "darwin": "macos-latest",
    "windows": "windows-latest",
}
MATRIX_PYTHON: Final = "3.11"
ROW_PASSED: Final = "passed_in_this_candidate"
ROW_REQUIRED: Final = "required_in_separate_matrix_row"
EXPECTED_FORMATS: Final = {
    BUILD_PROVENANCE_FILE: "omnivia.standard-build-provenance.v1",
    COMPATIBILITY_MATRIX_FILE: "omnivia.standard-compatibility-matrix.v1",
    QUALIFICATION_RESULT_FILE: "omnivia.standard-candidate-qualification.v1",
    RELEASE_MANIFEST_FILE: "omnivia.standard-release-manifest.v1",
    SIGNATURE_VERIFICATION_FILE: "omnivia.signature-verification.v1",
}
FIRST_PARTY_NAMES: Final = frozenset(
    {
        "omnivia-core",
        "omnivia-core-runtime",
        "omnivia-core-client",
        "omnivia-core-cli",
        "omnivia-core-mcp",
    }
)


class ComposeError(RuntimeError):
    """The candidate set cannot be composed into one qualification matrix."""


@dataclass(frozen=True, slots=True)
class CandidateRow:
    system: str
    machine: str
    python: str
    runner: str
    host: dict[str, Any]
    revision: str
    source_date_epoch: int
    candidate_key: str
    versions: dict[str, Any]
    first_party_wheels: dict[str, tuple[str, str]]
    checksum_index_sha256: str


def _digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> bytes:
    """Canonical byte form of a JSON value, used for equality and hashing.

    Comparing bytes rather than Python objects keeps JSON types distinct:
    ``1``, ``1.0`` and ``true`` are three different documents even though
    Python considers ``1 == 1.0 == True``.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_relative_path(raw: str, candidate_dir: Path, what: str) -> PurePosixPath:
    # NUL, C0 controls and DEL are rejected outright: they are never part of a
    # candidate path the builder writes, but they are exactly what a crafted
    # index would use to make a path read one way and open another.
    if (
        not raw
        or raw.startswith("/")
        or ":" in raw
        or "\\" in raw
        or any(character <= "\x1f" or character == "\x7f" for character in raw)
    ):
        raise ComposeError(f"{candidate_dir}: {what} names an unsafe path: {raw!r}")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ComposeError(f"{candidate_dir}: {what} names an unsafe path: {raw!r}")
    return PurePosixPath(raw)


def _require_text(candidate_dir: Path, value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ComposeError(f"{candidate_dir}: {what} is missing or not a non-empty string")
    return value


def _require_sha256(candidate_dir: Path, value: Any, what: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or not HEX_DIGITS.issuperset(value):
        raise ComposeError(f"{candidate_dir}: {what} is not a lowercase 64-character SHA-256")
    return value


def _parse_checksum_line(candidate_dir: Path, line: str) -> tuple[str, str]:
    # Written by `build-standard-candidate.py` as
    # f"{digest}  {relative_posix_path}" -- exactly 64 lowercase hex digits,
    # two spaces, then the path.  Anything else is malformed.
    if len(line) < 67 or line[64:66] != "  ":
        raise ComposeError(f"{candidate_dir}: the checksum index contains a malformed line: {line!r}")
    digest = _require_sha256(candidate_dir, line[:64], "a checksum index digest")
    return digest, line[66:]


def _candidate_files(candidate_dir: Path) -> set[str]:
    """Every regular file below the root, refusing symlinks and special nodes.

    ``os.walk`` with ``followlinks=False`` never descends a symlinked
    directory, so every symlink -- file or directory, indexed or not -- is seen
    exactly once here and refused rather than followed anywhere.  Anything that
    is neither a real directory nor a regular file (device, FIFO, socket) is
    refused for the same reason: a candidate is a tree of bytes, and nothing
    else belongs in it.
    """

    def _unreadable(error: OSError) -> None:
        raise ComposeError(f"{candidate_dir}: candidate tree is unreadable at {error.filename}")

    found: set[str] = set()
    for parent, directories, files in os.walk(candidate_dir, onerror=_unreadable, followlinks=False):
        base = Path(parent)
        for name in directories:
            entry = base / name
            if entry.is_symlink():
                raise ComposeError(
                    f"{candidate_dir}: candidate tree contains a symlinked directory: "
                    f"{entry.relative_to(candidate_dir).as_posix()}"
                )
        for name in files:
            entry = base / name
            relative = entry.relative_to(candidate_dir).as_posix()
            if entry.is_symlink():
                raise ComposeError(f"{candidate_dir}: candidate tree contains a symlink: {relative}")
            if not entry.is_file():
                raise ComposeError(f"{candidate_dir}: candidate tree contains a non-regular file: {relative}")
            found.add(relative)
    return found


def _load_checksum_index(candidate_dir: Path) -> tuple[dict[str, str], str]:
    """Parse and fully verify one candidate's checksum index.

    Returns ``{relative_path: digest}`` where every digest was computed from
    the bytes actually on disk *and* equals what the index claimed, plus the
    hex digest of the raw checksum-file bytes for the composed output.  Later
    stages therefore bind manifest claims to real artifact bytes by comparing
    against this mapping alone.  Universal-newline text reading normalizes
    CRLF to LF in memory without rewriting the input file, so both line
    endings are accepted identically.
    """
    root = candidate_dir.resolve()
    checksum_path = candidate_dir / CHECKSUMS_FILE
    if checksum_path.is_symlink() or not checksum_path.is_file():
        raise ComposeError(f"{candidate_dir}: {CHECKSUMS_FILE} is absent or not a regular file")
    try:
        raw_bytes = checksum_path.read_bytes()
        text = raw_bytes.decode("utf-8")
    except OSError as error:
        raise ComposeError(f"{candidate_dir}: checksum index is unreadable") from error
    except UnicodeDecodeError as error:
        raise ComposeError(f"{candidate_dir}: checksum index is not valid UTF-8") from error

    entries: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            continue
        digest, raw_relative = _parse_checksum_line(candidate_dir, line)
        relative = _safe_relative_path(raw_relative, candidate_dir, "the checksum index").as_posix()
        if relative in entries:
            raise ComposeError(f"{candidate_dir}: duplicate checksum path {relative!r}")
        entries[relative] = digest

    for relative, claimed in entries.items():
        target = candidate_dir / relative
        if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(root):
            raise ComposeError(
                f"{candidate_dir}: indexed path is absent, not a regular file, "
                f"or escapes the candidate root: {relative}"
            )
        actual = _digest_file(target)
        if actual != claimed:
            raise ComposeError(f"{candidate_dir}: checksum mismatch for {relative}")
        # Replace the claim with the measurement: from here on the mapping is
        # a statement about bytes on disk, not about what the index asserted.
        entries[relative] = actual

    actual_files = _candidate_files(candidate_dir) - {CHECKSUMS_FILE}
    unindexed = sorted(actual_files - set(entries))
    if unindexed:
        raise ComposeError(f"{candidate_dir}: unindexed file(s) present: {unindexed}")
    missing = sorted(set(entries) - actual_files)
    if missing:
        raise ComposeError(f"{candidate_dir}: indexed file(s) absent: {missing}")

    return entries, _digest_bytes(raw_bytes)


def _read_json(candidate_dir: Path, verified: dict[str, str], name: str) -> Any:
    relative = f"{METADATA_DIRECTORY}/{name}"
    if relative not in verified:
        raise ComposeError(f"{candidate_dir}: checksum index omits {relative}")
    try:
        return json.loads((candidate_dir / relative).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ComposeError(f"{candidate_dir}: {relative} is unreadable or malformed") from error


def _require_mapping(candidate_dir: Path, value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ComposeError(f"{candidate_dir}: {what} is malformed")
    return value


def _require_int_zero(candidate_dir: Path, value: Any, what: str) -> None:
    if type(value) is not int or value != 0:
        raise ComposeError(f"{candidate_dir}: {what} must be exactly 0")


def _require_profile(candidate_dir: Path, value: Any, what: str) -> None:
    """Require exactly the five Standard first-party names, listed once each."""
    if (
        not isinstance(value, list)
        or len(value) != len(FIRST_PARTY_NAMES)
        or any(not isinstance(name, str) for name in value)
        or set(value) != FIRST_PARTY_NAMES
    ):
        raise ComposeError(f"{candidate_dir}: {what} profile is not the five Standard first-party names")


def _verify_manifest_wheels(
    candidate_dir: Path, verified: dict[str, str], manifest: dict[str, Any]
) -> tuple[dict[str, tuple[str, str]], str]:
    """Bind every release-manifest wheel to verified bytes and recompute the candidate key.

    Returns the first-party ``{name: (version, sha256)}`` mapping and the
    candidate key recomputed exactly as ``build-standard-candidate.py``
    derives it: SHA-256 over the newline-joined manifest wheel digests in
    manifest order.
    """
    wheels = manifest.get("wheels")
    if not isinstance(wheels, list) or not wheels:
        raise ComposeError(f"{candidate_dir}: release-manifest.json wheels is malformed")

    digests: list[str] = []
    seen_paths: set[str] = set()
    first_party: dict[str, tuple[str, str]] = {}
    for entry in wheels:
        if not isinstance(entry, dict):
            raise ComposeError(f"{candidate_dir}: a release-manifest.json wheel entry is malformed")
        name = _require_text(candidate_dir, entry.get("name"), "a release-manifest.json wheel name")
        version = _require_text(candidate_dir, entry.get("version"), f"release-manifest.json wheel {name} version")
        raw_path = _require_text(candidate_dir, entry.get("path"), f"release-manifest.json wheel {name} path")
        sha256 = _require_sha256(candidate_dir, entry.get("sha256"), f"release-manifest.json wheel {name} sha256")

        relative = _safe_relative_path(raw_path, candidate_dir, f"release-manifest.json wheel {name}").as_posix()
        if relative in seen_paths:
            raise ComposeError(f"{candidate_dir}: duplicate release-manifest.json wheel path {relative!r}")
        seen_paths.add(relative)

        measured = verified.get(relative)
        if measured is None:
            raise ComposeError(f"{candidate_dir}: release-manifest.json wheel {relative} is not in {CHECKSUMS_FILE}")
        # `measured` is the digest of the bytes on disk and already equals the
        # checksum-index entry, so one comparison binds the manifest claim to
        # both the index and the artifact itself.
        if measured != sha256:
            raise ComposeError(
                f"{candidate_dir}: release-manifest.json sha256 for {relative} does not match the verified artifact"
            )

        # The builder always records `bytes`, so an entry without one is not a
        # manifest this script knows how to bind -- absent is a failure, not a
        # skipped check.  `type(...) is not int` also rejects `True`.
        size = entry.get("bytes")
        if type(size) is not int or size < 0:
            raise ComposeError(f"{candidate_dir}: release-manifest.json wheel {relative} bytes is not a size")
        if size != (candidate_dir / relative).stat().st_size:
            raise ComposeError(
                f"{candidate_dir}: release-manifest.json wheel {relative} bytes does not match the artifact"
            )

        if entry.get("first_party") is True:
            if name in first_party:
                raise ComposeError(f"{candidate_dir}: duplicate first-party wheel entry for {name}")
            first_party[name] = (version, sha256)
        digests.append(sha256)

    if set(first_party) != FIRST_PARTY_NAMES:
        raise ComposeError(f"{candidate_dir}: first-party wheel set does not match the Standard profile")

    return first_party, _digest_bytes("\n".join(digests).encode())


def _cross_check_source(
    candidate_dir: Path, revision: str, epoch: int, documents: Iterable[tuple[str, dict[str, Any]]]
) -> None:
    """Require any source claim outside build-provenance.json to repeat it exactly."""
    for label, document in documents:
        claimed_revision = document.get("source_revision")
        if claimed_revision is not None and (not isinstance(claimed_revision, str) or claimed_revision != revision):
            raise ComposeError(f"{candidate_dir}: {label} source_revision does not match build-provenance")
        claimed_epoch = document.get("source_date_epoch")
        if claimed_epoch is not None and (type(claimed_epoch) is not int or claimed_epoch != epoch):
            raise ComposeError(f"{candidate_dir}: {label} source_date_epoch does not match build-provenance")


def _load_candidate(candidate_dir: Path) -> CandidateRow:
    verified, checksum_index_sha256 = _load_checksum_index(candidate_dir)

    provenance = _require_mapping(
        candidate_dir, _read_json(candidate_dir, verified, BUILD_PROVENANCE_FILE), "build-provenance.json"
    )
    matrix = _require_mapping(
        candidate_dir, _read_json(candidate_dir, verified, COMPATIBILITY_MATRIX_FILE), "compatibility-matrix.json"
    )
    qualification = _require_mapping(
        candidate_dir, _read_json(candidate_dir, verified, QUALIFICATION_RESULT_FILE), "qualification-result.json"
    )
    manifest = _require_mapping(
        candidate_dir, _read_json(candidate_dir, verified, RELEASE_MANIFEST_FILE), "release-manifest.json"
    )
    signature = _require_mapping(
        candidate_dir, _read_json(candidate_dir, verified, SIGNATURE_VERIFICATION_FILE), "signature-verification.json"
    )

    # Pin the schema before reading anything out of it: a document that does not
    # name itself is not the document these checks were written against.
    for label, document in (
        (BUILD_PROVENANCE_FILE, provenance),
        (COMPATIBILITY_MATRIX_FILE, matrix),
        (QUALIFICATION_RESULT_FILE, qualification),
        (RELEASE_MANIFEST_FILE, manifest),
        (SIGNATURE_VERIFICATION_FILE, signature),
    ):
        if document.get("format") != EXPECTED_FORMATS[label]:
            raise ComposeError(f"{candidate_dir}: {label} format is not {EXPECTED_FORMATS[label]}")

    source = _require_mapping(candidate_dir, provenance.get("source"), "build-provenance.source")
    host = _require_mapping(candidate_dir, provenance.get("host"), "build-provenance.host")
    current = _require_mapping(candidate_dir, matrix.get("current"), "compatibility-matrix.current")
    versions = _require_mapping(candidate_dir, current.get("versions"), "compatibility-matrix.current.versions")

    if source.get("dirty") is not False:
        raise ComposeError(f"{candidate_dir}: source tree was dirty at build time")
    revision = _require_text(candidate_dir, source.get("revision"), "source revision")
    epoch = source.get("source_date_epoch")
    if type(epoch) is not int:
        raise ComposeError(f"{candidate_dir}: source_date_epoch is missing or not an integer")

    # The manifest is the only file that names the source revision again, so
    # it must repeat it; the other two are checked opportunistically.
    _require_text(candidate_dir, manifest.get("source_revision"), "release-manifest.json source_revision")
    _cross_check_source(
        candidate_dir,
        revision,
        epoch,
        (
            (RELEASE_MANIFEST_FILE, manifest),
            (COMPATIBILITY_MATRIX_FILE, matrix),
            (QUALIFICATION_RESULT_FILE, qualification),
        ),
    )

    first_party_wheels, candidate_key = _verify_manifest_wheels(candidate_dir, verified, manifest)
    # Recomputed from verified artifact bytes, never taken from metadata: an
    # attacker who edits all three files consistently still cannot make them
    # agree with the wheels they ship.
    for label, document in (
        (COMPATIBILITY_MATRIX_FILE, matrix),
        (QUALIFICATION_RESULT_FILE, qualification),
        (RELEASE_MANIFEST_FILE, manifest),
    ):
        claimed = _require_sha256(candidate_dir, document.get("candidate_key"), f"{label} candidate_key")
        if claimed != candidate_key:
            raise ComposeError(
                f"{candidate_dir}: {label} candidate_key does not match the digest of the manifest wheel set"
            )

    if qualification.get("verdict") != "pass":
        raise ComposeError(f"{candidate_dir}: qualification-result.json verdict is not 'pass'")
    _require_int_zero(candidate_dir, qualification.get("skips"), "qualification-result.json skips")
    _require_int_zero(candidate_dir, qualification.get("xfails"), "qualification-result.json xfails")
    if qualification.get("offline_install") is not True:
        raise ComposeError(f"{candidate_dir}: qualification-result.json offline_install is not exactly true")
    wheel_count = qualification.get("wheel_count")
    if type(wheel_count) is not int or wheel_count != len(manifest["wheels"]):
        raise ComposeError(
            f"{candidate_dir}: qualification-result.json wheel_count is not the manifest wheel count"
        )
    _require_profile(candidate_dir, qualification.get("profile"), QUALIFICATION_RESULT_FILE)
    _require_profile(candidate_dir, manifest.get("profile"), RELEASE_MANIFEST_FILE)
    if manifest.get("candidate_status") != "qualification_only":
        raise ComposeError(f"{candidate_dir}: release-manifest.json candidate_status is not 'qualification_only'")
    if matrix.get("complete_matrix_claimed") is not False:
        raise ComposeError(f"{candidate_dir}: compatibility-matrix.json complete_matrix_claimed is not exactly false")
    if current.get("journey") != "pass":
        raise ComposeError(f"{candidate_dir}: compatibility-matrix.json current.journey is not 'pass'")

    system = _require_text(candidate_dir, current.get("system"), "compatibility-matrix.current.system")
    if system not in MATRIX_RUNNERS:
        raise ComposeError(f"{candidate_dir}: unrecognized host system {system!r}")
    if host.get("system") != system:
        raise ComposeError(f"{candidate_dir}: current.system does not match build-provenance.host.system")
    machine = _require_text(candidate_dir, current.get("machine"), "compatibility-matrix.current.machine")
    if machine != host.get("machine"):
        raise ComposeError(f"{candidate_dir}: current.machine does not match build-provenance.host.machine")
    python = _require_text(candidate_dir, current.get("python"), "compatibility-matrix.current.python")
    if python != host.get("python_version"):
        raise ComposeError(f"{candidate_dir}: current.python does not match build-provenance.host.python_version")
    if current.get("host") != host:
        raise ComposeError(f"{candidate_dir}: current.host does not match build-provenance.host")
    runner = current.get("runner")
    if runner != MATRIX_RUNNERS[system]:
        raise ComposeError(f"{candidate_dir}: current.runner {runner!r} is not the expected selector for {system}")

    # Compared as a canonical multiset of whole rows, so a duplicated, extra,
    # missing, reordered-key or otherwise edited row all fail the same way: this
    # candidate claims exactly one platform passed and names the other two as
    # owed by separate rows, or it is not this candidate's matrix.
    expected_rows = [
        {
            "system": name,
            "runner": selector,
            "python": MATRIX_PYTHON,
            "status": ROW_PASSED if name == system else ROW_REQUIRED,
        }
        for name, selector in MATRIX_RUNNERS.items()
    ]
    rows = matrix.get("rows")
    if not isinstance(rows, list) or sorted(map(_canonical_json, rows)) != sorted(map(_canonical_json, expected_rows)):
        raise ComposeError(
            f"{candidate_dir}: compatibility-matrix.json rows are not the three required "
            f"Python {MATRIX_PYTHON} platform rows with {system} passed"
        )

    first_party_distributions = _require_mapping(
        candidate_dir,
        versions.get("first_party_distributions"),
        "compatibility-matrix.current.versions.first_party_distributions",
    )
    if set(first_party_distributions) != FIRST_PARTY_NAMES or any(
        not isinstance(value, str) or not value for value in first_party_distributions.values()
    ):
        raise ComposeError(f"{candidate_dir}: first_party_distributions does not match the Standard profile")

    for name, (version, _sha256) in first_party_wheels.items():
        if first_party_distributions.get(name) != version:
            raise ComposeError(
                f"{candidate_dir}: first-party wheel {name} version {version!r} does not match "
                f"compatibility-matrix versions.first_party_distributions"
            )

    signing = _require_mapping(candidate_dir, manifest.get("signing"), "release-manifest.json signing")
    if (
        signature.get("status") != "unsigned"
        or signature.get("verification_performed") is not False
        or signature.get("production_release_eligible") is not False
        or manifest.get("production_release_eligible") is not False
        or signing.get("status") != "unsigned"
    ):
        raise ComposeError(f"{candidate_dir}: candidate claims signing or production-release eligibility")

    return CandidateRow(
        system=system,
        machine=machine,
        python=python,
        runner=runner,
        host=host,
        revision=revision,
        source_date_epoch=epoch,
        candidate_key=candidate_key,
        versions=versions,
        first_party_wheels=first_party_wheels,
        checksum_index_sha256=checksum_index_sha256,
    )


def _resolved_candidate_dirs(candidate_dirs: Sequence[Path]) -> list[Path]:
    """Resolve the three input roots and refuse any pair that can alias.

    Overlapping roots would let one candidate's files be counted as another's
    evidence, and a resolved root is also what keeps ``--output`` from being
    written back into the inputs.
    """
    if len(candidate_dirs) != 3:
        raise ComposeError("exactly three candidate directories are required")
    resolved: list[Path] = []
    for directory in candidate_dirs:
        path = Path(directory).resolve()
        if not path.is_dir():
            raise ComposeError(f"{directory} is not a candidate directory")
        resolved.append(path)
    for index, path in enumerate(resolved):
        for other in resolved[index + 1 :]:
            if path == other or path.is_relative_to(other) or other.is_relative_to(path):
                raise ComposeError(f"candidate directories must be distinct and non-overlapping: {path} and {other}")
    return resolved


def compose(candidate_dirs: Sequence[Path]) -> dict[str, Any]:
    directories = _resolved_candidate_dirs(candidate_dirs)
    rows = [_load_candidate(directory) for directory in directories]

    by_system = {row.system: row for row in rows}
    if set(by_system) != set(MATRIX_RUNNERS):
        systems = sorted(row.system for row in rows)
        raise ComposeError(
            f"the candidate set must contain exactly one linux, one darwin and one windows row; got {systems}"
        )

    # Every aggregate below is taken from the canonically ordered rows, never
    # from argument order, so the same three candidates in any order produce
    # byte-identical output.
    ordered_rows = [by_system[system] for system in sorted(by_system)]
    canonical = ordered_rows[0]

    if any(row.revision != canonical.revision for row in ordered_rows[1:]) or any(
        row.source_date_epoch != canonical.source_date_epoch for row in ordered_rows[1:]
    ):
        raise ComposeError("all candidates must be clean builds of the exact same source revision and epoch")

    canonical_versions = _canonical_json(canonical.versions)
    if any(_canonical_json(row.versions) != canonical_versions for row in ordered_rows[1:]):
        raise ComposeError("candidates report divergent compatibility versions")

    # First-party wheel bytes are intentionally platform-distinct (compiled
    # extensions, path separators baked into metadata, etc.); only the name
    # and version are required to match, and that is already enforced per
    # row against compatibility-matrix.current.versions.first_party_distributions
    # in _load_candidate, transitively pinned across rows by the versions
    # equality check above.

    row_documents = [
        {
            "system": row.system,
            "runner": row.runner,
            "machine": row.machine,
            "python": row.python,
            "host": row.host,
            "candidate_key": row.candidate_key,
            "checksum_index_sha256": row.checksum_index_sha256,
            "first_party_wheels": [
                {"name": name, "version": version, "sha256": sha256}
                for name, (version, sha256) in sorted(row.first_party_wheels.items())
            ],
        }
        for row in ordered_rows
    ]

    source = {"revision": canonical.revision, "source_date_epoch": canonical.source_date_epoch}
    # The row documents carry every identity this matrix rests on -- host,
    # recomputed candidate key, checksum-index digest and each first-party
    # wheel's name/version/digest -- so hashing them binds the whole set.
    candidate_set_key = _digest_bytes(_canonical_json({"source": source, "rows": row_documents}))

    return {
        "format": OUTPUT_FORMAT,
        "source": source,
        "candidate_set_key": candidate_set_key,
        "versions": canonical.versions,
        "qualification_matrix": {
            "complete_matrix_claimed": True,
            "rows": row_documents,
        },
        "signing": {"status": "unsigned", "verification_performed": False},
        "production_release_eligible": False,
    }


def _write_output(
    document: dict[str, Any], output: Path, *, force: bool, candidate_dirs: Sequence[Path]
) -> None:
    """Publish the composed matrix as one complete file, or not at all.

    The document is always written to a same-directory temporary file first, so
    no reader ever observes a partial matrix.  Without ``--force`` it is then
    published by ``os.link``, which fails with ``EEXIST`` rather than
    overwriting: a destination that appears between any two instructions here
    still wins, where an ``exists()`` test followed by ``os.replace`` would
    silently clobber it.  With ``--force`` the overwrite is the point, and
    ``os.replace`` performs it atomically.
    """
    destination = Path(output).resolve()
    for directory in candidate_dirs:
        root = Path(directory).resolve()
        if destination == root or destination.is_relative_to(root):
            raise ComposeError(f"{output} is inside candidate directory {root}; refusing to write over input evidence")
    parent = destination.parent
    if not parent.is_dir():
        raise ComposeError(f"{output}: the output directory {parent} does not exist")
    payload = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        descriptor, temporary_name = tempfile.mkstemp(dir=parent, prefix=".compose-matrix-", suffix=".tmp")
    except OSError as error:
        raise ComposeError(f"{output}: cannot create a temporary file in {parent}") from error
    try:
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except OSError as error:
            raise ComposeError(f"{output}: writing the composed matrix failed") from error
        try:
            if force:
                os.replace(temporary_name, destination)
            else:
                os.link(temporary_name, destination)
        except FileExistsError as error:
            raise ComposeError(f"{output} already exists; pass --force to overwrite it") from error
        except OSError as error:
            raise ComposeError(f"{output}: publishing the composed matrix failed") from error
    finally:
        # A successful `os.replace` already consumed the temporary name, so this
        # is a no-op there.  Cleanup never raises: it must not mask the error
        # that brought us here, and a stray temp file is the lesser problem.
        try:
            os.unlink(temporary_name)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "candidates",
        nargs=3,
        type=Path,
        metavar="CANDIDATE_DIR",
        help="three Standard candidate directories (Linux, macOS, Windows), any order",
    )
    parser.add_argument("--output", required=True, type=Path, help="path to write the composed matrix JSON")
    parser.add_argument("--force", action="store_true", help="overwrite an existing --output file")
    arguments = parser.parse_args(argv)

    try:
        directories = _resolved_candidate_dirs(arguments.candidates)
        document = compose(directories)
        _write_output(document, arguments.output, force=arguments.force, candidate_dirs=directories)
    except ComposeError as error:
        print(f"Standard compatibility matrix composition failed: {error}", file=sys.stderr)
        return 1

    print(json.dumps({"format": OUTPUT_FORMAT, "output": str(arguments.output), "verdict": "pass"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
