"""Engineering source coverage and dependency applicability
(SPEC-CORE-ENGMEM-001, plan P0-04; spec §6.3, §15; migration 0050).

The trusted-source vertical. `engineering.source.record` appends immutable source
events to a stream its principal owns; the stream's coverage barrier advances
only along a contiguous, validated predecessor chain; `memory.create` records a
record version's whole-file dependency set against a recorded baseline; and one
deterministic evaluator compares that set with an explicitly requested target's
manifest. Every write runs inside the fenced mutation transaction the
coordinator opens, and the guard triggers of migration 0050 hold the ownership,
chain and coverage invariants a second time.

Rules enforced here:

- validation is strict and total. Unknown keys, malformed or duplicate paths,
  absolute, drive-prefixed, backslashed, empty, `.` or `..` segments, control
  characters, malformed digests and oversized manifests are refused, never
  repaired. Paths keep their exact Unicode and case;
- a stream belongs to the principal and repository that opened it. Another
  principal or repository cannot write to it or replace it;
- coverage follows the producer's sequence and predecessor chain, never capture
  time. An event may arrive out of order and wait inside a bounded pending
  window, and one bounded drain advances the barrier through every contiguous
  validated event. All history is retained;
- an identical redelivery records no new source event or snapshot (its audit
  and idempotency settlement still occur). A different event under a used
  sequence or snapshot identity is a conflict, whichever idempotency key it
  arrives under;
- the evaluator returns `matched` only for a complete, evidence-backed
  dependency set whose every required whole-file digest is attested by its
  recorded, covered baseline and equal at a covered, completely captured target
  in the same repository and stream. A changed required file is
  `potentially_stale`, an absent one under complete capture is `invalid`, and
  anything unqualified is `unknown`. Reviews, labels, base commits and recency
  play no part.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, TypeGuard

from omnivia_core.contracts.v1 import is_content_checksum, is_identifier
from omnivia_core_runtime.storage import repository_identity as repo_identity
from omnivia_core_runtime.storage.decisions import canonical_document, content_digest

#: The documented v1 bounds (spec §6.3 caps, stated here once).
MAX_MANIFEST_ENTRIES: Final = 256
MAX_MANIFEST_BYTES: Final = 65536
MAX_PATH_CHARS: Final = 512
MAX_SEQUENCE: Final = 2_147_483_647
MAX_DEPENDENCIES: Final = 64
#: How far past its covered chain a stream may announce. Out-of-order events wait
#: inside this window, which is also what bounds one drain: a stream never holds
#: more pending events than this, so one drain always reaches the end of a chain.
PENDING_WINDOW: Final = 64

SNAPSHOT_KINDS: Final = frozenset({"git_commit", "working_tree", "source_archive"})
CAPTURE_STATUSES: Final = frozenset({"complete", "incomplete"})
#: 0049's selector and meaning vocabularies. Only `whole_file` is evaluated in v1;
#: every other selector type is recorded and yields `unknown`.
SELECTOR_TYPES: Final = frozenset(
    {
        "whole_file",
        "source_span",
        "symbol",
        "config_key",
        "schema_contract",
        "external_evidence",
    }
)
MEANINGS: Final = frozenset(
    {"must_match", "requires_revalidation_on_change", "context_only"}
)
DEPENDENCY_COVERAGES: Final = frozenset({"complete", "partial"})

_RECORD_KEYS: Final = frozenset(
    {
        "repository_id",
        "stream_id",
        "sequence",
        "predecessor",
        "snapshot_id",
        "snapshot_kind",
        "base_commit",
        "capture_status",
        "manifest",
        "manifest_digest",
    }
)
_PREDECESSOR_KEYS: Final = frozenset({"sequence", "snapshot_id"})
_ENTRY_KEYS: Final = frozenset({"path", "digest"})
_PROFILE_KEYS: Final = frozenset(
    {
        "repository_id",
        "stream_id",
        "snapshot_id",
        "producer",
        "producer_version",
        "coverage",
        "dependencies",
    }
)
_DEPENDENCY_KEYS: Final = frozenset(
    {"selector_type", "selector", "meaning", "expected_digest"}
)


class SourceRecordInvalid(ValueError):
    """The source record is outside its bounded, validated shape."""


class SourceRecordTooLarge(ValueError):
    """The manifest exceeds its entry or canonical byte cap."""


class SourceStreamForeignPrincipal(RuntimeError):
    """The stream is owned by another principal."""


class SourceConflict(RuntimeError):
    """The event disagrees with an immutable identity or binding already stored."""


class SourceWindowExceeded(RuntimeError):
    """The event is further ahead of the covered chain than the pending window."""


class DependencyManifestInvalid(ValueError):
    """The `dependency_manifest` content profile is malformed."""


class DependencyBaselineUnavailable(LookupError):
    """The dependency manifest's baseline is not a recorded source event."""


