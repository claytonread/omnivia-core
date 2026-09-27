"""Service-owned import of legacy Engineering Memory notes as proposed candidates.

SPEC-CORE-ENGMEM-001 AC-032: a legacy note that carries no source revision keeps
its original evidence availability and unknown applicability, and no commit,
reviewer or acceptance is fabricated for it on the way in.

This is a maintenance path, not an application operation. Like
``--capture-source`` it runs only while a ``ServiceRunner`` owns the workspace,
writes only inside `fenced_transaction`, and is in neither the operation
catalogue nor any model-facing surface. The document path is input to this one
process: it is never persisted, returned or quoted, and no refusal carries a
path, content, identifier or chained exception.

**The document** is one strict JSON object in one regular, stable, bounded
local file (`source_capture._read_source`, the capture path's reader)::

    {"format": "omnivia.engineering-legacy-import.v1",
     "workspace_id": "<this exact workspace>",
     "domain_scope": "engineering.codebase",
     "notes": [{
        "legacy_id": "<Identifier>", "legacy_version": "<Identifier>",
        "record_type": "knowledge.finding" | "knowledge.risk" | "knowledge.decision",
        "title": "<1..200 code points>", "summary": "<1..2000>",   # summary optional
        "note": <the original legacy note: any JSON value>,
        "note_digest": "sha256:<hex of the note's RFC 8785 bytes>",
        "created_at": "<RFC 3339>", "updated_at": "<RFC 3339>",    # optional
        "evidence": {"disposition": "unavailable"} | {"disposition": "redacted"}
                  | {"disposition": "available", "evidence_ids": ["<id>", ...]}}]}

Every object is closed: an unknown or missing member, a wrong type, a duplicate
member name, a repeated legacy identity or a digest that does not match the
note's canonical bytes refuses the whole document before the workspace is opened.

**The mapping is fixed.** Each note becomes one new governed record with one
sealed 0009 version -- `layer='candidate'`, `authority_level='proposed'`, no
disposition, `candidate_origin='migrated_legacy_claim'`, correlated to the
migration run ``mig-<document digest hex>`` and to no audit, reviewer, policy,
model, prompt, continuity session or commit -- whose canonical content is::

    {"kind": "legacy_note", "title": ..., "summary": ...,
     "legacy": {"id": ..., "version": ..., "note": <verbatim>, "note_digest": ...,
                "created_at": ..., "updated_at": ...}}

The note is carried whole and never read for meaning, so nothing inside it --
a repository, a commit, a remote -- becomes top-level `applicability`, which
therefore stays unknown. Title and summary are the operator's explicit mapping,
validated and never truncated. The legacy times are kept verbatim in content and
are not re-asserted as Core time: the version is recorded, and proposed valid,
from the import instant with no end. One
`candidate.legacy_migrated` event and one legacy-lineage row (legacy id, version
and note digest) explain the version. An `available` note must name exact,
untombstoned evidence this workspace holds and links exactly that; `unavailable`
and `redacted` link nothing and carry a fixed reason code.

**Idempotent and resumable.** The whole document and every conflict are checked
before the first write, then each note commits in its own fenced transaction, so
an interruption leaves whole records only and a rerun resumes. A legacy identity
and version already imported with the same mapping is `already_imported` and
writes nothing; the same identity with anything different is refused. Available
evidence identifiers are a set and are stored in canonical lexical order. The
receipt distinguishes the attempted document run from the immutable run and
ordinal already sealed onto each record.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from omnivia_core.contracts.v1 import ContractSemanticError, is_content_checksum
from omnivia_core.contracts.v1.canonical_json import canonicalize, parse_json_document
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.source_capture import (
    _IDENTIFIER,
    SourceCaptureRefused,
    _read_source,
)
from omnivia_core_runtime.storage import engineering_preview
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.memory import (
    _ENGINEERING_CONTENT_CAP_BYTES,
    random_identifier,
    read_snapshot,
)

IMPORT_FORMAT: Final = "omnivia.engineering-legacy-import.v1"
RESULT_FORMAT: Final = "omnivia.engineering-legacy-import-result.v1"
DOMAIN_SCOPE: Final = "engineering.codebase"
RECORD_TYPES: Final = frozenset(
    {"knowledge.finding", "knowledge.risk", "knowledge.decision"}
)
# Even if every returned identifier occupies its 128-character input bound, a full
# result remains below MAX_RESULT_BYTES (proved by the receipt boundary test).
MAX_NOTES: Final = 64
MAX_EVIDENCE_IDS: Final = 32
MAX_RESULT_BYTES: Final = 64 * 1024

#: Who carried each claim across: this service, in the one role it has here. Not the
#: legacy author, whom the legacy system may not have recorded at all.
_ACTOR: Final = ("core-service", "service", "legacy_import")
#: The fixed reason 0009 requires beside evidence that is not available.
_REASON: Final = {
    "unavailable": "evidence.unavailable",
    "redacted": "evidence.redacted",
}
_INSTANT: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})\Z"
)
_NOTE_REQUIRED: Final = frozenset(
    {
        "legacy_id",
        "legacy_version",
        "record_type",
        "title",
        "note",
        "note_digest",
        "evidence",
    }
)
_NOTE_OPTIONAL: Final = frozenset({"summary", "created_at", "updated_at"})
_MALFORMED: Final = "import document does not match the v1 format"


class LegacyImportRefused(RuntimeError):
    """The document cannot be imported honestly. Fixed text, and nothing chained."""


@dataclass(frozen=True, slots=True)
class _Note:
    """One validated note: its identity, the exact content to store, its evidence."""

    ordinal: int
    legacy_id: str
    legacy_version: str
    record_type: str
    note_digest: str
    content_json: str
    content_digest: str
    evidence_disposition: str
    evidence_ids: tuple[str, ...]

    def mapping(self) -> tuple[object, ...]:
        """Everything a replay must match to be the same import."""
        return (
            self.note_digest,
            self.record_type,
            DOMAIN_SCOPE,
            self.content_digest,
            self.evidence_disposition,
            self.evidence_ids,
        )


@dataclass(frozen=True, slots=True)
class LegacyImportRecord:
    """One receipt row, including the immutable lineage that actually owns it."""

    legacy_id: str
    legacy_version: str
    record_id: str
    version: str
    migration_run_id: str
    append_ordinal: int
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "legacy_id": self.legacy_id,
            "legacy_version": self.legacy_version,
            "record_id": self.record_id,
            "version": self.version,
            "migration_run_id": self.migration_run_id,
            "append_ordinal": self.append_ordinal,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class _StoredImport:
    """The authoritative identity already sealed for one imported note."""

    record_id: str
    version: str
    migration_run_id: str
    append_ordinal: int


@dataclass(frozen=True, slots=True)
class LegacyImportResult:
    """The redacted, bounded receipt. The sealed 0009 lineage is the authority."""

    status: str
    workspace_id: str | None = None
    attempted_migration_run_id: str | None = None
    document_digest: str | None = None
    records: tuple[LegacyImportRecord, ...] = ()
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.status != "refused"

    def to_dict(self) -> dict[str, object]:
        return {
            "format": RESULT_FORMAT,
            "status": self.status,
            "workspace_id": self.workspace_id,
            "attempted_migration_run_id": self.attempted_migration_run_id,
            "document_digest": self.document_digest,
            "imported": sum(
                1 for record in self.records if record.status == "imported"
            ),
            "already_imported": sum(
                1 for record in self.records if record.status == "already_imported"
            ),
            "records": [record.to_dict() for record in self.records],
            "reason": self.reason,
        }


def _members(
    value: object, required: frozenset[str], optional: frozenset[str] = frozenset()
) -> dict[str, Any]:
    if type(value) is not dict or not required <= set(value) <= required | optional:
        raise LegacyImportRefused(_MALFORMED)
    return value


def _identifier(value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise LegacyImportRefused(_MALFORMED)
    return value


def _text(value: object, limit: int) -> str:
    if type(value) is not str or not 1 <= len(value) <= limit or "\x00" in value:
        raise LegacyImportRefused(_MALFORMED)
    return value


def _is_instant(value: object) -> bool:
    if type(value) is not str or _INSTANT.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _sha256(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _note(ordinal: int, entry: object) -> _Note:
    fields = _members(entry, _NOTE_REQUIRED, _NOTE_OPTIONAL)
    legacy_id = _identifier(fields["legacy_id"])
    legacy_version = _identifier(fields["legacy_version"])
    record_type = fields["record_type"]
    if type(record_type) is not str or record_type not in RECORD_TYPES:
        raise LegacyImportRefused(_MALFORMED)
    if not is_content_checksum(fields["note_digest"]):
        raise LegacyImportRefused(_MALFORMED)
    note_digest = _sha256(canonicalize(fields["note"]))
    if fields["note_digest"] != note_digest:
        raise LegacyImportRefused("a note digest does not match its canonical content")

    evidence = _members(
        fields["evidence"], frozenset({"disposition"}), frozenset({"evidence_ids"})
    )
    disposition = evidence["disposition"]
    evidence_ids: tuple[str, ...] = ()
    if disposition == "available":
        listed = evidence.get("evidence_ids")
        if type(listed) is not list or not 1 <= len(listed) <= MAX_EVIDENCE_IDS:
            raise LegacyImportRefused(_MALFORMED)
        evidence_ids = tuple(_identifier(item) for item in listed)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise LegacyImportRefused(_MALFORMED)
        evidence_ids = tuple(sorted(evidence_ids))
    elif disposition not in ("unavailable", "redacted") or "evidence_ids" in evidence:
        raise LegacyImportRefused(_MALFORMED)

    legacy: dict[str, Any] = {
        "id": legacy_id,
        "version": legacy_version,
        "note": fields["note"],
        "note_digest": note_digest,
    }
    for key in ("created_at", "updated_at"):
        if key in fields:
            if not _is_instant(fields[key]):
                raise LegacyImportRefused(_MALFORMED)
            legacy[key] = fields[key]
    content: dict[str, Any] = {
        "kind": "legacy_note",
        "title": _text(fields["title"], 200),
        "legacy": legacy,
    }
    if "summary" in fields:
        content["summary"] = _text(fields["summary"], 2000)
    content_json = canonicalize(content)
    if len(content_json.encode("utf-8")) > _ENGINEERING_CONTENT_CAP_BYTES:
        raise LegacyImportRefused("a note exceeds the engineering content bound")
    return _Note(
        ordinal=ordinal,
        legacy_id=legacy_id,
        legacy_version=legacy_version,
        record_type=record_type,
        note_digest=note_digest,
        content_json=content_json,
        content_digest=_sha256(content_json),
        evidence_disposition=str(disposition),
        evidence_ids=evidence_ids,
    )


def _parse(raw: bytes) -> tuple[str, str, tuple[_Note, ...]]:
    """The bound workspace, the document digest and every note, or a fixed refusal."""
    document: object = None
    canonical = ""
    try:
        document = parse_json_document(raw)
        canonical = canonicalize(document)
    except ContractSemanticError:
        canonical = ""
    if not canonical:
        raise LegacyImportRefused("import document is not strict JSON")
    top = _members(
        document, frozenset({"format", "workspace_id", "domain_scope", "notes"})
    )
    if top["format"] != IMPORT_FORMAT:
        raise LegacyImportRefused(_MALFORMED)
    workspace_id = _identifier(top["workspace_id"])
    if top["domain_scope"] != DOMAIN_SCOPE:
        raise LegacyImportRefused(
            "import document is not bound to the engineering domain"
        )
    entries = top["notes"]
    if type(entries) is not list or not 1 <= len(entries) <= MAX_NOTES:
        raise LegacyImportRefused(_MALFORMED)
    notes = tuple(_note(ordinal, entry) for ordinal, entry in enumerate(entries, 1))
    if len({(note.legacy_id, note.legacy_version) for note in notes}) != len(notes):
        raise LegacyImportRefused("a legacy note identity is repeated in the document")
    return workspace_id, _sha256(canonical), notes


def _load(path: Path) -> tuple[str, str, tuple[_Note, ...]]:
    raw = b""
    unreadable = False
    try:
        raw = _read_source(path)
    except SourceCaptureRefused:
        unreadable = True
    if unreadable:
        raise LegacyImportRefused(
            "import document is not one stable regular file within bounds"
        )
    return _parse(raw)


def _plan(
    connection: sqlite3.Connection, workspace_id: str, notes: tuple[_Note, ...]
) -> dict[int, _StoredImport]:
    """The durable identity each imported note maps to; refuse any conflict.

    Read-only and before the first write. Only sealed versions count: a legacy
    identity is imported exactly when a sealed version carries its lineage.
    """
    with read_snapshot(connection):
        rows = connection.execute(
            "SELECT l.legacy_source_id, l.legacy_source_version, l.legacy_source_digest, "
            "a.record_type, a.domain_scope, a.content_digest, a.evidence_disposition, "
            "a.assembly_id, a.governed_record_id, a.governed_record_version_id, "
            "l.migration_run_id, l.append_ordinal "
            "FROM omnivia_governed_legacy_lineage l "
            "JOIN omnivia_authoritative_governed_versions a "
            "ON a.workspace_id = l.workspace_id AND a.assembly_id = l.assembly_id "
            "WHERE l.workspace_id = ?",
            (workspace_id,),
        ).fetchall()
        links: dict[str, list[str]] = {}
        for assembly_id, evidence_id in connection.execute(
            "SELECT k.assembly_id, k.evidence_id "
            "FROM omnivia_governed_version_evidence_links k "
            "JOIN omnivia_governed_legacy_lineage l "
            "ON l.workspace_id = k.workspace_id AND l.assembly_id = k.assembly_id "
            "WHERE k.workspace_id = ? ORDER BY k.assembly_id, k.link_ordinal",
            (workspace_id,),
        ):
            links.setdefault(str(assembly_id), []).append(str(evidence_id))
        # Notes naming evidence that is absent from this workspace or tombstoned.
        unheld = {
            note.ordinal
            for note in notes
            for evidence_id in note.evidence_ids
            if connection.execute(
                "SELECT 1 FROM omnivia_evidence_artifacts a "
                "WHERE a.workspace_id = ? AND a.evidence_id = ? "
                "AND COALESCE((SELECT e.tombstoned_observation "
                "FROM omnivia_evidence_provenance_events e "
                "WHERE e.workspace_id = a.workspace_id AND e.evidence_id = a.evidence_id "
                "AND e.tombstoned_observation IS NOT NULL "
                "ORDER BY e.provenance_sequence DESC LIMIT 1), 0) = 0",
                (workspace_id, evidence_id),
            ).fetchone()
            is None
        }
    existing: dict[tuple[str, str], tuple[tuple[object, ...], _StoredImport]] = {}
    conflict = False
    for row in rows:
        key = (str(row[0]), str(row[1]))
        conflict = conflict or key in existing
        existing[key] = (
            (*map(str, row[2:7]), tuple(sorted(links.get(str(row[7]), ())))),
            _StoredImport(str(row[8]), str(row[9]), str(row[10]), int(row[11])),
        )
    mapped: dict[int, _StoredImport] = {}
    for note in notes:
        prior = existing.get((note.legacy_id, note.legacy_version))
        if prior is None:
            continue
        conflict = conflict or prior[0] != note.mapping()
        mapped[note.ordinal] = prior[1]
    if conflict:
        raise LegacyImportRefused(
            "a legacy note identity is already imported with different content"
        )
    if unheld - set(mapped):
        raise LegacyImportRefused("a note names evidence this workspace does not hold")
    return mapped


def _write(
    connection: sqlite3.Connection, workspace_id: str, run_id: str, note: _Note
) -> _StoredImport:
    """One whole sealed candidate version for `note`; call inside one fence."""
    now_us = time.time_ns() // 1000
    record_id = random_identifier("rec")
    version_id = random_identifier("ver")
    assembly_id = random_identifier("asm")
    event_id = random_identifier("pev")
    actor_id, actor_kind, actor_role = _ACTOR
    reason = _REASON.get(note.evidence_disposition)
    connection.execute(
        "INSERT INTO omnivia_governed_records "
        "(workspace_id, governed_record_id, record_type, domain_scope, recorded_at_us) "
        "VALUES (?, ?, ?, ?, ?)",
        (workspace_id, record_id, note.record_type, DOMAIN_SCOPE, now_us),
    )
    connection.execute(
        "INSERT INTO omnivia_governed_version_assemblies "
        "(workspace_id, assembly_id, governed_record_id, governed_record_version_id, "
        "record_type, domain_scope, layer, authority_level, governance_disposition, "
        "candidate_origin, extraction_kind, decision_source_kind, decision_source_id, "
        "authority_policy_id, authority_policy_version, policy_decision_ref, "
        "content_schema_version, content_json, content_digest, evidence_disposition, "
        "confidence_ppm, assertion_actor_id, assertion_actor_kind, assertion_actor_role, "
        "reason_code, reason_comment, valid_from_us, valid_to_us, recorded_at_us, "
        "append_ordinal, correlation_kind, correlation_id, audit_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, 'candidate', 'proposed', NULL, "
        "'migrated_legacy_claim', NULL, NULL, NULL, NULL, NULL, NULL, '1.0', ?, ?, ?, "
        "NULL, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, 'migration_run', ?, NULL)",
        (
            workspace_id,
            assembly_id,
            record_id,
            version_id,
            note.record_type,
            DOMAIN_SCOPE,
            note.content_json,
            note.content_digest,
            note.evidence_disposition,
            actor_id,
            actor_kind,
            actor_role,
            reason,
            now_us,
            now_us,
            note.ordinal,
            run_id,
        ),
    )
    connection.execute(
        "INSERT INTO omnivia_governed_provenance_events "
        "(workspace_id, provenance_event_id, assembly_id, governed_record_version_id, "
        "provenance_sequence, action, actor_id, actor_kind, actor_role, policy_id, "
        "policy_version, occurred_at_us, recorded_at_us, reason_code, reason_comment, "
        "audit_ref, correlation_kind, correlation_id, predecessor_record_id, "
        "predecessor_version_id, evidence_disposition) "
        "VALUES (?, ?, ?, ?, 1, 'candidate.legacy_migrated', ?, ?, ?, NULL, NULL, ?, ?, "
        "?, NULL, NULL, 'migration_run', ?, NULL, NULL, ?)",
        (
            workspace_id,
            event_id,
            assembly_id,
            version_id,
            actor_id,
            actor_kind,
            actor_role,
            now_us,
            now_us,
            reason,
            run_id,
            note.evidence_disposition,
        ),
    )
    connection.execute(
        "INSERT INTO omnivia_governed_legacy_lineage "
        "(workspace_id, assembly_id, provenance_event_id, correlation_kind, "
        "correlation_id, legacy_source_id, legacy_source_version, legacy_source_digest, "
        "migration_run_id, actor_id, actor_kind, actor_role, evidence_disposition, "
        "occurred_at_us, recorded_at_us, append_ordinal) "
        "VALUES (?, ?, ?, 'migration_run', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            assembly_id,
            event_id,
            run_id,
            note.legacy_id,
            note.legacy_version,
            note.note_digest,
            run_id,
            actor_id,
            actor_kind,
            actor_role,
            note.evidence_disposition,
            now_us,
            now_us,
            note.ordinal,
        ),
    )
    for ordinal, evidence_id in enumerate(note.evidence_ids, 1):
        connection.execute(
            "INSERT INTO omnivia_governed_version_evidence_links "
            "(workspace_id, assembly_id, provenance_event_id, link_ordinal, evidence_id, "
            "normalized_record_id, normalized_span_id, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)",
            (workspace_id, assembly_id, event_id, ordinal, evidence_id, now_us),
        )
    connection.execute(
        "INSERT INTO omnivia_governed_version_seals "
        "(workspace_id, seal_id, assembly_id, governed_record_version_id, "
        "correlation_kind, correlation_id, sealed_at_us) "
        "VALUES (?, ?, ?, ?, 'migration_run', ?, ?)",
        (
            workspace_id,
            random_identifier("seal"),
            assembly_id,
            version_id,
            run_id,
            now_us,
        ),
    )
    # The bounded preview `engineering.search` serves, in the same settlement.
    engineering_preview.record_preview(
        connection, workspace_id=workspace_id, assembly_id=assembly_id
    )
    return _StoredImport(record_id, version_id, run_id, note.ordinal)


def import_legacy_notes(
    *,
    workspace_root: Path,
    installation_root: Path,
    document_path: Path,
    core_version: str,
) -> LegacyImportResult:
    """Import one legacy note document while holding full workspace write authority."""
    workspace_id, document_digest, notes = _load(document_path)
    run_id = f"mig-{document_digest.removeprefix('sha256:')}"
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
            raise LegacyImportRefused("workspace ownership was refused")
        assert runner.connection is not None and runner.identity is not None
        assert runner.generation is not None
        if runner.workspace_id != workspace_id:
            raise LegacyImportRefused("import document is bound to another workspace")
        mapped = _plan(runner.connection, workspace_id, notes)
        records: list[LegacyImportRecord] = []
        failed = False
        for note in notes:
            status = "already_imported"
            identity = mapped.get(note.ordinal)
            if identity is None:
                status = "imported"
                try:
                    with fenced_transaction(
                        runner.connection,
                        runner.identity,
                        workspace_id=workspace_id,
                        fencing_generation=runner.generation,
                    ) as fenced:
                        identity = _write(fenced, workspace_id, run_id, note)
                except (sqlite3.Error, StorageError):
                    failed = True
            if identity is None or failed:
                break
            records.append(
                LegacyImportRecord(
                    legacy_id=note.legacy_id,
                    legacy_version=note.legacy_version,
                    record_id=identity.record_id,
                    version=identity.version,
                    migration_run_id=identity.migration_run_id,
                    append_ordinal=identity.append_ordinal,
                    status=status,
                )
            )
        if failed:
            raise LegacyImportRefused(
                "a note could not be committed; the failed note was rolled back and "
                "retry resumes the document"
            )
        return LegacyImportResult(
            status="imported"
            if any(record.status == "imported" for record in records)
            else "already_imported",
            workspace_id=workspace_id,
            attempted_migration_run_id=run_id,
            document_digest=document_digest,
            records=tuple(records),
            reason="legacy notes are imported as proposed candidates",
        )
    finally:
        runner.stop()


__all__ = [
    "IMPORT_FORMAT",
    "MAX_NOTES",
    "MAX_RESULT_BYTES",
    "RESULT_FORMAT",
    "LegacyImportRecord",
    "LegacyImportRefused",
    "LegacyImportResult",
    "import_legacy_notes",
]
