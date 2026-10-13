"""Service-owned document intake: capture a source once, extract it as often as asked, order the two separately.

This is a maintenance path in the shape of `service/source_capture.py`, not an application operation. It runs only
while a `ServiceRunner` owns the workspace, so every write uses the same lifetime lock, exclusive connection, lease,
mutation guard and fencing transaction as the live service, and nothing here is reachable over the application
catalogue.

*Capture* reads one local file once, publishes its bytes as a content-addressed blob outside the database
transaction (as source capture does), and then, in one fenced transaction, writes the evidence through
`source_capture._write_capture` and associates it with the logical document. The capture is admitted with the
capture sequence of the document and is the document's current capture only if the caller's expected base was still
the current capture. A capture on a stale base is retained as history and never becomes current, whenever it arrives.
A refresh that fails before the transaction leaves no row, so the previous current capture is untouched.

*Extraction* is a revision over one capture, its exact input digest and one content-addressed profile. Admitting a
revision freezes the document's source removal generation and the capture's retention generation, and a terminal
result is published, and the extraction head moved, in one fenced transaction that rechecks both. The extraction head
and the capture head never move together: an extraction, whatever capture it is over, cannot move the capture head.

*Reprocess captured copy* reads only the retained workspace blob of a capture and re-hashes it. It takes no path,
opens no original and creates no evidence and no capture. Running an extractor is the caller's: this slice ships no
format adapters, OCR, folder monitoring, native bookmarks or retrieval projection. `reprocess_captured_copy` takes the
extractor as a function and records the outcome it returns, or a failed outcome when it raises.

Operation keys are scoped to one operation kind in the workspace: a capture key and an extraction key are separate
namespaces. Replaying a key with the same request returns the stored record and writes nothing, and the same key with
a different request is refused.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.source_capture import (
    _IDENTIFIER,
    MAX_SOURCE_BYTES,
    SourceCaptureRefused,
    _read_source,
    _validate_request,
    _write_capture,
    publish_blob,
)
from omnivia_core_runtime.storage import document_intake as store
from omnivia_core_runtime.workspace.blob_publication import (
    BlobPublicationRefused,
    _opened_blob,
    blob_path,
)

CAPTURE_STATUS_CAPTURED: Final = "captured"
CAPTURE_STATUS_REPLAYED: Final = "replayed"
CAPTURE_METADATA_KIND: Final = "document_intake"


class DocumentIntakeRefused(RuntimeError):
    """The request cannot be admitted without weakening a boundary; the text is fixed and names no path."""


@dataclass(frozen=True, slots=True)
class DocumentCaptureResult:
    """One capture admission. `became_head` is whether the document's current capture is now this capture."""

    status: str
    capture: store.StoredCapture
    became_head: bool


@dataclass(frozen=True, slots=True)
class ExtractionOutcome:
    """What an extractor reports for one revision. `output_digest` is required for succeeded and partial only."""

    state: str
    output_digest: str | None = None
    coverage: Mapping[str, Any] = field(default_factory=dict)
    limitations: Mapping[str, Any] = field(default_factory=dict)
    worker_build: str = "unspecified"


Extractor = Callable[[bytes, Mapping[str, Any]], ExtractionOutcome]


@contextmanager
def owned_workspace(
    *, workspace_root: Path, installation_root: Path, core_version: str
) -> Iterator[ServiceRunner]:
    """Hold the workspace's full write authority for the duration of the block."""
    runner = ServiceRunner(
        ServiceSettings(
            workspace_root=workspace_root,
            installation_root=installation_root,
            core_version=core_version,
            endpoint=None,
        )
    )
    report = runner.start()
    try:
        if not report.ready:
            raise DocumentIntakeRefused(report.reason or "workspace ownership was refused")
        yield runner
    finally:
        runner.stop()


def _now_us() -> int:
    return time.time_ns() // 1000


def _fence(runner: ServiceRunner) -> Any:
    assert runner.connection is not None
    assert runner.identity is not None
    assert runner.workspace_id is not None
    assert runner.generation is not None
    return fenced_transaction(
        runner.connection,
        runner.identity,
        workspace_id=runner.workspace_id,
        fencing_generation=runner.generation,
    )


