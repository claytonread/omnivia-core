"""The four-operation `SourceConnector` SPI and its DTOs (V06-8, packet A6-N1).

Standard library only. This module is the *contract* the accepted A6-R3
candidate fixes: `describe`, `migrate_cursor`, `probe`, `poll`, and nothing
else. There is deliberately no `write`, no `delete`, no `schedule`, no
`open_workspace` and no `get_credential`.

Naming, because this package already had a connector protocol. The runtime
foundation's `SourceConnector` (`health` / `fetch` / `content`) in
`omnivia_core.connector.protocols` is what `IngestionCoordinator` drives today
and is untouched. The A6 SPI ships beside it as `SourceConnectorSpi` rather
than shadowing that name, so a reader never has to work out which
`SourceConnector` a call site meant. Where the two contracts already agree the
A6 name is an explicit alias rather than a second type: `HealthStatus` *is*
`SourceHealth` and `CancellationToken` *is* `Cancellation`. Where they disagree
-- a four-field `CursorState` against a two-field `ConnectorCursor`, an
`Observation` against a `SourceChange` -- they are separate types, because
pretending otherwise would mean one of them silently accepting values the other
half of the system refuses.

What a connector is handed is `PollContext`, and its whole surface is listed in
one place below so the storage-boundary assertion has something exact to check.
That assertion is an API ownership boundary and nothing more: it decides what
the host hands a connector, not what connector code can reach through platform
APIs (candidate section 7.2, deliberately excluded by the accepted `CON-P09`
v0.6 posture).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Protocol, TypeAlias, runtime_checkable

from omnivia_core.connector.models import (
    CHECKSUM_PATTERN,
    IDENTIFIER_PATTERN,
    MAX_LOCATOR_LENGTH,
    MAX_MESSAGE_LENGTH,
    MAX_TOKEN_LENGTH,
    MEDIA_TYPE_PATTERN,
    RETRY_CLASSES,
    TOKEN_PATTERN,
    ConnectorContractError,
    SourceHealth,
)
from omnivia_core.connector.protocols import Cancellation
from omnivia_core.contracts.v1.generated import (
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CAPABILITY_NOT_GRANTED,
    ERROR_CODE_DEADLINE_EXCEEDED,
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    RETRY_CLASS_NON_RETRYABLE,
)

# --- error identifiers -------------------------------------------------------

# SPI-local conditions. Candidate section 9: these are *not* added to the frozen
# application error taxonomy, because none of them crosses the application
# boundary. Whether any must later surface as a frozen error is a `CON-P01`
# question and is not decided here.
ERROR_CONNECTOR_CURSOR_NOT_MONOTONIC: Final = "connector_cursor_not_monotonic"
ERROR_CONNECTOR_CURSOR_UNMIGRATABLE: Final = "connector_cursor_unmigratable"
ERROR_CONNECTOR_CURSOR_FOREIGN: Final = "connector_cursor_foreign"
ERROR_CONNECTOR_IDENTITY_UNSTABLE: Final = "connector_identity_unstable"
ERROR_CONNECTOR_STATE_INVALID: Final = "connector_state_invalid"
ERROR_CONNECTOR_SECRET_EXPOSED: Final = "connector_secret_exposed"
ERROR_CONNECTOR_STORAGE_ACCESS_DENIED: Final = "connector_storage_access_denied"
ERROR_CONNECTOR_SCHEDULING_DENIED: Final = "connector_scheduling_denied"
ERROR_CONNECTOR_SOURCE_WRITEBACK_DENIED: Final = "connector_source_writeback_denied"

SPI_LOCAL_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        ERROR_CONNECTOR_CURSOR_NOT_MONOTONIC,
        ERROR_CONNECTOR_CURSOR_UNMIGRATABLE,
        ERROR_CONNECTOR_CURSOR_FOREIGN,
        ERROR_CONNECTOR_IDENTITY_UNSTABLE,
        ERROR_CONNECTOR_STATE_INVALID,
        ERROR_CONNECTOR_SECRET_EXPOSED,
        ERROR_CONNECTOR_STORAGE_ACCESS_DENIED,
        ERROR_CONNECTOR_SCHEDULING_DENIED,
        ERROR_CONNECTOR_SOURCE_WRITEBACK_DENIED,
    }
)

#: The frozen application error identifiers this SPI reuses rather than
#: restating. Every one already exists in the accepted taxonomy.
REUSED_FROZEN_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        ERROR_CODE_AUTHORIZATION_DENIED,
        ERROR_CODE_CAPABILITY_NOT_GRANTED,
        ERROR_CODE_DEADLINE_EXCEEDED,
        ERROR_CODE_INCOMPATIBLE_VERSION,
        ERROR_CODE_INVALID_REQUEST,
        ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    }
)


class ConnectorRefused(ConnectorContractError):
    """A typed refusal carrying the identifier the corpus names.

    The identifier is the payload. A refusal that only carried prose would make
    `CON-C054`'s "reported as an encoding failure, not as a foreign or
    non-monotonic cursor" unverifiable, which is the whole point of the case.
    """

    def __init__(self, error: str, detail: str) -> None:
        super().__init__(f"{error}: {detail}")
        self.error = error
        self.detail = detail


# --- domains -----------------------------------------------------------------

#: The largest value any unsigned SPI counter may carry. Bounded because the
#: canonical digest frames these as shortest unsigned decimal ASCII, and an
#: unbounded integer would frame to an unbounded field.
MAX_UNSIGNED: Final = 2**63 - 1

#: The normative cursor payload bound (`cursor_contract.max_opaque_payload_bytes`).
MAX_CURSOR_PAYLOAD_BYTES: Final = 4096

#: The largest inline `Observation.content` a connector may attach. Bounded
#: because content travels through the same in-memory batch as everything
#: else; a connector with more to say sends it as its own dead-lettered item
#: rather than as an unbounded field.
MAX_OBSERVATION_CONTENT_BYTES: Final = 1_048_576

#: Unpadded base64url. Alphabet *and* length group, so the check stays decidable
#: without decoding: a length of 4n+1 is one no unpadded base64url string can have.
BASE64URL_ALPHABET: Final = re.compile(rb"[A-Za-z0-9_-]*\Z")

#: The most levels of nesting an observation's metadata document may carry.
#: Checked by scanning rather than parsing, because parsing a deeply nested
#: document is itself the hostile-input hazard (`CON-C037`).
MAX_METADATA_DEPTH: Final = 16

#: The scope token domain a connector may enumerate.
SCOPE_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]*(?:[.:][a-z0-9_]+)*\Z")


class IdentityStability(Enum):
    """Declared at discovery, never inferred (`CON-D05`)."""

    SOURCE_NATIVE = "source_native"
    LOCATOR_DERIVED = "locator_derived"


class DeletionSignal(Enum):
    """What one observation says about the presence of its identity."""

    NONE = "none"
    EXPLICIT_DELETE = "explicit_delete"
    ABSENT_FROM_WINDOW = "absent_from_window"


class ConformanceDisposition(Enum):
    """The only three results a conformance case may report (`CON-R26`)."""

    PASSED = "passed"
    FAILED = "failed"
    DECLARED_NOT_APPLICABLE = "declared_not_applicable"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConnectorContractError(message)


def _require_unsigned(field_name: str, value: int) -> None:
    _require(
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_UNSIGNED,
        f"{field_name} must be an unsigned integer within the accepted domain",
    )


def _require_ascii_identifier(field_name: str, value: str) -> None:
    _require(
        isinstance(value, str)
        and len(value) <= MAX_TOKEN_LENGTH
        and value.isascii()
        and IDENTIFIER_PATTERN.fullmatch(value) is not None,
        f"{field_name} is outside the accepted identifier domain",
    )


# --- versioning --------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class SpiVersion:
    """`major.minor`, with independent parts (`CON-D08`)."""

    major: int
    minor: int

    def __post_init__(self) -> None:
        _require_unsigned("major", self.major)
        _require_unsigned("minor", self.minor)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"

    @classmethod
    def parse(cls, text: str) -> SpiVersion:
        parts = text.split(".") if isinstance(text, str) else []
        _require(
            len(parts) == 2 and all(p.isdigit() and p == str(int(p)) for p in parts),
            "an SPI version is exactly major.minor in shortest unsigned decimal",
        )
        return cls(major=int(parts[0]), minor=int(parts[1]))


#: The SPI major this host implements. An unknown *required* major is rejected;
#: an unknown additive *minor* is tolerated in production posture (`CON-D08`).
HOST_SPI_VERSION: Final = SpiVersion(major=1, minor=1)


# --- host-declared limits ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class PollLimits:
    """Host-declared and host-verified (`CON-D11`).

    The host counts the batch itself rather than trusting a declared count, so
    these are the numbers the validator measures against -- not numbers the
    connector is asked to police.
    """

    max_batch_items: int
    max_item_metadata_bytes: int
    max_run_bytes: int
    poll_deadline_ms: int

    def __post_init__(self) -> None:
        for name in (
            "max_batch_items",
            "max_item_metadata_bytes",
            "max_run_bytes",
            "poll_deadline_ms",
        ):
            value = getattr(self, name)
            _require(
                isinstance(value, int)
                and not isinstance(value, bool)
                and 1 <= value <= MAX_UNSIGNED,
                f"{name} must be a positive integer",
            )


# --- credentials -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CredentialHandle:
    """An opaque, non-parseable token carrying no secret material (`CON-C029`).

    The connector may pass it to the host resolver and may do nothing else with
    it. Its `repr` is redacted, because the realistic way a handle reaches a log
    is an exception rendering the context that carried it.
    """

    reference: str

    def __post_init__(self) -> None:
        _require_ascii_identifier("credential handle reference", self.reference)

    def __repr__(self) -> str:
        return "CredentialHandle(<opaque>)"

    def __str__(self) -> str:
        return "<opaque credential handle>"


#: Resolves a handle to material, for the duration of one poll invocation only.
#: The host owns the implementation; `CON-C031` is what makes the scope real.
CredentialResolver: TypeAlias = Callable[[CredentialHandle], bytes]


# --- deadline and cancellation ----------------------------------------------


@dataclass(frozen=True, slots=True)
class Deadline:
    """When this poll must have stopped, in microseconds since the epoch.

    Host-enforced, not connector-enforced (`CON-C036`): a connector that ignores
    it produces a batch the host refuses rather than a batch that runs forever.
    """

    expires_at_us: int

    def __post_init__(self) -> None:
        _require_unsigned("expires_at_us", self.expires_at_us)

    def expired(self, now_us: int) -> bool:
        _require_unsigned("now_us", now_us)
        return now_us > self.expires_at_us


#: The A6 name for the cancellation token. It is the callable the runtime
#: foundation already uses -- an explicit alias rather than a second protocol,
#: so a connector written against either name observes the same object.
CancellationToken: TypeAlias = Cancellation


# --- discovery ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectorDescriptor:
    """What `describe` returns. Side-effect free and byte-identical on repeat.

    `scheduling_declaration` exists so that `CON-C049`'s refusal is a real
    check rather than a vacuous one: a connector *can* express a polling
    interval here, and registration refuses it. Removing the field would make
    the case untestable while looking like it had been satisfied.
    """

    connector_id: str
    connector_version: str
    spi_version: SpiVersion
    identity_stability: IdentityStability
    declared_limits: PollLimits
    enumerable_scopes: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    supported_state_versions: tuple[int, ...] = (1,)
    declared_limitations: tuple[str, ...] = ()
    scheduling_declaration: str | None = None

    def __post_init__(self) -> None:
        _require_ascii_identifier("connector_id", self.connector_id)
        _require_ascii_identifier("connector_version", self.connector_version)
        _require(
            isinstance(self.spi_version, SpiVersion), "spi_version must be an SpiVersion"
        )
        _require(
            isinstance(self.identity_stability, IdentityStability),
            "identity_stability must be an IdentityStability",
        )
        _require(
            isinstance(self.declared_limits, PollLimits),
            "declared_limits must be a PollLimits",
        )
        for name in ("enumerable_scopes", "required_capabilities"):
            values = getattr(self, name)
            _require(isinstance(values, tuple), f"{name} must be a tuple")
            for value in values:
                _require(
                    isinstance(value, str)
                    and len(value) <= MAX_TOKEN_LENGTH
                    and SCOPE_PATTERN.fullmatch(value) is not None,
                    f"{name} entries are outside the accepted token domain",
                )
            _require(
                list(values) == sorted(set(values)),
                f"{name} must be unique and strictly ascending",
            )
        _require(
            isinstance(self.supported_state_versions, tuple)
            and len(self.supported_state_versions) > 0,
            "a connector must support at least one cursor state version",
        )
        for version in self.supported_state_versions:
            _require_unsigned("supported state version", version)
        _require(
            list(self.supported_state_versions)
            == sorted(set(self.supported_state_versions)),
            "supported_state_versions must be unique and strictly ascending",
        )
        _require(
            isinstance(self.declared_limitations, tuple)
            and all(
                isinstance(value, str)
                and TOKEN_PATTERN.fullmatch(value) is not None
                and len(value) <= MAX_TOKEN_LENGTH
                for value in self.declared_limitations
            ),
            "declared_limitations entries are outside the accepted token domain",
        )
        _require(
            list(self.declared_limitations) == sorted(set(self.declared_limitations)),
            "declared_limitations must be unique and strictly ascending",
        )
        _require(
            self.scheduling_declaration is None
            or (
                isinstance(self.scheduling_declaration, str)
                and 0 < len(self.scheduling_declaration) <= MAX_TOKEN_LENGTH
            ),
            "scheduling_declaration is outside the accepted domain",
        )


# --- the poll context --------------------------------------------------------

#: The member names a `PollContext` must never carry. `CON-C032` reads this and
#: the real type surface, so adding a connection to the context fails a test
#: rather than passing review.
POLL_CONTEXT_FORBIDDEN_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "blob_store",
        "blobs",
        "commit",
        "connection",
        "conn",
        "cursor_factory",
        "database",
        "db",
        "engine",
        "executor",
        "get_credential",
        "open_workspace",
        "path",
        "schedule",
        "scheduler",
        "secret",
        "session",
        "storage",
        "store",
        "workspace_path",
        "workspace_root",
        "write",
    }
)


@dataclass(frozen=True, slots=True)
class PollContext:
    """Everything the host hands a connector for one poll, and nothing else.

    No database connection, no blob store handle, no workspace filesystem path,
    no scheduling and no write-back. That is an API ownership boundary; it is
    not a sandbox, and section 7.2 of the candidate says so in the same words.
    """

    workspace_id: str
    run_id: str
    attempt_ordinal: int
    granted_scopes: tuple[str, ...]
    credential_handle: CredentialHandle
    resolve_credential: CredentialResolver
    limits: PollLimits
    deadline: Deadline
    cancellation: CancellationToken

    def __post_init__(self) -> None:
        _require_ascii_identifier("workspace_id", self.workspace_id)
        _require_ascii_identifier("run_id", self.run_id)
        _require(
            isinstance(self.attempt_ordinal, int)
            and not isinstance(self.attempt_ordinal, bool)
            and 1 <= self.attempt_ordinal <= MAX_UNSIGNED,
            "attempt_ordinal must be a positive integer",
        )
        _require(
            isinstance(self.granted_scopes, tuple)
            and all(
                isinstance(scope, str) and SCOPE_PATTERN.fullmatch(scope) is not None
                for scope in self.granted_scopes
            ),
            "granted_scopes entries are outside the accepted token domain",
        )
        _require(
            isinstance(self.credential_handle, CredentialHandle),
            "credential_handle must be a CredentialHandle",
        )
        _require(callable(self.resolve_credential), "resolve_credential must be callable")
        _require(isinstance(self.limits, PollLimits), "limits must be a PollLimits")
        _require(isinstance(self.deadline, Deadline), "deadline must be a Deadline")
        _require(callable(self.cancellation), "cancellation must be callable")


# --- cursor state ------------------------------------------------------------


def classify_payload(payload: bytes) -> str | None:
    """Name the encoding or size defect in a cursor payload, or `None`.

    Decidable without decoding, which is what lets the host bound an opaque
    value it has declared it will not interpret. Size is a separate identifier
    from encoding because the corpus distinguishes them: `CON-C057` is
    `size_limit_exceeded` and `CON-C054`-`CON-C056` are `connector_state_invalid`.
    """
    if not isinstance(payload, bytes):
        return ERROR_CONNECTOR_STATE_INVALID
    if len(payload) > MAX_CURSOR_PAYLOAD_BYTES:
        return ERROR_CODE_SIZE_LIMIT_EXCEEDED
    if BASE64URL_ALPHABET.fullmatch(payload) is None:
        return ERROR_CONNECTOR_STATE_INVALID
    if len(payload) % 4 == 1:
        return ERROR_CONNECTOR_STATE_INVALID
    return None


@dataclass(frozen=True, slots=True)
class CursorState:
    """Exactly four fields. Nothing else is cursor state.

    `payload` is the validated unpadded base64url *text*, as ASCII bytes: that
    is the form the canonical digest frames, so holding it in any other form
    would mean re-deriving it before every lineage check and having two answers
    when they disagreed.
    """

    state_version: int
    payload: bytes
    witness_seq: int
    predecessor_digest: bytes | None = None

    def __post_init__(self) -> None:
        _require_unsigned("state_version", self.state_version)
        _require_unsigned("witness_seq", self.witness_seq)
        defect = classify_payload(self.payload)
        if defect is not None:
            raise ConnectorRefused(defect, "cursor payload")
        _require(
            self.predecessor_digest is None
            or (
                isinstance(self.predecessor_digest, bytes)
                and len(self.predecessor_digest) == 32
            ),
            "predecessor_digest is absent or exactly 32 bytes; there is no other genesis",
        )


@dataclass(frozen=True, slots=True)
class CursorBinding:
    """The workspace and connector a cursor was persisted under.

    Frozen when the parent state is persisted, and reused for every later
    recomputation. A caller-supplied replacement is not accepted, which is the
    whole of `cursor_contract.cursor_bindings`.
    """

    workspace_id: str
    connector_id: str

    def __post_init__(self) -> None:
        _require_ascii_identifier("workspace_id", self.workspace_id)
        _require_ascii_identifier("connector_id", self.connector_id)


@dataclass(frozen=True, slots=True)
class CursorRecord:
    """A persisted cursor: the state, plus the bindings it was frozen under."""

    binding: CursorBinding
    state: CursorState

    def __post_init__(self) -> None:
        _require(
            isinstance(self.binding, CursorBinding), "binding must be a CursorBinding"
        )
        _require(isinstance(self.state, CursorState), "state must be a CursorState")


# --- observations and batches -------------------------------------------------


@dataclass(frozen=True, slots=True)
class Observation:
    """One typed statement about one source identity at one source time.

    Never itself canonical knowledge: the coordinator decides what it means.
    The field names are the corpus's own, so a case's `input.observations`
    entry maps onto this type without a translation layer nobody could check.

    `metadata_bytes` is the connector's declared metadata size, measured
    against `max_item_metadata_bytes`. `metadata_json` is the optional metadata
    document itself, and it is held as text rather than as a parsed object so
    that the depth ceiling can be enforced by scanning -- parsing a deeply
    nested document is the hostile-input hazard, not the defence against it.
    """

    source_native_id: str
    source_locator: str
    observed_at_us: int
    metadata_bytes: int
    content_checksum: str | None = None
    media_type: str | None = None
    source_version: str | None = None
    permission_labels: tuple[str, ...] = ()
    deletion_signal: DeletionSignal = DeletionSignal.NONE
    metadata_json: str = ""
    content: bytes | None = None
    source_event_at_us: int | None = None

    def __post_init__(self) -> None:
        _require_ascii_identifier("source_native_id", self.source_native_id)
        _require(
            isinstance(self.source_locator, str)
            and 0 < len(self.source_locator) <= MAX_LOCATOR_LENGTH
            and self.source_locator.isprintable(),
            "source_locator is outside the accepted domain",
        )
        _require_unsigned("observed_at_us", self.observed_at_us)
        _require_unsigned("metadata_bytes", self.metadata_bytes)
        _require(
            self.source_event_at_us is None
            or (
                isinstance(self.source_event_at_us, int)
                and not isinstance(self.source_event_at_us, bool)
                and 0 <= self.source_event_at_us <= MAX_UNSIGNED
            ),
            "source_event_at_us must be absent or an unsigned integer",
        )
        _require(
            isinstance(self.deletion_signal, DeletionSignal),
            "deletion_signal must be a DeletionSignal",
        )
        _require(
            isinstance(self.permission_labels, tuple)
            and all(
                isinstance(label, str)
                and len(label) <= MAX_TOKEN_LENGTH
                and TOKEN_PATTERN.fullmatch(label) is not None
                for label in self.permission_labels
            ),
            "permission_labels entries are outside the accepted token domain",
        )
        _require(
            list(self.permission_labels) == sorted(set(self.permission_labels)),
            "permission_labels must be unique and strictly ascending",
        )
        _require(
            self.content_checksum is None
            or CHECKSUM_PATTERN.fullmatch(self.content_checksum) is not None,
            "content_checksum must be sha256:<64 lowercase hex>",
        )
        _require(
            self.media_type is None
            or (
                len(self.media_type) <= 255
                and MEDIA_TYPE_PATTERN.fullmatch(self.media_type) is not None
            ),
            "media_type is outside the accepted domain",
        )
        _require(
            self.source_version is None
            or IDENTIFIER_PATTERN.fullmatch(self.source_version) is not None,
            "source_version is outside the accepted identifier domain",
        )
        _require(
            isinstance(self.metadata_json, str) and "\x00" not in self.metadata_json,
            "metadata_json is outside the accepted domain",
        )
        _require(
            self.content is None
            or (
                isinstance(self.content, bytes)
                and len(self.content) <= MAX_OBSERVATION_CONTENT_BYTES
            ),
            f"content must be absent or at most {MAX_OBSERVATION_CONTENT_BYTES} bytes",
        )
        _require(
            self.content is None or self.deletion_signal is not DeletionSignal.EXPLICIT_DELETE,
            "a deletion carries no content",
        )

    @property
    def deleted(self) -> bool:
        return self.deletion_signal is DeletionSignal.EXPLICIT_DELETE


@dataclass(frozen=True, slots=True)
class ItemFailure:
    """One replayable per-item failure. Does not fail its enclosing run."""

    source_native_id: str
    error: str
    retry_class: str = RETRY_CLASS_NON_RETRYABLE
    detail: str = ""

    def __post_init__(self) -> None:
        _require_ascii_identifier("source_native_id", self.source_native_id)
        _require(
            isinstance(self.error, str)
            and (
                self.error in SPI_LOCAL_ERROR_CODES
                or TOKEN_PATTERN.fullmatch(self.error) is not None
            ),
            "error is outside the accepted identifier domain",
        )
        _require(
            self.retry_class in RETRY_CLASSES,
            "retry_class is not one of the frozen retry classes",
        )
        _require(
            isinstance(self.detail, str)
            and len(self.detail) <= MAX_MESSAGE_LENGTH
            and "\x00" not in self.detail,
            "detail is outside the accepted domain",
        )


@dataclass(frozen=True, slots=True)
class Batch:
    """A bounded, ordered sequence of observations, committed whole or not at all."""

    observations: tuple[Observation, ...]
    successor_cursor: CursorState
    item_failures: tuple[ItemFailure, ...] = field(default=())

    def __post_init__(self) -> None:
        _require(
            isinstance(self.observations, tuple)
            and all(isinstance(item, Observation) for item in self.observations),
            "observations must be a tuple of Observation",
        )
        _require(
            isinstance(self.successor_cursor, CursorState),
            "successor_cursor must be a CursorState",
        )
        _require(
            isinstance(self.item_failures, tuple)
            and all(isinstance(item, ItemFailure) for item in self.item_failures),
            "item_failures must be a tuple of ItemFailure",
        )


#: Typed health. The A6 name for the value the runtime foundation already
#: exchanges -- an explicit alias, so `probe` and `health` cannot drift into two
#: different typed statuses meaning the same thing.
HealthStatus: TypeAlias = SourceHealth


# --- the SPI -----------------------------------------------------------------


@runtime_checkable
class SourceConnectorSpi(Protocol):
    """The accepted four-operation SPI (candidate section 4.1).

    Four operations. There is deliberately no `write`, no `delete`, no
    `schedule`, no `open_workspace` and no `get_credential`; `CON-C048` and
    `CON-C049` check that by reading this surface rather than by asserting it in
    prose.
    """

    @property
    def connector_id(self) -> str:
        """Stable identity, e.g. `acme.threadstore`."""

    @property
    def connector_version(self) -> str:
        """Package version, recorded in provenance."""

    @property
    def spi_version(self) -> SpiVersion:
        """The SPI major and minor this connector was written against."""

    def describe(self) -> ConnectorDescriptor:
        """Side-effect free discovery. Requests no credential material."""

    def migrate_cursor(self, cursor: CursorState) -> CursorState:
        """Re-encode a whole cursor state forward.

        Pure: no source access, no credential resolution, byte-identical output
        on repetition. Raise `ConnectorRefused` with
        `connector_cursor_unmigratable` for a state version this connector
        cannot migrate; the host turns that into an explicit resynchronization
        rather than a silent reset.
        """

    def probe(self, ctx: PollContext) -> HealthStatus:
        """Bounded, side-effect-free health probe. Advances no cursor."""

    def poll(self, ctx: PollContext, cursor: CursorState | None) -> Iterator[Batch]:
        """Produce batches from `cursor`, or from the beginning when it is `None`."""


#: The exact operation names the accepted contract fixes, in order.
SPI_OPERATIONS: Final[tuple[str, ...]] = (
    "describe",
    "migrate_cursor",
    "probe",
    "poll",
)

#: Operation names the SPI must *not* offer. `CON-C048` and `CON-C049` read this.
SPI_FORBIDDEN_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "create",
        "delete",
        "get_credential",
        "open_workspace",
        "put",
        "schedule",
        "update",
        "upload",
        "write",
        "write_back",
    }
)


def spi_surface(connector: object) -> tuple[str, ...]:
    """The public callables a connector object offers, in sorted order."""
    return tuple(
        sorted(
            name
            for name in dir(connector)
            if not name.startswith("_") and callable(getattr(connector, name, None))
        )
    )


def observation_identities(observations: Sequence[Observation]) -> tuple[str, ...]:
    """Source-native identities in reported order. Used by determinism checks."""
    return tuple(item.source_native_id for item in observations)


__all__ = [
    "BASE64URL_ALPHABET",
    "ERROR_CONNECTOR_CURSOR_FOREIGN",
    "ERROR_CONNECTOR_CURSOR_NOT_MONOTONIC",
    "ERROR_CONNECTOR_CURSOR_UNMIGRATABLE",
    "ERROR_CONNECTOR_IDENTITY_UNSTABLE",
    "ERROR_CONNECTOR_SCHEDULING_DENIED",
    "ERROR_CONNECTOR_SECRET_EXPOSED",
    "ERROR_CONNECTOR_SOURCE_WRITEBACK_DENIED",
    "ERROR_CONNECTOR_STATE_INVALID",
    "ERROR_CONNECTOR_STORAGE_ACCESS_DENIED",
    "HOST_SPI_VERSION",
    "MAX_CURSOR_PAYLOAD_BYTES",
    "MAX_METADATA_DEPTH",
    "MAX_OBSERVATION_CONTENT_BYTES",
    "MAX_UNSIGNED",
    "POLL_CONTEXT_FORBIDDEN_MEMBERS",
    "REUSED_FROZEN_ERROR_CODES",
    "SPI_FORBIDDEN_OPERATIONS",
    "SPI_LOCAL_ERROR_CODES",
    "SPI_OPERATIONS",
    "Batch",
    "CancellationToken",
    "ConformanceDisposition",
    "ConnectorDescriptor",
    "ConnectorRefused",
    "CredentialHandle",
    "CredentialResolver",
    "CursorBinding",
    "CursorRecord",
    "CursorState",
    "Deadline",
    "DeletionSignal",
    "HealthStatus",
    "IdentityStability",
    "ItemFailure",
    "Observation",
    "PollContext",
    "PollLimits",
    "SourceConnectorSpi",
    "SpiVersion",
    "classify_payload",
    "observation_identities",
    "spi_surface",
]
