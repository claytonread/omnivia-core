"""Bounded service-owned production of captured engineering source events.

The executor shares the live ``ServiceRunner`` connection, lease and fencing
generation. Each bounded pass coordinates one execution budget across two lanes:
chain-proven recovery of sealed captures left by a crash, and capture of a
registered checkout from local registration state. While one executor remains
live, neither lane may starve the other across bounded passes; see
``run_pending`` for the fairness rule.
Filesystem paths stay inside the trusted capture primitive and never enter an
application request or result.
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


@dataclass(frozen=True, slots=True)
class _CommitPlan:
    sequence: int
    predecessor_snapshot_id: str | None


@dataclass(slots=True)
class _Tally:
    """Mutable running counts for a pass, shared across lane calls.

    Mutating in place (instead of returning counts) means partial progress
    from a lane survives even if that lane raises mid-loop.
    """

    inspected: int = 0
    captured: int = 0
    committed: int = 0


@dataclass
class EngineeringSourceCaptureExecutor:
    """Produce captured source events on the live service's sole owning thread."""

    runner: ServiceRunner
    application: _ApplicationDispatch
    principal_id: str
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    _next_poll: float = 0.0
    _checkout_cursor: str | None = None
    _seal_cursor: tuple[int, str] | None = None
    _pending_turn: bool = True

    def run_pending(
        self,
        *,
        budget: int = DEFAULT_EXECUTION_BUDGET,
        force: bool = False,
    ) -> SourceProducerPass:
        """Run at most ``budget`` recovery/capture units and never drain forever.

        The budget is split across two lanes so neither can starve the other:
        pending-seal recovery and live-checkout capture. For ``budget >= 2``,
        pending recovery is capped at ``budget - 1`` so at least one unit is
        always available to checkout capture when it has work; the reserved
        unit is handed back to pending if checkout turns out to have none.
        For ``budget == 1`` there is no unit to reserve, so lane priority
        alternates every pass instead (``_pending_turn``, in-memory only -- a
        restart resets it, but the following pass still reaches the other
        lane). If a preferred lane has no work, the other lane may use the
        unit rather than idling. Durable rotation across restarts needs
        scheduler schema; this in-memory scheme is the migration-free
        safeguard.
        """

        if budget <= 0:
            return SourceProducerPass(inspected=0, captured=0, committed=0)
        now = self.runner.clock.monotonic()
        if not force and now < self._next_poll:
            return SourceProducerPass(inspected=0, captured=0, committed=0)
        self._next_poll = now + max(self.poll_interval_seconds, 0.0)

        if budget == 1:
            pending_first = self._pending_turn
            self._pending_turn = not self._pending_turn
        else:
            pending_first = True

        tally = _Tally()
        try:
            first_cap = budget - 1 if budget >= 2 else budget
            if pending_first:
                self._run_pending_lane(tally, limit=first_cap)
                self._run_checkout_lane(tally, limit=budget - tally.inspected)
                if tally.inspected < budget:
                    self._run_pending_lane(tally, limit=budget - tally.inspected)
            else:
                self._run_checkout_lane(tally, limit=first_cap)
                self._run_pending_lane(tally, limit=budget - tally.inspected)
        except (StorageError, sqlite3.Error):
            # Lost ownership and SQLite contention belong to this service pass, not
            # to a capture. Durable headers/events remain the recovery truth.
            pass
        except SourceCaptureRefused:
            # A local checkout may be temporarily unavailable or have moved. No
            # durable failure verdict is invented; the next bounded poll retries.
            pass
        return SourceProducerPass(
            inspected=tally.inspected, captured=tally.captured, committed=tally.committed
        )

    def _run_pending_lane(self, tally: _Tally, *, limit: int) -> None:
        """Attempt at most ``limit`` pending-seal recoveries."""

        if limit <= 0:
            return
        # Read one bounded candidate batch once. The in-memory cursor prevents a
        # refused oldest seal from consuming every later pass. Durable rotation
        # across restarts needs scheduler schema; this is the migration-free
        # safeguard.
        for repository_id, snapshot_id, stream_id in self._pending_seals(limit=limit):
            tally.inspected += 1
            try:
                self._commit(repository_id, snapshot_id, stream_id)
            except SourceCaptureRefused:
                continue
            tally.committed += 1

    def _run_checkout_lane(self, tally: _Tally, *, limit: int) -> None:
        """Attempt at most ``limit`` fresh registered-checkout captures."""

        consumed = 0
        while consumed < limit:
            checkout = self._next_checkout()
            if checkout is None:
                break
            repository_id, checkout_id, checkout_hint = checkout
            tally.inspected += 1
            consumed += 1

            assert self.runner.workspace_id is not None
            assert self.runner.identity is not None
            stream_id = _derived(
                "src-stream",
                self.runner.workspace_id,
                repository_id,
                self.runner.identity.installation_id,
                checkout_id,
            )
            # A stream with a gap may only accept the missing sealed predecessor.
            # Capturing another head would leave more unusable seals behind. The
            # observed frontier is revalidated at commit time so a concurrent
            # append or gap fails this attempt closed rather than misattaching.
            expected_frontier = self._stream_accepts_new_head(repository_id, stream_id)
            if expected_frontier is None:
                continue

            def renew_lease() -> bool:
                return self.runner.renew_lease_if_due()

            renew_lease()
            manifest = capture_working_tree_manifest(
                checkout_root=Path(checkout_hint), heartbeat=renew_lease
            )
            renew_lease()
            manifest_digest = manifest.manifest_digest
            snapshot_id = _derived(
                "src-snapshot",
                self.runner.workspace_id,
                repository_id,
                self.runner.identity.installation_id,
                checkout_id,
                manifest_digest,
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
            tally.captured += int(result.status == "captured")
            try:
                self._commit(
                    repository_id,
                    snapshot_id,
                    stream_id,
                    expected_frontier=expected_frontier,
                )
            except SourceCaptureRefused:
                # The seal stays durable. A later pass may use it only if durable
                # chain metadata names it, or may re-observe the checkout and bind
                # that fresh capture to the then-current frontier.
                continue
            else:
                tally.committed += 1

    def _pending_seals(self, *, limit: int) -> tuple[tuple[str, str, str], ...]:
        connection, workspace_id, installation_id = self._owned_facts()
        cursor = self._seal_cursor
        with self.runner.sqlite_gate:
            rows = connection.execute(
                "SELECT c.repository_id, c.snapshot_id, c.checkout_id, "
                "c.captured_at_us "
                "FROM omnivia_engineering_snapshot_captures c "
                "WHERE c.workspace_id = ? AND c.installation_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM omnivia_engineering_source_events e "
                " WHERE e.workspace_id = c.workspace_id "
                "AND e.snapshot_id = c.snapshot_id) "
                "AND (? IS NULL OR c.captured_at_us > ? OR "
                "(c.captured_at_us = ? AND c.snapshot_id > ?)) "
                "ORDER BY c.captured_at_us, c.snapshot_id LIMIT ?",
                (
                    workspace_id,
                    installation_id,
                    None if cursor is None else cursor[1],
                    None if cursor is None else cursor[0],
                    None if cursor is None else cursor[0],
                    None if cursor is None else cursor[1],
                    limit,
                ),
            ).fetchall()
            if not rows and cursor is not None:
                rows = connection.execute(
                    "SELECT c.repository_id, c.snapshot_id, c.checkout_id, "
                    "c.captured_at_us "
                    "FROM omnivia_engineering_snapshot_captures c "
                    "WHERE c.workspace_id = ? AND c.installation_id = ? "
                    "AND NOT EXISTS (SELECT 1 "
                    "FROM omnivia_engineering_source_events e "
                    "WHERE e.workspace_id = c.workspace_id "
                    "AND e.snapshot_id = c.snapshot_id) "
                    "ORDER BY c.captured_at_us, c.snapshot_id LIMIT ?",
                    (workspace_id, installation_id, limit),
                ).fetchall()
        if rows:
            self._seal_cursor = (int(rows[-1][3]), str(rows[-1][1]))
        else:
            self._seal_cursor = None
        return tuple(
            (
                str(repository_id),
                str(snapshot_id),
                _derived(
                    "src-stream",
                    workspace_id,
                    str(repository_id),
                    installation_id,
                    str(checkout_id),
                ),
            )
            for repository_id, snapshot_id, checkout_id, _captured_at_us in rows
        )

    def _next_checkout(self) -> tuple[str, str, str] | None:
        connection, workspace_id, installation_id = self._owned_facts()
        cursor = self._checkout_cursor
        with self.runner.sqlite_gate:
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
        with self.runner.sqlite_gate:
            return (
                connection.execute(
                    "SELECT 1 FROM omnivia_engineering_source_events "
                    "WHERE workspace_id = ? AND snapshot_id = ?",
                    (workspace_id, snapshot_id),
                ).fetchone()
                is not None
            )

    def _stream_accepts_new_head(
        self, repository_id: str, stream_id: str
    ) -> int | None:
        """Return the announced-sequence frontier a fresh capture may extend.

        ``None`` means a new head must not be attempted (repository mismatch or
        an open gap); ``0`` means the stream does not exist yet. The caller
        must hand this value back to ``_commit`` so the frontier is revalidated
        at commit time under the fencing gate.
        """
        connection, workspace_id, _installation_id = self._owned_facts()
        with self.runner.sqlite_gate:
            row = connection.execute(
                "SELECT repository_id, announced_sequence, covered_sequence "
                "FROM omnivia_engineering_source_streams "
                "WHERE workspace_id = ? AND stream_id = ?",
                (workspace_id, stream_id),
            ).fetchone()
        if row is None:
            return 0
        if str(row[0]) != repository_id or int(row[1]) != int(row[2]):
            return None
        return int(row[1])

    def _commit(
        self,
        repository_id: str,
        snapshot_id: str,
        stream_id: str,
        *,
        expected_frontier: int | None = None,
    ) -> None:
        connection, workspace_id, installation_id = self._owned_facts()
        with self.runner.sqlite_gate:
            seal = connection.execute(
                "SELECT repository_id, installation_id, checkout_id, "
                "rich_manifest_digest FROM omnivia_engineering_snapshot_captures "
                "WHERE workspace_id = ? AND snapshot_id = ?",
                (workspace_id, snapshot_id),
            ).fetchone()
            if seal is None or tuple(map(str, seal[:2])) != (
                repository_id,
                installation_id,
            ):
                raise SourceCaptureRefused("the sealed source capture is unavailable")
            checkout_id = str(seal[2])
            if stream_id != _derived(
                "src-stream",
                workspace_id,
                repository_id,
                installation_id,
                checkout_id,
            ):
                raise SourceCaptureRefused("the sealed source stream is unavailable")
            plan = self._commit_plan(
                connection,
                workspace_id=workspace_id,
                repository_id=repository_id,
                installation_id=installation_id,
                checkout_id=checkout_id,
                stream_id=stream_id,
                snapshot_id=snapshot_id,
                expected_frontier=expected_frontier,
            )
            payload: dict[str, object] = {
                "repository_id": repository_id,
                "stream_id": stream_id,
                "sequence": plan.sequence,
                "snapshot_id": snapshot_id,
                "expected_manifest_digest": str(seal[3]),
            }
            if plan.predecessor_snapshot_id is not None:
                payload["predecessor"] = {
                    "sequence": plan.sequence - 1,
                    "snapshot_id": plan.predecessor_snapshot_id,
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
                                id=_CAPABILITY,
                                minimum_version="1.0",
                                required=True,
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
            raise SourceCaptureRefused(
                "the sealed source capture could not be committed"
            )

    def _commit_plan(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        repository_id: str,
        installation_id: str,
        checkout_id: str,
        stream_id: str,
        snapshot_id: str,
        expected_frontier: int | None,
    ) -> _CommitPlan:
        """Choose the only append that can extend this stream's durable chain.

        ``expected_frontier`` is the announced-sequence frontier observed
        before a fresh checkout capture, or ``None`` for pending-seal recovery.
        An arbitrary recovered seal may only ever fill an exact, named gap; it
        may never become ``announced_sequence + 1`` on a contiguous stream.
        """

        stream = connection.execute(
            "SELECT repository_id, announced_sequence, covered_sequence "
            "FROM omnivia_engineering_source_streams "
            "WHERE workspace_id = ? AND stream_id = ?",
            (workspace_id, stream_id),
        ).fetchone()
        if stream is None:
            return _CommitPlan(sequence=1, predecessor_snapshot_id=None)
        if str(stream[0]) != repository_id:
            raise SourceCaptureRefused(
                "the source stream belongs to another repository"
            )
        origin = connection.execute(
            "SELECT repository_id, installation_id, checkout_id "
            "FROM omnivia_engineering_source_stream_origins "
            "WHERE workspace_id = ? AND stream_id = ?",
            (workspace_id, stream_id),
        ).fetchone()
        if origin is None or tuple(map(str, origin)) != (
            repository_id,
            installation_id,
            checkout_id,
        ):
            raise SourceCaptureRefused("the source stream origin is unavailable")

        announced, covered = int(stream[1]), int(stream[2])
        if covered < announced:
            missing = covered + 1
            successor = connection.execute(
                "SELECT sequence, predecessor_snapshot_id "
                "FROM omnivia_engineering_source_events "
                "WHERE workspace_id = ? AND stream_id = ? AND sequence > ? "
                "ORDER BY sequence LIMIT 1",
                (workspace_id, stream_id, covered),
            ).fetchone()
            if (
                successor is None
                or int(successor[0]) != missing + 1
                or successor[1] is None
                or str(successor[1]) != snapshot_id
            ):
                raise SourceCaptureRefused(
                    "the sealed capture is not the next missing stream predecessor"
                )
            return _CommitPlan(
                sequence=missing,
                predecessor_snapshot_id=self._snapshot_at(
                    connection, workspace_id, stream_id, covered
                ),
            )

        if expected_frontier is None or announced != expected_frontier:
            raise SourceCaptureRefused(
                "an unmatched sealed capture cannot extend the stream head"
            )
        return _CommitPlan(
            sequence=announced + 1,
            predecessor_snapshot_id=self._snapshot_at(
                connection, workspace_id, stream_id, announced
            ),
        )

    @staticmethod
    def _snapshot_at(
        connection: sqlite3.Connection,
        workspace_id: str,
        stream_id: str,
        sequence: int,
    ) -> str | None:
        if sequence == 0:
            return None
        row = connection.execute(
            "SELECT snapshot_id FROM omnivia_engineering_source_events "
            "WHERE workspace_id = ? AND stream_id = ? AND sequence = ?",
            (workspace_id, stream_id, sequence),
        ).fetchone()
        if row is None:
            raise SourceCaptureRefused("the source stream predecessor is unavailable")
        return str(row[0])

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
