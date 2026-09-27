"""Engineering applicability, priorities and review attestations
(SPEC-CORE-ENGMEM-001, plan PR-E/PR-G; spec §13, §14, §15).

Every write here runs inside the fenced mutation transaction the mutation
coordinator opens; the guard triggers of migration 0049 make an unguarded write
impossible. The module is the only writer for the four families of migration
0049.

Applicability rules (§15), enforced here:

- the latest appended assessment for one (record version, target snapshot) is
  the stored value. History is append-only and never an in-place flag;
- nothing here mints `matched`. Only a qualified dependency validation can show
  that a target is equivalent, and none exists yet. Registration, recency, a
  review outcome and a `review_evidence_id` are not that proof. The newest
  registered target is `unknown`, and an older registered target is
  `potentially_stale`;
- a review without validated evidence cannot clear `potentially_stale` or
  `invalid` (§15.5), and a caller-supplied evidence id is not validated
  evidence. The earlier status is carried forward;
- a stored `matched` (a legacy row) is not replayed. It is re-assessed
  under the same rules, and the history row is left unchanged;
- a target that is unregistered or belongs to another repository is `unknown`.

Priority rules (§13.3): a preference is per principal, per exact target; it
never changes governed state, and `preferred` influences selection only after
authorisation — the search handler applies it as a stable reorder of the
already-ranked page, never as a score.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

_PRIORITY_TABLE: Final = "omnivia_engineering_context_priorities"
_ASSESSMENTS_TABLE: Final = "omnivia_engineering_assessments"
_ATTESTATIONS_TABLE: Final = "omnivia_engineering_review_attestations"

_STATUSES: Final[tuple[str, ...]] = (
    "matched",
    "potentially_stale",
    "invalid",
    "unknown",
)


class AssessmentPreconditionFailed(RuntimeError):
    """The expected assessment version is not the target's current one."""


class RecordVersionNotFound(LookupError):
    """The exact record version a review or preference names is not resolvable."""


def set_priority(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    principal_id: str,
    target_record_id: str,
    target_version: str,
    priority: str,
    expires_at_us: int | None,
    updated_at_us: int,
) -> str:
    """Upsert one principal's own preference for one exact target."""
    existing = connection.execute(
        f"SELECT 1 FROM {_PRIORITY_TABLE} "
        "WHERE workspace_id = ? AND principal_id = ? "
        "AND target_record_id = ? AND target_version = ?",
        (workspace_id, principal_id, target_record_id, target_version),
    ).fetchone()
    if existing is not None:
        connection.execute(
            f"UPDATE {_PRIORITY_TABLE} SET priority = ?, expires_at_us = ?, "
            "updated_at_us = ?, audit_ref = ? "
            "WHERE workspace_id = ? AND principal_id = ? "
            "AND target_record_id = ? AND target_version = ?",
            (
                priority,
                expires_at_us,
                int(updated_at_us),
                settlement.audit_ref,
                workspace_id,
                principal_id,
                target_record_id,
                target_version,
            ),
        )
        return "updated"
    connection.execute(
        f"INSERT INTO {_PRIORITY_TABLE} "
        "(workspace_id, principal_id, target_record_id, target_version, priority, "
        "expires_at_us, updated_at_us, audit_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            principal_id,
            target_record_id,
            target_version,
            priority,
            expires_at_us,
            int(updated_at_us),
            settlement.audit_ref,
        ),
    )
    return "created"


def read_priority(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    principal_id: str,
    target_record_id: str,
    target_version: str,
) -> str | None:
    row = connection.execute(
        f"SELECT priority FROM {_PRIORITY_TABLE} "
        "WHERE workspace_id = ? AND principal_id = ? "
        "AND target_record_id = ? AND target_version = ?",
        (workspace_id, principal_id, target_record_id, target_version),
    ).fetchone()
    return None if row is None else str(row[0])


def preferred_targets(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    principal_id: str,
    now_us: int,
) -> frozenset[tuple[str, str]]:
    """The caller's preferred (record_id, version) pairs whose expiry has not passed."""
    rows = connection.execute(
        f"SELECT target_record_id, target_version, expires_at_us "
        f"FROM {_PRIORITY_TABLE} "
        "WHERE workspace_id = ? AND principal_id = ? AND priority = 'preferred'",
        (workspace_id, principal_id),
    ).fetchall()
    return frozenset(
        (str(row[0]), str(row[1]))
        for row in rows
        if row[2] is None or int(row[2]) > now_us
    )


