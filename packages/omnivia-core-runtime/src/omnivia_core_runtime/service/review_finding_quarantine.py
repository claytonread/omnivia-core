"""Service seam for evidence-only review finding quarantine (DEV-REQ-176).

The one internal entry point the later Runtime completion gate will call. It opens the fenced write
transaction every guarded table requires, so a writer whose generation has gone stale is refused
before anything is written, and it commits only the quarantine row. It is not an operation: no
catalogue entry, CLI, MCP or Platform surface reaches it in this slice.
"""

from __future__ import annotations

import sqlite3

from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.review_finding_quarantine import (
    QuarantinedFinding,
    ReviewFindingEnvelope,
    record_finding,
)


def quarantine_review_finding(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    idempotency_key: str,
    envelope: ReviewFindingEnvelope,
) -> QuarantinedFinding:
    """Quarantine one finding as evidence under current authority and return its record.

    The transaction writes only `omnivia_review_finding_quarantines`. A stale `fencing_generation`
    raises `StaleGeneration` on entry or before commit, and any refusal rolls the whole fence back.
    """
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as fenced:
        return record_finding(
            fenced,
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
            envelope=envelope,
        )
