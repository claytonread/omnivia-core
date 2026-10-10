"""One place that decides whether a staged attachment may still be sent (C7A).

`SubmitMessage`/`EnqueueMessage` admission (`service/chat_submit.py`) and the generation
executor (`service/chat_generation_executor.py`) must agree on what "this attachment is
still good" means, so both call :func:`revalidate_attachments`. It reads the staged row
(`omnivia_chat_staged_attachments`), the evidence tables behind it and the physical blob,
and nothing else: no path, URL or byte ever leaves it, and it writes nothing.

An accepted user Message keeps each attachment as a standard `attachment` part whose
payload carries the contract's opaque :class:`AttachmentReference` -- with the revision
always stated -- plus the two display facts migration 0070's own header says the service
owns and the database cannot see: `mediaType` and `sizeBytes`. Both are read here anyway,
from the evidence artifact and its blob row, to decide whether the file may be sent at
all; before they were carried they were read and dropped, and a renderer that received
the part had nothing to say about the file but an opaque id. `MessagePart.payload` is an
open, schema-versioned object by contract, so carrying them is a payload-registry fact
and not a schema widening.

There is deliberately NO name. Core stores none: the reference is identity only, and
0070 forbids a path, URL or renderer handle in every column. A consumer can say what a
file is and how big it is, never what it was called.

The executor projects that part to the provider contract's `file` part, carrying the
reference and nothing else.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from omnivia_core_runtime.storage.chat_attachments import (
    AttachmentEvidence,
    read_attachment_evidence,
    read_staged_attachment,
)
from omnivia_core_runtime.workspace.blob_publication import blob_is_intact

__all__ = [
    "ATTACHMENT_PART_TYPE",
    "AttachmentRefusal",
    "AttachmentRefused",
    "PinnedAttachment",
    "attachment_part_payload",
    "attachment_reference_of_part",
    "attachment_unavailable",
    "revalidate_attachments",
]

#: The Message part type an accepted attachment is kept as. Open and dot-namespaced by the
#: contract's own part-type vocabulary; the per-type payload registry is outside this repo.
ATTACHMENT_PART_TYPE: Final = "attachment"

#: Why a reference was refused. Deliberately coarse: one kind covers a missing, removed,
#: foreign-actor, foreign-conversation and foreign-workspace reference, so a refusal never
#: tells a caller anything about a file it did not stage.
AttachmentRefusal = Literal["duplicate", "not_staged", "unavailable", "revision"]


class AttachmentRefused(Exception):
    """An attachment that may not be sent, with the coarse reason and no value."""

    def __init__(self, reason: AttachmentRefusal) -> None:
        super().__init__(reason)
        self.reason: AttachmentRefusal = reason


#: The display facts an `attachment` part payload may carry beside the reference.
#: Optional, because a part committed before they were carried has neither; closed,
#: because a payload with anything else in it is not one this build wrote.
_DISPLAY_FACT_KEYS: Final = frozenset({"mediaType", "sizeBytes"})


@dataclass(frozen=True, slots=True)
class PinnedAttachment:
    """One reference proved sendable, with what the evidence says about the file.

    Deliberately NOT a `Mapping`. The wire `AttachmentReference` is closed
    (`additionalProperties: false`) and a draft's stored entry is validated as
    "exactly a tagged attachment reference", so a dict carrying the two display
    facts would leak them into the draft entry, the queued row and the provider
    `file` part the first time one was spread. :attr:`reference` is the only way
    to the wire shape, and it carries exactly the two contract fields.
    """

    reference_id: str
    source_revision: str
    media_type: str
    size_bytes: int

    @property
    def reference(self) -> Mapping[str, str]:
        """Exactly the contract's `AttachmentReference`, and nothing else."""
        return {"referenceId": self.reference_id, "sourceRevision": self.source_revision}


def attachment_part_payload(pinned: PinnedAttachment) -> Mapping[str, Any]:
    """The stored payload of one `attachment` Message part.

    The opaque reference, plus the media type and byte size the admission check
    already read from the evidence artifact and its blob row. No name: see this
    module's header -- Core stores none.
    """
    return {
        "attachmentReference": dict(pinned.reference),
        "mediaType": pinned.media_type,
        "sizeBytes": pinned.size_bytes,
    }