@dataclass(frozen=True)
class SourceRecord:
    """One validated source event, with its canonical manifest and digests."""

    repository_id: str
    stream_id: str
    sequence: int
    predecessor_snapshot_id: str | None
    snapshot_id: str
    snapshot_kind: str
    base_commit: str | None
    capture_status: str
    manifest: Mapping[str, str]
    manifest_json: str
    manifest_digest: str
    event_digest: str


@dataclass(frozen=True)
class CoveredSnapshot:
    """A recorded snapshot inside its stream's contiguous validated coverage."""

    repository_id: str
    stream_id: str
    sequence: int
    snapshot_id: str
    capture_status: str
    manifest_digest: str
    manifest: Mapping[str, str]


@dataclass(frozen=True)
class Dependency:
    selector_type: str
    selector: str
    meaning: str
    expected_digest: str | None


@dataclass(frozen=True)
class DependencyManifest:
    """The validated `dependency_manifest` content profile of one observation."""

    repository_id: str
    stream_id: str
    snapshot_id: str
    producer: str
    producer_version: str
    coverage: str
    dependencies: tuple[Dependency, ...]


def _plain(value: Any) -> Any:
    """The contract's immutable containers as plain JSON values."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_plain(item) for item in value]
    return value


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _control_free(text: str) -> bool:
    """No control characters and no lone surrogates (which UTF-8 cannot carry)."""
    return all(unicodedata.category(char) not in ("Cc", "Cs") for char in text)


def _member(value: object, vocabulary: frozenset[str]) -> TypeGuard[str]:
    """A closed-vocabulary value: a string first, so a list or dict is refused
    rather than raising `TypeError` on the hash lookup."""
    return isinstance(value, str) and value in vocabulary


def _bounded_text(value: object, limit: int) -> bool:
    return (
        isinstance(value, str) and 1 <= len(value) <= limit and _control_free(value)
    )


def valid_path(value: object) -> bool:
    """A repository-relative `/`-separated path, preserved exactly.

    Unicode and case are never normalized: `A.py` and `a.py` are two paths. An
    absolute path, a Windows drive prefix, a backslash, an empty, `.` or `..`
    segment and a control character are refused.
    """
    if not isinstance(value, str) or not _bounded_text(value, MAX_PATH_CHARS):
        return False
    if value.startswith("/") or "\\" in value:
        return False
    segments = value.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return False
    first = segments[0]
    return not (
        len(first) >= 2 and first[1] == ":" and first[0].isascii() and first[0].isalpha()
    )


def _identifier(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not is_identifier(item):
        raise SourceRecordInvalid("a source identity is malformed")
    return item


def parse_source_record(raw: object) -> SourceRecord:
    """Validate one `engineering.source.record` payload, strictly and totally."""
    value = _plain(raw)
    if not isinstance(value, dict) or not set(value) <= _RECORD_KEYS:
        raise SourceRecordInvalid("unknown or missing source record fields")
    repository_id = _identifier(value, "repository_id")
    stream_id = _identifier(value, "stream_id")
    snapshot_id = _identifier(value, "snapshot_id")
    sequence = value.get("sequence")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or not 1 <= sequence <= MAX_SEQUENCE
    ):
        raise SourceRecordInvalid("the stream sequence is outside its bounds")
    predecessor = value.get("predecessor")
    predecessor_snapshot_id: str | None = None
    if sequence == 1:
        if predecessor is not None:
            raise SourceRecordInvalid("the first event of a stream has no predecessor")
    else:
        if (
            not isinstance(predecessor, dict)
            or set(predecessor) != _PREDECESSOR_KEYS
            or not _is_int(predecessor["sequence"])
            or predecessor["sequence"] != sequence - 1
            or not is_identifier(predecessor["snapshot_id"])
            or predecessor["snapshot_id"] == snapshot_id
        ):
            raise SourceRecordInvalid("the predecessor must name the previous sequence")
        predecessor_snapshot_id = predecessor["snapshot_id"]
    snapshot_kind = value.get("snapshot_kind")
    if not _member(snapshot_kind, SNAPSHOT_KINDS):
        raise SourceRecordInvalid("the snapshot kind is not supported")
    base_commit = value.get("base_commit")
    if snapshot_kind == "git_commit":
        if not _bounded_text(base_commit, 128):
            raise SourceRecordInvalid("a git_commit snapshot states its commit")
    elif base_commit is not None:
        # A dirty working tree or an archive never asserts a base commit (§6.3).
        raise SourceRecordInvalid("only a git_commit snapshot states a base commit")
    capture_status = value.get("capture_status")
    if not _member(capture_status, CAPTURE_STATUSES):
        raise SourceRecordInvalid("the capture status is not supported")
    entries = value.get("manifest")
    if not isinstance(entries, list):
        raise SourceRecordInvalid("the manifest must be a list")
    if len(entries) > MAX_MANIFEST_ENTRIES:
        raise SourceRecordTooLarge("the manifest exceeds its entry cap")
    manifest: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            raise SourceRecordInvalid("a manifest entry is malformed")
        path = entry["path"]
        digest = entry["digest"]
        if not valid_path(path) or not is_content_checksum(digest):
            raise SourceRecordInvalid("a manifest path or digest is malformed")
        if path in manifest:
            raise SourceRecordInvalid("a manifest path is repeated")
        manifest[path] = digest
    manifest_json = canonical_document(manifest)
    if len(manifest_json.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise SourceRecordTooLarge("the canonical manifest exceeds its byte cap")
    manifest_digest = content_digest(manifest_json)
    stated = value.get("manifest_digest")
    if stated is not None and stated != manifest_digest:
        raise SourceRecordInvalid("the stated manifest digest is not the manifest's")
    event_digest = content_digest(
        canonical_document(
            {
                "repository_id": repository_id,
                "stream_id": stream_id,
                "sequence": sequence,
                "predecessor_snapshot_id": predecessor_snapshot_id,
                "snapshot_id": snapshot_id,
                "snapshot_kind": snapshot_kind,
                "base_commit": base_commit,
                "capture_status": capture_status,
                "manifest_digest": manifest_digest,
            }
        )
    )
    return SourceRecord(
        repository_id=repository_id,
        stream_id=stream_id,
        sequence=sequence,
        predecessor_snapshot_id=predecessor_snapshot_id,
        snapshot_id=snapshot_id,
        snapshot_kind=snapshot_kind,
        base_commit=base_commit,
        capture_status=capture_status,
        manifest=manifest,
        manifest_json=manifest_json,
        manifest_digest=manifest_digest,
        event_digest=event_digest,
    )


def _timestamp(us: int) -> str:
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=us)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _stream(
    connection: sqlite3.Connection, workspace_id: str, stream_id: str
) -> tuple[str, str, int, int, int] | None:
    """`(repository, principal, announced, covered, updated_at_us)`, or None."""
    row = connection.execute(
        "SELECT repository_id, principal_id, announced_sequence, covered_sequence, "
        "updated_at_us FROM omnivia_engineering_source_streams "
        "WHERE workspace_id = ? AND stream_id = ?",
        (workspace_id, stream_id),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1]), int(row[2]), int(row[3]), int(row[4])


def _event(
    connection: sqlite3.Connection, workspace_id: str, stream_id: str, sequence: int
) -> tuple[str, str | None, str, int] | None:
    """`(snapshot, predecessor snapshot, event digest, recorded_at_us)`, or None."""
    row = connection.execute(
        "SELECT snapshot_id, predecessor_snapshot_id, event_digest, recorded_at_us "
        "FROM omnivia_engineering_source_events "
        "WHERE workspace_id = ? AND stream_id = ? AND sequence = ?",
        (workspace_id, stream_id, sequence),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), None if row[1] is None else str(row[1]), str(row[2]), int(row[3])


def _advance_coverage(
    connection: sqlite3.Connection, workspace_id: str, stream_id: str, covered: int
) -> int:
    """The bounded drain: walk the contiguous validated chain past `covered`.

    Stops at the first missing sequence or broken predecessor link. The pending
    window bounds how many events can be waiting, so the loop bound is never what
    stops a valid chain short.
    """
    previous = None
    if covered:
        head = _event(connection, workspace_id, stream_id, covered)
        assert head is not None
        previous = head[0]
    for _ in range(PENDING_WINDOW):
        following = _event(connection, workspace_id, stream_id, covered + 1)
        if following is None or following[1] != previous:
            break
        covered += 1
        previous = following[0]
    return covered


def _result(
    record: SourceRecord,
    *,
    disposition: str,
    recorded_at_us: int,
    covered: int,
    announced: int,
    audit_ref: str,
) -> dict[str, Any]:
    return {
        "repository_id": record.repository_id,
        "stream_id": record.stream_id,
        "sequence": record.sequence,
        "snapshot_id": record.snapshot_id,
        "manifest_digest": record.manifest_digest,
        "capture_status": record.capture_status,
        "disposition": disposition,
        "coverage": {
            "state": "current" if covered == announced else "pending",
            "covered_sequence": covered,
            "announced_sequence": announced,
        },
        "recorded_at": _timestamp(recorded_at_us),
        "audit_reference": audit_ref,
    }


def record_source_event(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    principal_id: str,
    record: SourceRecord,
) -> dict[str, Any]:
    """Register the stream (and repository) on first use, append one event, and
    advance the coverage barrier, all inside the caller's fenced transaction."""
    now_us = settlement.settled_at_us
    stream = _stream(connection, workspace_id, record.stream_id)
    if stream is not None:
        repository_id, owner, announced, covered, updated_us = stream
        if owner != principal_id:
            raise SourceStreamForeignPrincipal(record.stream_id)
        if repository_id != record.repository_id:
            raise SourceConflict("the stream is bound to another repository")
        existing = _event(connection, workspace_id, record.stream_id, record.sequence)
        if existing is not None:
            if existing[2] != record.event_digest:
                raise SourceConflict("the stream sequence already holds another event")
            return _result(
                record,
                disposition="already_recorded",
                recorded_at_us=existing[3],
                covered=covered,
                announced=announced,
                audit_ref=settlement.audit_ref,
            )
    else:
        announced, covered, updated_us = 0, 0, now_us
    if connection.execute(
        "SELECT 1 FROM omnivia_engineering_snapshots "
        "WHERE workspace_id = ? AND snapshot_id = ?",
        (workspace_id, record.snapshot_id),
    ).fetchone():
        raise SourceConflict("the snapshot identity is already recorded")
    if record.sequence > covered + PENDING_WINDOW:
        raise SourceWindowExceeded(record.stream_id)
    before = _event(connection, workspace_id, record.stream_id, record.sequence - 1)
    after = _event(connection, workspace_id, record.stream_id, record.sequence + 1)
    if (before is not None and before[0] != record.predecessor_snapshot_id) or (
        after is not None and after[1] != record.snapshot_id
    ):
        raise SourceConflict("the event disagrees with its stored neighbours")

    updated_us = max(updated_us, now_us)
    if stream is None:
        if (
            repo_identity.resolve_repository(
                connection, workspace_id=workspace_id, repository_id=record.repository_id
            )
            is None
        ):
            repo_identity.register_repository(
                connection,
                settlement,
                workspace_id=workspace_id,
                repository_id=record.repository_id,
                display_name=record.repository_id,
                provider_hint=None,
                registered_at_us=now_us,
            )
        announced = record.sequence
        connection.execute(
            "INSERT INTO omnivia_engineering_source_streams "
            "(workspace_id, stream_id, repository_id, principal_id, announced_sequence, "
            "covered_sequence, registered_at_us, updated_at_us, audit_ref) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (
                workspace_id,
                record.stream_id,
                record.repository_id,
                principal_id,
                announced,
                now_us,
                updated_us,
                settlement.audit_ref,
            ),
        )
    elif record.sequence > announced:
        # The newest announced head is written before its event, in the same
        # transaction, so no reader ever sees an event beyond the stated head.
        announced = record.sequence
        connection.execute(
            "UPDATE omnivia_engineering_source_streams SET announced_sequence = ?, "
            "updated_at_us = ?, audit_ref = ? WHERE workspace_id = ? AND stream_id = ?",
            (announced, updated_us, settlement.audit_ref, workspace_id, record.stream_id),
        )

    digest = repo_identity.record_snapshot(
        connection,
        settlement,
        workspace_id=workspace_id,
        snapshot_id=record.snapshot_id,
        repository_id=record.repository_id,
        snapshot_kind=record.snapshot_kind,
        manifest=record.manifest,
        base_commit=record.base_commit,
        capture_status=record.capture_status,
        captured_at_us=now_us,
    )
    if digest != record.manifest_digest:  # pragma: no cover - one canonicalization
        raise SourceConflict("the snapshot manifest digest disagrees with its body")
    connection.execute(
        "INSERT INTO omnivia_engineering_source_events "
        "(workspace_id, stream_id, sequence, snapshot_id, predecessor_sequence, "
        "predecessor_snapshot_id, manifest_json, manifest_digest, manifest_entry_count, "
        "event_digest, recorded_at_us, audit_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            record.stream_id,
            record.sequence,
            record.snapshot_id,
            None if record.sequence == 1 else record.sequence - 1,
            record.predecessor_snapshot_id,
            record.manifest_json,
            record.manifest_digest,
            len(record.manifest),
            record.event_digest,
            now_us,
            settlement.audit_ref,
        ),
    )
    covered = _advance_coverage(connection, workspace_id, record.stream_id, covered)
    connection.execute(
        "UPDATE omnivia_engineering_source_streams SET covered_sequence = ?, "
        "updated_at_us = ?, audit_ref = ? WHERE workspace_id = ? AND stream_id = ?",
        (covered, updated_us, settlement.audit_ref, workspace_id, record.stream_id),
    )
    return _result(
        record,
        disposition="recorded",
        recorded_at_us=now_us,
        covered=covered,
        announced=announced,
        audit_ref=settlement.audit_ref,
    )


