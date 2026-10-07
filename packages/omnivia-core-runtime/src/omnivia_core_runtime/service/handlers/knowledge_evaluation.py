"""The `knowledge.evaluation.produce` operation (C16b): one mutation over the Stage 2 producer.

The caller submits the exact Stage 2 content it holds and the identifiers and source the evidence ledger
needs. `produce_evaluation_report` derives the report, its status, coverage, findings and integrity digest
from that content, and every canonical record it returns is registered as one evidence item in the
workspace's existing evidence ledger. That write runs inside the same `execute_mutation` transaction as the
audit, claim and outcome, so a refusal anywhere leaves nothing behind.

The caller states no verdict and names no actor or workspace. The principal is the authenticated caller and
the workspace is the one the request selects. The attempts are the principal's submission: the result says
who submitted them, and Core does not claim it observed any external model output.

Evidence identity is the profile's own ID. Each record is registered under its `record_id`, so the ledger holds
the Stage 2 profile by the ID Dev reads it by, and its content and integrity digests are the record's checksum.
The same canonical record submitted again is the same evidence and is reused rather than duplicated. A different
record, source, classification or retention under an existing identity is a conflict.

Every record registered in the semantic ledger above is, in the same transaction, also registered as one L0
evidence artifact under the same identity and checksum -- the ledger `evidence.search` actually reads. The two
registrations are one fact, not two independent writes: `evidence.search` is the sanctioned way a consumer reads
back what this operation produced, and a record this operation reports but `evidence.search` cannot find is a
contract defect this module exists to not have. The post-commit step that proves a reported record is findable
is the same barrier `evidence.capture` and `import.start`'s execution run after their own commits, for the same
reason: nesting the projection lifecycle inside the business transaction would either deadlock the single write
connection or roll back durable evidence because an index lagged.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INTERNAL_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ContractDecodeError,
    ContractSemanticError,
    KnowledgeEvaluationEvidenceRecord,
    KnowledgeEvaluationProduceInput,
    KnowledgeEvaluationProduceResult,
    idempotency_equivalence,
    to_canonical_json,
)
from omnivia_core.governed_knowledge.errors import GovernedKnowledgeError
from omnivia_core.governed_knowledge.evaluation import evaluation_report_to_content
from omnivia_core.governed_knowledge.stage2_producer import (
    CanonicalRecord,
    Stage2Production,
    produce_evaluation_report,
)
from omnivia_core.semantic_registry.errors import SemanticRegistryError
from omnivia_core.semantic_registry.evidence import (
    Classification,
    EvidenceItem,
    EvidenceLocatorScheme,
    EvidenceSource,
    EvidenceSourceKind,
)
from omnivia_core.semantic_registry.temporal import (
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
)
from omnivia_core_runtime.ownership.fencing import MutationGuard, read_guard
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.mutation import (
    MutationGrant,
    MutationSettlementContext,
    execute_mutation,
    issue_mutation_grant,
)
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    application_refusal,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.projections.fts import (
    build_search_projection,
    open_search_projection,
)
from omnivia_core_runtime.storage.semantic_evidence import (
    EvidenceObservationWriter,
    read_evidence_by_digest,
    read_evidence_item,
    read_evidence_source,
)
from omnivia_core_runtime.workspace.blob_publication import (
    BlobPublicationRefused,
    publish_blob,
)

OPERATION_EVALUATION_PRODUCE: Final = "knowledge.evaluation.produce"
KNOWLEDGE_EVALUATION_FAMILY_OPERATIONS: Final = frozenset({OPERATION_EVALUATION_PRODUCE})

_INPUT_KEYS: Final = frozenset(
    {
        "overlay",
        "suite",
        "cases",
        "attempts",
        "worker_bindings",
        "report_id",
        "triggering_case_ref",
        "classification",
        "retention_class",
        "source",
    }
)
_SOURCE_KEYS: Final = frozenset({"source_id", "kind", "locator_scheme", "locator", "version"})
_EVIDENCE_MIME_TYPE: Final = "application/json"
_MESSAGE_NO_STORAGE: Final = (
    "the evidence store is not reachable from this service instance"
)
_MESSAGE_INVALID: Final = "the request payload is not valid for this evaluation"
_MESSAGE_CONFLICT: Final = (
    "the submitted evidence conflicts with evidence already registered under the same identity"
)

#: The L0 `source_kind` this operation's own artifacts carry. Reserved to this operation the
#: way `direct_submission` is reserved to `evidence.capture`: nothing else in this build writes
#: it, so the 0041 source-identity index (workspace, kind, native id, locator, retrieved-at)
#: can never collide with an unrelated writer's rows.
_L0_SOURCE_KIND: Final = "governed_knowledge.evaluation"
_L0_PARSER_STATUS: Final = "not_parsed"
_L0_INGESTION_STATUS: Final = "ingested"
_L0_ACTOR_KIND: Final = "agent"
_L0_PROVENANCE_ACTION: Final = "governed_knowledge.produced"
_MESSAGE_BLOB_UNPUBLISHED: Final = (
    "the submitted evidence content could not be made durable in this workspace"
)
_MESSAGE_NOT_SEARCHABLE: Final = (
    "the evidence this evaluation produced did not become findable by evidence.search"
)
_MESSAGE_DIGEST_COLLISION: Final = (
    "one content digest names two different byte lengths in this workspace"
)


class _IdentityConflict(Exception):
    """A record or source would replace a different one already in the ledger."""


def _servable(decode: Callable[[object], object]) -> Callable[[Mapping[str, Any]], bool]:
    def valid(wire: Mapping[str, Any]) -> bool:
        try:
            decode(wire)
        except (ContractDecodeError, ContractSemanticError):
            return False
        return True

    return valid


_VALID_PRODUCE = _servable(KnowledgeEvaluationProduceResult.from_wire)


def _refusing(action: Callable[[], Any]) -> Any:
    """Run one domain step, mapping its refusals onto the contract's error codes.

    A domain validation failure is `invalid_request` with a fixed message, so no submitted value reaches a
    caller. An identity conflict is `conflict`. Nothing here writes, so the caller's rollback covers the rest.
    """
    try:
        return action()
    except _IdentityConflict as conflict:
        raise application_refusal(ERROR_CODE_CONFLICT, _MESSAGE_CONFLICT) from conflict
    except (GovernedKnowledgeError, SemanticRegistryError, ValueError) as invalid:
        raise application_refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from invalid


def _plain(value: Any) -> Any:
    """Decoded wire content holds tuples and mappings; the Stage 2 producer reads plain lists and dicts."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _captured_at(settled_at_us: int) -> TemporalInstant:
    """Core's own capture moment, at the second. No source time is authorised, so it is an ingestion fallback."""
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=settled_at_us // 1_000_000)
    return TemporalInstant(
        value=moment,
        precision=TemporalPrecision.SECOND,
        provenance=TemporalProvenance.INGESTION_FALLBACK,
    )


