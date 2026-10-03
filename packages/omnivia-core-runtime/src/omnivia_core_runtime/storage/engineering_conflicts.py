"""Durable deterministic engineering conflict discovery.

The enqueue path records only exact identities and digests inside the application
mutation that sealed the anchor. The processor advances an indexed stable-record
cursor at the run's resolution instant, commits an immutable accumulator after each
bounded page, and resumes from that watermark after restart. The 8/32 result budget
limits retained matches only; the separate page budget limits scan work. A terminal
``scan_complete_for_snapshot`` event is recorded only after the index is exhausted.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.service.mutation import MutationSettlementContext
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.engineering_preview import (
    PreviewCandidate,
    preview_search_text,
    read_authorized_previews,
)
from omnivia_core_runtime.storage.memory import (
    read_authorized_memory_frontier,
    read_snapshot,
)
from omnivia_core_runtime.storage.retrieval import EvidenceLabelGrant

IdentifierAllocator = Callable[[str], str]

DETECTOR_VERSION: Final = "engineering.conflict.discovery.v1"
DEFAULT_CANDIDATE_BUDGET: Final = 8
MAX_CANDIDATE_BUDGET: Final = 32
DEFAULT_SCAN_RECORD_BUDGET: Final = 128
MAX_SCAN_RECORD_BUDGET: Final = 512
MAX_CONTEXT_CONFLICT_ELIGIBLE_ENDPOINTS: Final = 10_000
MAX_CONTEXT_CONFLICT_ENDPOINTS: Final = 64
MAX_CONTEXT_CONFLICT_EDGES: Final = 512
MAX_CONTEXT_RELATION_ROWS_PER_BATCH: Final = 512
MAX_CONTEXT_RELATED_ENDPOINTS: Final = 10_000

_ELIGIBLE_RECORD_TYPES: Final = (
    "knowledge.finding",
    "knowledge.risk",
    "knowledge.decision",
)
_ELIGIBLE_OPERATIONS: Final = (
    "memory.create",
    "knowledge.propose",
    "candidate.approve",
    "record.supersede",
)
_DOMAIN: Final = "engineering.codebase"
_RELATION_READ_BATCH: Final = 400
_CONTEXT_RELATION_READ_BATCH: Final = (
    MAX_CONTEXT_RELATION_ROWS_PER_BATCH // MAX_CANDIDATE_BUDGET
)
_DISCOVERY_VIEWS: Final[tuple[str | None, ...]] = (
    "candidates",
    None,
    "history",
)
_LEXICAL_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "and",
        "decision",
        "finding",
        "knowledge",
        "record",
        "risk",
        "summary",
        "that",
        "the",
        "this",
        "title",
        "with",
    }
)


@dataclass(frozen=True, slots=True)
class DiscoveryRun:
    discovery_run_id: str
    anchor_assembly_id: str
    anchor_record_id: str
    anchor_version: str
    anchor_content_digest: str
    principal_id: str
    detector_version: str
    candidate_budget: int
    resolution_instant_us: int
    enqueued_at_us: int
    audit_ref: str


@dataclass(frozen=True, slots=True)
class RelationEndpoint:
    assembly_id: str
    record_id: str
    version: str
    content_digest: str


@dataclass(frozen=True, slots=True)
class RelationCandidate:
    relation_candidate_id: str
    endpoint_a: RelationEndpoint
    endpoint_b: RelationEndpoint
    detector_version: str
    scope_classification: str
    proposed_relation: str
    status: str
    first_discovery_run_id: str
    recorded_at_us: int


@dataclass(frozen=True, slots=True)
class ConflictComponent:
    """One connected group of exact visible endpoints requiring a warning."""

    endpoints: tuple[RelationEndpoint, ...]
    status: str = "unresolved"


@dataclass(frozen=True, slots=True)
class ConflictReadResult:
    components: tuple[ConflictComponent, ...]
    withheld_endpoint_ids: frozenset[str]


class ContextConflictLimitExceeded(StorageError):
    """The authorized context conflict graph exceeds its declared read bound."""


@dataclass(frozen=True, slots=True)
class DiscoveryProcessingResult:
    """The durable outcome of one bounded, resumable processor pass."""

    run: DiscoveryRun
    frontier_digest: str
    authorized_frontier_size: int
    structural_considered: int
    lexical_considered: int
    candidates: tuple[RelationCandidate, ...]
    coverage: str
    scan_complete: bool
    last_processed_record_id: str


DependencyKey = tuple[str, str, str, str, str | None]


@dataclass(frozen=True, slots=True)
class _RankedMatch:
    endpoint: RelationEndpoint
    recorded_at_us: int
    score: int
    channel: str
    basis: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _ScanProgress:
    batch_sequence: int
    cursor_record_id: str
    frontier_digest: str
    authorized_frontier_size: int
    structural_considered: int
    lexical_considered: int
    structural: tuple[_RankedMatch, ...]
    lexical: tuple[_RankedMatch, ...]


def _candidate_budget(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("candidate_budget must be an integer")
    if not 1 <= value <= MAX_CANDIDATE_BUDGET:
        raise ValueError(
            f"candidate_budget must be between 1 and {MAX_CANDIDATE_BUDGET}"
        )
    return value


def _run(row: sqlite3.Row | tuple[object, ...]) -> DiscoveryRun:
    return DiscoveryRun(
        discovery_run_id=str(row[0]),
        anchor_assembly_id=str(row[1]),
        anchor_record_id=str(row[2]),
        anchor_version=str(row[3]),
        anchor_content_digest=str(row[4]),
        principal_id=str(row[5]),
        detector_version=str(row[6]),
        candidate_budget=int(str(row[7])),
        resolution_instant_us=int(str(row[8])),
        enqueued_at_us=int(str(row[9])),
        audit_ref=str(row[10]),
    )


_RUN_COLUMNS: Final = (
    "discovery_run_id, anchor_assembly_id, anchor_record_id, anchor_version, "
    "anchor_content_digest, principal_id, detector_version, candidate_budget, "
    "resolution_instant_us, enqueued_at_us, audit_ref"
)


def enqueue_discovery(
    connection: sqlite3.Connection,
    settlement: MutationSettlementContext,
    *,
    workspace_id: str,
    assembly_id: str,
    allocate_identifier: IdentifierAllocator,
    candidate_budget: int = DEFAULT_CANDIDATE_BUDGET,
) -> DiscoveryRun:
    """Append one queued run for a newly sealed exact engineering version.

    The anchor, principal, operation, digest and instant are derived from immutable
    rows already written in the caller's settlement. No body or preview text is read.
    An accidental second call returns the existing run; ordinary idempotent request
    replay never enters the domain mutation and therefore reaches neither branch.
    """

    budget = _candidate_budget(candidate_budget)
    existing = connection.execute(
        f"SELECT {_RUN_COLUMNS} FROM omnivia_engineering_discovery_runs "
        "WHERE workspace_id = ? AND anchor_assembly_id = ? AND detector_version = ?",
        (workspace_id, assembly_id, DETECTOR_VERSION),
    ).fetchone()
    if existing is not None:
        return _run(existing)

    placeholders = ", ".join("?" for _ in _ELIGIBLE_RECORD_TYPES)
    operation_placeholders = ", ".join("?" for _ in _ELIGIBLE_OPERATIONS)
    anchor = connection.execute(
        "SELECT a.governed_record_id, a.governed_record_version_id, "
        "a.content_digest, e.principal_id "
        "FROM omnivia_governed_version_assemblies a "
        "JOIN omnivia_governed_version_seals s "
        "ON s.workspace_id = a.workspace_id AND s.assembly_id = a.assembly_id "
        "AND s.governed_record_version_id = a.governed_record_version_id "
        "JOIN omnivia_engineering_preview_projection p "
        "ON p.workspace_id = a.workspace_id AND p.assembly_id = a.assembly_id "
        "AND p.projection_version = 1 AND p.content_digest = a.content_digest "
        "JOIN omnivia_application_claim_lineage l "
        "ON l.workspace_id = a.workspace_id AND l.assembly_id = a.assembly_id "
        "AND l.governed_record_version_id = a.governed_record_version_id "
        "JOIN omnivia_application_audit_events e "
        "ON e.audit_ref = l.audit_ref AND e.workspace_id = l.workspace_id "
        "WHERE a.workspace_id = ? AND a.assembly_id = ? "
        f"AND a.record_type IN ({placeholders}) AND a.domain_scope = ? "
        f"AND l.operation IN ({operation_placeholders}) "
        "AND l.audit_ref = ? AND l.settled_at_us = ? "
        "AND e.operation = l.operation AND e.recorded_at_us = ? "
        "AND s.sealed_at_us = ?",
        (
            workspace_id,
            assembly_id,
            *_ELIGIBLE_RECORD_TYPES,
            _DOMAIN,
            *_ELIGIBLE_OPERATIONS,
            settlement.audit_ref,
            settlement.settled_at_us,
            settlement.settled_at_us,
            settlement.settled_at_us,
        ),
    ).fetchone()
    if anchor is None:
        raise StorageError(
            "conflict discovery requires a sealed eligible engineering observation"
        )

    discovery_run_id = allocate_identifier("edr")
    connection.execute(
        "INSERT INTO omnivia_engineering_discovery_runs "
        "(workspace_id, discovery_run_id, anchor_assembly_id, anchor_record_id, "
        "anchor_version, anchor_content_digest, principal_id, detector_version, "
        "candidate_budget, resolution_instant_us, enqueued_at_us, audit_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            discovery_run_id,
            assembly_id,
            str(anchor[0]),
            str(anchor[1]),
            str(anchor[2]),
            str(anchor[3]),
            DETECTOR_VERSION,
            budget,
            settlement.settled_at_us,
            settlement.settled_at_us,
            settlement.audit_ref,
        ),
    )
    connection.execute(
        "INSERT INTO omnivia_engineering_discovery_run_events "
        "(workspace_id, discovery_run_id, event_sequence, event_id, state, coverage, "
        "frontier_digest, authorized_frontier_size, structural_considered, "
        "lexical_considered, selected_count, failure_code, occurred_at_us) "
        "VALUES (?, ?, 1, ?, 'queued', NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?)",
        (
            workspace_id,
            discovery_run_id,
            allocate_identifier("ede"),
            settlement.settled_at_us,
        ),
    )
    return DiscoveryRun(
        discovery_run_id=discovery_run_id,
        anchor_assembly_id=assembly_id,
        anchor_record_id=str(anchor[0]),
        anchor_version=str(anchor[1]),
        anchor_content_digest=str(anchor[2]),
        principal_id=str(anchor[3]),
        detector_version=DETECTOR_VERSION,
        candidate_budget=budget,
        resolution_instant_us=settlement.settled_at_us,
        enqueued_at_us=settlement.settled_at_us,
        audit_ref=settlement.audit_ref,
    )


def read_oldest_queued_run(
    connection: sqlite3.Connection, *, workspace_id: str
) -> DiscoveryRun | None:
    """Return the oldest run with a queued event and no terminal event."""

    row = connection.execute(
        f"SELECT {', '.join('r.' + column.strip() for column in _RUN_COLUMNS.split(','))} "
        "FROM omnivia_engineering_discovery_runs r "
        "JOIN omnivia_engineering_discovery_run_events q "
        "ON q.workspace_id = r.workspace_id AND q.discovery_run_id = r.discovery_run_id "
        "AND q.event_sequence = 1 AND q.state = 'queued' "
        "WHERE r.workspace_id = ? AND NOT EXISTS ("
        "SELECT 1 FROM omnivia_engineering_discovery_run_events terminal "
        "WHERE terminal.workspace_id = r.workspace_id "
        "AND terminal.discovery_run_id = r.discovery_run_id "
        "AND terminal.event_sequence = 2) "
        "ORDER BY r.enqueued_at_us, r.discovery_run_id LIMIT 1",
        (workspace_id,),
    ).fetchone()
    return None if row is None else _run(row)


def _endpoint(
    connection: sqlite3.Connection, *, workspace_id: str, assembly_id: str
) -> RelationEndpoint:
    placeholders = ", ".join("?" for _ in _ELIGIBLE_RECORD_TYPES)
    row = connection.execute(
        "SELECT a.assembly_id, a.governed_record_id, "
        "a.governed_record_version_id, a.content_digest "
        "FROM omnivia_authoritative_governed_version_metadata a "
        "JOIN omnivia_engineering_preview_projection p "
        "ON p.workspace_id = a.workspace_id AND p.assembly_id = a.assembly_id "
        "AND p.projection_version = 1 AND p.content_digest = a.content_digest "
        "WHERE a.workspace_id = ? AND a.assembly_id = ? "
        f"AND a.record_type IN ({placeholders}) AND a.domain_scope = ?",
        (workspace_id, assembly_id, *_ELIGIBLE_RECORD_TYPES, _DOMAIN),
    ).fetchone()
    if row is None:
        raise StorageError("a relation endpoint is not an exact engineering projection")
    return RelationEndpoint(*(str(value) for value in row))


def _authorized_endpoint_pair(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run: DiscoveryRun,
    other_assembly_id: str,
    label_grant: EvidenceLabelGrant,
) -> tuple[RelationEndpoint, RelationEndpoint]:
    """Resolve a pair only after both assemblies enter the run's frozen grant frontier."""

    if (
        label_grant.workspace_id != workspace_id
        or label_grant.principal_id != run.principal_id
    ):
        raise StorageError("the discovery grant does not match the queued run")

    with read_snapshot(connection):
        admitted: set[str] = set()
        for view in _DISCOVERY_VIEWS:
            admitted.update(
                candidate.assembly_id
                for candidate in read_authorized_previews(
                    connection,
                    workspace_id=workspace_id,
                    resolution_instant_us=run.resolution_instant_us,
                    view=view,
                    label_grant=label_grant,
                )[0]
            )
        if run.anchor_assembly_id not in admitted or other_assembly_id not in admitted:
            # The same refusal covers absent, rejected, stale and ACL-denied exact
            # versions. No authoritative endpoint row is touched before this check.
            raise StorageError("a relation endpoint is not in the authorized frontier")
        return (
            _endpoint(
                connection,
                workspace_id=workspace_id,
                assembly_id=run.anchor_assembly_id,
            ),
            _endpoint(
                connection,
                workspace_id=workspace_id,
                assembly_id=other_assembly_id,
            ),
        )