def record_assessment(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    assessment_id: str,
    record_id: str,
    version: str,
    target_snapshot_id: str,
    status: str,
    basis: str,
    assessed_at_us: int,
) -> str:
    assert status in _STATUSES
    connection.execute(
        f"INSERT INTO {_ASSESSMENTS_TABLE} "
        "(workspace_id, assessment_id, record_id, version, target_snapshot_id, "
        "status, basis, assessed_at_us, audit_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            assessment_id,
            record_id,
            version,
            target_snapshot_id,
            status,
            basis,
            int(assessed_at_us),
            settlement.audit_ref,
        ),
    )
    return assessment_id


def latest_assessment(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    record_id: str,
    version: str,
    target_snapshot_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        f"SELECT assessment_id, status, basis, assessed_at_us "
        f"FROM {_ASSESSMENTS_TABLE} "
        "WHERE workspace_id = ? AND record_id = ? AND version = ? "
        "AND target_snapshot_id = ? "
        "ORDER BY assessed_at_us DESC, assessment_id DESC LIMIT 1",
        (workspace_id, record_id, version, target_snapshot_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "assessment_id": row[0],
        "status": row[1],
        "basis": row[2],
        "assessed_at_us": row[3],
    }


def record_attestation(
    connection: sqlite3.Connection,
    settlement: Any,
    *,
    workspace_id: str,
    attestation_id: str,
    record_id: str,
    version: str,
    target_snapshot_id: str,
    outcome: str,
    review_evidence_id: str | None,
    recorded_at_us: int,
) -> str:
    connection.execute(
        f"INSERT INTO {_ATTESTATIONS_TABLE} "
        "(workspace_id, attestation_id, record_id, version, target_snapshot_id, "
        "outcome, review_evidence_id, recorded_at_us, audit_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workspace_id,
            attestation_id,
            record_id,
            version,
            target_snapshot_id,
            outcome,
            review_evidence_id,
            int(recorded_at_us),
            settlement.audit_ref,
        ),
    )
    return attestation_id


def _newest_registered_snapshot(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    repository_id: str,
) -> str | None:
    row = connection.execute(
        "SELECT snapshot_id FROM omnivia_engineering_snapshots "
        "WHERE workspace_id = ? AND repository_id = ? "
        "ORDER BY captured_at_us DESC, snapshot_id DESC LIMIT 1",
        (workspace_id, repository_id),
    ).fetchone()
    return None if row is None else str(row[0])


def assess_against_registered_head(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    claimed_repository_id: str | None,
    target_snapshot_id: str,
    prior_status: str | None = None,
) -> str:
    """The v1 assessment (§15.4): a conservative subset with no dependency check.

    This never returns `matched`. The registry records which snapshots exist, but
    that does not show a target is equivalent to the record's dependencies.
    Without qualified dependency validation the result is `unknown` for the
    newest registered snapshot of the record's claimed repository. It is
    `potentially_stale` for an older registered snapshot, and `unknown` when the
    record claims no repository or the target is not registered for it.

    `prior_status` is the stored assessment. A prior `invalid` or
    `potentially_stale` is returned unchanged, because nothing here is validated
    evidence (§15.5). A prior `matched` or `unknown` gets the result above, so
    a legacy `matched` row is never certified again.
    """
    if prior_status in ("invalid", "potentially_stale"):
        return prior_status
    if claimed_repository_id is None:
        return "unknown"
    newest = _newest_registered_snapshot(
        connection, workspace_id=workspace_id, repository_id=claimed_repository_id
    )
    if newest is None or target_snapshot_id == newest:
        return "unknown"
    known = connection.execute(
        "SELECT 1 FROM omnivia_engineering_snapshots "
        "WHERE workspace_id = ? AND repository_id = ? AND snapshot_id = ?",
        (workspace_id, claimed_repository_id, target_snapshot_id),
    ).fetchone()
    return "potentially_stale" if known is not None else "unknown"
