"""Logical documents, original bindings, immutable source captures, their outbox and immutable extraction revisions:
persistence only (migration 0069).

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

An original binding is an opaque owner/device/access reference plus an optional opaque root and member; no path,
bookmark or credential reaches this module. A capture records the binding lineage it was captured under, its intent
kind and a stable disposition reason, and `decide_capture` is the one place those are derived, in the order the
migration's insert guard recomputes them.
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
#: The optional `ocr.resourceState` member. It is absent when the pinned models were verified, which keeps every
#: previously written profile byte-identical, and present only to state that the selected resources were not served.
#: A local-OCR profile that says so keeps its mode and its requested ordered languages while pinning no model hash,
#: because claiming a digest the invocation never used would be the dishonest encoding.
OCR_RESOURCE_STATES: Final = frozenset({"verified", "unavailable"})
MAX_PROFILE_BYTES: Final = 65536
MAX_SEGMENT_JSON_BYTES: Final = 131072
MAX_SEGMENTS_PER_REVISION: Final = 100000

DISPOSITION_HEAD_ELIGIBLE: Final = "head_eligible"
DISPOSITION_HISTORICAL_ONLY: Final = "historical_only"

INTENT_NEW_CAPTURE: Final = "new_capture"
INTENT_REFRESH: Final = "refresh"
INTENT_AUTOMATIC: Final = "automatic_capture"
INTENT_FOLDER: Final = "folder_capture"
INTENTS: Final = frozenset({INTENT_NEW_CAPTURE, INTENT_REFRESH, INTENT_AUTOMATIC, INTENT_FOLDER})
#: Intents that may be suppressed when identical bytes were explicitly expired or deleted.
AUTOMATIC_INTENTS: Final = frozenset({INTENT_AUTOMATIC, INTENT_FOLDER})

ASSOCIATION_SNAPSHOT_ONLY: Final = "snapshot_only"
ASSOCIATION_ACTIVE_BINDING: Final = "active_binding"
ASSOCIATION_HISTORICAL_BINDING: Final = "historical_binding"

REASON_ELIGIBLE: Final = "eligible"
REASON_REMOVED_SOURCE: Final = "removed_source"
REASON_SUPERSEDED_GENERATION: Final = "superseded_generation"
REASON_DISCONNECTED_GENERATION: Final = "disconnected_generation"
REASON_CREATION_CONFLICT: Final = "creation_conflict"
REASON_STALE_BASE: Final = "stale_base"

ROUTE_DIRECT_FILE: Final = "direct_file"
ROUTE_ROOT_MEMBER: Final = "root_member"
ROUTE_KINDS: Final = frozenset({ROUTE_DIRECT_FILE, ROUTE_ROOT_MEMBER})
BINDING_CONNECTED: Final = "connected"
BINDING_DISCONNECTED: Final = "disconnected"
#: Not a stored `connection_state`: the aggregate a document with no recorded route at all has.
BINDING_NONE: Final = "none"

#: The largest page one bounded history read serves, for captures and for revisions alike.
MAX_HISTORY_PAGE: Final = 200

OUTBOX_CAPTURE_PROCESSING: Final = "capture_processing"
OUTBOX_PENDING: Final = "pending"
OUTBOX_SETTLED: Final = "settled"
MAX_OUTBOX_BATCH: Final = 1000
RETENTION_RETAINED: Final = "retained"
RETENTION_STATES: Final = frozenset({"retained", "expired", "deleted"})
SOURCE_ACTIVE: Final = "active"
SOURCE_REMOVED: Final = "removed"

RESULT_STATES: Final = frozenset({"succeeded", "partial", "unsupported", "failed"})
PUBLISHABLE_STATES: Final = frozenset({"succeeded", "partial"})
PUBLICATION_DISPOSITIONS: Final = frozenset(
    {"promoted", "superseded", "historical_only", "fenced_removal", "fenced_retention", "not_published"}
)

SEGMENT_ROLES: Final = frozenset(
    {
        "body",
        "heading",
        "list_item",
        "table_cell",
        "formula",
        "saved_value",
        "chart",
        "image_alt",
        "equation",
        "comment",
        "note",
        "hidden_content",
        "deleted_text",
        "field_instruction",
        "object_inventory",
    }
)
DEFAULT_RETRIEVAL_ROLES: Final = frozenset(
    {"body", "heading", "list_item", "table_cell", "formula", "saved_value", "chart", "image_alt", "equation"}
)
SEGMENT_METHODS: Final = frozenset({"direct", "ocr", "derived", "inventory"})
SEGMENT_VISIBILITIES: Final = frozenset({"visible", "hidden", "annotation"})
SEGMENT_REVISIONS: Final = frozenset({"current", "inserted", "deleted", "none"})

PROFILE_FIELDS: Final = frozenset(
    {
        "schema",
        "adapter",
        "adapterVersion",
        "decoderBuild",
        "processingMode",
        "ocr",
        "embeddedImageOcr",
        "revisionView",
        "secondaryContentPolicy",
        "formulaPolicy",
        "html",
        "delimited",
        "limits",
        "readingOrderVersion",
        "deduplicationVersion",
        "parserFeatures",
        "unknownFeaturePolicy",
        "completenessPolicy",
    }
)
PROFILE_LIMIT_FIELDS: Final = frozenset(
    {
        "sourceBytes",
        "decodedPixels",
        "containerEntries",
        "expandedBytes",
        "outputTextBytes",
        "outputStructuredBytes",
        "memoryBytes",
        "timeMs",
        "temporaryBytes",
    }
)

_T = TypeVar("_T")


class DocumentIntakeInvalid(ValueError):
    """A value handed to storage, or a stored row read back, is outside its closed shape or fails its identity."""


def _sha256_hex(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def request_digest(request: dict[str, Any]) -> str:
    """The digest of one canonical request, as an operation key's replay check compares it."""
    return "sha256:" + _sha256_hex(to_canonical_json(request))


