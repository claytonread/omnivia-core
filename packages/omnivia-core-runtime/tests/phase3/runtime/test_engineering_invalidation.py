"""Engineering source invalidation worker (SPEC-CORE-ENGMEM-001; spec §15.3;
AC-057/AC-061; migration 0054).

Reuses `test_engineering_source_coverage`'s production `Workspace` harness for
setup (recording source events, observing dependency-qualified proposals) but
disables the handler's own best-effort drain (`EngineeringHandlers._drain_invalidation`)
so every test here drives `storage.engineering_invalidation.advance_invalidation`
and `.drain_invalidation` directly, under exact control of batch size, fencing
generation and the moment a failure lands. The wired, automatic path -- a live
`engineering.source.record` call reaching the worker on its own -- is covered
by `test_engineering_source_coverage.py`'s own end-to-end vertical test.
"""

from __future__ import annotations

import itertools
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import test_engineering_source_coverage as esc
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.handlers import engineering as handlers
from omnivia_core_runtime.service.runner import ServiceRunner
from omnivia_core_runtime.storage import engineering_invalidation as inv
from omnivia_core_runtime.storage import engineering_source

Workspace = esc.Workspace
WORKSPACE_ID = esc.WORKSPACE_ID
REPOSITORY = esc.REPOSITORY
STREAM = esc.STREAM
FILES_A = esc.FILES_A

INDEX = "omnivia_idx_engineering_dependencies_selector"

#: A stream row's `updated_at_us` only ever advances (migration 0050), and
#: `record_source_event` already stamped it with a real wall-clock reading at
#: whatever moment each test actually runs. A fixed literal, or a counter
#: merely seeded from the clock once at import time, can both end up smaller
#: than that and trip the same guard, so this reads the clock fresh on every
#: call and is bumped by at least one tick past its own last answer too.
_last_now_us = 0


def _now_us() -> int:
    global _last_now_us
    _last_now_us = max(_last_now_us, int(time.time() * 1_000_000)) + 1
    return _last_now_us


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        handlers.EngineeringHandlers, "_drain_invalidation", lambda self, *a, **k: None
    )
    opened = Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


def _progress(
    workspace: Workspace, stream_id: str = STREAM
) -> tuple[int, int, tuple[str, str] | None]:
    row = workspace.holder.connection.execute(
        "SELECT covered_sequence, processed_sequence, pending_dependent_record_id, "
        "pending_dependent_version FROM omnivia_engineering_source_streams "
        "WHERE workspace_id = ? AND stream_id = ?",
        (WORKSPACE_ID, stream_id),
    ).fetchone()
    after = None if row[2] is None else (str(row[2]), str(row[3]))
    return (int(row[0]), int(row[1]), after)


def _assessments(workspace: Workspace, record_id: str) -> list[tuple[str, str, str]]:
    return [
        (str(row[0]), str(row[1]), str(row[2]))
        for row in workspace.holder.connection.execute(
            "SELECT target_snapshot_id, status, basis FROM omnivia_engineering_assessments "
            "WHERE workspace_id = ? AND record_id = ? ORDER BY assessed_at_us, target_snapshot_id",
            (WORKSPACE_ID, record_id),
        ).fetchall()
    ]


def _advance(
    workspace: Workspace, *, now_us: int | None = None, stream_id: str = STREAM, **kwargs: Any
) -> inv.InvalidationProgress | None:
    return inv.advance_invalidation(
        workspace.holder.connection,
        workspace.holder.identity,
        workspace_id=WORKSPACE_ID,
        stream_id=stream_id,
        fencing_generation=workspace.holder.generation,
        now_us=_now_us() if now_us is None else now_us,
        **kwargs,
    )


def _drain(
    workspace: Workspace, *, now_us: int | None = None, stream_id: str = STREAM, **kwargs: Any
) -> inv.InvalidationProgress | None:
    return inv.drain_invalidation(
        workspace.holder.connection,
        workspace.holder.identity,
        workspace_id=WORKSPACE_ID,
        stream_id=stream_id,
        fencing_generation=workspace.holder.generation,
        now_us=_now_us() if now_us is None else now_us,
        **kwargs,
    )


# --- announcement vs. processing are two separately durable facts -------------------