def _identity_of(item: EvidenceItem) -> tuple[object, ...]:
    """Everything that makes an evidence item *the same item*. The capture moment is not part of it."""
    return (
        item.evidence_id,
        item.content_ref,
        item.content_digest,
        item.integrity_digest,
        item.mime_type,
        item.classification,
        item.retention_class,
        item.source,
        item.source_time,
        item.span,
    )


def _evidence_item(
    record: CanonicalRecord,
    *,
    workspace_id: str,
    source: EvidenceSource,
    classification: Classification,
    retention_class: str,
    captured_at: TemporalInstant,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=record.record_id,
        workspace_id=workspace_id,
        source=source,
        content_ref="omnivia-content:" + record.checksum,
        content_digest=record.checksum,
        integrity_digest=record.checksum,
        mime_type=_EVIDENCE_MIME_TYPE,
        classification=classification,
        retention_class=retention_class,
        captured_at=captured_at,
    )


def _plan_writes(
    connection: Any,
    *,
    workspace_id: str,
    source: EvidenceSource,
    items: list[EvidenceItem],
) -> list[EvidenceItem]:
    """Refuse every identity conflict before a single write, and return only the items that are new.

    An item already stored under its identity, byte for byte, is reused. Anything else under that identity
    or digest, or a source reused for different metadata, is a conflict. Two records in one submission that
    share an identity are a conflict too, since the second would replace the first.
    """
    stored_source = read_evidence_source(connection, workspace_id, source.source_id)
    if stored_source is not None and stored_source != source:
        raise _IdentityConflict()
    if len({item.evidence_id for item in items}) != len(items):
        raise _IdentityConflict()
    planned: list[EvidenceItem] = []
    for item in items:
        by_id = read_evidence_item(connection, workspace_id, item.evidence_id)
        by_digest = read_evidence_by_digest(connection, workspace_id, item.content_digest)
        if by_id is None and by_digest is None:
            planned.append(item)
        elif (
            by_id is not None
            and by_digest is not None
            and by_id.evidence_id == by_digest.evidence_id
            and _identity_of(by_id) == _identity_of(item)
        ):
            continue
        else:
            raise _IdentityConflict()
    return planned


