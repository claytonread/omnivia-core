"""Debouncer implementation for coalescing rapid file events.

The debouncer collects file changes over a configurable time window
before triggering batch processing.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from omnivia_memory.ingestion.watcher.models import (
    DebounceConfig,
    FileChange,
    FileChangeBatch,
)


@dataclass
class _PendingBatch:
    """Internal state for a pending batch of changes."""

    changes: list[FileChange] = field(default_factory=list)
    timer: threading.Timer | None = None
    event_count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class Debouncer:
    """Coalesces rapid file system events into batched operations.

    The debouncer waits for a configurable delay after the first event
    before processing. It can also trigger early flush when the number
    of events exceeds a threshold (min_events).
    """

    def __init__(
        self,
        config: DebounceConfig | None = None,
        on_batch: Callable[[FileChangeBatch], None] | None = None,
    ):
        """Initialize the debouncer.

        Args:
            config: Debounce configuration. Uses defaults if None.
            on_batch: Callback invoked when a batch is ready for processing.
        """
        self._config = config or DebounceConfig()
        self._on_batch = on_batch
        self._pending: dict[str, _PendingBatch] = defaultdict(_PendingBatch)
        self._lock = threading.Lock()

    def push(self, change: FileChange) -> None:
        """Add a file change event to be debounced.

        Args:
            change: The file change event.
        """
        key = self._get_key(change)

        with self._lock:
            batch = self._pending[key]
            batch.changes.append(change)
            batch.event_count += 1

            # Reset timer on each event
            if batch.timer:
                batch.timer.cancel()

            # Check for early flush trigger
            if batch.event_count >= self._config.min_events:
                self._flush(key, batch)
            else:
                delay_ms = self._compute_delay(batch.event_count)
                batch.timer = threading.Timer(delay_ms / 1000.0, self._delayed_flush, [key])
                batch.timer.start()

    def _compute_delay(self, event_count: int) -> int:
        """Compute the delay based on event count.

        The delay increases with event count up to max_delay_ms.
        """
        delay = self._config.initial_delay_ms * event_count
        return min(delay, self._config.max_delay_ms)

    def _get_key(self, change: FileChange) -> str:
        """Get the debounce key for a change. Default to path-based grouping."""
        return change.path.rsplit("/", 1)[0] if "/" in change.path else ""

    def _delayed_flush(self, key: str) -> None:
        """Flush pending changes after timer expires."""
        with self._lock:
            if key in self._pending:
                self._flush(key, self._pending[key])

    def _flush(self, key: str, batch: _PendingBatch) -> None:
        """Flush a batch of changes."""
        if batch.timer:
            batch.timer.cancel()
            batch.timer = None

        if not batch.changes:
            return

        file_batch = FileChangeBatch(
            changes=list(batch.changes),
            debounce_key=key,
        )
        batch.changes.clear()
        batch.event_count = 0

        if self._on_batch:
            self._on_batch(file_batch)

    def flush_all(self) -> list[FileChangeBatch]:
        """Immediately flush all pending batches.

        Returns:
            List of flushed batches.
        """
        batches = []
        with self._lock:
            keys = list(self._pending.keys())
            for key in keys:
                batch = self._pending[key]
                if batch.changes:
                    file_batch = FileChangeBatch(
                        changes=list(batch.changes),
                        debounce_key=key,
                    )
                    batches.append(file_batch)
                    batch.changes.clear()
                    batch.event_count = 0
                    if batch.timer:
                        batch.timer.cancel()
                        batch.timer = None

        # Invoke callbacks outside lock to avoid deadlock
        for batch in batches:
            if self._on_batch:
                self._on_batch(batch)

        return batches

    def clear(self, key: str | None = None) -> None:
        """Clear pending changes.

        Args:
            key: Specific key to clear, or None to clear all.
        """
        with self._lock:
            if key is not None:
                if key in self._pending:
                    batch = self._pending[key]
                    if batch.timer:
                        batch.timer.cancel()
                    del self._pending[key]
            else:
                for batch in self._pending.values():
                    if batch.timer:
                        batch.timer.cancel()
                self._pending.clear()

    def pending_count(self, key: str | None = None) -> int:
        """Get the count of pending changes.

        Args:
            key: Specific key to count, or None for total.
        """
        with self._lock:
            if key is not None:
                return len(self._pending.get(key, _PendingBatch()).changes)
            return sum(len(b.changes) for b in self._pending.values())