def covered_snapshot(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    snapshot_id: str,
    repository_id: str | None = None,
) -> CoveredSnapshot | None:
    """The recorded snapshot if its event is inside its stream's coverage.

    None when the snapshot was never recorded as a source event, lies beyond a
    gap in its stream, belongs to another repository than the one stated, or its
    stored manifest body no longer matches its digest. A read; it writes nothing.
    """
    row = connection.execute(
        "SELECT st.repository_id, e.stream_id, e.sequence, st.covered_sequence, "
        "e.manifest_json, e.manifest_digest, sn.capture_status "
        "FROM omnivia_engineering_source_events e "
        "JOIN omnivia_engineering_source_streams st "
        "ON st.workspace_id = e.workspace_id AND st.stream_id = e.stream_id "
        "JOIN omnivia_engineering_snapshots sn "
        "ON sn.workspace_id = e.workspace_id AND sn.snapshot_id = e.snapshot_id "
        "WHERE e.workspace_id = ? AND e.snapshot_id = ?",
        (workspace_id, snapshot_id),
    ).fetchone()
    if row is None or int(row[2]) > int(row[3]):
        return None
    if repository_id is not None and repository_id != str(row[0]):
        return None
    if content_digest(str(row[4])) != str(row[5]):
        return None
    manifest = json.loads(str(row[4]))
    return CoveredSnapshot(
        repository_id=str(row[0]),
        stream_id=str(row[1]),
        sequence=int(row[2]),
        snapshot_id=snapshot_id,
        capture_status=str(row[6]),
        manifest_digest=str(row[5]),
        manifest=manifest,
    )


