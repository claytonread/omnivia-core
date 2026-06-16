"""Tests for the file watcher / indexer module."""

from __future__ import annotations

import time

import pytest

from omnivia_memory.ingestion.watcher.debouncer import Debouncer
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
from omnivia_memory.ingestion.watcher.tracker import SourceTracker


# =============================================================================
# Models Tests
# =============================================================================


class TestFileChange:
    def test_create_file_change(self):
        """FileChange creates with required fields."""
        change = FileChange(path="/tmp/test.md", event_type=FileChangeType.CREATED)
        assert change.path == "/tmp/test.md"
        assert change.event_type == FileChangeType.CREATED
        assert change.old_path is None
        assert change.timestamp is not None

    def test_file_change_with_old_path(self):
        """FileChange supports old_path for MOVE events."""
        change = FileChange(
            path="/tmp/renamed.md",
            event_type=FileChangeType.MOVED,
            old_path="/tmp/original.md",
        )
        assert change.event_type == FileChangeType.MOVED
        assert change.old_path == "/tmp/original.md"

    def test_file_change_to_dict(self):
        """FileChange serializes to dict."""
        change = FileChange(
            path="/tmp/test.md",
            event_type=FileChangeType.MODIFIED,
            timestamp="2026-01-01T00:00:00Z",
        )
        d = change.to_dict()
        assert d["path"] == "/tmp/test.md"
        assert d["event_type"] == "modified"
        assert d["old_path"] is None

    def test_file_change_from_dict(self):
        """FileChange deserializes from dict."""
        data = {
            "path": "/tmp/test.md",
            "event_type": "deleted",
            "timestamp": "2026-01-01T00:00:00Z",
        }
        change = FileChange.from_dict(data)
        assert change.path == "/tmp/test.md"
        assert change.event_type == FileChangeType.DELETED


class TestFileChangeBatch:
    def test_batch_length(self):
        """FileChangeBatch.__len__ returns change count."""
        changes = [
            FileChange(path="/tmp/a.md", event_type=FileChangeType.CREATED),
            FileChange(path="/tmp/b.md", event_type=FileChangeType.CREATED),
        ]
        batch = FileChangeBatch(changes=changes, debounce_key="workspace-1")
        assert len(batch) == 2


class TestDebounceConfig:
    def test_default_config(self):
        """DebounceConfig has sensible defaults."""
        config = DebounceConfig()
        assert config.initial_delay_ms == 500
        assert config.max_delay_ms == 2000
        assert config.min_events == 3


class TestWatchedPath:
    def test_create_watched_path(self):
        """WatchedPath creates with required fields."""
        wp = WatchedPath(path="/tmp/docs", workspace_id="ws-1")
        assert wp.path == "/tmp/docs"
        assert wp.workspace_id == "ws-1"
        assert wp.recursive is True
        assert wp.ignore_patterns == []

    def test_watched_path_serialization(self):
        """WatchedPath round-trips through dict."""
        wp = WatchedPath(
            path="/tmp/docs",
            workspace_id="ws-1",
            recursive=True,
            ignore_patterns=[".git", "__pycache__"],
        )
        d = wp.to_dict()
        restored = WatchedPath.from_dict(d)
        assert restored.path == wp.path
        assert restored.workspace_id == wp.workspace_id
        assert restored.recursive == wp.recursive
        assert restored.ignore_patterns == [".git", "__pycache__"]


