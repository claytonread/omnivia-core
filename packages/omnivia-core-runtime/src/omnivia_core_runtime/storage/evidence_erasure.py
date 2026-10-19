"""Physical erasure of one directly captured evidence artifact (LEX-01).

Migration 0075 is the authority for a delete: a receipt in `omnivia_evidence_erasures`
that names the evidence is the one durable fact the 0008 DELETE guards accept. This module
is the one writer of that receipt and of the deletes it licenses, and it runs on the fenced
connection the mutation coordinator hands the domain callback, so the receipt, the deletes,
the audit event and the idempotency claim commit together or not at all.

**What is erased, and in which order.** The artifact's normalized spans and records, its
event references and provenance events, its permission labels, the artifact itself, the
staged source its capture wrote beside it, and -- when no other record holds the address --
the blob object row and the integrity events observed about it. Foreign keys are immediate, so the
order is children first; a receipt goes in before any of them because every guard reads it.
The projection document is not deleted here: it belongs to a run, a run's documents are
reclaimed when a newer run activates, and `fts._plan_run` starts that newer run when it finds
an active document whose artifact is gone.

**What is refused, and why a refusal is not a partial erase.** Evidence another record
depends on is left exactly as it was. Governed records cite evidence through
`omnivia_governed_version_evidence_links`, and a vector projection run holds occurrences; an
erase that left either pointing at nothing, or that deleted a citation, would rewrite
knowledge this operation has no authority over. Evidence that did not come from
`evidence.capture` is refused for the same reason: its provenance, staging and downstream
coverage belong to the import, document and engineering lanes, none of which this operation
can unwind. `DEPENDENTS` and `OWNED_CHILDREN` together name every table that holds a foreign
key to an artifact; a test holds that list to the schema, so a later table that references
evidence cannot be added without deciding which of the two it is.

**The bytes.** The blob file is the caller's to remove after the transaction commits, because
a file unlinked inside a transaction that then rolled back cannot be put back. What this
module decides, from durable state, is whether the address is still held by something else.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Final

from omnivia_core.contracts.v1 import EVIDENCE_CAPTURE_SOURCE_KIND
from omnivia_core_runtime.storage.connection import StorageError

#: The only kind of evidence this operation erases: what `evidence.capture` itself stored.
ERASABLE_SOURCE_KIND: Final = EVIDENCE_CAPTURE_SOURCE_KIND

#: Tables whose rows belong to one artifact and go with it, children before parents.
OWNED_CHILDREN: Final[tuple[str, ...]] = (
    "omnivia_normalized_source_spans",
    "omnivia_normalized_source_records",
    "omnivia_evidence_event_references",
    "omnivia_evidence_provenance_events",
    "omnivia_evidence_permission_labels",
)

#: `(table, column)` pairs that make an artifact undeletable: another record names it.
#: The first four hold a foreign key to the artifact, so deleting it would fail anyway; the
#: last is a derived projection this operation does not own and so does not clear.
DEPENDENTS: Final[tuple[tuple[str, str], ...]] = (
    ("omnivia_governed_version_evidence_links", "evidence_id"),
    ("omnivia_document_captures", "evidence_id"),
    ("omnivia_engineering_snapshot_captures", "manifest_evidence_id"),
    ("omnivia_code_analysis_imports", "manifest_evidence_id"),
    ("omnivia_semantic_vector_occurrences", "evidence_id"),
)

CONTENT_ERASED: Final = "erased"
CONTENT_RETAINED_SHARED: Final = "retained_shared"


class EvidenceNotFound(StorageError):
    """No such evidence in this workspace, and no receipt that there ever was."""


class EvidenceNotErasable(StorageError):
    """The evidence exists and this operation will not erase it."""


@dataclass(frozen=True, slots=True)
class ErasureReceipt:
    evidence_id: str
    blob_content_digest: str | None
    #: Whether an earlier request had already erased this evidence, so this one wrote nothing.
    already_erased: bool = False

    @property
    def content_disposition(self) -> str:
        return CONTENT_ERASED if self.blob_content_digest else CONTENT_RETAINED_SHARED


def read_receipt(
    connection: sqlite3.Connection, *, workspace_id: str, evidence_id: str
) -> ErasureReceipt | None:
    """The newest receipt naming this evidence id in this workspace, if any."""
    row = connection.execute(
        "SELECT blob_content_digest FROM omnivia_evidence_erasures "
        "WHERE workspace_id = ? AND evidence_id = ? "
        "ORDER BY erased_at_us DESC, audit_ref DESC LIMIT 1",
        (workspace_id, evidence_id),
    ).fetchone()
    if row is None:
        return None
    return ErasureReceipt(evidence_id, None if row[0] is None else str(row[0]))


def address_in_use(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    digest: str,
    excluding_evidence: str = "",
    excluding_staged: str = "",
) -> bool:
    """Whether any record still holds the blob address `digest`.

    An artifact, a staged source or a document capture does, and so does nothing else this
    schema ties to a blob by foreign key. The two `excluding_*` arguments name the artifact
    and the staged source an erasure is itself removing; an empty one matches no id, so it
    excludes nothing.
    """
    queries = (
        (
            (
                "SELECT 1 FROM omnivia_evidence_artifacts "
                "WHERE workspace_id = ? AND blob_content_digest = ? AND evidence_id <> ?"
            ),
            (workspace_id, digest, excluding_evidence),
        ),
        (
            (
                "SELECT 1 FROM omnivia_staged_sources "
                "WHERE blob_workspace_id = ? AND blob_content_digest = ? "
                "AND staged_source_ref <> ?"
            ),
            (workspace_id, digest, excluding_staged),
        ),
        (
            (
                "SELECT 1 FROM omnivia_document_captures "
                "WHERE workspace_id = ? AND source_digest = ?"
            ),
            (workspace_id, digest),
        ),
    )
    return any(
        connection.execute(f"{query} LIMIT 1", parameters).fetchone() is not None
        for query, parameters in queries
    )


def erase_evidence(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    evidence_id: str,
    audit_ref: str,
    erased_at_us: int,
) -> ErasureReceipt:
    """Write the receipt and delete the artifact with everything it owns, or refuse.

    Runs inside the caller's fenced transaction and raises before writing anything it would
    have to undo. A repeat for evidence an earlier request erased returns that request's
    receipt and writes nothing.
    """
    artifact = connection.execute(
        "SELECT source_kind, blob_content_digest, staged_source_ref "
        "FROM omnivia_evidence_artifacts WHERE workspace_id = ? AND evidence_id = ?",
        (workspace_id, evidence_id),
    ).fetchone()
    if artifact is None:
        receipt = read_receipt(
            connection, workspace_id=workspace_id, evidence_id=evidence_id
        )
        if receipt is None:
            raise EvidenceNotFound("no such evidence in this workspace")
        return replace(receipt, already_erased=True)
    if str(artifact[0]) != ERASABLE_SOURCE_KIND:
        raise EvidenceNotErasable("evidence from this source is not erased here")
    for table, column in DEPENDENTS:
        if (
            connection.execute(
                f"SELECT 1 FROM {table} WHERE workspace_id = ? AND {column} = ? LIMIT 1",
                (workspace_id, evidence_id),
            ).fetchone()
            is not None
        ):
            raise EvidenceNotErasable("other records depend on this evidence")

    digest = str(artifact[1])
    staged = None if artifact[2] is None else str(artifact[2])
    # The staged source a direct submission wrote beside its artifact goes with it, unless
    # something else names it: another artifact, or an import claim.
    own_staged = (
        staged
        if staged is not None
        and connection.execute(
            "SELECT 1 FROM omnivia_evidence_artifacts "
            "WHERE workspace_id = ? AND staged_source_ref = ? AND evidence_id <> ? "
            "UNION ALL SELECT 1 FROM omnivia_application_import_claims "
            "WHERE staged_source_ref = ? LIMIT 1",
            (workspace_id, staged, evidence_id, staged),
        ).fetchone()
        is None
        else None
    )
    removed = (
        None
        if address_in_use(
            connection,
            workspace_id=workspace_id,
            digest=digest,
            excluding_evidence=evidence_id,
            excluding_staged=own_staged or "",
        )
        else digest
    )

    connection.execute(
        "INSERT INTO omnivia_evidence_erasures "
        "(audit_ref, workspace_id, evidence_id, staged_source_ref, blob_content_digest, "
        "erased_at_us) VALUES (?, ?, ?, ?, ?, ?)",
        (audit_ref, workspace_id, evidence_id, own_staged, removed, erased_at_us),
    )
    for table in OWNED_CHILDREN:
        connection.execute(
            f"DELETE FROM {table} WHERE workspace_id = ? AND evidence_id = ?",
            (workspace_id, evidence_id),
        )
    connection.execute(
        "DELETE FROM omnivia_evidence_artifacts WHERE workspace_id = ? AND evidence_id = ?",
        (workspace_id, evidence_id),
    )
    if own_staged is not None:
        connection.execute(
            "DELETE FROM omnivia_staged_sources "
            "WHERE workspace_id = ? AND staged_source_ref = ?",
            (workspace_id, own_staged),
        )
    if removed is not None:
        connection.execute(
            "DELETE FROM omnivia_blob_objects WHERE workspace_id = ? AND content_digest = ?",
            (workspace_id, removed),
        )
        connection.execute(
            "DELETE FROM omnivia_blob_integrity_events "
            "WHERE workspace_id = ? AND content_digest = ?",
            (workspace_id, removed),
        )
    return ErasureReceipt(evidence_id, removed)


@contextmanager
def secure_delete(connection: sqlite3.Connection) -> Iterator[None]:
    """Overwrite what a DELETE frees with zeros, for the statements run inside it.

    SQLite leaves a deleted row's bytes in its page until something reuses the page, and in
    WAL mode the pre-delete page also survives in the log. This is the first half of making
    "erased" true of the file and not only of the schema; :func:`truncate_wal` is the other.
    The previous setting is restored, so the pragma is a scope and not a new default.
    """
    row = connection.execute("PRAGMA secure_delete").fetchone()
    previous = int(row[0]) if row is not None else 0
    connection.execute("PRAGMA secure_delete = ON")
    try:
        yield
    finally:
        connection.execute(f"PRAGMA secure_delete = {previous}")


def truncate_wal(connection: sqlite3.Connection) -> bool:
    """Checkpoint the write-ahead log into the database file and truncate it.

    `True` when the log was fully checkpointed, so no earlier image of a page survives in it.
    """
    row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return row is not None and int(row[0]) == 0


__all__ = [
    "CONTENT_ERASED",
    "CONTENT_RETAINED_SHARED",
    "DEPENDENTS",
    "ERASABLE_SOURCE_KIND",
    "OWNED_CHILDREN",
    "ErasureReceipt",
    "EvidenceNotErasable",
    "EvidenceNotFound",
    "address_in_use",
    "erase_evidence",
    "read_receipt",
    "secure_delete",
    "truncate_wal",
]
