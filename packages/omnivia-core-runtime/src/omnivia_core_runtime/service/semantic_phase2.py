"""Permission-checked Phase 2 evidence, candidate, and temporal application API.

This module is an in-process service boundary.  It accepts only transport-established
authority, returns typed neutral records, and composes the existing fenced repositories.
It deliberately contains no transport, worker, network, approval, or publication path.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from omnivia_core.semantic_registry import (
    AGGREGATION_RULE_VERSION,
    NORMALIZATION_RULE_VERSION,
    AssertionRetraction,
    AssertionSupersession,
    CandidateAggregation,
    CandidateReconsideration,
    CandidateSuppression,
    ChangeOperation,
    Classification,
    EffectiveValidInterval,
    EvidenceItem,
    EvidenceSpan,
    IdAllocator,
    ObservationBundle,
    ObservationGeneration,
    ReconsiderationReason,
    SuppressionDecision,
    TemporalInstant,
    UUIDv7Allocator,
    assertion_effective_interval,
    build_candidate,
    build_reconsideration,
    candidate_equivalence_signature,
    content_digest,
    effective_classification,
    suppression_active,
)
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.service.semantic_registry import (
    Proposal,
    SemanticRegistryService,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.semantic_events import (
    SEMANTIC_CANDIDATE_CREATED_V1,
    SEMANTIC_CANDIDATE_RECONSIDERED_V1,
    SEMANTIC_CANDIDATE_SUPPRESSED_V1,
    SEMANTIC_OBSERVATION_RECORDED_V1,
)
from omnivia_core_runtime.storage.semantic_evidence import (
    read_evidence_item,
    read_observation_bundle,
    semantic_evidence_writer,
)
from omnivia_core_runtime.storage.semantic_governance import (
    AssertionRecord,
    CandidateRecord,
    query_assertions,
    read_assertion,
    read_assertion_retractions,
    read_assertion_supersessions,
    read_candidate,
    semantic_governance_writer,
)
from omnivia_core_runtime.storage.semantic_registry import (
    OutboxRow,
    read_outbox,
    read_pointer,
    read_version,
    version_of,
)

RESPONSE_SCHEMA_VERSION: Final = "1.0.0"
OBSERVATION_RECORDED_EVENT: Final = SEMANTIC_OBSERVATION_RECORDED_V1
CANDIDATE_CREATED_EVENT: Final = SEMANTIC_CANDIDATE_CREATED_V1
CANDIDATE_SUPPRESSED_EVENT: Final = SEMANTIC_CANDIDATE_SUPPRESSED_V1
CANDIDATE_RECONSIDERED_EVENT: Final = SEMANTIC_CANDIDATE_RECONSIDERED_V1
_PHASE2_EVENT_ID_KEYS: Final = {
    OBSERVATION_RECORDED_EVENT: "observation_id",
    CANDIDATE_CREATED_EVENT: "candidate_id",
    CANDIDATE_SUPPRESSED_EVENT: "suppression_id",
    CANDIDATE_RECONSIDERED_EVENT: "reconsideration_id",
}

EVIDENCE_REGISTER: Final = "evidence.register"
EVIDENCE_METADATA_READ: Final = "evidence.metadata.read"
EVIDENCE_CONTENT_READ: Final = "evidence.content.read"
OBSERVATION_CREATE_MANUAL: Final = "observation.create.manual"
OBSERVATION_CREATE_RULE: Final = "observation.create.rule"
CANDIDATE_AGGREGATE: Final = "candidate.aggregate"
CANDIDATE_READ: Final = "candidate.read"
CANDIDATE_REJECT: Final = "candidate.reject"
CANDIDATE_RECONSIDER: Final = "candidate.reconsider"
CANDIDATE_CONVERT: Final = "candidate.convert"
ASSERTION_HISTORY_READ: Final = "assertion.history.read"
TEMPORAL_QUERY: Final = "temporal.query"

#: Mutating methods on the in-process Phase 2 seam. Any future HTTP, MCP, CLI or
#: other public adapter must enumerate its composition through the repository-wide
#: governed caller-scoped idempotency seam before this inventory can become nonempty.
PHASE2_MUTATING_SERVICE_OPERATIONS: Final = frozenset(
    {
        "register_evidence",
        "create_observation",
        "aggregate_candidate",
        "reject_candidate",
        "reconsider_candidate",
        "convert_candidate",
    }
)
PHASE2_EXTERNAL_MUTATION_ADAPTERS: Final[tuple[str, ...]] = ()

SEMANTIC_NOT_FOUND: Final = "SEMANTIC_NOT_FOUND"
SEMANTIC_PERMISSION_DENIED: Final = "SEMANTIC_PERMISSION_DENIED"
SEMANTIC_WORKSPACE_MISMATCH: Final = "SEMANTIC_WORKSPACE_MISMATCH"
SEMANTIC_BASE_CONFLICT: Final = "SEMANTIC_BASE_CONFLICT"
SEMANTIC_FENCE_STALE: Final = "SEMANTIC_FENCE_STALE"
SEMANTIC_EVIDENCE_RESTRICTED: Final = "SEMANTIC_EVIDENCE_RESTRICTED"
SEMANTIC_TEMPORAL_BOUNDARY_INVALID: Final = "SEMANTIC_TEMPORAL_BOUNDARY_INVALID"

_MESSAGE_NOT_FOUND: Final = "the requested semantic record is unavailable"
_MESSAGE_DENIED: Final = "the requested semantic operation is not authorised"
_MESSAGE_RESTRICTED: Final = "protected evidence is not authorised"
_MESSAGE_WORKSPACE: Final = (
    "the request authority does not match the claimed workspace or actor"
)
_MESSAGE_BASE: Final = "the candidate base is no longer current"
_MESSAGE_FENCE: Final = "the canonical writer lease is stale"
_MESSAGE_TEMPORAL: Final = "the requested temporal boundary is invalid"


class SemanticServiceError(Exception):
    """Stable, non-disclosing service refusal."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(f"[{code}] {message}")