class TestSourceReference:
    def test_is_stale_without_hash(self):
        """Reference is stale if last_known_hash is None."""
        ref = SourceReference(
            watched_path="/tmp/docs",
            source_path="/tmp/docs/test.md",
            source_id="src-1",
            workspace_id="ws-1",
            last_known_hash=None,
        )
        assert ref.is_stale("abc123") is True

    def test_is_stale_when_missing(self):
        """Reference is stale if current hash is None (file deleted)."""
        ref = SourceReference(
            watched_path="/tmp/docs",
            source_path="/tmp/docs/test.md",
            source_id="src-1",
            workspace_id="ws-1",
            last_known_hash="abc123",
        )
        assert ref.is_stale(None) is True

    def test_is_stale_on_hash_mismatch(self):
        """Reference is stale if hashes differ."""
        ref = SourceReference(
            watched_path="/tmp/docs",
            source_path="/tmp/docs/test.md",
            source_id="src-1",
            workspace_id="ws-1",
            last_known_hash="abc123",
        )
        assert ref.is_stale("xyz789") is True

    def test_is_not_stale_on_hash_match(self):
        """Reference is not stale if hashes match."""
        ref = SourceReference(
            watched_path="/tmp/docs",
            source_path="/tmp/docs/test.md",
            source_id="src-1",
            workspace_id="ws-1",
            last_known_hash="abc123",
        )
        assert ref.is_stale("abc123") is False

    def test_source_reference_serialization(self):
        """SourceReference round-trips through dict."""
        ref = SourceReference(
            watched_path="/tmp/docs",
            source_path="/tmp/docs/test.md",
            source_id="src-1",
            workspace_id="ws-1",
            last_known_hash="abc123",
            indexed_at="2026-01-01T00:00:00Z",
        )

        restored = SourceReference.from_dict(ref.to_dict())

        assert restored.watched_path == ref.watched_path
        assert restored.source_path == ref.source_path
        assert restored.source_id == ref.source_id
        assert restored.workspace_id == ref.workspace_id
        assert restored.last_known_hash == ref.last_known_hash
        assert restored.indexed_at == ref.indexed_at


class TestIndexerStatus:
    def test_default_status(self):
        """IndexerStatus has expected defaults."""
        status = IndexerStatus(
            state=IndexerState.IDLE,
            workspace_id="ws-1",
        )
        assert status.active_watched_paths == []
        assert status.pending_changes == 0
        assert status.last_index_at is None
        assert status.indexed_count == 0
        assert status.deleted_count == 0


class TestScheduledJob:
    def test_create_scheduled_job(self):
        """ScheduledJob.create generates a job with ID."""
        job = ScheduledJob.create(job_type="reindex", workspace_id="ws-1", delay_seconds=5)
        assert job.job_id is not None
        assert job.job_type == "reindex"
        assert job.workspace_id == "ws-1"
        assert job.delay_seconds == 5


class TestIndexerScheduler:
    def test_scheduler_is_abstract(self):
        """IndexerScheduler.schedule_reindex raises NotImplementedError."""
        scheduler = IndexerScheduler()
        with pytest.raises(NotImplementedError):
            scheduler.schedule_reindex("ws-1")

    def test_scheduler_schedule_full_scan_raises(self):
        """IndexerScheduler.schedule_full_scan raises NotImplementedError."""
        scheduler = IndexerScheduler()
        with pytest.raises(NotImplementedError):
            scheduler.schedule_full_scan("ws-1")

    def test_scheduler_cancel_raises(self):
        """IndexerScheduler.cancel raises NotImplementedError."""
        scheduler = IndexerScheduler()
        with pytest.raises(NotImplementedError):
            scheduler.cancel("job-1")

    def test_scheduler_list_pending_raises(self):
        """IndexerScheduler.list_pending raises NotImplementedError."""
        scheduler = IndexerScheduler()
        with pytest.raises(NotImplementedError):
            scheduler.list_pending()


# =============================================================================
# Debouncer Tests
# =============================================================================