def _append_relation_candidate(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run: DiscoveryRun,
    other_assembly_id: str,
    label_grant: EvidenceLabelGrant,
    allocate_identifier: IdentifierAllocator,
) -> RelationCandidate:
    """Append or retrieve one symmetric unresolved candidate for ``run``.

    Both exact endpoints must first enter the frozen preview frontier admitted by the
    run principal's explicit label grant. The persistence helper classifies the pair
    as ``scoped_difference`` only on immutable checkout proof (migration 0062) and
    otherwise keeps ``unresolved_overlap``.
    """

    anchor, other = _authorized_endpoint_pair(
        connection,
        workspace_id=workspace_id,
        run=run,
        other_assembly_id=other_assembly_id,
        label_grant=label_grant,
    )
    return _append_authorized_relation_candidate(
        connection,
        workspace_id=workspace_id,
        run=run,
        anchor=anchor,
        other=other,
        allocate_identifier=allocate_identifier,
    )


def _classify_scope(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    endpoint_a: RelationEndpoint,
    endpoint_b: RelationEndpoint,
) -> tuple[str, str]:
    """Return ``scoped_difference`` only when both endpoints' stored applicability
    snapshots have immutable checkout proof for one repository and distinct checkouts.

    Unequal labels, stream ids or model-supplied identifiers are never read; absent,
    incomplete or disagreeing capture evidence keeps ``unresolved_overlap``. The
    insert guard re-derives the same proof, so this is a hint, not the authority.
    """

    proven = connection.execute(
        "SELECT 1 "
        "FROM omnivia_engineering_preview_projection pa "
        "JOIN omnivia_engineering_snapshot_checkout_proofs xa "
        "ON xa.workspace_id = pa.workspace_id AND xa.snapshot_id = pa.snapshot_id "
        "AND xa.repository_id = pa.repository_id "
        "JOIN omnivia_engineering_preview_projection pb "
        "ON pb.workspace_id = pa.workspace_id "
        "JOIN omnivia_engineering_snapshot_checkout_proofs xb "
        "ON xb.workspace_id = pb.workspace_id AND xb.snapshot_id = pb.snapshot_id "
        "AND xb.repository_id = pb.repository_id "
        "WHERE pa.workspace_id = ? AND pa.assembly_id = ? "
        "AND pa.projection_version = 1 AND pa.content_digest = ? "
        "AND pb.assembly_id = ? AND pb.projection_version = 1 "
        "AND pb.content_digest = ? "
        "AND xa.repository_id = xb.repository_id AND xa.checkout_id <> xb.checkout_id "
        "LIMIT 1",
        (
            workspace_id,
            endpoint_a.assembly_id,
            endpoint_a.content_digest,
            endpoint_b.assembly_id,
            endpoint_b.content_digest,
        ),
    ).fetchone()
    if proven is None:
        return "unresolved_overlap", "related"
    return "scoped_difference", "scoped_difference"