def test_the_new_head_and_coverage_are_durable_before_the_worker_ever_runs(
    workspace: Workspace,
) -> None:
    result = workspace.record(esc._source(1, "esnap-a", FILES_A))
    assert result["coverage"] == {
        "state": "current",
        "covered_sequence": 1,
        "announced_sequence": 1,
    }
    assert _progress(workspace) == (1, 0, None)

    result2 = workspace.record(esc._source(2, "esnap-b", FILES_A, predecessor="esnap-a"))
    assert result2["coverage"]["covered_sequence"] == 2
    # Announcing coverage never waits on or is blocked by invalidation: the
    # watermark is untouched until something explicitly drives the worker.
    assert _progress(workspace) == (2, 0, None)

    progress = _drain(workspace)
    assert progress is not None and progress.processed_sequence == 2 and progress.caught_up


def test_pending_streams_lists_exactly_the_streams_with_a_lagging_watermark(
    workspace: Workspace,
) -> None:
    assert inv.pending_streams(workspace.holder.connection, workspace_id=WORKSPACE_ID) == ()
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    assert inv.pending_streams(workspace.holder.connection, workspace_id=WORKSPACE_ID) == (
        STREAM,
    )
    _drain(workspace)
    assert inv.pending_streams(workspace.holder.connection, workspace_id=WORKSPACE_ID) == ()


# --- atomicity and fencing ------------------------------------------------------------


