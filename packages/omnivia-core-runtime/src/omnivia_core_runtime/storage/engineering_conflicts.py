"""Durable Stage A primitives for deterministic engineering conflict discovery.

The enqueue path records only exact identities and digests. It runs inside the
application mutation that sealed the anchor and never starts a transaction of its
own. Candidate processing remains a later stage: this module supplies the durable
queue, append helpers, and an authorization-first read seam without claiming that
the current preview frontier is an indexed bounded scan.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core_runtime.service.mutation import MutationSettlementContext
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.engineering_preview import read_authorized_previews
from omnivia_core_runtime.storage.memory import read_snapshot
from omnivia_core_runtime.storage.retrieval import EvidenceLabelGrant

IdentifierAllocator = Callable[[str], str]

DETECTOR_VERSION: Final = "engineering.conflict.discovery.v1"
DEFAULT_CANDIDATE_BUDGET: Final = 8
MAX_CANDIDATE_BUDGET: Final = 32

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
_DISCOVERY_VIEWS: Final[tuple[str | None, ...]] = (
    "candidates",
    None,
    "history",
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
                )
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
    run principal's explicit label grant. Stage A has no immutable
    stream/snapshot-to-checkout binding, so this helper can persist only
    ``unresolved_overlap``. A later migration may admit ``scoped_difference`` once it
    can prove that binding instead of inferring it from unequal stream identifiers.
    """

    anchor, other = _authorized_endpoint_pair(
        connection,
        workspace_id=workspace_id,
        run=run,
        other_assembly_id=other_assembly_id,
        label_grant=label_grant,
    )
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
        connection.execute(
            "INSERT INTO omnivia_engineering_relation_candidates "
            "(workspace_id, relation_candidate_id, endpoint_a_assembly_id, "
            "endpoint_a_record_id, endpoint_a_version, endpoint_a_digest, "
            "endpoint_b_assembly_id, endpoint_b_record_id, endpoint_b_version, "
            "endpoint_b_digest, detector_version, scope_classification, "
            "proposed_relation, status, first_discovery_run_id, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unresolved_overlap', "
            "'related', 'pending', ?, ?)",
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
                run.discovery_run_id,
                run.resolution_instant_us,
            ),
        )
        row = (
            candidate_id,
            "unresolved_overlap",
            "related",
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


def read_authorized_relation_candidates(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    anchor_assembly_id: str,
    resolution_instant_us: int,
    view: str | None,
    label_grant: EvidenceLabelGrant,
) -> tuple[RelationCandidate, ...]:
    """Return pending relations only when both exact endpoints are authorized.

    Authorization is resolved before candidate rows are selected or ordered. The
    helper returns no hidden endpoint, dangling edge, or hidden count. It intentionally
    has no result limit; a caller that adds one must apply it after this filtering.
    """

    with read_snapshot(connection):
        visible_previews = read_authorized_previews(
            connection,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
            view=view,
            label_grant=label_grant,
        )
        visible = {candidate.assembly_id: candidate for candidate in visible_previews}
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
        endpoint_a_preview = visible.get(str(row[1]))
        endpoint_b_preview = visible.get(str(row[5]))
        if (
            endpoint_a_preview is None
            or endpoint_b_preview is None
            or endpoint_a_preview.content_digest != str(row[4])
            or endpoint_b_preview.content_digest != str(row[8])
        ):
            continue
        candidates.append(
            RelationCandidate(
                relation_candidate_id=str(row[0]),
                endpoint_a=RelationEndpoint(*(str(value) for value in row[1:5])),
                endpoint_b=RelationEndpoint(*(str(value) for value in row[5:9])),
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


__all__ = [
    "DEFAULT_CANDIDATE_BUDGET",
    "DETECTOR_VERSION",
    "MAX_CANDIDATE_BUDGET",
    "DiscoveryRun",
    "RelationCandidate",
    "RelationEndpoint",
    "enqueue_discovery",
    "read_authorized_relation_candidates",
    "read_oldest_queued_run",
]
