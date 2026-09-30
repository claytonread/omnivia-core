"""Engineering source invalidation worker (SPEC-CORE-ENGMEM-001; spec §15.3;
AC-057/AC-061; migration 0054).

`engineering.source.record` (`storage.engineering_source.record_source_event`)
already announces one stream's new head and advances its coverage barrier
atomically, inside the caller's fenced mutation. The gap between that barrier
(`covered_sequence`) and this module's own watermark (`processed_sequence`,
migration 0054) *is* the durable invalidation queue: no event is ever
enqueued as a separate row, because the coverage barrier's own advance is
already the durable, atomic announcement that new work exists, and the
watermark lagging behind it is already a durable, resumable cursor over that
work. Nothing here is a cache invalidation in the sense of deleting or
recomputing state that answers a live read: `current_safe` proves each
version directly, on every read, from `storage.engineering_source`'s own
evaluator, and never consults anything this module writes. What this module
adds is the missing producer for the `diagnostic`-mode assessment history
0049 already defined and reserved a `deterministic` basis for, so that a
change to a depended-on file gets a conservative, durable record without
waiting for a review or a new snapshot registration to trigger one.

One bounded step (`advance_invalidation`) processes at most one covered
source event of one stream: it diffs that event's manifest against its
stored predecessor's, looks up every exact-version dependency whose
whole-file selector names a changed path (bounded and indexed by migration
0054's reverse index), and re-runs the existing conservative evaluator
(`storage.engineering_source.evaluate_applicability`) for each affected
(record, version) against this event as the target -- appending a
`deterministic` assessment for every one of them. Nothing here invents a new
way to conclude `matched`, `potentially_stale`, `invalid` or `unknown`: the
evaluator is the same one `current_safe` calls directly, so the only
questions this module answers for it are "which event is next" and "which
recorded dependencies does it touch". Nothing here ever infers a status
from a shared commit, an unchanged span, recency or a review outcome -- a
changed, deleted or incomplete-capture path is scored exactly as the
evaluator already scores it, including a renamed file, which reads as an
ordinary absence at its old path (there is no rename field to consult) and an
ordinary new dependency-less path at its new one.

Bounded fan-out is the one thing a single covered event does not otherwise
guarantee: many exact record versions can depend on the same changed file. A
step therefore pages through the affected set (`batch_limit` per step,
tracked by a durable keyset cursor, `pending_dependent_record_id` /
`pending_dependent_version`) and only advances `processed_sequence` once
every affected dependent of that exact event has a durable assessment; until
then the unfinished event's progress is itself the durable, explicit "there
is unresolved work here" record, rather than a silent skip. Recovery after a
restart is exactly calling this again: every fact it needs
(`processed_sequence`, the pending-dependent pair, `covered_sequence`) is a
column on the guarded stream row, so nothing in-memory is required to resume
where a previous instance stopped, and `drain_invalidation` is the bounded
loop that walks forward until a stream is caught up or a step budget is
spent. `pending_streams` is the read a caller uses to find streams with
outstanding work at all, for the live per-record trigger in
`service.handlers.engineering`, the startup catch-up in
`service.runner.ServiceRunner._recover`, and the bounded per-tick catch-up in
`service.runner.ServiceRunner.drain_pending_invalidation`.

The cursor is a *keyset* over `(record_id, version)`, not a row count: each
page's query resumes strictly past the last key a prior page of this exact
event durably assessed (`_affected_dependents`'s `after`), rather than
skipping a stated number of rows into a result the next page re-queries
fresh. A row count is unsafe here because the affected set is read fresh on
every page while other fenced writes keep landing between pages (a
dependent's dependency manifest is sealed by its own, independent
`memory.create`, never by this worker): a dependency set committed between
two pages, at a key behind the offset a count would resume from, shifts
every row after it by one position, so the next OFFSET-based page silently
re-reads one row it already assessed and drops one it had not reached yet
-- a duplicate that also, one page later, becomes a skip. A keyset cursor
has no such position to shift: any row at or behind the last assessed key is
either already durably assessed or, if it did not exist yet when this page
ran, simply outside this event's cohort, and every row strictly ahead of
that key is read exactly once, in order, however many rows land behind or
ahead of it in between. A dependency set created after this event's fan-out
began and sealed at a key behind the cursor is not a correctness gap: it is
in exactly the same position as a version whose dependency did not exist
before the previous event, or a version created between two events entirely
-- current-state evaluation is `current_safe`'s job, proved directly from
the same evaluator on every read and never sourced from this history, and
the very next covered event whose diff touches one of that version's
dependencies calls `_affected_dependents` fresh and finds it then. A
dependency set sealed at a key at or ahead of the cursor, by contrast, is
picked up by the very next page -- assessed against this event a little
earlier than strictly owed to it, never skipped and never assessed twice for
an event already finished with it.

Every write here runs inside `ownership.fencing.fenced_transaction`, the same
generation-fenced single-writer authority every other guarded write in this
service uses, and reuses the audit lineage of the exact source event being
processed -- the same principal's same `engineering.source.record` audit
event the streams-table guard trigger already requires -- rather than
minting a new one. A writer whose fencing generation has been superseded is
refused by that same seam before anything commits, exactly as any other
fenced writer is.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage import engineering_applicability as app_storage
from omnivia_core_runtime.storage import engineering_source as source_storage
from omnivia_core_runtime.storage.memory import IdentifierAllocator, random_identifier

#: How many affected dependents of one source event one step may durably
#: assess before it must persist its cursor and yield. Reuses the stream's own
#: pending-coverage window (0050) rather than inventing a second bound: both
#: exist to keep one bounded pass over one stream's backlog finite.
DEPENDENT_BATCH_LIMIT: Final = source_storage.PENDING_WINDOW

#: How many bounded steps `drain_invalidation` takes before yielding control
#: back to its caller, so a live request or a startup recovery pass is itself
#: bounded rather than draining an arbitrarily long backlog in one call.
DRAIN_STEP_LIMIT: Final = source_storage.PENDING_WINDOW

#: How many lagging streams one bounded selection (one service tick, or the
#: startup catch-up pass) may pick up. Reuses the same existing window rather
#: than inventing a second bound: both exist to keep one bounded pass over an
#: unbounded backlog -- of a stream's own events there, of the workspace's own
#: streams here -- finite.
TICK_STREAM_LIMIT: Final = source_storage.PENDING_WINDOW

_BASIS_DETERMINISTIC: Final = "deterministic"


class InvalidationWorkerFault(RuntimeError):
    """A sequence the coverage barrier guarantees present could not be read.

    Raised rather than silently treated as "nothing to do": `covered_sequence`
    is the barrier's own promise that every sequence up to it is a present,
    validated event (migration 0050's guard enforces this), so a missing one
    here means that promise did not hold, which is a fact to surface rather
    than paper over.
    """


@dataclass(frozen=True, slots=True)
class InvalidationProgress:
    """What one bounded worker pass over one stream found and durably did."""

    workspace_id: str
    stream_id: str
    processed_sequence: int
    covered_sequence: int
    pending_dependent_after: tuple[str, str] | None
    assessed: int

    @property
    def caught_up(self) -> bool:
        """Whether this stream has no covered work left to process."""
        return self.processed_sequence >= self.covered_sequence


@dataclass(frozen=True, slots=True)
class _AuditLineage:
    """The one field `engineering_applicability.record_assessment` reads off a
    settlement: its audit reference. This worker settles nothing of its own --
    it reuses the exact source event's own audited settlement instead."""

    audit_ref: str


def _changed_paths(
    previous: Mapping[str, str] | None, current: Mapping[str, str]
) -> frozenset[str]:
    """Every path whose digest disagrees between two consecutive manifests.

    `previous` is `None` for a stream's first event, which has no earlier
    manifest of its own stream to differ from; every one of its paths is then
    "changed" in the sense that matters here, establishing dependencies for
    the first time. No dependency can yet be recorded against an
    even-earlier event of this same stream, so this is a bookkeeping nicety
    rather than one that changes what is found: the lookup below still
    returns nothing for it.
    """
    if previous is None:
        return frozenset(current)
    return frozenset(
        path
        for path in frozenset(previous) | frozenset(current)
        if previous.get(path) != current.get(path)
    )


def _affected_dependents(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    repository_id: str,
    stream_id: str,
    changed_paths: frozenset[str],
    limit: int,
    after: tuple[str, str] | None,
) -> tuple[tuple[str, str], ...]:
    """The affected (record_id, version) pairs of one page, indexed and bounded.

    Bounded by `changed_paths` (at most twice the manifest entry cap) through
    migration 0054's reverse index on `(workspace_id, selector_type,
    selector)`, joined to the recorded baseline's own repository and stream so
    a path match in another repository never crosses over. `after`, when
    given, is the last (record_id, version) a prior page of this exact event
    durably assessed: the keyset predicate below resumes strictly past it, so
    a dependency set some other fenced write commits between two pages --
    whatever key it lands at -- never shifts which row this page starts from,
    unlike an `OFFSET` into a result the next page re-queries fresh.
    """
    if not changed_paths:
        return ()
    ordered_paths = sorted(changed_paths)
    placeholders = ",".join("?" for _ in ordered_paths)
    keyset = ""
    params: list[Any] = [workspace_id, *ordered_paths, repository_id, stream_id]
    if after is not None:
        keyset = "AND (s.record_id > ? OR (s.record_id = ? AND s.version > ?)) "
        params.extend([after[0], after[0], after[1]])
    params.append(limit)
    rows = connection.execute(
        "SELECT DISTINCT s.record_id, s.version "
        "FROM omnivia_engineering_dependencies d "
        "INDEXED BY omnivia_idx_engineering_dependencies_selector "
        "JOIN omnivia_engineering_dependency_sets s "
        "ON s.workspace_id = d.workspace_id AND s.record_id = d.record_id "
        "AND s.version = d.version "
        "WHERE d.workspace_id = ? AND d.selector_type = 'whole_file' "
        f"AND d.selector IN ({placeholders}) "
        "AND s.repository_id = ? AND s.stream_id = ? "
        f"{keyset}"
        "ORDER BY s.record_id, s.version LIMIT ?",
        params,
    ).fetchall()
    return tuple((str(row[0]), str(row[1])) for row in rows)


def _evidence_available(
    connection: sqlite3.Connection, *, workspace_id: str, record_id: str, version: str
) -> bool:
    """Whether this exact version was saved with resolved evidence available.

    The same fact `current_safe` and `engineering.review.record` already read
    off the governed version: an `available` disposition with at least one
    linked evidence item. Neither counts on its own (§15.4).
    """
    row = connection.execute(
        "SELECT v.evidence_disposition, COUNT(l.evidence_id) "
        "FROM omnivia_governed_version_assemblies v "
        "LEFT JOIN omnivia_governed_version_evidence_links l "
        "ON l.workspace_id = v.workspace_id AND l.assembly_id = v.assembly_id "
        "WHERE v.workspace_id = ? AND v.governed_record_id = ? "
        "AND v.governed_record_version_id = ? GROUP BY v.assembly_id",
        (workspace_id, record_id, version),
    ).fetchone()
    if row is None:
        return False
    return str(row[0]) == "available" and int(row[1]) > 0


def pending_streams(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    limit: int | None = None,
    after: str | None = None,
) -> tuple[str, ...]:
    """Stream ids of this workspace whose invalidation watermark lags coverage.

    A read; it writes nothing and takes no fence. Ordered by stream id, and --
    like `_affected_dependents`'s own page -- a *keyset* over that order:
    `after`, when given, resumes strictly past it rather than skipping a row
    count into a result a later call re-queries fresh, and `limit`, when
    given, bounds how many stream ids one call returns. Both default to
    unbounded (`None`) so a caller that wants every lagging stream at once
    still gets exactly that.
    """
    keyset = ""
    params: list[Any] = [workspace_id]
    if after is not None:
        keyset = "AND stream_id > ? "
        params.append(after)
    query = (
        "SELECT stream_id FROM omnivia_engineering_source_streams "
        "WHERE workspace_id = ? AND processed_sequence < covered_sequence "
        f"{keyset}"
        "ORDER BY stream_id"
    )
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    rows = connection.execute(query, params).fetchall()
    return tuple(str(row[0]) for row in rows)


def _stream_page(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    limit: int,
    after: str | None,
) -> tuple[tuple[str, int, int], ...]:
    """`limit` raw stream rows in primary-key order, unfiltered.

    `pending_streams`'s `processed_sequence < covered_sequence` predicate is
    residual against `omnivia_engineering_source_streams`' only applicable
    index, its `(workspace_id, stream_id)` primary key: a workspace with no
    lagging stream in the next `limit` rows still forces a scan onward
    looking for one, unbounded in rows read even though `LIMIT` caps the rows
    *returned*. This instead reads exactly `limit` raw rows in primary-key
    order past `after`, unfiltered, and leaves the lagging check to the
    caller -- so one call's raw-row cost is fixed at `limit` no matter how
    many of those rows, or how many rows total, are lagging.
    """
    keyset = ""
    params: list[Any] = [workspace_id]
    if after is not None:
        keyset = "AND stream_id > ? "
        params.append(after)
    params.append(limit)
    rows = connection.execute(
        "SELECT stream_id, processed_sequence, covered_sequence "
        "FROM omnivia_engineering_source_streams "
        "WHERE workspace_id = ? "
        f"{keyset}"
        "ORDER BY stream_id LIMIT ?",
        params,
    ).fetchall()
    return tuple((str(row[0]), int(row[1]), int(row[2])) for row in rows)


def select_pending_streams(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    limit: int,
    after: str | None,
) -> tuple[tuple[str, ...], str | None]:
    """One fair, bounded page of lagging streams, and where the next page resumes.

    Bounded by *raw* stream rows scanned, not by how many of them turn out to
    be lagging (see `_stream_page`): this reads exactly `limit` raw rows in
    primary-key order past `after` and filters the lagging ones out in
    Python, rather than asking SQLite to keep seeking past `limit` rows for
    `limit` *matches* the way `pending_streams`'s own residual predicate
    would. A workspace with every stream caught up therefore costs the same
    fixed `limit` rows per call as one with every stream lagging; it simply
    takes more calls to reach a lagging stream that sorts behind a run of
    caught-up ones, converging over later ticks exactly as a backlog beyond
    `TICK_STREAM_LIMIT` streams already does.

    Wraps back to the start of the keyset only when the *raw* page itself
    comes back empty with `after` already set -- the one condition that
    means nothing sorts past the cursor at all -- and retries once, from the
    start, with the same `limit`; a raw page that is merely all caught-up is
    never grounds to read past `limit` rows in the same call, since that is
    exactly the unbounded scan this replaces. The wrap keeps one call's total
    raw-row cost capped at `2 * limit`, and is why a persistently failing
    low-sort stream occupies at most one page per sweep of the workspace's
    streams rather than permanently crowding out every stream that sorts
    after it. The returned cursor is the last stream id this page's raw scan
    reached, lagging or not (`None` once a sweep has wrapped and found no
    stream at all), for a caller to pass back in as `after` on its next call.
    """
    page = _stream_page(connection, workspace_id=workspace_id, limit=limit, after=after)
    if not page and after is not None:
        page = _stream_page(connection, workspace_id=workspace_id, limit=limit, after=None)
    lagging = tuple(stream_id for stream_id, processed, covered in page if processed < covered)
    next_after = page[-1][0] if page else None
    return lagging, next_after


def advance_invalidation(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    stream_id: str,
    fencing_generation: int,
    now_us: int,
    batch_limit: int = DEPENDENT_BATCH_LIMIT,
    allocate_identifier: IdentifierAllocator = random_identifier,
) -> InvalidationProgress | None:
    """Durably advance one stream's invalidation watermark by one bounded step.

    `None` when the stream is not registered. Otherwise the stream's current
    progress if it already has no covered work left, or -- if it has -- the
    result of assessing at most `batch_limit` of the next unprocessed event's
    affected dependents and either completing that event (the watermark
    advances, its keyset cursor resets to `None`) or persisting the last key
    this page reached (the watermark holds, its cursor advances to that key).
    Both outcomes commit together with every assessment in the one fenced
    transaction opened here; a caller loops (`drain_invalidation`) to walk
    further.
    """
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as fenced:
        stream = fenced.execute(
            "SELECT repository_id, covered_sequence, processed_sequence, "
            "pending_dependent_record_id, pending_dependent_version, updated_at_us "
            "FROM omnivia_engineering_source_streams "
            "WHERE workspace_id = ? AND stream_id = ?",
            (workspace_id, stream_id),
        ).fetchone()
        if stream is None:
            return None
        repository_id = str(stream[0])
        covered = int(stream[1])
        processed = int(stream[2])
        after = None if stream[3] is None else (str(stream[3]), str(stream[4]))
        stored_updated_at_us = int(stream[5])
        if processed >= covered:
            return InvalidationProgress(
                workspace_id=workspace_id,
                stream_id=stream_id,
                processed_sequence=processed,
                covered_sequence=covered,
                pending_dependent_after=after,
                assessed=0,
            )

        target_sequence = processed + 1
        current = source_storage.sequenced_event(
            fenced, workspace_id=workspace_id, stream_id=stream_id, sequence=target_sequence
        )
        if current is None:
            raise InvalidationWorkerFault(
                f"stream {stream_id!r} covers sequence {target_sequence} but its event "
                "is unreadable"
            )
        previous = (
            None
            if target_sequence == 1
            else source_storage.sequenced_event(
                fenced,
                workspace_id=workspace_id,
                stream_id=stream_id,
                sequence=target_sequence - 1,
            )
        )
        if target_sequence > 1 and previous is None:
            raise InvalidationWorkerFault(
                f"stream {stream_id!r} covers sequence {target_sequence} but its "
                "predecessor event is unreadable"
            )
        changed = _changed_paths(None if previous is None else previous.manifest, current.manifest)
        dependents = _affected_dependents(
            fenced,
            workspace_id=workspace_id,
            repository_id=repository_id,
            stream_id=stream_id,
            changed_paths=changed,
            limit=batch_limit,
            after=after,
        )

        target = source_storage.covered_snapshot(
            fenced,
            workspace_id=workspace_id,
            snapshot_id=current.snapshot_id,
            repository_id=repository_id,
        )
        if target is None:  # pragma: no cover - the coverage barrier guarantees this
            raise InvalidationWorkerFault(
                f"stream {stream_id!r} sequence {target_sequence} is covered but its "
                "snapshot is not"
            )
        lineage = _AuditLineage(audit_ref=current.audit_ref)
        for record_id, version in dependents:
            status = source_storage.evaluate_applicability(
                fenced,
                workspace_id=workspace_id,
                record_id=record_id,
                version=version,
                evidence_available=_evidence_available(
                    fenced, workspace_id=workspace_id, record_id=record_id, version=version
                ),
                target=target,
            )
            app_storage.record_assessment(
                fenced,
                lineage,
                workspace_id=workspace_id,
                assessment_id=allocate_identifier("eiv"),
                record_id=record_id,
                version=version,
                target_snapshot_id=current.snapshot_id,
                status=status,
                basis=_BASIS_DETERMINISTIC,
                assessed_at_us=now_us,
            )

        if len(dependents) < batch_limit:
            new_processed, new_after = target_sequence, None
        else:
            new_processed, new_after = processed, dependents[-1]
        # A caller's clock can read behind this row's own last write -- a
        # step backward, or simply this call landing before another fenced
        # write already stamped a later instant -- and the streams-table
        # guard trigger rejects any UPDATE that would move `updated_at_us`
        # backward. Clamping forward (the same guard `record_source_event`
        # already applies to this column) keeps the invariant intact without
        # raising and losing every assessment above to a rollback. Nothing
        # assessed above is touched by this: each kept the caller's own,
        # unclamped `now_us`, so a behind-the-clock reading still reads as
        # what it was, and only this row's own liveness bookkeeping is held
        # to its existing monotonic promise.
        write_updated_at_us = max(now_us, stored_updated_at_us)
        fenced.execute(
            "UPDATE omnivia_engineering_source_streams SET processed_sequence = ?, "
            "pending_dependent_record_id = ?, pending_dependent_version = ?, "
            "updated_at_us = ?, audit_ref = ? "
            "WHERE workspace_id = ? AND stream_id = ?",
            (
                new_processed,
                None if new_after is None else new_after[0],
                None if new_after is None else new_after[1],
                write_updated_at_us,
                current.audit_ref,
                workspace_id,
                stream_id,
            ),
        )
        return InvalidationProgress(
            workspace_id=workspace_id,
            stream_id=stream_id,
            processed_sequence=new_processed,
            covered_sequence=covered,
            pending_dependent_after=new_after,
            assessed=len(dependents),
        )


def drain_invalidation(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    stream_id: str,
    fencing_generation: int,
    now_us: int,
    batch_limit: int = DEPENDENT_BATCH_LIMIT,
    max_steps: int = DRAIN_STEP_LIMIT,
    allocate_identifier: IdentifierAllocator = random_identifier,
) -> InvalidationProgress | None:
    """Call `advance_invalidation` until this stream is caught up or bounded out.

    Each step is its own committed fenced transaction, so a step this call
    already completed survives a later step that raises, and a caller that is
    interrupted mid-drain resumes exactly where the durable cursor says to.
    """
    progress: InvalidationProgress | None = None
    for _ in range(max_steps):
        progress = advance_invalidation(
            connection,
            identity,
            workspace_id=workspace_id,
            stream_id=stream_id,
            fencing_generation=fencing_generation,
            now_us=now_us,
            batch_limit=batch_limit,
            allocate_identifier=allocate_identifier,
        )
        if progress is None or progress.caught_up:
            break
    return progress


__all__ = [
    "DEPENDENT_BATCH_LIMIT",
    "DRAIN_STEP_LIMIT",
    "TICK_STREAM_LIMIT",
    "InvalidationProgress",
    "InvalidationWorkerFault",
    "advance_invalidation",
    "drain_invalidation",
    "pending_streams",
    "select_pending_streams",
]