def parse_dependency_manifest(raw: object) -> DependencyManifest:
    """Validate one `dependency_manifest` content profile; never drop a dependency."""
    value = _plain(raw)
    if not isinstance(value, dict) or set(value) != _PROFILE_KEYS:
        raise DependencyManifestInvalid("unknown or missing dependency manifest fields")
    if not (
        is_identifier(value["repository_id"])
        and is_identifier(value["stream_id"])
        and is_identifier(value["snapshot_id"])
        and _bounded_text(value["producer"], 128)
        and _bounded_text(value["producer_version"], 64)
        and _member(value["coverage"], DEPENDENCY_COVERAGES)
    ):
        raise DependencyManifestInvalid("a dependency manifest field is malformed")
    raw_dependencies = value["dependencies"]
    if not isinstance(raw_dependencies, list) or len(raw_dependencies) > MAX_DEPENDENCIES:
        raise DependencyManifestInvalid("the dependency list is malformed or too long")
    dependencies: list[Dependency] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_dependencies:
        if (
            not isinstance(item, dict)
            or not set(item) <= _DEPENDENCY_KEYS
            or not {"selector_type", "selector", "meaning"} <= set(item)
        ):
            raise DependencyManifestInvalid("a dependency is malformed")
        selector_type = item["selector_type"]
        selector = item["selector"]
        meaning = item["meaning"]
        expected = item.get("expected_digest")
        if not (_member(selector_type, SELECTOR_TYPES) and _member(meaning, MEANINGS)):
            raise DependencyManifestInvalid("a dependency vocabulary value is unknown")
        if selector_type == "whole_file":
            if not valid_path(selector) or not is_content_checksum(expected):
                raise DependencyManifestInvalid("a whole-file dependency is malformed")
        elif not _bounded_text(selector, MAX_PATH_CHARS) or (
            expected is not None and not is_content_checksum(expected)
        ):
            raise DependencyManifestInvalid("a dependency selector is malformed")
        if (selector_type, selector) in seen:
            raise DependencyManifestInvalid("a dependency selector is repeated")
        seen.add((selector_type, selector))
        dependencies.append(Dependency(selector_type, selector, meaning, expected))
    return DependencyManifest(
        repository_id=value["repository_id"],
        stream_id=value["stream_id"],
        snapshot_id=value["snapshot_id"],
        producer=value["producer"],
        producer_version=value["producer_version"],
        coverage=value["coverage"],
        dependencies=tuple(dependencies),
    )


