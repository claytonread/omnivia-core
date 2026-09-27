"""Authoritative persistence for the V06-5 S2 memory operation family."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Final, cast

from omnivia_core.contracts.v1 import (
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INVALID_REQUEST,
    RETRY_CLASS_RETRYABLE_AFTER_DELAY,
    CandidateAssertion,
    GovernedRecord,
    MemoryCreateInput,
    MemoryCreateResult,
    RecordIdentity,
    RecordProvenance,
    RecordTemporalMetadata,
    SourceReference,
    resolve_governed_record_view,
    to_canonical_json,
    validate_memory_create_result,
)
from omnivia_core_runtime.service.mutation import MutationSettlementContext
from omnivia_core_runtime.service.operations import OperationError
from omnivia_core_runtime.storage.governed import (
    GovernedRecordValue,
    hydrate_authorized_governed_record_values,
)
from omnivia_core_runtime.storage.retrieval import EvidenceLabelGrant

if TYPE_CHECKING:
    from omnivia_core_runtime.storage.engineering_source import DependencyManifest

IdentifierAllocator = Callable[[str], str]

_PROFILE_TYPE: Final = "memory.fact"
#: The governed record types engineering observations ride (§8.1 via §22.1): the
#: schema catalogue is frozen to the 0009 vocabulary, so observations use the
#: catalogue's own finding/risk/decision types under the engineering domain.
_ENGINEERING_RECORD_TYPES: Final = ("knowledge.finding", "knowledge.risk", "knowledge.decision")
_ENGINEERING_DOMAIN: Final = "engineering.codebase"
#: The write-time content cap (§8.1): every engineering observation body is
#: bounded to this many canonical UTF-8 bytes, so a caller-facing budget
#: reasoner may use `count * ENGINEERING_CONTENT_CAP_BYTES` as a real,
#: non-fabricated worst-case bound on what full hydration would read, without
#: reading a single body.
ENGINEERING_CONTENT_CAP_BYTES: Final = 65536
_MESSAGE_INVALID_PROFILE: Final = "the memory claim is outside this supported profile"
_MESSAGE_EVIDENCE_UNAVAILABLE: Final = (
    "the memory claim's evidence is not currently available"
)


@dataclass(frozen=True, slots=True)
class AuthorizedMemorySnapshot:
    resolution_instant_us: int
    view: str
    values: tuple[GovernedRecordValue, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class AuthorizedVersion:
    """One sealed version an evidence-label grant admits, as identity facts only.

    Every field is an identity, a currentness fact or a stored digest: nothing here
    is read from `content_json`, `claim_json` or any other body column, so a caller
    holding only this value has hydrated no content. `has_evidence` is whether the
    exact version itself links any evidence.
    """

    assembly_id: str
    record_id: str
    version_id: str
    record_type: str
    domain_scope: str
    layer: str
    governance_disposition: str | None
    evidence_disposition: str
    content_digest: str
    recorded_at_us: int
    has_evidence: bool


@dataclass(frozen=True, slots=True)
class AuthorizedMemoryFrontier:
    """The frozen authorized frontier of one view, before anything is hydrated.

    `versions` are the admitted versions in the resolver's own order;
    `support_assembly_ids` are the admitted records' whole transition chains, which a
    later hydration needs and this value does not read.
    """

    resolution_instant_us: int
    view: str
    versions: tuple[AuthorizedVersion, ...]
    support_assembly_ids: tuple[str, ...]
    digest: str


def random_identifier(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


#: SQLite's host-parameter ceiling (32 766 on current builds, historically 999)
#: is an implementation limit, not a design boundary: a 100 000-record workspace
#: crosses it the first time a frontier folds evidence by `IN (...)` list. The
#: id list is therefore issued in fixed chunks and the merged rows re-sorted in
#: Python by the statement's own ORDER BY keys, which reproduces the unchunked
#: statement's rows in its order exactly at any list size (BINARY collation on
#: TEXT is code-point order, and the sort columns here are non-null keys).
_SQL_VARIABLE_CHUNK: Final = 512


def _execute_in_rows(
    connection: sqlite3.Connection,
    *,
    select: str,
    pre: str,
    in_column: str,
    post: str = "",
    leading: tuple[object, ...] = (),
    ids: Sequence[str],
    trailing: tuple[object, ...] = (),
    order_key: Callable[[tuple[object, ...]], tuple[object, ...]],
) -> list[tuple[object, ...]]:
    """One `IN (...)` query issued in host-parameter chunks, merged in order."""
    rows: list[tuple[object, ...]] = []
    for start in range(0, len(ids), _SQL_VARIABLE_CHUNK):
        chunk = ids[start : start + _SQL_VARIABLE_CHUNK]
        placeholders = ", ".join("?" for _ in chunk)
        statement = f"{select} WHERE {pre} AND {in_column} IN ({placeholders}) {post}"
        rows.extend(connection.execute(statement, (*leading, *chunk, *trailing)).fetchall())
    rows.sort(key=order_key)
    return rows


def _microseconds(value: str) -> int:
    moment = datetime.fromisoformat(value)
    return int(moment.timestamp() * 1_000_000)


def _timestamp(value: int) -> str:
    moment = datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    milliseconds = moment.microsecond // 1000
    if milliseconds == 0:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds:03d}Z"


def _digest(document: str) -> str:
    return f"sha256:{sha256(document.encode('utf-8')).hexdigest()}"


def _fold_labels(rows: list[tuple[object, ...]]) -> tuple[str, ...]:
    held: set[str] = set()
    for _sequence, action, label in rows:
        if str(action) == "withdrawn":
            held.discard(str(label))
        else:
            held.add(str(label))
    return tuple(sorted(held))


def _source_key(source: SourceReference) -> tuple[str, str, str | None, int | None]:
    return (
        source.kind,
        source.source_id,
        source.locator,
        None if source.retrieved_at is None else _microseconds(source.retrieved_at),
    )


def resolve_memory_claim_evidence(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    claim: MemoryCreateInput,
    label_grant: EvidenceLabelGrant,
) -> tuple[str, ...]:
    if claim.evidence_disposition != "available":
        if claim.sources or claim.assertion.evidence:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_PROFILE)
        return ()

    resolved: dict[tuple[str, str, str | None, int | None], str] = {}
    for source in claim.sources:
        key = _source_key(source)
        rows = connection.execute(
            "SELECT evidence_id FROM omnivia_evidence_artifacts "
            "WHERE workspace_id = ? AND source_kind = ? AND source_native_id = ? "
            "AND source_locator IS ? AND source_retrieved_at_us IS ? "
            "ORDER BY evidence_id ASC",
            (workspace_id, *key),
        ).fetchall()
        if len(rows) != 1:
            raise OperationError(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE,
                _MESSAGE_EVIDENCE_UNAVAILABLE,
                retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
            )
        evidence_id = str(rows[0][0])
        labels = _fold_labels(
            connection.execute(
                "SELECT label_sequence, label_action, permission_label "
                "FROM omnivia_evidence_permission_labels "
                "WHERE workspace_id = ? AND evidence_id = ? "
                "ORDER BY label_sequence ASC",
                (workspace_id, evidence_id),
            ).fetchall()
        )
        if not label_grant.permits(labels):
            raise OperationError(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE,
                _MESSAGE_EVIDENCE_UNAVAILABLE,
                retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
            )
        resolved[key] = evidence_id

    resolved_by_source = {
        (source.kind, source.source_id): resolved[_source_key(source)]
        for source in claim.sources
    }
    for evidence in claim.assertion.evidence:
        if evidence.span is not None or evidence.excerpt is not None:
            raise OperationError(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE,
                _MESSAGE_EVIDENCE_UNAVAILABLE,
                retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
            )
        if (evidence.source.kind, evidence.source.source_id) not in resolved_by_source:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_PROFILE)
    return tuple(resolved[_source_key(source)] for source in claim.sources)


def _plain_content(value: Any) -> Any:
    """Decode the contract's immutable containers into JSON-serialisable ones."""
    if isinstance(value, Mapping):
        return {key: _plain_content(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_content(item) for item in value]
    return value


def _validate_engineering_observation_content(
    content: Mapping[str, Any],
) -> DependencyManifest | None:
    """The `engineering.observation` content profile (SPEC-CORE-ENGMEM-001 §8.1).

    Text is validated, never silently truncated on save: a missing or
    wrong-typed required field, an oversized field or an oversized payload is a
    typed refusal, and the caller splits or fixes it explicitly. The 64 KiB cap
    bounds the canonical content bytes excluding separately referenced
    evidence. An optional `dependency_manifest` is validated whole and returned
    for persistence; a malformed one is refused rather than partly kept.
    """
    title = content.get("title")
    summary = content.get("summary")
    what = content.get("what")
    kind = content.get("kind")
    if (
        not isinstance(title, str)
        or not 1 <= len(title) <= 200
        or not isinstance(summary, str)
        or not 1 <= len(summary) <= 2000
        or not isinstance(what, str)
        or not 1 <= len(what) <= 2000
    ):
        raise OperationError(
            ERROR_CODE_INVALID_REQUEST,
            "an engineering observation requires title (<=200), summary (<=2000) "
            "and what (<=2000) as bounded strings",
        )
    if not isinstance(kind, str) or not 1 <= len(kind) <= 64:
        raise OperationError(
            ERROR_CODE_INVALID_REQUEST,
            "an engineering observation requires a bounded kind",
        )
    basis = content.get("assertion_basis")
    if basis is not None and (
        not isinstance(basis, str)
        or basis
        not in ("observed", "derived", "reported", "hypothesis")
    ):
        raise OperationError(
            ERROR_CODE_INVALID_REQUEST,
            "assertion_basis must be one of observed, derived, reported, hypothesis",
        )
    encoded = to_canonical_json(_plain_content(content))
    if len(encoded.encode("utf-8")) > ENGINEERING_CONTENT_CAP_BYTES:
        raise OperationError(
            ERROR_CODE_INVALID_REQUEST,
            "the engineering observation content exceeds the 65536-byte payload cap",
        )
    if "dependency_manifest" not in content:
        return None
    # Imported at use: engineering_source reaches this module back through decisions.
    from omnivia_core_runtime.storage import engineering_source

    try:
        manifest = engineering_source.parse_dependency_manifest(
            content["dependency_manifest"]
        )
    except engineering_source.DependencyManifestInvalid as error:
        raise OperationError(
            ERROR_CODE_INVALID_REQUEST,
            "the engineering dependency_manifest is outside its bounded profile",
        ) from error
    applicability = content.get("applicability")
    if isinstance(applicability, Mapping) and any(
        applicability.get(key) not in (None, getattr(manifest, key))
        for key in ("repository_id", "snapshot_id")
    ):
        raise OperationError(
            ERROR_CODE_INVALID_REQUEST,
            "the engineering applicability and dependency_manifest name different sources",
        )
    return manifest


def create_memory_record(
    connection: sqlite3.Connection,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    claim: MemoryCreateInput,
    label_grant: EvidenceLabelGrant,
    allocate_identifier: IdentifierAllocator = random_identifier,
) -> dict[str, object]:
    """Persist one sealed human proposal plus its immutable application lineage."""
    dependency_manifest: DependencyManifest | None = None
    if (
        claim.record_type in _ENGINEERING_RECORD_TYPES
        and claim.domain_scope == _ENGINEERING_DOMAIN
    ):
        dependency_manifest = _validate_engineering_observation_content(claim.content)
    elif claim.record_type == _PROFILE_TYPE:
        fact = claim.content.get("fact")
        if (
            not isinstance(fact, str)
            or not fact
            or claim.extraction is not None
        ):
            code = (
                ERROR_CODE_DEPENDENCY_UNAVAILABLE
                if claim.extraction is not None
                else ERROR_CODE_INVALID_REQUEST
            )
            retry = (
                RETRY_CLASS_RETRYABLE_AFTER_DELAY
                if claim.extraction is not None
                else "non_retryable"
            )
            raise OperationError(code, _MESSAGE_INVALID_PROFILE, retry_class=retry)
    else:
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_PROFILE)

    evidence_ids = resolve_memory_claim_evidence(
        connection,
        workspace_id=workspace_id,
        claim=claim,
        label_grant=label_grant,
    )
    asserted_at_us = _microseconds(claim.assertion.asserted_at)
    valid_from_us = _microseconds(
        claim.assertion.proposed_valid_from or claim.assertion.asserted_at
    )
    valid_to_us = (
        None
        if claim.assertion.proposed_valid_until is None
        else _microseconds(claim.assertion.proposed_valid_until)
    )
    event_at_us = None if claim.event_at is None else _microseconds(claim.event_at)
    observed_at_us = (
        None if claim.observed_at is None else _microseconds(claim.observed_at)
    )
    if (
        asserted_at_us > settlement.settled_at_us
        or (event_at_us is not None and event_at_us > settlement.settled_at_us)
        or (observed_at_us is not None and observed_at_us > settlement.settled_at_us)
    ):
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_PROFILE)

    record_id = allocate_identifier("rec")
    version_id = allocate_identifier("ver")
    assembly_id = allocate_identifier("asm")
    event_id = allocate_identifier("pev")
    seal_id = allocate_identifier("seal")
    content_json = to_canonical_json(_plain_content(dict(claim.content)))
    claim_json = to_canonical_json(_plain_content(claim.to_wire()))
    reason = (
        None if claim.evidence_disposition == "available" else "evidence.unavailable"
    )

    connection.execute(
        "INSERT INTO omnivia_governed_records "
        "(workspace_id, governed_record_id, record_type, domain_scope, recorded_at_us) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            workspace_id,
            record_id,
            claim.record_type,
            claim.domain_scope,
            settlement.settled_at_us,
        ),
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
        "VALUES (?, ?, ?, ?, ?, ?, 'candidate', 'proposed', NULL, 'human_proposed', "
        "NULL, NULL, NULL, NULL, NULL, NULL, '1.0', ?, ?, ?, NULL, ?, ?, ?, ?, NULL, "
        "?, ?, ?, 1, 'm1_audit', ?, ?)",
        (
            workspace_id,
            assembly_id,
            record_id,
            version_id,
            claim.record_type,
            claim.domain_scope,
            content_json,
            _digest(content_json),
            claim.evidence_disposition,
            claim.assertion.actor_id,
            claim.assertion.actor_kind,
            claim.assertion.actor_role,
            reason,
            valid_from_us,
            valid_to_us,
            settlement.settled_at_us,
            settlement.audit_ref,
            settlement.audit_ref,
        ),
    )
    connection.execute(
        "INSERT INTO omnivia_governed_provenance_events "
        "(workspace_id, provenance_event_id, assembly_id, governed_record_version_id, "
        "provenance_sequence, action, actor_id, actor_kind, actor_role, policy_id, "
        "policy_version, occurred_at_us, recorded_at_us, reason_code, reason_comment, "
        "audit_ref, correlation_kind, correlation_id, predecessor_record_id, "
        "predecessor_version_id, evidence_disposition) "
        "VALUES (?, ?, ?, ?, 1, 'candidate.human_proposed', ?, ?, ?, NULL, NULL, ?, ?, "
        "?, NULL, ?, 'm1_audit', ?, NULL, NULL, ?)",
        (
            workspace_id,
            event_id,
            assembly_id,
            version_id,
            claim.assertion.actor_id,
            claim.assertion.actor_kind,
            claim.assertion.actor_role,
            asserted_at_us,
            settlement.settled_at_us,
            reason,
            settlement.audit_ref,
            settlement.audit_ref,
            claim.evidence_disposition,
        ),
    )
    for ordinal, evidence_id in enumerate(evidence_ids, 1):
        connection.execute(
            "INSERT INTO omnivia_governed_version_evidence_links "
            "(workspace_id, assembly_id, provenance_event_id, link_ordinal, evidence_id, "
            "normalized_record_id, normalized_span_id, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                workspace_id,
                assembly_id,
                event_id,
                ordinal,
                evidence_id,
                settlement.settled_at_us,
            ),
        )
    connection.execute(
        "INSERT INTO omnivia_governed_version_seals "
        "(workspace_id, seal_id, assembly_id, governed_record_version_id, "
        "correlation_kind, correlation_id, sealed_at_us) "
        "VALUES (?, ?, ?, ?, 'm1_audit', ?, ?)",
        (
            workspace_id,
            seal_id,
            assembly_id,
            version_id,
            settlement.audit_ref,
            settlement.settled_at_us,
        ),
    )
    if claim.domain_scope == _ENGINEERING_DOMAIN:
        # The bounded preview `engineering.search` serves is written with the
        # version, so a search never has to read this content to preview it.
        # Imported at use: engineering_preview reads this module's frontier.
        from omnivia_core_runtime.storage import engineering_preview

        engineering_preview.record_preview(
            connection, workspace_id=workspace_id, assembly_id=assembly_id
        )
    connection.execute(
        "INSERT INTO omnivia_application_claim_lineage "
        "(workspace_id, assembly_id, governed_record_version_id, operation, audit_ref, "
        "claim_json, claim_digest, claim_byte_length, claim_ingested_at_us, settled_at_us) "
        "VALUES (?, ?, ?, 'memory.create', ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            assembly_id,
            version_id,
            settlement.audit_ref,
            claim_json,
            _digest(claim_json),
            len(claim_json.encode("utf-8")),
            settlement.settled_at_us,
            settlement.settled_at_us,
        ),
    )
    if dependency_manifest is not None:
        # Same fenced transaction as the proposal: the dependency set exists exactly
        # when this version does. Its digests stay claims until the evaluator checks
        # them against the recorded baseline manifest.
        from omnivia_core_runtime.storage import engineering_source

        try:
            engineering_source.record_dependency_set(
                connection,
                settlement,
                workspace_id=workspace_id,
                record_id=record_id,
                version=version_id,
                manifest=dependency_manifest,
                allocate_identifier=allocate_identifier,
            )
        except engineering_source.DependencyBaselineUnavailable as error:
            raise OperationError(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE,
                "the dependency manifest's baseline source snapshot is not recorded",
                retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
            ) from error

    at = _timestamp(settlement.settled_at_us)
    temporal = RecordTemporalMetadata(
        event_at=claim.event_at,
        observed_at=claim.observed_at,
        ingested_at=at,
        recorded_at=at,
        valid_from=claim.assertion.proposed_valid_from,
        valid_until=claim.assertion.proposed_valid_until,
    )
    record = GovernedRecord(
        workspace_id=workspace_id,
        record_type=claim.record_type,
        domain_scope=claim.domain_scope,
        authority_level="proposed",
        reviewer=None,
        provenance=RecordProvenance(
            identity=RecordIdentity(
                record_id=record_id,
                version=version_id,
                layer="l1",
                governance_state="proposed",
                currentness="current",
            ),
            temporal=temporal,
            history=(),
            evidence_disposition=claim.evidence_disposition,
            sources=claim.sources,
            assertion=CandidateAssertion.from_wire(claim.assertion.to_wire()),
        ),
        content=claim.content,
    )
    result = MemoryCreateResult(record=record)
    validate_memory_create_result(result, workspace_id)
    return result.to_wire()


