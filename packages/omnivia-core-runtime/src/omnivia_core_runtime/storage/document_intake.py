"""Logical documents, immutable source captures and immutable extraction revisions: persistence only (migration 0069).

Shaped like `storage/task_context.py`. Every `insert_*`, `advance_*` and `set_*` function expects its caller to be
inside a `fenced_transaction`; the domain rules in `service/document_intake.py` open that fence and decide what may
be admitted or promoted. The migration's triggers repeat the rules that must hold whatever the caller does, so a
function here that is given a wrong value fails with the database's refusal rather than writing it.

A capture is bound to existing evidence and to the exact digest and length of its immutable binary source. A profile
is identified by the SHA-256 of its canonical JSON, and reads recompute that digest, so an altered row reads as
`DocumentIntakeInvalid` and never as another profile. Capture and extraction identifiers derive from their operation
key, and each row carries the digest of the request that used the key, so replaying a request is recognisable and a
different request cannot take the key.

The capture head and the extraction head are separate columns of one row in `omnivia_document_heads`, and each moves
only through its own `advance_*` function.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import astuple, dataclass, fields
from hashlib import sha256
from typing import Any, Final, TypeVar

from omnivia_core.contracts.v1 import to_canonical_json

CAPTURE_PREFIX: Final = "dcap-"
REVISION_PREFIX: Final = "dext-"
PROFILE_SCHEMA: Final = "omnivia.extraction-profile.v1"
PROCESSING_MODES: Final = frozenset({"text_only", "text_and_local_ocr"})
MAX_PROFILE_BYTES: Final = 65536

DISPOSITION_HEAD_ELIGIBLE: Final = "head_eligible"
DISPOSITION_HISTORICAL_ONLY: Final = "historical_only"
RETENTION_RETAINED: Final = "retained"
RETENTION_STATES: Final = frozenset({"retained", "expired", "deleted"})
SOURCE_ACTIVE: Final = "active"
SOURCE_REMOVED: Final = "removed"

RESULT_STATES: Final = frozenset({"succeeded", "partial", "unsupported", "failed"})
PUBLISHABLE_STATES: Final = frozenset({"succeeded", "partial"})
PUBLICATION_DISPOSITIONS: Final = frozenset(
    {"promoted", "superseded", "historical_only", "fenced_removal", "fenced_retention", "not_published"}
)

_T = TypeVar("_T")


class DocumentIntakeInvalid(ValueError):
    """A value handed to storage, or a stored row read back, is outside its closed shape or fails its identity."""


def _sha256_hex(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def request_digest(request: dict[str, Any]) -> str:
    """The digest of one canonical request, as an operation key's replay check compares it."""
    return "sha256:" + _sha256_hex(to_canonical_json(request))


def capture_id_for(workspace_id: str, operation_key: str) -> str:
    return CAPTURE_PREFIX + _sha256_hex(f"{workspace_id}\0{operation_key}")


def revision_id_for(workspace_id: str, operation_key: str) -> str:
    return REVISION_PREFIX + _sha256_hex(f"{workspace_id}\0{operation_key}")


