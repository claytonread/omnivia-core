"""Service-owned bounded execution of engineering conflict discovery runs.

Memory mutations enqueue exact versions and return. The local transport invokes this
executor between requests, where it holds the service identity and current fencing
generation. Each pass advances a fixed number of indexed record pages; progress is
durable, so restart resumes from the stored record-id watermark.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final

from omnivia_core_runtime.ownership.identity import Clock, ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.engineering_conflicts import (
    DEFAULT_SCAN_RECORD_BUDGET,
    process_oldest_queued_run,
    read_oldest_queued_run,
)
from omnivia_core_runtime.storage.memory import random_identifier
from omnivia_core_runtime.storage.retrieval import local_owner_label_grant

DEFAULT_EXECUTION_BUDGET: Final = 8


@dataclass(frozen=True)
class EngineeringConflictExecutor:
    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    workspace_id: str
    fencing_generation: int
    clock: Clock

    def run_pending(
        self,
        *,
        budget: int = DEFAULT_EXECUTION_BUDGET,
        scan_record_budget: int = DEFAULT_SCAN_RECORD_BUDGET,
    ) -> tuple[str, ...]:
        """Advance at most ``budget`` durable scan pages and never drain forever."""

        advanced: list[str] = []
        try:
            while len(advanced) < budget:
                run = read_oldest_queued_run(
                    self.connection, workspace_id=self.workspace_id
                )
                if run is None:
                    break
                occurred_at_us = max(
                    run.enqueued_at_us,
                    int(self.clock.wall_time().timestamp() * 1_000_000),
                )
                result = process_oldest_queued_run(
                    self.connection,
                    self.identity,
                    workspace_id=self.workspace_id,
                    fencing_generation=self.fencing_generation,
                    label_grant=local_owner_label_grant(
                        principal_id=run.principal_id,
                        workspace_id=self.workspace_id,
                        granted_workspace=self.workspace_id,
                    ),
                    allocate_identifier=random_identifier,
                    occurred_at_us=occurred_at_us,
                    scan_record_budget=scan_record_budget,
                )
                if result is None:
                    break
                advanced.append(result.run.discovery_run_id)
        except (StorageError, sqlite3.Error):
            # Fence loss or connection contention belongs to the service instance,
            # not to the queued run. The next owned pass resumes the same page.
            pass
        return tuple(advanced)


__all__ = ["DEFAULT_EXECUTION_BUDGET", "EngineeringConflictExecutor"]