def _append_authorized_relation_candidate(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run: DiscoveryRun,
    anchor: RelationEndpoint,
    other: RelationEndpoint,
    allocate_identifier: IdentifierAllocator,
) -> RelationCandidate:
    """Append a pair already admitted by this processor's frozen frontier.

    The caller must derive both endpoints from ``read_authorized_previews`` in the
    same fenced transaction. Keeping this small persistence helper separate avoids
    re-reading the whole authorised frontier once for every selected result.
    """

    if anchor.assembly_id != run.anchor_assembly_id:
        raise StorageError("the relation anchor does not match the discovery run")
    if anchor.content_digest != run.anchor_content_digest:
        raise StorageError("the relation anchor digest does not match the discovery run")
    if anchor.record_id == other.record_id:
        raise StorageError("versions of one stable record are not discovery candidates")
    endpoint_a, endpoint_b = sorted(
        (anchor, other), key=lambda endpoint: (endpoint.record_id, endpoint.version)
    )
    row = connection.execute(
        "SELECT relation_candidate_id, scope_classification, proposed_relation, status, "
        "first_discovery_run_id, recorded_at_us "
        "FROM omnivia_engineering_relation_candidates "
        "WHERE workspace_id = ? AND endpoint_a_record_id = ? "
        "AND endpoint_a_version = ? AND endpoint_b_record_id = ? "
        "AND endpoint_b_version = ? AND detector_version = ?",
        (
            workspace_id,
            endpoint_a.record_id,
            endpoint_a.version,
            endpoint_b.record_id,
            endpoint_b.version,
            run.detector_version,
        ),
    ).fetchone()
    if row is None:
        candidate_id = allocate_identifier("erc")
        scope, relation = _classify_scope(
            connection,
            workspace_id=workspace_id,
            endpoint_a=endpoint_a,
            endpoint_b=endpoint_b,
        )
        connection.execute(
            "INSERT INTO omnivia_engineering_relation_candidates "
            "(workspace_id, relation_candidate_id, endpoint_a_assembly_id, "
            "endpoint_a_record_id, endpoint_a_version, endpoint_a_digest, "
            "endpoint_b_assembly_id, endpoint_b_record_id, endpoint_b_version, "
            "endpoint_b_digest, detector_version, scope_classification, "
            "proposed_relation, status, first_discovery_run_id, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                workspace_id,
                candidate_id,
                endpoint_a.assembly_id,
                endpoint_a.record_id,
                endpoint_a.version,
                endpoint_a.content_digest,
                endpoint_b.assembly_id,
                endpoint_b.record_id,
                endpoint_b.version,
                endpoint_b.content_digest,
                run.detector_version,
                scope,
                relation,
                run.discovery_run_id,
                run.resolution_instant_us,
            ),
        )
        row = (
            candidate_id,
            scope,
            relation,
            "pending",
            run.discovery_run_id,
            run.resolution_instant_us,
        )
    return RelationCandidate(
        relation_candidate_id=str(row[0]),
        endpoint_a=endpoint_a,
        endpoint_b=endpoint_b,
        detector_version=run.detector_version,
        scope_classification=str(row[1]),
        proposed_relation=str(row[2]),
        status=str(row[3]),
        first_discovery_run_id=str(row[4]),
        recorded_at_us=int(str(row[5])),
    )


def _append_candidate_observation(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run: DiscoveryRun,
    candidate: RelationCandidate,
    selected_order: int,
    channel: str,
    score: int,
    basis: Mapping[str, object],
    recorded_at_us: int,
) -> None:
    basis_json = to_canonical_json(dict(basis))
    connection.execute(
        "INSERT INTO omnivia_engineering_discovery_candidate_observations "
        "(workspace_id, discovery_run_id, relation_candidate_id, selected_order, "
        "channel, score, basis_json, recorded_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            run.discovery_run_id,
            candidate.relation_candidate_id,
            selected_order,
            channel,
            score,
            basis_json,
            recorded_at_us,
        ),
    )


def _append_terminal_event(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run: DiscoveryRun,
    state: str,
    coverage: str,
    frontier_digest: str | None,
    authorized_frontier_size: int,
    structural_considered: int,
    lexical_considered: int,
    selected_count: int,
    failure_code: str | None,
    occurred_at_us: int,
    allocate_identifier: IdentifierAllocator,
) -> None:
    connection.execute(
        "INSERT INTO omnivia_engineering_discovery_run_events "
        "(workspace_id, discovery_run_id, event_sequence, event_id, state, coverage, "
        "frontier_digest, authorized_frontier_size, structural_considered, "
        "lexical_considered, selected_count, failure_code, occurred_at_us) "
        "VALUES (?, ?, 2, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            run.discovery_run_id,
            allocate_identifier("ede"),
            state,
            coverage,
            frontier_digest,
            authorized_frontier_size,
            structural_considered,
            lexical_considered,
            selected_count,
            failure_code,
            occurred_at_us,
        ),
    )


def _authorized_preview_frontier(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run: DiscoveryRun,
    label_grant: EvidenceLabelGrant,
    record_ids: tuple[str, ...],
) -> tuple[PreviewCandidate, ...]:
    """Read one bounded stable-record page of the run principal's previews."""

    if (
        label_grant.workspace_id != workspace_id
        or label_grant.principal_id != run.principal_id
    ):
        raise StorageError("the discovery grant does not match the queued run")
    visible: dict[str, PreviewCandidate] = {}
    for view in _DISCOVERY_VIEWS:
        eligible = {
            version.assembly_id
            for version in read_authorized_memory_frontier(
                connection,
                workspace_id=workspace_id,
                resolution_instant_us=run.resolution_instant_us,
                view=view,
                label_grant=label_grant,
                domain_scope=_DOMAIN,
                record_ids=record_ids,
            ).versions
            if version.record_type in _ELIGIBLE_RECORD_TYPES
        }
        for candidate in read_authorized_previews(
            connection,
            workspace_id=workspace_id,
            resolution_instant_us=run.resolution_instant_us,
            view=view,
            label_grant=label_grant,
            record_ids=record_ids,
        )[0]:
            if candidate.assembly_id not in eligible:
                continue
            prior = visible.setdefault(candidate.assembly_id, candidate)
            if prior != candidate:
                raise StorageError("an exact preview changed within one discovery snapshot")
    return tuple(
        sorted(
            visible.values(),
            key=lambda candidate: (
                candidate.record_id,
                candidate.version,
                candidate.assembly_id,
            ),
        )
    )