def attachment_reference_of_part(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
    """The reference an `attachment` part's payload holds, or `None` if it is not exactly one.

    The display facts (`mediaType`, `sizeBytes`) are tolerated and ignored: they are
    optional, so a part committed before they were carried still decodes, and they are
    display only, so nothing downstream of this function may read them. Anything ELSE
    beside the reference is still malformed.
    """
    reference = payload.get("attachmentReference")
    if (
        "attachmentReference" not in payload
        or not (set(payload) - {"attachmentReference"}) <= _DISPLAY_FACT_KEYS
        or not isinstance(reference, Mapping)
        or set(reference) != {"referenceId", "sourceRevision"}
        or not isinstance(reference["referenceId"], str)
        or not isinstance(reference["sourceRevision"], str)
    ):
        return None
    return {
        "referenceId": reference["referenceId"],
        "sourceRevision": reference["sourceRevision"],
    }


def attachment_unavailable(
    evidence: AttachmentEvidence, blobs_root: Path, *, rehash: bool
) -> bool:
    """Whether a file can no longer be offered: a tombstone, a failed ancestry or
    integrity record, or bytes that are not physically there."""
    return (
        evidence.tombstoned
        or not evidence.ancestry_verified
        or evidence.integrity_failed
        or not blob_is_intact(
            blobs_root,
            evidence.blob_content_digest,
            evidence.content_length_bytes,
            rehash=rehash,
        )
    )


def revalidate_attachments(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    conversation_id: str,
    actor_id: str,
    references: Sequence[Mapping[str, Any]],
    blobs_root: Path | None,
    require_staged: bool,
    rehash: bool,
) -> tuple[PinnedAttachment, ...]:
    """Prove every reference is still sendable, or raise :class:`AttachmentRefused`.

    Returns the references in the order given, each with the revision it is pinned to
    and the media type and byte size the evidence states for it.
    All-or-nothing: the first refusal raises and nothing partial is returned. Writes
    nothing, so a refusal inside a fenced transaction leaves nothing to undo.

    `require_staged` is the admission rule: each reference must be an *active* staged
    attachment of this workspace, conversation and `actor_id`, and a stated
    `sourceRevision` must equal the one pinned at staging (an unstated one takes it). Off,
    the reference is a sent Message's own, which must state its revision, and only the
    evidence is re-proved: the composer's later removal of a staged file does not
    retroactively unsend an earlier turn.

    Either way the evidence must still exist at that exact revision and blob, and must be
    neither tombstoned, unverified in its ancestry, integrity-failed nor physically
    absent. `rehash` also re-reads the bytes against their address.
    """
    seen: set[str] = set()
    pinned: list[PinnedAttachment] = []
    for reference in references:
        reference_id = reference["referenceId"]
        if reference_id in seen:
            raise AttachmentRefused("duplicate")
        seen.add(reference_id)
        stated = reference.get("sourceRevision")
        row_digest: str | None = None
        if require_staged:
            row = read_staged_attachment(
                connection,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                actor_id=actor_id,
                attachment_reference_id=reference_id,
            )
            if row is None or row.state != "active":
                raise AttachmentRefused("not_staged")
            revision, row_digest = row.source_revision, row.blob_content_digest
        elif isinstance(stated, str):
            revision = stated
        else:
            raise AttachmentRefused("revision")
        evidence = read_attachment_evidence(
            connection, workspace_id=workspace_id, evidence_id=reference_id
        )
        if (
            evidence is None
            or blobs_root is None
            or attachment_unavailable(evidence, blobs_root, rehash=rehash)
        ):
            raise AttachmentRefused("unavailable")
        if (
            (stated is not None and stated != revision)
            or evidence.content_checksum != revision
            or (row_digest is not None and evidence.blob_content_digest != row_digest)
        ):
            raise AttachmentRefused("revision")
        pinned.append(
            PinnedAttachment(
                reference_id=reference_id,
                source_revision=revision,
                media_type=evidence.media_type,
                size_bytes=evidence.content_length_bytes,
            )
        )
    return tuple(pinned)