def capture_request_digest(request: dict[str, Any], *, association: str, disposition: str, reason: str) -> str:
    """The digest a capture stores: its caller request (intent and binding lineage included) and the decision made.

    A replay recomputes it from the new request and the stored decision, so a changed request, or a stored decision
    that no longer matches the request it was made for, is a different digest.
    """
    return request_digest(
        {**request, "association": association, "disposition": disposition, "dispositionReason": reason}
    )


def capture_id_for(workspace_id: str, operation_key: str) -> str:
    return CAPTURE_PREFIX + _sha256_hex(f"{workspace_id}\0{operation_key}")


def revision_id_for(workspace_id: str, operation_key: str) -> str:
    return REVISION_PREFIX + _sha256_hex(f"{workspace_id}\0{operation_key}")


def canonical_profile(profile: dict[str, Any]) -> str:
    """The canonical JSON of an ExtractionProfileV1, after the closed fields it must carry are checked."""
    if not isinstance(profile, dict) or profile.get("schema") != PROFILE_SCHEMA:
        raise DocumentIntakeInvalid("extraction profile has the wrong schema")
    if frozenset(profile) != PROFILE_FIELDS:
        raise DocumentIntakeInvalid("extraction profile fields are not the closed v1 shape")
    if profile.get("processingMode") not in PROCESSING_MODES:
        raise DocumentIntakeInvalid("extraction profile has an unknown processing mode")
    for key in (
        "adapter",
        "adapterVersion",
        "decoderBuild",
        "readingOrderVersion",
        "deduplicationVersion",
    ):
        value = profile.get(key)
        if not isinstance(value, str) or not value:
            raise DocumentIntakeInvalid(f"extraction profile needs a non-empty {key}")
    if not isinstance(profile.get("embeddedImageOcr"), bool):
        raise DocumentIntakeInvalid("extraction profile embedded-image OCR must be boolean")
    if profile.get("revisionView") != "current_text_with_separate_annotations":
        raise DocumentIntakeInvalid("extraction profile has an unknown revision view")
    if profile.get("secondaryContentPolicy") != "preserve_role_and_visibility":
        raise DocumentIntakeInvalid("extraction profile has an unknown secondary-content policy")
    if profile.get("formulaPolicy") != "expression_and_saved_cache_without_calculation":
        raise DocumentIntakeInvalid("extraction profile has an unknown formula policy")
    if profile.get("unknownFeaturePolicy") not in {"partial", "unsupported"}:
        raise DocumentIntakeInvalid("extraction profile has an unknown-feature policy outside v1")
    if profile.get("completenessPolicy") != "explicit_unit_coverage":
        raise DocumentIntakeInvalid("extraction profile has an unknown completeness policy")
    _validate_ocr_profile(profile.get("ocr"), processing_mode=str(profile["processingMode"]))
    _validate_text_decision(profile.get("html"), kind="html", allowed_fields={"encoding"})
    _validate_text_decision(
        profile.get("delimited"),
        kind="delimited",
        allowed_fields={"encoding", "delimiter", "quote"},
    )
    limits = profile.get("limits")
    if not isinstance(limits, dict) or frozenset(limits) != PROFILE_LIMIT_FIELDS:
        raise DocumentIntakeInvalid("extraction profile limits are not the closed v1 shape")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 2**63 - 1
        for value in limits.values()
    ):
        raise DocumentIntakeInvalid("extraction profile limits must be positive integers")
    features = profile.get("parserFeatures")
    if (
        not isinstance(features, list)
        or not features
        or len(features) > 128
        or any(not isinstance(value, str) or not value or len(value) > 128 for value in features)
        or len(set(features)) != len(features)
        or features != sorted(features)
    ):
        raise DocumentIntakeInvalid("extraction profile parser features must be a sorted unique string list")
    try:
        document = to_canonical_json(profile)
    except (TypeError, ValueError) as error:
        raise DocumentIntakeInvalid("extraction profile is not canonical JSON") from error
    if len(document.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise DocumentIntakeInvalid("extraction profile exceeds the accepted bound")
    return document


def _validate_ocr_profile(value: Any, *, processing_mode: str) -> None:
    if not isinstance(value, dict) or frozenset(value) - {"resourceState"} != {
        "engine",
        "engineRevision",
        "modelHashes",
        "languages",
    }:
        raise DocumentIntakeInvalid("extraction profile OCR fields are not the closed v1 shape")
    resource_state = value.get("resourceState")
    if resource_state is not None and resource_state not in OCR_RESOURCE_STATES:
        raise DocumentIntakeInvalid("extraction profile has an unknown OCR resource state")
    engine = value.get("engine")
    revision = value.get("engineRevision")
    hashes = value.get("modelHashes")
    languages = value.get("languages")
    if engine not in {"none", "tesseract", "apple_vision"}:
        raise DocumentIntakeInvalid("extraction profile has an unknown OCR engine")
    if not isinstance(revision, str) or not revision:
        raise DocumentIntakeInvalid("extraction profile needs an OCR engine revision")
    if (
        not isinstance(hashes, list)
        or len(hashes) > 16
        or any(
            not isinstance(digest, str)
            or len(digest) != 71
            or not digest.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in digest[7:])
            for digest in hashes
        )
        or hashes != sorted(set(hashes))
    ):
        raise DocumentIntakeInvalid("extraction profile OCR model hashes are invalid")
    if (
        not isinstance(languages, list)
        or len(languages) > 3
        or any(not isinstance(language, str) or not language for language in languages)
        or len(set(languages)) != len(languages)
    ):
        raise DocumentIntakeInvalid("extraction profile OCR languages are invalid")
    if processing_mode == "text_only" and (
        engine != "none" or hashes or languages or resource_state is not None
    ):
        raise DocumentIntakeInvalid("a text-only profile cannot select OCR resources")
    unavailable = resource_state == "unavailable"
    if processing_mode == "text_and_local_ocr" and (
        engine == "none" or not languages or (not hashes and not unavailable)
    ):
        raise DocumentIntakeInvalid("a local-OCR profile must pin its engine, models and languages")
    if unavailable and hashes:
        raise DocumentIntakeInvalid("an unavailable OCR selection cannot pin model hashes")