def _dependency_keys(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    candidate: PreviewCandidate,
) -> tuple[DependencyKey, ...]:
    """Read one authorised exact version's sealed dependency metadata."""

    sealed = connection.execute(
        "SELECT dependency_count, audit_ref "
        "FROM omnivia_engineering_dependency_sets "
        "WHERE workspace_id = ? AND record_id = ? AND version = ?",
        (workspace_id, candidate.record_id, candidate.version),
    ).fetchone()
    if sealed is None:
        return ()
    count = int(sealed[0])
    rows = connection.execute(
        "SELECT selector_type, selector, meaning, producer, expected_digest, audit_ref "
        "FROM omnivia_engineering_dependencies "
        "INDEXED BY omnivia_idx_engineering_dependencies_version "
        "WHERE workspace_id = ? AND record_id = ? AND version = ? "
        "ORDER BY selector_type, selector, meaning, producer, expected_digest LIMIT ?",
        (workspace_id, candidate.record_id, candidate.version, count + 1),
    ).fetchall()
    if len(rows) != count or any(str(row[5]) != str(sealed[1]) for row in rows):
        raise StorageError("an engineering dependency set is not sealed consistently")
    return tuple(
        (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            None if row[4] is None else str(row[4]),
        )
        for row in rows
    )


def _frontier_digest(
    previews: tuple[PreviewCandidate, ...],
    dependencies: Mapping[str, tuple[DependencyKey, ...]],
    *,
    previous_digest: str | None,
) -> str:
    """Extend the deterministic authorized-input digest item by item.

    Page boundaries are deliberately absent from the preimage. Empty authorized
    pages leave the chain unchanged, so retrying with another record-page budget
    produces the same final frontier identity.
    """

    manifest = [
        {
            "assembly_id": candidate.assembly_id,
            "record_id": candidate.record_id,
            "version": candidate.version,
            "content_digest": candidate.content_digest,
            "recorded_at_us": candidate.recorded_at_us,
            "governance_state": candidate.governance_state,
            "evidence_disposition": candidate.evidence_disposition,
            "evidence_available": candidate.evidence_available,
            "title": candidate.title,
            "preview": candidate.preview,
            "truncated": candidate.truncated,
            "observation_kind": candidate.observation_kind,
            "assertion_basis": candidate.assertion_basis,
            "topic_key": candidate.topic_key,
            "repository_id": candidate.repository_id,
            "snapshot_id": candidate.snapshot_id,
            "dependencies": [list(item) for item in dependencies[candidate.assembly_id]],
        }
        for candidate in previews
    ]
    digest = previous_digest
    if digest is None:
        seed = to_canonical_json(
            {"detector_version": DETECTOR_VERSION, "frontier": "authorized_previews"}
        ).encode("utf-8")
        digest = f"sha256:{hashlib.sha256(seed).hexdigest()}"
    for item in manifest:
        encoded = to_canonical_json(
            {"previous_digest": digest, "candidate": item}
        ).encode("utf-8")
        digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    return digest


def _structural_match(
    anchor: PreviewCandidate,
    candidate: PreviewCandidate,
    *,
    anchor_dependencies: frozenset[DependencyKey],
    candidate_dependencies: frozenset[DependencyKey],
) -> tuple[int, dict[str, object]] | None:
    """Score exact structural overlap without storing paths or source text."""

    same_topic = bool(
        anchor.topic_key
        and candidate.topic_key
        and anchor.topic_key == candidate.topic_key
    )
    shared_dependencies = anchor_dependencies & candidate_dependencies
    same_assertion = bool(
        anchor.assertion_basis
        and candidate.assertion_basis
        and anchor.assertion_basis == candidate.assertion_basis
        and anchor.observation_kind
        and candidate.observation_kind
        and anchor.observation_kind == candidate.observation_kind
    )
    same_repository = bool(
        anchor.repository_id
        and candidate.repository_id
        and anchor.repository_id == candidate.repository_id
    )
    same_snapshot = bool(
        anchor.snapshot_id
        and candidate.snapshot_id
        and anchor.snapshot_id == candidate.snapshot_id
    )
    # Repository and snapshot equality scope a real structural overlap. They are
    # intentionally insufficient alone: otherwise one busy checkout would consume
    # every result slot with unrelated observations.
    if not (same_topic or shared_dependencies):
        return None
    basis: dict[str, object] = {
        "same_topic": same_topic,
        "shared_dependency_count": len(shared_dependencies),
        "same_assertion": same_assertion,
        "same_repository": same_repository,
        "same_snapshot": same_snapshot,
    }
    score = (
        (8 if same_topic else 0)
        + min(len(shared_dependencies), 64) * 4
        + (2 if same_assertion else 0)
        + (2 if same_repository else 0)
        + (1 if same_snapshot else 0)
    )
    return score, basis


def _lexical_tokens(candidate: PreviewCandidate) -> frozenset[str]:
    """Distinct case-folded preview words of three or more code points."""

    return frozenset(
        token
        for token in re.findall(r"\w+", preview_search_text(candidate))
        if len(token) >= 3 and token not in _LEXICAL_STOPWORDS
    )


def _rank_key(
    value: _RankedMatch,
) -> tuple[int, int, str, str, str]:
    return (
        -value.score,
        -value.recorded_at_us,
        value.endpoint.record_id,
        value.endpoint.version,
        value.endpoint.assembly_id,
    )


def _scan_record_page(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
    after_record_id: str,
    record_budget: int,
) -> tuple[tuple[str, ...], bool]:
    """Read one stable, indexed page without materialising the governed frontier."""

    rows = connection.execute(
        "SELECT governed_record_id FROM omnivia_governed_records "
        "INDEXED BY omnivia_idx_engineering_discovery_record_scan "
        "WHERE workspace_id = ? AND domain_scope = ? AND governed_record_id > ? "
        "AND record_type IN (?, ?, ?) AND recorded_at_us <= ? "
        "ORDER BY governed_record_id LIMIT ?",
        (
            workspace_id,
            _DOMAIN,
            after_record_id,
            *_ELIGIBLE_RECORD_TYPES,
            resolution_instant_us,
            record_budget + 1,
        ),
    ).fetchall()
    page = tuple(str(row[0]) for row in rows[:record_budget])
    return page, len(rows) > record_budget


def _match_wire(match: _RankedMatch) -> dict[str, object]:
    return {
        "assembly_id": match.endpoint.assembly_id,
        "record_id": match.endpoint.record_id,
        "version": match.endpoint.version,
        "content_digest": match.endpoint.content_digest,
        "recorded_at_us": match.recorded_at_us,
        "score": match.score,
        "channel": match.channel,
        "basis": dict(match.basis),
    }


def _match_from_wire(value: object, *, channel: str) -> _RankedMatch:
    if not isinstance(value, dict):
        raise StorageError("discovery scan progress contains an invalid match")
    try:
        stored_channel = value["channel"]
        recorded_at_us = value["recorded_at_us"]
        score = value["score"]
        basis = value["basis"]
        endpoint_values = tuple(
            value[key]
            for key in ("assembly_id", "record_id", "version", "content_digest")
        )
    except KeyError as error:
        raise StorageError("discovery scan progress contains an invalid match") from error
    if (
        stored_channel != channel
        or type(recorded_at_us) is not int
        or type(score) is not int
        or not isinstance(basis, dict)
        or not all(isinstance(item, str) for item in endpoint_values)
    ):
        raise StorageError("discovery scan progress contains an invalid match")
    return _RankedMatch(
        endpoint=RelationEndpoint(*endpoint_values),
        recorded_at_us=recorded_at_us,
        score=score,
        channel=channel,
        basis=basis,
    )


def _top_matches_json(
    structural: tuple[_RankedMatch, ...], lexical: tuple[_RankedMatch, ...]
) -> str:
    return to_canonical_json(
        {
            "lexical": [_match_wire(match) for match in lexical],
            "structural": [_match_wire(match) for match in structural],
        }
    )