def record_dependency_set(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    record_id: str,
    version: str,
    manifest: DependencyManifest,
    allocate_identifier: Any,
) -> None:
    """Persist one record version's dependencies and seal them with their set row.

    The baseline must already be a recorded source event of the stated repository
    and stream; the caller's claimed digests are stored as claims and attested
    only by the evaluator against that recorded manifest.
    """
    if connection.execute(
        "SELECT 1 FROM omnivia_engineering_source_events e "
        "JOIN omnivia_engineering_source_streams st "
        "ON st.workspace_id = e.workspace_id AND st.stream_id = e.stream_id "
        "WHERE e.workspace_id = ? AND e.snapshot_id = ? AND e.stream_id = ? "
        "AND st.repository_id = ?",
        (workspace_id, manifest.snapshot_id, manifest.stream_id, manifest.repository_id),
    ).fetchone() is None:
        raise DependencyBaselineUnavailable(manifest.snapshot_id)
    for dependency in manifest.dependencies:
        connection.execute(
            "INSERT INTO omnivia_engineering_dependencies "
            "(workspace_id, dependency_id, record_id, version, selector_type, selector, "
            "meaning, producer, recorded_at_us, audit_ref, expected_digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                allocate_identifier("edep"),
                record_id,
                version,
                dependency.selector_type,
                dependency.selector,
                dependency.meaning,
                manifest.producer,
                settlement.settled_at_us,
                settlement.audit_ref,
                dependency.expected_digest,
            ),
        )
    connection.execute(
        "INSERT INTO omnivia_engineering_dependency_sets "
        "(workspace_id, record_id, version, repository_id, stream_id, snapshot_id, "
        "producer, producer_version, coverage, dependency_count, recorded_at_us, "
        "audit_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            record_id,
            version,
            manifest.repository_id,
            manifest.stream_id,
            manifest.snapshot_id,
            manifest.producer,
            manifest.producer_version,
            manifest.coverage,
            len(manifest.dependencies),
            settlement.settled_at_us,
            settlement.audit_ref,
        ),
    )