def read_phase2_events(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    aggregate_id: str,
    expected_generation: int | None = None,
) -> tuple[OutboxRow, ...]:
    """Read a Phase 2 stream and reject unknown versions or malformed envelopes."""
    rows = read_outbox(connection, workspace_id=workspace_id, aggregate_id=aggregate_id)
    for row in rows:
        id_key = _PHASE2_EVENT_ID_KEYS.get(row.event_kind)
        if id_key is None:
            raise StorageError("unsupported Phase 2 semantic event version")
        expected_keys = {"workspace_id", "fencing_generation", id_key}
        generation = row.payload.get("fencing_generation")
        if (
            set(row.payload) != expected_keys
            or row.payload.get("workspace_id") != workspace_id
            or row.payload.get(id_key) != aggregate_id
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation <= 0
            or (expected_generation is not None and generation != expected_generation)
            or content_digest(row.payload) != row.payload_digest
        ):
            raise StorageError("invalid Phase 2 semantic event envelope")
    return rows


@dataclass(frozen=True, slots=True)
class SemanticAuthority:
    """Server-established authority for one request; never built from payload data."""

    principal_id: str
    workspace_id: str
    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))


AuthorizationCheck = Callable[[SemanticAuthority, str], bool]
ContentResolver = Callable[[str], bytes]


def authority_grants(context: SemanticAuthority, capability: str) -> bool:
    """Default fail-closed authorizer over the server-established capability set."""
    return capability in context.capabilities


@dataclass(frozen=True, slots=True)
class EvidenceMetadataView:
    response_schema_version: str
    resolved_workspace_id: str
    evidence_id: str
    source_id: str
    source_kind: str
    locator_scheme: str
    locator: str
    source_version: str
    content_digest: str
    integrity_digest: str
    mime_type: str
    classification: Classification
    retention_class: str
    captured_at: TemporalInstant
    source_time: TemporalInstant | None
    permission_filtered: bool = True


@dataclass(frozen=True, slots=True)
class SensitiveEvidenceView:
    response_schema_version: str
    resolved_workspace_id: str
    evidence_id: str
    content: bytes
    span: EvidenceSpan | None
    permission_filtered: bool = True


