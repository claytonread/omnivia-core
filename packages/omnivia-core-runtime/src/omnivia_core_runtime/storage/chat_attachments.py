"""Chat staged attachments (C7A, migration 0070): persistence and the evidence read.

Row-oriented like `storage.chat`: each function reads or writes one table's rows and adds
nothing 0070's guards do not already enforce. The writers take the connection of a
fenced transaction the caller already holds (the Chat command seam's), so every write
lands in, and rolls back with, that transaction; 0070's persisted triggers refuse an
unguarded write regardless of who issues it.

`read_attachment_evidence` is the one read this module makes outside the Chat tables. A
staged attachment's reference is an evidence identity (`omnivia_evidence_artifacts`), and
what the service needs to know about it -- its immutable revision, media type, blob
identity and length, whether its staged-source ancestry verified, whether the latest
tombstone observation is a tombstone, whether the latest blob integrity event failed --
is spread over five 0008 tables. It is read here so the service holds no SQL of its own.
It reads only; physical blob presence is the service's, through the blob primitive.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core_runtime.storage.connection import StorageError

__all__ = [
    "AttachmentEvidence",
    "StagedAttachment",
    "attachment_draft_entry",
    "count_active_staged_attachments",
    "draft_attachment_references",
    "insert_staged_attachment",
    "read_attachment_evidence",
    "read_staged_attachment",
    "transition_staged_attachment",
]

#: `omnivia_blob_integrity_events.outcome` values that say the bytes at an address are
#: not the bytes it names. The other outcomes (`verified`, `orphan_candidate`,
#: `staging_protected`, `collection_deferred`) say nothing against the object.
_INTEGRITY_FAILURES: Final = ("missing", "digest_mismatch")

#: `omnivia_chat_drafts.references_json` is one combined array for both reference kinds,
#: and an `AttachmentReference` and a `ContextReference` are the same two-field shape on
#: the wire. Every entry this build stores therefore carries its kind, so a reader splits
#: the array by what the entry says rather than by a guess. An entry without a kind, or
#: with a kind this build does not read, is not decoded: it fails closed.
_ATTACHMENT_KIND: Final = "attachment"
_REFERENCE_KEYS: Final = frozenset({"kind", "referenceId", "sourceRevision"})

_COLUMNS = (
    "workspace_id, conversation_id, actor_id, attachment_reference_id, draft_id, "
    "source_revision, blob_content_digest, state, version, staged_at_us, "
    "updated_at_us, removed_at_us"
)


@dataclass(frozen=True, slots=True)
class StagedAttachment:
    """One `omnivia_chat_staged_attachments` row, decoded one-for-one."""

    workspace_id: str
    conversation_id: str
    actor_id: str
    attachment_reference_id: str
    draft_id: str | None
    source_revision: str
    blob_content_digest: str
    state: str
    version: int
    staged_at_us: int
    updated_at_us: int
    removed_at_us: int | None


@dataclass(frozen=True, slots=True)
class AttachmentEvidence:
    """What the 0008 tables say about one evidence artifact, for staging decisions."""

    content_checksum: str
    media_type: str
    blob_content_digest: str
    content_length_bytes: int
    #: No staged source names the artifact, or the one that does verified and addresses
    #: exactly these bytes at exactly this length.
    ancestry_verified: bool
    tombstoned: bool
    integrity_failed: bool


def read_staged_attachment(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    actor_id: str,
    attachment_reference_id: str,
) -> StagedAttachment | None:
    row = connection.execute(
        f"SELECT {_COLUMNS} FROM omnivia_chat_staged_attachments "
        "WHERE workspace_id = ? AND conversation_id = ? AND actor_id = ? "
        "AND attachment_reference_id = ?",
        (workspace_id, conversation_id, actor_id, attachment_reference_id),
    ).fetchone()
    return None if row is None else StagedAttachment(*row)


def count_active_staged_attachments(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    actor_id: str,
) -> int:
    (count,) = connection.execute(
        "SELECT COUNT(*) FROM omnivia_chat_staged_attachments "
        "WHERE workspace_id = ? AND conversation_id = ? AND actor_id = ? "
        "AND state = 'active'",
        (workspace_id, conversation_id, actor_id),
    ).fetchone()
    return int(count)


def insert_staged_attachment(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    actor_id: str,
    attachment_reference_id: str,
    draft_id: str | None,
    source_revision: str,
    blob_content_digest: str,
    staged_at_us: int,
) -> None:
    """First staging of one reference by one actor in one conversation: active, version 1."""
    connection.execute(
        "INSERT INTO omnivia_chat_staged_attachments "
        f"({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, NULL)",
        (
            workspace_id,
            conversation_id,
            actor_id,
            attachment_reference_id,
            draft_id,
            source_revision,
            blob_content_digest,
            staged_at_us,
            staged_at_us,
        ),
    )


def transition_staged_attachment(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    actor_id: str,
    attachment_reference_id: str,
    expected_version: int,
    state: str,
    draft_id: str | None,
    updated_at_us: int,
) -> bool:
    """Move a row between `active` and `removed`, only from the version expected.

    `False` means the compare-and-set matched nothing and the row is untouched. 0070's
    own triggers separately refuse an illegal transition or a broken invariant.
    """
    cursor = connection.execute(
        "UPDATE omnivia_chat_staged_attachments SET state = ?, draft_id = ?, "
        "version = ?, updated_at_us = ?, removed_at_us = ? "
        "WHERE workspace_id = ? AND conversation_id = ? AND actor_id = ? "
        "AND attachment_reference_id = ? AND version = ?",
        (
            state,
            draft_id,
            expected_version + 1,
            updated_at_us,
            updated_at_us if state == "removed" else None,
            workspace_id,
            conversation_id,
            actor_id,
            attachment_reference_id,
            expected_version,
        ),
    )
    return cursor.rowcount == 1


def read_attachment_evidence(
    connection: sqlite3.Connection, *, workspace_id: str, evidence_id: str
) -> AttachmentEvidence | None:
    """The artifact `evidence_id` names in this workspace, or `None` if it holds none."""
    row = connection.execute(
        "SELECT a.content_checksum, a.media_type, a.blob_content_digest, "
        "b.content_length_bytes, a.staged_source_ref "
        "FROM omnivia_evidence_artifacts a "
        "JOIN omnivia_blob_objects b ON b.workspace_id = a.workspace_id "
        "AND b.content_digest = a.blob_content_digest "
        "WHERE a.workspace_id = ? AND a.evidence_id = ?",
        (workspace_id, evidence_id),
    ).fetchone()
    if row is None:
        return None
    checksum, media_type, digest, length, staged_source_ref = row
    ancestry_verified = staged_source_ref is None or (
        connection.execute(
            "SELECT 1 FROM omnivia_staged_sources "
            "WHERE staged_source_ref = ? AND workspace_id = ? "
            "AND staging_outcome = 'verified' AND blob_workspace_id = ? "
            "AND blob_content_digest = ? AND content_length_bytes = ?",
            (staged_source_ref, workspace_id, workspace_id, digest, length),
        ).fetchone()
        is not None
    )
    observation = connection.execute(
        "SELECT tombstoned_observation FROM omnivia_evidence_provenance_events "
        "WHERE workspace_id = ? AND evidence_id = ? "
        "AND tombstoned_observation IS NOT NULL "
        "ORDER BY provenance_sequence DESC LIMIT 1",
        (workspace_id, evidence_id),
    ).fetchone()
    integrity = connection.execute(
        "SELECT outcome FROM omnivia_blob_integrity_events "
        "WHERE workspace_id = ? AND content_digest = ? "
        "ORDER BY integrity_sequence DESC LIMIT 1",
        (workspace_id, digest),
    ).fetchone()
    return AttachmentEvidence(
        content_checksum=checksum,
        media_type=media_type,
        blob_content_digest=digest,
        content_length_bytes=int(length),
        ancestry_verified=ancestry_verified,
        tombstoned=observation is not None and bool(observation[0]),
        integrity_failed=integrity is not None and integrity[0] in _INTEGRITY_FAILURES,
    )


def attachment_draft_entry(reference: Mapping[str, Any]) -> Mapping[str, Any]:
    """One wire `AttachmentReference`, as the tagged entry a draft stores."""
    return {"kind": _ATTACHMENT_KIND, **reference}


def draft_attachment_references(
    references: Sequence[Any],
) -> tuple[Mapping[str, Any], ...]:
    """The wire `AttachmentReference`s a draft's stored array holds, in stored order.

    Raises :class:`~omnivia_core_runtime.storage.connection.StorageError` for any entry
    that is not exactly a tagged attachment reference, so a context reference (or
    anything else) can never be read back as an attachment.
    """
    decoded: list[Mapping[str, Any]] = []
    for entry in references:
        if (
            not isinstance(entry, Mapping)
            or entry.get("kind") != _ATTACHMENT_KIND
            or not set(entry) <= _REFERENCE_KEYS
            or not isinstance(entry.get("referenceId"), str)
            or not isinstance(entry.get("sourceRevision", ""), str)
        ):
            raise StorageError("a stored chat draft reference is not an attachment reference")
        decoded.append({key: value for key, value in entry.items() if key != "kind"})
    return tuple(decoded)
