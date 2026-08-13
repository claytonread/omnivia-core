"""Public connector contracts for source discovery and incremental sync (V06-8).

This package owns the *contract*: the protocol a connector satisfies and the
validated immutable values it exchanges. It owns no storage, no scheduling and
no transport, and it depends on nothing outside the standard library and this
distribution's own contract package.

The runtime side -- durable runs, checkpoints, dead letters, health history --
lives in `omnivia_core_runtime`, which depends on this package and never the
other way round. Bidirectional synchronisation back to a source is out of
scope by decision, not by omission: nothing here writes to a source.

Two connector surfaces live here and they are not the same surface. The
runtime-foundation `SourceConnector` (`health` / `fetch` / `content`) is what
`IngestionCoordinator` drives today. The accepted A6 SPI --
`describe` / `migrate_cursor` / `probe` / `poll` -- ships as
`SourceConnectorSpi` in `omnivia_core.connector.spi`, with its host-side checks
in `.host`, a deterministic reference implementation in `.fake` and the
corpus-driven conformance kit in `.conformance`. Those four modules are not
re-exported from here: they are a larger surface than most callers of the
contract want, and importing them by name says which of the two connector
worlds a call site is in.
"""

from __future__ import annotations

from omnivia_core.connector.models import (
    CHECKSUM_PATTERN,
    IDENTIFIER_PATTERN,
    MAX_BATCH_CHANGES,
    MAX_CURSOR_TOKEN_LENGTH,
    MAX_DEAD_LETTER_ATTEMPTS,
    MAX_LOCATOR_LENGTH,
    MAX_MESSAGE_LENGTH,
    MAX_TOKEN_LENGTH,
    MEDIA_TYPE_PATTERN,
    RETRY_CLASSES,
    TOKEN_PATTERN,
    ChangeKind,
    ConnectorContractError,
    ConnectorCursor,
    ConnectorError,
    ConnectorFailure,
    DeadLetter,
    HealthState,
    SourceBatch,
    SourceChange,
    SourceHealth,
    SyncOutcome,
)
from omnivia_core.connector.protocols import Cancellation, SourceConnector

__all__ = [
    "CHECKSUM_PATTERN",
    "IDENTIFIER_PATTERN",
    "MAX_BATCH_CHANGES",
    "MAX_CURSOR_TOKEN_LENGTH",
    "MAX_DEAD_LETTER_ATTEMPTS",
    "MAX_LOCATOR_LENGTH",
    "MAX_MESSAGE_LENGTH",
    "MAX_TOKEN_LENGTH",
    "MEDIA_TYPE_PATTERN",
    "RETRY_CLASSES",
    "TOKEN_PATTERN",
    "Cancellation",
    "ChangeKind",
    "ConnectorContractError",
    "ConnectorCursor",
    "ConnectorError",
    "ConnectorFailure",
    "DeadLetter",
    "HealthState",
    "SourceBatch",
    "SourceChange",
    "SourceConnector",
    "SourceHealth",
    "SyncOutcome",
]