class TestDebouncer:
    def test_push_adds_event(self):
        """push() adds event to pending batch."""
        debouncer = Debouncer()
        change = FileChange(path="/tmp/test.md", event_type=FileChangeType.CREATED)
        debouncer.push(change)
        assert debouncer.pending_count() == 1

    def test_flush_all_returns_batches(self):
        """flush_all() returns all pending batches."""
        debouncer = Debouncer()
        debouncer.push(FileChange(path="/tmp/a.md", event_type=FileChangeType.CREATED))
        debouncer.push(FileChange(path="/tmp/b.md", event_type=FileChangeType.CREATED))

        batches = debouncer.flush_all()
        # Both files share /tmp key, so one batch with 2 changes
        assert len(batches) == 1
        assert len(batches[0].changes) == 2
        assert debouncer.pending_count() == 0

    def test_clear_removes_pending(self):
        """clear() removes pending events."""
        debouncer = Debouncer()
        debouncer.push(FileChange(path="/tmp/test.md", event_type=FileChangeType.CREATED))
        assert debouncer.pending_count() == 1

        debouncer.clear()
        assert debouncer.pending_count() == 0

    def test_clear_specific_key(self):
        """clear(key) removes only that key's events."""
        debouncer = Debouncer()
        debouncer.push(FileChange(path="/tmp/a.md", event_type=FileChangeType.CREATED))
        debouncer.push(FileChange(path="/tmp/b.md", event_type=FileChangeType.CREATED))

        debouncer.clear("/tmp")
        assert debouncer.pending_count() == 0

    def test_on_batch_callback(self):
        """Callback is invoked when batch is flushed."""
        callback_batches = []

        def on_batch(batch: FileChangeBatch):
            callback_batches.append(batch)

        debouncer = Debouncer(
            config=DebounceConfig(initial_delay_ms=50, min_events=100),
            on_batch=on_batch,
        )
        debouncer.push(FileChange(path="/tmp/test.md", event_type=FileChangeType.CREATED))

        # Trigger immediate flush via flush_all
        debouncer.flush_all()
        assert len(callback_batches) == 1
        assert len(callback_batches[0].changes) == 1

    def test_early_flush_on_min_events(self):
        """Batch flushes early when min_events reached."""
        callback_batches = []

        def on_batch(batch: FileChangeBatch):
            callback_batches.append(batch)

        debouncer = Debouncer(
            config=DebounceConfig(initial_delay_ms=1000, min_events=3),
            on_batch=on_batch,
        )

        # Push 3 events (meets min_events threshold)
        debouncer.push(FileChange(path="/tmp/1.md", event_type=FileChangeType.CREATED))
        debouncer.push(FileChange(path="/tmp/2.md", event_type=FileChangeType.CREATED))
        debouncer.push(FileChange(path="/tmp/3.md", event_type=FileChangeType.CREATED))

        # Give callback time to fire
        time.sleep(0.1)

        assert len(callback_batches) == 1
        assert len(callback_batches[0].changes) == 3

    def test_pending_count_by_key(self):
        """pending_count(key) returns count for specific key."""
        debouncer = Debouncer()
        debouncer.push(FileChange(path="/tmp/a.md", event_type=FileChangeType.CREATED))
        debouncer.push(FileChange(path="/tmp/b.md", event_type=FileChangeType.CREATED))

        # Both files share /tmp key
        assert debouncer.pending_count("/tmp") == 2


# =============================================================================
# SourceTracker Tests
# =============================================================================


