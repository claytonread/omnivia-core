"""The connector protocol a source implementation satisfies (V06-8).

One protocol, three methods, no base class to inherit and nothing to register.
A connector is anything that can say how healthy its source is, hand back a
page of changes from a cursor, and produce the bytes behind one change.

Discovery and incremental synchronisation are the same call on purpose.
`fetch(None, ...)` is the initial pass and `fetch(cursor, ...)` is every pass
after it; a connector that needed two entry points would also need to keep
them agreeing about what "where I got to" means.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from omnivia_core.connector.models import (
    ConnectorCursor,
    SourceBatch,
    SourceChange,
    SourceHealth,
)

#: Returns True once the caller wants the run to stop. Checked between items
#: and between batches, never mid-write: cancellation ends a run cleanly at a
#: durable boundary rather than abandoning one part-way.
Cancellation = Callable[[], bool]


@runtime_checkable
class SourceConnector(Protocol):
    """A source of ingestible items."""

    @property
    def connector_id(self) -> str:
        """This connector's stable identity within the workspace."""

    @property
    def source_kind(self) -> str:
        """The evidence source kind every item from this connector carries."""

    @property
    def state_version(self) -> int:
        """The state format this connector writes and can resume from.

        A stored cursor from a *newer* version is refused rather than guessed
        at; one from an older version is discarded and the next run starts
        from the beginning, which is the only safe reading of a token whose
        meaning has changed.
        """

    def health(self) -> SourceHealth:
        """Report the source's current usability. Must not raise."""

    def fetch(
        self, cursor: ConnectorCursor | None, *, cancelled: Cancellation
    ) -> SourceBatch:
        """Return the next page of changes after `cursor`.

        `cursor` is None for an initial synchronisation. Raise `ConnectorError`
        to classify a failure; anything else is treated as non-retryable.

        Three obligations the caller enforces rather than assumes, because each
        one is a durable fault if it is broken:

        - the returned batch's cursor carries this connector's `state_version`;
        - a batch with `has_more` set ends at a cursor this run has not already
          been handed, so the run advances rather than circles;
        - the return value is a `SourceBatch`.
        """

    def content(self, change: SourceChange) -> bytes:
        """Return the exact bytes `change.checksum` names.

        Exact means both declarations: the bytes hash to `change.checksum` and
        there are `change.content_length_bytes` of them. A disagreement is the
        source contradicting itself and is refused, never reconciled.
        """


__all__ = ["Cancellation", "SourceConnector"]