def test_a_failure_mid_batch_rolls_back_every_assessment_and_the_watermark(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    _drain(workspace)  # catch up through event 1 before anything depends on it
    first = workspace.observe(esc._observation(esc._manifest(), title="First"))
    second = workspace.observe(esc._observation(esc._manifest(), title="Second"))
    workspace.record(
        esc._source(
            2, "esnap-b", {**FILES_A, "src/auth.py": esc._sha("auth v2")}, predecessor="esnap-a"
        )
    )
    before = _progress(workspace)
    assert before == (2, 1, None)

    calls: list[str] = []
    original = engineering_source.evaluate_applicability

    def spy(*args: Any, **kwargs: Any) -> str:
        calls.append(str(kwargs["record_id"]))
        if len(calls) == 2:
            raise RuntimeError("boom")
        return original(*args, **kwargs)

    monkeypatch.setattr(engineering_source, "evaluate_applicability", spy)
    with pytest.raises(RuntimeError, match="boom"):
        _advance(workspace)
    assert set(calls) == {first["record_id"], second["record_id"]}
    # Nothing committed: not the first (already-evaluated) assessment, and not
    # the watermark or cursor -- one fenced transaction, one all-or-nothing.
    assert _assessments(workspace, first["record_id"]) == []
    assert _assessments(workspace, second["record_id"]) == []
    assert _progress(workspace) == before

    monkeypatch.undo()
    progress = _advance(workspace)
    assert progress is not None
    assert (progress.processed_sequence, progress.assessed) == (2, 2)
    assert len(_assessments(workspace, first["record_id"])) == 1
    assert len(_assessments(workspace, second["record_id"])) == 1


def test_a_stale_writer_generation_is_refused_and_writes_nothing(
    workspace: Workspace,
) -> None:
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    workspace.observe(esc._observation(esc._manifest()))
    workspace.record(
        esc._source(
            2, "esnap-b", {**FILES_A, "src/auth.py": esc._sha("auth v2")}, predecessor="esnap-a"
        )
    )
    before = _progress(workspace)
    with pytest.raises(StaleGeneration):
        inv.advance_invalidation(
            workspace.holder.connection,
            workspace.holder.identity,
            workspace_id=WORKSPACE_ID,
            stream_id=STREAM,
            fencing_generation=workspace.holder.generation + 1,
            now_us=_now_us(),
        )
    assert _progress(workspace) == before
    assert workspace.counts()["omnivia_engineering_assessments"] == 0


# --- duplicates and out-of-order recovery ----------------------------------------------


def test_a_duplicate_event_advances_neither_the_watermark_nor_the_assessment_history(
    workspace: Workspace,
) -> None:
    workspace.record(esc._source(1, "esnap-a", FILES_A), key="k1")
    _drain(workspace)  # catch up through event 1 before anything depends on it
    record = workspace.observe(esc._observation(esc._manifest()))
    workspace.record(
        esc._source(
            2, "esnap-b", {**FILES_A, "src/auth.py": esc._sha("auth v2")}, predecessor="esnap-a"
        ),
        key="k2",
    )
    progress = _drain(workspace)
    assert progress is not None and progress.processed_sequence == 2
    after_first_drain = _assessments(workspace, record["record_id"])
    assert after_first_drain == [("esnap-b", "potentially_stale", "deterministic")]

    replay = workspace.record(esc._source(1, "esnap-a", FILES_A), key="k3")
    assert replay["disposition"] == "already_recorded"
    assert _progress(workspace) == (2, 2, None)

    progress = _drain(workspace)
    assert progress is not None
    assert (progress.processed_sequence, progress.assessed, progress.caught_up) == (2, 0, True)
    assert _assessments(workspace, record["record_id"]) == after_first_drain


def test_out_of_order_gap_then_recovery_resumes_from_the_durable_cursor(
    workspace: Workspace,
) -> None:
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    _drain(workspace)  # catch up through event 1 before anything depends on it
    record = workspace.observe(esc._observation(esc._manifest()))
    # 3 arrives before 2: a gap. Coverage and the watermark both stay at 1.
    workspace.record(
        esc._source(
            3, "esnap-c", {**FILES_A, "src/auth.py": esc._sha("auth v3")}, predecessor="esnap-b"
        )
    )
    assert _drain(workspace) is not None
    assert _progress(workspace) == (1, 1, None)

    # Restart: close and reopen the connection, exactly as a service restart
    # would. Nothing in memory is lost because nothing in memory was needed.
    workspace.restart()
    assert _progress(workspace) == (1, 1, None)

    # The missing predecessor arrives: one bounded coverage drain covers 2 and
    # 3 together, but the watermark itself has not moved -- nothing here has
    # driven the worker yet.
    workspace.record(
        esc._source(
            2, "esnap-b", {**FILES_A, "src/auth.py": esc._sha("auth v2")}, predecessor="esnap-a"
        )
    )
    assert _progress(workspace) == (3, 1, None)

    progress = _drain(workspace)
    assert progress is not None
    assert (progress.processed_sequence, progress.caught_up) == (3, True)
    # Both newly-covered events share one drain call's timestamp, so compare
    # as a set rather than assume which of two equally-stamped rows sorts first.
    assert {row[0] for row in _assessments(workspace, record["record_id"])} == {
        "esnap-b",
        "esnap-c",
    }


# --- changed / deleted / renamed / incomplete-capture / reverted targets --------------


def test_changed_deleted_incomplete_renamed_and_reverted_paths_each_score_correctly(
    workspace: Workspace,
) -> None:
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    _drain(workspace)  # catch up through event 1 before anything depends on it
    record = workspace.observe(esc._observation(esc._manifest()))

    # Modified: auth.py's digest changes under complete capture -> stale.
    workspace.record(
        esc._source(
            2, "esnap-mod", {**FILES_A, "src/auth.py": esc._sha("auth v2")}, predecessor="esnap-a"
        )
    )
    # Deleted under complete capture: an absent required file -> invalid.
    workspace.record(
        esc._source(
            3,
            "esnap-del",
            {"src/util.py": esc.UTIL_V1, "README.md": esc.README_V1},
            predecessor="esnap-mod",
        )
    )
    # The same absence under incomplete capture proves nothing -> unknown.
    workspace.record(
        esc._source(
            4,
            "esnap-partial",
            {"src/util.py": esc.UTIL_V1},
            predecessor="esnap-del",
            capture="incomplete",
        )
    )
    # An "ambiguous rename": auth.py's old path stays absent while a new,
    # unrelated path with the same content appears. There is no rename field
    # to consult, so this reads as an ordinary absence at the recorded
    # dependency's own path -- invalid again, not specially reconciled.
    workspace.record(
        esc._source(
            5,
            "esnap-renamed",
            {
                "src/auth_renamed.py": esc.AUTH_V1,
                "src/util.py": esc.UTIL_V1,
                "README.md": esc.README_V1,
            },
            predecessor="esnap-partial",
        )
    )
    # A revert to the original digests qualifies again.
    workspace.record(esc._source(6, "esnap-revert", FILES_A, predecessor="esnap-renamed"))

    progress = _drain(workspace, max_steps=10)
    assert progress is not None and progress.caught_up

    # Five events land in one bounded drain call and so share one timestamp:
    # compared as {target: status}, not as an order-sensitive list.
    assessed = _assessments(workspace, record["record_id"])
    assert {target: status for target, status, _basis in assessed} == {
        "esnap-mod": "potentially_stale",
        "esnap-del": "invalid",
        "esnap-partial": "unknown",
        "esnap-renamed": "invalid",
        "esnap-revert": "matched",
    }
    assert all(basis == "deterministic" for _target, _status, basis in assessed)
    # This history is exactly what a live direct check finds at each target:
    # the worker calls the same evaluator, it does not invent a second rule.
    for target, expected_status, _basis in assessed:
        assert workspace.status(record, target) == expected_status


def test_a_dirty_working_tree_target_is_scored_like_any_other_snapshot(
    workspace: Workspace,
) -> None:
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    _drain(workspace)  # catch up through event 1 before anything depends on it
    record = workspace.observe(esc._observation(esc._manifest()))
    workspace.record(
        esc._source(
            2,
            "esnap-dirty",
            {**FILES_A, "src/auth.py": esc._sha("auth v2")},
            predecessor="esnap-a",
            kind="working_tree",
            base_commit=None,
        )
    )
    progress = _drain(workspace)
    assert progress is not None and progress.caught_up
    assert _assessments(workspace, record["record_id"]) == [
        ("esnap-dirty", "potentially_stale", "deterministic"),
    ]


# --- bounded fan-out and its durable cursor --------------------------------------------


def test_a_large_events_fan_out_persists_its_cursor_across_bounded_steps(
    workspace: Workspace,
) -> None:
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    records = [
        workspace.observe(esc._observation(esc._manifest(), title=f"Provider {i}"))
        for i in range(3)
    ]
    # Catch up through event 1 (its own trivial self-match for all three
    # records) so this test can isolate event 2's fan-out alone.
    setup = _drain(workspace)
    assert setup is not None and setup.processed_sequence == 1
    for record in records:
        assert _assessments(workspace, record["record_id"]) == [
            ("esnap-a", "matched", "deterministic")
        ]

    workspace.record(
        esc._source(
            2, "esnap-b", {**FILES_A, "src/auth.py": esc._sha("auth v2")}, predecessor="esnap-a"
        )
    )
    first_page = _advance(workspace, batch_limit=2)
    assert first_page is not None
    assert (first_page.processed_sequence, first_page.assessed) == (1, 2)
    # Some two of the three -- whichever sorts first -- not yet all three, so
    # the keyset cursor is set rather than reset.
    assert first_page.pending_dependent_after is not None

    # A restart between pages: the durable cursor alone is what lets the next
    # call pick up exactly the one dependent the first page had not reached.
    workspace.restart()
    second_page = _advance(workspace, batch_limit=2)
    assert second_page is not None
    assert (
        second_page.processed_sequence,
        second_page.pending_dependent_after,
        second_page.assessed,
    ) == (2, None, 1)

    for record in records:
        assert [row[0] for row in _assessments(workspace, record["record_id"])] == [
            "esnap-a",
            "esnap-b",
        ]


# --- the reverse lookup is indexed and bounded -----------------------------------------


def test_the_changed_path_lookup_seeks_the_reverse_index(workspace: Workspace) -> None:
    """Mirrors `_affected_dependents`'s page-2-and-later shape: the keyset
    predicate is an extra filter on `s`, not a reason for the planner to stop
    seeking `d` by the reverse index."""
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    workspace.observe(esc._observation(esc._manifest()))
    connection = workspace.holder.connection
    plan = [
        str(row[3])
        for row in connection.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT DISTINCT s.record_id, s.version "
            "FROM omnivia_engineering_dependencies d "
            "INDEXED BY omnivia_idx_engineering_dependencies_selector "
            "JOIN omnivia_engineering_dependency_sets s "
            "ON s.workspace_id = d.workspace_id AND s.record_id = d.record_id "
            "AND s.version = d.version "
            "WHERE d.workspace_id = ? AND d.selector_type = 'whole_file' "
            "AND d.selector IN (?, ?) AND s.repository_id = ? AND s.stream_id = ? "
            "AND (s.record_id > ? OR (s.record_id = ? AND s.version > ?)) "
            "ORDER BY s.record_id, s.version LIMIT ?",
            (
                WORKSPACE_ID,
                "src/auth.py",
                "src/util.py",
                REPOSITORY,
                STREAM,
                "rec-0",
                "rec-0",
                "v0",
                64,
            ),
        ).fetchall()
    ]
    assert any(line.startswith(f"SEARCH d USING INDEX {INDEX}") for line in plan), plan