@contextmanager
def read_snapshot(connection: sqlite3.Connection) -> Iterator[None]:
    """One read snapshot: begin unless the caller already holds a transaction.

    Commits only a transaction this block began, and rolls it back on any failure, so
    a caller composing several reads inside its own transaction gets its own snapshot
    back, unended.
    """
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        yield
    except BaseException:
        if owns_transaction:
            connection.execute("ROLLBACK")
        raise
    if owns_transaction:
        connection.execute("COMMIT")


def read_authorized_memory_frontier(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
    view: str | None,
    label_grant: EvidenceLabelGrant,
    domain_scope: str | None = None,
    body_free: bool = True,
) -> AuthorizedMemoryFrontier:
    """Select identity and ACL facts only: the admitted versions, hydrating nothing.

    Every statement reads `omnivia_authoritative_governed_version_metadata`, the
    sealed versions without their body column, so nothing here can name a body. The
    evidence-label grant is evaluated here, from identities and evidence links, so a
    caller that reads anything about an admitted version afterwards reads it for a
    version the grant already admitted. `domain_scope`, when given, narrows
    the versions considered by a stored identity fact before any label is folded; a
    record never changes domain, so it never changes which versions are admitted
    within it.

    `body_free` selects that metadata view (migration 0053). The legacy memory family
    passes False to read 0009's full view instead, still selecting no body column, so
    it runs on schemas that predate 0053.
    """
    versions = (
        "omnivia_authoritative_governed_version_metadata"
        if body_free
        else "omnivia_authoritative_governed_versions"
    )
    resolved_view = resolve_governed_record_view(view)
    with read_snapshot(connection):
        domain_filter = "" if domain_scope is None else "AND domain_scope = ? "
        rows = connection.execute(
            "SELECT assembly_id, governed_record_id, governed_record_version_id, layer, "
            "governance_disposition, authority_level, valid_from_us, valid_to_us, "
            "recorded_at_us, append_ordinal, correlation_kind, correlation_id, "
            "record_type, domain_scope, content_digest, evidence_disposition "
            f"FROM {versions} "
            f"WHERE workspace_id = ? AND recorded_at_us <= ? {domain_filter}"
            "ORDER BY governed_record_id, recorded_at_us, append_ordinal, assembly_id",
            (
                workspace_id,
                resolution_instant_us,
                *(() if domain_scope is None else (domain_scope,)),
            ),
        ).fetchall()
        # This first phase may read only identities and the minimum currentness
        # facts required to select the view.  In particular, do not join the
        # provenance event that carries the public supersession reason until the
        # record's evidence grant has been evaluated below.
        supersessions = connection.execute(
            "SELECT r.governed_record_id, r.source_version_id, "
            "r.target_version_id, r.assembly_id, "
            "MAX(r.recorded_at_us, t.recorded_at_us) "
            "FROM omnivia_record_supersessions r "
            "JOIN omnivia_governed_version_seals s "
            "ON s.workspace_id = r.workspace_id AND s.assembly_id = r.assembly_id "
            f"JOIN {versions} t "
            "ON t.workspace_id = r.workspace_id AND t.assembly_id = r.assembly_id "
            "AND t.governed_record_version_id = r.target_version_id "
            "WHERE r.workspace_id = ? "
            "AND MAX(r.recorded_at_us, t.recorded_at_us) <= ? "
            "ORDER BY r.source_version_id, r.target_version_id, r.assembly_id",
            (workspace_id, resolution_instant_us),
        ).fetchall()
        replaced = {
            str(row[1]): int(row[4]) for row in supersessions
        }
        # Endpoint identity is needed both to remove transitioned candidates and
        # to include every supporting assembly in the evidence-label fold.  All
        # public transition material remains deferred until `authorized_ids` is
        # frozen.
        application_transition_endpoints = connection.execute(
            "SELECT governed_record_id, source_assembly_id, source_record_version_id, "
            "target_assembly_id, target_record_version_id "
            "FROM omnivia_application_governance_transitions "
            "WHERE workspace_id = ? AND settled_at_us <= ? "
            "ORDER BY governed_record_id, source_record_version_id, "
            "target_record_version_id, source_assembly_id, target_assembly_id",
            (workspace_id, resolution_instant_us),
        ).fetchall()
        application_replaced = {
            str(row[2]) for row in application_transition_endpoints
        }
        if resolved_view == "candidates":
            selected = [
                row
                for row in rows
                if str(row[3]) == "candidate"
                and str(row[2]) not in application_replaced
            ]
        else:
            canonical = [
                row
                for row in rows
                if str(row[3]) == "governed"
                and str(row[4]) == "accepted"
                and str(row[5]) == "canonical"
            ]
            if resolved_view == "history":
                selected = [
                    row
                    for row in canonical
                    if replaced.get(str(row[2]), resolution_instant_us + 1)
                    <= resolution_instant_us
                ]
            else:
                frontier: dict[str, tuple[object, ...]] = {}
                for row in canonical:
                    version_id = str(row[2])
                    if (
                        replaced.get(version_id, resolution_instant_us + 1)
                        <= resolution_instant_us
                    ):
                        continue
                    valid_to = None if row[7] is None else int(row[7])
                    if not (
                        int(row[6]) <= resolution_instant_us
                        and (valid_to is None or resolution_instant_us < valid_to)
                    ):
                        continue
                    key = (
                        int(row[8]),
                        str(row[10]),
                        str(row[11]),
                        int(row[9]),
                        str(row[0]),
                    )
                    held = frontier.get(str(row[1]))
                    held_key = (
                        None
                        if held is None
                        else (
                            cast("int", held[8]),
                            str(held[10]),
                            str(held[11]),
                            cast("int", held[9]),
                            str(held[0]),
                        )
                    )
                    if held_key is None or key > held_key:
                        frontier[str(row[1])] = row
                selected = [frontier[key] for key in sorted(frontier)]

        assembly_ids = tuple(str(row[0]) for row in selected)
        record_by_assembly = {str(row[0]): str(row[1]) for row in selected}
        support_by_record: dict[str, set[str]] = {
            record_id: set() for record_id in record_by_assembly.values()
        }
        for transition in application_transition_endpoints:
            (
                record_id,
                source_assembly,
                _source_version,
                target_assembly,
                _target_version,
            ) = transition[:5]
            if str(record_id) in support_by_record:
                support_by_record[str(record_id)].update(
                    {str(source_assembly), str(target_assembly)}
                )
        for assembly_id, record_id in record_by_assembly.items():
            support_by_record[record_id].add(assembly_id)
        support_ids = tuple(
            sorted({item for values in support_by_record.values() for item in values})
        )
        evidence_rows: list[tuple[object, ...]] = []
        label_rows: list[tuple[object, ...]] = []
        if support_ids:
            evidence_rows = _execute_in_rows(
                connection,
                select="SELECT assembly_id, evidence_id "
                "FROM omnivia_governed_version_evidence_links",
                pre="workspace_id = ?",
                in_column="assembly_id",
                leading=(workspace_id,),
                ids=support_ids,
                order_key=lambda row: (str(row[0]), str(row[1])),
            )
            evidence_ids = tuple(sorted({str(row[1]) for row in evidence_rows}))
            if evidence_ids:
                label_rows = _execute_in_rows(
                    connection,
                    select="SELECT evidence_id, label_sequence, label_action, permission_label "
                    "FROM omnivia_evidence_permission_labels",
                    pre="workspace_id = ?",
                    in_column="evidence_id",
                    leading=(workspace_id,),
                    ids=evidence_ids,
                    order_key=lambda row: (str(row[0]), cast("int", row[1])),
                )
        labels_by_evidence: dict[str, list[tuple[object, ...]]] = {}
        for evidence_id, sequence, action, label in label_rows:
            labels_by_evidence.setdefault(str(evidence_id), []).append(
                (sequence, action, label)
            )
        evidence_by_assembly: dict[str, list[str]] = {}
        for evidence_assembly_id, evidence_id in evidence_rows:
            evidence_by_assembly.setdefault(str(evidence_assembly_id), []).append(
                str(evidence_id)
            )
        authorized_ids = tuple(
            assembly_id
            for assembly_id in assembly_ids
            if all(
                label_grant.permits(
                    _fold_labels(labels_by_evidence.get(evidence_id, []))
                )
                for support_id in support_by_record[record_by_assembly[assembly_id]]
                for evidence_id in evidence_by_assembly.get(support_id, [])
            )
        )
        authorized_support_ids = tuple(
            sorted(
                {
                    support_id
                    for assembly_id in authorized_ids
                    for support_id in support_by_record[record_by_assembly[assembly_id]]
                }
            )
        )
        authorized_record_ids = tuple(
            sorted(
                {
                    record_by_assembly[assembly_id]
                    for assembly_id in authorized_ids
                }
            )
        )
        application_transitions: list[tuple[object, ...]] = []
        if authorized_record_ids:
            application_transitions = _execute_in_rows(
                connection,
                select="SELECT governed_record_id, source_assembly_id, "
                "source_record_version_id, target_assembly_id, "
                "target_record_version_id, transition_id, operation, "
                "rationale_digest, rationale_byte_length, reason_code, "
                "reason_comment, actor_id, actor_kind, audit_ref, settled_at_us "
                "FROM omnivia_application_governance_transitions",
                pre="workspace_id = ?",
                in_column="governed_record_id",
                post="AND settled_at_us <= ?",
                leading=(workspace_id,),
                ids=authorized_record_ids,
                trailing=(resolution_instant_us,),
                order_key=lambda row: (
                    str(row[0]),
                    cast("int", row[14]),
                    str(row[5]),
                ),
            )
        authorized_id_set = set(authorized_ids)
        authorized_support_id_set = set(authorized_support_ids)
        permitted_evidence_ids = {
            str(row[1])
            for row in evidence_rows
            if str(row[0]) in authorized_support_id_set
        }
        authorized_record_id_set = set(authorized_record_ids)
        digest_document = to_canonical_json(
            {
                "view_policy": "memory-s2-v1",
                "view": resolved_view,
                "resolution_instant_us": resolution_instant_us,
                "frontier": [
                    [str(row[0]), str(row[1]), str(row[2]), int(row[8])]
                    for row in selected
                    if str(row[0]) in authorized_id_set
                ],
                "evidence": [
                    list(map(str, row))
                    for row in evidence_rows
                    if str(row[0]) in authorized_support_id_set
                ],
                "label_stream": [
                    [str(item) for item in row]
                    for row in label_rows
                    if str(row[0]) in permitted_evidence_ids
                ],
                "transition_chain": [
                    [None if item is None else str(item) for item in row]
                    for row in application_transitions
                    if str(row[0]) in authorized_record_id_set
                ],
                "grant": {
                    "principal_id": label_grant.principal_id,
                    "workspace_id": label_grant.workspace_id,
                    "all_labels": label_grant.all_labels,
                    "labels": sorted(label_grant.labels),
                },
            }
        )
        selected_by_assembly = {str(row[0]): row for row in selected}

        def admitted(assembly_id: str) -> AuthorizedVersion:
            row = selected_by_assembly[assembly_id]
            return AuthorizedVersion(
                assembly_id=assembly_id,
                record_id=str(row[1]),
                version_id=str(row[2]),
                record_type=str(row[12]),
                domain_scope=str(row[13]),
                layer=str(row[3]),
                governance_disposition=None if row[4] is None else str(row[4]),
                evidence_disposition=str(row[15]),
                content_digest=str(row[14]),
                recorded_at_us=int(row[8]),
                has_evidence=bool(evidence_by_assembly.get(assembly_id)),
            )

        return AuthorizedMemoryFrontier(
            resolution_instant_us=resolution_instant_us,
            view=resolved_view,
            versions=tuple(admitted(assembly_id) for assembly_id in authorized_ids),
            support_assembly_ids=authorized_support_ids,
            digest=_digest(digest_document),
        )


