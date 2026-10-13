"""Bounded local binary staging for document intake.

The local transport hands this module already-transferred bytes.  No caller path
crosses the service boundary.  A stable transfer key makes an acknowledgement-loss
retry resolve to the same immutable descriptor, while a changed digest or media type
under that key is refused.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections.abc import Mapping
from typing import Any, Final

from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service.document_intake import MAX_DOCUMENT_CAPTURE_BYTES
from omnivia_core_runtime.service.runner import ServiceRunner
from omnivia_core_runtime.service.source_capture import (
    SourceCaptureRefused,
    _ensure_blob,
    _validate_request,
    publish_blob,
)
from omnivia_core_runtime.workspace.blob_publication import _opened_blob, blob_path

DOCUMENT_TRANSFER_SOURCE_KIND: Final = "document"


class DocumentTransferRefused(RuntimeError):
    """The binary stage could not be admitted without weakening its boundary."""


def _staged_ref(workspace_id: str, transfer_key: str) -> str:
    digest = hashlib.sha256(f"{workspace_id}|{transfer_key}".encode()).hexdigest()
    return f"stg-document-{digest[:40]}"


def stage_document_bytes(
    runner: ServiceRunner,
    *,
    content: bytes,
    media_type: str,
    transfer_key: str,
) -> Mapping[str, Any]:
    """Publish one exact snapshot and return its server-issued immutable descriptor."""
    if not content or len(content) > MAX_DOCUMENT_CAPTURE_BYTES:
        raise DocumentTransferRefused("the document transfer size is outside the accepted bound")
    try:
        _validate_request(transfer_key, media_type)
    except SourceCaptureRefused as error:
        raise DocumentTransferRefused("the document transfer header is invalid") from error

    connection = runner.connection
    identity = runner.identity
    workspace_id = runner.workspace_id
    generation = runner.generation
    if connection is None or identity is None or workspace_id is None or generation is None:
        raise DocumentTransferRefused("the workspace is not available for document transfer")

    checksum = "sha256:" + hashlib.sha256(content).hexdigest()
    length = len(content)
    staged_source_ref = _staged_ref(workspace_id, transfer_key)
    descriptor: dict[str, Any] = {
        "staged_source_ref": staged_source_ref,
        "source_kind": DOCUMENT_TRANSFER_SOURCE_KIND,
        "content_checksum": checksum,
        "content_length_bytes": length,
        "media_type": media_type,
    }
    expected = (
        DOCUMENT_TRANSFER_SOURCE_KIND,
        checksum,
        length,
        media_type,
        checksum,
        "verified",
        workspace_id,
        checksum,
    )
    existing = connection.execute(
        "SELECT source_kind, declared_checksum, content_length_bytes, media_type, "
        "computed_checksum, staging_outcome, blob_workspace_id, blob_content_digest "
        "FROM omnivia_staged_sources WHERE staged_source_ref = ? AND workspace_id = ?",
        (staged_source_ref, workspace_id),
    ).fetchone()
    if existing is not None and tuple(existing) != expected:
        raise DocumentTransferRefused("the document transfer key already names different content")
    publish_blob(runner.layout.blobs_path, checksum, content)

    now_us = time.time_ns() // 1000
    metadata = to_canonical_json({"capture": "document_transfer_v1"})
    metadata_digest = "sha256:" + hashlib.sha256(metadata.encode("utf-8")).hexdigest()
    try:
        with fenced_transaction(
            connection,
            identity,
            workspace_id=workspace_id,
            fencing_generation=generation,
        ):
            existing = connection.execute(
                "SELECT source_kind, declared_checksum, content_length_bytes, media_type, "
                "computed_checksum, staging_outcome, blob_workspace_id, blob_content_digest "
                "FROM omnivia_staged_sources WHERE staged_source_ref = ? AND workspace_id = ?",
                (staged_source_ref, workspace_id),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != expected:
                    raise DocumentTransferRefused(
                        "the document transfer key already names different content"
                    )
                return descriptor

            _ensure_blob(runner, checksum, length, now_us)
            connection.execute(
                "INSERT INTO omnivia_staged_sources "
                "(staged_source_ref, workspace_id, source_kind, declared_checksum, "
                "content_length_bytes, media_type, source_version, computed_checksum, "
                "original_metadata_json, original_metadata_digest, staging_outcome, "
                "blob_workspace_id, blob_content_digest, recorded_at_us) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'verified', ?, ?, ?)",
                (
                    staged_source_ref,
                    workspace_id,
                    DOCUMENT_TRANSFER_SOURCE_KIND,
                    checksum,
                    length,
                    media_type,
                    checksum,
                    metadata,
                    metadata_digest,
                    workspace_id,
                    checksum,
                    now_us,
                ),
            )
    except DocumentTransferRefused:
        raise
    except (sqlite3.Error, OSError, SourceCaptureRefused) as error:
        raise DocumentTransferRefused("the document transfer could not be preserved") from error
    return descriptor


def read_staged_document(
    service: Any,
    *,
    workspace_id: str,
    staged_source_ref: str,
    source_kind: str,
    content_checksum: str,
    content_length_bytes: int,
    media_type: str,
) -> bytes:
    """Resolve one verified staged descriptor to its exact retained bytes."""
    if source_kind != DOCUMENT_TRANSFER_SOURCE_KIND:
        raise DocumentTransferRefused("the staged source is not a document")
    connection = getattr(service, "connection", None)
    layout = getattr(service, "layout", None)
    blobs_root = getattr(layout, "blobs_path", None)
    if connection is None or not workspace_id or blobs_root is None:
        raise DocumentTransferRefused("the workspace is not available for document transfer")
    row = connection.execute(
        "SELECT source_kind, declared_checksum, content_length_bytes, media_type, "
        "computed_checksum, staging_outcome, blob_workspace_id, blob_content_digest "
        "FROM omnivia_staged_sources WHERE staged_source_ref = ? AND workspace_id = ?",
        (staged_source_ref, workspace_id),
    ).fetchone()
    expected = (
        source_kind,
        content_checksum,
        content_length_bytes,
        media_type,
        content_checksum,
        "verified",
        workspace_id,
        content_checksum,
    )
    if row is None or tuple(row) != expected or not 1 <= content_length_bytes <= MAX_DOCUMENT_CAPTURE_BYTES:
        raise DocumentTransferRefused("the staged document descriptor does not verify")
    descriptor = _opened_blob(blob_path(blobs_root, content_checksum))
    try:
        chunks: list[bytes] = []
        remaining = content_length_bytes + 1
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
        len(content) != content_length_bytes
        or "sha256:" + hashlib.sha256(content).hexdigest() != content_checksum
    ):
        raise DocumentTransferRefused("the staged document bytes do not verify")
    return content


__all__ = [
    "DOCUMENT_TRANSFER_SOURCE_KIND",
    "DocumentTransferRefused",
    "read_staged_document",
    "stage_document_bytes",
]
