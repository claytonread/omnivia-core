"""Validated, immutable connector wire and state models (V06-8).

Standard library only, like every other public contract surface in this
package. The domains below are not invented here: each one is the exact domain
the authoritative workspace schema already accepts for the column the value
ends up in, so a value this module admits cannot be refused later by a trigger,
and a value a trigger would refuse is refused here -- at the boundary, where
the caller can still do something about it.

Two consequences of that are worth stating rather than leaving to be
rediscovered. Checksums are `sha256:<64 lowercase hex>` and nothing else,
because the staged-source relation that records declared-versus-computed
content identity accepts only that form. And `permission_labels` must arrive
strictly ascending and unique, because ACL ingestion has to be replayable: a
connector that reports the same labels in a different order on a later pass
would otherwise produce a different record for the same source state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from omnivia_core.contracts.v1.generated import (
    FROZEN_RETRY_CLASSES,
    RETRY_CLASS_NON_RETRYABLE,
)

#: `sha256:<64 lowercase hex>`: the only content identity the durable
#: staged-source and blob relations accept.
CHECKSUM_PATTERN: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")

#: Opaque identifier domain, shared by connector ids and source-native ids.
IDENTIFIER_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

#: Dotted lowercase token domain, shared by source kinds and permission labels.
TOKEN_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\Z")

#: `type/subtype`, as the evidence and staged-source relations accept it.
MEDIA_TYPE_PATTERN: Final = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*\Z"
)

MAX_TOKEN_LENGTH: Final = 128
MAX_LOCATOR_LENGTH: Final = 2048
MAX_MESSAGE_LENGTH: Final = 2048
MAX_CURSOR_TOKEN_LENGTH: Final = 4096

#: The most times one item may be tried before it is dead-lettered. The durable
#: `attempts` column accepts exactly this range, so a caller configuring more
#: retries than this would build a dead letter no schema will store.
MAX_DEAD_LETTER_ATTEMPTS: Final = 256

#: The most changes one page may report. A page is held whole in memory and
#: applied in one transaction, so an unbounded page is an unbounded transaction;
#: a connector with more to say says it in the next page, which is what
#: `has_more` is for.
MAX_BATCH_CHANGES: Final = 1024

#: The frozen retry vocabulary, reused verbatim from the application contract.
#: A connector classifies its own failures in the same terms the rest of the
#: system already uses, so a classification never has to be translated.
RETRY_CLASSES: Final[frozenset[str]] = frozenset(FROZEN_RETRY_CLASSES)


class ConnectorContractError(ValueError):
    """A connector value is outside the accepted domain."""


class ChangeKind(Enum):
    """What one reported change says happened to a source item."""

    UPSERTED = "upserted"
    RENAMED = "renamed"
    DELETED = "deleted"


class HealthState(Enum):
    """How usable a source is right now."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConnectorContractError(message)


def _require_pattern(
    field: str, value: str, pattern: re.Pattern[str], limit: int
) -> None:
    _require(
        isinstance(value, str) and len(value) <= limit and pattern.fullmatch(value)
        is not None,
        f"{field} is outside the accepted domain",
    )


def _require_text(field: str, value: str, limit: int) -> None:
    _require(
        isinstance(value, str) and 0 < len(value) <= limit and "\x00" not in value,
        f"{field} is outside the accepted domain",
    )


@dataclass(frozen=True, slots=True)
class ConnectorCursor:
    """Versioned, opaque connector state.

    `state_version` is the connector's own state format, not the workspace's.
    It travels with the token because a token is only meaningful to the code
    that wrote it: a connector that has moved on to a newer state format must
    be able to tell a stale token from one it can resume.
    """

    state_version: int
    token: str

    def __post_init__(self) -> None:
        _require(
            isinstance(self.state_version, int)
            and not isinstance(self.state_version, bool)
            and 1 <= self.state_version <= 2**31 - 1,
            "state_version must be a positive 32-bit integer",
        )
        _require_text("token", self.token, MAX_CURSOR_TOKEN_LENGTH)

    def to_wire(self) -> dict[str, Any]:
        return {"state_version": self.state_version, "token": self.token}

    @classmethod
    def from_wire(cls, document: dict[str, Any]) -> ConnectorCursor:
        _require(
            set(document) == {"state_version", "token"},
            "cursor document does not carry exactly the cursor fields",
        )
        return cls(state_version=document["state_version"], token=document["token"])


@dataclass(frozen=True, slots=True)
class ConnectorFailure:
    """One classified connector failure."""

    code: str
    message: str
    retry_class: str = RETRY_CLASS_NON_RETRYABLE

    def __post_init__(self) -> None:
        _require_pattern("code", self.code, TOKEN_PATTERN, MAX_TOKEN_LENGTH)
        _require_text("message", self.message, MAX_MESSAGE_LENGTH)
        _require(
            self.retry_class in RETRY_CLASSES,
            "retry_class is not one of the frozen retry classes",
        )

    @property
    def retryable(self) -> bool:
        return self.retry_class != RETRY_CLASS_NON_RETRYABLE

    def to_wire(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retry_class": self.retry_class,
        }

    @classmethod
    def from_wire(cls, document: dict[str, Any]) -> ConnectorFailure:
        _require(
            set(document) == {"code", "message", "retry_class"},
            "failure document does not carry exactly the failure fields",
        )
        return cls(
            code=document["code"],
            message=document["message"],
            retry_class=document["retry_class"],
        )


