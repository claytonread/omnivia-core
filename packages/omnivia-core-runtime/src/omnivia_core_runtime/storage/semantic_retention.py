"""Fenced, content-free Phase 2 evidence retention and deletion planning."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from omnivia_core.semantic_registry import EvidenceItem, TemporalInstant, content_digest
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.semantic_evidence import _from_us, _to_us


class DeletionStorageClass(str, Enum):
    CANONICAL_METADATA = "canonical_metadata"
    PROTECTED_CONTENT = "protected_content"
    SOURCE_SPANS = "source_spans"
    RAW_COMPLETIONS = "raw_completions"
    WORKER_SCRATCH = "worker_scratch"
    SEARCH_PROJECTION = "search_projection"
    GRAPH_PROJECTION = "graph_projection"
    VECTOR_PROJECTION = "vector_projection"
    CACHES = "caches"
    LOGS = "logs"
    BACKUPS = "backups"


ALL_DELETION_STORAGE_CLASSES = tuple(DeletionStorageClass)


class DeletionPlanState(str, Enum):
    READY = "ready"
    BLOCKED_NOT_DUE = "blocked_not_due"
    BLOCKED_LEGAL_HOLD = "blocked_legal_hold"


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    workspace_id: str
    policy_version: str
    default_retention_days: int
    created_at: TemporalInstant

    def __post_init__(self) -> None:
        if not self.workspace_id or not self.policy_version:
            raise ValueError("retention policy identity is required")
        if (
            not isinstance(self.default_retention_days, int)
            or isinstance(self.default_retention_days, bool)
            or self.default_retention_days <= 0
        ):
            raise ValueError("default_retention_days must be a positive integer")


@dataclass(frozen=True, slots=True)
class EvidenceLegalHold:
    workspace_id: str
    hold_id: str
    evidence_id: str
    reason_code: str
    placed_at: TemporalInstant


@dataclass(frozen=True, slots=True)
class EvidenceLegalHoldRelease:
    workspace_id: str
    release_id: str
    hold_id: str
    actor_principal_id: str
    released_at: TemporalInstant


@dataclass(frozen=True, slots=True)
class EvidenceDeletionTarget:
    storage_class: DeletionStorageClass
    target_ref: str
    target_digest: str


@dataclass(frozen=True, slots=True)
class EvidenceDeletionPlan:
    workspace_id: str
    plan_id: str
    evidence_id: str
    policy_version: str
    reason_code: str
    requested_at: TemporalInstant
    due_at: TemporalInstant
    state: DeletionPlanState
    targets: tuple[EvidenceDeletionTarget, ...]


@dataclass(frozen=True, slots=True)
class EvidenceDeletionReceipt:
    workspace_id: str
    receipt_id: str
    plan_id: str
    reason_code: str
    completed_at: TemporalInstant
    target_digests: tuple[str, ...]


def _instant_payload(value: TemporalInstant) -> dict[str, str]:
    return {
        "value": value.value.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "precision": value.precision.value,
        "provenance": value.provenance.value,
    }


def retention_policy_digest(value: RetentionPolicy) -> str:
    return content_digest(
        {
            "workspace_id": value.workspace_id,
            "policy_version": value.policy_version,
            "default_retention_days": value.default_retention_days,
            "created_at": _instant_payload(value.created_at),
        }
    )


def legal_hold_digest(value: EvidenceLegalHold) -> str:
    return content_digest(
        {
            "workspace_id": value.workspace_id,
            "hold_id": value.hold_id,
            "evidence_id": value.evidence_id,
            "reason_code": value.reason_code,
            "placed_at": _instant_payload(value.placed_at),
        }
    )


def legal_hold_release_digest(value: EvidenceLegalHoldRelease) -> str:
    return content_digest(
        {
            "workspace_id": value.workspace_id,
            "release_id": value.release_id,
            "hold_id": value.hold_id,
            "actor_principal_id": value.actor_principal_id,
            "released_at": _instant_payload(value.released_at),
        }
    )


def deletion_target_digest(
    workspace_id: str, evidence_id: str, storage_class: DeletionStorageClass
) -> str:
    return content_digest(
        {
            "workspace_id": workspace_id,
            "evidence_id": evidence_id,
            "storage_class": storage_class.value,
        }
    )


def deletion_plan_digest(value: EvidenceDeletionPlan) -> str:
    return content_digest(
        {
            "workspace_id": value.workspace_id,
            "plan_id": value.plan_id,
            "evidence_id": value.evidence_id,
            "policy_version": value.policy_version,
            "reason_code": value.reason_code,
            "requested_at": _instant_payload(value.requested_at),
            "due_at": _instant_payload(value.due_at),
            "state": value.state.value,
            "targets": [
                {
                    "storage_class": target.storage_class.value,
                    "target_ref": target.target_ref,
                    "target_digest": target.target_digest,
                }
                for target in value.targets
            ],
        }
    )


def deletion_receipt_digest(value: EvidenceDeletionReceipt) -> str:
    return content_digest(
        {
            "workspace_id": value.workspace_id,
            "receipt_id": value.receipt_id,
            "plan_id": value.plan_id,
            "reason_code": value.reason_code,
            "completed_at": _instant_payload(value.completed_at),
            "target_digests": sorted(value.target_digests),
        }
    )


def build_deletion_plan(
    *,
    plan_id: str,
    evidence: EvidenceItem,
    policy: RetentionPolicy,
    requested_at: TemporalInstant,
    reason_code: str,
    active_legal_hold: bool,
    retention_override_days: int | None = None,
) -> EvidenceDeletionPlan:
    """Build a deterministic plan; an override may tighten but never loosen policy."""
    if evidence.workspace_id != policy.workspace_id:
        raise StorageError("retention policy and evidence workspace do not match")
    days = policy.default_retention_days
    if retention_override_days is not None:
        if (
            isinstance(retention_override_days, bool)
            or retention_override_days <= 0
            or retention_override_days > policy.default_retention_days
        ):
            raise StorageError(
                "retention override may only shorten the workspace default"
            )
        days = retention_override_days
    due_value = evidence.captured_at.value + timedelta(days=days)
    due_at = TemporalInstant(
        value=due_value,
        precision=evidence.captured_at.precision,
        provenance=evidence.captured_at.provenance,
    )
    state = (
        DeletionPlanState.BLOCKED_LEGAL_HOLD
        if active_legal_hold
        else DeletionPlanState.READY
        if requested_at.value >= due_at.value
        else DeletionPlanState.BLOCKED_NOT_DUE
    )
    targets = tuple(
        EvidenceDeletionTarget(
            storage_class=storage_class,
            target_ref=evidence.evidence_id,
            target_digest=deletion_target_digest(
                evidence.workspace_id, evidence.evidence_id, storage_class
            ),
        )
        for storage_class in ALL_DELETION_STORAGE_CLASSES
    )
    return EvidenceDeletionPlan(
        workspace_id=evidence.workspace_id,
        plan_id=plan_id,
        evidence_id=evidence.evidence_id,
        policy_version=policy.policy_version,
        reason_code=reason_code,
        requested_at=requested_at,
        due_at=due_at,
        state=state,
        targets=targets,
    )


def build_deletion_receipt(
    *, receipt_id: str, plan: EvidenceDeletionPlan, completed_at: TemporalInstant
) -> EvidenceDeletionReceipt:
    if plan.state is not DeletionPlanState.READY:
        raise StorageError("only a ready deletion plan can be receipted")
    return EvidenceDeletionReceipt(
        workspace_id=plan.workspace_id,
        receipt_id=receipt_id,
        plan_id=plan.plan_id,
        reason_code=plan.reason_code,
        completed_at=completed_at,
        target_digests=tuple(target.target_digest for target in plan.targets),
    )


def active_legal_holds(
    connection: sqlite3.Connection, workspace_id: str, evidence_id: str
) -> tuple[EvidenceLegalHold, ...]:
    rows = connection.execute(
        "SELECT h.hold_id,h.reason_code,h.placed_at_us,h.placed_at_precision,"
        "h.placed_at_provenance "
        "FROM omnivia_semantic_evidence_legal_holds h "
        "LEFT JOIN omnivia_semantic_evidence_legal_hold_releases r "
        "ON r.workspace_id=h.workspace_id AND r.hold_id=h.hold_id "
        "WHERE h.workspace_id=? AND h.evidence_id=? AND r.release_id IS NULL "
        "ORDER BY h.placed_at_us,h.hold_id",
        (workspace_id, evidence_id),
    ).fetchall()
    return tuple(
        EvidenceLegalHold(
            workspace_id=workspace_id,
            hold_id=str(row[0]),
            evidence_id=evidence_id,
            reason_code=str(row[1]),
            placed_at=_from_us(int(row[2]), str(row[3]), str(row[4])),
        )
        for row in rows
    )


class SemanticRetentionWriter:
    def __init__(self, connection: sqlite3.Connection, workspace_id: str) -> None:
        self.connection = connection
        self.workspace_id = workspace_id

    def _workspace(self, value: str) -> None:
        if value != self.workspace_id:
            raise StorageError("retention record workspace does not match writer")

    def append_policy(self, value: RetentionPolicy) -> None:
        self._workspace(value.workspace_id)
        self.connection.execute(
            "INSERT INTO omnivia_semantic_retention_policies "
            "(workspace_id,policy_version,default_retention_days,created_at_us,"
            "created_at_precision,created_at_provenance,policy_digest) VALUES (?,?,?,?,?,?,?)",
            (
                self.workspace_id,
                value.policy_version,
                value.default_retention_days,
                _to_us(value.created_at),
                value.created_at.precision.value,
                value.created_at.provenance.value,
                retention_policy_digest(value),
            ),
        )

    def place_legal_hold(self, value: EvidenceLegalHold) -> None:
        self._workspace(value.workspace_id)
        self.connection.execute(
            "INSERT INTO omnivia_semantic_evidence_legal_holds "
            "(workspace_id,hold_id,evidence_id,reason_code,placed_at_us,"
            "placed_at_precision,placed_at_provenance,hold_digest) VALUES (?,?,?,?,?,?,?,?)",
            (
                self.workspace_id,
                value.hold_id,
                value.evidence_id,
                value.reason_code,
                _to_us(value.placed_at),
                value.placed_at.precision.value,
                value.placed_at.provenance.value,
                legal_hold_digest(value),
            ),
        )

    def release_legal_hold(self, value: EvidenceLegalHoldRelease) -> None:
        self._workspace(value.workspace_id)
        self.connection.execute(
            "INSERT INTO omnivia_semantic_evidence_legal_hold_releases "
            "(workspace_id,release_id,hold_id,actor_principal_id,released_at_us,"
            "released_at_precision,released_at_provenance,release_digest) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                self.workspace_id,
                value.release_id,
                value.hold_id,
                value.actor_principal_id,
                _to_us(value.released_at),
                value.released_at.precision.value,
                value.released_at.provenance.value,
                legal_hold_release_digest(value),
            ),
        )

    def append_plan(self, value: EvidenceDeletionPlan) -> None:
        self._workspace(value.workspace_id)
        self.connection.execute(
            "INSERT INTO omnivia_semantic_evidence_deletion_plans "
            "(workspace_id,plan_id,evidence_id,policy_version,reason_code,requested_at_us,"
            "requested_at_precision,requested_at_provenance,due_at_us,due_at_precision,"
            "due_at_provenance,plan_state,plan_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.workspace_id,
                value.plan_id,
                value.evidence_id,
                value.policy_version,
                value.reason_code,
                _to_us(value.requested_at),
                value.requested_at.precision.value,
                value.requested_at.provenance.value,
                _to_us(value.due_at),
                value.due_at.precision.value,
                value.due_at.provenance.value,
                value.state.value,
                deletion_plan_digest(value),
            ),
        )
        for ordinal, target in enumerate(value.targets):
            self.connection.execute(
                "INSERT INTO omnivia_semantic_evidence_deletion_targets "
                "(workspace_id,plan_id,ordinal,storage_class,target_ref,target_digest) "
                "VALUES (?,?,?,?,?,?)",
                (
                    self.workspace_id,
                    value.plan_id,
                    ordinal,
                    target.storage_class.value,
                    target.target_ref,
                    target.target_digest,
                ),
            )

    def append_receipt(self, value: EvidenceDeletionReceipt) -> None:
        self._workspace(value.workspace_id)
        self.connection.execute(
            "INSERT INTO omnivia_semantic_evidence_deletion_receipts "
            "(workspace_id,receipt_id,plan_id,reason_code,completed_at_us,completed_at_precision,"
            "completed_at_provenance,deleted_target_count,receipt_digest) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                self.workspace_id,
                value.receipt_id,
                value.plan_id,
                value.reason_code,
                _to_us(value.completed_at),
                value.completed_at.precision.value,
                value.completed_at.provenance.value,
                len(value.target_digests),
                deletion_receipt_digest(value),
            ),
        )


@contextmanager
def semantic_retention_writer(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
) -> Iterator[SemanticRetentionWriter]:
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        yield SemanticRetentionWriter(connection, workspace_id)


def verify_retention_digests(connection: sqlite3.Connection, workspace_id: str) -> None:
    """Verify the content-free retention records after backup/restore."""
    for version, days, created, precision, provenance, stored in connection.execute(
        "SELECT policy_version,default_retention_days,created_at_us,created_at_precision,"
        "created_at_provenance,policy_digest "
        "FROM omnivia_semantic_retention_policies WHERE workspace_id=?",
        (workspace_id,),
    ).fetchall():
        policy_record = RetentionPolicy(
            workspace_id,
            str(version),
            int(days),
            _from_us(int(created), str(precision), str(provenance)),
        )
        if retention_policy_digest(policy_record) != stored:
            raise StorageError("stored retention policy digest verification failed")
    for row in connection.execute(
        "SELECT hold_id,evidence_id,reason_code,placed_at_us,placed_at_precision,"
        "placed_at_provenance,hold_digest FROM omnivia_semantic_evidence_legal_holds "
        "WHERE workspace_id=?",
        (workspace_id,),
    ).fetchall():
        hold_record = EvidenceLegalHold(
            workspace_id=workspace_id,
            hold_id=str(row[0]),
            evidence_id=str(row[1]),
            reason_code=str(row[2]),
            placed_at=_from_us(int(row[3]), str(row[4]), str(row[5])),
        )
        if legal_hold_digest(hold_record) != row[6]:
            raise StorageError("stored legal hold digest verification failed")
    for row in connection.execute(
        "SELECT release_id,hold_id,actor_principal_id,released_at_us,"
        "released_at_precision,released_at_provenance,release_digest "
        "FROM omnivia_semantic_evidence_legal_hold_releases WHERE workspace_id=?",
        (workspace_id,),
    ).fetchall():
        release_record = EvidenceLegalHoldRelease(
            workspace_id=workspace_id,
            release_id=str(row[0]),
            hold_id=str(row[1]),
            actor_principal_id=str(row[2]),
            released_at=_from_us(int(row[3]), str(row[4]), str(row[5])),
        )
        if legal_hold_release_digest(release_record) != row[6]:
            raise StorageError("stored legal hold release digest verification failed")
    plan_rows = connection.execute(
        "SELECT plan_id,evidence_id,policy_version,reason_code,requested_at_us,"
        "requested_at_precision,requested_at_provenance,due_at_us,due_at_precision,"
        "due_at_provenance,plan_state,plan_digest FROM omnivia_semantic_evidence_deletion_plans "
        "WHERE workspace_id=?",
        (workspace_id,),
    ).fetchall()
    for row in plan_rows:
        targets = tuple(
            EvidenceDeletionTarget(
                DeletionStorageClass(target[0]), str(target[1]), str(target[2])
            )
            for target in connection.execute(
                "SELECT storage_class,target_ref,target_digest "
                "FROM omnivia_semantic_evidence_deletion_targets "
                "WHERE workspace_id=? AND plan_id=? ORDER BY ordinal",
                (workspace_id, row[0]),
            ).fetchall()
        )
        plan_record = EvidenceDeletionPlan(
            workspace_id=workspace_id,
            plan_id=str(row[0]),
            evidence_id=str(row[1]),
            policy_version=str(row[2]),
            reason_code=str(row[3]),
            requested_at=_from_us(int(row[4]), str(row[5]), str(row[6])),
            due_at=_from_us(int(row[7]), str(row[8]), str(row[9])),
            state=DeletionPlanState(str(row[10])),
            targets=targets,
        )
        if deletion_plan_digest(plan_record) != row[11]:
            raise StorageError("stored deletion plan digest verification failed")
        for target in targets:
            expected = deletion_target_digest(
                workspace_id, plan_record.evidence_id, target.storage_class
            )
            if (
                target.target_digest != expected
                or target.target_ref != plan_record.evidence_id
            ):
                raise StorageError("stored deletion target digest verification failed")
    for row in connection.execute(
        "SELECT r.receipt_id,r.plan_id,r.reason_code,r.completed_at_us,"
        "r.completed_at_precision,r.completed_at_provenance,r.deleted_target_count,"
        "r.receipt_digest "
        "FROM omnivia_semantic_evidence_deletion_receipts r WHERE r.workspace_id=?",
        (workspace_id,),
    ).fetchall():
        target_digests = tuple(
            str(target_row[0])
            for target_row in connection.execute(
                "SELECT target_digest FROM omnivia_semantic_evidence_deletion_targets "
                "WHERE workspace_id=? AND plan_id=? ORDER BY ordinal",
                (workspace_id, row[1]),
            ).fetchall()
        )
        if int(row[6]) != len(target_digests):
            raise StorageError(
                "stored deletion receipt target count verification failed"
            )
        receipt_record = EvidenceDeletionReceipt(
            workspace_id=workspace_id,
            receipt_id=str(row[0]),
            plan_id=str(row[1]),
            reason_code=str(row[2]),
            completed_at=_from_us(int(row[3]), str(row[4]), str(row[5])),
            target_digests=target_digests,
        )
        if deletion_receipt_digest(receipt_record) != row[7]:
            raise StorageError("stored deletion receipt digest verification failed")


__all__ = [
    "ALL_DELETION_STORAGE_CLASSES",
    "DeletionPlanState",
    "DeletionStorageClass",
    "EvidenceDeletionPlan",
    "EvidenceDeletionReceipt",
    "EvidenceDeletionTarget",
    "EvidenceLegalHold",
    "EvidenceLegalHoldRelease",
    "RetentionPolicy",
    "SemanticRetentionWriter",
    "active_legal_holds",
    "build_deletion_plan",
    "build_deletion_receipt",
    "deletion_plan_digest",
    "deletion_receipt_digest",
    "deletion_target_digest",
    "legal_hold_digest",
    "legal_hold_release_digest",
    "retention_policy_digest",
    "semantic_retention_writer",
    "verify_retention_digests",
]