@dataclass(frozen=True, slots=True)
class CandidateView:
    response_schema_version: str
    resolved_workspace_id: str
    model_version_id: str
    record: CandidateRecord
    classification: Classification
    permission_filtered: bool = True


@dataclass(frozen=True, slots=True)
class AssertionHistoryView:
    response_schema_version: str
    resolved_workspace_id: str
    records: tuple[AssertionRecord, ...]
    supersessions: tuple[AssertionSupersession, ...]
    retractions: tuple[AssertionRetraction, ...]
    permission_filtered: bool = True


@dataclass(frozen=True, slots=True)
class TemporalQueryView:
    response_schema_version: str
    resolved_workspace_id: str
    resolved_recorded_at: TemporalInstant
    resolved_valid_at: TemporalInstant
    records: tuple[AssertionRecord, ...]
    effective_intervals: tuple[EffectiveValidInterval, ...]
    permission_filtered: bool = True


class SemanticPhase2Service:
    """Phase 2 service bound to one authoritative workspace writer."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        identity: ServiceInstanceIdentity,
        *,
        workspace_id: str,
        fencing_generation: int,
        authorizer: AuthorizationCheck = authority_grants,
        content_resolver: ContentResolver | None = None,
        allocator: IdAllocator | None = None,
        clock: Callable[[], int] | None = None,
        workspace_floor: Classification = Classification.PUBLIC,
    ) -> None:
        self.connection = connection
        self.identity = identity
        self.workspace_id = workspace_id
        self.fencing_generation = fencing_generation
        self.authorizer = authorizer
        self.content_resolver = content_resolver
        self.allocator = allocator or UUIDv7Allocator()
        self.clock = clock or (lambda: time.time_ns() // 1_000)
        self.workspace_floor = workspace_floor

    def _id(self, prefix: str) -> str:
        return f"{prefix}-{self.allocator.new_id()}"

    def _check(
        self,
        context: SemanticAuthority,
        capability: str,
        *,
        claimed_workspace_id: str | None = None,
        claimed_actor_id: str | None = None,
        sensitive: bool = False,
    ) -> None:
        if (
            context.workspace_id != self.workspace_id
            or (
                claimed_workspace_id is not None
                and claimed_workspace_id != context.workspace_id
            )
            or (
                claimed_actor_id is not None
                and claimed_actor_id != context.principal_id
            )
        ):
            raise SemanticServiceError(SEMANTIC_WORKSPACE_MISMATCH, _MESSAGE_WORKSPACE)
        if not self.authorizer(context, capability):
            if sensitive:
                raise SemanticServiceError(
                    SEMANTIC_EVIDENCE_RESTRICTED, _MESSAGE_RESTRICTED
                )
            raise SemanticServiceError(SEMANTIC_PERMISSION_DENIED, _MESSAGE_DENIED)

    def _not_found(self) -> SemanticServiceError:
        return SemanticServiceError(SEMANTIC_NOT_FOUND, _MESSAGE_NOT_FOUND)

    def register_evidence(
        self, context: SemanticAuthority, item: EvidenceItem, *, actor_id: str
    ) -> EvidenceItem:
        self._check(
            context,
            EVIDENCE_REGISTER,
            claimed_workspace_id=item.workspace_id,
            claimed_actor_id=actor_id,
        )
        result = item
        failure: SemanticServiceError | None = None
        try:
            with semantic_evidence_writer(
                self.connection,
                self.identity,
                workspace_id=self.workspace_id,
                fencing_generation=self.fencing_generation,
            ) as writer:
                self._check(context, EVIDENCE_REGISTER, claimed_actor_id=actor_id)
                result = writer.register_evidence(item)
        except StaleGeneration:
            failure = SemanticServiceError(SEMANTIC_FENCE_STALE, _MESSAGE_FENCE)
        if failure is not None:
            raise failure from None
        return result

    def evidence_metadata(
        self, context: SemanticAuthority, evidence_id: str
    ) -> EvidenceMetadataView:
        self._check(context, EVIDENCE_METADATA_READ)
        item = read_evidence_item(self.connection, self.workspace_id, evidence_id)
        if item is None:
            raise self._not_found()
        return EvidenceMetadataView(
            response_schema_version=RESPONSE_SCHEMA_VERSION,
            resolved_workspace_id=self.workspace_id,
            evidence_id=item.evidence_id,
            source_id=item.source.source_id,
            source_kind=item.source.kind.value,
            locator_scheme=item.source.locator_scheme.value,
            locator=item.source.locator,
            source_version=item.source.version,
            content_digest=item.content_digest,
            integrity_digest=item.integrity_digest,
            mime_type=item.mime_type,
            classification=effective_classification(
                self.workspace_floor, item.classification, item.classification
            ),
            retention_class=item.retention_class,
            captured_at=item.captured_at,
            source_time=item.source_time,
        )

    def sensitive_evidence(
        self, context: SemanticAuthority, evidence_id: str
    ) -> SensitiveEvidenceView:
        # This check intentionally precedes both the metadata lookup and resolver call.
        self._check(context, EVIDENCE_CONTENT_READ, sensitive=True)
        item = read_evidence_item(self.connection, self.workspace_id, evidence_id)
        if item is None:
            raise self._not_found()
        if self.content_resolver is None:
            raise self._not_found()
        content = self.content_resolver(item.content_ref)
        return SensitiveEvidenceView(
            response_schema_version=RESPONSE_SCHEMA_VERSION,
            resolved_workspace_id=self.workspace_id,
            evidence_id=item.evidence_id,
            content=content,
            span=item.span,
        )

    def create_observation(
        self, context: SemanticAuthority, bundle: ObservationBundle, *, actor_id: str
    ) -> ObservationBundle:
        capability = (
            OBSERVATION_CREATE_MANUAL
            if bundle.observation.generation is ObservationGeneration.MANUAL
            else OBSERVATION_CREATE_RULE
        )
        self._check(
            context,
            capability,
            claimed_workspace_id=bundle.observation.workspace_id,
            claimed_actor_id=actor_id,
        )
        failure: SemanticServiceError | None = None
        try:
            with semantic_evidence_writer(
                self.connection,
                self.identity,
                workspace_id=self.workspace_id,
                fencing_generation=self.fencing_generation,
            ) as writer:
                self._check(context, capability, claimed_actor_id=actor_id)
                writer.append_observation(bundle)
                writer.append_outbox(
                    outbox_id=self._id("ob"),
                    aggregate_id=bundle.observation.observation_id,
                    event_kind=OBSERVATION_RECORDED_EVENT,
                    payload={
                        "workspace_id": self.workspace_id,
                        "fencing_generation": self.fencing_generation,
                        "observation_id": bundle.observation.observation_id,
                    },
                    now_us=self.clock(),
                )
        except StaleGeneration:
            failure = SemanticServiceError(SEMANTIC_FENCE_STALE, _MESSAGE_FENCE)
        if failure is not None:
            raise failure from None
        return bundle

    def aggregate_candidate(
        self,
        context: SemanticAuthority,
        *,
        target_model_id: str,
        candidate_kind: str,
        proposed_operation: ChangeOperation,
        observation_ids: Sequence[str],
        created_at: TemporalInstant,
        actor_id: str,
        candidate_id: str | None = None,
        aggregation_version: str = AGGREGATION_RULE_VERSION,
        normalization_version: str = NORMALIZATION_RULE_VERSION,
    ) -> CandidateAggregation:
        self._check(context, CANDIDATE_AGGREGATE, claimed_actor_id=actor_id)
        pointer = read_pointer(
            self.connection, workspace_id=self.workspace_id, model_id=target_model_id
        )
        if pointer is None or pointer.current_version_id is None:
            raise self._not_found()
        base_row = read_version(
            self.connection,
            workspace_id=self.workspace_id,
            model_id=target_model_id,
            version_id=pointer.current_version_id,
        )
        if base_row is None:
            raise self._not_found()
        bundles: list[ObservationBundle] = []
        evidence: dict[str, EvidenceItem] = {}
        for observation_id in observation_ids:
            bundle = read_observation_bundle(
                self.connection, self.workspace_id, observation_id
            )
            if bundle is None:
                raise self._not_found()
            bundles.append(bundle)
            for link in bundle.evidence_links:
                if link.evidence_id not in evidence:
                    item = read_evidence_item(
                        self.connection, self.workspace_id, link.evidence_id
                    )
                    if item is None:
                        raise self._not_found()
                    evidence[item.evidence_id] = item
        aggregation = build_candidate(
            candidate_id=candidate_id or self._id("cand"),
            candidate_kind=candidate_kind,
            workspace_id=self.workspace_id,
            base=version_of(base_row),
            proposed_operation=proposed_operation,
            bundles=bundles,
            evidence=evidence,
            created_at=created_at,
            aggregation_version=aggregation_version,
            normalization_version=normalization_version,
        )
        failure: SemanticServiceError | None = None
        try:
            with semantic_governance_writer(
                self.connection,
                self.identity,
                workspace_id=self.workspace_id,
                fencing_generation=self.fencing_generation,
            ) as writer:
                self._check(context, CANDIDATE_AGGREGATE, claimed_actor_id=actor_id)
                writer.append_candidate(
                    aggregation.candidate, aggregation.contributions
                )
                writer.append_outbox(
                    outbox_id=self._id("ob"),
                    aggregate_id=aggregation.candidate.candidate_id,
                    event_kind=CANDIDATE_CREATED_EVENT,
                    payload={
                        "workspace_id": self.workspace_id,
                        "fencing_generation": self.fencing_generation,
                        "candidate_id": aggregation.candidate.candidate_id,
                    },
                    now_us=self.clock(),
                )
        except StaleGeneration:
            failure = SemanticServiceError(SEMANTIC_FENCE_STALE, _MESSAGE_FENCE)
        except StorageError:
            failure = SemanticServiceError(SEMANTIC_BASE_CONFLICT, _MESSAGE_BASE)
        if failure is not None:
            raise failure from None
        return aggregation

    def inspect_candidate(
        self, context: SemanticAuthority, candidate_id: str
    ) -> CandidateView:
        self._check(context, CANDIDATE_READ)
        record = read_candidate(self.connection, self.workspace_id, candidate_id)
        if record is None:
            raise self._not_found()
        classifications: list[Classification] = []
        for contribution in record.contributions:
            bundle = read_observation_bundle(
                self.connection, self.workspace_id, contribution.observation_id
            )
            if bundle is None:
                raise self._not_found()
            classifications.append(bundle.observation.classification)
        classification = effective_classification(
            self.workspace_floor,
            self.workspace_floor,
            self.workspace_floor,
            tuple(classifications),
        )
        return CandidateView(
            response_schema_version=RESPONSE_SCHEMA_VERSION,
            resolved_workspace_id=self.workspace_id,
            model_version_id=record.candidate.base_version_id,
            record=record,
            classification=classification,
        )

    def reject_candidate(
        self,
        context: SemanticAuthority,
        candidate_id: str,
        *,
        actor_id: str,
        created_at: TemporalInstant,
        expires_at: TemporalInstant | None = None,
        suppression_id: str | None = None,
        suppression_rule_version: str = "candidate-suppression-v1",
    ) -> CandidateSuppression:
        self._check(context, CANDIDATE_REJECT, claimed_actor_id=actor_id)
        record = read_candidate(self.connection, self.workspace_id, candidate_id)
        if record is None:
            raise self._not_found()
        candidate = record.candidate
        value = CandidateSuppression(
            workspace_id=self.workspace_id,
            suppression_id=suppression_id or self._id("sup"),
            equivalence_signature=candidate_equivalence_signature(
                self.workspace_id,
                candidate.candidate_kind,
                candidate.target_model_id,
                candidate.proposed_operation,
                candidate.normalization_version,
                candidate.aggregation_version,
            ),
            rejection_ref=candidate.candidate_id,
            suppression_rule_version=suppression_rule_version,
            created_at=created_at,
            evidence_snapshot_digest=candidate.evidence_snapshot_digest,
            aggregation_version=candidate.aggregation_version,
            expires_at=expires_at,
        )
        failure: SemanticServiceError | None = None
        try:
            with semantic_governance_writer(
                self.connection,
                self.identity,
                workspace_id=self.workspace_id,
                fencing_generation=self.fencing_generation,
            ) as writer:
                self._check(context, CANDIDATE_REJECT, claimed_actor_id=actor_id)
                writer.append_suppression(value)
                writer.append_outbox(
                    outbox_id=self._id("ob"),
                    aggregate_id=value.rejection_ref,
                    event_kind=CANDIDATE_SUPPRESSED_EVENT,
                    payload={
                        "workspace_id": self.workspace_id,
                        "fencing_generation": self.fencing_generation,
                        "candidate_id": value.rejection_ref,
                        "suppression_id": value.suppression_id,
                    },
                    now_us=self.clock(),
                )
        except StaleGeneration:
            failure = SemanticServiceError(SEMANTIC_FENCE_STALE, _MESSAGE_FENCE)
        if failure is not None:
            raise failure from None
        return value

    def reconsider_candidate(
        self,
        context: SemanticAuthority,
        suppression: CandidateSuppression,
        *,
        actor_id: str,
        reason: ReconsiderationReason,
        recorded_at: TemporalInstant,
        evidence_snapshot_digest: str | None = None,
        aggregation_version: str | None = None,
        reconsideration_id: str | None = None,
    ) -> CandidateReconsideration:
        self._check(
            context,
            CANDIDATE_RECONSIDER,
            claimed_workspace_id=suppression.workspace_id,
            claimed_actor_id=actor_id,
        )
        if reason is ReconsiderationReason.HUMAN_OVERRIDE:
            value = CandidateReconsideration(
                workspace_id=self.workspace_id,
                reconsideration_id=reconsideration_id or self._id("rec"),
                suppression_id=suppression.suppression_id,
                reason=reason,
                recorded_at=recorded_at,
                actor_principal_id=actor_id,
            )
        else:
            next_evidence = (
                evidence_snapshot_digest or suppression.evidence_snapshot_digest
            )
            next_version = aggregation_version or suppression.aggregation_version
            activity = suppression_active(
                suppression, recorded_at, next_evidence, next_version
            )
            if activity.active or activity.reason is not reason:
                raise SemanticServiceError(SEMANTIC_BASE_CONFLICT, _MESSAGE_BASE)
            value = build_reconsideration(
                reconsideration_id=reconsideration_id or self._id("rec"),
                suppression=suppression,
                decision=SuppressionDecision(
                    suppressed=False,
                    suppression_id=suppression.suppression_id,
                    reason=reason,
                ),
                recorded_at=recorded_at,
                evidence_snapshot_digest=next_evidence,
                aggregation_version=next_version,
            )
        failure: SemanticServiceError | None = None
        try:
            with semantic_governance_writer(
                self.connection,
                self.identity,
                workspace_id=self.workspace_id,
                fencing_generation=self.fencing_generation,
            ) as writer:
                self._check(context, CANDIDATE_RECONSIDER, claimed_actor_id=actor_id)
                writer.append_reconsideration(value)
                writer.append_outbox(
                    outbox_id=self._id("ob"),
                    aggregate_id=value.suppression_id,
                    event_kind=CANDIDATE_RECONSIDERED_EVENT,
                    payload={
                        "workspace_id": self.workspace_id,
                        "fencing_generation": self.fencing_generation,
                        "suppression_id": value.suppression_id,
                        "reconsideration_id": value.reconsideration_id,
                    },
                    now_us=self.clock(),
                )
        except StaleGeneration:
            failure = SemanticServiceError(SEMANTIC_FENCE_STALE, _MESSAGE_FENCE)
        if failure is not None:
            raise failure from None
        return value

    def convert_candidate(
        self, context: SemanticAuthority, candidate_id: str, *, actor_id: str
    ) -> Proposal:
        self._check(context, CANDIDATE_CONVERT, claimed_actor_id=actor_id)
        record = read_candidate(self.connection, self.workspace_id, candidate_id)
        if record is None:
            raise self._not_found()
        candidate = record.candidate
        pointer = read_pointer(
            self.connection,
            workspace_id=self.workspace_id,
            model_id=candidate.target_model_id,
        )
        if pointer is None or pointer.current_version_id != candidate.base_version_id:
            raise SemanticServiceError(SEMANTIC_BASE_CONFLICT, _MESSAGE_BASE)
        # Recheck immediately at the canonical command boundary. The Phase 1 service
        # then opens its own fenced transaction and only creates an unapproved draft.
        self._check(context, CANDIDATE_CONVERT, claimed_actor_id=actor_id)
        registry = SemanticRegistryService(
            self.connection,
            self.identity,
            self.workspace_id,
            self.fencing_generation,
            clock=self.clock,
            allocator=self.allocator,
        )
        return registry.propose(
            candidate.target_model_id,
            (candidate.proposed_operation,),
            before_write=lambda: self._check(
                context, CANDIDATE_CONVERT, claimed_actor_id=actor_id
            ),
        )

    def assertion_history(self, context: SemanticAuthority) -> AssertionHistoryView:
        self._check(context, ASSERTION_HISTORY_READ)
        ids = self.connection.execute(
            "SELECT assertion_id FROM omnivia_semantic_assertions "
            "WHERE workspace_id=? ORDER BY recorded_at_us,assertion_id",
            (self.workspace_id,),
        ).fetchall()
        records = tuple(
            record
            for row in ids
            if (
                record := read_assertion(
                    self.connection, self.workspace_id, str(row[0])
                )
            )
            is not None
        )
        return AssertionHistoryView(
            response_schema_version=RESPONSE_SCHEMA_VERSION,
            resolved_workspace_id=self.workspace_id,
            records=records,
            supersessions=read_assertion_supersessions(
                self.connection, self.workspace_id
            ),
            retractions=read_assertion_retractions(self.connection, self.workspace_id),
        )

    def query_knowledge_at(
        self,
        context: SemanticAuthority,
        *,
        recorded_at: TemporalInstant,
        valid_at: TemporalInstant,
    ) -> TemporalQueryView:
        self._check(context, TEMPORAL_QUERY)
        failure: SemanticServiceError | None = None
        try:
            records = query_assertions(
                self.connection,
                self.workspace_id,
                recorded_at=recorded_at,
                valid_at=valid_at,
            )
        except (ValueError, TypeError):
            failure = SemanticServiceError(
                SEMANTIC_TEMPORAL_BOUNDARY_INVALID, _MESSAGE_TEMPORAL
            )
        if failure is not None:
            raise failure from None
        return TemporalQueryView(
            response_schema_version=RESPONSE_SCHEMA_VERSION,
            resolved_workspace_id=self.workspace_id,
            resolved_recorded_at=recorded_at,
            resolved_valid_at=valid_at,
            records=records,
            effective_intervals=tuple(
                assertion_effective_interval(record.assertion) for record in records
            ),
        )


__all__ = [
    "ASSERTION_HISTORY_READ",
    "CANDIDATE_AGGREGATE",
    "CANDIDATE_CONVERT",
    "CANDIDATE_CREATED_EVENT",
    "CANDIDATE_READ",
    "CANDIDATE_RECONSIDER",
    "CANDIDATE_RECONSIDERED_EVENT",
    "CANDIDATE_REJECT",
    "CANDIDATE_SUPPRESSED_EVENT",
    "EVIDENCE_CONTENT_READ",
    "EVIDENCE_METADATA_READ",
    "EVIDENCE_REGISTER",
    "OBSERVATION_CREATE_MANUAL",
    "OBSERVATION_CREATE_RULE",
    "OBSERVATION_RECORDED_EVENT",
    "PHASE2_EXTERNAL_MUTATION_ADAPTERS",
    "PHASE2_MUTATING_SERVICE_OPERATIONS",
    "TEMPORAL_QUERY",
    "AssertionHistoryView",
    "CandidateView",
    "EvidenceMetadataView",
    "SemanticAuthority",
    "SemanticPhase2Service",
    "SemanticServiceError",
    "SensitiveEvidenceView",
    "TemporalQueryView",
    "authority_grants",
    "read_phase2_events",
]