def engineering_observation_payload_bytes(content: Mapping[str, Any]) -> int:
    """The exact canonical UTF-8 byte length of one hydrated observation's body.

    The same canonicalisation `_validate_engineering_observation_content` bounds
    at write time, read back at hydration time, so a caller counting bytes it
    actually read reports the same number the write path already enforced --
    never a fabricated or re-estimated one.
    """
    return len(to_canonical_json(_plain_content(dict(content))).encode("utf-8"))


def read_authorized_memory_snapshot(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
    view: str | None,
    label_grant: EvidenceLabelGrant,
) -> AuthorizedMemorySnapshot:
    """Select identity and ACL facts first, then hydrate only admitted assemblies."""
    with read_snapshot(connection):
        frontier = read_authorized_memory_frontier(
            connection,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
            view=view,
            label_grant=label_grant,
            body_free=False,
        )
        values = hydrate_authorized_governed_record_values(
            connection,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
            assembly_ids=tuple(version.assembly_id for version in frontier.versions),
            support_assembly_ids=frontier.support_assembly_ids,
        )
    return AuthorizedMemorySnapshot(
        resolution_instant_us=resolution_instant_us,
        view=frontier.view,
        values=values,
        digest=frontier.digest,
    )


__all__ = [
    "AuthorizedMemoryFrontier",
    "AuthorizedMemorySnapshot",
    "AuthorizedVersion",
    "IdentifierAllocator",
    "create_memory_record",
    "random_identifier",
    "read_authorized_memory_frontier",
    "read_authorized_memory_snapshot",
    "read_snapshot",
    "resolve_memory_claim_evidence",
]