def _read_scan_progress(
    connection: sqlite3.Connection, *, workspace_id: str, run: DiscoveryRun
) -> _ScanProgress | None:
    row = connection.execute(
        "SELECT batch_sequence, cursor_record_id, frontier_digest, "
        "authorized_frontier_size, structural_considered, lexical_considered, "
        "top_matches_json FROM omnivia_engineering_discovery_scan_progress "
        "INDEXED BY omnivia_idx_engineering_discovery_scan_progress_latest "
        "WHERE workspace_id = ? AND discovery_run_id = ? "
        "ORDER BY batch_sequence DESC LIMIT 1",
        (workspace_id, run.discovery_run_id),
    ).fetchone()
    if row is None:
        return None
    try:
        held = json.loads(str(row[6]))
        if not isinstance(held, dict):
            raise TypeError
        structural = tuple(
            _match_from_wire(value, channel="structural")
            for value in held["structural"]
        )
        lexical = tuple(
            _match_from_wire(value, channel="lexical") for value in held["lexical"]
        )
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise StorageError("discovery scan progress is not readable") from error
    return _ScanProgress(
        batch_sequence=int(row[0]),
        cursor_record_id=str(row[1]),
        frontier_digest=str(row[2]),
        authorized_frontier_size=int(row[3]),
        structural_considered=int(row[4]),
        lexical_considered=int(row[5]),
        structural=structural,
        lexical=lexical,
    )


def _retain_top(
    prior: tuple[_RankedMatch, ...],
    discovered: list[_RankedMatch],
    *,
    budget: int,
) -> tuple[_RankedMatch, ...]:
    by_assembly = {match.endpoint.assembly_id: match for match in (*prior, *discovered)}
    return tuple(sorted(by_assembly.values(), key=_rank_key)[:budget])


def process_oldest_queued_run(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    label_grant: EvidenceLabelGrant,
    allocate_identifier: IdentifierAllocator,
    occurred_at_us: int,
    scan_record_budget: int = DEFAULT_SCAN_RECORD_BUDGET,
) -> DiscoveryProcessingResult | None:
    """Advance the oldest queued run by one indexed, durable record-id page.

    The scan cursor and bounded global top matches commit after every page. A crash
    retries the same page; restart resumes after the last committed record id. The
    candidate budget limits retained results only and is independent of the record
    page budget. A terminal complete event is appended only after index exhaustion.
    """

    if type(scan_record_budget) is not int:
        raise TypeError("scan_record_budget must be an integer")
    if not 1 <= scan_record_budget <= MAX_SCAN_RECORD_BUDGET:
        raise ValueError(
            f"scan_record_budget must be between 1 and {MAX_SCAN_RECORD_BUDGET}"
        )
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        run = read_oldest_queued_run(connection, workspace_id=workspace_id)
        if run is None:
            return None
        if occurred_at_us < run.enqueued_at_us:
            raise ValueError("occurred_at_us precedes the queued discovery run")
        prior = _read_scan_progress(connection, workspace_id=workspace_id, run=run)
        after_record_id = "" if prior is None else prior.cursor_record_id
        record_ids, has_more = _scan_record_page(
            connection,
            workspace_id=workspace_id,
            resolution_instant_us=run.resolution_instant_us,
            after_record_id=after_record_id,
            record_budget=scan_record_budget,
        )
        if not record_ids:
            raise StorageError("a queued discovery scan has no remaining indexed page")

        anchor_previews = _authorized_preview_frontier(
            connection,
            workspace_id=workspace_id,
            run=run,
            label_grant=label_grant,
            record_ids=(run.anchor_record_id,),
        )
        anchor = next(
            (
                candidate
                for candidate in anchor_previews
                if candidate.assembly_id == run.anchor_assembly_id
                and candidate.content_digest == run.anchor_content_digest
            ),
            None,
        )
        previews = _authorized_preview_frontier(
            connection,
            workspace_id=workspace_id,
            run=run,
            label_grant=label_grant,
            record_ids=record_ids,
        )
        dependency_candidates = {
            candidate.assembly_id: candidate
            for candidate in (*previews, *anchor_previews)
        }
        dependencies = {
            assembly_id: _dependency_keys(
                connection,
                workspace_id=workspace_id,
                candidate=candidate,
            )
            for assembly_id, candidate in dependency_candidates.items()
        }
        digest = _frontier_digest(
            previews,
            dependencies,
            previous_digest=None if prior is None else prior.frontier_digest,
        )
        eligible = tuple(
            candidate
            for candidate in previews
            if candidate.assembly_id != run.anchor_assembly_id
            and candidate.record_id != run.anchor_record_id
        )
        structural: list[_RankedMatch] = []
        lexical: list[_RankedMatch] = []
        lexical_considered = 0
        if anchor is not None:
            anchor_dependencies = frozenset(dependencies[anchor.assembly_id])
            anchor_tokens = _lexical_tokens(anchor)
            for candidate in eligible:
                endpoint = RelationEndpoint(
                    candidate.assembly_id,
                    candidate.record_id,
                    candidate.version,
                    candidate.content_digest,
                )
                matched = _structural_match(
                    anchor,
                    candidate,
                    anchor_dependencies=anchor_dependencies,
                    candidate_dependencies=frozenset(
                        dependencies[candidate.assembly_id]
                    ),
                )
                if matched is not None:
                    score, basis = matched
                    structural.append(
                        _RankedMatch(
                            endpoint,
                            candidate.recorded_at_us,
                            score,
                            "structural",
                            basis,
                        )
                    )
                    continue
                if (
                    anchor.repository_id
                    and candidate.repository_id
                    and anchor.repository_id != candidate.repository_id
                ):
                    # Two explicit, different repository identities are a proven
                    # disjoint scope. Unknown scope remains a potential overlap.
                    continue
                lexical_considered += 1
                shared_tokens = anchor_tokens & _lexical_tokens(candidate)
                if shared_tokens:
                    lexical.append(
                        _RankedMatch(
                            endpoint,
                            candidate.recorded_at_us,
                            len(shared_tokens),
                            "lexical",
                            {"shared_token_count": len(shared_tokens)},
                        )
                    )

        top_structural = _retain_top(
            () if prior is None else prior.structural,
            structural,
            budget=run.candidate_budget,
        )
        top_lexical = _retain_top(
            () if prior is None else prior.lexical,
            lexical,
            budget=run.candidate_budget,
        )
        authorized_frontier_size = (
            (0 if prior is None else prior.authorized_frontier_size) + len(previews)
        )
        structural_considered = (
            (0 if prior is None else prior.structural_considered)
            + (len(eligible) if anchor is not None else 0)
        )
        total_lexical_considered = (
            (0 if prior is None else prior.lexical_considered) + lexical_considered
        )
        batch_sequence = 1 if prior is None else prior.batch_sequence + 1
        cursor_record_id = record_ids[-1]
        connection.execute(
            "INSERT INTO omnivia_engineering_discovery_scan_progress "
            "(workspace_id, discovery_run_id, batch_sequence, progress_id, "
            "cursor_record_id, frontier_digest, authorized_frontier_size, "
            "structural_considered, lexical_considered, top_matches_json, "
            "recorded_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                run.discovery_run_id,
                batch_sequence,
                allocate_identifier("edp"),
                cursor_record_id,
                digest,
                authorized_frontier_size,
                structural_considered,
                total_lexical_considered,
                _top_matches_json(top_structural, top_lexical),
                occurred_at_us,
            ),
        )
        if has_more:
            return DiscoveryProcessingResult(
                run=run,
                frontier_digest=digest,
                authorized_frontier_size=authorized_frontier_size,
                structural_considered=structural_considered,
                lexical_considered=total_lexical_considered,
                candidates=(),
                coverage="partial",
                scan_complete=False,
                last_processed_record_id=cursor_record_id,
            )

        selected = list(top_structural[: run.candidate_budget])
        selected.extend(top_lexical[: run.candidate_budget - len(selected)])
        selected_record_ids = tuple(
            sorted({run.anchor_record_id, *(item.endpoint.record_id for item in selected)})
        )
        terminal_previews = _authorized_preview_frontier(
            connection,
            workspace_id=workspace_id,
            run=run,
            label_grant=label_grant,
            record_ids=selected_record_ids,
        )
        terminal_visible = {
            candidate.assembly_id: candidate for candidate in terminal_previews
        }
        visible_anchor = terminal_visible.get(run.anchor_assembly_id)
        persisted: list[RelationCandidate] = []
        if (
            visible_anchor is not None
            and RelationEndpoint(
                visible_anchor.assembly_id,
                visible_anchor.record_id,
                visible_anchor.version,
                visible_anchor.content_digest,
            )
            == RelationEndpoint(
                run.anchor_assembly_id,
                run.anchor_record_id,
                run.anchor_version,
                run.anchor_content_digest,
            )
        ):
            anchor_endpoint = RelationEndpoint(
                visible_anchor.assembly_id,
                visible_anchor.record_id,
                visible_anchor.version,
                visible_anchor.content_digest,
            )
            for match in selected:
                visible_other = terminal_visible.get(match.endpoint.assembly_id)
                if (
                    visible_other is None
                    or RelationEndpoint(
                        visible_other.assembly_id,
                        visible_other.record_id,
                        visible_other.version,
                        visible_other.content_digest,
                    )
                    != match.endpoint
                ):
                    continue
                relation = _append_authorized_relation_candidate(
                    connection,
                    workspace_id=workspace_id,
                    run=run,
                    anchor=anchor_endpoint,
                    other=match.endpoint,
                    allocate_identifier=allocate_identifier,
                )
                _append_candidate_observation(
                    connection,
                    workspace_id=workspace_id,
                    run=run,
                    candidate=relation,
                    selected_order=len(persisted) + 1,
                    channel=match.channel,
                    score=match.score,
                    basis=match.basis,
                    recorded_at_us=occurred_at_us,
                )
                persisted.append(relation)
        _append_terminal_event(
            connection,
            workspace_id=workspace_id,
            run=run,
            state="completed",
            coverage="scan_complete_for_snapshot",
            frontier_digest=digest,
            authorized_frontier_size=authorized_frontier_size,
            structural_considered=structural_considered,
            lexical_considered=total_lexical_considered,
            selected_count=len(persisted),
            failure_code=None,
            occurred_at_us=occurred_at_us,
            allocate_identifier=allocate_identifier,
        )
        return DiscoveryProcessingResult(
            run=run,
            frontier_digest=digest,
            authorized_frontier_size=authorized_frontier_size,
            structural_considered=structural_considered,
            lexical_considered=total_lexical_considered,
            candidates=tuple(persisted),
            coverage="scan_complete_for_snapshot",
            scan_complete=True,
            last_processed_record_id=cursor_record_id,
        )