# --- keyset pagination is stable under concurrent inserts ------------------------------


def test_a_dependency_set_inserted_between_pages_is_not_reprocessed_or_skipped(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dependency set some other fenced write seals strictly between two
    pages of one event's fan-out must not shift which row the next page
    resumes from -- the failure an `OFFSET` count is exposed to, since the
    same query re-run later counts positions in a result that just grew.

    Record ids are ordinarily opaque UUIDs, so `uuid.uuid4` is patched to a
    controlled, monotonically increasing sequence: everything created while
    one sequence is installed sorts below everything created under a later,
    lower one, however many identifiers one `memory.create` call happens to
    allocate internally. This lets the test name, in advance, which of three
    (record_id, version) pairs sorts where, without depending on that
    allocation count: two "old" dependents from a high range (whichever
    sorts first is the one the first page durably assesses) and, created
    only after that page has already committed, a "new" one from a range
    strictly below both -- exactly the arrangement an `OFFSET` cursor would
    already have counted past.
    """

    def sequential(counter: itertools.count[int]) -> Any:
        def fake_uuid4() -> uuid.UUID:
            return uuid.UUID(int=next(counter))

        return fake_uuid4

    workspace.record(esc._source(1, "esnap-a", FILES_A))
    _drain(workspace)  # catch up through event 1 before anything depends on it

    monkeypatch.setattr(uuid, "uuid4", sequential(itertools.count(1_000)))
    old_first = workspace.observe(esc._observation(esc._manifest(), title="Old A"))
    old_next = workspace.observe(esc._observation(esc._manifest(), title="Old B"))
    assert old_first["record_id"] < old_next["record_id"]

    workspace.record(
        esc._source(
            2, "esnap-b", {**FILES_A, "src/auth.py": esc._sha("auth v2")}, predecessor="esnap-a"
        )
    )
    first_page = _advance(workspace, batch_limit=1)
    assert first_page is not None
    assert (first_page.processed_sequence, first_page.assessed) == (1, 1)
    assert _assessments(workspace, old_first["record_id"]) == [
        ("esnap-b", "potentially_stale", "deterministic")
    ]
    assert _assessments(workspace, old_next["record_id"]) == []

    # Inserted strictly between the first page and the rest, at a key below
    # everything the first page already durably assessed.
    monkeypatch.setattr(uuid, "uuid4", sequential(itertools.count(0)))
    inserted = workspace.observe(esc._observation(esc._manifest(), title="Inserted"))
    assert inserted["record_id"] < old_first["record_id"]

    progress = _drain(workspace, max_steps=10)
    assert progress is not None and progress.caught_up

    # The already-durable assessment is exactly one row, not reprocessed;
    # the not-yet-reached one is exactly one row too, not skipped.
    assert _assessments(workspace, old_first["record_id"]) == [
        ("esnap-b", "potentially_stale", "deterministic")
    ]
    assert _assessments(workspace, old_next["record_id"]) == [
        ("esnap-b", "potentially_stale", "deterministic")
    ]
    # The inserted set, sealed after this event's fan-out already began, is
    # outside this event's cohort -- never a correctness gap, since
    # `current_safe` proves its current state directly and never consults
    # this history.
    assert _assessments(workspace, inserted["record_id"]) == []
    assert (
        workspace.status(
            {"record_id": inserted["record_id"], "version": inserted["version"]}, "esnap-b"
        )
        == "potentially_stale"
    )


# --- a caller's clock never regresses the stream row's own timestamp -------------------


def test_a_regressing_clock_reading_is_clamped_forward_not_raised(
    workspace: Workspace,
) -> None:
    """`advance_invalidation` writes the stream row's own `updated_at_us`
    alongside its watermark advance, and the streams-table guard trigger
    (migrations 0050/0054) rejects any UPDATE that would move that column
    backward. A caller's clock can still read behind the row's own last
    write -- a real clock steps backward under correction, and nothing
    otherwise excludes a fenced writer's own reading landing earlier than a
    previous one -- so this proves the worker clamps forward rather than
    raising and losing the batch's assessments to a rollback.
    """
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    _drain(workspace)  # catch up through event 1 before anything depends on it
    record = workspace.observe(esc._observation(esc._manifest()))
    workspace.record(
        esc._source(
            2, "esnap-b", {**FILES_A, "src/auth.py": esc._sha("auth v2")}, predecessor="esnap-a"
        )
    )
    before = workspace.holder.connection.execute(
        "SELECT updated_at_us FROM omnivia_engineering_source_streams "
        "WHERE workspace_id = ? AND stream_id = ?",
        (WORKSPACE_ID, STREAM),
    ).fetchone()[0]

    behind = before - 1_000_000
    progress = _advance(workspace, now_us=behind)
    assert progress is not None
    assert (progress.processed_sequence, progress.assessed) == (2, 1)
    assert _assessments(workspace, record["record_id"]) == [
        ("esnap-b", "potentially_stale", "deterministic")
    ]

    after = workspace.holder.connection.execute(
        "SELECT updated_at_us FROM omnivia_engineering_source_streams "
        "WHERE workspace_id = ? AND stream_id = ?",
        (WORKSPACE_ID, STREAM),
    ).fetchone()[0]
    assert after == before  # clamped forward, never regressed

    assessed_at_us = workspace.holder.connection.execute(
        "SELECT assessed_at_us FROM omnivia_engineering_assessments "
        "WHERE workspace_id = ? AND record_id = ?",
        (WORKSPACE_ID, record["record_id"]),
    ).fetchone()[0]
    # The assessment itself keeps the caller's real, behind-the-clock
    # reading: only the stream row's own liveness bookkeeping is clamped.
    assert assessed_at_us == behind


# --- a bounded per-tick continuation converges a multi-event backlog -------------------


def test_drain_pending_invalidation_converges_a_multi_event_backlog_across_ticks(
    workspace: Workspace,
) -> None:
    """`ServiceRunner.drain_pending_invalidation` is one bounded
    `advance_invalidation` step per pending stream per call -- the same bound
    the serve loop's own 250ms poll gets it (`main._serve_until_stopped`). A
    backlog of several already-covered events exceeds that one-step budget,
    so repeated calls (successive ticks) must converge it fully, with no
    further source write and no restart.
    """
    workspace.record(esc._source(1, "esnap-a", FILES_A))
    _drain(workspace)  # catch up through event 1 before anything depends on it
    record = workspace.observe(esc._observation(esc._manifest()))

    b = {**FILES_A, "src/auth.py": esc._sha("auth v2")}
    workspace.record(esc._source(2, "esnap-b", b, predecessor="esnap-a"))
    c = {**b, "src/auth.py": esc._sha("auth v3")}
    workspace.record(esc._source(3, "esnap-c", c, predecessor="esnap-b"))
    d = {**c, "src/auth.py": esc._sha("auth v4")}
    workspace.record(esc._source(4, "esnap-d", d, predecessor="esnap-c"))
    e = {**d, "src/auth.py": esc._sha("auth v5")}
    workspace.record(esc._source(5, "esnap-e", e, predecessor="esnap-d"))
    assert _progress(workspace) == (5, 1, None)

    # `__new__` avoids the full startup sequence (a real manifest, disk
    # locks, a socket lease): `drain_pending_invalidation` only ever reads
    # these five attributes, all of which `Workspace`'s own harness already
    # holds.
    runner = _runner(workspace)

    for _ in range(4):
        assert _progress(workspace)[1] < 5
        runner.clock.advance_wall(1.0)
        runner.drain_pending_invalidation()

    assert _progress(workspace) == (5, 5, None)
    assert {row[0] for row in _assessments(workspace, record["record_id"])} == {
        "esnap-b",
        "esnap-c",
        "esnap-d",
        "esnap-e",
    }


def _runner(workspace: Workspace) -> ServiceRunner:
    """The same construction-only harness `drain_pending_invalidation` above
    uses: `__new__` skips the full startup sequence, since this method only
    ever reads the five attributes `Workspace`'s own harness already holds."""
    runner = ServiceRunner.__new__(ServiceRunner)
    runner.connection = workspace.holder.connection
    runner.identity = workspace.holder.identity
    runner.generation = workspace.holder.generation
    runner.workspace_id = WORKSPACE_ID
    runner.clock = FakeClock()
    return runner


def test_a_backlog_past_the_per_stream_step_bound_converges_across_startup_and_later_ticks(
    workspace: Workspace,
) -> None:
    """A single stream's backlog past `DRAIN_STEP_LIMIT` (64) events is exactly
    what startup recovery's one bounded `drain_invalidation` call cannot finish
    in one pass -- it is bounded per call by design (module docstring). The
    fix here bounds startup's *stream count*, not this existing per-stream
    step bound, so this proves the pre-existing bound and the tick-based
    hand-off still compose correctly at a backlog this large."""
    workspace.record(esc._source(1, "esnap-0", FILES_A))
    _drain(workspace)  # catch up through event 1 before piling on a backlog

    total_events = inv.DRAIN_STEP_LIMIT + 5
    predecessor = "esnap-0"
    for sequence in range(2, total_events + 1):
        snapshot_id = f"esnap-{sequence}"
        workspace.record(esc._source(sequence, snapshot_id, FILES_A, predecessor=predecessor))
        predecessor = snapshot_id
    assert _progress(workspace) == (total_events, 1, None)

    # Startup recovery's own one-shot, bounded-by-`DRAIN_STEP_LIMIT` pass over
    # this one stream: it cannot reach the end of a backlog this large in a
    # single call.
    startup_progress = _drain(workspace)
    assert startup_progress is not None
    assert startup_progress.processed_sequence == 1 + inv.DRAIN_STEP_LIMIT
    assert not startup_progress.caught_up

    # Subsequent ticks -- one bounded `advance_invalidation` step per tick for
    # this one lagging stream -- converge exactly the remainder, with no
    # further source write and no restart.
    runner = _runner(workspace)
    remaining = total_events - (1 + inv.DRAIN_STEP_LIMIT)
    for _ in range(remaining):
        assert _progress(workspace)[1] < total_events
        runner.clock.advance_wall(1.0)
        runner.drain_pending_invalidation()

    assert _progress(workspace) == (total_events, total_events, None)


def test_bounded_per_tick_stream_selection_scans_caught_up_streams_before_reaching_a_pending_one(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`select_pending_streams` bounds each call by *raw* stream rows scanned,
    not by how many of them are lagging: `omnivia_engineering_source_streams`'
    only applicable index is its `(workspace_id, stream_id)` primary key, so a
    residual lagging filter (`pending_streams`'s own) would force a scan past
    every caught-up row looking for one that matches, unbounded once a
    workspace is mostly caught up however small the row cap. Capped here to
    exactly one raw row per tick: two caught-up, low-sort streams ahead of one
    pending, high-sort stream must each take their own tick before the
    pending one is even reached -- neither skipped over to find it sooner --
    and the keyset must wrap fairly once it runs off the end back around to
    the pending stream's own next turn."""
    monkeypatch.setattr(inv, "TICK_STREAM_LIMIT", 1)

    for stream_id in ("estream-a", "estream-b"):
        workspace.record(esc._source(1, f"{stream_id}-1", FILES_A, stream=stream_id))
        _drain(workspace, stream_id=stream_id)  # caught up, no backlog

    workspace.record(esc._source(1, "estream-c-1", FILES_A, stream="estream-c"))
    _drain(workspace, stream_id="estream-c")
    workspace.record(
        esc._source(2, "estream-c-2", FILES_A, predecessor="estream-c-1", stream="estream-c")
    )
    assert _progress(workspace, stream_id="estream-c") == (2, 1, None)

    runner = _runner(workspace)

    # Tick 1: the only raw row within reach is estream-a, caught up -- no
    # lagging stream is selected, but the cursor still advances past it
    # rather than the call scanning onward looking for one that matches.
    runner.clock.advance_wall(1.0)
    runner.drain_pending_invalidation()
    assert runner._invalidation_cursor == "estream-a"
    assert _progress(workspace, stream_id="estream-c") == (2, 1, None)

    # Tick 2: estream-b, likewise caught up.
    runner.clock.advance_wall(1.0)
    runner.drain_pending_invalidation()
    assert runner._invalidation_cursor == "estream-b"
    assert _progress(workspace, stream_id="estream-c") == (2, 1, None)

    # Tick 3: estream-c, the one lagging stream, is finally reached and
    # advanced.
    runner.clock.advance_wall(1.0)
    runner.drain_pending_invalidation()
    assert runner._invalidation_cursor == "estream-c"
    assert _progress(workspace, stream_id="estream-c") == (2, 2, None)

    # Tick 4: nothing sorts past estream-c, so the raw page is empty and the
    # keyset wraps back to the start of the sweep within this same tick.
    runner.clock.advance_wall(1.0)
    runner.drain_pending_invalidation()
    assert runner._invalidation_cursor == "estream-a"


def test_bounded_per_tick_stream_selection_is_fair_despite_one_stream_repeatedly_failing(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`select_pending_streams` caps one tick to `TICK_STREAM_LIMIT` streams and
    carries its keyset cursor forward tick to tick. Capped here to exactly one
    stream per tick to make the cap and the fairness it buys observable: a
    persistently failing low-sort stream (`estream-a`) must not prevent
    later-sorting streams from getting their own turn on later ticks, and must
    itself keep getting a turn each sweep rather than being dropped."""
    monkeypatch.setattr(inv, "TICK_STREAM_LIMIT", 1)

    streams = ("estream-a", "estream-b", "estream-c")
    for stream_id in streams:
        workspace.record(esc._source(1, f"{stream_id}-1", FILES_A, stream=stream_id))
        _drain(workspace, stream_id=stream_id)  # baseline only, no backlog yet
        workspace.record(
            esc._source(2, f"{stream_id}-2", FILES_A, predecessor=f"{stream_id}-1", stream=stream_id)
        )
        assert _progress(workspace, stream_id=stream_id) == (2, 1, None)

    original_advance = inv.advance_invalidation
    failures: list[str] = []

    def flaky_advance(*args: Any, stream_id: str, **kwargs: Any) -> Any:
        if stream_id == "estream-a":
            failures.append(stream_id)
            raise RuntimeError("boom")
        return original_advance(*args, stream_id=stream_id, **kwargs)

    monkeypatch.setattr(inv, "advance_invalidation", flaky_advance)

    runner = _runner(workspace)
    for _ in range(3):
        runner.clock.advance_wall(1.0)
        runner.drain_pending_invalidation()

    # Three ticks, one stream each: the always-failing lowest-sort stream took
    # exactly one of them, and the other two each took one of the remaining
    # streams -- neither later stream was starved by the failing one.
    assert len(failures) == 1
    assert _progress(workspace, stream_id="estream-a") == (2, 1, None)
    assert _progress(workspace, stream_id="estream-b") == (2, 2, None)
    assert _progress(workspace, stream_id="estream-c") == (2, 2, None)

    # A fourth tick wraps the fair cursor back to the start of the sweep and
    # gives the failing stream another turn -- it is not dropped forever.
    runner.clock.advance_wall(1.0)
    runner.drain_pending_invalidation()
    assert len(failures) == 2
    assert _progress(workspace, stream_id="estream-a") == (2, 1, None)


def test_tick_error_output_never_leaks_a_stream_id_or_raw_exception_text(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Service stderr on a tick failure must be a fixed, bounded diagnostic
    code -- never the stream id or the exception's own text, both of which
    can carry a workspace's source identifiers."""
    secret_stream_id = "estream-do-not-print-me"
    workspace.record(esc._source(1, "esnap-a", FILES_A, stream=secret_stream_id))
    workspace.record(
        esc._source(2, "esnap-b", FILES_A, predecessor="esnap-a", stream=secret_stream_id)
    )
    runner = _runner(workspace)

    def leaky_advance(*args: Any, stream_id: str, **kwargs: Any) -> Any:
        raise RuntimeError(f"leaked secret detail about {stream_id} at /private/path")

    monkeypatch.setattr(inv, "advance_invalidation", leaky_advance)
    runner.drain_pending_invalidation()
    captured = capsys.readouterr()
    assert secret_stream_id not in captured.err
    assert "leaked secret detail" not in captured.err
    assert "/private/path" not in captured.err
    assert "INVALIDATION_TICK_ERROR: advance_failed" in captured.err

    monkeypatch.undo()

    def leaky_select(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"leaked secret detail about {secret_stream_id}")

    monkeypatch.setattr(inv, "select_pending_streams", leaky_select)
    runner.drain_pending_invalidation()
    captured = capsys.readouterr()
    assert secret_stream_id not in captured.err
    assert "leaked secret detail" not in captured.err
    assert "INVALIDATION_TICK_ERROR: select_failed" in captured.err
