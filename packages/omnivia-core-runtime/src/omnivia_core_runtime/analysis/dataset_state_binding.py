"""Storage-bound plan admission over the result-use checkpoint (SPEC-CORE-DATA-001 WP07).

Binds the accepted plan-admission checkpoint to the current, server-read migration 0062
`DatasetStateRecord`. The context is validated first and the workspace comes only from
the subject that validation returns. The requested dataset id must be an exact built-in
identifier before SQLite sees it. The current state is then read exactly once, and the
exact record storage returned is handed to the checkpoint unchanged.

Every binding failure is the one fixed `AnalysisUseAuthorityRefused`: a read that raises,
no row, a record of the wrong type, or a record bound to another workspace or dataset.
The read failure is dropped before the refusal is raised, so no storage text or cause
reaches it. Nothing here reads the stored authority epoch, freshness deadline or any
other stored value as permission, and the resolver stays an argument: this module is an
adapter, not a resolver. Successful deny and warning outcomes return unchanged.

Internal only: nothing here is exported, wired to an operation or used by `analysis.start`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import TypeGuard

from omnivia_core.contracts.v1 import Identifier, is_identifier
from omnivia_core_runtime.analysis.authority import (
    AnalysisUseAuthorityRefused,
    AnalysisUseAuthorityResolver,
    analysis_use_authority_subject_from_context,
)
from omnivia_core_runtime.analysis.result_use_checkpoints import (
    AnalysisResultUseCheckpoint,
    evaluate_plan_admission_checkpoint,
)
from omnivia_core_runtime.service.operations import OperationContext
from omnivia_core_runtime.storage.dataset_state import (
    DatasetStateObservation,
    DatasetStateRecord,
    read_current_state,
)


def evaluate_storage_bound_plan_admission_checkpoint(
    context: OperationContext,
    connection: sqlite3.Connection,
    *,
    dataset_id: str,
    subject_digest: Identifier,
    resolved_use_class: str,
    evaluation_instant: datetime,
    resolver: AnalysisUseAuthorityResolver,
) -> AnalysisResultUseCheckpoint:
    """Evaluate plan admission against `dataset_id`'s current stored state."""
    subject = analysis_use_authority_subject_from_context(context)
    if type(dataset_id) is not str or not is_identifier(dataset_id):
        raise AnalysisUseAuthorityRefused()
    workspace_id = subject.workspace_id
    record: object
    try:
        record = read_current_state(
            connection, workspace_id=workspace_id, dataset_id=dataset_id
        )
    except Exception:  # noqa: BLE001 - any read failure is a refusal, and is dropped
        record = None
    # Raised outside the handler above, so the storage failure is not its context.
    if not _is_bound(record, workspace_id, dataset_id):
        raise AnalysisUseAuthorityRefused()
    return evaluate_plan_admission_checkpoint(
        subject,
        dataset=record,
        subject_digest=subject_digest,
        resolved_use_class=resolved_use_class,
        evaluation_instant=evaluation_instant,
        resolver=resolver,
    )


def _is_bound(
    record: object, workspace_id: str, dataset_id: str
) -> TypeGuard[DatasetStateRecord]:
    """The record is a stored one for exactly this workspace and dataset."""
    if type(record) is not DatasetStateRecord:
        return False
    observation = record.observation
    if type(observation) is not DatasetStateObservation:
        return False
    # Exact built-in strings are proven before any `==`, so no subclass hook runs.
    stored_workspace = record.workspace_id
    stored_dataset = observation.dataset_id
    return (
        type(stored_workspace) is str
        and type(stored_dataset) is str
        and stored_workspace == workspace_id
        and stored_dataset == dataset_id
    )
