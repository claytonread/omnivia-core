"""Pure semantic validation for the durable-job and provider-neutral import DTOs
(`import.start`, `job.get`, `job.cancel`, `job.retry`, `job.events`; A2.4, ADR-039).

Structural decoding lives in :mod:`generated`; this module is the same kind of pure,
standard-library-only layer :mod:`semantics` is for the workspace/governed-memory DTOs and
:mod:`semantics_evidence` is for L0 evidence, split out here because a durable job is its
own domain: it has a lifecycle, an attempt history, an event stream, and two control
operations, none of which any other document in this contract reasons about.

Five rules shape everything below.

*Trusted context is an argument, not an option.* Several of the facts this contract turns on
are not decidable from the document in hand: whether a reported import source is the one
`import.start` accepted, whether a `retry_scheduled` recovered a job that was actually
recoverable. Each is checked against context the caller must supply --
`accepted_import_source` on the terminal-result path, `previous` on the control-result path
-- and each is a required keyword with no default, because the failure mode of an optional
argument is that it is omitted, and a rule that silently disappears when a caller forgets it
is not a rule. Where the intrinsic, document-only half of a check is useful on its own, it is
published under its own explicit name (`..._shape`) rather than reached by leaving an
argument out.

*Recovery is one operation.* `job.retry` is the only way a caller asks for recovery, and
whether that recovery retries a failed job from the beginning or resumes a cancelled one
from a checkpoint is chosen from server state and reported back, never selected on the
wire. There is no `job.resume` operation and no `JobControl.resume` member, so nothing
here can be asked to honour a control the contract does not have.

*A state-based refusal is a successful control result, not an API error.* A job that
cannot be cancelled or recovered returns a refusal disposition alongside its current
unchanged handle. `conflict` is never raised merely because a job is already terminal.
Authorization failures, a missing job, and workspace failures stay typed API errors.

*Open vocabularies decode but never grant.* Every state, availability, and disposition is
an open code, so a newer peer's unseen value still decodes and is preserved. An
unrecognized value implies neither terminality nor permission nor acceptance: it fails
safe everywhere it is read.

*Tokens are adapter-owned; bindings are trusted input.* A `job.events` continuation token
is opaque, and nothing here decodes one. A caller that needs to prove what a token was
bound to hands in a trusted, non-wire :class:`JobEventsPageBinding` instead. Token bytes,
encoding, signing, persistence, and expiry are deliberately outside this contract, and
every binding mismatch fails with one uniform diagnostic that never reveals which member
disagreed.

Every public function in this module is a *direct* entry point: a caller may reach it
without going through a tolerant `from_wire` decoder first (a hand-built dataclass, a
value threaded through from another layer, or simply a caller mistake). None of them may
therefore ever raise a raw `TypeError`/`AttributeError` from an unguarded `len()`, regex
match, iteration, or attribute access on a wrongly-typed argument: every such misuse is
caught by an explicit type check up front and reraised as `ContractSemanticError`.

Nothing here parses or produces JSON beyond the canonical-bytes fingerprint used for
idempotency equivalence, and nothing here may depend on runtime, storage, HTTP, MCP, CLI,
Platform, Dev, or a validation framework. Handlers, schedulers, workers, leases,
checkpoints, transports, and durable storage are all out of scope.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from types import MappingProxyType
from typing import Final, NamedTuple

from omnivia_core.contracts.v1.canonical_json import canonical_bytes
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    CONTENT_CHECKSUM_PATTERN,
    IDENTIFIER_PATTERN,
    MEDIA_TYPE_PATTERN,
    OPAQUE_TOKEN_PATTERN,
    OPEN_CODE_PATTERN,
    OPERATION_NAME_PATTERN,
    PURPOSE_PATTERN,
    SCOPE_PATTERN,
    TIMESTAMP_PATTERN,
    WORKSPACE_ID_PATTERN,
    ApiError,
    CapabilityRequirement,
    ContractDecodeError,
    ImportCompletionResult,
    ImportSourceDescriptor,
    ImportStartInput,
    ImportStartResult,
    JobAttempt,
    JobCancelInput,
    JobCancellationOutcome,
    JobCancelResult,
    JobControl,
    JobEvent,
    JobEventsInput,
    JobEventsResult,
    JobGetInput,
    JobGetResult,
    JobHandle,
    JobIdentity,
    JobProgress,
    JobReference,
    JobRetryInput,
    JobRetryResult,
    JobTerminalCancellation,
    JobTerminalFailure,
    JobTerminalSuccess,
    OperationAuditMetadata,
    OperationIdempotencyMetadata,
    OperationJobMetadata,
    OperationPaginationMetadata,
    OperationPreconditionMetadata,
    OperationScope,
    PageMetadata,
    RequestMetadata,
)
from omnivia_core.contracts.v1.semantics import validate_page_limit

__all__ = [
    "CONTENT_CHECKSUM_ALGORITHM",
    "IDEMPOTENCY_CONFLICT",
    "IDEMPOTENCY_DISTINCT",
    "IDEMPOTENCY_EQUIVALENCE_EXCLUDED_INPUTS",
    "IDEMPOTENCY_EQUIVALENCE_INCLUDED_INPUTS",
    "IDEMPOTENCY_REPLAY",
    "IMPORT_COMPLETION_RESULT_SCHEMA_REF",
    "IMPORT_JOB_KIND",
    "IMPORT_START_OPERATION",
    "JOB_CANCELLATION_AVAILABILITIES",
    "JOB_CANCELLATION_AVAILABILITY_CANCELLABLE",
    "JOB_CANCELLATION_AVAILABILITY_CANCELLATION_PENDING",
    "JOB_CANCELLATION_AVAILABILITY_CANCELLED",
    "JOB_CANCELLATION_AVAILABILITY_NOT_CANCELLABLE",
    "JOB_CANCELLATION_DISPOSITIONS",
    "JOB_CANCELLATION_DISPOSITION_CANCELLATION_REQUESTED",
    "JOB_CANCELLATION_DISPOSITION_CANCELLED",
    "JOB_CANCELLATION_DISPOSITION_NOT_CANCELLABLE",
    "JOB_CANCEL_OPERATION",
    "JOB_EVENTS_MAX_PAGE_SIZE",
    "JOB_EVENTS_OPERATION",
    "JOB_EVENTS_ORDERING_SEQUENCE_ASC",
    "JOB_EVENTS_PAGE_BINDING_FORMAT_VERSION",
    "JOB_EVENTS_PAGE_BINDING_REJECTION_CODE",
    "JOB_EVENTS_PAGE_BINDING_REJECTION_MESSAGE",
    "JOB_EVENTS_PAGE_BINDING_REJECTION_RETRY_CLASS",
    "JOB_GET_OPERATION",
    "JOB_LIFECYCLE_OPERATIONS",
    "JOB_LIFECYCLE_OPERATION_POSTURES",
    "JOB_RECOVERY_AVAILABILITIES",
    "JOB_RECOVERY_AVAILABILITY_NOT_RETRYABLE",
    "JOB_RECOVERY_AVAILABILITY_RESUMABLE",
    "JOB_RECOVERY_AVAILABILITY_RETRYABLE",
    "JOB_RECOVERY_DISPOSITIONS",
    "JOB_RECOVERY_DISPOSITION_NOT_RETRYABLE",
    "JOB_RECOVERY_DISPOSITION_RESUME_SCHEDULED",
    "JOB_RECOVERY_DISPOSITION_RETRY_SCHEDULED",
    "JOB_RETRY_OPERATION",
    "JOB_STATES",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_FAILED",
    "JOB_STATE_OBSERVABLE_PROGRESSIONS",
    "JOB_STATE_QUEUED",
    "JOB_STATE_RECOVERY_TRANSITIONS",
    "JOB_STATE_RUNNING",
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_TRANSITIONS",
    "JOB_TERMINAL_RESULT_KINDS",
    "JOB_TERMINAL_RESULT_KIND_IMPORT_COMPLETION",
    "JOB_TERMINAL_STATES",
    "IdempotencyEquivalence",
    "IdempotencyScope",
    "JobEventsPageBinding",
    "JobOperationPosture",
    "classify_idempotent_replay",
    "decode_import_start_input",
    "decode_job_cancel_input",
    "decode_job_events_input",
    "decode_job_get_input",
    "decode_job_retry_input",
    "idempotency_equivalence",
    "is_accepted_cancellation_disposition",
    "is_accepted_recovery_disposition",
    "is_terminal_job_state",
    "permits_cancellation",
    "permits_recovery",
    "validate_import_completion_result",
    "validate_import_completion_result_shape",
    "validate_import_source_descriptor",
    "validate_import_start_input",
    "validate_import_start_result",
    "validate_job_attempt",
    "validate_job_attempt_history",
    "validate_job_cancel_input",
    "validate_job_cancel_result",
    "validate_job_cancel_result_shape",
    "validate_job_control_replay",
    "validate_job_events_input",
    "validate_job_events_page_binding",
    "validate_job_events_result",
    "validate_job_get_input",
    "validate_job_get_result",
    "validate_job_handle",
    "validate_job_observation_progression",
    "validate_job_retry_input",
    "validate_job_retry_result",
    "validate_job_retry_result_shape",
    "validate_job_state_transition",
    "validate_job_terminal_result",
    "validate_operation_idempotency_metadata",
    "validate_request_idempotency",
    "validate_synchronous_job_response_job_reference",
]

# --- bounds restated from the schemas ----------------------------------------
#
# Every bound below is restated from the JSON Schema rather than delegated to it, for the
# reason every length bound in the sibling semantic modules already is: the tolerant
# generated decoder applies no `maxLength`, `minimum`, or `maxItems`, so a payload
# carrying an overlong identifier or a 5000-event page would otherwise decode, pass
# semantic validation, and fail only against strict JSON Schema.

_BOUNDED_VALUE_MAX_LENGTH: Final = 128
_OPAQUE_TOKEN_MAX_LENGTH: Final = 512
_MEDIA_TYPE_MAX_LENGTH: Final = 255
_CONTENT_CHECKSUM_LENGTH: Final = 71
_MESSAGE_MAX_LENGTH: Final = 2048
_ATTEMPTS_MAX_ITEMS: Final = 256
_SCOPES_MAX_ITEMS: Final = 64
_REQUIRED_CAPABILITIES_MAX_ITEMS: Final = 64

_IDENTIFIER_RE: Final = re.compile(IDENTIFIER_PATTERN)
_OPEN_CODE_RE: Final = re.compile(OPEN_CODE_PATTERN)
_OPAQUE_TOKEN_RE: Final = re.compile(OPAQUE_TOKEN_PATTERN)
_OPERATION_NAME_RE: Final = re.compile(OPERATION_NAME_PATTERN)
_WORKSPACE_ID_RE: Final = re.compile(WORKSPACE_ID_PATTERN)
_TIMESTAMP_RE: Final = re.compile(TIMESTAMP_PATTERN)
_MEDIA_TYPE_RE: Final = re.compile(MEDIA_TYPE_PATTERN)
_CONTENT_CHECKSUM_RE: Final = re.compile(CONTENT_CHECKSUM_PATTERN)
_PURPOSE_RE: Final = re.compile(PURPOSE_PATTERN)
_SCOPE_RE: Final = re.compile(SCOPE_PATTERN)

CONTENT_CHECKSUM_ALGORITHM: Final = "sha256"
"""The one digest algorithm `ContentChecksum` admits in v1.

Stated as what this release requires rather than as what a checksum is: admitting a second
algorithm later widens the pattern additively, and a reader that hard-codes the prefix
would then be wrong. Read this constant instead of spelling `sha256:` inline."""

# --- job states ---------------------------------------------------------------

JOB_STATE_QUEUED: Final = "queued"
JOB_STATE_RUNNING: Final = "running"
JOB_STATE_SUCCEEDED: Final = "succeeded"
JOB_STATE_FAILED: Final = "failed"
JOB_STATE_CANCELLED: Final = "cancelled"

JOB_STATES: Final[tuple[str, ...]] = (
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_FAILED,
    JOB_STATE_CANCELLED,
)
"""The `JobState` vocabulary this build knows. `JobState` stays an open code on the wire:
a value outside this tuple is valid, must be preserved, and implies nothing at all."""

JOB_TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_CANCELLED}
)
"""The known states a job never leaves except through an accepted `job.retry`.

An unknown state is deliberately absent from this set and therefore never terminal: this
build cannot know whether a state it has never seen ends a job, and guessing that it does
would let a caller stop polling a job that is still running."""

JOB_STATE_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        JOB_STATE_QUEUED: frozenset(
            {JOB_STATE_QUEUED, JOB_STATE_RUNNING, JOB_STATE_FAILED, JOB_STATE_CANCELLED}
        ),
        JOB_STATE_RUNNING: frozenset(
            {JOB_STATE_RUNNING, JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_CANCELLED}
        ),
        JOB_STATE_SUCCEEDED: frozenset({JOB_STATE_SUCCEEDED}),
        JOB_STATE_FAILED: frozenset({JOB_STATE_FAILED}),
        JOB_STATE_CANCELLED: frozenset({JOB_STATE_CANCELLED}),
    }
)
"""Which known state may follow which, absent an accepted recovery.

Each state maps to itself so that re-observing a job that has not moved is a legal step
rather than a spurious failure. `succeeded` is a closed corner: a job that succeeded is
finished, and nothing -- including `job.retry` -- reopens it."""

JOB_STATE_RECOVERY_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        JOB_STATE_FAILED: frozenset({JOB_STATE_QUEUED}),
        JOB_STATE_CANCELLED: frozenset({JOB_STATE_QUEUED}),
    }
)
"""The only transitions an accepted `job.retry` adds, and the only way a terminal job is
ever left. A failed job is retried and a cancelled resumable one is resumed; both re-enter
`queued` under the same job id, and neither creates an attempt until execution starts."""

JOB_STATE_OBSERVABLE_PROGRESSIONS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        JOB_STATE_QUEUED: frozenset(JOB_STATES),
        JOB_STATE_RUNNING: frozenset(
            {JOB_STATE_RUNNING, JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_CANCELLED}
        ),
        JOB_STATE_SUCCEEDED: frozenset({JOB_STATE_SUCCEEDED}),
        JOB_STATE_FAILED: frozenset({JOB_STATE_FAILED}),
        JOB_STATE_CANCELLED: frozenset({JOB_STATE_CANCELLED}),
    }
)
"""Which known state may be *observed* after which -- :data:`JOB_STATE_TRANSITIONS` closed
over repetition.