class ConnectorError(Exception):
    """Raised by a connector to report a classified failure.

    The classification is the payload. A connector that raises anything else is
    treated as non-retryable, because an unclassified failure is one nobody has
    said is safe to repeat.
    """

    def __init__(self, failure: ConnectorFailure) -> None:
        _require(
            isinstance(failure, ConnectorFailure),
            "a connector error must carry a ConnectorFailure",
        )
        super().__init__(f"{failure.code}: {failure.message}")
        self.failure = failure


@dataclass(frozen=True, slots=True)
class SourceChange:
    """One reported change to one source item, in source-native terms.

    The native id is the source's own identity and is never rewritten, so a
    rename is a locator change under a stable id rather than a delete and a
    create. That distinction is the whole reason `previous_locator` exists.
    """

    change_kind: ChangeKind
    native_id: str
    locator: str | None = None
    previous_locator: str | None = None
    checksum: str | None = None
    media_type: str | None = None
    content_length_bytes: int | None = None
    source_version: str | None = None
    permission_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require(
            isinstance(self.change_kind, ChangeKind),
            "change_kind must be a ChangeKind",
        )
        _require_pattern(
            "native_id", self.native_id, IDENTIFIER_PATTERN, MAX_TOKEN_LENGTH
        )
        if self.locator is not None:
            _require_text("locator", self.locator, MAX_LOCATOR_LENGTH)
        if self.source_version is not None:
            _require_pattern(
                "source_version",
                self.source_version,
                IDENTIFIER_PATTERN,
                MAX_TOKEN_LENGTH,
            )
        _require(
            isinstance(self.permission_labels, tuple),
            "permission_labels must be a tuple",
        )
        for label in self.permission_labels:
            _require_pattern(
                "permission_label", label, TOKEN_PATTERN, MAX_TOKEN_LENGTH
            )
        _require(
            list(self.permission_labels) == sorted(set(self.permission_labels)),
            "permission_labels must be unique and strictly ascending",
        )

        if self.change_kind is ChangeKind.DELETED:
            _require(
                self.checksum is None
                and self.media_type is None
                and self.content_length_bytes is None
                and self.previous_locator is None,
                "a deletion carries no content, and no previous locator",
            )
            return

        _require(
            self.checksum is not None
            and CHECKSUM_PATTERN.fullmatch(self.checksum) is not None,
            "checksum must be sha256:<64 lowercase hex>",
        )
        _require(
            self.media_type is not None
            and len(self.media_type) <= 255
            and MEDIA_TYPE_PATTERN.fullmatch(self.media_type) is not None,
            "media_type is outside the accepted domain",
        )
        _require(
            isinstance(self.content_length_bytes, int)
            and not isinstance(self.content_length_bytes, bool)
            and self.content_length_bytes >= 0,
            "content_length_bytes must be a non-negative integer",
        )
        _require(self.locator is not None, "a present item must carry a locator")
        if self.change_kind is ChangeKind.RENAMED:
            previous = self.previous_locator
            _require(
                previous is not None, "a rename must carry the locator it moved from"
            )
            assert previous is not None
            _require_text("previous_locator", previous, MAX_LOCATOR_LENGTH)
            _require(
                self.previous_locator != self.locator,
                "a rename must move the item to a different locator",
            )
        else:
            _require(
                self.previous_locator is None,
                "only a rename carries a previous locator",
            )

    def to_wire(self) -> dict[str, Any]:
        return {
            "change_kind": self.change_kind.value,
            "native_id": self.native_id,
            "locator": self.locator,
            "previous_locator": self.previous_locator,
            "checksum": self.checksum,
            "media_type": self.media_type,
            "content_length_bytes": self.content_length_bytes,
            "source_version": self.source_version,
            "permission_labels": list(self.permission_labels),
        }


@dataclass(frozen=True, slots=True)
class SourceBatch:
    """One page of changes and the cursor that page ends at.

    The cursor belongs to the *end* of the batch, so committing the batch and
    committing the cursor is one decision rather than two that can disagree. Its
    `state_version` must be the connector's own: a page cannot end at a state
    format its author does not write, and a caller that accepted one would
    durably store a token nothing can interpret.

    `has_more` says another page follows *this cursor*, so the next page must
    end at a cursor this one has not already returned. A repeated cursor under
    `has_more` describes a loop rather than progress, and the caller is entitled
    to refuse it.
    """

    changes: tuple[SourceChange, ...]
    cursor: ConnectorCursor
    has_more: bool = False

    def __post_init__(self) -> None:
        _require(isinstance(self.changes, tuple), "changes must be a tuple")
        _require(
            len(self.changes) <= MAX_BATCH_CHANGES,
            f"a batch may report at most {MAX_BATCH_CHANGES} changes",
        )
        for change in self.changes:
            _require(
                isinstance(change, SourceChange),
                "every change must be a SourceChange",
            )
        native_ids = [change.native_id for change in self.changes]
        _require(
            len(set(native_ids)) == len(native_ids),
            "a batch must report each source item at most once",
        )
        _require(
            isinstance(self.cursor, ConnectorCursor), "cursor must be a ConnectorCursor"
        )
        _require(isinstance(self.has_more, bool), "has_more must be a bool")

    def to_wire(self) -> dict[str, Any]:
        return {
            "changes": [change.to_wire() for change in self.changes],
            "cursor": self.cursor.to_wire(),
            "has_more": self.has_more,
        }


