"""Compatibility facade for the file watcher / indexer models.

Deprecated: import these from ``omnivia_core.ingestion.watcher.models``
instead.
"""
# ruff: noqa: F401 -- names below are re-exported, not used in this module
# This facade *is* the re-export: strict consumers must see every name below as
# explicitly exported from this module, exactly as they did before conversion.
# The unconverted watcher runtime (`debouncer`) and the hybrid `watcher` barrel
# both take their models from here, so every name has to stay explicitly
# exported.
# mypy: implicit_reexport = True

# The `attr-defined` ignore covers only the intentionally preserved incidental
# bindings this leaf's historical namespace still has to resolve
# (`TYPE_CHECKING`, `annotations`, `dataclass`, `datetime`, `field`, `timezone`,
# and the plain `enum` and `uuid` module bindings). No other error code is
# suppressed.
from omnivia_core.ingestion.watcher.models import (  # type: ignore[attr-defined]
    TYPE_CHECKING,
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
    annotations,
    dataclass,
    datetime,
    enum,
    field,
    timezone,
    uuid,
)