Two different questions share one vocabulary. `job.events` records every step a job takes,
so consecutive events are adjacent and the step table applies to them directly. A caller
polling `job.get`, by contrast, samples: it may read a job as `queued` and next as
`succeeded` without ever seeing `running`, and demanding it observe each step would reject a
true statement about a later instant for the crime of having been made a second too late.

What the closure does *not* soften is regression. `succeeded`, `failed`, and `cancelled`
still reach only themselves, `running` never returns to `queued`, and a terminal state is
still left only under an accepted recovery -- which, having gone through `queued`, then
reaches whatever `queued` reaches."""

# --- control availabilities (what a handle says a caller may do) ---------------

JOB_CANCELLATION_AVAILABILITY_CANCELLABLE: Final = "cancellable"
JOB_CANCELLATION_AVAILABILITY_CANCELLATION_PENDING: Final = "cancellation_pending"
JOB_CANCELLATION_AVAILABILITY_CANCELLED: Final = "cancelled"
JOB_CANCELLATION_AVAILABILITY_NOT_CANCELLABLE: Final = "not_cancellable"

JOB_CANCELLATION_AVAILABILITIES: Final[tuple[str, ...]] = (
    JOB_CANCELLATION_AVAILABILITY_CANCELLABLE,
    JOB_CANCELLATION_AVAILABILITY_CANCELLATION_PENDING,
    JOB_CANCELLATION_AVAILABILITY_CANCELLED,
    JOB_CANCELLATION_AVAILABILITY_NOT_CANCELLABLE,
)

JOB_RECOVERY_AVAILABILITY_RETRYABLE: Final = "retryable"
JOB_RECOVERY_AVAILABILITY_RESUMABLE: Final = "resumable"
JOB_RECOVERY_AVAILABILITY_NOT_RETRYABLE: Final = "not_retryable"

JOB_RECOVERY_AVAILABILITIES: Final[tuple[str, ...]] = (
    JOB_RECOVERY_AVAILABILITY_RETRYABLE,
    JOB_RECOVERY_AVAILABILITY_RESUMABLE,
    JOB_RECOVERY_AVAILABILITY_NOT_RETRYABLE,
)

# --- control dispositions (what one control call actually did) ----------------

JOB_CANCELLATION_DISPOSITION_CANCELLATION_REQUESTED: Final = "cancellation_requested"
JOB_CANCELLATION_DISPOSITION_CANCELLED: Final = "cancelled"
JOB_CANCELLATION_DISPOSITION_NOT_CANCELLABLE: Final = "not_cancellable"

JOB_CANCELLATION_DISPOSITIONS: Final[tuple[str, ...]] = (
    JOB_CANCELLATION_DISPOSITION_CANCELLATION_REQUESTED,
    JOB_CANCELLATION_DISPOSITION_CANCELLED,
    JOB_CANCELLATION_DISPOSITION_NOT_CANCELLABLE,
)

JOB_RECOVERY_DISPOSITION_RETRY_SCHEDULED: Final = "retry_scheduled"
JOB_RECOVERY_DISPOSITION_RESUME_SCHEDULED: Final = "resume_scheduled"
JOB_RECOVERY_DISPOSITION_NOT_RETRYABLE: Final = "not_retryable"

JOB_RECOVERY_DISPOSITIONS: Final[tuple[str, ...]] = (
    JOB_RECOVERY_DISPOSITION_RETRY_SCHEDULED,
    JOB_RECOVERY_DISPOSITION_RESUME_SCHEDULED,
    JOB_RECOVERY_DISPOSITION_NOT_RETRYABLE,
)

_ACCEPTED_CANCELLATION_DISPOSITIONS: Final[frozenset[str]] = frozenset(
    {JOB_CANCELLATION_DISPOSITION_CANCELLATION_REQUESTED}
)
"""The one `job.cancel` disposition that reports a mutation this call performed.

`cancelled` is deliberately absent. It means the job was *already* cancelled and the call
therefore changed nothing -- an idempotent no-op that returns the current handle unchanged,
exactly like `not_cancellable`. Counting it as an acceptance would let a `job.cancel` claim
to have cancelled a job it merely found cancelled, and would excuse it from the
unchanged-handle proof every non-mutating disposition owes."""

_ACCEPTED_RECOVERY_DISPOSITIONS: Final[frozenset[str]] = frozenset(
    {
        JOB_RECOVERY_DISPOSITION_RETRY_SCHEDULED,
        JOB_RECOVERY_DISPOSITION_RESUME_SCHEDULED,
    }
)

_RECOVERY_DISPOSITION_PRECONDITIONS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        JOB_RECOVERY_DISPOSITION_RETRY_SCHEDULED: (
            JOB_STATE_FAILED,
            JOB_RECOVERY_AVAILABILITY_RETRYABLE,
        ),
        JOB_RECOVERY_DISPOSITION_RESUME_SCHEDULED: (
            JOB_STATE_CANCELLED,
            JOB_RECOVERY_AVAILABILITY_RESUMABLE,
        ),
    }
)
"""The exact prior `(state, recovery availability)` each accepted recovery requires.

The pairing is not a default that a disposition may pick from: a retry is scheduled for a
failed job its handle called `retryable`, a resume for a cancelled job its handle called
`resumable`, and nothing else yields either. Deriving the expected disposition from the
prior state alone would let a `resumable` handle produce `retry_scheduled` -- an availability
and an action that were never about the same recovery."""

# --- import and job operations ------------------------------------------------

IMPORT_JOB_KIND: Final = "ingestion.import"
IMPORT_START_OPERATION: Final = "import.start"
JOB_GET_OPERATION: Final = "job.get"
JOB_CANCEL_OPERATION: Final = "job.cancel"
JOB_RETRY_OPERATION: Final = "job.retry"
JOB_EVENTS_OPERATION: Final = "job.events"

JOB_LIFECYCLE_OPERATIONS: Final[tuple[str, ...]] = (
    IMPORT_START_OPERATION,
    JOB_GET_OPERATION,
    JOB_CANCEL_OPERATION,
    JOB_RETRY_OPERATION,
    JOB_EVENTS_OPERATION,
)
"""The five operations this module covers: the one that starts a durable job and the four
that observe or control one. The full operation catalogue is a later phase."""

JOB_TERMINAL_RESULT_KIND_IMPORT_COMPLETION: Final = "import_completion"
JOB_TERMINAL_RESULT_KINDS: Final[tuple[str, ...]] = (
    JOB_TERMINAL_RESULT_KIND_IMPORT_COMPLETION,
)
"""The `JobTerminalSuccess.result_kind` vocabulary this build knows.

Open on the wire: an unrecognized kind decodes and is preserved, but `result` must not be
interpreted under it -- a caller that cannot name the shape cannot read the payload."""

IMPORT_COMPLETION_RESULT_SCHEMA_REF: Final = (
    "https://contracts.omnivia.dev/application/v1/jobs.schema.json#/$defs/ImportCompletionResult"
)
"""What `OperationJobMetadata.terminal_result_schema_ref` binds for `import.start`.

Published here so the later operation catalogue binds the reference this module already
enforces semantically, rather than inventing a second answer to the same question."""

# --- job.events pagination ----------------------------------------------------

JOB_EVENTS_MAX_PAGE_SIZE: Final = 1000
"""`job.events`' declared maximum page size, and `JobEventsResult.events`' schema
`maxItems`: the ceiling applied whether or not the request states a `limit`."""

JOB_EVENTS_ORDERING_SEQUENCE_ASC: Final = "sequence_asc"
"""The one ordering a `job.events` pagination session may be bound to. Events are read in
ascending sequence and in no other order, so a token bound to anything else is not a token
this operation issued."""

JOB_EVENTS_PAGE_BINDING_FORMAT_VERSION: Final = 1
"""The token format version this build understands.

The number is part of the binding, not of the token bytes: adapters own encoding, signing,
persistence, and expiry, and this contract owns only what a token must be found to mean."""

JOB_EVENTS_PAGE_BINDING_REJECTION_CODE: Final = "invalid_request"
"""The stable classification every `job.events` binding mismatch reports.

One code for every mismatch, deliberately: a principal, workspace, operation, job,
ordering, format-version, or snapshot disagreement are all "this token is not valid for
this request", and reporting which member disagreed would turn a rejected token into an
oracle for what a valid one contains.

The code itself is the canonical `invalid_request` from `errors.schema.json`'s
`x-omnivia-error-catalogue`, not a spelling this module invented: a token that is not valid
for the request presenting it is one more request the server cannot execute as stated, and a
second, `job.events`-specific alias for that fact would let two peers classify the same
failure differently."""

JOB_EVENTS_PAGE_BINDING_REJECTION_RETRY_CLASS: Final = "non_retryable"
"""Rejecting a continuation token is never worth retrying: the identical request carries
the identical token and fails identically. The caller starts a new tokenless session."""

JOB_EVENTS_PAGE_BINDING_REJECTION_MESSAGE: Final = (
    "job.events continuation token is not valid for this request"
)
"""The one diagnostic every binding mismatch raises, verbatim."""

# --- idempotency --------------------------------------------------------------

IDEMPOTENCY_REPLAY: Final = "replay"
IDEMPOTENCY_CONFLICT: Final = "idempotency_conflict"
IDEMPOTENCY_DISTINCT: Final = "distinct"

IDEMPOTENCY_EQUIVALENCE_INCLUDED_INPUTS: Final[tuple[str, ...]] = (
    "operation_input",
    "purpose",
    "scopes",
    "required_capabilities",
)
"""What two requests must agree on to be the same request.

The operation input is the obvious member; the other three are here because each of them
changes what the server was actually asked to do. A repeat under a different purpose, a
narrower scope set, or a different capability floor is a different request that happens to
reuse a key, and answering it from the first request's result would hand back an outcome
authorized under conditions the second caller never stated.

`required_capabilities` participates in full -- id, minimum version, *and* the `required`
flag -- because "I need this capability" and "use it if you have it" are different requests
even when they name the same capability at the same version."""

IDEMPOTENCY_EQUIVALENCE_EXCLUDED_INPUTS: Final[tuple[str, ...]] = (
    "request_id",
    "correlation_id",
    "trace_id",
    "deadline_ms",
    "client",
)
"""What two requests may freely differ on and still be the same request.

