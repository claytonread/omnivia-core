"""Strict-mypy consumer fixture for the watcher models compatibility facade.

``omnivia-memory`` ships ``py.typed``, so a downstream package running
``mypy --strict`` type-checks against these legacy import paths. This module is
that consumer for ``omnivia_memory.ingestion.watcher.models``: it imports all ten
routed symbols from that leaf (never from ``omnivia_core``) and proves the facade
re-exports usefully typed objects -- the exact canonical types, not ``Any`` --
via ``typing.assert_type``.

The one deliberate exception is ``to_dict``. These records were written with a
bare ``-> dict`` return, which strict mypy reads as ``dict[Any, Any]``, and this
fixture asserts exactly that rather than the ``dict[str, Any]`` its ingestion
sibling gets. Pinning the shape the canonical module actually publishes is the
point: the facade must not quietly widen or narrow it, and tightening the
annotation is a change to the canonical contract, not to this facade.

The watcher gets its own fixture rather than joining its ``ingestion`` sibling
because the two are separate routes under separate hybrid barrels, and the
consumer partition enforced by ``tests/test_typed_facade_consumers.py`` is exact:
one fixture per declared route group, with no leaf named twice.

``SourceReference`` is the sharpest case here: the runtime-only
``omnivia_memory.ingestion.watcher.tracker`` defines a *distinct* dataclass of the
same name for its own use, so a facade that re-exported the tracker's class
instead of the models one would still import cleanly and still type-check against
a structural reading. Annotating against the name this leaf publishes is what
keeps the two apart for a typed caller.

It exists to be checked, not run: it is a mypy target in the acceptance
workflow's ``Run strict mypy`` step (see
``tests/test_core_acceptance_workflow.py``). If the facade ever stopped
explicitly re-exporting these names or degraded them to ``Any``, strict mypy
would fail here.
"""

from typing import Any, assert_type

from omnivia_memory.ingestion.watcher.models import (
    DebounceConfig,
    FileChange,
    FileChangeBatch,
    FileChangeType,
    IndexerScheduler,
    IndexerState,
    IndexerStatus,
    ScheduledJob,
    SourceReference,
    WatchedPath,
)


def build_change() -> FileChange:
    """A single filesystem change event, through the facade's own types."""
    change = FileChange(
        path="/workspace/notes.md",
        event_type=FileChangeType.MOVED,
        old_path="/workspace/old.md",
        timestamp="2026-07-30T00:00:00+00:00",
    )
    assert_type(change.path, str)
    assert_type(change.event_type, FileChangeType)
    assert_type(change.old_path, str | None)
    assert_type(change.timestamp, str)
    assert_type(change.to_dict(), dict[Any, Any])
    assert_type(FileChange.from_dict(change.to_dict()), FileChange)
    return change


def build_batch() -> FileChangeBatch:
    """A debounce batch keeps its changes as the facade's own record type."""
    batch = FileChangeBatch(
        changes=[build_change()],
        debounce_key="workspace-1",
        generated_at="2026-07-30T00:00:00+00:00",
    )
    assert_type(batch.changes, list[FileChange])
    assert_type(batch.debounce_key, str)
    assert_type(batch.generated_at, str)
    assert_type(len(batch), int)
    return batch


def build_config() -> DebounceConfig:
    """The debounce policy record and its three bounded integers."""
    config = DebounceConfig(initial_delay_ms=250, max_delay_ms=1000, min_events=2)
    assert_type(config.initial_delay_ms, int)
    assert_type(config.max_delay_ms, int)
    assert_type(config.min_events, int)
    assert_type(config.to_dict(), dict[Any, Any])
    return config


def build_watched_path() -> WatchedPath:
    """A watched root, with its ignore patterns."""
    watched = WatchedPath(
        path="/workspace",
        workspace_id="workspace-1",
        recursive=False,
        ignore_patterns=["*.tmp"],
        created_at="2026-07-30T00:00:00+00:00",
    )
    assert_type(watched.path, str)
    assert_type(watched.workspace_id, str)
    assert_type(watched.recursive, bool)
    assert_type(watched.ignore_patterns, list[str])
    assert_type(watched.created_at, str)
    assert_type(watched.to_dict(), dict[Any, Any])
    assert_type(WatchedPath.from_dict(watched.to_dict()), WatchedPath)
    return watched