def _validate_text_decision(value: Any, *, kind: str, allowed_fields: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != allowed_fields:
        raise DocumentIntakeInvalid(f"extraction profile {kind} fields are not the closed v1 shape")
    if any(not isinstance(item, str) or not item for item in value.values()):
        raise DocumentIntakeInvalid(f"extraction profile {kind} decisions must be non-empty strings")


def profile_digest(profile: dict[str, Any]) -> str:
    """ExtractionProfileV1 identity: the SHA-256 of its canonical JSON."""
    return "sha256:" + _sha256_hex(canonical_profile(profile))


@dataclass(frozen=True, slots=True)
class StoredDocument:
    workspace_id: str
    document_id: str
    fencing_generation: int
    created_at_us: int


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
    updated_at_us: int


@dataclass(frozen=True, slots=True)
class StoredBinding:
    workspace_id: str
    owner_ref: str
    document_id: str
    route_kind: str
    root_ref: str | None
    member_ref: str | None
    binding_generation: int
    connection_state: str
    fencing_generation: int
    created_at_us: int
    changed_at_us: int


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
    requested_source_identity: str
    target_binding_revision: str
    processing_profile_ref: str
    processing_profile_revision: str
    consent_json: str | None
    bulk_origin_json: str | None
    display_name: str | None
    expected_base_capture_id: str | None
    intent_kind: str
    association_kind: str
    binding_owner_ref: str | None
    binding_generation: int | None
    binding_route_kind: str | None
    binding_root_ref: str | None
    binding_member_ref: str | None
    disposition: str
    disposition_reason: str
    source_removal_generation: int
    operation_key: str
    request_digest: str
    fencing_generation: int
    created_at_us: int
    #: The processing preferences this capture was admitted under, as the canonical
    #: JSON of one settings document (migration 0071). `None` only for a capture
    #: admitted before that migration, which recorded no selection. The field is
    #: last because `_insert`/`_select` derive their column lists from the field
    #: order, and a defaulted field cannot precede an undefaulted one.
    processing_settings_json: str | None = None


@dataclass(frozen=True, slots=True)
class CaptureRetention:
    workspace_id: str
    capture_id: str
    retention_state: str
    capture_retention_generation: int


@dataclass(frozen=True, slots=True)
class StoredOutbox:
    workspace_id: str
    capture_id: str
    kind: str
    document_id: str
    outbox_sequence: int
    outbox_state: str
    fencing_generation: int
    created_at_us: int
    settled_at_us: int | None


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
    segments_digest: str
    coverage_json: str
    limitations_json: str
    worker_build: str
    publication_disposition: str
    fencing_generation: int
    completed_at_us: int


@dataclass(frozen=True, slots=True)
class StoredSegment:
    workspace_id: str
    extraction_revision_id: str
    segment_ordinal: int
    semantic_role: str
    text: str | None
    structured_value_json: str | None
    source_locator_json: str
    extraction_method: str
    visibility: str
    revision_state: str
    language: str | None
    confidence: float | None
    fencing_generation: int


@dataclass(frozen=True, slots=True)
class ProjectedSegment:
    workspace_id: str
    document_id: str
    extraction_revision_id: str
    capture_id: str
    profile_digest: str
    segment_ordinal: int
    semantic_role: str
    text: str | None
    structured_value_json: str | None
    source_locator_json: str
    extraction_method: str
    visibility: str
    revision_state: str
    language: str | None
    confidence: float | None
    fencing_generation: int
    published_at_us: int


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


def read_document(
    connection: sqlite3.Connection, *, workspace_id: str, document_id: str
) -> StoredDocument | None:
    """The logical document row, which is the authority on whether this workspace owns it."""
    return _select(
        connection,
        StoredDocument,
        "omnivia_documents",
        "workspace_id = ? AND document_id = ?",
        (workspace_id, document_id),
    )


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


def read_binding(
    connection: sqlite3.Connection, *, workspace_id: str, owner_ref: str
) -> StoredBinding | None:
    return _select(
        connection,
        StoredBinding,
        "omnivia_document_original_bindings",
        "workspace_id = ? AND owner_ref = ?",
        (workspace_id, owner_ref),
    )


def read_outbox(
    connection: sqlite3.Connection, *, workspace_id: str, capture_id: str
) -> StoredOutbox | None:
    return _select(
        connection,
        StoredOutbox,
        "omnivia_document_capture_outbox",
        "workspace_id = ? AND capture_id = ? AND kind = ?",
        (workspace_id, capture_id, OUTBOX_CAPTURE_PROCESSING),
    )


def pending_outbox(
    connection: sqlite3.Connection, *, workspace_id: str, limit: int
) -> list[StoredOutbox]:
    """The oldest pending obligations, in admission order, at most `limit` of them."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_OUTBOX_BATCH:
        raise DocumentIntakeInvalid(f"an outbox read is bounded to 1 through {MAX_OUTBOX_BATCH} rows")
    names = _names(StoredOutbox)
    rows = connection.execute(
        f"SELECT {', '.join(names)} FROM omnivia_document_capture_outbox "
        "WHERE workspace_id = ? AND outbox_state = 'pending' ORDER BY outbox_sequence LIMIT ?",
        (workspace_id, limit),
    ).fetchall()
    return [StoredOutbox(*row) for row in rows]


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


def read_segments(
    connection: sqlite3.Connection, *, workspace_id: str, extraction_revision_id: str
) -> list[StoredSegment]:
    names = _names(StoredSegment)
    rows = connection.execute(
        f"SELECT {', '.join(names)} FROM omnivia_extracted_segments "
        "WHERE workspace_id = ? AND extraction_revision_id = ? ORDER BY segment_ordinal",
        (workspace_id, extraction_revision_id),
    ).fetchall()
    return [StoredSegment(*row) for row in rows]


def read_projection(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    document_id: str,
    roles: frozenset[str] = DEFAULT_RETRIEVAL_ROLES,
    query: str | None = None,
) -> list[ProjectedSegment]:
    """Read the current extraction projection, limited to explicitly allowed semantic roles."""
    if not roles or not roles <= SEGMENT_ROLES:
        raise DocumentIntakeInvalid("retrieval roles are outside the accepted semantic-role set")
    names = _names(ProjectedSegment)
    placeholders = ", ".join("?" for _ in roles)
    params: list[Any] = [workspace_id, document_id, *sorted(roles)]
    predicate = ""
    if query is not None:
        normalized = query.strip().casefold()
        if not normalized:
            raise DocumentIntakeInvalid("a projection search needs a non-empty query")
        predicate = " AND text IS NOT NULL AND instr(lower(text), ?) > 0"
        params.append(normalized)
    rows = connection.execute(
        f"SELECT {', '.join(names)} FROM omnivia_document_extraction_projection "
        f"WHERE workspace_id = ? AND document_id = ? AND semantic_role IN ({placeholders})"
        f"{predicate} ORDER BY segment_ordinal",
        tuple(params),
    ).fetchall()
    return [ProjectedSegment(*row) for row in rows]


@dataclass(frozen=True, slots=True)
class CaptureHistoryRow:
    """One capture with the two facts stored beside it rather than on it.

    Retention and the processing obligation live in their own tables and move on their own
    generations, so they are read with the capture rather than inferred from it. `outbox_state`
    is `None` when no obligation was ever recorded, which is not the same fact as a settled one.
    """

    capture: StoredCapture
    retention_state: str
    capture_retention_generation: int
    retention_changed_at_us: int
    outbox_state: str | None


@dataclass(frozen=True, slots=True)
class ExtractionHistoryRow:
    """One extraction revision with its terminal result, which is absent until it has one."""

    revision: StoredRevision
    result: StoredResult | None


def _page_bounds(offset: int, limit: int) -> None:
    for name, value, low in (("offset", offset, 0), ("limit", limit, 1)):
        if not isinstance(value, int) or isinstance(value, bool) or value < low:
            raise DocumentIntakeInvalid(f"a history {name} is outside its bound")
    if limit > MAX_HISTORY_PAGE:
        raise DocumentIntakeInvalid(f"a history read is bounded to {MAX_HISTORY_PAGE} rows")


#: The two positions in a `capture_fingerprints` row a caller reads by meaning rather than
#: by digesting the whole row. Stated here so the SELECT below and its one reader cannot
#: drift into disagreeing about which column is which.
CAPTURE_FACT_RETENTION_STATE: Final = 3
CAPTURE_FACT_MEDIA_TYPE: Final = 7


def capture_fingerprints(
    connection: sqlite3.Connection, *, workspace_id: str, document_id: str
) -> list[tuple[Any, ...]]:
    """Every capture fact a later write can still change, plus the media type, per document.

    The other immutable capture columns are deliberately absent: a capture is never edited,
    so its identity and its ordering number are the whole of what detects one appearing. What
    does move is retention, the generation and instant that moved it, and the processing
    obligation -- and each of those changes what a page of history says about a capture that
    an earlier page already returned. The row count is the document's complete capture total,
    so a separate `COUNT(*)` would be a second statement of the same fact read at a second
    instant.

    `media_type` is the one immutable column here, and it is not here to detect a change. It
    is here so one read tells its caller the complete set of media types the document's
    active retained captures represent: the approved reprocess profiles for each of those
    types can then be resolved once, from this same read, instead of once per capture per
    page. Resolving them per page is what let installed resources move between two pages of
    one series without the snapshot noticing.
    """
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT c.capture_id, c.capture_sequence, c.source_removal_generation, "
            "r.retention_state, r.capture_retention_generation, r.changed_at_us, "
            "o.outbox_state, c.media_type FROM omnivia_document_captures c "
            "LEFT JOIN omnivia_document_capture_retention r "
            "  ON r.workspace_id = c.workspace_id AND r.capture_id = c.capture_id "
            "LEFT JOIN omnivia_document_capture_outbox o "
            "  ON o.workspace_id = c.workspace_id AND o.capture_id = c.capture_id "
            "  AND o.kind = ? "
            "WHERE c.workspace_id = ? AND c.document_id = ? ORDER BY c.capture_sequence",
            (OUTBOX_CAPTURE_PROCESSING, workspace_id, document_id),
        ).fetchall()
    ]


def read_capture_history(
    connection: sqlite3.Connection, *, workspace_id: str, document_id: str, offset: int, limit: int
) -> list[CaptureHistoryRow]:
    """One bounded page of a document's immutable captures, in ascending admission sequence.

    `(workspace_id, document_id, capture_sequence)` is unique, so that order is total and a
    page is stable: captures are append-only, so a later page cannot renumber an earlier one.
    """
    _page_bounds(offset, limit)
    names = _names(StoredCapture)
    columns = ", ".join(f"c.{name}" for name in names)
    rows = connection.execute(
        f"SELECT {columns}, r.retention_state, r.capture_retention_generation, r.changed_at_us, "
        "o.outbox_state FROM omnivia_document_captures c "
        # Both joins are LEFT joins, and the retention one is checked below rather than
        # relied on: an inner join would silently drop a capture whose retention row is
        # missing, and a page short by one row is a page whose totals and continuation
        # would then never agree. A missing row is refused instead.
        "LEFT JOIN omnivia_document_capture_retention r "
        "  ON r.workspace_id = c.workspace_id AND r.capture_id = c.capture_id "
        "LEFT JOIN omnivia_document_capture_outbox o "
        "  ON o.workspace_id = c.workspace_id AND o.capture_id = c.capture_id AND o.kind = ? "
        "WHERE c.workspace_id = ? AND c.document_id = ? "
        "ORDER BY c.capture_sequence LIMIT ? OFFSET ?",
        (OUTBOX_CAPTURE_PROCESSING, workspace_id, document_id, limit, offset),
    ).fetchall()
    width = len(names)
    history: list[CaptureHistoryRow] = []
    for row in rows:
        if row[width] is None:
            raise DocumentIntakeInvalid("a capture holds no retention row")
        history.append(
            CaptureHistoryRow(
                capture=StoredCapture(*row[:width]),
                retention_state=str(row[width]),
                capture_retention_generation=int(row[width + 1]),
                retention_changed_at_us=int(row[width + 2]),
                outbox_state=None if row[width + 3] is None else str(row[width + 3]),
            )
        )
    return history


def extraction_fingerprints(
    connection: sqlite3.Connection, *, workspace_id: str, document_id: str
) -> list[tuple[Any, ...]]:
    """Every extraction-revision fact a later write can still change, for the whole document.

    A revision is immutable once admitted, but the result beside it arrives afterwards and
    turns a pending revision into a terminal one with an outcome, a disposition and a digest.
    As with the captures, the row count is the document's complete revision total.
    """
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT v.extraction_revision_id, v.document_ordinal, v.extraction_sequence, "
            "v.head_eligible, v.profile_digest, x.result_state, x.publication_disposition, "
            "x.output_digest, x.worker_build, x.completed_at_us "
            "FROM omnivia_extraction_revisions v LEFT JOIN omnivia_extraction_results x "
            "  ON x.workspace_id = v.workspace_id "
            "  AND x.extraction_revision_id = v.extraction_revision_id "
            "WHERE v.workspace_id = ? AND v.document_id = ? ORDER BY v.document_ordinal",
            (workspace_id, document_id),
        ).fetchall()
    ]


def read_extraction_history(
    connection: sqlite3.Connection, *, workspace_id: str, document_id: str, offset: int, limit: int
) -> list[ExtractionHistoryRow]:
    """One bounded page of a document's extraction revisions, in ascending document ordinal.

    `(workspace_id, document_id, document_ordinal)` is unique, so that order is total across
    every capture of the document. The ordinal is an ordering fact only: which revision is
    current is `omnivia_document_heads.current_extraction_id` and nothing else.
    """
    _page_bounds(offset, limit)
    revision_names = _names(StoredRevision)
    result_names = _names(StoredResult)
    columns = ", ".join(
        [*(f"v.{name}" for name in revision_names), *(f"x.{name}" for name in result_names)]
    )
    rows = connection.execute(
        f"SELECT {columns} FROM omnivia_extraction_revisions v "
        "LEFT JOIN omnivia_extraction_results x "
        "  ON x.workspace_id = v.workspace_id AND x.extraction_revision_id = v.extraction_revision_id "
        "WHERE v.workspace_id = ? AND v.document_id = ? "
        "ORDER BY v.document_ordinal LIMIT ? OFFSET ?",
        (workspace_id, document_id, limit, offset),
    ).fetchall()
    width = len(revision_names)
    return [
        ExtractionHistoryRow(
            revision=StoredRevision(*row[:width]),
            result=None if row[width] is None else StoredResult(*row[width:]),
        )
        for row in rows
    ]


def document_binding_state(
    connection: sqlite3.Connection, *, workspace_id: str, document_id: str
) -> str:
    """Whether this document has a connected original route, only disconnected ones, or none.

    An aggregate over the opaque routes, never one of them: no owner reference, root, member
    or generation leaves this function, because none of those is a fact a reader of history
    needs and every one of them is native-owner material.
    """
    row = connection.execute(
        "SELECT COUNT(*), COALESCE(SUM(connection_state = ?), 0) "
        "FROM omnivia_document_original_bindings WHERE workspace_id = ? AND document_id = ?",
        (BINDING_CONNECTED, workspace_id, document_id),
    ).fetchone()
    if int(row[0]) == 0:
        return BINDING_NONE
    return BINDING_CONNECTED if int(row[1]) > 0 else BINDING_DISCONNECTED


def projection_published_at(
    connection: sqlite3.Connection, *, workspace_id: str, document_id: str
) -> int | None:
    """When this document's current retrieval projection was published, or `None` if nothing is.

    `replace_projection` writes every row of one promotion with the same instant, so the
    maximum is that instant. `None` means retrieval holds nothing for this document now --
    which a pending, failed or historical-only extraction leaves exactly as it found it.
    """
    row = connection.execute(
        "SELECT MAX(published_at_us) FROM omnivia_document_extraction_projection "
        "WHERE workspace_id = ? AND document_id = ?",
        (workspace_id, document_id),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


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


def insert_binding(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_ref: str,
    document_id: str,
    route_kind: str,
    root_ref: str | None,
    member_ref: str | None,
    state: str,
    fencing_generation: int,
    now_us: int,
) -> StoredBinding:
    """Record a binding at generation one, connected or (for a tombstone that came first) disconnected."""
    binding = StoredBinding(
        workspace_id, owner_ref, document_id, route_kind, root_ref, member_ref, 1, state,
        fencing_generation, now_us, now_us,
    )  # fmt: skip
    _insert(connection, "omnivia_document_original_bindings", binding)
    return binding


def move_binding(
    connection: sqlite3.Connection,
    binding: StoredBinding,
    *,
    connect: bool,
    fencing_generation: int,
    now_us: int,
) -> StoredBinding:
    """Disconnect a connected binding at its generation, or reconnect a disconnected one a generation later."""
    generation = binding.binding_generation + (1 if connect else 0)
    state = BINDING_CONNECTED if connect else BINDING_DISCONNECTED
    cursor = connection.execute(
        "UPDATE omnivia_document_original_bindings SET connection_state = ?, binding_generation = ?, "
        "fencing_generation = ?, changed_at_us = ? "
        "WHERE workspace_id = ? AND owner_ref = ? AND binding_generation = ? AND connection_state = ?",
        (
            state,
            generation,
            fencing_generation,
            now_us,
            binding.workspace_id,
            binding.owner_ref,
            binding.binding_generation,
            binding.connection_state,
        ),
    )
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("the original binding moved under this write")
    moved = read_binding(connection, workspace_id=binding.workspace_id, owner_ref=binding.owner_ref)
    assert moved is not None
    return moved


def decide_capture(
    head: DocumentHead,
    binding: StoredBinding | None,
    *,
    claimed_generation: int | None,
    intent_kind: str,
    expected_base_capture_id: str | None,
    has_captures: bool,
) -> tuple[str, str, str]:
    """`(association, disposition, reason)` for a capture, by the first reason that applies.

    The order is the one the migration's insert guard recomputes: removed source, superseded generation,
    disconnected generation, creation conflict, stale base. A creation conflict is a new capture of a document that
    already has any capture; every other intent is checked against the current capture as its base, null included.
    """
    if binding is None:
        association = ASSOCIATION_SNAPSHOT_ONLY
    elif binding.connection_state == BINDING_CONNECTED and binding.binding_generation == claimed_generation:
        association = ASSOCIATION_ACTIVE_BINDING
    else:
        association = ASSOCIATION_HISTORICAL_BINDING
    reason = REASON_ELIGIBLE
    if head.source_state != SOURCE_ACTIVE:
        reason = REASON_REMOVED_SOURCE
    elif binding is not None and claimed_generation is not None and claimed_generation < binding.binding_generation:
        reason = REASON_SUPERSEDED_GENERATION
    elif binding is not None and binding.connection_state != BINDING_CONNECTED:
        reason = REASON_DISCONNECTED_GENERATION
    elif intent_kind == INTENT_NEW_CAPTURE and has_captures:
        reason = REASON_CREATION_CONFLICT
    elif intent_kind != INTENT_NEW_CAPTURE and expected_base_capture_id != head.current_capture_id:
        reason = REASON_STALE_BASE
    disposition = DISPOSITION_HEAD_ELIGIBLE if reason == REASON_ELIGIBLE else DISPOSITION_HISTORICAL_ONLY
    return association, disposition, reason


def has_nonretained_identical(
    connection: sqlite3.Connection, *, workspace_id: str, document_id: str, source_digest: str
) -> bool:
    """Whether this document already has a capture of the same bytes that was explicitly expired or deleted."""
    row = connection.execute(
        "SELECT 1 FROM omnivia_document_captures c "
        "JOIN omnivia_document_capture_retention r "
        "ON r.workspace_id = c.workspace_id AND r.capture_id = c.capture_id "
        "WHERE c.workspace_id = ? AND c.document_id = ? AND c.source_digest = ? "
        "AND r.retention_state IN ('expired', 'deleted') LIMIT 1",
        (workspace_id, document_id, source_digest),
    ).fetchone()
    return row is not None


def current_retrievable_capture(
    connection: sqlite3.Connection, *, workspace_id: str, document_id: str
) -> StoredCapture | None:
    """The document's head capture if it is still retained; never an older capture in its place."""
    head = read_head(connection, workspace_id=workspace_id, document_id=document_id)
    if head is None or head.current_capture_id is None:
        return None
    retention = read_retention(connection, workspace_id=workspace_id, capture_id=head.current_capture_id)
    if retention is None or retention.retention_state != RETENTION_RETAINED:
        return None
    return read_capture(connection, workspace_id=workspace_id, capture_id=head.current_capture_id)


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


def insert_capture_outbox(
    connection: sqlite3.Connection, capture: StoredCapture, *, now_us: int
) -> StoredOutbox:
    """The one pending capture-processing obligation of a capture; written in the capture's own transaction."""
    sequence = int(
        connection.execute(
            "SELECT COALESCE(MAX(outbox_sequence), 0) FROM omnivia_document_capture_outbox WHERE workspace_id = ?",
            (capture.workspace_id,),
        ).fetchone()[0]
    )
    row = StoredOutbox(
        capture.workspace_id, capture.capture_id, OUTBOX_CAPTURE_PROCESSING, capture.document_id,
        sequence + 1, OUTBOX_PENDING, capture.fencing_generation, now_us, None,
    )  # fmt: skip
    _insert(connection, "omnivia_document_capture_outbox", row)
    return row


def settle_outbox(
    connection: sqlite3.Connection, *, workspace_id: str, capture_id: str, fencing_generation: int, now_us: int
) -> StoredOutbox:
    """Settle a capture's obligation; settling a settled one returns it unchanged."""
    current = read_outbox(connection, workspace_id=workspace_id, capture_id=capture_id)
    if current is None:
        raise DocumentIntakeInvalid("no such capture obligation")
    if current.outbox_state == OUTBOX_SETTLED:
        return current
    cursor = connection.execute(
        "UPDATE omnivia_document_capture_outbox SET outbox_state = 'settled', settled_at_us = ?, "
        "fencing_generation = ? WHERE workspace_id = ? AND capture_id = ? AND kind = ? "
        "AND outbox_state = 'pending'",
        (now_us, fencing_generation, workspace_id, capture_id, OUTBOX_CAPTURE_PROCESSING),
    )
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("the capture obligation moved under this write")
    settled = read_outbox(connection, workspace_id=workspace_id, capture_id=capture_id)
    assert settled is not None
    return settled


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


def insert_segments(connection: sqlite3.Connection, segments: tuple[StoredSegment, ...]) -> None:
    if len(segments) > MAX_SEGMENTS_PER_REVISION:
        raise DocumentIntakeInvalid("an extraction has too many segments")
    for segment in segments:
        _insert(connection, "omnivia_extracted_segments", segment)


def replace_projection(
    connection: sqlite3.Connection,
    revision: StoredRevision,
    segments: tuple[StoredSegment, ...],
    *,
    fencing_generation: int,
    now_us: int,
) -> None:
    """Atomically replace one document's retrieval projection with a promoted revision's segments."""
    connection.execute(
        "DELETE FROM omnivia_document_extraction_projection WHERE workspace_id = ? AND document_id = ?",
        (revision.workspace_id, revision.document_id),
    )
    for segment in segments:
        _insert(
            connection,
            "omnivia_document_extraction_projection",
            ProjectedSegment(
                workspace_id=revision.workspace_id,
                document_id=revision.document_id,
                extraction_revision_id=revision.extraction_revision_id,
                capture_id=revision.capture_id,
                profile_digest=revision.profile_digest,
                segment_ordinal=segment.segment_ordinal,
                semantic_role=segment.semantic_role,
                text=segment.text,
                structured_value_json=segment.structured_value_json,
                source_locator_json=segment.source_locator_json,
                extraction_method=segment.extraction_method,
                visibility=segment.visibility,
                revision_state=segment.revision_state,
                language=segment.language,
                confidence=segment.confidence,
                fencing_generation=fencing_generation,
                published_at_us=now_us,
            ),
        )


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
