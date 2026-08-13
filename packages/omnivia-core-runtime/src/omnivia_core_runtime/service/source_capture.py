"""Service-owned capture of one local file as immutable workspace evidence.

This is a maintenance path, not an application operation.  It runs only while a
``ServiceRunner`` owns the workspace and therefore uses the same lifetime lock,
exclusive connection, lease, mutation guard and fencing transaction as the live
service.  The source path is input to this one process only: it is never persisted,
returned, logged or accepted over the frozen application catalogue.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.workspace.filesystem import fsync_directory

SOURCE_CAPTURE_FORMAT: Final = "omnivia.source-capture-result.v1"
SOURCE_KIND: Final = "document"
MAX_SOURCE_BYTES: Final = 16 * 1024 * 1024

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MEDIA_TYPE = re.compile(
    r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z"
)


class SourceCaptureRefused(RuntimeError):
    """The requested source cannot be captured without weakening the boundary."""


@dataclass(frozen=True, slots=True)
class SourceCaptureResult:
    """The redacted, versioned result of one source-capture attempt."""

    status: str
    workspace_id: str | None
    source_id: str
    evidence_id: str | None = None
    content_digest: str | None = None
    content_length_bytes: int | None = None
    media_type: str | None = None
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.status in {"captured", "already_captured"}

    def to_dict(self) -> dict[str, object]:
        return {
            "format": SOURCE_CAPTURE_FORMAT,
            "status": self.status,
            "workspace_id": self.workspace_id,
            "source": {"kind": SOURCE_KIND, "source_id": self.source_id},
            "evidence_id": self.evidence_id,
            "content_digest": self.content_digest,
            "content_length_bytes": self.content_length_bytes,
            "media_type": self.media_type,
            "reason": self.reason,
        }


def _validate_request(source_id: str, media_type: str) -> None:
    if _IDENTIFIER.fullmatch(source_id) is None:
        raise SourceCaptureRefused("source id is outside the accepted identifier domain")
    if len(media_type) > 255 or _MEDIA_TYPE.fullmatch(media_type) is None:
        raise SourceCaptureRefused("media type is outside the accepted domain")


def _read_source(path: Path) -> bytes:
    """Read one stable, regular, bounded file without following a symlink."""
    try:
        before = path.lstat()
    except OSError as error:
        raise SourceCaptureRefused("source file is not available") from error
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise SourceCaptureRefused("source must be one regular file")
    if before.st_size > MAX_SOURCE_BYTES:
        raise SourceCaptureRefused("source exceeds the capture size limit")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SourceCaptureRefused("source file cannot be opened safely") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise SourceCaptureRefused("source must be one regular file")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise SourceCaptureRefused("source changed while capture began")
        chunks: list[bytes] = []
        remaining = MAX_SOURCE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    if len(content) > MAX_SOURCE_BYTES:
        raise SourceCaptureRefused("source exceeds the capture size limit")
    if (
        (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or len(content) != after.st_size
    ):
        raise SourceCaptureRefused("source changed while it was being captured")
    return content


def _blob_path(runner: ServiceRunner, digest: str) -> Path:
    return runner.layout.blobs_path / "sha256" / digest.removeprefix("sha256:")


def _verify_published_blob(path: Path, content: bytes) -> None:
    existing = _read_source(path)
    if existing != content:
        raise SourceCaptureRefused("the content-addressed blob does not verify")


def _publish_blob(runner: ServiceRunner, digest: str, content: bytes) -> Path:
    """Publish bytes before the database may refer to them."""
    target = _blob_path(runner, digest)
    directory = target.parent
    if not directory.exists():
        directory.mkdir(mode=0o700, parents=False)
        fsync_directory(directory.parent)
    if target.exists() or target.is_symlink():
        _verify_published_blob(target, content)
        return target

    temporary = directory / f".{target.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # pragma: no cover - defensive operating-system failure
                raise OSError("short write while publishing blob")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, target)
        fsync_directory(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    _verify_published_blob(target, content)
    return target


def _existing_capture(
    runner: ServiceRunner,
    *,
    source_id: str,
    digest: str,
    length: int,
    media_type: str,
) -> SourceCaptureResult | None:
    assert runner.connection is not None and runner.workspace_id is not None
    rows = runner.connection.execute(
        "SELECT evidence_id, blob_content_digest, media_type "
        "FROM omnivia_evidence_artifacts "
        "WHERE workspace_id = ? AND source_kind = ? AND source_native_id = ? "
        "AND source_locator IS NULL AND source_retrieved_at_us IS NULL "
        "ORDER BY evidence_id ASC",
        (runner.workspace_id, SOURCE_KIND, source_id),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise SourceCaptureRefused("source identity is not unique in this workspace")
    evidence_id, stored_digest, stored_media_type = map(str, rows[0])
    if stored_digest != digest or stored_media_type != media_type:
        raise SourceCaptureRefused("source identity already names different evidence")
    row = runner.connection.execute(
        "SELECT content_length_bytes FROM omnivia_blob_objects "
        "WHERE workspace_id = ? AND content_digest = ?",
        (runner.workspace_id, digest),
    ).fetchone()
    if row is None or int(row[0]) != length:
        raise SourceCaptureRefused("the existing source has no matching blob identity")
    return SourceCaptureResult(
        status="already_captured",
        workspace_id=runner.workspace_id,
        source_id=source_id,
        evidence_id=evidence_id,
        content_digest=digest,
        content_length_bytes=length,
        media_type=media_type,
        reason="the exact source identity and content are already captured",
    )


def _append_capture(
    runner: ServiceRunner,
    *,
    source_id: str,
    digest: str,
    length: int,
    media_type: str,
) -> SourceCaptureResult:
    assert runner.connection is not None
    assert runner.identity is not None
    assert runner.workspace_id is not None
    assert runner.generation is not None

    now_us = time.time_ns() // 1000
    nonce = uuid.uuid4().hex
    staged_source_ref = f"stg-local-{nonce}"
    evidence_id = f"evd-local-{nonce}"
    integrity_event_id = f"bie-local-{nonce}"
    provenance_event_id = f"prv-local-{nonce}"
    metadata = to_canonical_json(
        {"capture": "local_file", "source_id": source_id}
    )
    if len(metadata) > 8192:
        raise SourceCaptureRefused("source metadata exceeds the accepted bound")
    metadata_digest = f"sha256:{hashlib.sha256(metadata.encode()).hexdigest()}"

    with fenced_transaction(
        runner.connection,
        runner.identity,
        workspace_id=runner.workspace_id,
        fencing_generation=runner.generation,
    ):
        blob = runner.connection.execute(
            "SELECT content_length_bytes FROM omnivia_blob_objects "
            "WHERE workspace_id = ? AND content_digest = ?",
            (runner.workspace_id, digest),
        ).fetchone()
        if blob is None:
            runner.connection.execute(
                "INSERT INTO omnivia_blob_objects "
                "(workspace_id, content_digest, content_length_bytes, created_at_us, "
                "verified_at_us) VALUES (?, ?, ?, ?, ?)",
                (runner.workspace_id, digest, length, now_us, now_us),
            )
        elif int(blob[0]) != length:
            raise SourceCaptureRefused("the blob identity has a conflicting length")

        integrity_sequence = int(
            runner.connection.execute(
                "SELECT COALESCE(MAX(integrity_sequence), 0) + 1 "
                "FROM omnivia_blob_integrity_events "
                "WHERE workspace_id = ? AND content_digest = ?",
                (runner.workspace_id, digest),
            ).fetchone()[0]
        )
        runner.connection.execute(
            "INSERT INTO omnivia_blob_integrity_events "
            "(integrity_event_id, workspace_id, content_digest, integrity_sequence, "
            "outcome, observed_digest, observed_length_bytes, expected_length_bytes, "
            "inventory_id, checked_at_us) VALUES (?, ?, ?, ?, 'verified', ?, ?, ?, "
            "NULL, ?)",
            (
                integrity_event_id,
                runner.workspace_id,
                digest,
                integrity_sequence,
                digest,
                length,
                length,
                now_us,
            ),
        )
        runner.connection.execute(
            "INSERT INTO omnivia_staged_sources "
            "(staged_source_ref, workspace_id, source_kind, declared_checksum, "
            "content_length_bytes, media_type, source_version, computed_checksum, "
            "original_metadata_json, original_metadata_digest, staging_outcome, "
            "blob_workspace_id, blob_content_digest, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'verified', ?, ?, ?)",
            (
                staged_source_ref,
                runner.workspace_id,
                SOURCE_KIND,
                digest,
                length,
                media_type,
                digest,
                metadata,
                metadata_digest,
                runner.workspace_id,
                digest,
                now_us,
            ),
        )
        runner.connection.execute(
            "INSERT INTO omnivia_evidence_artifacts "
            "(evidence_id, workspace_id, source_kind, source_native_id, "
            "source_locator, source_retrieved_at_us, event_at_us, observed_at_us, "
            "ingested_at_us, recorded_at_us, content_checksum, blob_content_digest, "
            "media_type, original_metadata_json, original_metadata_digest, "
            "sensitivity, parser_status, ingestion_status, staged_source_ref, "
            "import_run_id) VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, "
            "?, ?, ?, 'private', 'not_parsed', 'ingested', ?, NULL)",
            (
                evidence_id,
                runner.workspace_id,
                SOURCE_KIND,
                source_id,
                now_us,
                now_us,
                digest,
                digest,
                media_type,
                metadata,
                metadata_digest,
                staged_source_ref,
            ),
        )
        runner.connection.execute(
            "INSERT INTO omnivia_evidence_provenance_events "
            "(provenance_event_id, evidence_id, workspace_id, provenance_sequence, "
            "actor_id, actor_kind, action, occurred_at_us, reason_code, reason_comment, "
            "parser_status, ingestion_status, tombstoned_observation, source_kind, "
            "source_native_id, audit_ref) VALUES (?, ?, ?, 1, 'core-service', "
            "'service', 'captured', ?, NULL, NULL, 'not_parsed', 'ingested', 0, ?, ?, "
            "NULL)",
            (
                provenance_event_id,
                evidence_id,
                runner.workspace_id,
                now_us,
                SOURCE_KIND,
                source_id,
            ),
        )

    return SourceCaptureResult(
        status="captured",
        workspace_id=runner.workspace_id,
        source_id=source_id,
        evidence_id=evidence_id,
        content_digest=digest,
        content_length_bytes=length,
        media_type=media_type,
        reason="source captured as immutable workspace evidence",
    )


def capture_local_source(
    *,
    workspace_root: Path,
    installation_root: Path,
    source_path: Path,
    source_id: str,
    media_type: str,
    core_version: str,
) -> SourceCaptureResult:
    """Capture one local source while holding the workspace's full write authority."""
    _validate_request(source_id, media_type)
    content = _read_source(source_path)
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
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
            raise SourceCaptureRefused(report.reason or "workspace ownership was refused")
        existing = _existing_capture(
            runner,
            source_id=source_id,
            digest=digest,
            length=len(content),
            media_type=media_type,
        )
        _publish_blob(runner, digest, content)
        if existing is not None:
            return existing
        return _append_capture(
            runner,
            source_id=source_id,
            digest=digest,
            length=len(content),
            media_type=media_type,
        )
    finally:
        runner.stop()


__all__ = [
    "MAX_SOURCE_BYTES",
    "SOURCE_CAPTURE_FORMAT",
    "SourceCaptureRefused",
    "SourceCaptureResult",
    "capture_local_source",
]