def _read_visible_relation_candidates(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    anchor_assembly_id: str,
    visible: Mapping[str, RelationEndpoint],
) -> tuple[RelationCandidate, ...]:
    if anchor_assembly_id not in visible:
        return ()
    admitted_others = sorted(set(visible) - {anchor_assembly_id})
    rows: list[sqlite3.Row | tuple[object, ...]] = []
    for start in range(0, len(admitted_others), _RELATION_READ_BATCH):
        batch = admitted_others[start : start + _RELATION_READ_BATCH]
        placeholders = ", ".join("?" for _ in batch)
        columns = (
            "relation_candidate_id, endpoint_a_assembly_id, "
            "endpoint_a_record_id, endpoint_a_version, endpoint_a_digest, "
            "endpoint_b_assembly_id, endpoint_b_record_id, endpoint_b_version, "
            "endpoint_b_digest, detector_version, scope_classification, "
            "proposed_relation, status, first_discovery_run_id, recorded_at_us"
        )
        rows.extend(
            connection.execute(
                f"SELECT {columns} FROM omnivia_engineering_relation_candidates "
                "INDEXED BY omnivia_idx_engineering_relation_candidates_endpoint_a "
                "WHERE workspace_id = ? AND endpoint_a_assembly_id = ? "
                f"AND status = 'pending' AND endpoint_b_assembly_id IN ({placeholders}) "
                "UNION ALL "
                f"SELECT {columns} FROM omnivia_engineering_relation_candidates "
                "INDEXED BY omnivia_idx_engineering_relation_candidates_endpoint_b "
                "WHERE workspace_id = ? AND endpoint_b_assembly_id = ? "
                f"AND status = 'pending' AND endpoint_a_assembly_id IN ({placeholders})",
                (
                    workspace_id,
                    anchor_assembly_id,
                    *batch,
                    workspace_id,
                    anchor_assembly_id,
                    *batch,
                ),
            ).fetchall()
        )
    candidates: list[RelationCandidate] = []
    for row in rows:
        endpoint_a = RelationEndpoint(*(str(value) for value in row[1:5]))
        endpoint_b = RelationEndpoint(*(str(value) for value in row[5:9]))
        if (
            visible.get(endpoint_a.assembly_id) != endpoint_a
            or visible.get(endpoint_b.assembly_id) != endpoint_b
        ):
            continue
        candidates.append(
            RelationCandidate(
                relation_candidate_id=str(row[0]),
                endpoint_a=endpoint_a,
                endpoint_b=endpoint_b,
                detector_version=str(row[9]),
                scope_classification=str(row[10]),
                proposed_relation=str(row[11]),
                status=str(row[12]),
                first_discovery_run_id=str(row[13]),
                recorded_at_us=int(str(row[14])),
            )
        )
    candidates.sort(
        key=lambda candidate: (
            candidate.recorded_at_us,
            candidate.relation_candidate_id,
        )
    )
    return tuple(candidates)


_NON_MATERIAL_ASSESSMENTS: Final = frozenset(
    {"compatible", "not_conflict", "related", "scoped_difference"}
)


def _latest_relation_assessment(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    relation_candidate_id: str,
    resolution_instant_us: int,
) -> tuple[str, str | None] | None:
    """Read one deterministic latest result through the candidate-history index."""

    row = connection.execute(
        "SELECT status, assessed_relation "
        "FROM omnivia_engineering_relation_assessment_results INDEXED BY "
        "omnivia_idx_engineering_relation_assessment_results_candidate "
        "WHERE workspace_id = ? AND relation_candidate_id = ? "
        "AND reconciled_at_us <= ? "
        "ORDER BY reconciled_at_us DESC, result_id DESC LIMIT 1",
        (workspace_id, relation_candidate_id, resolution_instant_us),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), None if row[1] is None else str(row[1])


def _context_relation_status(
    candidate: RelationCandidate,
    assessment: tuple[str, str | None] | None,
) -> str | None:
    """Classify one pending relation without promoting uncertainty to fact."""

    if assessment is not None and assessment[0] == "assessed":
        relation = assessment[1]
        if relation == "conflicts_with":
            return "unresolved"
        if relation in _NON_MATERIAL_ASSESSMENTS:
            return None
        # A proposed supersession is not governed resolution. Until review it
        # remains unsafe to select either endpoint as the sole conclusion.
        return "unresolved_overlap"
    if candidate.scope_classification == "scoped_difference":
        return None
    # Missing, unavailable and failed semantic assessment retain the structural
    # overlap as a neutral warning rather than asserting a contradiction.
    return "unresolved_overlap"