def decide(
    dependencies: Sequence[tuple[str, str, str, str | None]],
    *,
    baseline: CoveredSnapshot,
    target: CoveredSnapshot,
    qualified: bool,
) -> str:
    """The pure v1 applicability rule over whole-file digests (§15.4).

    Adverse findings need only an attested dependency: a required file the
    baseline attests that changed is `potentially_stale`, and one absent from a
    complete target is `invalid`. `matched` needs everything: a qualified set,
    complete baseline and target captures, at least one required dependency,
    only whole-file selectors, and every required digest attested by the baseline
    and equal at the target.
    """
    invalid = stale = False
    unknown = (
        not qualified
        or baseline.capture_status != "complete"
        or target.capture_status != "complete"
    )
    required = 0
    for selector_type, selector, meaning, expected in dependencies:
        if selector_type != "whole_file":
            unknown = True
            continue
        if meaning == "context_only":
            continue
        required += 1
        if expected is None or baseline.manifest.get(selector) != expected:
            # The caller's digest is a claim until the recorded baseline attests it.
            unknown = True
            continue
        observed = target.manifest.get(selector)
        if observed == expected:
            continue
        if observed is not None:
            stale = True
        elif target.capture_status == "complete":
            invalid = True
        else:
            unknown = True
    if invalid:
        return "invalid"
    if stale:
        return "potentially_stale"
    if unknown or required == 0:
        return "unknown"
    return "matched"