These are exactly the fields a retry is *expected* to change: a new attempt id, a new trace,
a fresh time budget, a client that has since been upgraded. Folding any of them into the
fingerprint would make every honest retry an `idempotency_conflict`, which is the failure
mode idempotency keys exist to prevent."""


# --- direct-entry type guards -------------------------------------------------


def _require_type(value: object, expected: type, label: str) -> None:
    """Raise unless `value` is an instance of `expected`.

    The guard every direct-entry-point function in this module runs before doing anything
    else with an argument it did not itself decode: a caller reaching these functions
    without a tolerant `from_wire` decode in front (a hand-built dataclass, a plain dict,
    `None`, a wrong-typed field on an otherwise-valid dataclass) must see a
    `ContractSemanticError`, never a raw `TypeError`/`AttributeError` from whatever this
    module does with `value` next.
    """
    if isinstance(value, bool) and expected is not bool:
        raise ContractSemanticError(f"{label}: expected {expected!r}, got bool")
    if not isinstance(value, expected):
        raise ContractSemanticError(f"{label}: expected {expected!r}, got {type(value).__name__}")


def _require_str(value: object, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ContractSemanticError(f"{label}: expected a string, got {type(value).__name__}")
    return value


def _require_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractSemanticError(f"{label}: expected an integer, got {type(value).__name__}")
    return value


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractSemanticError(f"{label}: expected a boolean, got {type(value).__name__}")
    return value


def _require_sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractSemanticError(f"{label}: expected a sequence, got {type(value).__name__}")
    return value


def _require_non_negative(value: object, label: str) -> int:
    number = _require_int(value, label)
    if number < 0:
        raise ContractSemanticError(f"{label}: {number!r} must be zero or greater")
    return number


# --- bounded pattern-vocabulary validation ------------------------------------


def _validate_identifier(value: object, label: str) -> str:
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _IDENTIFIER_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid Identifier")
    return text


def _validate_open_code(value: object, label: str) -> str:
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _OPEN_CODE_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid OpenCode")
    return text


def _validate_opaque_token(value: object, label: str) -> str:
    text = _require_str(value, label)
    if len(text) > _OPAQUE_TOKEN_MAX_LENGTH or not _OPAQUE_TOKEN_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid OpaqueToken")
    return text


def _validate_operation_name(value: object, label: str) -> str:
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _OPERATION_NAME_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid OperationName")
    return text


def _validate_workspace_id(value: object, label: str) -> str:
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _WORKSPACE_ID_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid WorkspaceId")
    return text


def _validate_message(value: object, label: str) -> str:
    text = _require_str(value, label)
    if len(text) > _MESSAGE_MAX_LENGTH:
        raise ContractSemanticError(f"{label} exceeds the maximum length of {_MESSAGE_MAX_LENGTH}")
    return text


def _parse_timestamp(value: object, label: str) -> datetime:
    text = _require_str(value, label)
    if not _TIMESTAMP_RE.match(text):
        raise ContractSemanticError(
            f"{label}: {text!r} is not a canonical RFC 3339 UTC timestamp "
            "(naive, offset, and malformed spellings are all rejected)"
        )
    try:
        return datetime.fromisoformat(text)
    except ValueError as error:
        raise ContractSemanticError(
            f"{label}: {text!r} is not a valid RFC 3339 timestamp"
        ) from error


# --- open-vocabulary readings that fail safe ----------------------------------


def is_terminal_job_state(state: object) -> bool:
    """Whether `state` is a state this build knows a job never leaves on its own.

    An unknown state is never terminal. That is the fail-safe direction: treating an
    unrecognized state as terminal would stop a caller polling a job that is still running,
    while treating it as non-terminal costs nothing but another read.
    """
    return isinstance(state, str) and state in JOB_TERMINAL_STATES


def permits_cancellation(availability: object) -> bool:
    """Whether a handle carrying `availability` says `job.cancel` would be accepted.

    Only `cancellable` does. `cancellation_pending` and `cancelled` are already-answered
    states rather than fresh permission, `not_cancellable` is a refusal, and an unknown
    value grants nothing: a permission this build cannot recognize is not a permission.

    A non-string argument is not a permission either, and never raises: this is a reader, so
    every value that is not the one word that grants simply does not grant.
    """
    return (
        isinstance(availability, str)
        and availability == JOB_CANCELLATION_AVAILABILITY_CANCELLABLE
    )


def permits_recovery(availability: object) -> bool:
    """Whether a handle carrying `availability` says `job.retry` would be accepted.

    `retryable` and `resumable` both do -- they are the two recoveries the one operation
    performs, not two operations. `not_retryable` is a refusal, and an unknown value grants
    nothing. Which of the two recoveries a given handle actually offers still matters at the
    point a recovery is accepted (:data:`_RECOVERY_DISPOSITION_PRECONDITIONS`); this reader
    answers only whether recovery is offered at all.
    """
    return isinstance(availability, str) and availability in {
        JOB_RECOVERY_AVAILABILITY_RETRYABLE,
        JOB_RECOVERY_AVAILABILITY_RESUMABLE,
    }


def is_accepted_cancellation_disposition(disposition: object) -> bool:
    """Whether `disposition` reports a `job.cancel` that actually changed the job.

    Only `cancellation_requested` does. `cancelled` reports a job that was *already*
    cancelled, so the call changed nothing; it is an idempotent no-op that owes the same
    unchanged-handle proof `not_cancellable` owes. An unrecognized disposition never implies
    acceptance either: a control outcome this build cannot read must not be recorded as a
    control that happened.
    """
    return isinstance(disposition, str) and disposition in _ACCEPTED_CANCELLATION_DISPOSITIONS


def is_accepted_recovery_disposition(disposition: object) -> bool:
    """Whether `disposition` reports a `job.retry` that actually scheduled recovery.

    `retry_scheduled` and `resume_scheduled` do. `not_retryable` is a refusal, and an
    unrecognized disposition never implies acceptance.
    """
    return isinstance(disposition, str) and disposition in _ACCEPTED_RECOVERY_DISPOSITIONS


def _validate_state_progression(
    previous: object,
    current: object,
    table: Mapping[str, frozenset[str]],
    *,
    recovery_accepted: object,
    label: str,
) -> None:
    """The shared body of the two state rules, differing only in which table they read.

    Only known-to-known steps are checked either way. A step out of, or into, a state this
    build has never seen is not judged at all: an unknown state implies neither terminality
    nor any particular successor, and inventing an answer for it would either reject a newer
    peer's legitimate lifecycle or bless a transition this build cannot reason about.

    `recovery_accepted` is type-checked rather than merely read for truthiness, because it is
    the single flag that unlocks a step out of a terminal state: a caller that passes `"no"`
    or `1` must be told it made no coherent statement, not silently granted the exception.
    """
    accepted = _require_bool(recovery_accepted, "recovery_accepted")
    before = _require_str(previous, f"{label} (previous)")
    after = _require_str(current, f"{label} (current)")
    if before not in table or after not in JOB_STATES:
        return
    allowed = set(table[before])
    if accepted:
        for recovered in JOB_STATE_RECOVERY_TRANSITIONS.get(before, frozenset()):
            allowed.add(recovered)
            allowed |= set(table.get(recovered, frozenset()))
    if after not in allowed:
        suffix = "" if accepted else " (no accepted job.retry was declared)"
        raise ContractSemanticError(
            f"{label}: a job may not move from {before!r} to {after!r}{suffix}"
        )


def validate_job_state_transition(
    previous: object, current: object, *, recovery_accepted: object = False, label: str = "state"
) -> None:
    """Raise unless a job may move from `previous` to `current` in one step.

    The *adjacent* rule, for the places where every step is on the record: consecutive
    `job.events` entries, and any caller reasoning about a single transition. For two
    observations that may have missed steps in between, use
    :func:`validate_job_observation_progression`, which reads
    :data:`JOB_STATE_OBSERVABLE_PROGRESSIONS` instead.

    `recovery_accepted` is what a caller sets when an accepted `job.retry` sits between the
    two states. It is the only thing that opens `failed -> queued` and `cancelled -> queued`,
    which are the only ways a terminal job is ever left. It never opens anything out of
    `succeeded`: a job that succeeded is finished.
    """
    _validate_state_progression(
        previous,
        current,
        JOB_STATE_TRANSITIONS,
        recovery_accepted=recovery_accepted,
        label=label,
    )


# --- import source ------------------------------------------------------------


def validate_import_source_descriptor(source: object, label: str = "source") -> None:
    """Raise unless `source` is a shape-valid, provider-neutral `ImportSourceDescriptor`.

    The descriptor is immutable and names only an already-staged, server-issued handle plus
    the content facts that handle resolves to. There is nothing here to check about how the
    content is reached, because the wire shape admits no path, URL, inline archive,
    credential, or parser selector to check.
    """
    _require_type(source, ImportSourceDescriptor, label)
    assert isinstance(source, ImportSourceDescriptor)
    _validate_opaque_token(source.staged_source_ref, f"{label}.staged_source_ref")
    _validate_open_code(source.source_kind, f"{label}.source_kind")
    checksum = _require_str(source.content_checksum, f"{label}.content_checksum")
    if len(checksum) != _CONTENT_CHECKSUM_LENGTH or not _CONTENT_CHECKSUM_RE.fullmatch(checksum):
        raise ContractSemanticError(
            f"{label}.content_checksum: {checksum!r} is not a valid ContentChecksum "
            f"({CONTENT_CHECKSUM_ALGORITHM}: followed by 64 lowercase hex characters)"
        )
    _require_non_negative(source.content_length_bytes, f"{label}.content_length_bytes")
    media_type = _require_str(source.media_type, f"{label}.media_type")
    if len(media_type) > _MEDIA_TYPE_MAX_LENGTH or not _MEDIA_TYPE_RE.fullmatch(media_type):
        raise ContractSemanticError(f"{label}.media_type: {media_type!r} is not a valid MediaType")
    if source.source_version is not None:
        _validate_identifier(source.source_version, f"{label}.source_version")


# --- job identity, progress, attempts, handle ---------------------------------


def _validate_job_identity(identity: object, label: str = "identity") -> JobIdentity:
    _require_type(identity, JobIdentity, label)
    assert isinstance(identity, JobIdentity)
    _validate_opaque_token(identity.job_id, f"{label}.job_id")
    _validate_open_code(identity.job_kind, f"{label}.job_kind")
    _validate_operation_name(identity.originating_operation, f"{label}.originating_operation")
    _validate_identifier(identity.audit_reference, f"{label}.audit_reference")
    if identity.workspace_id is not None:
        _validate_workspace_id(identity.workspace_id, f"{label}.workspace_id")
    return identity


def _validate_job_progress(progress: object, state: str, label: str = "progress") -> None:
    """Raise unless `progress` is internally coherent and agrees with `state`.

    Two rules, both about not overstating what happened. Completed units may never exceed a
    stated total, and a job that *succeeded* while stating a total must have reached it: a
    success reporting 7 of 10 items is either a partial import mislabelled as complete or a
    counter nobody maintained, and neither is something a caller can act on.
    """
    _require_type(progress, JobProgress, label)
    assert isinstance(progress, JobProgress)
    _validate_open_code(progress.unit, f"{label}.unit")
    completed = _require_non_negative(progress.completed_units, f"{label}.completed_units")
    if progress.message is not None:
        _validate_message(progress.message, f"{label}.message")
    if progress.total_units is None:
        return
    total = _require_non_negative(progress.total_units, f"{label}.total_units")
    if completed > total:
        raise ContractSemanticError(
            f"{label}: completed_units {completed} exceeds total_units {total}"
        )
    if state == JOB_STATE_SUCCEEDED and completed != total:
        raise ContractSemanticError(
            f"{label}: a succeeded job stating a total must have completed it, "
            f"got {completed} of {total}"
        )


def validate_job_attempt(attempt: object, label: str = "attempt") -> None:
    """Raise unless `attempt` is one internally coherent execution attempt.

    An attempt carries a terminal error exactly when it failed. That biconditional is the
    whole rule: a `failed` attempt with no error says a job broke and declines to say how,
    and a `succeeded` or `cancelled` attempt carrying one says a job both worked and did
    not. A state this build does not know may carry no error either -- an unreadable state
    is not licence to attach a failure to it.

    Finish times follow the same shape: a `running` attempt has not finished, and every
    attempt in a known terminal state has.

    An attempt exists because execution started, so `queued` is not an attempt state at all.
    Waiting to run is a state of the *job*: a job may sit queued with no attempt, and it may
    sit queued still reporting the finished attempt a recovery retained, but an attempt that
    claims to be queued describes an execution that never began while occupying a number in
    the history of executions that did.
    """
    _require_type(attempt, JobAttempt, label)
    assert isinstance(attempt, JobAttempt)
    number = _require_int(attempt.attempt_number, f"{label}.attempt_number")
    if number < 1:
        raise ContractSemanticError(
            f"{label}.attempt_number: attempts are 1-based, got {number!r}"
        )
    started = _parse_timestamp(attempt.started_at, f"{label}.started_at")
    state = _validate_open_code(attempt.state, f"{label}.state")
    if state == JOB_STATE_QUEUED:
        raise ContractSemanticError(
            f"{label}.state: {JOB_STATE_QUEUED!r} is a state of the job, not of one of its "
            "executions; an attempt exists because execution started"
        )

    finished: datetime | None = None
    if attempt.finished_at is not None:
        finished = _parse_timestamp(attempt.finished_at, f"{label}.finished_at")
        if finished < started:
            raise ContractSemanticError(
                f"{label}: finished_at {attempt.finished_at!r} precedes "
                f"started_at {attempt.started_at!r}"
            )

    if attempt.error is not None:
        _require_type(attempt.error, ApiError, f"{label}.error")
        assert isinstance(attempt.error, ApiError)
        _validate_open_code(attempt.error.code, f"{label}.error.code")
        _validate_message(attempt.error.message, f"{label}.error.message")
        _validate_open_code(attempt.error.retry_class, f"{label}.error.retry_class")
        if state != JOB_STATE_FAILED:
            raise ContractSemanticError(
                f"{label}: only a failed attempt carries a terminal error, state is {state!r}"
            )
    elif state == JOB_STATE_FAILED:
        raise ContractSemanticError(f"{label}: a failed attempt must carry its terminal error")

    if state == JOB_STATE_RUNNING and finished is not None:
        raise ContractSemanticError(f"{label}: a running attempt has not finished")
    if state in JOB_TERMINAL_STATES and finished is None:
        raise ContractSemanticError(f"{label}: a {state} attempt must carry finished_at")


def _validate_attempt_instants(
    attempt: JobAttempt, created: datetime | None, updated: datetime | None, label: str
) -> None:
    """Raise unless every instant `attempt` states falls inside the job's own lifetime.

    A job cannot have executed before it existed or after it was last observed, so these are
    the same bounds whether the attempt arrives inside a complete history or alone on a
    handle.
    """
    for value, instant_label in (
        (attempt.started_at, "started_at"),
        (attempt.finished_at, "finished_at"),
    ):
        if value is None:
            continue
        instant = _parse_timestamp(value, f"{label}.{instant_label}")
        if created is not None and instant < created:
            raise ContractSemanticError(f"{label}.{instant_label} precedes the job's created_at")
        if updated is not None and instant > updated:
            raise ContractSemanticError(f"{label}.{instant_label} follows the job's updated_at")


def validate_job_attempt_history(
    attempts: object,
    *,
    executing: object = False,
    created_at: object = None,
    updated_at: object = None,
    label: str = "attempts",
) -> None:
    """Raise unless `attempts` is an exact chronology of one job's executions.

    Numbering is `1..N` with no gaps and no repeats, so "attempt 3" always means the third
    one. Attempts never overlap and never move backwards: each starts no earlier than the
    previous one finished, and equal instants are allowed because two attempts of the same
    job may legitimately abut. Only the final attempt may be unfinished, and only when the
    handle is `executing` -- an unfinished attempt with a later attempt after it describes
    two executions running at once, and an unfinished final attempt on a job that is not
    running describes an execution nobody is performing.

    Only a `failed` or `cancelled` attempt may be followed by another one. Retrying a failed
    execution and resuming a cancelled one are the two things that produce a next attempt, so
    a `succeeded` attempt is final: an attempt after a success says the job both finished and
    kept going, and nothing in the state machine reopens `succeeded`. An attempt whose state
    this build has never seen is judged for shape only, exactly as an unknown job state is:
    this build cannot know whether a state it does not recognize ends an execution.

    When `created_at`/`updated_at` are supplied, every attempt instant must fall inside
    them: a job cannot have executed before it existed or after it was last observed.

    `executing` is type-checked rather than read for truthiness: it is what licenses an
    unfinished final attempt, and a caller passing a non-boolean has stated nothing.
    """
    is_executing = _require_bool(executing, "executing")
    items = _require_sequence(attempts, label)
    if len(items) > _ATTEMPTS_MAX_ITEMS:
        raise ContractSemanticError(
            f"{label}: {len(items)} attempts exceeds the maximum of {_ATTEMPTS_MAX_ITEMS}"
        )

    created = _parse_timestamp(created_at, "created_at") if created_at is not None else None
    updated = _parse_timestamp(updated_at, "updated_at") if updated_at is not None else None

    previous_finish: datetime | None = None
    for index, item in enumerate(items):
        item_label = f"{label}[{index}]"
        validate_job_attempt(item, item_label)
        assert isinstance(item, JobAttempt)
        if item.attempt_number != index + 1:
            raise ContractSemanticError(
                f"{item_label}.attempt_number: attempts are numbered 1..N contiguously, "
                f"expected {index + 1}, got {item.attempt_number!r}"
            )
        started = _parse_timestamp(item.started_at, f"{item_label}.started_at")
        finished = (
            _parse_timestamp(item.finished_at, f"{item_label}.finished_at")
            if item.finished_at is not None
            else None
        )
        if previous_finish is not None and started < previous_finish:
            raise ContractSemanticError(
                f"{item_label}: attempt {item.attempt_number} starts before attempt "
                f"{index} finished; attempts never overlap or move backward in time"
            )
        is_final = index == len(items) - 1
        if finished is None:
            if not is_final:
                raise ContractSemanticError(
                    f"{item_label}: only the final attempt may be unfinished"
                )
            if not is_executing:
                raise ContractSemanticError(
                    f"{item_label}: an unfinished attempt belongs only to an actively "
                    "executing job"
                )
        if (
            not is_final
            and item.state in JOB_STATES
            and item.state not in {JOB_STATE_FAILED, JOB_STATE_CANCELLED}
        ):
            raise ContractSemanticError(
                f"{item_label}: a {item.state} attempt is final; only a failed or cancelled "
                "attempt is ever followed by another one"
            )
        _validate_attempt_instants(item, created, updated, item_label)
        previous_finish = finished


def _validate_job_control(control: object, state: str, label: str = "control") -> JobControl:
    """Raise unless `control` is shape-valid and coherent with a *known* `state`.

    A handle states what a caller may do *now*, so what it says must follow from where the
    job actually stands: a succeeded job offers no control at all, a failed one offers
    recovery but not cancellation, a cancelled one is already cancelled and may be
    resumable, and a job still queued or running is cancellable (or already has a
    cancellation pending) but has nothing to recover from yet.

    An unknown state is judged only for shape, and so is an unknown *availability*. This
    build cannot know what controls a state it has never seen permits, nor what an
    availability it has never seen means, and guessing in either direction would refuse a
    newer peer's legitimate handle. Nothing is lost by preserving it: an unrecognized
    availability grants no permission at :func:`permits_cancellation` /
    :func:`permits_recovery`, so a control call can never be accepted on the strength of one.
    What is rejected is a *known* availability that contradicts a known state -- a succeeded
    job advertising `cancellable` is not a value this build failed to recognize, it is one it
    recognizes and knows to be false.
    """
    _require_type(control, JobControl, label)
    assert isinstance(control, JobControl)
    cancellation = _validate_open_code(control.cancellation, f"{label}.cancellation")
    recovery = _validate_open_code(control.recovery, f"{label}.recovery")
    if state not in JOB_STATES:
        return control

    allowed_cancellation: frozenset[str]
    allowed_recovery: frozenset[str]
    if state in {JOB_STATE_QUEUED, JOB_STATE_RUNNING}:
        allowed_cancellation = frozenset(
            {
                JOB_CANCELLATION_AVAILABILITY_CANCELLABLE,
                JOB_CANCELLATION_AVAILABILITY_CANCELLATION_PENDING,
            }
        )
        allowed_recovery = frozenset({JOB_RECOVERY_AVAILABILITY_NOT_RETRYABLE})
    elif state == JOB_STATE_SUCCEEDED:
        allowed_cancellation = frozenset({JOB_CANCELLATION_AVAILABILITY_NOT_CANCELLABLE})
        allowed_recovery = frozenset({JOB_RECOVERY_AVAILABILITY_NOT_RETRYABLE})
    elif state == JOB_STATE_FAILED:
        allowed_cancellation = frozenset({JOB_CANCELLATION_AVAILABILITY_NOT_CANCELLABLE})
        allowed_recovery = frozenset(
            {JOB_RECOVERY_AVAILABILITY_RETRYABLE, JOB_RECOVERY_AVAILABILITY_NOT_RETRYABLE}
        )
    else:
        allowed_cancellation = frozenset({JOB_CANCELLATION_AVAILABILITY_CANCELLED})
        allowed_recovery = frozenset(
            {JOB_RECOVERY_AVAILABILITY_RESUMABLE, JOB_RECOVERY_AVAILABILITY_NOT_RETRYABLE}
        )

    if (
        cancellation in JOB_CANCELLATION_AVAILABILITIES
        and cancellation not in allowed_cancellation
    ):
        raise ContractSemanticError(
            f"{label}.cancellation: a {state} job may only report "
            f"{sorted(allowed_cancellation)}, got {cancellation!r}"
        )
    if recovery in JOB_RECOVERY_AVAILABILITIES and recovery not in allowed_recovery:
        raise ContractSemanticError(
            f"{label}.recovery: a {state} job may only report "
            f"{sorted(allowed_recovery)}, got {recovery!r}"
        )
    return control


def validate_job_handle(handle: object, label: str = "job") -> None:
    """Raise unless `handle` is one internally coherent observation of a job.

    Beyond shape, three agreements are enforced. The handle's timestamps bracket its own
    history (`created_at <= updated_at`, and the latest attempt falls between them);
    the controls it advertises follow from its state; and the latest attempt agrees with
    that state -- a `running` job is running *under an attempt*, and a job in a known
    terminal state has finished the attempt that took it there.

    A `queued` job may carry a finished attempt, and that is not a contradiction: an
    accepted `job.retry` returns a job to `queued` without creating an attempt, so the
    previous terminal attempt remains the latest one until execution actually restarts. It
    is exactly that attempt, though -- the finished `failed` or `cancelled` one recovery was
    scheduled from. A queued job reporting a `succeeded` latest attempt claims a job that
    finished and is somehow waiting to run again, and a queued job reporting a `running` one
    claims an execution nobody started; both are rejected. A queued job that has never
    executed simply reports no attempt at all.
    """
    _require_type(handle, JobHandle, label)
    assert isinstance(handle, JobHandle)
    _validate_job_identity(handle.identity, f"{label}.identity")
    state = _validate_open_code(handle.state, f"{label}.state")
    created = _parse_timestamp(handle.created_at, f"{label}.created_at")
    updated = _parse_timestamp(handle.updated_at, f"{label}.updated_at")
    if updated < created:
        raise ContractSemanticError(
            f"{label}: updated_at {handle.updated_at!r} precedes created_at "
            f"{handle.created_at!r}"
        )
    _validate_job_control(handle.control, state, f"{label}.control")
    if handle.progress is not None:
        _validate_job_progress(handle.progress, state, f"{label}.progress")

    if handle.latest_attempt is None:
        if state == JOB_STATE_RUNNING:
            raise ContractSemanticError(
                f"{label}: a running job must report the attempt it is running under"
            )
        if state in {JOB_STATE_SUCCEEDED, JOB_STATE_FAILED}:
            raise ContractSemanticError(
                f"{label}: a {state} job must report the attempt that produced that outcome"
            )
        return

    # The latest attempt is attempt *N*, not attempt 1, so it is validated on its own rather
    # than through `validate_job_attempt_history`: the 1..N contiguity rule belongs to a
    # complete history, and a handle never carries one.
    attempt_label = f"{label}.latest_attempt"
    validate_job_attempt(handle.latest_attempt, attempt_label)
    attempt = handle.latest_attempt
    assert isinstance(attempt, JobAttempt)
    _validate_attempt_instants(attempt, created, updated, attempt_label)
    if state == JOB_STATE_RUNNING and attempt.state != JOB_STATE_RUNNING:
        raise ContractSemanticError(
            f"{label}: a running job's latest attempt must itself be running, "
            f"got {attempt.state!r}"
        )
    if state in {JOB_STATE_SUCCEEDED, JOB_STATE_FAILED} and attempt.state != state:
        raise ContractSemanticError(
            f"{label}: a {state} job's latest attempt must be {state!r}, got {attempt.state!r}"
        )
    if state != JOB_STATE_RUNNING and attempt.finished_at is None:
        raise ContractSemanticError(
            f"{label}: a job that is not running carries no unfinished attempt"
        )
    if (
        state == JOB_STATE_QUEUED
        and attempt.state in JOB_STATES
        and attempt.state not in {JOB_STATE_FAILED, JOB_STATE_CANCELLED}
    ):
        raise ContractSemanticError(
            f"{label}: a queued job's latest attempt is the finished failed or cancelled "
            f"attempt an accepted recovery was scheduled from, got {attempt.state!r}"
        )


def _validate_attempt_progression(
    previous: JobAttempt | None, current: JobAttempt | None, label: str
) -> None:
    """Raise unless `current` is a legal later reading of the same job's latest attempt.

    Attempts only ever move forward. A caller polls a job at instants of its own choosing, so
    it may miss attempt 2 entirely and read 1 then 3 -- skipping is not regression. What is
    rejected is going back: a lower attempt number than one already reported, or the
    disappearance of an attempt the job already reported having made.

    Rewriting the *same* attempt is rejected too. Attempt N is one execution, so its start
    instant is fixed, and once it has finished, everything it says about how it ended --
    its state, its finish instant, its error -- is history. An attempt that finished and then
    reports a different outcome (or unfinishes itself) is not a later observation of that
    execution; it is a second, contradictory account of it. An attempt still running may of
    course finish, which is the one legitimate change to the same number.
    """
    if current is None:
        if previous is None:
            return
        raise ContractSemanticError(
            f"{label}: attempt {previous.attempt_number} was already reported and a later "
            "observation never drops it; a job's execution history only grows"
        )
    if previous is None:
        return
    if current.attempt_number < previous.attempt_number:
        raise ContractSemanticError(
            f"{label}.attempt_number moved backwards from {previous.attempt_number!r} to "
            f"{current.attempt_number!r}; polling may skip an attempt but never unwinds one"
        )
    if current.attempt_number != previous.attempt_number:
        return
    if current.started_at != previous.started_at:
        raise ContractSemanticError(
            f"{label}: attempt {previous.attempt_number} restated its started_at as "
            f"{current.started_at!r}, having reported {previous.started_at!r}; one execution "
            "starts once"
        )
    if previous.finished_at is None:
        return
    if (
        current.finished_at != previous.finished_at
        or current.state != previous.state
        or current.error != previous.error
    ):
        raise ContractSemanticError(
            f"{label}: attempt {previous.attempt_number} had already finished as "
            f"{previous.state!r}, and a finished execution's outcome is history; a later "
            "observation may not rewrite it"
        )


def _validate_progress_progression(previous: JobProgress, current: JobProgress, label: str) -> None:
    """Raise unless `current` is a legal later reading of the same job's counters.

    What a job's counters count is a property of the job, so a handle that starts counting
    `item` and later counts `byte` has silently redefined every number a caller already read.
    Completed units never fall for the same reason: a caller that saw 7 of 10 done and later
    reads 3 cannot tell whether the job lost work or the counter lost meaning.

    A total, once stated, is likewise fixed. `total_units` is the work expected, and a job
    that revises or withdraws it retroactively changes what every fraction a caller already
    read meant. Discovering a total later is fine -- absent then present states more than it
    did before, not something different.
    """
    if previous.unit != current.unit:
        raise ContractSemanticError(
            f"{label}.unit changed from {previous.unit!r} to {current.unit!r}; a job's "
            "progress unit is fixed for its history"
        )
    previous_completed = _require_non_negative(previous.completed_units, f"{label}.completed_units")
    current_completed = _require_non_negative(current.completed_units, f"{label}.completed_units")
    if current_completed < previous_completed:
        raise ContractSemanticError(
            f"{label}.completed_units fell from {previous_completed} to {current_completed}; "
            "completed work is never uncompleted"
        )
    if previous.total_units is None:
        return
    if current.total_units is None:
        raise ContractSemanticError(
            f"{label}.total_units was stated as {previous.total_units!r} and is now absent; a "
            "withdrawn total retroactively unmakes every fraction a caller already read"
        )
    if current.total_units != previous.total_units:
        raise ContractSemanticError(
            f"{label}.total_units changed from {previous.total_units!r} to "
            f"{current.total_units!r}; the work a job expects is fixed once stated"
        )


def validate_job_observation_progression(
    previous: object, current: object, *, recovery_accepted: object = False, label: str = "job"
) -> None:
    """Raise unless `current` is a legal later observation of the same job as `previous`.

    Identity is fixed for a job's whole life, including across an accepted recovery: a
    retried job is the same job, not a new one. Time only moves forward, the state is one
    :data:`JOB_STATE_OBSERVABLE_PROGRESSIONS` permits, the latest attempt only ever advances
    (:func:`_validate_attempt_progression`), and the progress counters only ever grow under a
    fixed unit and a fixed total (:func:`_validate_progress_progression`).

    Nothing here requires a caller to have *seen* every intermediate step. Polling is
    sampling: a job may be read as `queued` and next as `succeeded`, or at attempt 1 and next
    at attempt 3, and both are legal because each observation is a true statement about a
    later instant. That is why the state check reads the observable-progression closure rather
    than the adjacent-step table -- demanding that a caller witness `running` would reject a
    true reading for having arrived a second too late. What is never legal is a *regression*
    -- time, state, attempt number, or completed units going backwards -- or a *rewrite* of
    something an earlier observation already fixed.
    """
    validate_job_handle(previous, f"{label} (previous)")
    validate_job_handle(current, f"{label} (current)")
    assert isinstance(previous, JobHandle)
    assert isinstance(current, JobHandle)
    if previous.identity != current.identity:
        raise ContractSemanticError(
            f"{label}: identity changed between observations; a job keeps its identity for "
            "its whole life, including across an accepted recovery"
        )
    if _parse_timestamp(current.created_at, "created_at") != _parse_timestamp(
        previous.created_at, "created_at"
    ):
        raise ContractSemanticError(f"{label}: created_at changed between observations")
    if _parse_timestamp(current.updated_at, "updated_at") < _parse_timestamp(
        previous.updated_at, "updated_at"
    ):
        raise ContractSemanticError(f"{label}: updated_at moved backwards between observations")
    _validate_state_progression(
        previous.state,
        current.state,
        JOB_STATE_OBSERVABLE_PROGRESSIONS,
        recovery_accepted=recovery_accepted,
        label=f"{label}.state",
    )
    _validate_attempt_progression(
        previous.latest_attempt, current.latest_attempt, f"{label}.latest_attempt"
    )
    if previous.progress is not None and current.progress is not None:
        _validate_progress_progression(previous.progress, current.progress, f"{label}.progress")


# --- terminal results ---------------------------------------------------------


def validate_import_completion_result_shape(result: object, label: str = "result") -> None:
    """Raise unless `result` is an internally consistent `ImportCompletionResult`.

    *Intrinsic checks only*, named so at the call site: everything here is decidable from the
    document alone, and nothing here can tell whether the run it describes is the run that
    was actually performed. Two identities must add up. Every discovered item is accounted
    for exactly once, so `discovered_items` equals created plus skipped plus failed: a report
    whose parts do not sum is an import nobody can audit. And `partial` is not an independent
    claim but exactly `failed_items > 0`, so a successful job can never quietly present an
    incomplete import as a whole one.

    A caller holding the job id and the descriptor `import.start` accepted wants
    :func:`validate_import_completion_result`, which adds the two checks this one structurally
    cannot make.
    """
    _require_type(result, ImportCompletionResult, label)
    assert isinstance(result, ImportCompletionResult)
    _validate_opaque_token(result.import_run_id, f"{label}.import_run_id")
    validate_import_source_descriptor(result.source, f"{label}.source")
    discovered = _require_non_negative(result.discovered_items, f"{label}.discovered_items")
    created = _require_non_negative(
        result.evidence_records_created, f"{label}.evidence_records_created"
    )
    skipped = _require_non_negative(result.skipped_items, f"{label}.skipped_items")
    failed = _require_non_negative(result.failed_items, f"{label}.failed_items")
    partial = _require_bool(result.partial, f"{label}.partial")

    if discovered != created + skipped + failed:
        raise ContractSemanticError(
            f"{label}: discovered_items {discovered} does not equal "
            f"evidence_records_created {created} + skipped_items {skipped} + "
            f"failed_items {failed}"
        )
    if partial != (failed > 0):
        raise ContractSemanticError(
            f"{label}.partial must be exactly failed_items > 0; got partial={partial!r} "
            f"with failed_items={failed}"
        )


def validate_import_completion_result(
    result: object,
    *,
    job_id: object,
    accepted_import_source: object,
    label: str = "result",
) -> None:
    """Raise unless `result` is the completion of the run `job_id` performed over
    `accepted_import_source`.

    The full semantic path. Both context arguments are *required* and neither may be `None`,
    because the two facts they establish are the only ones that make an import report
    trustworthy, and an argument that may be omitted is one that is omitted: a validator that
    quietly skipped these when a caller passed nothing would accept a completion claiming any
    run id and any source at all -- exactly the report an attacker or a bug would produce.

    `import_run_id` must equal `job_id`: the run and the job that performed it are one thing,
    which is why the run id is typed as the same `OpaqueToken` a job id is. `source` must
    equal `accepted_import_source` exactly -- staged handle, source kind, checksum, byte
    length, media type, and source version alike -- so what was imported is never in question
    after the fact. A completion is free to report a smaller or larger import than the caller
    expected; it is not free to report a different *source*.
    """
    validate_import_completion_result_shape(result, label)
    assert isinstance(result, ImportCompletionResult)
    if job_id is None:
        raise ContractSemanticError(
            "job_id: the job that performed this run is required; an import completion "
            "cannot be checked against a run nobody named"
        )
    expected_id = _validate_opaque_token(job_id, "job_id")
    if result.import_run_id != expected_id:
        raise ContractSemanticError(
            f"{label}.import_run_id {result.import_run_id!r} does not equal the job id "
            f"{expected_id!r}"
        )
    if accepted_import_source is None:
        raise ContractSemanticError(
            "accepted_import_source: the immutable descriptor import.start accepted is "
            "required; a reported source that is checked against nothing is self-asserted"
        )
    validate_import_source_descriptor(accepted_import_source, "accepted_import_source")
    if result.source != accepted_import_source:
        raise ContractSemanticError(
            f"{label}.source is not the immutable descriptor import.start accepted"
        )


def _validate_import_terminal_success(
    terminal: JobTerminalSuccess, accepted_import_source: object, label: str
) -> None:
    """Enforce the import-success binding in both directions.

    An `ingestion.import` job started by `import.start` may only succeed with an
    `import_completion` result, and an `import_completion` result may only appear on such a
    job. Checking one direction would leave the other open: a bare opaque payload passed off
    as an import outcome, or an import completion attached to a job that never ran an
    import.

    Once the payload *is* an import completion, `accepted_import_source` is mandatory. A
    successful import that no one can check against the source it was started with is a
    report of an import that may never have read what it says it read.
    """
    identity = terminal.identity
    is_import_job = (
        identity.job_kind == IMPORT_JOB_KIND
        and identity.originating_operation == IMPORT_START_OPERATION
    )
    kind = _validate_open_code(terminal.result_kind, f"{label}.result_kind")
    if is_import_job and kind != JOB_TERMINAL_RESULT_KIND_IMPORT_COMPLETION:
        raise ContractSemanticError(
            f"{label}.result_kind: a successful {IMPORT_JOB_KIND} job started by "
            f"{IMPORT_START_OPERATION} must report "
            f"{JOB_TERMINAL_RESULT_KIND_IMPORT_COMPLETION!r}, got {kind!r}"
        )
    if kind != JOB_TERMINAL_RESULT_KIND_IMPORT_COMPLETION:
        return
    if not is_import_job:
        raise ContractSemanticError(
            f"{label}.result_kind: {JOB_TERMINAL_RESULT_KIND_IMPORT_COMPLETION!r} appears only "
            f"on a {IMPORT_JOB_KIND} job started by {IMPORT_START_OPERATION}, got job_kind "
            f"{identity.job_kind!r} from {identity.originating_operation!r}"
        )
    try:
        completion = ImportCompletionResult.from_wire(terminal.result, f"{label}.result")
    except ContractDecodeError as error:
        # `terminal` is a trusted DTO reaching a direct entry point, so a structurally
        # unusable `result` must surface as a semantic failure rather than as the decode
        # error a wire-facing caller would have seen.
        raise ContractSemanticError(
            f"{label}.result is not a readable {JOB_TERMINAL_RESULT_KIND_IMPORT_COMPLETION} "
            f"payload: {error}"
        ) from error
    if accepted_import_source is None:
        raise ContractSemanticError(
            f"accepted_import_source: a successful {IMPORT_JOB_KIND} job reports the source it "
            "read, and that report is only checkable against the immutable descriptor "
            "import.start accepted; it is required here and may be None only for a terminal "
            "branch that is not an import success"
        )
    validate_import_completion_result(
        completion,
        job_id=identity.job_id,
        accepted_import_source=accepted_import_source,
        label=f"{label}.result",
    )


def validate_job_terminal_result(
    terminal: object,
    *,
    accepted_import_source: object,
    created_at: object = None,
    updated_at: object = None,
    label: str = "terminal_result",
) -> None:
    """Raise unless `terminal` is a coherent final outcome with the history that produced it.

    Each branch states its own state exactly -- success is `succeeded`, failure is `failed`,
    cancellation is `cancelled` -- so the branch and the state can never tell a caller two
    different things. A success or a failure needs at least one attempt, because neither
    outcome is reachable without executing; a cancellation may have none, and only a
    cancellation, because a job cancelled while still queued never started. Where attempts
    exist, the last one agrees with the branch, carries the job's `finished_at` -- the job
    finished when its final attempt did -- and, for a failure, carries the *same* error the
    terminal result reports: the failure that ended the last attempt is the failure that
    ended the job, and two spellings of it that could disagree would leave a caller unable to
    say which one is the outcome.

    `accepted_import_source` is the immutable descriptor `import.start` accepted. It is a
    required argument with no default: an import success cannot be fully validated without
    it, and a default would let the one check that grounds an import report in reality
    disappear whenever a caller forgot to pass it. `None` is a legitimate *value* here -- for
    a failure, a cancellation, or a success of some other job kind, there is no accepted
    import source to check against -- but it is a statement the caller makes, not an omission
    the validator infers.

    `created_at`/`updated_at`, when supplied, are the enclosing handle's lifetime, and every
    attempt instant in the history must fall inside them. :func:`validate_job_get_result`
    always supplies them, because that is where the handle and the history meet.
    """
    if not isinstance(terminal, (JobTerminalSuccess, JobTerminalFailure, JobTerminalCancellation)):
        raise ContractSemanticError(
            f"{label}: expected one JobTerminalResult branch, got {type(terminal).__name__}"
        )
    _validate_job_identity(terminal.identity, f"{label}.identity")
    state = _validate_open_code(terminal.state, f"{label}.state")
    finished_at = _parse_timestamp(terminal.finished_at, f"{label}.finished_at")

    if isinstance(terminal, JobTerminalSuccess):
        expected_state = JOB_STATE_SUCCEEDED
    elif isinstance(terminal, JobTerminalFailure):
        expected_state = JOB_STATE_FAILED
    else:
        expected_state = JOB_STATE_CANCELLED
    if state != expected_state:
        raise ContractSemanticError(
            f"{label}.state: a {type(terminal).__name__} states {expected_state!r} exactly, "
            f"got {state!r}"
        )

    validate_job_attempt_history(
        terminal.attempts,
        executing=False,
        created_at=created_at,
        updated_at=updated_at,
        label=f"{label}.attempts",
    )
    attempts = tuple(terminal.attempts)
    if not attempts:
        if expected_state != JOB_STATE_CANCELLED:
            raise ContractSemanticError(
                f"{label}: a {expected_state} job must have executed at least once"
            )
    else:
        last = attempts[-1]
        if last.state != expected_state:
            raise ContractSemanticError(
                f"{label}.attempts: the final attempt must be {expected_state!r} to match the "
                f"terminal branch, got {last.state!r}"
            )
        if _parse_timestamp(last.finished_at, f"{label}.attempts[-1].finished_at") != finished_at:
            raise ContractSemanticError(
                f"{label}.finished_at must equal the final attempt's finished_at; a job "
                "finished when its last attempt did"
            )

    if isinstance(terminal, JobTerminalFailure):
        _require_type(terminal.error, ApiError, f"{label}.error")
        assert isinstance(terminal.error, ApiError)
        _validate_open_code(terminal.error.code, f"{label}.error.code")
        _validate_message(terminal.error.message, f"{label}.error.message")
        _validate_open_code(terminal.error.retry_class, f"{label}.error.retry_class")
        if attempts and attempts[-1].error != terminal.error:
            raise ContractSemanticError(
                f"{label}.error is not the error the final attempt ended with; the failure "
                "that ended the last attempt is the failure that ended the job"
            )
    elif isinstance(terminal, JobTerminalCancellation):
        _require_type(terminal.cancellation, JobCancellationOutcome, f"{label}.cancellation")
        assert isinstance(terminal.cancellation, JobCancellationOutcome)
        _validate_open_code(terminal.cancellation.reason, f"{label}.cancellation.reason")
    else:
        _validate_import_terminal_success(terminal, accepted_import_source, label)


# --- import.start --------------------------------------------------------------


def validate_import_start_input(input_: object, label: str = "input") -> None:
    """Raise unless `input_` is a valid `ImportStartInput`.

    There is exactly one field to validate, and that is the point: `import.start` accepts a
    staged source handle and nothing a caller could steer an import with.
    """
    _require_type(input_, ImportStartInput, label)
    assert isinstance(input_, ImportStartInput)
    validate_import_source_descriptor(input_.source, f"{label}.source")


def decode_import_start_input(payload: object, path: str = "ImportStartInput") -> ImportStartInput:
    """Decode and fully validate `payload` into a semantically valid `ImportStartInput`."""
    input_ = ImportStartInput.from_wire(payload, path)
    validate_import_start_input(input_, path)
    return input_


def validate_import_start_result(
    result: object, *, response_job_reference: object, label: str = "result"
) -> None:
    """Raise unless `result` is a valid `ImportStartResult` naming the job the response does.

    `import.start` always starts a durable job, so `ResponseMetadata.job` is mandatory here
    rather than optional, and the two statements of which job was started must agree. A
    result handle and a response reference naming different jobs would leave the caller
    holding one id and tracking another.

    The started job is an `ingestion.import` job started by `import.start`, and it has not
    finished yet: a synchronous terminal state on the operation that starts asynchronous work
    would mean the job never existed.
    """
    _require_type(result, ImportStartResult, label)
    assert isinstance(result, ImportStartResult)
    validate_job_handle(result.job, f"{label}.job")
    assert isinstance(result.job, JobHandle)
    identity = result.job.identity
    if identity.job_kind != IMPORT_JOB_KIND:
        raise ContractSemanticError(
            f"{label}.job.identity.job_kind must be {IMPORT_JOB_KIND!r}, got {identity.job_kind!r}"
        )
    if identity.originating_operation != IMPORT_START_OPERATION:
        raise ContractSemanticError(
            f"{label}.job.identity.originating_operation must be {IMPORT_START_OPERATION!r}, "
            f"got {identity.originating_operation!r}"
        )
    state = result.job.state
    if state in JOB_TERMINAL_STATES:
        raise ContractSemanticError(
            f"{label}.job.state: import.start returns the job it started, which has not "
            f"reached {state!r} yet"
        )
    _require_type(response_job_reference, JobReference, "response_job_reference")
    assert isinstance(response_job_reference, JobReference)
    reference_id = _validate_opaque_token(
        response_job_reference.job_id, "response_job_reference.job_id"
    )
    if reference_id != identity.job_id:
        raise ContractSemanticError(
            f"{label}.job.identity.job_id {identity.job_id!r} does not equal the response "
            f"metadata job reference {reference_id!r}"
        )


def validate_synchronous_job_response_job_reference(
    operation: object, response_job_reference: object, label: str = "response_job_reference"
) -> None:
    """Raise unless a synchronous `job.*` response carries no `ResponseMetadata.job`.

    `job.get`, `job.cancel`, `job.retry`, and `job.events` complete synchronously; they
    observe or steer a job that already exists rather than starting one. A job reference on
    their response metadata would be a second, redundant statement about a job the result
    already names in full -- and one a caller could mistake for asynchronous work this call
    had begun.
    """
    name = _validate_operation_name(operation, "operation")
    if name == IMPORT_START_OPERATION:
        raise ContractSemanticError(
            f"operation: {IMPORT_START_OPERATION!r} always returns a job; use "
            "validate_import_start_result"
        )
    if response_job_reference is not None:
        raise ContractSemanticError(
            f"{label}: {name!r} completes synchronously and must not add a job reference to "
            "its response metadata"
        )


# --- job.get -------------------------------------------------------------------


def validate_job_get_input(input_: object, label: str = "input") -> None:
    """Raise unless `input_` is a valid `JobGetInput`."""
    _require_type(input_, JobGetInput, label)
    assert isinstance(input_, JobGetInput)
    _validate_opaque_token(input_.job_id, f"{label}.job_id")


def decode_job_get_input(payload: object, path: str = "JobGetInput") -> JobGetInput:
    """Decode and fully validate `payload` into a semantically valid `JobGetInput`."""
    input_ = JobGetInput.from_wire(payload, path)
    validate_job_get_input(input_, path)
    return input_


def validate_job_get_result(
    result: object,
    request: object = None,
    *,
    accepted_import_source: object,
    label: str = "result",
) -> None:
    """Raise unless `result` is one coherent read of one job.

    `terminal_result` is present exactly when the handle is in a *known* terminal state: a
    terminal job that withholds its outcome is unreadable, and a non-terminal one that
    supplies an outcome is claiming to have finished. An unknown state carries no terminal
    result, because this build cannot tell whether it ends a job -- which is the fail-safe
    direction, since the caller polls again rather than stopping on an outcome nobody stated.

    Where both are present, they are one description of one job rather than two that happen
    to travel together. Identity and state match exactly. The attempt history is bounded by
    the handle's own lifetime -- a job cannot have executed before it existed or after it was
    last observed -- and the handle's `latest_attempt` *is* the final attempt of that history:
    a handle showing attempt 2 beside a history ending at attempt 3, or beside a differently
    spelled attempt 3, states two irreconcilable accounts of the same execution. A history
    with no attempts (only a cancellation may have one) leaves the handle with no latest
    attempt either.

    `accepted_import_source` is required and is passed straight through to
    :func:`validate_job_terminal_result`; see there for why it has no default.
    """
    _require_type(result, JobGetResult, label)
    assert isinstance(result, JobGetResult)
    validate_job_handle(result.job, f"{label}.job")
    assert isinstance(result.job, JobHandle)
    if request is not None:
        validate_job_get_input(request, "request")
        assert isinstance(request, JobGetInput)
        if request.job_id != result.job.identity.job_id:
            raise ContractSemanticError(
                f"{label}.job.identity.job_id {result.job.identity.job_id!r} does not answer "
                f"the requested job {request.job_id!r}"
            )

    terminal = result.terminal_result
    state = result.job.state
    if terminal is None:
        if is_terminal_job_state(state):
            raise ContractSemanticError(
                f"{label}: a job in the known terminal state {state!r} must report its "
                "terminal_result"
            )
        return
    if not is_terminal_job_state(state):
        raise ContractSemanticError(
            f"{label}.terminal_result is present for a job in state {state!r}, which this "
            "build does not know to be terminal"
        )
    validate_job_terminal_result(
        terminal,
        accepted_import_source=accepted_import_source,
        created_at=result.job.created_at,
        updated_at=result.job.updated_at,
        label=f"{label}.terminal_result",
    )
    if terminal.identity != result.job.identity:
        raise ContractSemanticError(
            f"{label}.terminal_result identifies a different job than {label}.job"
        )
    if terminal.state != state:
        raise ContractSemanticError(
            f"{label}.terminal_result.state {terminal.state!r} disagrees with "
            f"{label}.job.state {state!r}"
        )
    attempts = tuple(terminal.attempts)
    final_attempt = attempts[-1] if attempts else None
    if result.job.latest_attempt != final_attempt:
        raise ContractSemanticError(
            f"{label}.job.latest_attempt is not the final attempt of "
            f"{label}.terminal_result.attempts; the handle and the history describe the same "
            "executions, so the latest attempt is the last one the history records (and is "
            "absent exactly when the history is empty)"
        )


# --- job.cancel and job.retry --------------------------------------------------


def validate_job_cancel_input(input_: object, label: str = "input") -> None:
    """Raise unless `input_` is a valid `JobCancelInput`."""
    _require_type(input_, JobCancelInput, label)
    assert isinstance(input_, JobCancelInput)
    _validate_opaque_token(input_.job_id, f"{label}.job_id")
    if input_.reason is not None:
        _validate_open_code(input_.reason, f"{label}.reason")


def decode_job_cancel_input(payload: object, path: str = "JobCancelInput") -> JobCancelInput:
    """Decode and fully validate `payload` into a semantically valid `JobCancelInput`."""
    input_ = JobCancelInput.from_wire(payload, path)
    validate_job_cancel_input(input_, path)
    return input_


def validate_job_retry_input(input_: object, label: str = "input") -> None:
    """Raise unless `input_` is a valid `JobRetryInput`."""
    _require_type(input_, JobRetryInput, label)
    assert isinstance(input_, JobRetryInput)
    _validate_opaque_token(input_.job_id, f"{label}.job_id")


def decode_job_retry_input(payload: object, path: str = "JobRetryInput") -> JobRetryInput:
    """Decode and fully validate `payload` into a semantically valid `JobRetryInput`."""
    input_ = JobRetryInput.from_wire(payload, path)
    validate_job_retry_input(input_, path)
    return input_


def _require_unchanged_handle(previous: JobHandle, current: JobHandle, label: str) -> None:
    if previous != current:
        raise ContractSemanticError(
            f"{label}: a control call that changed nothing returns the current handle "
            "unchanged"
        )


def _require_previous_handle(previous: object, current: JobHandle, label: str) -> JobHandle:
    """Return the trusted pre-mutation handle, or raise.

    A control result describes a *transition*, and a transition cannot be judged from its
    destination alone: `retry_scheduled` on a queued job is either a legitimate recovery of a
    failed job or a fabrication, and the returned handle is identical in both cases. So the
    handle observed before the call is a required argument on the full validators, not an
    optional enrichment that silently disables every contextual rule when a caller omits it.
    """
    if previous is None:
        raise ContractSemanticError(
            f"{label}: the handle observed before this call is required; a control result "
            "states what changed, and what changed cannot be read from the new handle alone"
        )
    validate_job_handle(previous, "previous")
    assert isinstance(previous, JobHandle)
    if previous.identity != current.identity:
        raise ContractSemanticError(f"{label}: a control call never changes a job's identity")
    return previous


def validate_job_cancel_result_shape(
    result: object, request: object = None, label: str = "result"
) -> str:
    """Raise unless `result` is a shape-valid `job.cancel` outcome, and return its disposition.

    *Structural checks only*, named so at the call site: the result is a `JobCancelResult`,
    its handle is internally coherent, its disposition is a well-formed open code, and it
    answers the job `request` asked about. Nothing here judges whether the disposition was
    *earned*, because nothing here has the handle from before the call.

    Use this when there is genuinely no pre-mutation handle to check against -- validating a
    recorded response, or the replay half of an idempotent pair. Use
    :func:`validate_job_cancel_result` everywhere else.
    """
    _require_type(result, JobCancelResult, label)
    assert isinstance(result, JobCancelResult)
    validate_job_handle(result.job, f"{label}.job")
    assert isinstance(result.job, JobHandle)
    disposition = _validate_open_code(
        result.cancellation_disposition, f"{label}.cancellation_disposition"
    )
    if request is not None:
        validate_job_cancel_input(request, "request")
        assert isinstance(request, JobCancelInput)
        if request.job_id != result.job.identity.job_id:
            raise ContractSemanticError(
                f"{label}.job.identity.job_id {result.job.identity.job_id!r} does not answer "
                f"the requested job {request.job_id!r}"
            )
    return disposition


def validate_job_cancel_result(
    result: object, request: object = None, *, previous: object, label: str = "result"
) -> str:
    """Raise unless `result` is a coherent `job.cancel` outcome for `previous`, and return its
    disposition.

    A refusal is a *successful* result, never a `conflict` error: that is what makes
    cancelling an already-terminal job safe to call without first racing to read its state.
    Three of the vocabulary's readings change nothing and each owes the same proof -- the
    handle comes back exactly as it went in. `not_cancellable` refused; `cancelled` found the
    job already cancelled; an unrecognized disposition is a control outcome this build cannot
    read and must never be recorded as a control that happened.

    `cancellation_requested` is the one reading that mutates, and it is checked against the
    job it claims to have acted on. The prior handle must actually have offered cancellation
    -- `cancellable`, and nothing else: a succeeded or failed job has nothing left to stop, a
    job whose cancellation is already `cancellation_pending` was asked once already, an
    unrecognized availability grants nothing, and none of them may be rewound into a fresh
    acceptance. The observation must be a legal step from the prior one. And the handle that
    comes back must have *stopped* offering cancellation: a job that reports itself still
    `cancellable` after accepting a cancellation has recorded nothing.

    `previous` is the handle observed before the call. It is required; see
    :func:`_require_previous_handle`.
    """
    disposition = validate_job_cancel_result_shape(result, request, label)
    assert isinstance(result, JobCancelResult)
    assert isinstance(result.job, JobHandle)
    previous_handle = _require_previous_handle(previous, result.job, label)

    if not is_accepted_cancellation_disposition(disposition):
        if disposition == JOB_CANCELLATION_DISPOSITION_CANCELLED and (
            result.job.state != JOB_STATE_CANCELLED
        ):
            raise ContractSemanticError(
                f"{label}: disposition {disposition!r} reports a job that was already "
                f"cancelled, got a handle in state {result.job.state!r}"
            )
        _require_unchanged_handle(previous_handle, result.job, label)
        return disposition

    if not permits_cancellation(previous_handle.control.cancellation):
        raise ContractSemanticError(
            f"{label}: disposition {disposition!r} was reported for a job whose handle offered "
            f"{previous_handle.control.cancellation!r}; only "
            f"{JOB_CANCELLATION_AVAILABILITY_CANCELLABLE!r} accepts a cancellation"
        )
    if result.job.state in {JOB_STATE_SUCCEEDED, JOB_STATE_FAILED}:
        raise ContractSemanticError(
            f"{label}: disposition {disposition!r} cannot be reported for a job that has "
            f"already finished as {result.job.state!r}"
        )
    validate_job_observation_progression(
        previous_handle, result.job, recovery_accepted=False, label=f"{label}.job"
    )
    if permits_cancellation(result.job.control.cancellation):
        raise ContractSemanticError(
            f"{label}: disposition {disposition!r} was accepted but the returned handle still "
            f"reports {JOB_CANCELLATION_AVAILABILITY_CANCELLABLE!r}; an accepted cancellation "
            "leaves a job pending or cancelled, never freshly cancellable again"
        )
    return disposition


def validate_job_retry_result_shape(
    result: object, request: object = None, label: str = "result"
) -> str:
    """Raise unless `result` is a shape-valid `job.retry` outcome, and return its disposition.

    *Structural checks only*, the recovery counterpart of
    :func:`validate_job_cancel_result_shape`, and used for the same two reasons.
    """
    _require_type(result, JobRetryResult, label)
    assert isinstance(result, JobRetryResult)
    validate_job_handle(result.job, f"{label}.job")
    assert isinstance(result.job, JobHandle)
    disposition = _validate_open_code(result.recovery_disposition, f"{label}.recovery_disposition")
    if request is not None:
        validate_job_retry_input(request, "request")
        assert isinstance(request, JobRetryInput)
        if request.job_id != result.job.identity.job_id:
            raise ContractSemanticError(
                f"{label}.job.identity.job_id {result.job.identity.job_id!r} does not answer "
                f"the requested job {request.job_id!r}"
            )
    return disposition


def validate_job_retry_result(
    result: object, request: object = None, *, previous: object, label: str = "result"
) -> str:
    """Raise unless `result` is a coherent `job.retry` outcome for `previous`, and return its
    disposition.

    A refusal is a *successful* result: `not_retryable`, and every disposition this build does
    not recognize, comes back with the current handle unchanged rather than as a `conflict`
    error.

    An accepted recovery is checked against exactly what it claims. `retry_scheduled` is what
    a `failed` job its handle called `retryable` yields; `resume_scheduled` is what a
    `cancelled` job its handle called `resumable` yields; neither pairing substitutes for the
    other, and no other prior state or availability yields either -- a queued job has nothing
    to recover from, and a handle that offered no recovery cannot have granted one. The
    recovered job is the *same* job, back in `queued`, and it starts no attempt: attempt N+1
    appears only when execution actually begins, so the previous finished attempt is still the
    latest one on the handle this call returns.

    `previous` is the handle observed before the call. It is required; see
    :func:`_require_previous_handle`.
    """
    disposition = validate_job_retry_result_shape(result, request, label)
    assert isinstance(result, JobRetryResult)
    assert isinstance(result.job, JobHandle)
    previous_handle = _require_previous_handle(previous, result.job, label)

    if not is_accepted_recovery_disposition(disposition):
        _require_unchanged_handle(previous_handle, result.job, label)
        return disposition

    required_state, required_availability = _RECOVERY_DISPOSITION_PRECONDITIONS[disposition]
    if previous_handle.state != required_state:
        raise ContractSemanticError(
            f"{label}.recovery_disposition: {disposition!r} recovers a "
            f"{required_state!r} job, and this one was {previous_handle.state!r}"
        )
    if previous_handle.control.recovery != required_availability:
        raise ContractSemanticError(
            f"{label}.recovery_disposition: {disposition!r} recovers a job whose handle "
            f"offered {required_availability!r}, and this one offered "
            f"{previous_handle.control.recovery!r}"
        )
    if result.job.state != JOB_STATE_QUEUED:
        raise ContractSemanticError(
            f"{label}: an accepted recovery returns the job to {JOB_STATE_QUEUED!r}, got "
            f"{result.job.state!r}"
        )
    validate_job_observation_progression(
        previous_handle, result.job, recovery_accepted=True, label=f"{label}.job"
    )
    if previous_handle.latest_attempt != result.job.latest_attempt:
        raise ContractSemanticError(
            f"{label}: an accepted recovery creates no attempt; the previous terminal attempt "
            "remains the latest one until execution starts"
        )
    return disposition


def validate_job_control_replay(first: object, replay: object, label: str = "replay") -> None:
    """Raise unless `replay` is the same control outcome as `first`, replayed.

    A replayed control call re-authorizes and answers from the recorded outcome; it performs
    no second mutation, starts no second job, drives no second control transition, and
    creates no attempt. What must therefore hold is the *semantic* outcome -- the same
    operation, the same job, the same disposition -- together with a handle that is an honest
    later reading of that same job.

    The handle itself legitimately differs. The job goes on evolving between the original
    call and the replay: a cancellation that was `cancellation_requested` may since have taken
    effect, and a job that a `retry_scheduled` left `queued` may since have started running
    and even finished. Requiring an identical handle would reject an honest replay for
    reporting the truth. So the replay handle is checked as an *observation progression* from
    the recorded one (:func:`validate_job_observation_progression`) rather than for equality:
    time, state, latest attempt, and progress may all advance, and none of them may go
    backwards or be rewritten.

    Crucially, the recorded call's own transition rules are *not* re-applied to the replay
    observation. Those rules judge a mutation against the handle it acted on, and the replay
    performed no mutation; re-running them would demand that a job which has since moved on
    still look like the instant the original call returned. That is why the two results are
    validated for shape here (:func:`validate_job_cancel_result_shape`,
    :func:`validate_job_retry_result_shape`) rather than through the full contextual
    validators.

    What a replay may never do: name a different job (identity is compared in full, not by
    `job_id` alone -- a job kind, originating operation, audit reference, or workspace that
    drifted under a stable id is a different job wearing the same name), report a different
    disposition, drive a second transition (an accepted recovery is `recovery_accepted=False`
    from here on: the one recovery already happened), or create an attempt. A job an accepted
    recovery left `queued` still shows the retained attempt for as long as it is still
    `queued`; a new attempt beside an unchanged state is execution the replay claims to have
    started.
    """
    if type(first) is not type(replay):
        raise ContractSemanticError(
            f"{label}: a replay answers the same operation, got "
            f"{type(first).__name__} and {type(replay).__name__}"
        )
    accepted_recovery = False
    if isinstance(first, JobCancelResult) and isinstance(replay, JobCancelResult):
        first_disposition: str = validate_job_cancel_result_shape(first, label="first")
        replay_disposition: str = validate_job_cancel_result_shape(replay, label=label)
    elif isinstance(first, JobRetryResult) and isinstance(replay, JobRetryResult):
        first_disposition = validate_job_retry_result_shape(first, label="first")
        replay_disposition = validate_job_retry_result_shape(replay, label=label)
        accepted_recovery = is_accepted_recovery_disposition(first_disposition)
    elif isinstance(first, ImportStartResult) and isinstance(replay, ImportStartResult):
        validate_job_handle(first.job, "first.job")
        validate_job_handle(replay.job, f"{label}.job")
        first_disposition = ""
        replay_disposition = ""
    else:
        raise ContractSemanticError(
            f"{label}: expected a JobCancelResult, JobRetryResult, or ImportStartResult pair, "
            f"got {type(first).__name__}"
        )
    recorded = first.job
    replayed = replay.job
    assert isinstance(recorded, JobHandle)
    assert isinstance(replayed, JobHandle)
    if recorded.identity != replayed.identity:
        raise ContractSemanticError(
            f"{label}: a replay names the same job; the recorded identity "
            f"{recorded.identity!r} became {replayed.identity!r}, which means a second job or "
            "a second mutation was performed"
        )
    if first_disposition != replay_disposition:
        raise ContractSemanticError(
            f"{label}: a replay preserves the semantic disposition; {first_disposition!r} "
            f"became {replay_disposition!r}"
        )
    validate_job_observation_progression(
        recorded, replayed, recovery_accepted=False, label=f"{label}.job"
    )
    if (
        accepted_recovery
        and replayed.state == JOB_STATE_QUEUED
        and replayed.latest_attempt != recorded.latest_attempt
    ):
        raise ContractSemanticError(
            f"{label}: the replayed job is still {JOB_STATE_QUEUED!r} but reports a different "
            "latest attempt; a replay schedules no second recovery and starts no execution"
        )


# --- job.events ----------------------------------------------------------------


class JobEventsPageBinding(NamedTuple):
    """What a `job.events` continuation token was found to be bound to.

    Trusted, non-wire input. Nothing in this contract decodes a token: format, encoding,
    signing, storage, and expiry are the adapter's, and this is the adapter's *statement*
    of what it found, handed to a validator that can then check the binding without ever
    parsing an opaque value.

    Every member is part of the binding because every member is something a stolen or
    stale token could otherwise carry across: another principal's session, another
    workspace, another operation's cursor, another job, a different ordering, a different
    snapshot, or a token format this build does not implement.
    """

    token_format_version: int
    principal_id: str
    workspace_id: str
    operation: str
    job_id: str
    ordering: str
    snapshot_event_count: int
    next_sequence: int


def _reject_page_binding() -> ContractSemanticError:
    """The one rejection every binding mismatch raises.

    Uniform by design: reporting *which* member disagreed would turn a rejected token into
    an oracle a caller could walk to discover what a valid token contains.
    """
    return ContractSemanticError(JOB_EVENTS_PAGE_BINDING_REJECTION_MESSAGE)


def validate_job_events_page_binding(
    binding: object, *, principal_id: object, workspace_id: object, job_id: object
) -> JobEventsPageBinding:
    """Raise unless `binding` is a well-formed binding for exactly this request.

    The token format version, the operation, and the ordering are fixed values rather than
    negotiated ones: a token bound to a different format, a different operation, or a
    different ordering is not a token this operation issued. The principal, workspace, and
    job must be exactly the ones now asking. The cursor must sit inside the snapshot the
    same token declares -- `next_sequence` in `[0, snapshot_event_count]`, where the upper
    bound is the exhausted position.

    Every failure, including a malformed binding, raises the one uniform diagnostic.
    """
    if not isinstance(binding, JobEventsPageBinding):
        raise _reject_page_binding()
    expected_principal = _validate_identifier(principal_id, "principal_id")
    expected_workspace = _validate_workspace_id(workspace_id, "workspace_id")
    expected_job = _validate_opaque_token(job_id, "job_id")
    try:
        version = _require_int(binding.token_format_version, "token_format_version")
        bound_principal = _require_str(binding.principal_id, "principal_id")
        bound_workspace = _require_str(binding.workspace_id, "workspace_id")
        bound_operation = _require_str(binding.operation, "operation")
        bound_job = _require_str(binding.job_id, "job_id")
        bound_ordering = _require_str(binding.ordering, "ordering")
        snapshot = _require_non_negative(binding.snapshot_event_count, "snapshot_event_count")
        cursor = _require_non_negative(binding.next_sequence, "next_sequence")
    except ContractSemanticError as error:
        raise _reject_page_binding() from error
    if (
        version != JOB_EVENTS_PAGE_BINDING_FORMAT_VERSION
        or bound_principal != expected_principal
        or bound_workspace != expected_workspace
        or bound_operation != JOB_EVENTS_OPERATION
        or bound_job != expected_job
        or bound_ordering != JOB_EVENTS_ORDERING_SEQUENCE_ASC
        or not 0 <= cursor <= snapshot
    ):
        raise _reject_page_binding()
    return binding


def validate_job_events_input(input_: object, label: str = "input") -> None:
    """Raise unless `input_` is a valid `JobEventsInput`.

    Present page metadata must actually name a continuation token. `{}` is not a spelling of
    "start at the beginning" -- that is what omitting `page` says -- but a request to
    continue from nowhere, and a validator could only honour it by guessing which of the two
    the caller meant.
    """
    _require_type(input_, JobEventsInput, label)
    assert isinstance(input_, JobEventsInput)
    _validate_opaque_token(input_.job_id, f"{label}.job_id")
    if input_.limit is not None:
        validate_page_limit(_require_int(input_.limit, f"{label}.limit"))
    if input_.page is not None:
        _validate_request_page(input_.page, f"{label}.page")


def _validate_request_page(page: object, label: str) -> str:
    _require_type(page, PageMetadata, label)
    assert isinstance(page, PageMetadata)
    if page.continuation_token is None:
        raise ContractSemanticError(
            f"{label} is present but names no continuation_token; an absent page is how "
            "'start at the first page' is stated"
        )
    return _validate_opaque_token(page.continuation_token, f"{label}.continuation_token")


def decode_job_events_input(payload: object, path: str = "JobEventsInput") -> JobEventsInput:
    """Decode and fully validate `payload` into a semantically valid `JobEventsInput`."""
    input_ = JobEventsInput.from_wire(payload, path)
    validate_job_events_input(input_, path)
    return input_


def _validate_job_event(event: object, label: str) -> tuple[int, datetime, str]:
    _require_type(event, JobEvent, label)
    assert isinstance(event, JobEvent)
    sequence = _require_non_negative(event.sequence, f"{label}.sequence")
    occurred = _parse_timestamp(event.occurred_at, f"{label}.occurred_at")
    state = _validate_open_code(event.state, f"{label}.state")
    if event.message is not None:
        _validate_message(event.message, f"{label}.message")
    return sequence, occurred, state


def validate_job_events_result(
    result: object,
    request: object,
    *,
    principal_id: object,
    workspace_id: object,
    request_binding: object = None,
    result_binding: object = None,
    label: str = "result",
) -> None:
    """Raise unless `result` is one page of one snapshot-stable `job.events` session.

    A session is defined by the snapshot captured when it began: its sequences are exactly
    `0 .. snapshot_event_count - 1`, and an event recorded after that capture never appears
    in it. That is the whole point of the snapshot -- a caller paging through a running job's
    history reads a fixed history, not one that grows underneath the cursor. A fresh
    tokenless request captures a new snapshot and may see more.

    Within the session, a page starts exactly where the previous one stopped and is
    contiguous: strictly increasing, duplicate-free, no gaps. A continuation token is offered
    exactly when more of the snapshot remains, so "is there another page" is answered by the
    same fact that decides where the next one starts, and the session always terminates.

    `request_binding` and `result_binding` are the trusted bindings for the request's and the
    result's tokens (`None` when there is no token). They are how the binding is proven
    without decoding an opaque value; a mismatch raises the one uniform token rejection.
    """
    _require_type(result, JobEventsResult, label)
    assert isinstance(result, JobEventsResult)
    validate_job_events_input(request, "request")
    assert isinstance(request, JobEventsInput)
    _validate_opaque_token(result.job_id, f"{label}.job_id")
    if result.job_id != request.job_id:
        raise ContractSemanticError(
            f"{label}.job_id {result.job_id!r} does not answer the requested job "
            f"{request.job_id!r}"
        )
    snapshot = _require_non_negative(result.snapshot_event_count, f"{label}.snapshot_event_count")

    if request.page is None:
        if request_binding is not None:
            raise ContractSemanticError(
                "request_binding was supplied for a request that carries no page"
            )
        cursor = 0
    else:
        if request_binding is None:
            raise _reject_page_binding()
        bound = validate_job_events_page_binding(
            request_binding,
            principal_id=principal_id,
            workspace_id=workspace_id,
            job_id=request.job_id,
        )
        if bound.snapshot_event_count != snapshot:
            raise _reject_page_binding()
        cursor = bound.next_sequence

    events = _require_sequence(result.events, f"{label}.events")
    ceiling = request.limit if request.limit is not None else JOB_EVENTS_MAX_PAGE_SIZE
    if len(events) > ceiling:
        raise ContractSemanticError(
            f"{label}.events: {len(events)} events exceeds the page ceiling of {ceiling}"
        )

    previous_instant: datetime | None = None
    previous_state: str | None = None
    for index, item in enumerate(events):
        item_label = f"{label}.events[{index}]"
        sequence, occurred, state = _validate_job_event(item, item_label)
        if sequence != cursor + index:
            raise ContractSemanticError(
                f"{item_label}.sequence: a page is contiguous from the position it continued "
                f"from, expected {cursor + index}, got {sequence}"
            )
        if sequence >= snapshot:
            raise ContractSemanticError(
                f"{item_label}.sequence {sequence} falls outside the captured snapshot "
                f"[0, {snapshot})"
            )
        if previous_instant is not None and occurred < previous_instant:
            raise ContractSemanticError(
                f"{item_label}.occurred_at moves backwards; a job's event timestamps are "
                "non-decreasing"
            )
        if previous_state is not None:
            validate_job_state_transition(
                previous_state, state, recovery_accepted=True, label=f"{item_label}.state"
            )
        previous_instant = occurred
        previous_state = state

    next_sequence = cursor + len(events)
    if next_sequence < snapshot and not events:
        raise ContractSemanticError(
            f"{label}.events is empty while {snapshot - cursor} events of the snapshot remain; "
            "a page that returns nothing can never exhaust the session"
        )

    _require_type(result.page, PageMetadata, f"{label}.page")
    assert isinstance(result.page, PageMetadata)
    if next_sequence < snapshot:
        if result.page.continuation_token is None:
            raise ContractSemanticError(
                f"{label}.page must carry a continuation_token while events "
                f"[{next_sequence}, {snapshot}) of the snapshot remain"
            )
        _validate_opaque_token(result.page.continuation_token, f"{label}.page.continuation_token")
        if result_binding is None:
            raise _reject_page_binding()
        offered = validate_job_events_page_binding(
            result_binding,
            principal_id=principal_id,
            workspace_id=workspace_id,
            job_id=result.job_id,
        )
        if offered.snapshot_event_count != snapshot or offered.next_sequence != next_sequence:
            raise _reject_page_binding()
        return
    if result.page.continuation_token is not None:
        raise ContractSemanticError(
            f"{label}.page offers a continuation while the captured snapshot is exhausted"
        )
    if result_binding is not None:
        raise ContractSemanticError(
            "result_binding was supplied for a result that offers no continuation"
        )


# --- idempotency ---------------------------------------------------------------


class IdempotencyScope(NamedTuple):
    """The scope an idempotency key is interpreted in.

    A key is never global. It is the caller's word inside one validated principal, one
    workspace, and one operation, so the same key used by a different principal, against a
    different workspace, or on a different operation is a different key -- otherwise a key
    guessed or leaked from one context would let a replay be answered from another's result.

    `principal_id` is the *validated* principal from `GrantedAuthority`, never a
    `PrincipalClaim`: an unvalidated claim scoping an idempotency key would let a caller pick
    whose replay it receives.
    """

    principal_id: str
    workspace_id: str | None
    operation: str
    idempotency_key: str


class IdempotencyEquivalence(NamedTuple):
    """A scoped idempotency key together with the fingerprint of what it was used for."""

    scope: IdempotencyScope
    fingerprint: str


def _normalized_scopes(scopes: object, label: str) -> list[str]:
    items = _require_sequence(scopes, label)
    if len(items) > _SCOPES_MAX_ITEMS:
        raise ContractSemanticError(
            f"{label}: {len(items)} scopes exceeds the maximum of {_SCOPES_MAX_ITEMS}"
        )
    normalized: set[str] = set()
    for index, item in enumerate(items):
        text = _require_str(item, f"{label}[{index}]")
        if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _SCOPE_RE.fullmatch(text):
            raise ContractSemanticError(f"{label}[{index}]: {text!r} is not a valid Scope")
        normalized.add(text)
    return sorted(normalized)


def _normalized_capabilities(requirements: object, label: str) -> list[list[object]]:
    """Normalize `requirements` into a canonical, order-independent fingerprint member.

    Every field of a `CapabilityRequirement` participates, `required` included. The flag is
    not decoration: it is the difference between "I need this capability" and "use it if you
    have it", so two requests that agree on an id and a minimum version but disagree on
    whether it was mandatory asked the server for different things. Folding only the id and
    the version in would make those two requests fingerprint identically, and the second
    would be answered from the first's recorded outcome -- an outcome produced under a
    capability floor the second caller never stated.
    """
    items = _require_sequence(requirements, label)
    if len(items) > _REQUIRED_CAPABILITIES_MAX_ITEMS:
        raise ContractSemanticError(
            f"{label}: {len(items)} requirements exceeds the maximum of "
            f"{_REQUIRED_CAPABILITIES_MAX_ITEMS}"
        )
    normalized: set[tuple[str, str, bool]] = set()
    for index, item in enumerate(items):
        _require_type(item, CapabilityRequirement, f"{label}[{index}]")
        assert isinstance(item, CapabilityRequirement)
        normalized.add(
            (
                _require_str(item.id, f"{label}[{index}].id"),
                _require_str(item.minimum_version, f"{label}[{index}].minimum_version"),
                _require_bool(item.required, f"{label}[{index}].required"),
            )
        )
    return [
        [identifier, version, required] for identifier, version, required in sorted(normalized)
    ]


def idempotency_equivalence(
    operation: object,
    metadata: object,
    operation_input: object,
    *,
    principal_id: object,
    workspace_id: object = None,
) -> IdempotencyEquivalence:
    """Build the scoped key and equivalence fingerprint for one idempotent request.

    Two requests are the same request when their scopes are equal and their fingerprints
    are equal. The fingerprint covers exactly
    :data:`IDEMPOTENCY_EQUIVALENCE_INCLUDED_INPUTS` and deliberately none of
    :data:`IDEMPOTENCY_EQUIVALENCE_EXCLUDED_INPUTS`: a genuine retry changes its request id,
    its trace, its deadline, and possibly its client build, and folding any of those in would
    make every honest retry a conflict.

    Scopes and capability requirements are compared as sets, sorted before hashing: they are
    an unordered statement of what the caller is exercising, so two spellings of the same set
    must fingerprint identically. `operation_input` is the *wire* payload -- the caller passes
    `input_.to_wire()` -- so equivalence is decided over the normalized document rather than
    over Python object identity.
    """
    name = _validate_operation_name(operation, "operation")
    _require_type(metadata, RequestMetadata, "metadata")
    assert isinstance(metadata, RequestMetadata)
    if metadata.idempotency_key is None:
        raise ContractSemanticError(
            "metadata.idempotency_key: an equivalence key cannot be built for a request that "
            "carries no idempotency key"
        )
    key = _validate_identifier(metadata.idempotency_key, "metadata.idempotency_key")
    validated_principal = _validate_identifier(principal_id, "principal_id")
    scoped_workspace = (
        _validate_workspace_id(workspace_id, "workspace_id") if workspace_id is not None else None
    )
    purpose = _require_str(metadata.purpose, "metadata.purpose")
    if len(purpose) > _BOUNDED_VALUE_MAX_LENGTH or not _PURPOSE_RE.fullmatch(purpose):
        raise ContractSemanticError(f"metadata.purpose: {purpose!r} is not a valid Purpose")

    document = {
        "operation": name,
        "operation_input": operation_input,
        "purpose": purpose,
        "scopes": _normalized_scopes(metadata.scopes, "metadata.scopes"),
        "required_capabilities": _normalized_capabilities(
            metadata.required_capabilities, "metadata.required_capabilities"
        ),
    }
    try:
        payload = canonical_bytes(document, "$")
    except ValueError as error:
        raise ContractSemanticError(
            f"operation_input: cannot be canonicalized for idempotency equivalence: {error}"
        ) from error
    fingerprint = f"{CONTENT_CHECKSUM_ALGORITHM}:{sha256(payload).hexdigest()}"
    return IdempotencyEquivalence(
        scope=IdempotencyScope(
            principal_id=validated_principal,
            workspace_id=scoped_workspace,
            operation=name,
            idempotency_key=key,
        ),
        fingerprint=fingerprint,
    )


def classify_idempotent_replay(first: object, second: object) -> str:
    """Classify `second` against the recorded `first` as replay, conflict, or distinct.

    Three outcomes, and only one of them is an error. Equal scope and equal fingerprint is a
    :data:`IDEMPOTENCY_REPLAY`: the same caller asking the same thing again, to be answered
    from the recorded outcome without performing a second mutation. Equal scope and a
    different fingerprint is :data:`IDEMPOTENCY_CONFLICT`: one key was used for two different
    requests, which is the exact situation the key exists to catch. Different scopes are
    :data:`IDEMPOTENCY_DISTINCT` -- two unrelated requests that happen to share a key string.
    """
    _require_type(first, IdempotencyEquivalence, "first")
    _require_type(second, IdempotencyEquivalence, "second")
    assert isinstance(first, IdempotencyEquivalence)
    assert isinstance(second, IdempotencyEquivalence)
    if first.scope != second.scope:
        return IDEMPOTENCY_DISTINCT
    return IDEMPOTENCY_REPLAY if first.fingerprint == second.fingerprint else IDEMPOTENCY_CONFLICT


def validate_operation_idempotency_metadata(
    idempotency: object, label: str = "idempotency"
) -> None:
    """Raise unless an `OperationIdempotencyMetadata` states something an implementation can
    satisfy.

    The three fields are not independent. Requiring a key an operation does not honour is a
    demand nothing can meet; declaring a request safe to retry without a key while also
    rejecting requests that omit one says both things at once. Each combination below is
    rejected because no server could implement it, not because of a style preference.
    """
    _require_type(idempotency, OperationIdempotencyMetadata, label)
    assert isinstance(idempotency, OperationIdempotencyMetadata)
    supports = _require_bool(idempotency.supports_idempotency_key, f"{label}.supports_idempotency_key")
    required = _require_bool(idempotency.required, f"{label}.required")
    safe = _require_bool(idempotency.safe_to_retry, f"{label}.safe_to_retry")
    if required and not supports:
        raise ContractSemanticError(
            f"{label}: an operation cannot require an idempotency key it does not support"
        )
    if safe and required:
        raise ContractSemanticError(
            f"{label}: a request that is safe to retry without a key cannot also be rejected "
            "for omitting one"
        )


def validate_request_idempotency(
    idempotency: object, precondition: object, metadata: object, label: str = "metadata"
) -> None:
    """Raise unless `metadata` satisfies what the operation declares about keys and
    preconditions.

    All four failures are the same kind of failure: a non-retryable invalid request. A
    missing required key, a key sent to an operation that honours none, a precondition sent
    to an operation that applies none, and a missing required precondition are all requests
    the server cannot execute as stated -- and retrying the identical request cannot change
    that.
    """
    validate_operation_idempotency_metadata(idempotency, "idempotency")
    assert isinstance(idempotency, OperationIdempotencyMetadata)
    _require_type(precondition, OperationPreconditionMetadata, "precondition")
    assert isinstance(precondition, OperationPreconditionMetadata)
    _require_type(metadata, RequestMetadata, label)
    assert isinstance(metadata, RequestMetadata)
    supports_precondition = _require_bool(
        precondition.supports_mutation_precondition,
        "precondition.supports_mutation_precondition",
    )
    precondition_required = _require_bool(precondition.required, "precondition.required")

    if idempotency.required and metadata.idempotency_key is None:
        raise ContractSemanticError(
            f"{label}.idempotency_key is required by this operation and is absent"
        )
    if metadata.idempotency_key is not None:
        if not idempotency.supports_idempotency_key:
            raise ContractSemanticError(
                f"{label}.idempotency_key was sent to an operation that honours none"
            )
        _validate_identifier(metadata.idempotency_key, f"{label}.idempotency_key")
    if metadata.mutation_precondition is not None and not supports_precondition:
        raise ContractSemanticError(
            f"{label}.mutation_precondition was sent to an operation that applies none"
        )
    if precondition_required and metadata.mutation_precondition is None:
        raise ContractSemanticError(
            f"{label}.mutation_precondition is required by this operation and is absent"
        )


# --- frozen operation metadata posture (for the later catalogue) ---------------


class JobOperationPosture(NamedTuple):
    """The declared contract characteristics of one job-lifecycle operation.

    Exactly the parts of `OperationMetadata` that A2.4 froze, and none of the parts the
    operation catalogue still owns: the payload schema references, the capability's minimum
    version, and the allowed error list are all catalogue decisions. Publishing the frozen
    half here means the catalogue *binds* these characteristics rather than restating them,
    so the two can never disagree.
    """

    required_capability_id: str
    scope: OperationScope
    job: OperationJobMetadata
    pagination: OperationPaginationMetadata
    idempotency: OperationIdempotencyMetadata
    precondition: OperationPreconditionMetadata
    audit: OperationAuditMetadata


def _posture(
    *,
    capability: str,
    scopes: tuple[str, ...],
    side_effect: str,
    completion_mode: str,
    job_kind: str | None = None,
    terminal_result_schema_ref: str | None = None,
    paginated: bool = False,
    max_page_size: int | None = None,
    supports_key: bool,
    key_required: bool,
    safe_to_retry: bool,
    audit_category: str,
) -> JobOperationPosture:
    return JobOperationPosture(
        required_capability_id=capability,
        scope=OperationScope(
            required_scopes=scopes, side_effect=side_effect, scope_kind="workspace"
        ),
        job=OperationJobMetadata(
            completion_mode=completion_mode,
            job_kind=job_kind,
            terminal_result_schema_ref=terminal_result_schema_ref,
        ),
        pagination=OperationPaginationMetadata(paginated=paginated, max_page_size=max_page_size),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=supports_key,
            required=key_required,
            safe_to_retry=safe_to_retry,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False, required=False
        ),
        audit=OperationAuditMetadata(audited=True, audit_category=audit_category),
    )


JOB_LIFECYCLE_OPERATION_POSTURES: Final[Mapping[str, JobOperationPosture]] = MappingProxyType(
    {
        IMPORT_START_OPERATION: _posture(
            capability="ingestion.import",
            scopes=("memory:write",),
            side_effect="create",
            completion_mode="always_returns_job",
            job_kind=IMPORT_JOB_KIND,
            terminal_result_schema_ref=IMPORT_COMPLETION_RESULT_SCHEMA_REF,
            supports_key=True,
            key_required=True,
            safe_to_retry=False,
            audit_category="mutation",
        ),
        JOB_GET_OPERATION: _posture(
            capability="job.read",
            scopes=("job:read",),
            side_effect="none",
            completion_mode="synchronous",
            supports_key=False,
            key_required=False,
            safe_to_retry=True,
            audit_category="read",
        ),
        JOB_CANCEL_OPERATION: _posture(
            capability="job.control",
            scopes=("job:control",),
            side_effect="update",
            completion_mode="synchronous",
            supports_key=True,
            key_required=True,
            safe_to_retry=False,
            audit_category="mutation",
        ),
        JOB_RETRY_OPERATION: _posture(
            capability="job.control",
            scopes=("job:control",),
            side_effect="update",
            completion_mode="synchronous",
            supports_key=True,
            key_required=True,
            safe_to_retry=False,
            audit_category="mutation",
        ),
        JOB_EVENTS_OPERATION: _posture(
            capability="job.read",
            scopes=("job:read",),
            side_effect="none",
            completion_mode="synchronous",
            paginated=True,
            max_page_size=JOB_EVENTS_MAX_PAGE_SIZE,
            supports_key=False,
            key_required=False,
            safe_to_retry=True,
            audit_category="read",
        ),
    }
)
"""The frozen A2.4 metadata posture for the five job-lifecycle operations.

Every mutation here requires an idempotency key and applies no precondition: durable work
and control transitions must never be duplicated by a network-level repeat, and neither
`job.cancel` nor `job.retry` is an optimistic-concurrency update of a versioned record --
their safety comes from being idempotent and from refusing by disposition, not from an
`If-Match`. Both reads are safe to retry without a key and are still audited: reading a
job's state and its event history is an access worth recording."""