def _read_context_relation_candidates(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
    label_grant: EvidenceLabelGrant,
    visible: Mapping[str, RelationEndpoint],
) -> tuple[tuple[tuple[RelationCandidate, str], ...], frozenset[str]]:
    """Read bounded unsafe relations and hide unauthorized peer identities.

    Each indexed batch is capped. If a batch exceeds the cap, every visible anchor
    in that batch is withheld generically; no hidden endpoint identity or count is
    returned. Latest assessment reads are one indexed row per candidate.
    """

    assembly_ids = tuple(sorted(visible))
    if not assembly_ids:
        return (), frozenset()
    if len(assembly_ids) > MAX_CONTEXT_CONFLICT_ELIGIBLE_ENDPOINTS:
        raise ContextConflictLimitExceeded(
            "the context conflict eligible endpoint set exceeds its bound"
        )
    columns = (
        "candidate.relation_candidate_id, candidate.endpoint_a_assembly_id, "
        "candidate.endpoint_a_record_id, candidate.endpoint_a_version, "
        "candidate.endpoint_a_digest, candidate.endpoint_b_assembly_id, "
        "candidate.endpoint_b_record_id, candidate.endpoint_b_version, "
        "candidate.endpoint_b_digest, candidate.detector_version, "
        "candidate.scope_classification, candidate.proposed_relation, "
        "candidate.status, candidate.first_discovery_run_id, "
        "candidate.recorded_at_us"
    )
    unsafe: dict[str, tuple[RelationCandidate, str]] = {}
    assessment_cache: dict[str, tuple[str, str | None] | None] = {}
    withheld: set[str] = set()
    for start in range(0, len(assembly_ids), _CONTEXT_RELATION_READ_BATCH):
        batch = assembly_ids[start : start + _CONTEXT_RELATION_READ_BATCH]
        placeholders = ", ".join("?" for _ in batch)
        for anchor_column, other_column, index_name, anchor_slice in (
            (
                "endpoint_a_assembly_id",
                "endpoint_b_assembly_id",
                "omnivia_idx_engineering_relation_candidates_endpoint_a",
                slice(1, 5),
            ),
            (
                "endpoint_b_assembly_id",
                "endpoint_a_assembly_id",
                "omnivia_idx_engineering_relation_candidates_endpoint_b",
                slice(5, 9),
            ),
        ):
            rows = connection.execute(
                f"SELECT {columns} FROM omnivia_engineering_relation_candidates "
                f"AS candidate INDEXED BY {index_name} "
                "WHERE candidate.workspace_id = ? AND candidate.status = 'pending' "
                "AND candidate.recorded_at_us <= ? "
                f"AND candidate.{anchor_column} IN ({placeholders}) "
                f"ORDER BY candidate.{anchor_column}, candidate.{other_column}, "
                "candidate.recorded_at_us, candidate.relation_candidate_id LIMIT ?",
                (
                    workspace_id,
                    resolution_instant_us,
                    *batch,
                    MAX_CONTEXT_RELATION_ROWS_PER_BATCH + 1,
                ),
            ).fetchall()
            if len(rows) > MAX_CONTEXT_RELATION_ROWS_PER_BATCH:
                raise ContextConflictLimitExceeded(
                    "the context conflict relation read exceeds its bound"
                )
            for row in rows:
                endpoint_a = RelationEndpoint(*(str(value) for value in row[1:5]))
                endpoint_b = RelationEndpoint(*(str(value) for value in row[5:9]))
                anchor = RelationEndpoint(*(str(value) for value in row[anchor_slice]))
                if visible.get(anchor.assembly_id) != anchor:
                    withheld.add(anchor.assembly_id)
                    continue
                candidate = RelationCandidate(
                    relation_candidate_id=str(row[0]),
                    endpoint_a=endpoint_a,
                    endpoint_b=endpoint_b,
                    detector_version=str(row[9]),
                    scope_classification=str(row[10]),
                    proposed_relation=str(row[11]),
                    status=str(row[12]),
                    first_discovery_run_id=str(row[13]),
                    recorded_at_us=int(str(row[14])),
                )
                if candidate.relation_candidate_id not in assessment_cache:
                    assessment_cache[candidate.relation_candidate_id] = (
                        _latest_relation_assessment(
                            connection,
                            workspace_id=workspace_id,
                            relation_candidate_id=candidate.relation_candidate_id,
                            resolution_instant_us=resolution_instant_us,
                        )
                    )
                assessment = assessment_cache[candidate.relation_candidate_id]
                status = _context_relation_status(candidate, assessment)
                if status is None:
                    continue
                unsafe[candidate.relation_candidate_id] = (candidate, status)

    related_requests = {
        endpoint.assembly_id: endpoint
        for candidate, _status in unsafe.values()
        for endpoint in (candidate.endpoint_a, candidate.endpoint_b)
        if visible.get(endpoint.assembly_id) != endpoint
    }
    authorized_related: dict[str, RelationEndpoint] = dict(visible)
    if len(related_requests) > MAX_CONTEXT_RELATED_ENDPOINTS:
        # The generic handler notice is emitted only if one of these anchors would
        # otherwise enter the final selection.
        withheld.update(visible)
    elif related_requests:
        related_record_ids = tuple(
            sorted({endpoint.record_id for endpoint in related_requests.values()})
        )
        for view in _DISCOVERY_VIEWS:
            frontier = read_authorized_memory_frontier(
                connection,
                workspace_id=workspace_id,
                resolution_instant_us=resolution_instant_us,
                view=view,
                label_grant=label_grant,
                domain_scope=_DOMAIN,
                record_ids=related_record_ids,
            )
            for version in frontier.versions:
                endpoint = RelationEndpoint(
                    version.assembly_id,
                    version.record_id,
                    version.version_id,
                    version.content_digest,
                )
                if related_requests.get(endpoint.assembly_id) == endpoint:
                    authorized_related[endpoint.assembly_id] = endpoint

    held: dict[str, tuple[RelationCandidate, str]] = {}
    for candidate_id, relation in unsafe.items():
        candidate, _status = relation
        endpoint_a_eligible = visible.get(candidate.endpoint_a.assembly_id) == (
            candidate.endpoint_a
        )
        endpoint_b_eligible = visible.get(candidate.endpoint_b.assembly_id) == (
            candidate.endpoint_b
        )
        if endpoint_a_eligible and endpoint_b_eligible:
            held[candidate_id] = relation
            if len(held) > MAX_CONTEXT_CONFLICT_EDGES:
                raise ContextConflictLimitExceeded(
                    "the context conflict edge set exceeds its bound"
                )
            continue
        if endpoint_a_eligible:
            eligible = candidate.endpoint_a
            other = candidate.endpoint_b
        elif endpoint_b_eligible:
            eligible = candidate.endpoint_b
            other = candidate.endpoint_a
        else:  # pragma: no cover - every row was selected by one exact anchor
            continue
        if authorized_related.get(other.assembly_id) != other:
            withheld.add(eligible.assembly_id)

    # A generically withheld endpoint cannot leave its visible peer looking like
    # an uncontested conclusion. Propagate withholding over every known unsafe edge.
    changed = True
    while changed:
        changed = False
        for candidate, _status in held.values():
            endpoints = {
                candidate.endpoint_a.assembly_id,
                candidate.endpoint_b.assembly_id,
            }
            if endpoints & withheld and not endpoints <= withheld:
                withheld.update(endpoints)
                changed = True

    return (
        tuple(
            sorted(
                (
                    relation
                    for relation in held.values()
                    if relation[0].endpoint_a.assembly_id not in withheld
                    and relation[0].endpoint_b.assembly_id not in withheld
                ),
                key=lambda relation: (
                    relation[0].endpoint_a.record_id,
                    relation[0].endpoint_a.version,
                    relation[0].endpoint_b.record_id,
                    relation[0].endpoint_b.version,
                    relation[0].recorded_at_us,
                    relation[0].relation_candidate_id,
                ),
            ),
        ),
        frozenset(withheld),
    )