def _identifier(value: str, what: str) -> None:
    if _IDENTIFIER.fullmatch(value) is None:
        raise DocumentIntakeRefused(f"{what} is outside the accepted identifier domain")


def register_profile(runner: ServiceRunner, profile: dict[str, Any]) -> str:
    """Store an ExtractionProfileV1 and return its digest, which is the SHA-256 of its canonical JSON."""
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    try:
        with _fence(runner):
            return store.record_profile(
                runner.connection,
                workspace_id=runner.workspace_id,
                profile=profile,
                fencing_generation=runner.generation,
                now_us=_now_us(),
            )
    except store.DocumentIntakeInvalid as error:
        raise DocumentIntakeRefused(str(error)) from error


def capture_document(
    runner: ServiceRunner,
    *,
    source_path: Path,
    document_id: str,
    expected_base_capture_id: str | None,
    operation_key: str,
    media_type: str,
) -> DocumentCaptureResult:
    """Capture one local file as a new immutable capture of `document_id`.

    `expected_base_capture_id` is the capture the caller saw as current, or `None` when it saw none: it is required,
    never defaulted, because the head decision depends on it. The source is read once and never again.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    _identifier(document_id, "document id")
    _identifier(operation_key, "operation key")
    try:
        _validate_request(document_id, media_type)
        content = _read_source(source_path)
    except SourceCaptureRefused as error:
        raise DocumentIntakeRefused(str(error)) from error
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    request = store.request_digest(
        {
            "documentId": document_id,
            "expectedBaseCaptureId": expected_base_capture_id,
            "mediaType": media_type,
            "sourceDigest": digest,
            "sourceLengthBytes": len(content),
        }
    )
    workspace_id = runner.workspace_id
    replay = _captured_replay(runner, operation_key, request)
    if replay is not None:
        return replay
    try:
        publish_blob(runner.layout.blobs_path, digest, content)
    except SourceCaptureRefused as error:
        raise DocumentIntakeRefused(str(error)) from error

    with _fence(runner):
        replay = _captured_replay(runner, operation_key, request)
        if replay is not None:
            return replay
        now_us = _now_us()
        head = store.ensure_document(
            runner.connection,
            workspace_id=workspace_id,
            document_id=document_id,
            fencing_generation=runner.generation,
            now_us=now_us,
        )
        if head.source_state != store.SOURCE_ACTIVE:
            raise DocumentIntakeRefused("the document's source is removed")
        if expected_base_capture_id is not None:
            base = store.read_capture(
                runner.connection, workspace_id=workspace_id, capture_id=expected_base_capture_id
            )
            if base is None or base.document_id != document_id:
                raise DocumentIntakeRefused("the expected base is not a capture of this document")
        capture_id = store.capture_id_for(workspace_id, operation_key)
        try:
            written = _write_capture(
                runner,
                source_id=capture_id,
                digest=digest,
                length=len(content),
                media_type=media_type,
                capture=CAPTURE_METADATA_KIND,
            )
        except SourceCaptureRefused as error:
            raise DocumentIntakeRefused(str(error)) from error
        assert written.evidence_id is not None
        eligible = expected_base_capture_id == head.current_capture_id
        capture = store.StoredCapture(
            workspace_id=workspace_id,
            capture_id=capture_id,
            document_id=document_id,
            capture_sequence=store.last_capture_sequence(
                runner.connection, workspace_id=workspace_id, document_id=document_id
            )
            + 1,
            evidence_id=written.evidence_id,
            source_digest=digest,
            source_length_bytes=len(content),
            media_type=media_type,
            expected_base_capture_id=expected_base_capture_id,
            disposition=(
                store.DISPOSITION_HEAD_ELIGIBLE if eligible else store.DISPOSITION_HISTORICAL_ONLY
            ),
            source_removal_generation=head.source_removal_generation,
            operation_key=operation_key,
            request_digest=request,
            fencing_generation=runner.generation,
            created_at_us=now_us,
        )
        store.insert_capture(runner.connection, capture, now_us=now_us)
        if eligible:
            store.advance_capture_head(runner.connection, capture, now_us=now_us)
    return DocumentCaptureResult(CAPTURE_STATUS_CAPTURED, capture, eligible)


def _captured_replay(
    runner: ServiceRunner, operation_key: str, request: str
) -> DocumentCaptureResult | None:
    assert runner.connection is not None and runner.workspace_id is not None
    existing = store.read_capture_by_key(
        runner.connection, workspace_id=runner.workspace_id, operation_key=operation_key
    )
    if existing is None:
        return None
    if existing.request_digest != request:
        raise DocumentIntakeRefused("the operation key already names a different capture request")
    head = store.read_head(
        runner.connection, workspace_id=runner.workspace_id, document_id=existing.document_id
    )
    assert head is not None
    return DocumentCaptureResult(
        CAPTURE_STATUS_REPLAYED, existing, head.current_capture_id == existing.capture_id
    )


def read_retained_source(runner: ServiceRunner, *, capture_id: str) -> bytes:
    """The retained bytes of a capture, re-hashed against the digest and length the capture recorded.

    Reads the workspace blob only. A capture that is not retained, or whose retained copy is missing or does not
    verify, is refused; nothing outside the workspace's blob store is opened.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    capture = store.read_capture(
        runner.connection, workspace_id=runner.workspace_id, capture_id=capture_id
    )
    if capture is None:
        raise DocumentIntakeRefused("no such capture")
    retention = store.read_retention(
        runner.connection, workspace_id=runner.workspace_id, capture_id=capture_id
    )
    assert retention is not None
    if retention.retention_state != store.RETENTION_RETAINED:
        raise DocumentIntakeRefused("the capture is no longer retained")
    try:
        descriptor = _opened_blob(blob_path(runner.layout.blobs_path, capture.source_digest))
    except BlobPublicationRefused as error:
        raise DocumentIntakeRefused("the retained copy is not available") from error
    try:
        chunks: list[bytes] = []
        remaining = min(capture.source_length_bytes, MAX_SOURCE_BYTES) + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    content = b"".join(chunks)
    if (
        len(content) != capture.source_length_bytes
        or f"sha256:{hashlib.sha256(content).hexdigest()}" != capture.source_digest
    ):
        raise DocumentIntakeRefused("the retained copy does not verify")
    return content