@dataclass
class KnowledgeEvaluationHandlers:
    """The one evaluation operation over one workspace, under the contributor session it is composed with."""

    service: Any
    session: AuthenticatedSession
    binding: ServiceBinding
    clock: Clock
    allocate_identifier: Callable[[str], str]

    def _connection(self) -> Any:
        connection = getattr(self.service, "connection", None)
        if connection is None:
            raise application_refusal(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        return connection

    def _authority(self) -> tuple[Any, Any, MutationGuard]:
        connection = self._connection()
        identity = getattr(self.service, "identity", None)
        guard = read_guard(connection)
        if identity is None or guard is None:
            raise application_refusal(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        return connection, identity, guard

    def _blobs_root(self) -> Path:
        """The workspace's blob root, the same fact `evidence.capture`'s own barrier reads it from."""
        layout = getattr(self.service, "layout", None)
        blobs_root = getattr(layout, "blobs_path", None)
        if not isinstance(blobs_root, Path):
            raise application_refusal(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        return blobs_root

    @staticmethod
    def _input(context: OperationContext) -> KnowledgeEvaluationProduceInput:
        """Decode the request, refusing any key the operation does not declare rather than dropping it."""
        raw = context.request.input
        if not isinstance(raw, Mapping) or not set(raw) <= _INPUT_KEYS:
            raise application_refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        source = raw.get("source")
        if not isinstance(source, Mapping) or not set(source) <= _SOURCE_KEYS:
            raise application_refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        try:
            return KnowledgeEvaluationProduceInput.from_wire(raw)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise application_refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error

    def _grant(
        self, context: OperationContext, payload: Mapping[str, Any]
    ) -> tuple[MutationGrant, Any]:
        _connection, _identity, guard = self._authority()
        if context.authorization is None:
            raise application_refusal(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            payload,
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )
        grant = issue_mutation_grant(
            context.authorization,
            session=self.session,
            binding=self.binding,
            guard=guard,
            equivalence=equivalence,
            clock=self.clock,
        )
        return grant, equivalence

    def _register_l0_artifact(
        self,
        fenced: Any,
        *,
        workspace_id: str,
        record: CanonicalRecord,
        caller_source_id: str,
        sensitivity: str,
        now_us: int,
        principal: str,
        audit_ref: str,
        blobs_root: Path,
    ) -> None:
        """Register one canonical record as the L0 artifact `evidence.search` reads.

        The same identity as the semantic registration beside it: `evidence_id` is the record's own
        `record_id`, and `content_checksum`/`blob_content_digest` are its `checksum`. `source_native_id`
        and `source_locator` are that same `record_id` too -- a Dev consumer of `evidence.search`, which
        redacts `source`, can derive the exact identity of the row it is looking at from the record it
        was authorized to see, rather than from the caller's own `source.source_id`, which it cannot read.
        Distinct per record, which is what keeps 0041's source-identity index (workspace, kind, native id,
        locator, retrieved-at) satisfied across every record one submission produces. The caller's source
        is not lost: it is still the semantic registration's `EvidenceSource` and is carried here too, in
        `original_metadata_json`, as `caller_source_id` -- present for provenance, never the row's own
        identity. `staged_source_ref` and `import_run_id` are left NULL; there is no staging claim and no
        import run behind a Stage 2 submission to name.

        Bytes before the row that names them, the same order `evidence.capture` writes in: the content this
        checksum addresses is published to the blob store first, so the row this call is about to insert can
        never outlive its own bytes.
        """
        source_native_id = record.record_id
        content = record.canonical_json.encode("utf-8")
        checksum = record.checksum
        published = True
        try:
            publish_blob(blobs_root, checksum, content)
        except (BlobPublicationRefused, OSError):
            published = False
        if not published:
            raise application_refusal(ERROR_CODE_INTERNAL_RECOVERABLE, _MESSAGE_BLOB_UNPUBLISHED)

        blob = fenced.execute(
            "SELECT content_length_bytes FROM omnivia_blob_objects "
            "WHERE workspace_id = ? AND content_digest = ?",
            (workspace_id, checksum),
        ).fetchone()
        if blob is None:
            fenced.execute(
                "INSERT INTO omnivia_blob_objects "
                "(workspace_id, content_digest, content_length_bytes, created_at_us, "
                "verified_at_us) VALUES (?, ?, ?, ?, ?)",
                (workspace_id, checksum, len(content), now_us, now_us),
            )
            integrity_sequence = int(
                fenced.execute(
                    "SELECT COALESCE(MAX(integrity_sequence), 0) + 1 "
                    "FROM omnivia_blob_integrity_events "
                    "WHERE workspace_id = ? AND content_digest = ?",
                    (workspace_id, checksum),
                ).fetchone()[0]
            )
            fenced.execute(
                "INSERT INTO omnivia_blob_integrity_events "
                "(integrity_event_id, workspace_id, content_digest, integrity_sequence, "
                "outcome, observed_digest, observed_length_bytes, expected_length_bytes, "
                "inventory_id, checked_at_us) VALUES (?, ?, ?, ?, 'verified', ?, ?, ?, NULL, ?)",
                (
                    self.allocate_identifier("bie"),
                    workspace_id,
                    checksum,
                    integrity_sequence,
                    checksum,
                    len(content),
                    len(content),
                    now_us,
                ),
            )
        elif int(blob[0]) != len(content):
            # One content address, two byte lengths: the Stage 2 producer disagrees with itself about
            # what these bytes are. Not the caller's doing -- `_plan_writes` already proved this exact
            # digest is new -- and not repairable here.
            raise application_refusal(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_DIGEST_COLLISION)

        metadata = to_canonical_json(
            {
                "produced_by": OPERATION_EVALUATION_PRODUCE,
                "record_kind": record.kind,
                "caller_source_id": caller_source_id,
            }
        )
        metadata_digest = f"sha256:{hashlib.sha256(metadata.encode('utf-8')).hexdigest()}"
        fenced.execute(
            "INSERT INTO omnivia_evidence_artifacts "
            "(evidence_id, workspace_id, source_kind, source_native_id, source_locator, "
            "source_retrieved_at_us, event_at_us, observed_at_us, ingested_at_us, "
            "recorded_at_us, content_checksum, blob_content_digest, media_type, "
            "original_metadata_json, original_metadata_digest, sensitivity, parser_status, "
            "ingestion_status, staged_source_ref, import_run_id) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
            (
                record.record_id,
                workspace_id,
                _L0_SOURCE_KIND,
                source_native_id,
                record.record_id,
                now_us,
                now_us,
                checksum,
                checksum,
                _EVIDENCE_MIME_TYPE,
                metadata,
                metadata_digest,
                sensitivity,
                _L0_PARSER_STATUS,
                _L0_INGESTION_STATUS,
            ),
        )
        fenced.execute(
            "INSERT INTO omnivia_evidence_provenance_events "
            "(provenance_event_id, evidence_id, workspace_id, provenance_sequence, actor_id, "
            "actor_kind, action, occurred_at_us, reason_code, reason_comment, parser_status, "
            "ingestion_status, tombstoned_observation, source_kind, source_native_id, "
            "audit_ref) VALUES (?, ?, ?, 1, ?, ?, ?, ?, NULL, NULL, ?, ?, 0, ?, ?, ?)",
            (
                self.allocate_identifier("prv"),
                record.record_id,
                workspace_id,
                principal,
                _L0_ACTOR_KIND,
                _L0_PROVENANCE_ACTION,
                now_us,
                _L0_PARSER_STATUS,
                _L0_INGESTION_STATUS,
                _L0_SOURCE_KIND,
                source_native_id,
                audit_ref,
            ),
        )

    def _require_findable(
        self, connection: Any, identity: Any, *, workspace_id: str, evidence_ids: tuple[str, ...]
    ) -> None:
        """Gate A for this operation: refuse to report success `evidence.search` would contradict.

        The same barrier `evidence.capture` and `import.start`'s execution run after their own commits.
        The guard is re-read rather than reused, so the generation this runs under is the live one and not
        the one the mutation began with. Membership in the rebuilt projection's material is what is asked,
        the same fact `import_execution.py`'s own barrier asks for an artifact whose bytes this path does
        not claim to make full-text searchable -- the identity surface is what the frozen frontier and the
        ranker key off, and that is exactly what `SearchProjection.material` carries.
        """
        if not evidence_ids:
            return
        failed = False
        try:
            guard = read_guard(connection)
            if guard is None:
                failed = True
            else:
                build_search_projection(
                    connection,
                    identity,
                    workspace_id=workspace_id,
                    fencing_generation=guard.fencing_generation,
                    now_us=int(self.clock.wall_time().timestamp() * 1_000_000),
                )
                projection = open_search_projection(
                    connection, workspace_id=workspace_id, blobs_root=self._blobs_root()
                )
                failed = any(
                    evidence_id not in projection.material for evidence_id in evidence_ids
                )
        except (StorageError, OSError):
            failed = True
        if not failed:
            return
        raise application_refusal(ERROR_CODE_INTERNAL_RECOVERABLE, _MESSAGE_NOT_SEARCHABLE)

    def _produce(
        self,
        fenced: Any,
        context: OperationContext,
        request: KnowledgeEvaluationProduceInput,
        settlement: MutationSettlementContext,
    ) -> Mapping[str, Any]:
        """Derive the report, check every identity, then register the records. Runs inside the transaction."""
        production: Stage2Production = _refusing(
            lambda: produce_evaluation_report(
                {
                    "overlay": _plain(request.overlay),
                    "suite": _plain(request.suite),
                    "cases": _plain(request.cases),
                    "attempts": _plain(request.attempts),
                    "worker_bindings": _plain(request.worker_bindings),
                },
                report_id=request.report_id,
                triggering_case_ref=request.triggering_case_ref,
                classification=Classification(request.classification),
                retention_class=request.retention_class,
            )
        )
        source = _refusing(
            lambda: EvidenceSource(
                source_id=request.source.source_id,
                kind=EvidenceSourceKind(request.source.kind),
                locator_scheme=EvidenceLocatorScheme(request.source.locator_scheme),
                locator=request.source.locator,
                version=request.source.version,
            )
        )
        captured_at = _captured_at(settlement.settled_at_us)

        def item_for(record: CanonicalRecord) -> EvidenceItem:
            return _evidence_item(
                record,
                workspace_id=context.workspace_id,
                source=source,
                classification=Classification(request.classification),
                retention_class=request.retention_class,
                captured_at=captured_at,
            )

        items = _refusing(lambda: [item_for(record) for record in production.records])
        # Every identity is checked before the first write, so a conflict on the last record leaves nothing
        # written for the first. The transaction would roll that back too; checking first keeps it obvious.
        planned = _refusing(
            lambda: _plan_writes(
                fenced,
                workspace_id=context.workspace_id,
                source=source,
                items=items,
            )
        )
        records_by_id = {record.record_id: record for record in production.records}
        blobs_root = self._blobs_root()
        writer = EvidenceObservationWriter(fenced, context.workspace_id)
        for item in planned:
            writer.register_evidence(item)
            self._register_l0_artifact(
                fenced,
                workspace_id=context.workspace_id,
                record=records_by_id[item.evidence_id],
                caller_source_id=source.source_id,
                sensitivity=request.classification,
                now_us=settlement.settled_at_us,
                principal=context.principal,
                audit_ref=settlement.audit_ref,
                blobs_root=blobs_root,
            )
        return KnowledgeEvaluationProduceResult(
            report_id=production.report.report_id,
            report=evaluation_report_to_content(production.report),
            submitted_by=context.principal,
            evidence=tuple(
                KnowledgeEvaluationEvidenceRecord(
                    evidence_id=record.record_id,
                    record_kind=record.kind,
                    record_id=record.record_id,
                    content_digest=record.checksum,
                )
                for record in production.records
            ),
        ).to_wire()

    def knowledge_evaluation_produce(self, context: OperationContext) -> AuditedOperationResult:
        """Derive one evaluation report from submitted Stage 2 content and register its evidence."""
        request = self._input(context)
        payload = request.to_wire()
        connection, identity, _guard = self._authority()
        grant, equivalence = self._grant(context, payload)
        outcome = execute_mutation(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            mutate=lambda fenced, settlement: self._produce(fenced, context, request, settlement),
            validate_result=_VALID_PRODUCE,
            clock=self.clock,
            allocate_identifier=self.allocate_identifier,
        )
        # Gate A, after the commit and outside it -- on a replay too, since a replay's stored result
        # names the same records a first attempt would have, and nothing here may report a success
        # `evidence.search` would contradict.
        evidence_ids = tuple(
            entry["evidence_id"]
            for entry in outcome.result.get("evidence", ())
            if isinstance(entry, Mapping) and isinstance(entry.get("evidence_id"), str)
        )
        self._require_findable(
            connection, identity, workspace_id=context.workspace_id, evidence_ids=evidence_ids
        )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)


__all__ = [
    "KNOWLEDGE_EVALUATION_FAMILY_OPERATIONS",
    "OPERATION_EVALUATION_PRODUCE",
    "KnowledgeEvaluationHandlers",
]
