"""Bounded service-owned production of captured engineering source events.

The executor shares the live ``ServiceRunner`` connection, lease and fencing
generation. It first commits any sealed capture left behind by a crash, then may
capture one registered checkout from local registration state. Filesystem paths stay
inside the trusted capture primitive and never enter an application request or result.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, Protocol

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    CapabilityRequirement,
    ClientIdentity,
    RequestEnvelope,
    RequestMetadata,
    SuccessResponseEnvelope,
)
from omnivia_core_runtime.service.runner import ServiceRunner
from omnivia_core_runtime.service.source_capture import (
    SourceCaptureRefused,
    capture_working_tree_manifest,
    capture_working_tree_snapshot_owned,
)
from omnivia_core_runtime.storage.connection import StorageError

DEFAULT_EXECUTION_BUDGET: Final = 2
DEFAULT_POLL_INTERVAL_SECONDS: Final = 1.0
_OPERATION: Final = "engineering.source.capture.commit"
_PURPOSE: Final = "engineering_source"
_SCOPE: Final = "engineering:source"
_CAPABILITY: Final = "engineering.source"
_CLIENT: Final = ClientIdentity(id="omnivia-core-source-producer", version="1.0.0")


class _ApplicationDispatch(Protocol):
    def dispatch(self, request: RequestEnvelope) -> Any: ...


def _derived(prefix: str, *parts: str) -> str:
    digest = sha256("|".join(parts).encode("utf-8")).hexdigest()[:40]
    return f"{prefix}-{digest}"


@dataclass(frozen=True, slots=True)
class SourceProducerPass:
    """A redacted pass summary: bounded counts only, with no checkout facts."""

    inspected: int
    captured: int
    committed: int


@dataclass
class EngineeringSourceCaptureExecutor:
    """Produce captured source events on the live service's sole owning thread."""

    runner: ServiceRunner
    application: _ApplicationDispatch
    principal_id: str
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    _next_poll: float = 0.0
    _checkout_cursor: str | None = None

    def run_pending(
        self,
        *,
        budget: int = DEFAULT_EXECUTION_BUDGET,
        force: bool = False,
    ) -> SourceProducerPass:
        """Run at most ``budget`` recovery/capture units and never drain forever."""

        if budget <= 0:
            return SourceProducerPass(inspected=0, captured=0, committed=0)
        now = self.runner.clock.monotonic()
        if not force and now < self._next_poll:
            return SourceProducerPass(inspected=0, captured=0, committed=0)
        self._next_poll = now + max(self.poll_interval_seconds, 0.0)
        inspected = captured = committed = 0
        try:
            while inspected < budget:
                pending = self._pending_seal()
                if pending is not None:
                    repository_id, snapshot_id, stream_id = pending
                    inspected += 1
                    self._commit(repository_id, snapshot_id, stream_id)
                    committed += 1
                    continue
                checkout = self._next_checkout()
                if checkout is None:
                    break
                repository_id, checkout_id, checkout_hint = checkout
                inspected += 1
                def renew_lease() -> bool:
                    return self.runner.renew_lease_if_due(gate_already_held=True)

                renew_lease()
                manifest = capture_working_tree_manifest(
                    checkout_root=Path(checkout_hint)
                )
                renew_lease()
                manifest_digest = manifest.manifest_digest
                assert self.runner.workspace_id is not None
                assert self.runner.identity is not None
                snapshot_id = _derived(
                    "src-snapshot",
                    self.runner.workspace_id,
                    repository_id,
                    self.runner.identity.installation_id,
                    checkout_id,
                    manifest_digest,
                )
                stream_id = _derived(
                    "src-stream",
                    self.runner.workspace_id,
                    repository_id,
                    self.runner.identity.installation_id,
                    checkout_id,
                )
                if self._snapshot_already_committed(snapshot_id):
                    continue
                result = capture_working_tree_snapshot_owned(
                    self.runner,
                    repository_id=repository_id,
                    checkout_root=Path(checkout_hint),
                    snapshot_id=snapshot_id,
                    manifest=manifest,
                    renew_lease=renew_lease,
                )
                captured += int(result.status == "captured")
                self._commit(repository_id, snapshot_id, stream_id)
                committed += 1
        except (StorageError, sqlite3.Error):
            # Lost ownership and SQLite contention belong to this service pass, not
            # to a capture. Durable headers/events remain the recovery truth.
            pass
        except SourceCaptureRefused:
            # A local checkout may be temporarily unavailable or have moved. No
            # durable failure verdict is invented; the next bounded poll retries.
            pass
        return SourceProducerPass(
            inspected=inspected, captured=captured, committed=committed
        )

    def _pending_seal(self) -> tuple[str, str, str] | None:
        connection, workspace_id, installation_id = self._owned_facts()
        row = connection.execute(
            "SELECT c.repository_id, c.snapshot_id, c.checkout_id "
            "FROM omnivia_engineering_snapshot_captures c "
            "WHERE c.workspace_id = ? AND c.installation_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM omnivia_engineering_source_events e "
            " WHERE e.workspace_id = c.workspace_id AND e.snapshot_id = c.snapshot_id) "
            "ORDER BY c.captured_at_us, c.snapshot_id LIMIT 1",
            (workspace_id, installation_id),
        ).fetchone()
        if row is None:
            return None
        repository_id, snapshot_id, checkout_id = map(str, row)
        return (
            repository_id,
            snapshot_id,
            _derived(
                "src-stream",
                workspace_id,
                repository_id,
                installation_id,
                checkout_id,
            ),
        )

    def _next_checkout(self) -> tuple[str, str, str] | None:
        connection, workspace_id, installation_id = self._owned_facts()
        cursor = self._checkout_cursor
        row = connection.execute(
            "SELECT repository_id, checkout_id, checkout_hint "
            "FROM omnivia_engineering_checkouts "
            "WHERE workspace_id = ? AND installation_id = ? "
            "AND (? IS NULL OR checkout_id > ?) ORDER BY checkout_id LIMIT 1",
            (workspace_id, installation_id, cursor, cursor),
        ).fetchone()
        if row is None and cursor is not None:
            row = connection.execute(
                "SELECT repository_id, checkout_id, checkout_hint "
                "FROM omnivia_engineering_checkouts "
                "WHERE workspace_id = ? AND installation_id = ? "
                "ORDER BY checkout_id LIMIT 1",
                (workspace_id, installation_id),
            ).fetchone()
        if row is None:
            self._checkout_cursor = None
            return None
        repository_id, checkout_id, checkout_hint = map(str, row)
        self._checkout_cursor = checkout_id
        return repository_id, checkout_id, checkout_hint

    def _snapshot_already_committed(self, snapshot_id: str) -> bool:
        connection, workspace_id, _installation_id = self._owned_facts()
        return (
            connection.execute(
                "SELECT 1 FROM omnivia_engineering_source_events "
                "WHERE workspace_id = ? AND snapshot_id = ?",
                (workspace_id, snapshot_id),
            ).fetchone()
            is not None
        )

    def _commit(self, repository_id: str, snapshot_id: str, stream_id: str) -> None:
        connection, workspace_id, installation_id = self._owned_facts()
        seal = connection.execute(
            "SELECT repository_id, installation_id, rich_manifest_digest "
            "FROM omnivia_engineering_snapshot_captures "
            "WHERE workspace_id = ? AND snapshot_id = ?",
            (workspace_id, snapshot_id),
        ).fetchone()
        if seal is None or tuple(map(str, seal[:2])) != (
            repository_id,
            installation_id,
        ):
            raise SourceCaptureRefused("the sealed source capture is unavailable")
        stream = connection.execute(
            "SELECT announced_sequence FROM omnivia_engineering_source_streams "
            "WHERE workspace_id = ? AND stream_id = ?",
            (workspace_id, stream_id),
        ).fetchone()
        sequence = 1 if stream is None else int(stream[0]) + 1
        payload: dict[str, object] = {
            "repository_id": repository_id,
            "stream_id": stream_id,
            "sequence": sequence,
            "snapshot_id": snapshot_id,
            "expected_manifest_digest": str(seal[2]),
        }
        if sequence > 1:
            previous = connection.execute(
                "SELECT snapshot_id FROM omnivia_engineering_source_events "
                "WHERE workspace_id = ? AND stream_id = ? AND sequence = ?",
                (workspace_id, stream_id, sequence - 1),
            ).fetchone()
            if previous is None:
                raise SourceCaptureRefused("the source stream predecessor is unavailable")
            payload["predecessor"] = {
                "sequence": sequence - 1,
                "snapshot_id": str(previous[0]),
            }
        request_id = _derived("req-source", workspace_id, stream_id, snapshot_id)
        response = self.application.dispatch(
            RequestEnvelope(
                operation=_OPERATION,
                metadata=RequestMetadata(
                    request_id=request_id,
                    correlation_id=_derived(
                        "cor-source", workspace_id, stream_id, snapshot_id
                    ),
                    trace_id=_derived(
                        "trc-source", workspace_id, stream_id, snapshot_id
                    ),
                    api_version=CONTRACT_VERSION,
                    client=_CLIENT,
                    workspace_id=workspace_id,
                    scopes=(_SCOPE,),
                    purpose=_PURPOSE,
                    required_capabilities=(
                        CapabilityRequirement(
                            id=_CAPABILITY, minimum_version="1.0", required=True
                        ),
                    ),
                    idempotency_key=_derived(
                        "idem-source", workspace_id, stream_id, snapshot_id
                    ),
                    mutation_precondition=None,
                    principal_claim=None,
                ),
                input=payload,
            )
        )
        if not isinstance(response, SuccessResponseEnvelope):
            raise SourceCaptureRefused("the sealed source capture could not be committed")

    def _owned_facts(self) -> tuple[sqlite3.Connection, str, str]:
        if (
            self.runner.connection is None
            or self.runner.workspace_id is None
            or self.runner.identity is None
            or self.runner.generation is None
        ):
            raise SourceCaptureRefused("workspace ownership is not active")
        return (
            self.runner.connection,
            self.runner.workspace_id,
            self.runner.identity.installation_id,
        )


__all__ = [
    "DEFAULT_EXECUTION_BUDGET",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "EngineeringSourceCaptureExecutor",
    "SourceProducerPass",
]
