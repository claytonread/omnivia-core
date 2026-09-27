"""Service-owned capture of one local file as immutable workspace evidence.

This is a maintenance path, not an application operation.  It runs only while a
``ServiceRunner`` owns the workspace and therefore uses the same lifetime lock,
exclusive connection, lease, mutation guard and fencing transaction as the live
service.  The source path is input to this one process only: it is never persisted,
returned, logged or accepted over the frozen application catalogue.

Publishing the bytes is not part of that boundary and no longer lives here: it is
`workspace.blob_publication`, which knows only a blob root, a digest and bytes.
`publish_blob` is re-exported for the callers that already reach it through this
module, but there is one implementation of it and this is not it: this module's
`publish_blob` delegates to that implementation and translates its
`BlobPublicationRefused` into `SourceCaptureRefused`, so callers that already catch
`SourceCaptureRefused` through this module keep catching publication failures reached
through this facade.

`read_checkout_file` is a different, smaller thing that lives here because it is
service-owned and reads the same way: one file of an explicitly trusted checkout, by an
Engineering Memory repository path, verified against a digest the caller already holds.
It opens no workspace and writes nothing, and it is only the read step of capturing a
working tree: enumerating a checkout, recording a snapshot and publishing bytes are
other steps.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from omnivia_core.contracts.v1 import is_content_checksum, to_canonical_json
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.storage.engineering_source import valid_path
from omnivia_core_runtime.workspace.blob_publication import BlobPublicationRefused
from omnivia_core_runtime.workspace.blob_publication import (
    publish_blob as _publish_blob,
)

SOURCE_CAPTURE_FORMAT: Final = "omnivia.source-capture-result.v1"
SOURCE_KIND: Final = "document"
MAX_SOURCE_BYTES: Final = 16 * 1024 * 1024

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MEDIA_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z")


class SourceCaptureRefused(RuntimeError):
    """The requested source cannot be captured without weakening the boundary.

    Not a subclass of `BlobPublicationRefused`: a base-class refusal raised by the
    provider-neutral primitive is not an instance of this subclass, so that
    inheritance would not actually let a caller catching this also catch publication
    failures. Instead this module's `publish_blob` facade catches
    `BlobPublicationRefused` itself and raises this in its place.
    """


def publish_blob(blobs_root: Path, digest: str, content: bytes) -> Path:
    """Legacy facade over the one provider-neutral publication implementation.

    Translates `BlobPublicationRefused` into `SourceCaptureRefused` so callers that
    reach publication through this module keep catching `SourceCaptureRefused`.
    """
    try:
        return _publish_blob(blobs_root, digest, content)
    except BlobPublicationRefused as error:
        raise SourceCaptureRefused(str(error)) from error


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
        raise SourceCaptureRefused(
            "source id is outside the accepted identifier domain"
        )
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
    metadata = to_canonical_json({"capture": "local_file", "source_id": source_id})
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
            raise SourceCaptureRefused(
                report.reason or "workspace ownership was refused"
            )
        existing = _existing_capture(
            runner,
            source_id=source_id,
            digest=digest,
            length=len(content),
            media_type=media_type,
        )
        publish_blob(runner.layout.blobs_path, digest, content)
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


#: Whether a checkout can be walked by descriptor at all. Asked of the platform rather
#: than assumed from ``os.name``: without ``dir_fd``, ``O_NOFOLLOW`` and ``O_DIRECTORY``
#: the only walk left is by pathname, which is the race this read exists to refuse, so
#: such a host is refused rather than given a weaker read.
_NO_FOLLOW_WALK: Final = (
    {os.open, os.stat} <= os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)
_DIRECTORY_FLAGS: Final = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
#: ``O_NONBLOCK`` so a FIFO raced into place cannot hold the open. It is refused by kind
#: once open, and changes nothing about reading a regular file.
_FILE_FLAGS: Final = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
)


@dataclass(frozen=True, slots=True)
class CheckoutFile:
    """The bytes of one trusted-checkout file and the digest they were verified against."""

    content: bytes
    digest: str


def _open_component(name: bytes, flags: int, directory: int | None) -> int | None:
    """Open one name relative to a held directory, or `None`; the OS error is dropped.

    Dropped, not chained: an ``OSError`` quotes the name, and for the root that is a
    local absolute path. The caller raises after this returns, outside any handler.
    """
    try:
        return os.open(name, flags, dir_fd=directory)
    except (OSError, ValueError):
        return None


def _identity(name: bytes, directory: int | None) -> tuple[int, int] | None:
    """What `name` is right now, relative to a held directory, without following it."""
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except OSError:
        return None
    return current.st_dev, current.st_ino


def read_checkout_file(
    *, checkout_root: Path, relative_path: str, expected_digest: str
) -> CheckoutFile:
    """Read one file of a trusted checkout, refusing anything that is not that file.

    `checkout_root` is explicit and trusted, but must itself be a real directory: it is
    opened without following a link, like every name below it. `relative_path` is not
    trusted. It must satisfy the Engineering Memory path rules
    (`engineering_source.valid_path`), which keep its exact Unicode and case and refuse
    an absolute path, a `..` segment and every other spelling that could leave the
    checkout; it is used exactly as given, never normalised or case-folded.

    The walk opens each component relative to the descriptor of the one before it, never
    by a composed path, and holds every descriptor until the read is over. A symlink,
    whether it is the file or any parent, is refused rather than followed, and no rename
    can redirect a walk that is already inside a directory. The file must be one regular
    file of at most `MAX_SOURCE_BYTES`, read within that bound. Afterwards every name is
    resolved again and must still be the object that was opened, and the file's size and
    modification time must not have moved; otherwise the source changed or moved while
    it was read and is refused. Last, the bytes must hash to `expected_digest`.

    Every refusal is a `SourceCaptureRefused` with fixed text. No path, local or
    relative, and no operating-system error is quoted or chained. Not every host can do
    this walk; one that cannot is refused, not given a weaker read. Nothing is persisted.
    """
    if not valid_path(relative_path):
        raise SourceCaptureRefused(
            "repository path is outside the accepted portable domain"
        )
    if not is_content_checksum(expected_digest):
        raise SourceCaptureRefused(
            "expected digest is outside the accepted checksum domain"
        )
    if not _NO_FOLLOW_WALK:
        raise SourceCaptureRefused(
            "this host cannot read a checkout without following links"
        )

    *parents, leaf = (part.encode() for part in relative_path.split("/"))
    with ExitStack() as stack:
        # Each held name as (its directory, the name, the object opened): the second
        # look at the end compares against exactly this.
        held: list[tuple[int | None, bytes, os.stat_result]] = []
        directory: int | None = None
        for name in (os.fsencode(checkout_root), *parents):
            descriptor = _open_component(name, _DIRECTORY_FLAGS, directory)
            if descriptor is None:
                raise SourceCaptureRefused(
                    "checkout root is not an accessible directory"
                    if directory is None
                    else "source cannot be opened as a file inside the checkout"
                )
            stack.callback(os.close, descriptor)
            held.append((directory, name, os.fstat(descriptor)))
            directory = descriptor
        descriptor = _open_component(leaf, _FILE_FLAGS, directory)
        if descriptor is None:
            raise SourceCaptureRefused(
                "source cannot be opened as a file inside the checkout"
            )
        stack.callback(os.close, descriptor)
        opened = os.fstat(descriptor)
        held.append((directory, leaf, opened))
        if not stat.S_ISREG(opened.st_mode):
            raise SourceCaptureRefused("source must be one regular file")
        if opened.st_size > MAX_SOURCE_BYTES:
            raise SourceCaptureRefused("source exceeds the capture size limit")

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

        if len(content) > MAX_SOURCE_BYTES:
            raise SourceCaptureRefused("source exceeds the capture size limit")
        if (
            (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns)
            or len(content) != after.st_size
            or any(
                _identity(name, directory) != (status.st_dev, status.st_ino)
                for directory, name, status in held
            )
        ):
            raise SourceCaptureRefused("source changed while it was being read")

    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    if digest != expected_digest:
        raise SourceCaptureRefused("source content does not match the expected digest")
    return CheckoutFile(content=content, digest=digest)


__all__ = [
    "MAX_SOURCE_BYTES",
    "SOURCE_CAPTURE_FORMAT",
    "BlobPublicationRefused",
    "CheckoutFile",
    "SourceCaptureRefused",
    "SourceCaptureResult",
    "capture_local_source",
    "publish_blob",
    "read_checkout_file",
]