def read_authorized_conflict_components(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
    label_grant: EvidenceLabelGrant,
    eligible_endpoints: tuple[RelationEndpoint, ...],
) -> ConflictReadResult:
    """Return warned components and authorization-safe generic withholdings.

    The eligible endpoint values are identities only. They are re-authorized under
    the caller's grant and pinned instant before relation rows are read. Both the
    eligible and material graph sets have hard bounds, and only exact visible pairs
    can enter a component. A latest assessed conflict produces a material warning;
    an unresolved structural overlap produces a neutral warning; a latest assessed
    non-material relation suppresses the candidate. A relation to an endpoint that
    does not reauthorize withholds the visible endpoint without returning the hidden
    identity, relation type or count.
    """

    if label_grant.workspace_id != workspace_id:
        raise StorageError("the context conflict grant does not match the workspace")
    requested: dict[str, RelationEndpoint] = {}
    for endpoint in eligible_endpoints:
        prior = requested.setdefault(endpoint.assembly_id, endpoint)
        if prior != endpoint:
            raise StorageError("a context endpoint identity is inconsistent")
    if len(requested) > MAX_CONTEXT_CONFLICT_ELIGIBLE_ENDPOINTS:
        raise ContextConflictLimitExceeded(
            "the context conflict eligible endpoint set exceeds its bound"
        )
    if not requested:
        # A single visible endpoint can still have an inaccessible unsafe peer.
        return ConflictReadResult((), frozenset())

    record_ids = tuple(sorted({endpoint.record_id for endpoint in requested.values()}))
    with read_snapshot(connection):
        visible: dict[str, RelationEndpoint] = {}
        for view in _DISCOVERY_VIEWS:
            frontier = read_authorized_memory_frontier(
                connection,
                workspace_id=workspace_id,
                resolution_instant_us=resolution_instant_us,
                view=view,
                label_grant=label_grant,
                domain_scope=_DOMAIN,
                record_ids=record_ids,
            )
            for version in frontier.versions:
                endpoint = RelationEndpoint(
                    version.assembly_id,
                    version.record_id,
                    version.version_id,
                    version.content_digest,
                )
                if requested.get(endpoint.assembly_id) == endpoint:
                    prior = visible.setdefault(endpoint.assembly_id, endpoint)
                    if prior != endpoint:  # pragma: no cover - exact identity key
                        raise StorageError(
                            "an exact context endpoint changed within one read snapshot"
                        )

        candidates, withheld_endpoint_ids = _read_context_relation_candidates(
            connection,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
            label_grant=label_grant,
            visible=visible,
        )
        withheld_endpoint_ids = frozenset(
            {*withheld_endpoint_ids, *(set(requested) - set(visible))}
        )
    if not candidates:
        return ConflictReadResult((), withheld_endpoint_ids)

    participating = {
        endpoint.assembly_id
        for candidate, _status in candidates
        for endpoint in (candidate.endpoint_a, candidate.endpoint_b)
    }
    if len(participating) > MAX_CONTEXT_CONFLICT_ENDPOINTS:
        raise ContextConflictLimitExceeded(
            "the material context conflict endpoint set exceeds its bound"
        )

    parent = {assembly_id: assembly_id for assembly_id in visible}

    def find(assembly_id: str) -> str:
        while parent[assembly_id] != assembly_id:
            parent[assembly_id] = parent[parent[assembly_id]]
            assembly_id = parent[assembly_id]
        return assembly_id

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        parent[second] = first

    for candidate, _status in candidates:
        left = candidate.endpoint_a.assembly_id
        right = candidate.endpoint_b.assembly_id
        union(left, right)

    groups: dict[str, list[RelationEndpoint]] = {}
    for assembly_id in participating:
        groups.setdefault(find(assembly_id), []).append(visible[assembly_id])
    status_by_root: dict[str, str] = {}
    for candidate, status in candidates:
        root = find(candidate.endpoint_a.assembly_id)
        if status == "unresolved" or root not in status_by_root:
            status_by_root[root] = status
    components = [
        ConflictComponent(
            endpoints=tuple(
                sorted(
                    endpoints,
                    key=lambda endpoint: (
                        endpoint.record_id,
                        endpoint.version,
                        endpoint.assembly_id,
                    ),
                )
            ),
            status=status_by_root[root],
        )
        for root, endpoints in groups.items()
        if len(endpoints) >= 2
    ]
    components.sort(
        key=lambda component: tuple(
            (endpoint.record_id, endpoint.version, endpoint.assembly_id)
            for endpoint in component.endpoints
        )
    )
    return ConflictReadResult(tuple(components), withheld_endpoint_ids)


def read_authorized_relation_candidates(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    anchor_assembly_id: str,
    resolution_instant_us: int,
    view: str | None,
    label_grant: EvidenceLabelGrant,
) -> tuple[RelationCandidate, ...]:
    """Return one view's pending relations after both endpoints are authorized."""

    with read_snapshot(connection):
        previews, _frontier_digest = read_authorized_previews(
            connection,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
            view=view,
            label_grant=label_grant,
        )
        visible = {
            candidate.assembly_id: RelationEndpoint(
                candidate.assembly_id,
                candidate.record_id,
                candidate.version,
                candidate.content_digest,
            )
            for candidate in previews
        }
        return _read_visible_relation_candidates(
            connection,
            workspace_id=workspace_id,
            anchor_assembly_id=anchor_assembly_id,
            visible=visible,
        )


def read_authorized_relation_candidates_for_anchor(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    anchor_record_id: str,
    anchor_version: str,
    resolution_instant_us: int,
    label_grant: EvidenceLabelGrant,
) -> tuple[RelationCandidate, ...]:
    """Return all-view pending relations for an exact authorized expand anchor.

    This identity-only path does not depend on the preview projection. It authorizes
    the anchor first, reads only that anchor's internal pending endpoint identities,
    authorizes those stable records through the scoped frontier seam, and checks
    exact endpoint identity plus digest before any edge is returned.
    """

    with read_snapshot(connection):
        anchor_assembly_id: str | None = None
        for view in _DISCOVERY_VIEWS:
            frontier = read_authorized_memory_frontier(
                connection,
                workspace_id=workspace_id,
                resolution_instant_us=resolution_instant_us,
                view=view,
                label_grant=label_grant,
                domain_scope=_DOMAIN,
                record_ids=(anchor_record_id,),
            )
            for version in frontier.versions:
                if (
                    version.record_id == anchor_record_id
                    and version.version_id == anchor_version
                ):
                    anchor_assembly_id = version.assembly_id
        if anchor_assembly_id is None:
            return ()

        endpoint_rows = connection.execute(
            "SELECT endpoint_b_record_id FROM omnivia_engineering_relation_candidates "
            "INDEXED BY omnivia_idx_engineering_relation_candidates_endpoint_a "
            "WHERE workspace_id = ? AND endpoint_a_assembly_id = ? "
            "AND status = 'pending' UNION ALL "
            "SELECT endpoint_a_record_id FROM omnivia_engineering_relation_candidates "
            "INDEXED BY omnivia_idx_engineering_relation_candidates_endpoint_b "
            "WHERE workspace_id = ? AND endpoint_b_assembly_id = ? "
            "AND status = 'pending'",
            (
                workspace_id,
                anchor_assembly_id,
                workspace_id,
                anchor_assembly_id,
            ),
        ).fetchall()
        scoped_record_ids = tuple(
            sorted({anchor_record_id, *(str(row[0]) for row in endpoint_rows)})
        )
        visible: dict[str, RelationEndpoint] = {}
        for view in _DISCOVERY_VIEWS:
            frontier = read_authorized_memory_frontier(
                connection,
                workspace_id=workspace_id,
                resolution_instant_us=resolution_instant_us,
                view=view,
                label_grant=label_grant,
                domain_scope=_DOMAIN,
                record_ids=scoped_record_ids,
            )
            for version in frontier.versions:
                endpoint = RelationEndpoint(
                    version.assembly_id,
                    version.record_id,
                    version.version_id,
                    version.content_digest,
                )
                prior = visible.setdefault(version.assembly_id, endpoint)
                if prior != endpoint:
                    raise StorageError(
                        "an exact relation endpoint changed within one read snapshot"
                    )
        return _read_visible_relation_candidates(
            connection,
            workspace_id=workspace_id,
            anchor_assembly_id=anchor_assembly_id,
            visible=visible,
        )


__all__ = [
    "DEFAULT_CANDIDATE_BUDGET",
    "DEFAULT_SCAN_RECORD_BUDGET",
    "DETECTOR_VERSION",
    "MAX_CANDIDATE_BUDGET",
    "MAX_CONTEXT_CONFLICT_EDGES",
    "MAX_CONTEXT_CONFLICT_ELIGIBLE_ENDPOINTS",
    "MAX_CONTEXT_CONFLICT_ENDPOINTS",
    "MAX_CONTEXT_RELATION_ROWS_PER_BATCH",
    "MAX_SCAN_RECORD_BUDGET",
    "ConflictComponent",
    "ConflictReadResult",
    "ContextConflictLimitExceeded",
    "DiscoveryProcessingResult",
    "DiscoveryRun",
    "RelationCandidate",
    "RelationEndpoint",
    "enqueue_discovery",
    "process_oldest_queued_run",
    "read_authorized_conflict_components",
    "read_authorized_relation_candidates",
    "read_authorized_relation_candidates_for_anchor",
    "read_oldest_queued_run",
]