def admit_extraction(
    runner: ServiceRunner,
    *,
    capture_id: str,
    profile_digest: str,
    expected_base_extraction_id: str | None,
    operation_key: str,
) -> tuple[store.StoredRevision, bool]:
    """Admit one extraction revision over a retained capture; returns the revision and whether it was a replay.

    The revision is bound to the capture's exact source digest, the profile, the next per-capture sequence and the
    next per-document ordinal, and it freezes the document's source removal generation and the capture's retention
    generation. It is head-eligible only if the expected base is the extraction head at this moment.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    _identifier(operation_key, "operation key")
    workspace_id = runner.workspace_id
    with _fence(runner):
        capture = store.read_capture(
            runner.connection, workspace_id=workspace_id, capture_id=capture_id
        )
        if capture is None:
            raise DocumentIntakeRefused("no such capture")
        request = store.request_digest(
            {
                "captureId": capture_id,
                "expectedBaseExtractionId": expected_base_extraction_id,
                "inputDigest": capture.source_digest,
                "profileDigest": profile_digest,
            }
        )
        existing = store.read_revision_by_key(
            runner.connection, workspace_id=workspace_id, operation_key=operation_key
        )
        if existing is not None:
            if existing.request_digest != request:
                raise DocumentIntakeRefused(
                    "the operation key already names a different extraction request"
                )
            return existing, True
        retention = store.read_retention(
            runner.connection, workspace_id=workspace_id, capture_id=capture_id
        )
        assert retention is not None
        if retention.retention_state != store.RETENTION_RETAINED:
            raise DocumentIntakeRefused("the capture is no longer retained")
        head = store.read_head(
            runner.connection, workspace_id=workspace_id, document_id=capture.document_id
        )
        assert head is not None
        if head.source_state != store.SOURCE_ACTIVE:
            raise DocumentIntakeRefused("the document's source is removed")
        if (
            store.read_profile(runner.connection, workspace_id=workspace_id, digest=profile_digest)
            is None
        ):
            raise DocumentIntakeRefused("the extraction profile is not registered")
        if expected_base_extraction_id is not None:
            base = store.read_revision(
                runner.connection,
                workspace_id=workspace_id,
                extraction_revision_id=expected_base_extraction_id,
            )
            if base is None or base.document_id != capture.document_id:
                raise DocumentIntakeRefused("the expected base is not an extraction of this document")
        sequence, ordinal = store.last_extraction_numbers(
            runner.connection,
            workspace_id=workspace_id,
            document_id=capture.document_id,
            capture_id=capture_id,
        )
        revision = store.StoredRevision(
            workspace_id=workspace_id,
            extraction_revision_id=store.revision_id_for(workspace_id, operation_key),
            document_id=capture.document_id,
            capture_id=capture_id,
            input_digest=capture.source_digest,
            profile_digest=profile_digest,
            extraction_sequence=sequence + 1,
            document_ordinal=ordinal + 1,
            expected_base_extraction_id=expected_base_extraction_id,
            head_eligible=int(expected_base_extraction_id == head.current_extraction_id),
            source_removal_generation=head.source_removal_generation,
            capture_retention_generation=retention.capture_retention_generation,
            operation_key=operation_key,
            request_digest=request,
            fencing_generation=runner.generation,
            created_at_us=_now_us(),
        )
        store.insert_revision(runner.connection, revision)
    return revision, False


def complete_extraction(
    runner: ServiceRunner, *, extraction_revision_id: str, outcome: ExtractionOutcome
) -> store.StoredResult:
    """Record the terminal result of a revision and, only if it is still eligible, make it the extraction head.

    One fenced transaction reads the current generations and heads, inserts the immutable result with the
    disposition they imply, and moves the extraction head only for `promoted`. A failed, unsupported, historical,
    fenced or superseded result never moves it. Completing a revision twice returns the stored result when the
    outcome is identical and is refused otherwise.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    workspace_id = runner.workspace_id
    if outcome.state not in store.RESULT_STATES:
        raise DocumentIntakeRefused("the extraction outcome has an unknown state")
    try:
        coverage = to_canonical_json(dict(outcome.coverage))
        limitations = to_canonical_json(dict(outcome.limitations))
    except (TypeError, ValueError) as error:
        raise DocumentIntakeRefused("coverage and limitations must be canonical JSON") from error
    with _fence(runner):
        revision = store.read_revision(
            runner.connection, workspace_id=workspace_id, extraction_revision_id=extraction_revision_id
        )
        if revision is None:
            raise DocumentIntakeRefused("no such extraction revision")
        now_us = _now_us()
        disposition = _disposition(runner.connection, revision, outcome.state)
        result = store.StoredResult(
            workspace_id=workspace_id,
            extraction_revision_id=extraction_revision_id,
            result_state=outcome.state,
            output_digest=outcome.output_digest,
            coverage_json=coverage,
            limitations_json=limitations,
            worker_build=outcome.worker_build,
            publication_disposition=disposition,
            fencing_generation=runner.generation,
            completed_at_us=now_us,
        )
        existing = store.read_result(
            runner.connection, workspace_id=workspace_id, extraction_revision_id=extraction_revision_id
        )
        if existing is not None:
            same = (
                existing.result_state,
                existing.output_digest,
                existing.coverage_json,
                existing.limitations_json,
                existing.worker_build,
            ) == (outcome.state, outcome.output_digest, coverage, limitations, outcome.worker_build)
            if not same:
                raise DocumentIntakeRefused("the extraction revision already has a different terminal result")
            return existing
        try:
            store.insert_result(runner.connection, result)
        except sqlite3.IntegrityError as error:
            raise DocumentIntakeRefused("the extraction result is outside its closed shape") from error
        if disposition == "promoted":
            store.advance_extraction_head(
                runner.connection, revision, fencing_generation=runner.generation, now_us=now_us
            )
    return result