def evaluate_applicability(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    record_id: str,
    version: str,
    evidence_available: bool,
    target: CoveredSnapshot,
) -> str:
    """The one evaluator: an exact record version's dependencies at one target.

    A record version without a dependency set, or one recorded against another
    repository or stream, is `unknown`: nothing is inherited across streams or
    versions. The baseline must itself be covered, and the stored rows must be
    exactly the sealed count. A read; it writes nothing.
    """
    row = connection.execute(
        "SELECT repository_id, stream_id, snapshot_id, coverage, dependency_count "
        "FROM omnivia_engineering_dependency_sets "
        "WHERE workspace_id = ? AND record_id = ? AND version = ?",
        (workspace_id, record_id, version),
    ).fetchone()
    if row is None:
        return "unknown"
    if str(row[0]) != target.repository_id or str(row[1]) != target.stream_id:
        return "unknown"
    baseline = covered_snapshot(
        connection,
        workspace_id=workspace_id,
        snapshot_id=str(row[2]),
        repository_id=str(row[0]),
    )
    if baseline is None or baseline.stream_id != target.stream_id:
        return "unknown"
    sealed = int(row[4])
    # Bounded by the sealed count (at most 64); one row more than sealed, or fewer,
    # is an inconsistent set and fails closed rather than being evaluated.
    dependencies = [
        (str(dep[0]), str(dep[1]), str(dep[2]), None if dep[3] is None else str(dep[3]))
        for dep in connection.execute(
            "SELECT selector_type, selector, meaning, expected_digest "
            "FROM omnivia_engineering_dependencies "
            "WHERE workspace_id = ? AND record_id = ? AND version = ? "
            "ORDER BY selector_type, selector, meaning LIMIT ?",
            (workspace_id, record_id, version, sealed + 1),
        ).fetchall()
    ]
    if len(dependencies) != sealed:
        return "unknown"
    return decide(
        dependencies,
        baseline=baseline,
        target=target,
        qualified=evidence_available and str(row[3]) == "complete",
    )