@dataclass(frozen=True, slots=True)
class SourceHealth:
    """What a connector reports about the source it is bound to."""

    state: HealthState
    detail: str = ""

    def __post_init__(self) -> None:
        _require(isinstance(self.state, HealthState), "state must be a HealthState")
        _require(
            isinstance(self.detail, str)
            and len(self.detail) <= MAX_MESSAGE_LENGTH
            and "\x00" not in self.detail,
            "detail is outside the accepted domain",
        )

    @property
    def usable(self) -> bool:
        return self.state is not HealthState.UNAVAILABLE

    def to_wire(self) -> dict[str, Any]:
        return {"state": self.state.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """One source item this run could not ingest, and why."""

    native_id: str
    failure: ConnectorFailure
    attempts: int

    def __post_init__(self) -> None:
        _require_pattern(
            "native_id", self.native_id, IDENTIFIER_PATTERN, MAX_TOKEN_LENGTH
        )
        _require(
            isinstance(self.failure, ConnectorFailure),
            "failure must be a ConnectorFailure",
        )
        _require(
            isinstance(self.attempts, int)
            and not isinstance(self.attempts, bool)
            and 1 <= self.attempts <= MAX_DEAD_LETTER_ATTEMPTS,
            f"attempts must be between 1 and {MAX_DEAD_LETTER_ATTEMPTS}",
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "native_id": self.native_id,
            "failure": self.failure.to_wire(),
            "attempts": self.attempts,
        }


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    """What one synchronisation run did.

    The counters are one per verdict the run can reach on a single change, and
    they are disjoint: every applied change is counted exactly once. That is
    what makes them readable as the run's state machine rather than as
    statistics. In particular `unchanged` means *nothing durable was written*,
    so an ACL change or a reappearance after a tombstone -- both of which do
    write -- have their own terms rather than hiding inside it.

    `truncated` says the run stopped at one of its own per-run limits with work
    the source still has. The batch it stopped at was not partly consumed: the
    limit is decided from what that batch declared, before any of its content was
    fetched. The cursor is therefore at a committed batch boundary as always, so
    the next run continues from it with a whole budget; nothing was dropped, and
    a caller that ignores the flag simply syncs in more passes.
    """

    run_id: str
    state: str
    ingested: int = 0
    unchanged: int = 0
    relabelled: int = 0
    restored: int = 0
    renamed: int = 0
    deleted: int = 0
    dead_lettered: int = 0
    truncated: bool = False
    cursor: ConnectorCursor | None = None
    failure: ConnectorFailure | None = None

    def __post_init__(self) -> None:
        _require_text("run_id", self.run_id, MAX_TOKEN_LENGTH)
        _require(
            self.state in {"succeeded", "failed", "cancelled"},
            "state must be succeeded, failed or cancelled",
        )
        for field_name in (
            "ingested",
            "unchanged",
            "relabelled",
            "restored",
            "renamed",
            "deleted",
            "dead_lettered",
        ):
            value = getattr(self, field_name)
            _require(
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0,
                f"{field_name} must be a non-negative integer",
            )
        _require(isinstance(self.truncated, bool), "truncated must be a bool")
        # The nested values are validated as models, not merely as present or
        # absent: an outcome is the run's public word on what happened, and one
        # carrying a cursor that is not a cursor would be a value that passes
        # here and fails at `to_wire`, where no caller is left to fix it.
        _require(
            self.cursor is None or isinstance(self.cursor, ConnectorCursor),
            "cursor must be a ConnectorCursor or None",
        )
        _require(
            self.failure is None or isinstance(self.failure, ConnectorFailure),
            "failure must be a ConnectorFailure or None",
        )
        _require(
            (self.state == "failed") == (self.failure is not None),
            "a failed run carries a failure, and only a failed run does",
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "ingested": self.ingested,
            "unchanged": self.unchanged,
            "relabelled": self.relabelled,
            "restored": self.restored,
            "renamed": self.renamed,
            "deleted": self.deleted,
            "dead_lettered": self.dead_lettered,
            "truncated": self.truncated,
            "cursor": None if self.cursor is None else self.cursor.to_wire(),
            "failure": None if self.failure is None else self.failure.to_wire(),
        }


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
    "ChangeKind",
    "ConnectorContractError",
    "ConnectorCursor",
    "ConnectorError",
    "ConnectorFailure",
    "DeadLetter",
    "HealthState",
    "SourceBatch",
    "SourceChange",
    "SourceHealth",
    "SyncOutcome",
]
