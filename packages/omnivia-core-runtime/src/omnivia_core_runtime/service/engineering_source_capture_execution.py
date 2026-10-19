"""Bounded service-owned production of captured engineering source events.

The executor shares the live ``ServiceRunner`` connection, lease and fencing
generation. Each bounded pass coordinates one execution budget across two lanes:
chain-proven recovery of sealed captures left by a crash, and capture of a
registered checkout from local registration state. Durable lane and keyset
cursors keep either lane from starving across bounded passes and service
restarts; see ``run_pending`` for the fairness rule.
Filesystem paths stay inside the trusted capture primitive and never enter an
application request or result.

A trusted watcher may hint that a registered checkout changed (``hint``). A hint
is an in-memory, payload-free advisory: it wakes the next service tick and puts
the named checkout ahead of ordinary rotation, never replacing the durable poll.
Losing a hint (a full set, a restart, an unavailable checkout, a storage or
callback failure) only delays that checkout until the unchanged poll reaches it.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
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
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.service.runner import ServiceRunner
from omnivia_core_runtime.service.source_capture import (
    SourceCaptureRefused,
    capture_working_tree_manifest,
    capture_working_tree_snapshot_owned,
)
from omnivia_core_runtime.storage import engineering_source_producer
from omnivia_core_runtime.storage.connection import StorageError

DEFAULT_EXECUTION_BUDGET: Final = 2
DEFAULT_POLL_INTERVAL_SECONDS: Final = 1.0
#: Hard cap on distinct pending hints. A full set drops the new identity (its
#: checkout is still reached by polling) rather than growing.
MAX_PENDING_HINTS: Final = 64
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
    # Written by request threads through ``hint``, read and drained only by the
    # service-owned tick; ``_hint_lock`` guards just these two fields.
    _hints: dict[tuple[str, str], None] = field(default_factory=dict)
    _wake: bool = False
    _hint_lock: threading.Lock = field(default_factory=threading.Lock)
    # Service-thread only: alternates hinted and rotation units in the checkout
    # lane, so a hint burst cannot starve rotation and rotation cannot starve a hint.
    _hint_turn: bool = True

    def hint(self, repository_id: str, checkout_id: str) -> None:
        """Record that one registered checkout may have changed; safe from any thread.

        Identities only, deduplicated and bounded by ``MAX_PENDING_HINTS``. Even a
        full set wakes the next tick, so a dropped identity is polled soon rather
        than not at all. Registration is the caller's check; ``_take_hinted_checkout``
        re-verifies it against storage before any path is read.
        """

        with self._hint_lock:
            if len(self._hints) < MAX_PENDING_HINTS:
                self._hints.setdefault((repository_id, checkout_id))
            self._wake = True

    def run_pending(
        self,
        *,
        budget: int = DEFAULT_EXECUTION_BUDGET,
        force: bool = False,
    ) -> SourceProducerPass:
        """Run at most ``budget`` recovery/capture units and never drain forever.

        The durable scheduler alternates the first lane before doing any
        filesystem or application work. For ``budget >= 2``, the first lane is
        capped at ``budget - 1`` so the other lane keeps one unit. If either lane
        has no work, the other may use the unused budget. Legacy pre-0058 seals
        are indexed into the queue in a separate bounded batch.
        """

        if budget <= 0:
            return SourceProducerPass(inspected=0, captured=0, committed=0)
        now = self.runner.clock.monotonic()
        with self._hint_lock:
            woken, self._wake = self._wake, False
        if not force and now < self._next_poll and not woken:
            return SourceProducerPass(inspected=0, captured=0, committed=0)
        if now >= self._next_poll or force:
            # A hint-woken early pass leaves the poll schedule alone, so the
            # periodic poll keeps its own cadence.
            self._next_poll = now + max(self.poll_interval_seconds, 0.0)

        tally = _Tally()
        try:
            self._seed_legacy_captures()
            first_lane = self._take_lane_turn()
            first_cap = budget - 1 if budget >= 2 else budget
            if first_lane == "recovery":
                self._run_pending_lane(tally, limit=first_cap)
                self._run_checkout_lane(tally, limit=budget - tally.inspected)
                if tally.inspected < budget:
                    self._run_pending_lane(tally, limit=budget - tally.inspected)
            else:
                self._run_checkout_lane(tally, limit=first_cap)
                self._run_pending_lane(tally, limit=budget - tally.inspected)
                if tally.inspected < budget:
                    self._run_checkout_lane(tally, limit=budget - tally.inspected)
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
        for item in self._take_queue_items(limit=limit):
            tally.inspected += 1
            try:
                if self._snapshot_already_committed(item.snapshot_id, item.stream_id):
                    self._mark_queue_settled(item.snapshot_id)
                    continue
                self._commit(
                    item.repository_id,
                    item.snapshot_id,
                    item.stream_id,
                    expected_frontier=item.expected_frontier,
                    expected_predecessor_snapshot_id=(
                        item.expected_predecessor_snapshot_id
                    ),
                )
            except SourceCaptureRefused:
                self._mark_queue_retry(item.snapshot_id)
                continue
            self._mark_queue_settled(item.snapshot_id)
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
            stream_id = engineering_source_producer.source_stream_id(
                self.runner.workspace_id,
                repository_id,
                self.runner.identity.installation_id,
                checkout_id,
            )
            # A stream with a gap may only accept the missing sealed predecessor.
            # Capturing another head would leave more unusable seals behind. The
            # observed frontier is revalidated at commit time so a concurrent
            # append or gap fails this attempt closed rather than misattaching.
            plan = self._stream_accepts_new_head(repository_id, stream_id)
            if plan is None:
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
            if self._snapshot_already_committed(snapshot_id, stream_id):
                continue
            result = capture_working_tree_snapshot_owned(
                self.runner,
                repository_id=repository_id,
                checkout_root=Path(checkout_hint),
                snapshot_id=snapshot_id,
                manifest=manifest,
                renew_lease=renew_lease,
                producer_expected_frontier=plan.sequence - 1,
                producer_expected_predecessor_snapshot_id=(
                    plan.predecessor_snapshot_id
                ),
            )
            tally.captured += int(result.status == "captured")
            if result.status != "captured":
                # An existing seal is owned by its durable queue row. In
                # particular, a retry must respect available_at_us instead of
                # being redispatched by every checkout turn.
                continue
            try:
                self._commit(
                    repository_id,
                    snapshot_id,
                    stream_id,
                    expected_frontier=plan.sequence - 1,
                    expected_predecessor_snapshot_id=plan.predecessor_snapshot_id,
                )
            except SourceCaptureRefused:
                # The seal stays durable. A later pass may use it only if durable
                # chain metadata names it, or may re-observe the checkout and bind
                # that fresh capture to the then-current frontier.
                self._mark_queue_retry(snapshot_id)
                continue
            else:
                self._mark_queue_settled(snapshot_id)
                tally.committed += 1

    def _seed_legacy_captures(self) -> None:
        connection, workspace_id, installation_id = self._owned_facts()
        identity, generation = self._ownership_token()
        with self.runner.sqlite_gate:
            engineering_source_producer.seed_legacy_captures(
                connection,
                identity,
                workspace_id=workspace_id,
                installation_id=installation_id,
                fencing_generation=generation,
                now_us=self._now_us(),
            )

    def _take_lane_turn(self) -> str:
        connection, workspace_id, installation_id = self._owned_facts()
        identity, generation = self._ownership_token()
        with self.runner.sqlite_gate:
            return engineering_source_producer.take_lane_turn(
                connection,
                identity,
                workspace_id=workspace_id,
                installation_id=installation_id,
                fencing_generation=generation,
                now_us=self._now_us(),
            )

    def _take_queue_items(
        self, *, limit: int
    ) -> tuple[engineering_source_producer.SourceQueueItem, ...]:
        connection, workspace_id, installation_id = self._owned_facts()
        identity, generation = self._ownership_token()
        with self.runner.sqlite_gate:
            return engineering_source_producer.take_queue_items(
                connection,
                identity,
                workspace_id=workspace_id,
                installation_id=installation_id,
                fencing_generation=generation,
                now_us=self._now_us(),
                limit=limit,
            )

    def _next_checkout(self) -> tuple[str, str, str] | None:
        """A hinted checkout on its turn, otherwise the ordinary rotation's next."""

        if self._hint_turn:
            hinted = self._take_hinted_checkout()
            if hinted is not None:
                self._hint_turn = False
                return hinted
        self._hint_turn = True
        return self._take_next_checkout()

    def _take_hinted_checkout(self) -> tuple[str, str, str] | None:
        """Pop pending hints until one names a checkout registered to this service.

        A hint that is stale or was never registered is dropped without spending a
        unit. A popped hint is not requeued: if the pass then fails, the poll recovers.
        The rotation cursor is untouched, so ordinary rotation order is unchanged.
        """

        connection, workspace_id, installation_id = self._owned_facts()
        while True:
            with self._hint_lock:
                if not self._hints:
                    return None
                repository_id, checkout_id = next(iter(self._hints))
                del self._hints[(repository_id, checkout_id)]
            with self.runner.sqlite_gate:
                row = connection.execute(
                    "SELECT repository_id, checkout_id, checkout_hint "
                    "FROM omnivia_engineering_checkouts "
                    "WHERE workspace_id = ? AND installation_id = ? "
                    "AND repository_id = ? AND checkout_id = ?",
                    (workspace_id, installation_id, repository_id, checkout_id),
                ).fetchone()
            if row is not None:
                return str(row[0]), str(row[1]), str(row[2])

    def _take_next_checkout(self) -> tuple[str, str, str] | None:
        connection, workspace_id, installation_id = self._owned_facts()
        identity, generation = self._ownership_token()
        with self.runner.sqlite_gate:
            return engineering_source_producer.take_next_checkout(
                connection,
                identity,
                workspace_id=workspace_id,
                installation_id=installation_id,
                fencing_generation=generation,
                now_us=self._now_us(),
            )

    def _mark_queue_retry(self, snapshot_id: str) -> None:
        connection, workspace_id, installation_id = self._owned_facts()
        identity, generation = self._ownership_token()
        with self.runner.sqlite_gate:
            engineering_source_producer.mark_queue_retry(
                connection,
                identity,
                workspace_id=workspace_id,
                installation_id=installation_id,
                snapshot_id=snapshot_id,
                fencing_generation=generation,
                now_us=self._now_us(),
            )

    def _mark_queue_settled(self, snapshot_id: str) -> None:
        connection, workspace_id, installation_id = self._owned_facts()
        identity, generation = self._ownership_token()
        with self.runner.sqlite_gate:
            engineering_source_producer.mark_queue_settled(
                connection,
                identity,
                workspace_id=workspace_id,
                installation_id=installation_id,
                snapshot_id=snapshot_id,
                fencing_generation=generation,
                now_us=self._now_us(),
            )

    def _snapshot_already_committed(self, snapshot_id: str, stream_id: str) -> bool:
        connection, workspace_id, _installation_id = self._owned_facts()
        with self.runner.sqlite_gate:
            row = connection.execute(
                "SELECT stream_id, manifest_format "
                "FROM omnivia_engineering_source_events "
                "WHERE workspace_id = ? AND snapshot_id = ?",
                (workspace_id, snapshot_id),
            ).fetchone()
        if row is None:
            return False
        if (str(row[0]), str(row[1])) != (stream_id, "captured_v1"):
            raise SourceCaptureRefused(
                "the sealed snapshot is already bound to another source event"
            )
        return True

    def _stream_accepts_new_head(
        self, repository_id: str, stream_id: str
    ) -> _CommitPlan | None:
        """Return the exact stream head a fresh capture may extend.

        ``None`` means a new head must not be attempted (repository mismatch or
        an open gap). The caller persists this plan with the capture seal and
        hands it back to ``_commit`` for revalidation under the fencing gate.
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
                return _CommitPlan(sequence=1, predecessor_snapshot_id=None)
            if str(row[0]) != repository_id or int(row[1]) != int(row[2]):
                return None
            announced = int(row[1])
            return _CommitPlan(
                sequence=announced + 1,
                predecessor_snapshot_id=self._snapshot_at(
                    connection, workspace_id, stream_id, announced
                ),
            )

    def _commit(
        self,
        repository_id: str,
        snapshot_id: str,
        stream_id: str,
        *,
        expected_frontier: int | None = None,
        expected_predecessor_snapshot_id: str | None = None,
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
            if stream_id != engineering_source_producer.source_stream_id(
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
                expected_predecessor_snapshot_id=(
                    expected_predecessor_snapshot_id
                ),
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
        expected_predecessor_snapshot_id: str | None,
    ) -> _CommitPlan:
        """Choose the only append that can extend this stream's durable chain.

        ``expected_frontier`` and ``expected_predecessor_snapshot_id`` are the
        exact head observed before a fresh checkout capture. Both are rechecked
        here. A generic recovered seal has no expected frontier and may only
        initialize an absent stream or fill an exact, named gap; it may never
        become ``announced_sequence + 1`` on a contiguous stream.
        """

        if (
            expected_frontier is None
            and expected_predecessor_snapshot_id is not None
        ) or (
            expected_frontier == 0
            and expected_predecessor_snapshot_id is not None
        ) or (
            expected_frontier is not None
            and expected_frontier > 0
            and expected_predecessor_snapshot_id is None
        ):
            raise SourceCaptureRefused("the captured source intent is incomplete")

        stream = connection.execute(
            "SELECT repository_id, announced_sequence, covered_sequence "
            "FROM omnivia_engineering_source_streams "
            "WHERE workspace_id = ? AND stream_id = ?",
            (workspace_id, stream_id),
        ).fetchone()
        if stream is None:
            if expected_frontier not in (None, 0) or (
                expected_predecessor_snapshot_id is not None
            ):
                raise SourceCaptureRefused(
                    "the captured source stream frontier has changed"
                )
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
            durable_predecessor = self._snapshot_at(
                connection, workspace_id, stream_id, covered
            )
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
            if expected_frontier is not None and (
                missing != expected_frontier + 1
                or durable_predecessor
                != expected_predecessor_snapshot_id
            ):
                raise SourceCaptureRefused(
                    "the captured source stream frontier has changed"
                )
            return _CommitPlan(
                sequence=missing,
                predecessor_snapshot_id=durable_predecessor,
            )

        predecessor = self._snapshot_at(
            connection, workspace_id, stream_id, announced
        )
        if (
            expected_frontier is None
            or announced != expected_frontier
            or predecessor != expected_predecessor_snapshot_id
        ):
            raise SourceCaptureRefused(
                "an unmatched sealed capture cannot extend the stream head"
            )
        return _CommitPlan(
            sequence=announced + 1,
            predecessor_snapshot_id=predecessor,
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

    def _now_us(self) -> int:
        return max(1, int(self.runner.clock.wall_time().timestamp() * 1_000_000))

    def _ownership_token(self) -> tuple[ServiceInstanceIdentity, int]:
        if self.runner.identity is None or self.runner.generation is None:
            raise SourceCaptureRefused("workspace ownership is not active")
        return self.runner.identity, self.runner.generation

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
    "MAX_PENDING_HINTS",
    "EngineeringSourceCaptureExecutor",
    "SourceProducerPass",
]