def build_source_reference() -> SourceReference:
    """The models leaf's own path-to-source mapping.

    Not the runtime-only tracker's same-named dataclass: this annotation is what
    keeps the collision apart for a strict caller.
    """
    reference = SourceReference(
        watched_path="/workspace",
        source_path="/workspace/notes.md",
        source_id="source-1",
        workspace_id="workspace-1",
        last_known_hash="abc123",
        indexed_at="2026-07-30T00:00:00+00:00",
    )
    assert_type(reference.watched_path, str)
    assert_type(reference.source_path, str)
    assert_type(reference.source_id, str)
    assert_type(reference.workspace_id, str)
    assert_type(reference.last_known_hash, str | None)
    assert_type(reference.indexed_at, str)
    assert_type(reference.is_stale("abc123"), bool)
    assert_type(reference.is_stale(None), bool)
    assert_type(reference.to_dict(), dict[Any, Any])
    assert_type(SourceReference.from_dict(reference.to_dict()), SourceReference)
    return reference


def build_status() -> IndexerStatus:
    """The per-workspace indexer status record, keyed by its state enum."""
    status = IndexerStatus(
        state=IndexerState.DEBOUNCING,
        workspace_id="workspace-1",
        active_watched_paths=["/workspace"],
        pending_changes=3,
        last_index_at="2026-07-30T00:00:00+00:00",
        last_error=None,
        indexed_count=7,
        deleted_count=1,
    )
    assert_type(status.state, IndexerState)
    assert_type(status.workspace_id, str)
    assert_type(status.active_watched_paths, list[str])
    assert_type(status.pending_changes, int)
    assert_type(status.last_index_at, str | None)
    assert_type(status.last_error, str | None)
    assert_type(status.indexed_count, int)
    assert_type(status.deleted_count, int)
    assert_type(status.to_dict(), dict[Any, Any])
    assert_type(status.state.value, str)
    return status


def build_job() -> ScheduledJob:
    """The scheduled-job record and its classmethod constructor."""
    job = ScheduledJob.create("reindex", "workspace-1", delay_seconds=1.5)
    assert_type(job, ScheduledJob)
    assert_type(job.job_id, str)
    assert_type(job.job_type, str)
    assert_type(job.workspace_id, str)
    assert_type(job.scheduled_at, str)
    assert_type(job.delay_seconds, float)
    return job


class RecordingScheduler(IndexerScheduler):
    """The abstract scheduler is subclassable and its signatures are typed.

    Core publishes the interface and defers the implementation to consuming
    applications, so a downstream subclass is exactly the shape a typed caller
    writes -- and it only type-checks if the facade re-exported the real class.
    """

    def __init__(self) -> None:
        self.jobs: list[ScheduledJob] = []

    def schedule_reindex(self, workspace_id: str, delay_seconds: float = 0) -> str:
        job = ScheduledJob.create("reindex", workspace_id, delay_seconds)
        self.jobs.append(job)
        return job.job_id

    def schedule_full_scan(
        self, workspace_id: str, at_timestamp: str | None = None
    ) -> str:
        job = ScheduledJob.create("full_scan", workspace_id)
        self.jobs.append(job)
        return job.job_id

    def cancel(self, job_id: str) -> bool:
        before = len(self.jobs)
        self.jobs = [job for job in self.jobs if job.job_id != job_id]
        return len(self.jobs) != before

    def list_pending(self) -> list[ScheduledJob]:
        return list(self.jobs)


def drive_scheduler() -> int:
    """Use the subclass through the base-class type the facade publishes."""
    scheduler: IndexerScheduler = RecordingScheduler()
    job_id = scheduler.schedule_reindex("workspace-1", delay_seconds=0.5)
    assert_type(job_id, str)
    assert_type(scheduler.schedule_full_scan("workspace-1"), str)
    assert_type(scheduler.cancel(job_id), bool)
    pending = scheduler.list_pending()
    assert_type(pending, list[ScheduledJob])
    return len(pending)


def summarize() -> int:
    """One end-to-end pass through every routed name."""
    batch = build_batch()
    build_config()
    build_watched_path()
    build_source_reference()
    status = build_status()
    build_job()
    total = len(batch) + status.pending_changes + drive_scheduler()
    assert_type(total, int)
    return total