class TestSourceTracker:
    def test_register_adds_reference(self):
        """register() adds a source reference."""
        tracker = SourceTracker()
        ref = SourceReference(
            watched_path="/tmp/docs",
            source_path="/tmp/docs/test.md",
            source_id="src-1",
            workspace_id="ws-1",
        )
        tracker.register("/tmp/docs", ref)
        assert tracker.count() == 1

    def test_get_reference_returns_registered(self):
        """get_reference() returns previously registered reference."""
        tracker = SourceTracker()
        ref = SourceReference(
            watched_path="/tmp/docs",
            source_path="/tmp/docs/test.md",
            source_id="src-1",
            workspace_id="ws-1",
        )
        tracker.register("/tmp/docs", ref)

        found = tracker.get_reference("/tmp/docs/test.md", "ws-1")
        assert found is not None
        assert found.source_id == "src-1"

    def test_get_reference_missing(self):
        """get_reference() returns None for unregistered path."""
        tracker = SourceTracker()
        found = tracker.get_reference("/tmp/missing.md", "ws-1")
        assert found is None

    def test_unregister_removes_reference(self):
        """unregister() removes a source reference."""
        tracker = SourceTracker()
        ref = SourceReference(
            watched_path="/tmp/docs",
            source_path="/tmp/docs/test.md",
            source_id="src-1",
            workspace_id="ws-1",
        )
        tracker.register("/tmp/docs", ref)
        assert tracker.count() == 1

        result = tracker.unregister("/tmp/docs/test.md", "ws-1")
        assert result is True
        assert tracker.count() == 0

    def test_unregister_missing(self):
        """unregister() returns False for unregistered path."""
        tracker = SourceTracker()
        result = tracker.unregister("/tmp/missing.md", "ws-1")
        assert result is False

    def test_update_hash(self):
        """update_hash() updates the hash."""
        tracker = SourceTracker()
        ref = SourceReference(
            watched_path="/tmp/docs",
            source_path="/tmp/docs/test.md",
            source_id="src-1",
            workspace_id="ws-1",
            last_known_hash="old-hash",
        )
        tracker.register("/tmp/docs", ref)

        result = tracker.update_hash("/tmp/docs/test.md", "ws-1", "new-hash")
        assert result is True

        updated = tracker.get_reference("/tmp/docs/test.md", "ws-1")
        assert updated.last_known_hash == "new-hash"

    def test_list_by_watched_path(self):
        """list_by_watched_path() returns all references under path."""
        tracker = SourceTracker()
        tracker.register(
            "/tmp/docs",
            SourceReference(
                watched_path="/tmp/docs",
                source_path="/tmp/docs/a.md",
                source_id="src-1",
                workspace_id="ws-1",
            ),
        )
        tracker.register(
            "/tmp/docs",
            SourceReference(
                watched_path="/tmp/docs",
                source_path="/tmp/docs/b.md",
                source_id="src-2",
                workspace_id="ws-1",
            ),
        )
        tracker.register(
            "/tmp/other",
            SourceReference(
                watched_path="/tmp/other",
                source_path="/tmp/other/c.md",
                source_id="src-3",
                workspace_id="ws-1",
            ),
        )

        refs = tracker.list_by_watched_path("/tmp/docs")
        assert len(refs) == 2

    def test_list_by_workspace(self):
        """list_by_workspace() returns all references for workspace."""
        tracker = SourceTracker()
        tracker.register(
            "/tmp/docs",
            SourceReference(
                watched_path="/tmp/docs",
                source_path="/tmp/docs/a.md",
                source_id="src-1",
                workspace_id="ws-1",
            ),
        )
        tracker.register(
            "/tmp/other",
            SourceReference(
                watched_path="/tmp/other",
                source_path="/tmp/other/b.md",
                source_id="src-2",
                workspace_id="ws-2",
            ),
        )

        ws1_refs = tracker.list_by_workspace("ws-1")
        ws2_refs = tracker.list_by_workspace("ws-2")

        assert len(ws1_refs) == 1
        assert len(ws2_refs) == 1

    def test_clear_all(self):
        """clear(None) removes all references."""
        tracker = SourceTracker()
        tracker.register(
            "/tmp/docs",
            SourceReference(
                watched_path="/tmp/docs",
                source_path="/tmp/docs/a.md",
                source_id="src-1",
                workspace_id="ws-1",
            ),
        )
        tracker.clear()
        assert tracker.count() == 0

    def test_clear_workspace(self):
        """clear(workspace_id) removes only that workspace's references."""
        tracker = SourceTracker()
        tracker.register(
            "/tmp/docs",
            SourceReference(
                watched_path="/tmp/docs",
                source_path="/tmp/docs/a.md",
                source_id="src-1",
                workspace_id="ws-1",
            ),
        )
        tracker.register(
            "/tmp/other",
            SourceReference(
                watched_path="/tmp/other",
                source_path="/tmp/other/b.md",
                source_id="src-2",
                workspace_id="ws-2",
            ),
        )

        tracker.clear("ws-1")
        assert tracker.count("ws-1") == 0
        assert tracker.count("ws-2") == 1

    def test_get_stale_references(self):
        """get_stale_references() returns references by workspace."""
        tracker = SourceTracker()
        tracker.register(
            "/tmp/docs",
            SourceReference(
                watched_path="/tmp/docs",
                source_path="/tmp/docs/a.md",
                source_id="src-1",
                workspace_id="ws-1",
                last_known_hash="hash1",
            ),
        )
        tracker.register(
            "/tmp/docs",
            SourceReference(
                watched_path="/tmp/docs",
                source_path="/tmp/docs/b.md",
                source_id="src-2",
                workspace_id="ws-1",
                last_known_hash="hash2",
            ),
        )

        stale = tracker.get_stale_references("ws-1")
        assert len(stale) == 2

    def test_count_with_workspace_filter(self):
        """count(workspace_id) returns filtered count."""
        tracker = SourceTracker()
        tracker.register(
            "/tmp/docs",
            SourceReference(
                watched_path="/tmp/docs",
                source_path="/tmp/docs/a.md",
                source_id="src-1",
                workspace_id="ws-1",
            ),
        )
        tracker.register(
            "/tmp/other",
            SourceReference(
                watched_path="/tmp/other",
                source_path="/tmp/other/b.md",
                source_id="src-2",
                workspace_id="ws-2",
            ),
        )

        assert tracker.count("ws-1") == 1
        assert tracker.count("ws-2") == 1
        assert tracker.count() == 2