def _disposition(connection: sqlite3.Connection, revision: store.StoredRevision, state: str) -> str:
    """What publishing this revision's result means right now, from the heads and generations as they stand."""
    if state not in store.PUBLISHABLE_STATES:
        return "not_published"
    if not revision.head_eligible:
        return "historical_only"
    head = store.read_head(
        connection, workspace_id=revision.workspace_id, document_id=revision.document_id
    )
    retention = store.read_retention(
        connection, workspace_id=revision.workspace_id, capture_id=revision.capture_id
    )
    assert head is not None and retention is not None
    if (
        head.source_state != store.SOURCE_ACTIVE
        or head.source_removal_generation != revision.source_removal_generation
    ):
        return "fenced_removal"
    if (
        retention.retention_state != store.RETENTION_RETAINED
        or retention.capture_retention_generation != revision.capture_retention_generation
    ):
        return "fenced_retention"
    if head.extraction_head_ordinal > revision.document_ordinal:
        return "superseded"
    return "promoted"


def reprocess_captured_copy(
    runner: ServiceRunner,
    *,
    capture_id: str,
    profile: dict[str, Any],
    expected_base_extraction_id: str | None,
    operation_key: str,
    extractor: Extractor,
) -> store.StoredResult:
    """Add an extraction revision over a capture's retained copy; never reread the original, never recapture.

    The retained copy is read and verified first, so a deleted, expired or corrupt copy is refused before anything
    is admitted. An extractor that raises is recorded as a failed result with a fixed limitation, not as a refusal.
    A replay of an operation key whose revision already has a result returns that result without running the
    extractor again.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    try:
        digest = register_profile(runner, profile)
        content = read_retained_source(runner, capture_id=capture_id)
        revision, replayed = admit_extraction(
            runner,
            capture_id=capture_id,
            profile_digest=digest,
            expected_base_extraction_id=expected_base_extraction_id,
            operation_key=operation_key,
        )
    except store.DocumentIntakeInvalid as error:
        raise DocumentIntakeRefused(str(error)) from error
    if replayed:
        stored = store.read_result(
            runner.connection,
            workspace_id=runner.workspace_id,
            extraction_revision_id=revision.extraction_revision_id,
        )
        if stored is not None:
            return stored
    try:
        outcome = extractor(content, profile)
    except Exception:  # noqa: BLE001 - an injected extractor failure is a recorded terminal outcome
        outcome = ExtractionOutcome(
            state="failed", limitations={"reason": "extractor_raised"}, worker_build="unavailable"
        )
    return complete_extraction(
        runner, extraction_revision_id=revision.extraction_revision_id, outcome=outcome
    )


def expire_capture(runner: ServiceRunner, *, capture_id: str, deleted: bool = False) -> int:
    """Move a capture's retention to expired (or deleted) and return its new generation.

    This changes eligibility only: it does not remove the blob, and it does not demote a capture head.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    try:
        with _fence(runner):
            return store.set_capture_retention(
                runner.connection,
                workspace_id=runner.workspace_id,
                capture_id=capture_id,
                state="deleted" if deleted else "expired",
                fencing_generation=runner.generation,
                now_us=_now_us(),
            )
    except store.DocumentIntakeInvalid as error:
        raise DocumentIntakeRefused(str(error)) from error


def set_document_source_state(runner: ServiceRunner, *, document_id: str, removed: bool) -> int:
    """Suppress (`removed`) or restore a document's source and return its new source removal generation."""
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    try:
        with _fence(runner):
            return store.set_source_state(
                runner.connection,
                workspace_id=runner.workspace_id,
                document_id=document_id,
                state=store.SOURCE_REMOVED if removed else store.SOURCE_ACTIVE,
                fencing_generation=runner.generation,
                now_us=_now_us(),
            )
    except store.DocumentIntakeInvalid as error:
        raise DocumentIntakeRefused(str(error)) from error