def canonical_profile(profile: dict[str, Any]) -> str:
    """The canonical JSON of an ExtractionProfileV1, after the closed fields it must carry are checked."""
    if not isinstance(profile, dict) or profile.get("schema") != PROFILE_SCHEMA:
        raise DocumentIntakeInvalid("extraction profile has the wrong schema")
    if profile.get("processingMode") not in PROCESSING_MODES:
        raise DocumentIntakeInvalid("extraction profile has an unknown processing mode")
    for key in ("adapter", "adapterVersion"):
        value = profile.get(key)
        if not isinstance(value, str) or not value:
            raise DocumentIntakeInvalid(f"extraction profile needs a non-empty {key}")
    try:
        document = to_canonical_json(profile)
    except (TypeError, ValueError) as error:
        raise DocumentIntakeInvalid("extraction profile is not canonical JSON") from error
    if len(document.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise DocumentIntakeInvalid("extraction profile exceeds the accepted bound")
    return document


def profile_digest(profile: dict[str, Any]) -> str:
    """ExtractionProfileV1 identity: the SHA-256 of its canonical JSON."""
    return "sha256:" + _sha256_hex(canonical_profile(profile))


@dataclass(frozen=True, slots=True)
class DocumentHead:
    workspace_id: str
    document_id: str
    source_state: str
    source_removal_generation: int
    current_capture_id: str | None
    capture_head_sequence: int
    current_extraction_id: str | None
    extraction_head_ordinal: int


@dataclass(frozen=True, slots=True)
class StoredCapture:
    workspace_id: str
    capture_id: str
    document_id: str
    capture_sequence: int
    evidence_id: str
    source_digest: str
    source_length_bytes: int
    media_type: str
    expected_base_capture_id: str | None
    disposition: str
    source_removal_generation: int
    operation_key: str
    request_digest: str
    fencing_generation: int
    created_at_us: int


@dataclass(frozen=True, slots=True)
class CaptureRetention:
    workspace_id: str
    capture_id: str
    retention_state: str
    capture_retention_generation: int


@dataclass(frozen=True, slots=True)
class StoredRevision:
    workspace_id: str
    extraction_revision_id: str
    document_id: str
    capture_id: str
    input_digest: str
    profile_digest: str
    extraction_sequence: int
    document_ordinal: int
    expected_base_extraction_id: str | None
    head_eligible: int
    source_removal_generation: int
    capture_retention_generation: int
    operation_key: str
    request_digest: str
    fencing_generation: int
    created_at_us: int


@dataclass(frozen=True, slots=True)
class StoredResult:
    workspace_id: str
    extraction_revision_id: str
    result_state: str
    output_digest: str | None
    coverage_json: str
    limitations_json: str
    worker_build: str
    publication_disposition: str
    fencing_generation: int
    completed_at_us: int


def _names(cls: type[Any]) -> tuple[str, ...]:
    return tuple(field.name for field in fields(cls))


def _insert(connection: sqlite3.Connection, table: str, row: Any) -> None:
    names = _names(type(row))
    connection.execute(
        f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
        astuple(row),
    )


def _select(
    connection: sqlite3.Connection, cls: type[_T], table: str, where: str, params: tuple[Any, ...]
) -> _T | None:
    names = _names(cls)
    row = connection.execute(
        f"SELECT {', '.join(names)} FROM {table} WHERE {where}", params
    ).fetchone()
    return None if row is None else cls(*row)


def read_head(connection: sqlite3.Connection, *, workspace_id: str, document_id: str) -> DocumentHead | None:
    return _select(
        connection,
        DocumentHead,
        "omnivia_document_heads",
        "workspace_id = ? AND document_id = ?",
        (workspace_id, document_id),
    )


def read_capture(
    connection: sqlite3.Connection, *, workspace_id: str, capture_id: str
) -> StoredCapture | None:
    return _select(
        connection,
        StoredCapture,
        "omnivia_document_captures",
        "workspace_id = ? AND capture_id = ?",
        (workspace_id, capture_id),
    )


def read_capture_by_key(
    connection: sqlite3.Connection, *, workspace_id: str, operation_key: str
) -> StoredCapture | None:
    return _select(
        connection,
        StoredCapture,
        "omnivia_document_captures",
        "workspace_id = ? AND operation_key = ?",
        (workspace_id, operation_key),
    )


def read_retention(
    connection: sqlite3.Connection, *, workspace_id: str, capture_id: str
) -> CaptureRetention | None:
    return _select(
        connection,
        CaptureRetention,
        "omnivia_document_capture_retention",
        "workspace_id = ? AND capture_id = ?",
        (workspace_id, capture_id),
    )


def read_revision(
    connection: sqlite3.Connection, *, workspace_id: str, extraction_revision_id: str
) -> StoredRevision | None:
    return _select(
        connection,
        StoredRevision,
        "omnivia_extraction_revisions",
        "workspace_id = ? AND extraction_revision_id = ?",
        (workspace_id, extraction_revision_id),
    )


def read_revision_by_key(
    connection: sqlite3.Connection, *, workspace_id: str, operation_key: str
) -> StoredRevision | None:
    return _select(
        connection,
        StoredRevision,
        "omnivia_extraction_revisions",
        "workspace_id = ? AND operation_key = ?",
        (workspace_id, operation_key),
    )


def read_result(
    connection: sqlite3.Connection, *, workspace_id: str, extraction_revision_id: str
) -> StoredResult | None:
    return _select(
        connection,
        StoredResult,
        "omnivia_extraction_results",
        "workspace_id = ? AND extraction_revision_id = ?",
        (workspace_id, extraction_revision_id),
    )


def read_profile(
    connection: sqlite3.Connection, *, workspace_id: str, digest: str
) -> dict[str, Any] | None:
    """The stored profile, after its digest is recomputed from the stored JSON, or `None`."""
    row = connection.execute(
        "SELECT profile_json FROM omnivia_extraction_profiles WHERE workspace_id = ? AND profile_digest = ?",
        (workspace_id, digest),
    ).fetchone()
    if row is None:
        return None
    text = str(row[0])
    if "sha256:" + _sha256_hex(text) != digest:
        raise DocumentIntakeInvalid("stored extraction profile does not verify its identity")
    profile: dict[str, Any] = json.loads(text)
    if canonical_profile(profile) != text:
        raise DocumentIntakeInvalid("stored extraction profile is not canonical")
    return profile


def record_profile(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    profile: dict[str, Any],
    fencing_generation: int,
    now_us: int,
) -> str:
    """Persist `profile` under the caller's fence, or find it already stored; return its digest."""
    document = canonical_profile(profile)
    digest = "sha256:" + _sha256_hex(document)
    if read_profile(connection, workspace_id=workspace_id, digest=digest) is None:
        connection.execute(
            "INSERT INTO omnivia_extraction_profiles "
            "(workspace_id, profile_digest, profile_json, fencing_generation, created_at_us) "
            "VALUES (?, ?, ?, ?, ?)",
            (workspace_id, digest, document, fencing_generation, now_us),
        )
    return digest


def ensure_document(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    document_id: str,
    fencing_generation: int,
    now_us: int,
) -> DocumentHead:
    """The document's head, creating the document and its empty head on first use."""
    head = read_head(connection, workspace_id=workspace_id, document_id=document_id)
    if head is not None:
        return head
    connection.execute(
        "INSERT INTO omnivia_documents (workspace_id, document_id, fencing_generation, created_at_us) "
        "VALUES (?, ?, ?, ?)",
        (workspace_id, document_id, fencing_generation, now_us),
    )
    connection.execute(
        "INSERT INTO omnivia_document_heads (workspace_id, document_id, source_state, "
        "source_removal_generation, current_capture_id, capture_head_sequence, current_extraction_id, "
        "extraction_head_ordinal, fencing_generation, updated_at_us) "
        "VALUES (?, ?, 'active', 1, NULL, 0, NULL, 0, ?, ?)",
        (workspace_id, document_id, fencing_generation, now_us),
    )
    head = read_head(connection, workspace_id=workspace_id, document_id=document_id)
    assert head is not None
    return head


def last_capture_sequence(connection: sqlite3.Connection, *, workspace_id: str, document_id: str) -> int:
    return int(
        connection.execute(
            "SELECT COALESCE(MAX(capture_sequence), 0) FROM omnivia_document_captures "
            "WHERE workspace_id = ? AND document_id = ?",
            (workspace_id, document_id),
        ).fetchone()[0]
    )


def insert_capture(
    connection: sqlite3.Connection, capture: StoredCapture, *, now_us: int
) -> None:
    """Insert one capture and its retained retention row; the caller advances the head when it is eligible."""
    _insert(connection, "omnivia_document_captures", capture)
    connection.execute(
        "INSERT INTO omnivia_document_capture_retention (workspace_id, capture_id, retention_state, "
        "capture_retention_generation, fencing_generation, changed_at_us) VALUES (?, ?, 'retained', 1, ?, ?)",
        (capture.workspace_id, capture.capture_id, capture.fencing_generation, now_us),
    )


def advance_capture_head(
    connection: sqlite3.Connection, capture: StoredCapture, *, now_us: int
) -> None:
    cursor = connection.execute(
        "UPDATE omnivia_document_heads SET current_capture_id = ?, capture_head_sequence = ?, "
        "fencing_generation = ?, updated_at_us = ? WHERE workspace_id = ? AND document_id = ?",
        (
            capture.capture_id,
            capture.capture_sequence,
            capture.fencing_generation,
            now_us,
            capture.workspace_id,
            capture.document_id,
        ),
    )
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("the document head moved under this write")


def last_extraction_numbers(
    connection: sqlite3.Connection, *, workspace_id: str, document_id: str, capture_id: str
) -> tuple[int, int]:
    """The last per-capture extraction sequence and the last per-document extraction ordinal."""
    row = connection.execute(
        "SELECT (SELECT COALESCE(MAX(extraction_sequence), 0) FROM omnivia_extraction_revisions "
        "WHERE workspace_id = ? AND capture_id = ?), "
        "(SELECT COALESCE(MAX(document_ordinal), 0) FROM omnivia_extraction_revisions "
        "WHERE workspace_id = ? AND document_id = ?)",
        (workspace_id, capture_id, workspace_id, document_id),
    ).fetchone()
    return int(row[0]), int(row[1])


def insert_revision(connection: sqlite3.Connection, revision: StoredRevision) -> None:
    _insert(connection, "omnivia_extraction_revisions", revision)


def insert_result(connection: sqlite3.Connection, result: StoredResult) -> None:
    _insert(connection, "omnivia_extraction_results", result)


def advance_extraction_head(
    connection: sqlite3.Connection, revision: StoredRevision, *, fencing_generation: int, now_us: int
) -> None:
    cursor = connection.execute(
        "UPDATE omnivia_document_heads SET current_extraction_id = ?, extraction_head_ordinal = ?, "
        "fencing_generation = ?, updated_at_us = ? WHERE workspace_id = ? AND document_id = ?",
        (
            revision.extraction_revision_id,
            revision.document_ordinal,
            fencing_generation,
            now_us,
            revision.workspace_id,
            revision.document_id,
        ),
    )
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("the document head moved under this write")


def set_capture_retention(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    capture_id: str,
    state: str,
    fencing_generation: int,
    now_us: int,
) -> int:
    """Move a capture's retention forward to `state` and return its new generation."""
    if state not in RETENTION_STATES - {RETENTION_RETAINED}:
        raise DocumentIntakeInvalid("a capture's retention only moves to expired or deleted")
    current = read_retention(connection, workspace_id=workspace_id, capture_id=capture_id)
    if current is None:
        raise DocumentIntakeInvalid("no such capture")
    generation = current.capture_retention_generation + 1
    cursor = connection.execute(
        "UPDATE omnivia_document_capture_retention SET retention_state = ?, "
        "capture_retention_generation = ?, fencing_generation = ?, changed_at_us = ? "
        "WHERE workspace_id = ? AND capture_id = ? AND capture_retention_generation = ?",
        (
            state,
            generation,
            fencing_generation,
            now_us,
            workspace_id,
            capture_id,
            current.capture_retention_generation,
        ),
    )
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("the capture retention moved under this write")
    return generation


def set_source_state(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    document_id: str,
    state: str,
    fencing_generation: int,
    now_us: int,
) -> int:
    """Switch a document between active and removed and return its new source removal generation."""
    head = read_head(connection, workspace_id=workspace_id, document_id=document_id)
    if head is None:
        raise DocumentIntakeInvalid("no such document")
    if state not in {SOURCE_ACTIVE, SOURCE_REMOVED} or state == head.source_state:
        raise DocumentIntakeInvalid("a source state change must name the other state")
    generation = head.source_removal_generation + 1
    cursor = connection.execute(
        "UPDATE omnivia_document_heads SET source_state = ?, source_removal_generation = ?, "
        "fencing_generation = ?, updated_at_us = ? "
        "WHERE workspace_id = ? AND document_id = ? AND source_removal_generation = ?",
        (
            state,
            generation,
            fencing_generation,
            now_us,
            workspace_id,
            document_id,
            head.source_removal_generation,
        ),
    )
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("the source state moved under this write")
    return generation
