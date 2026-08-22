"""Pure semantic validation for the canonical Agent Runtime records (RT-101).

Structural decoding lives in :mod:`generated`; this module is the same kind of pure,
standard-library-only layer :mod:`semantics_jobs` is for the durable-job DTOs, split out
here because a canonical `Run` is its own domain: it has a policy it is pinned to, a budget
it is admitted under, capability grants it may act through, steps and attempts, durable
waits and approvals, effects that are intended before they happen and settled afterwards,
and evidence and cleanup that outlive the work itself.

The rules this module encodes are the ones a JSON Schema cannot state.

*Workspace-scoped and source-qualified.* Every record in a run restates the run's
`workspace_id` and `run_id`, and every one must agree; a correlation into another domain is
an `ExternalReference` that names which domain its identifier belongs to, because an
agent-lane ledger `run_id` and a canonical `Run.run_id` are different identifiers that must
never be joined.

*History is immutable and contiguous.* Step ordinals run `1..N`, attempt numbers run `1..N`
within their step, and event sequences run `0..N`; instants never regress; a succeeded
attempt is final. A correction is a further record, never an edit to a recorded one.

*No effect before intent.* A receipt or a settlement that names no declared intent is an
effect nobody authorized and nobody can reconcile, so it is refused outright -- and a
`committed` settlement must name the receipt that proves it.

*Uncertainty is not failure.* An `unknown` settlement says reconciliation is owed. A run
holding one may not close: it is `uncertain`, not `failed`, because reporting it as failed
would licence a retry that duplicates a committed effect.

*Idempotency is logical and stable.* Two admissions carrying the same `logical_key` are one
run replayed; the same key over a different definition is a conflict. The same rule applies
one level down to effects, keyed by `idempotency_key` over `request_digest`. The three
classifications are the ones the application wire already uses
(:mod:`semantics_jobs`), not a second vocabulary for the same idea.

*Policy is monotonic, and discovery is not authority.* A run's policy revisions move
forward and its grants may narrow but never widen, so authority a run once lost cannot be
silently re-granted mid-flight. A capability that appears only in `discovered_capabilities`
authorizes nothing: an effect naming one is refused.

*Cancellation preserves evidence, and cleanup is observable.* Cancelling a run stops the
work, never the record: a cancelled run's evidence stays retained, and a run that actually
executed keeps something to show for it. A run that reached a terminal status states what
it released, including cleanup that failed.

*External logs are subordinate evidence.* Only evidence the runtime itself recorded may be
`authoritative`. An external log, an agent-lane ledger and a control-plane projection
corroborate the runtime's own record; they never replace it.

*Unknown open values fail safe.* `RunStatus` and its sibling vocabularies are closed at the
schema and open on the wire: an unrecognized value decodes and is preserved verbatim, but no
decision is ever taken from it. It is never terminal, never successful, never a licence to
start a new effect, and never a run a caller may treat as finished --
:func:`validate_terminal_run` refuses it rather than guessing.

`ResolveWait` is a Runtime command and not job recovery. It resolves exactly one durable
`Wait`, it is not `job.retry`, there is no `job.resume`, it is not a `JobControl` member,
and it names no job at all. An `approval_decision` it carries is checked against the
`Approval` actually recorded for that wait, so the approval id it quotes is a decision
somebody made rather than one it asserts about itself.

Every public function in this module is a *direct* entry point: a caller may reach it
without going through a tolerant `from_wire` decoder first. None of them may therefore ever
raise a raw `TypeError`/`AttributeError` from an unguarded `len()`, regex match, iteration,
or attribute access on a wrongly-typed argument: every such misuse is caught by an explicit
type check up front and reraised as `ContractSemanticError`.

Trusted context is an argument, not an option. Several rules cannot be decided from one DTO
-- whether a receipt has an intent, whether a grant is still backed by policy, whether a
`ResolveWait` resolves the wait it claims to. Each takes that context as a required keyword
with no default, following :mod:`semantics_jobs`, because a rule that silently disappears
when a caller forgets an argument is not a rule.

Nothing here depends on runtime, storage, HTTP, MCP, CLI, Platform, Dev, or a validation
framework. Schedulers, workers, leases, transports and durable storage are all out of scope,
and this module writes no SQL and knows no table.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Final

from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    AUDIT_REFERENCE_PATTERN,
    CAPABILITY_ID_PATTERN,
    CONTENT_CHECKSUM_PATTERN,
    IDEMPOTENCY_KEY_PATTERN,
    IDENTIFIER_PATTERN,
    MEDIA_TYPE_PATTERN,
    OPAQUE_TOKEN_PATTERN,
    OPEN_CODE_PATTERN,
    OPERATION_NAME_PATTERN,
    PURPOSE_PATTERN,
    RELEASE_VERSION_PATTERN,
    SCOPE_PATTERN,
    TIMESTAMP_PATTERN,
    WORKSPACE_ID_PATTERN,
    ApiError,
    Approval,
    Artifact,
    Attempt,
    BudgetSnapshot,
    CapabilityGrant,
    CleanupReceipt,
    EffectIntent,
    EffectReceipt,
    EffectSettlement,
    EvidenceItem,
    ExternalReference,
    PolicySnapshot,
    ResolveWait,
    Run,
    RunDefinitionRef,
    RunStep,
    RuntimeEvent,
    Wait,
)
from omnivia_core.contracts.v1.semantics_jobs import (
    IDEMPOTENCY_CONFLICT,
    IDEMPOTENCY_DISTINCT,
    IDEMPOTENCY_REPLAY,
)

__all__ = [
    "APPROVAL_DECISIONS",
    "APPROVAL_DECISION_APPROVED",
    "APPROVAL_DECISION_REJECTED",
    "ATTEMPT_CONTINUABLE_STATUSES",
    "ATTEMPT_STATUSES",
    "ATTEMPT_STATUS_CANCELLED",
    "ATTEMPT_STATUS_FAILED",
    "ATTEMPT_STATUS_RUNNING",
    "ATTEMPT_STATUS_SUCCEEDED",
    "ATTEMPT_STATUS_UNCERTAIN",
    "ATTEMPT_TERMINAL_STATUSES",
    "CLEANUP_OUTCOMES",
    "EFFECT_OUTCOMES",
    "EFFECT_OUTCOME_COMMITTED",
    "EFFECT_OUTCOME_NOT_COMMITTED",
    "EFFECT_OUTCOME_UNKNOWN",
    "RUNTIME_AUTHORITATIVE_SOURCE_KIND",
    "RUNTIME_SOURCE_KINDS",
    "RUN_DEFINITION_KINDS",
    "RUN_STATUSES",
    "RUN_STATUS_ADMITTED",
    "RUN_STATUS_CANCELLED",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_PARTIALLY_COMPLETED",
    "RUN_STATUS_RUNNING",
    "RUN_STATUS_SUCCEEDED",
    "RUN_STATUS_TRANSITIONS",
    "RUN_STATUS_UNCERTAIN",
    "RUN_STATUS_WAITING",
    "RUN_STEP_STATUSES",
    "RUN_STEP_TERMINAL_STATUSES",
    "RUN_TERMINAL_STATUSES",
    "WAIT_KINDS",
    "WAIT_RESOLUTIONS",
    "WAIT_RESOLUTION_CANCELLED",
    "WAIT_RESOLUTION_FOR_KIND",
    "WAIT_STATUSES",
    "WAIT_STATUS_PENDING",
    "classify_effect_replay",
    "classify_run_replay",
    "decode_resolve_wait",
    "is_authoritative_source",
    "is_known_run_status",
    "is_successful_run_status",
    "is_terminal_run_status",
    "is_waiting_run_status",
    "permits_new_effect",
    "validate_approval",
    "validate_artifact",
    "validate_attempt",
    "validate_attempt_history",
    "validate_budget_snapshot",
    "validate_budget_snapshot_progression",
    "validate_capability_grant",
    "validate_cleanup_receipt",
    "validate_effect_intent",
    "validate_effect_receipt",
    "validate_effect_settlement",
    "validate_evidence_item",
    "validate_external_reference",
    "validate_policy_snapshot",
    "validate_policy_snapshot_progression",
    "validate_resolve_wait",
    "validate_resolve_wait_shape",
    "validate_run",
    "validate_run_status_transition",
    "validate_run_step",
    "validate_runtime_event_stream",
    "validate_terminal_run",
    "validate_wait",
]

# --- bounds restated from the schema ------------------------------------------
#
# Restated rather than delegated to, for the reason every length bound in the sibling
# semantic modules already is: the tolerant generated decoder applies no `maxLength`,
# `minimum` or `maxItems`, so a payload carrying an overlong identifier or a 5000-event
# stream would otherwise decode, pass semantic validation, and fail only against strict
# JSON Schema.

_BOUNDED_VALUE_MAX_LENGTH: Final = 128
_OPAQUE_TOKEN_MAX_LENGTH: Final = 512
_MEDIA_TYPE_MAX_LENGTH: Final = 255
_RELEASE_VERSION_MAX_LENGTH: Final = 128
_CONTENT_CHECKSUM_LENGTH: Final = 71
_MESSAGE_MAX_LENGTH: Final = 2048

_MAX_CAPABILITIES: Final = 256
_MAX_SCOPES: Final = 64
_MAX_GRANTS: Final = 64
_MAX_STEPS: Final = 256
_MAX_ATTEMPTS: Final = 256
_MAX_WAITS: Final = 64
_MAX_APPROVALS: Final = 64
_MAX_EFFECTS: Final = 256
_MAX_EVENTS: Final = 1000
_MAX_ARTIFACTS: Final = 256
_MAX_EVIDENCE: Final = 256
_MAX_CLEANUP_RECEIPTS: Final = 64
_MAX_CORRELATIONS: Final = 16

_AUDIT_REFERENCE_RE: Final = re.compile(AUDIT_REFERENCE_PATTERN)
_CAPABILITY_ID_RE: Final = re.compile(CAPABILITY_ID_PATTERN)
_CONTENT_CHECKSUM_RE: Final = re.compile(CONTENT_CHECKSUM_PATTERN)
_IDEMPOTENCY_KEY_RE: Final = re.compile(IDEMPOTENCY_KEY_PATTERN)
_IDENTIFIER_RE: Final = re.compile(IDENTIFIER_PATTERN)
_MEDIA_TYPE_RE: Final = re.compile(MEDIA_TYPE_PATTERN)
_OPAQUE_TOKEN_RE: Final = re.compile(OPAQUE_TOKEN_PATTERN)
_OPEN_CODE_RE: Final = re.compile(OPEN_CODE_PATTERN)
_OPERATION_NAME_RE: Final = re.compile(OPERATION_NAME_PATTERN)
_PURPOSE_RE: Final = re.compile(PURPOSE_PATTERN)
_RELEASE_VERSION_RE: Final = re.compile(RELEASE_VERSION_PATTERN)
_SCOPE_RE: Final = re.compile(SCOPE_PATTERN)
_TIMESTAMP_RE: Final = re.compile(TIMESTAMP_PATTERN)
_WORKSPACE_ID_RE: Final = re.compile(WORKSPACE_ID_PATTERN)

# --- run status ---------------------------------------------------------------

RUN_STATUS_ADMITTED: Final = "admitted"
RUN_STATUS_RUNNING: Final = "running"
RUN_STATUS_WAITING: Final = "waiting"
RUN_STATUS_SUCCEEDED: Final = "succeeded"
RUN_STATUS_PARTIALLY_COMPLETED: Final = "partially_completed"
RUN_STATUS_FAILED: Final = "failed"
RUN_STATUS_CANCELLED: Final = "cancelled"
RUN_STATUS_UNCERTAIN: Final = "uncertain"

RUN_STATUSES: Final[tuple[str, ...]] = (
    RUN_STATUS_ADMITTED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING,
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_PARTIALLY_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_UNCERTAIN,
)
"""The canonical, Core-owned `RunStatus` vocabulary this build knows.

Deliberately neither `JobState` -- which has no way to say a run is waiting -- nor the
control plane's run status, which is not durable. Open on the wire: a value outside this
tuple decodes and is preserved, and nothing is inferred from it."""

RUN_TERMINAL_STATUSES: Final[tuple[str, ...]] = (
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_PARTIALLY_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_CANCELLED,
)
"""The four statuses a run is finished in.

`uncertain` is deliberately absent. An unreconciled effect is an open question, not an
outcome, and calling it terminal would let a run close over work nobody can account for."""

RUN_STATUS_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        RUN_STATUS_ADMITTED: frozenset({RUN_STATUS_RUNNING, RUN_STATUS_CANCELLED}),
        RUN_STATUS_RUNNING: frozenset(
            {
                RUN_STATUS_WAITING,
                RUN_STATUS_SUCCEEDED,
                RUN_STATUS_PARTIALLY_COMPLETED,
                RUN_STATUS_FAILED,
                RUN_STATUS_CANCELLED,
                RUN_STATUS_UNCERTAIN,
            }
        ),
        RUN_STATUS_WAITING: frozenset(
            {RUN_STATUS_RUNNING, RUN_STATUS_CANCELLED, RUN_STATUS_FAILED}
        ),
        RUN_STATUS_UNCERTAIN: frozenset(
            {
                RUN_STATUS_RUNNING,
                RUN_STATUS_SUCCEEDED,
                RUN_STATUS_PARTIALLY_COMPLETED,
                RUN_STATUS_FAILED,
                RUN_STATUS_CANCELLED,
            }
        ),
        RUN_STATUS_SUCCEEDED: frozenset(),
        RUN_STATUS_PARTIALLY_COMPLETED: frozenset(),
        RUN_STATUS_FAILED: frozenset(),
        RUN_STATUS_CANCELLED: frozenset(),
    }
)
"""Which run status may follow which, in one step.

Every terminal status is a sink: a canonical run is never reopened, and recovering work is a
new run rather than a resurrection of a closed one. `uncertain` is the exception that proves
it -- it is the only non-terminal status a run can sit in after execution stopped, precisely
so reconciliation has somewhere to happen before the run closes."""

# --- step, attempt, wait, approval, effect and cleanup vocabularies -------------

RUN_STEP_STATUSES: Final[tuple[str, ...]] = (
    "pending",
    "running",
    "waiting",
    "succeeded",
    "failed",
    "cancelled",
    "skipped",
)
RUN_STEP_TERMINAL_STATUSES: Final[tuple[str, ...]] = (
    "succeeded",
    "failed",
    "cancelled",
    "skipped",
)
_RUN_STEP_STATUS_WAITING: Final = "waiting"
_RUN_STEP_STATUS_RUNNING: Final = "running"

ATTEMPT_STATUS_RUNNING: Final = "running"
ATTEMPT_STATUS_SUCCEEDED: Final = "succeeded"
ATTEMPT_STATUS_FAILED: Final = "failed"
ATTEMPT_STATUS_CANCELLED: Final = "cancelled"
ATTEMPT_STATUS_UNCERTAIN: Final = "uncertain"

ATTEMPT_STATUSES: Final[tuple[str, ...]] = (
    ATTEMPT_STATUS_RUNNING,
    ATTEMPT_STATUS_SUCCEEDED,
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_CANCELLED,
    ATTEMPT_STATUS_UNCERTAIN,
)
ATTEMPT_TERMINAL_STATUSES: Final[tuple[str, ...]] = (
    ATTEMPT_STATUS_SUCCEEDED,
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_CANCELLED,
    ATTEMPT_STATUS_UNCERTAIN,
)
ATTEMPT_CONTINUABLE_STATUSES: Final[tuple[str, ...]] = (
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_CANCELLED,
    ATTEMPT_STATUS_UNCERTAIN,
)
"""The attempt outcomes another attempt may follow.

`succeeded` is absent because a succeeded attempt is final, and `running` is absent because
two attempts of one step never overlap."""

WAIT_KINDS: Final[tuple[str, ...]] = ("approval", "external_signal", "timer")
WAIT_STATUS_PENDING: Final = "pending"
WAIT_STATUSES: Final[tuple[str, ...]] = ("pending", "resolved", "expired", "cancelled")

WAIT_RESOLUTION_CANCELLED: Final = "cancelled"
_WAIT_RESOLUTION_APPROVAL_DECISION: Final = "approval_decision"
WAIT_RESOLUTIONS: Final[tuple[str, ...]] = (
    _WAIT_RESOLUTION_APPROVAL_DECISION,
    "external_signal",
    "timer_expiry",
    WAIT_RESOLUTION_CANCELLED,
)

WAIT_RESOLUTION_FOR_KIND: Final[Mapping[str, str]] = MappingProxyType(
    {
        "approval": _WAIT_RESOLUTION_APPROVAL_DECISION,
        "external_signal": "external_signal",
        "timer": "timer_expiry",
    }
)
"""The one resolution each wait kind admits, beside `cancelled`.

A signal never resolves an approval and an approval decision never resolves a timer: the
kind fixes what the wait is actually waiting for, and accepting a mismatched resolution
would resume a run on a condition that never occurred. `cancelled` is the universal release,
because cancelling a run releases whatever it was waiting on."""

APPROVAL_DECISION_APPROVED: Final = "approved"
APPROVAL_DECISION_REJECTED: Final = "rejected"
APPROVAL_DECISIONS: Final[tuple[str, ...]] = (
    APPROVAL_DECISION_APPROVED,
    APPROVAL_DECISION_REJECTED,
)

EFFECT_OUTCOME_COMMITTED: Final = "committed"
EFFECT_OUTCOME_NOT_COMMITTED: Final = "not_committed"
EFFECT_OUTCOME_UNKNOWN: Final = "unknown"
EFFECT_OUTCOMES: Final[tuple[str, ...]] = (
    EFFECT_OUTCOME_COMMITTED,
    EFFECT_OUTCOME_NOT_COMMITTED,
    EFFECT_OUTCOME_UNKNOWN,
)

CLEANUP_OUTCOMES: Final[tuple[str, ...]] = ("released", "not_required", "failed")

RUN_DEFINITION_KINDS: Final[tuple[str, ...]] = ("agent_component", "workflow")

RUNTIME_AUTHORITATIVE_SOURCE_KIND: Final = "runtime"
RUNTIME_SOURCE_KINDS: Final[tuple[str, ...]] = (
    RUNTIME_AUTHORITATIVE_SOURCE_KIND,
    "application_job",
    "control_plane_projection",
    "agent_lane_ledger",
    "external_log",
)
"""The domains a correlation may name.

Exactly one of them -- `runtime` -- is authoritative. The others exist so a foreign
identifier can be recorded without being mistaken for a canonical one: an agent-lane ledger
`run_id` and a canonical `Run.run_id` are different identifiers in different domains, and
the only thing stopping them being joined is that every reference says which it is."""


# --- direct-entry type guards -------------------------------------------------


def _require_type(value: object, expected: type, label: str) -> None:
    """Raise unless `value` is an instance of `expected`.

    The guard every direct-entry-point function in this module runs before touching an
    argument it did not itself decode, so a caller reaching these functions without a
    tolerant `from_wire` in front sees a `ContractSemanticError` rather than a raw
    `TypeError`/`AttributeError` from whatever happens next.
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


def _require_sequence(value: object, label: str, maximum: int) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractSemanticError(f"{label}: expected a sequence, got {type(value).__name__}")
    if len(value) > maximum:
        raise ContractSemanticError(f"{label}: carries more than {maximum} entries")
    return value


def _require_at_least(value: object, minimum: int, label: str) -> int:
    number = _require_int(value, label)
    if number < minimum:
        raise ContractSemanticError(f"{label}: {number!r} must be {minimum} or greater")
    return number


# --- bounded pattern-vocabulary validation ------------------------------------


def _bounded(value: object, label: str, expression: re.Pattern[str], kind: str, maximum: int) -> str:
    text = _require_str(value, label)
    if len(text) > maximum or not expression.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid {kind}")
    return text


def _validate_identifier(value: object, label: str) -> str:
    return _bounded(value, label, _IDENTIFIER_RE, "Identifier", _BOUNDED_VALUE_MAX_LENGTH)


def _validate_workspace_id(value: object, label: str) -> str:
    return _bounded(value, label, _WORKSPACE_ID_RE, "WorkspaceId", _BOUNDED_VALUE_MAX_LENGTH)


def _validate_open_code(value: object, label: str) -> str:
    return _bounded(value, label, _OPEN_CODE_RE, "OpenCode", _BOUNDED_VALUE_MAX_LENGTH)


def _validate_capability_id(value: object, label: str) -> str:
    return _bounded(value, label, _CAPABILITY_ID_RE, "CapabilityId", _BOUNDED_VALUE_MAX_LENGTH)


def _validate_audit_reference(value: object, label: str) -> str:
    return _bounded(value, label, _AUDIT_REFERENCE_RE, "AuditReference", _BOUNDED_VALUE_MAX_LENGTH)


def _validate_idempotency_key(value: object, label: str) -> str:
    return _bounded(value, label, _IDEMPOTENCY_KEY_RE, "IdempotencyKey", _BOUNDED_VALUE_MAX_LENGTH)


def _validate_opaque_token(value: object, label: str) -> str:
    return _bounded(value, label, _OPAQUE_TOKEN_RE, "OpaqueToken", _OPAQUE_TOKEN_MAX_LENGTH)


def _validate_content_checksum(value: object, label: str) -> str:
    return _bounded(value, label, _CONTENT_CHECKSUM_RE, "ContentChecksum", _CONTENT_CHECKSUM_LENGTH)


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
        raise ContractSemanticError(f"{label}: {text!r} is not a valid RFC 3339 timestamp") from error


def _require_member(value: object, vocabulary: tuple[str, ...], label: str, kind: str) -> str:
    """Raise unless `value` is a declared member of a closed Runtime vocabulary.

    Applied only where a decision actually turns on the value. `RunStatus` is deliberately
    *not* validated this way on a decoded record: an unrecognized status is preserved, and
    the fail-safe readers below are what refuse to act on it.
    """
    text = _require_str(value, label)
    if text not in vocabulary:
        raise ContractSemanticError(f"{label}: {text!r} is not a known {kind}")
    return text


def _require_scoped(
    actual: object, expected: str, label: str, what: str
) -> None:
    """Raise unless a child record restates the identifier its parent published.

    The one rule every record in a run obeys twice over -- once for the workspace, once for
    the run -- so a history can never be assembled from records that belong to different
    runs or, worse, to the same run id in a different workspace.
    """
    text = _require_str(actual, label)
    if text != expected:
        raise ContractSemanticError(f"{label}: {text!r} is not this {what} ({expected!r})")


def _require_known_step(run_step_id: object, step_ids: Sequence[str], label: str) -> None:
    """Raise unless an optional `run_step_id` names a step of the run recording it.

    Every record that may attribute itself to a step -- an event, an artifact, an evidence
    item -- names one that exists or names none at all. A step id resolving to nothing
    attributes work to a step no reader can follow, which is worse than saying nothing.
    """
    if run_step_id is not None and run_step_id not in step_ids:
        raise ContractSemanticError(
            f"{label}: run_step_id {run_step_id!r} names no step of this run"
        )


def _require_unique(values: Sequence[str], label: str, what: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ContractSemanticError(f"{label}: {what} {value!r} appears more than once")
        seen.add(value)


# --- open-vocabulary readings that fail safe ----------------------------------


def is_known_run_status(status: object) -> bool:
    """Whether `status` is a `RunStatus` this build declares.

    Every other reader is written in terms of this one: a status outside the vocabulary is
    not a status this build can reason about, and the honest answer to any question about it
    is "no".
    """
    return isinstance(status, str) and status in RUN_STATUSES


def is_terminal_run_status(status: object) -> bool:
    """Whether a run in `status` is finished.

    `uncertain` is not, and neither is an unrecognized status: treating one as finished
    would close a run over work this build cannot account for.
    """
    return isinstance(status, str) and status in RUN_TERMINAL_STATUSES


def is_successful_run_status(status: object) -> bool:
    """Whether a run in `status` succeeded outright.

    `partially_completed` is not a success for this purpose: it is a run that reached the end
    with something unfinished, and a caller reading it as success would act on an answer
    nobody gave. An unrecognized status is never a success.
    """
    return isinstance(status, str) and status == RUN_STATUS_SUCCEEDED


def is_waiting_run_status(status: object) -> bool:
    """Whether a run in `status` is durably suspended on a `Wait`.

    Only `waiting` is. An unrecognized status is never treated as waiting, so a wait is never
    resolved against a run this build cannot read.
    """
    return isinstance(status, str) and status == RUN_STATUS_WAITING


def permits_new_effect(status: object) -> bool:
    """Whether a run in `status` may declare a new `EffectIntent`.

    Only a running run may. A run that is admitted has not started, a waiting one is
    suspended, a terminal one is finished, an `uncertain` one owes a reconciliation before it
    acts again, and an unrecognized status grants nothing.
    """
    return isinstance(status, str) and status == RUN_STATUS_RUNNING


def is_authoritative_source(source_kind: object) -> bool:
    """Whether facts from `source_kind` may be treated as authority.

    Only the canonical runtime's own record may. A control-plane projection, an agent-lane
    ledger and an external log are correlation and corroboration; discovering a fact through
    one is not the same as the runtime having recorded it, and an unrecognized kind is never
    authority either.
    """
    return isinstance(source_kind, str) and source_kind == RUNTIME_AUTHORITATIVE_SOURCE_KIND


# --- value types ---------------------------------------------------------------


def validate_external_reference(
    reference: object, *, workspace_id: object, label: str = "reference"
) -> None:
    """Raise unless `reference` is a well-formed, workspace-scoped correlation.

    `workspace_id` is trusted context rather than something read off the reference: the point
    of the check is that the reference belongs to the workspace the caller is working in, and
    comparing a value against itself proves nothing.
    """
    _require_type(reference, ExternalReference, label)
    assert isinstance(reference, ExternalReference)
    expected = _validate_workspace_id(workspace_id, "workspace_id")
    _require_member(
        reference.source_kind, RUNTIME_SOURCE_KINDS, f"{label}.source_kind", "RuntimeSourceKind"
    )
    _validate_opaque_token(reference.source_id, f"{label}.source_id")
    _require_scoped(reference.workspace_id, expected, f"{label}.workspace_id", "workspace")


def _validate_run_definition_ref(definition: object, label: str) -> None:
    _require_type(definition, RunDefinitionRef, label)
    assert isinstance(definition, RunDefinitionRef)
    _require_member(
        definition.definition_kind,
        RUN_DEFINITION_KINDS,
        f"{label}.definition_kind",
        "RunDefinitionKind",
    )
    _validate_identifier(definition.definition_id, f"{label}.definition_id")
    _bounded(
        definition.definition_version,
        f"{label}.definition_version",
        _RELEASE_VERSION_RE,
        "ReleaseVersion",
        _RELEASE_VERSION_MAX_LENGTH,
    )


# --- policy and budget ---------------------------------------------------------


def validate_policy_snapshot(
    policy: object, *, run_id: object, workspace_id: object, label: str = "policy"
) -> None:
    """Raise unless `policy` is a well-formed snapshot pinned to this run and workspace.

    The two capability sets are checked for duplicates independently and are allowed to
    overlap: a capability that is both offered and granted is the ordinary case. What is not
    allowed is reading the discovered set as authority, which
    :func:`validate_capability_grant` refuses.
    """
    _require_type(policy, PolicySnapshot, label)
    assert isinstance(policy, PolicySnapshot)
    expected_workspace = _validate_workspace_id(workspace_id, "workspace_id")
    expected_run = _validate_identifier(run_id, "run_id")
    _require_scoped(policy.workspace_id, expected_workspace, f"{label}.workspace_id", "workspace")
    _require_scoped(policy.run_id, expected_run, f"{label}.run_id", "run")
    _validate_identifier(policy.policy_snapshot_id, f"{label}.policy_snapshot_id")
    _require_at_least(policy.revision, 1, f"{label}.revision")
    _parse_timestamp(policy.pinned_at, f"{label}.pinned_at")
    _validate_open_code(policy.decision_reason, f"{label}.decision_reason")
    _validate_audit_reference(policy.audit_reference, f"{label}.audit_reference")
    for field, values in (
        ("granted_capabilities", policy.granted_capabilities),
        ("discovered_capabilities", policy.discovered_capabilities),
    ):
        entries = _require_sequence(values, f"{label}.{field}", _MAX_CAPABILITIES)
        ids = [
            _validate_capability_id(entry, f"{label}.{field}[{index}]")
            for index, entry in enumerate(entries)
        ]
        _require_unique(ids, f"{label}.{field}", "capability")


def validate_policy_snapshot_progression(
    previous: object, current: object, label: str = "policy"
) -> None:
    """Raise unless `current` is a legal successor of `previous` for one run.

    Policy is monotonic in both senses that matter: revisions move forward and instants do
    not regress, and the granted set may narrow but never widen. A run that lost authority
    mid-flight cannot silently regain it, so a later snapshot granting something an earlier
    one did not is refused even though each snapshot is individually well formed.

    This is a direct entry point, so *individually well formed* is established here rather
    than assumed: both snapshots are validated in full -- capability sequences included --
    against the previous snapshot's own run and workspace, before any set arithmetic. A
    hand-built snapshot whose `granted_capabilities` is a number or a nested list is refused
    as a contract error rather than exploding inside `set()`.
    """
    _require_type(previous, PolicySnapshot, f"{label} (previous)")
    _require_type(current, PolicySnapshot, f"{label} (current)")
    assert isinstance(previous, PolicySnapshot)
    assert isinstance(current, PolicySnapshot)
    scope = {
        "run_id": _validate_identifier(previous.run_id, f"{label} (previous).run_id"),
        "workspace_id": _validate_workspace_id(
            previous.workspace_id, f"{label} (previous).workspace_id"
        ),
    }
    validate_policy_snapshot(previous, **scope, label=f"{label} (previous)")
    validate_policy_snapshot(current, **scope, label=f"{label} (current)")
    before = previous.revision
    after = current.revision
    if after <= before:
        raise ContractSemanticError(
            f"{label}: revision {after} does not advance on {before}; policy revisions "
            "move forward only"
        )
    if _parse_timestamp(current.pinned_at, f"{label} (current).pinned_at") < _parse_timestamp(
        previous.pinned_at, f"{label} (previous).pinned_at"
    ):
        raise ContractSemanticError(f"{label}: pinned_at regresses between revisions")
    widened = sorted(set(current.granted_capabilities) - set(previous.granted_capabilities))
    if widened:
        raise ContractSemanticError(
            f"{label}: revision {after} grants {widened}, which revision {before} did not; "
            "a run's granted capabilities may narrow but never widen"
        )


def validate_budget_snapshot(
    budget: object, *, run_id: object, workspace_id: object, label: str = "budget"
) -> None:
    """Raise unless `budget` is a well-formed snapshot pinned to this run and workspace.

    Consumption never exceeds its own ceiling. A run reporting more consumed than it was
    admitted for has either overspent or lost count, and neither is a number a caller can act
    on.
    """
    _require_type(budget, BudgetSnapshot, label)
    assert isinstance(budget, BudgetSnapshot)
    expected_workspace = _validate_workspace_id(workspace_id, "workspace_id")
    expected_run = _validate_identifier(run_id, "run_id")
    _require_scoped(budget.workspace_id, expected_workspace, f"{label}.workspace_id", "workspace")
    _require_scoped(budget.run_id, expected_run, f"{label}.run_id", "run")
    _validate_identifier(budget.budget_snapshot_id, f"{label}.budget_snapshot_id")
    _require_at_least(budget.revision, 1, f"{label}.revision")
    _parse_timestamp(budget.pinned_at, f"{label}.pinned_at")
    for limit_field, consumed_field in (
        ("max_cost_units", "consumed_cost_units"),
        ("max_token_units", "consumed_token_units"),
    ):
        limit = _require_at_least(getattr(budget, limit_field), 0, f"{label}.{limit_field}")
        consumed = _require_at_least(getattr(budget, consumed_field), 0, f"{label}.{consumed_field}")
        if consumed > limit:
            raise ContractSemanticError(
                f"{label}: {consumed_field} {consumed} exceeds {limit_field} {limit}"
            )
    if budget.max_wall_clock_ms is not None:
        _require_at_least(budget.max_wall_clock_ms, 0, f"{label}.max_wall_clock_ms")


def validate_budget_snapshot_progression(
    previous: object, current: object, label: str = "budget"
) -> None:
    """Raise unless `current` is a legal successor of `previous` for one run.

    Budget is monotonic the same way policy is, in the direction that protects the caller:
    revisions move forward, ceilings may narrow but never widen, and consumption never
    decreases. A counter that went backwards is a report nobody can audit.

    And, as with policy, both snapshots are validated in full first, against the previous
    snapshot's own run and workspace: two budgets for different runs -- or for the same run
    id in a different workspace -- are not two revisions of one budget, and comparing their
    ceilings would answer a question nobody asked.
    """
    _require_type(previous, BudgetSnapshot, f"{label} (previous)")
    _require_type(current, BudgetSnapshot, f"{label} (current)")
    assert isinstance(previous, BudgetSnapshot)
    assert isinstance(current, BudgetSnapshot)
    scope = {
        "run_id": _validate_identifier(previous.run_id, f"{label} (previous).run_id"),
        "workspace_id": _validate_workspace_id(
            previous.workspace_id, f"{label} (previous).workspace_id"
        ),
    }
    validate_budget_snapshot(previous, **scope, label=f"{label} (previous)")
    validate_budget_snapshot(current, **scope, label=f"{label} (current)")
    before = previous.revision
    after = current.revision
    if after <= before:
        raise ContractSemanticError(
            f"{label}: revision {after} does not advance on {before}; budget revisions "
            "move forward only"
        )
    for field in ("max_cost_units", "max_token_units"):
        widened = _require_at_least(getattr(current, field), 0, f"{label} (current).{field}")
        original = _require_at_least(getattr(previous, field), 0, f"{label} (previous).{field}")
        if widened > original:
            raise ContractSemanticError(
                f"{label}: {field} widens from {original} to {widened}; a pinned ceiling "
                "may narrow but never widen"
            )
    for field in ("consumed_cost_units", "consumed_token_units"):
        later = _require_at_least(getattr(current, field), 0, f"{label} (current).{field}")
        earlier = _require_at_least(getattr(previous, field), 0, f"{label} (previous).{field}")
        if later < earlier:
            raise ContractSemanticError(
                f"{label}: {field} regresses from {earlier} to {later}; consumption never "
                "decreases"
            )


def validate_capability_grant(
    grant: object,
    *,
    run_id: object,
    workspace_id: object,
    policy: object,
    label: str = "capability_grant",
) -> None:
    """Raise unless `grant` is a well-formed grant this run's policy actually backs.

    This is where *discovery is not authority* becomes a refusal rather than a slogan: the
    granted capability must appear in the pinned policy's `granted_capabilities`. Appearing
    in `discovered_capabilities` -- the set naming what the workspace was found to offer --
    authorizes nothing, and a grant whose capability has since been narrowed out of the
    granted set is refused too, because a grant may not outlive the decision behind it.
    """
    _require_type(grant, CapabilityGrant, label)
    assert isinstance(grant, CapabilityGrant)
    _require_type(policy, PolicySnapshot, "policy")
    assert isinstance(policy, PolicySnapshot)
    expected_workspace = _validate_workspace_id(workspace_id, "workspace_id")
    expected_run = _validate_identifier(run_id, "run_id")
    _require_scoped(grant.workspace_id, expected_workspace, f"{label}.workspace_id", "workspace")
    _require_scoped(grant.run_id, expected_run, f"{label}.run_id", "run")
    _validate_identifier(grant.capability_grant_id, f"{label}.capability_grant_id")
    _validate_identifier(grant.policy_snapshot_id, f"{label}.policy_snapshot_id")
    capability = _validate_capability_id(grant.capability_id, f"{label}.capability_id")
    granted_at = _parse_timestamp(grant.granted_at, f"{label}.granted_at")
    if grant.expires_at is not None:
        expires_at = _parse_timestamp(grant.expires_at, f"{label}.expires_at")
        if expires_at < granted_at:
            raise ContractSemanticError(f"{label}: expires_at precedes granted_at")
    scopes = _require_sequence(grant.scopes, f"{label}.scopes", _MAX_SCOPES)
    for index, scope in enumerate(scopes):
        _bounded(scope, f"{label}.scopes[{index}]", _SCOPE_RE, "Scope", _BOUNDED_VALUE_MAX_LENGTH)
    _bounded(grant.purpose, f"{label}.purpose", _PURPOSE_RE, "Purpose", _BOUNDED_VALUE_MAX_LENGTH)
    if capability not in policy.granted_capabilities:
        discovered = capability in policy.discovered_capabilities
        detail = (
            " it appears only in discovered_capabilities, and discovery is not authority"
            if discovered
            else ""
        )
        raise ContractSemanticError(
            f"{label}: capability {capability!r} is not granted by the pinned policy;{detail}"
        )


# --- steps and attempts --------------------------------------------------------


def validate_attempt(
    attempt: object,
    *,
    run_id: object,
    run_step_id: object,
    workspace_id: object,
    label: str = "attempt",
) -> None:
    """Raise unless `attempt` is a well-formed execution of this step.

    Two rules beyond shape. A running attempt has not finished, so it states no
    `finished_at`, and every attempt that has stopped states one; and an `uncertain` attempt
    carries no failure, because uncertainty is not failure -- attaching an error to it would
    licence exactly the blind retry the uncertainty forbids.
    """
    _require_type(attempt, Attempt, label)
    assert isinstance(attempt, Attempt)
    expected_workspace = _validate_workspace_id(workspace_id, "workspace_id")
    expected_run = _validate_identifier(run_id, "run_id")
    expected_step = _validate_identifier(run_step_id, "run_step_id")
    _require_scoped(attempt.workspace_id, expected_workspace, f"{label}.workspace_id", "workspace")
    _require_scoped(attempt.run_id, expected_run, f"{label}.run_id", "run")
    _require_scoped(attempt.run_step_id, expected_step, f"{label}.run_step_id", "step")
    _validate_identifier(attempt.attempt_id, f"{label}.attempt_id")
    _require_at_least(attempt.attempt_number, 1, f"{label}.attempt_number")
    status = _require_member(attempt.status, ATTEMPT_STATUSES, f"{label}.status", "AttemptStatus")
    started_at = _parse_timestamp(attempt.started_at, f"{label}.started_at")

    running = status == ATTEMPT_STATUS_RUNNING
    if running and attempt.finished_at is not None:
        raise ContractSemanticError(f"{label}: a running attempt has not finished")
    if not running:
        if attempt.finished_at is None:
            raise ContractSemanticError(f"{label}: a {status!r} attempt must state finished_at")
        if _parse_timestamp(attempt.finished_at, f"{label}.finished_at") < started_at:
            raise ContractSemanticError(f"{label}: finished_at precedes started_at")
    if attempt.failure is not None:
        _require_type(attempt.failure, ApiError, f"{label}.failure")
        if status != ATTEMPT_STATUS_FAILED:
            raise ContractSemanticError(
                f"{label}: a {status!r} attempt carries no failure; uncertainty is not failure"
            )
    elif status == ATTEMPT_STATUS_FAILED:
        raise ContractSemanticError(f"{label}: a failed attempt must state its failure")


def validate_attempt_history(
    attempts: object,
    *,
    run_id: object,
    run_step_id: object,
    workspace_id: object,
    label: str = "attempts",
) -> None:
    """Raise unless `attempts` is a contiguous, immutable history of one step.

    Attempt numbers run `1..N` with no gap and no repeat, attempts never overlap in time,
    and only a `failed`, `cancelled` or `uncertain` attempt may be followed by another: a
    `succeeded` attempt is final, and a history that continues past one is describing work
    that was already done.
    """
    entries = _require_sequence(attempts, label, _MAX_ATTEMPTS)
    previous_finished: datetime | None = None
    previous_status: str | None = None
    for index, attempt in enumerate(entries):
        item_label = f"{label}[{index}]"
        validate_attempt(
            attempt,
            run_id=run_id,
            run_step_id=run_step_id,
            workspace_id=workspace_id,
            label=item_label,
        )
        assert isinstance(attempt, Attempt)
        if attempt.attempt_number != index + 1:
            raise ContractSemanticError(
                f"{item_label}: attempt_number {attempt.attempt_number} breaks the contiguous "
                f"1..N sequence (expected {index + 1})"
            )
        if previous_status is not None and previous_status not in ATTEMPT_CONTINUABLE_STATUSES:
            raise ContractSemanticError(
                f"{item_label}: no attempt may follow a {previous_status!r} attempt"
            )
        started_at = _parse_timestamp(attempt.started_at, f"{item_label}.started_at")
        if previous_finished is not None and started_at < previous_finished:
            raise ContractSemanticError(
                f"{item_label}: starts before the preceding attempt finished; attempts of one "
                "step never overlap"
            )
        previous_finished = (
            None
            if attempt.finished_at is None
            else _parse_timestamp(attempt.finished_at, f"{item_label}.finished_at")
        )
        previous_status = attempt.status
    _require_unique(
        [attempt.attempt_id for attempt in entries if isinstance(attempt, Attempt)],
        label,
        "attempt",
    )


def validate_run_step(
    step: object, *, run_id: object, workspace_id: object, label: str = "step"
) -> None:
    """Raise unless `step` is a well-formed step of this run, with a coherent history.

    A `waiting` step names the wait holding it, and no other step does: a suspended step that
    cannot say what it is suspended on cannot be resolved, and a step that is not suspended
    has nothing to name. Whether the named wait exists is a whole-run question, answered by
    :func:`validate_run`.
    """
    _require_type(step, RunStep, label)
    assert isinstance(step, RunStep)
    expected_workspace = _validate_workspace_id(workspace_id, "workspace_id")
    expected_run = _validate_identifier(run_id, "run_id")
    _require_scoped(step.workspace_id, expected_workspace, f"{label}.workspace_id", "workspace")
    _require_scoped(step.run_id, expected_run, f"{label}.run_id", "run")
    step_id = _validate_identifier(step.run_step_id, f"{label}.run_step_id")
    _require_at_least(step.ordinal, 1, f"{label}.ordinal")
    _validate_open_code(step.step_kind, f"{label}.step_kind")
    status = _require_member(step.status, RUN_STEP_STATUSES, f"{label}.status", "RunStepStatus")
    created_at = _parse_timestamp(step.created_at, f"{label}.created_at")
    if _parse_timestamp(step.updated_at, f"{label}.updated_at") < created_at:
        raise ContractSemanticError(f"{label}: updated_at precedes created_at")
    validate_attempt_history(
        step.attempts,
        run_id=expected_run,
        run_step_id=step_id,
        workspace_id=expected_workspace,
        label=f"{label}.attempts",
    )
    waiting = status == _RUN_STEP_STATUS_WAITING
    if waiting and step.wait_id is None:
        raise ContractSemanticError(f"{label}: a waiting step must name the wait holding it")
    if not waiting and step.wait_id is not None:
        raise ContractSemanticError(f"{label}: a {status!r} step names no wait")
    if step.wait_id is not None:
        _validate_identifier(step.wait_id, f"{label}.wait_id")


# --- waits and approvals -------------------------------------------------------


def validate_wait(
    wait: object, *, run_id: object, workspace_id: object, label: str = "wait"
) -> None:
    """Raise unless `wait` is a well-formed durable suspension of this run.

    `resolved_at` is present exactly when the wait has stopped being pending, so "still
    waiting" and "resolved but undated" are not two spellings of one state. Only a resolved
    `approval` wait names an approval: a timer that claims an approver is describing a
    decision nobody made, and a *pending* wait that already names one is claiming the
    decision it is still waiting for.
    """
    _require_type(wait, Wait, label)
    assert isinstance(wait, Wait)
    expected_workspace = _validate_workspace_id(workspace_id, "workspace_id")
    expected_run = _validate_identifier(run_id, "run_id")
    _require_scoped(wait.workspace_id, expected_workspace, f"{label}.workspace_id", "workspace")
    _require_scoped(wait.run_id, expected_run, f"{label}.run_id", "run")
    _validate_identifier(wait.wait_id, f"{label}.wait_id")
    _validate_identifier(wait.run_step_id, f"{label}.run_step_id")
    kind = _require_member(wait.kind, WAIT_KINDS, f"{label}.kind", "WaitKind")
    status = _require_member(wait.status, WAIT_STATUSES, f"{label}.status", "WaitStatus")
    created_at = _parse_timestamp(wait.created_at, f"{label}.created_at")
    _validate_content_checksum(wait.resume_digest, f"{label}.resume_digest")
    if (
        wait.expires_at is not None
        and _parse_timestamp(wait.expires_at, f"{label}.expires_at") < created_at
    ):
        raise ContractSemanticError(f"{label}: expires_at precedes created_at")
    if wait.resolution_reason is not None:
        _validate_open_code(wait.resolution_reason, f"{label}.resolution_reason")

    pending = status == WAIT_STATUS_PENDING
    if pending and wait.resolved_at is not None:
        raise ContractSemanticError(f"{label}: a pending wait has not been resolved")
    if not pending:
        if wait.resolved_at is None:
            raise ContractSemanticError(f"{label}: a {status!r} wait must state resolved_at")
        if _parse_timestamp(wait.resolved_at, f"{label}.resolved_at") < created_at:
            raise ContractSemanticError(f"{label}: resolved_at precedes created_at")
    if wait.approval_id is not None:
        _validate_identifier(wait.approval_id, f"{label}.approval_id")
        if kind != "approval":
            raise ContractSemanticError(f"{label}: a {kind!r} wait names no approval")
        if pending:
            raise ContractSemanticError(
                f"{label}: a pending wait names no approval; the approval is the decision this "
                "wait is still waiting for"
            )


def validate_approval(
    approval: object, *, run_id: object, workspace_id: object, label: str = "approval"
) -> None:
    """Raise unless `approval` is a well-formed request/decision pair for this run.

    A decision is complete or absent, never partial: the decision, its instant, its decider
    and its audit reference stand or fall together, because a decision nobody can attribute
    is not an approval. Everything about the request half is immutable once written, so a
    decision that predates its own request is refused rather than reordered.
    """
    _require_type(approval, Approval, label)
    assert isinstance(approval, Approval)
    expected_workspace = _validate_workspace_id(workspace_id, "workspace_id")
    expected_run = _validate_identifier(run_id, "run_id")
    _require_scoped(approval.workspace_id, expected_workspace, f"{label}.workspace_id", "workspace")
    _require_scoped(approval.run_id, expected_run, f"{label}.run_id", "run")
    _validate_identifier(approval.approval_id, f"{label}.approval_id")
    _validate_identifier(approval.wait_id, f"{label}.wait_id")
    _validate_identifier(approval.approver_role, f"{label}.approver_role")
    requested_at = _parse_timestamp(approval.requested_at, f"{label}.requested_at")
    for optional_field in ("assigned_to", "escalated_to"):
        value = getattr(approval, optional_field)
        if value is not None:
            _validate_identifier(value, f"{label}.{optional_field}")
    if (
        approval.expires_at is not None
        and _parse_timestamp(approval.expires_at, f"{label}.expires_at") < requested_at
    ):
        raise ContractSemanticError(f"{label}: expires_at precedes requested_at")
    if approval.comment is not None:
        _validate_message(approval.comment, f"{label}.comment")

    decided = [
        name
        for name in ("decision", "decided_at", "decided_by", "audit_reference")
        if getattr(approval, name) is not None
    ]
    if decided and len(decided) != 4:
        missing = sorted({"decision", "decided_at", "decided_by", "audit_reference"} - set(decided))
        raise ContractSemanticError(
            f"{label}: a recorded decision states all of decision, decided_at, decided_by and "
            f"audit_reference; missing {missing}"
        )
    if not decided:
        return
    _require_member(approval.decision, APPROVAL_DECISIONS, f"{label}.decision", "ApprovalDecision")
    _validate_identifier(approval.decided_by, f"{label}.decided_by")
    _validate_audit_reference(approval.audit_reference, f"{label}.audit_reference")
    if _parse_timestamp(approval.decided_at, f"{label}.decided_at") < requested_at:
        raise ContractSemanticError(f"{label}: decided_at precedes requested_at")


# --- effects -------------------------------------------------------------------


def validate_effect_intent(
    intent: object,
    *,
    run_id: object,
    workspace_id: object,
    grants: object,
    label: str = "effect_intent",
) -> None:
    """Raise unless `intent` is a well-formed effect this run is authorized to attempt.

    `grants` is the run's issued capability grants, supplied as trusted context: an intent
    names the grant it acts through, that grant must exist, and it must be a grant of the
    very capability the intent invokes. A grant for something else is not authorization for
    this effect, and no capability the run merely discovered is authorization for anything --
    :func:`validate_capability_grant` is what holds each grant to its policy.

    The context itself is held to one rule, because this is a direct entry point and nothing
    upstream may have checked it: two grants sharing a `capability_grant_id` are refused
    rather than resolved to whichever came last, since which of them authorized the effect
    would then be decided by list order.
    """
    _require_type(intent, EffectIntent, label)
    assert isinstance(intent, EffectIntent)
    expected_workspace = _validate_workspace_id(workspace_id, "workspace_id")
    expected_run = _validate_identifier(run_id, "run_id")
    _require_scoped(intent.workspace_id, expected_workspace, f"{label}.workspace_id", "workspace")
    _require_scoped(intent.run_id, expected_run, f"{label}.run_id", "run")
    _validate_identifier(intent.effect_intent_id, f"{label}.effect_intent_id")
    _validate_identifier(intent.run_step_id, f"{label}.run_step_id")
    _validate_identifier(intent.attempt_id, f"{label}.attempt_id")
    capability = _validate_capability_id(intent.capability_id, f"{label}.capability_id")
    grant_id = _validate_identifier(intent.capability_grant_id, f"{label}.capability_grant_id")
    _validate_open_code(intent.effect_kind, f"{label}.effect_kind")
    _validate_idempotency_key(intent.idempotency_key, f"{label}.idempotency_key")
    _validate_content_checksum(intent.request_digest, f"{label}.request_digest")
    _parse_timestamp(intent.declared_at, f"{label}.declared_at")

    issued = _require_sequence(grants, "grants", _MAX_GRANTS)
    by_id: dict[str, CapabilityGrant] = {}
    for index, grant in enumerate(issued):
        _require_type(grant, CapabilityGrant, f"grants[{index}]")
        assert isinstance(grant, CapabilityGrant)
        issued_id = _require_str(grant.capability_grant_id, f"grants[{index}].capability_grant_id")
        if issued_id in by_id:
            raise ContractSemanticError(
                f"grants[{index}]: capability_grant_id {issued_id!r} appears more than once; a "
                "duplicated grant id makes the grant an intent acts through ambiguous"
            )
        by_id[issued_id] = grant
    grant = by_id.get(grant_id)
    if grant is None:
        raise ContractSemanticError(
            f"{label}: capability_grant_id {grant_id!r} names no grant issued to this run"
        )
    if grant.capability_id != capability:
        raise ContractSemanticError(
            f"{label}: grant {grant_id!r} grants {grant.capability_id!r}, not {capability!r}"
        )


def _intents_by_id(intents: object, label: str = "intents") -> dict[str, EffectIntent]:
    """Index the declared intents a receipt or settlement is judged against.

    The context itself is held to one rule, for the reason :func:`validate_effect_intent`
    holds its grants to the same one: two intents sharing an `effect_intent_id` would make
    "the intent this receipt is evidence for" a question list order answers, and the instant
    a receipt is checked against would then depend on which duplicate came last.
    """
    entries = _require_sequence(intents, label, _MAX_EFFECTS)
    by_id: dict[str, EffectIntent] = {}
    for index, intent in enumerate(entries):
        _require_type(intent, EffectIntent, f"{label}[{index}]")
        assert isinstance(intent, EffectIntent)
        intent_id = _require_str(intent.effect_intent_id, f"{label}[{index}].effect_intent_id")
        if intent_id in by_id:
            raise ContractSemanticError(
                f"{label}[{index}]: effect_intent_id {intent_id!r} appears more than once; a "
                "duplicated intent id makes the intent an effect is judged against ambiguous"
            )
        by_id[intent_id] = intent
    return by_id


def validate_effect_receipt(
    receipt: object,
    *,
    run_id: object,
    workspace_id: object,
    intents: object,
    label: str = "effect_receipt",
) -> None:
    """Raise unless `receipt` is evidence for an effect this run actually intended.

    No effect before intent, enforced as a refusal: a receipt naming no declared intent is an
    effect nobody authorized, and one observed before its intent was declared is evidence of
    something other than what the intent describes. The provider-side reference, when there
    is one, is subordinate -- it identifies the effect elsewhere and never establishes here
    that it happened.
    """
    _require_type(receipt, EffectReceipt, label)
    assert isinstance(receipt, EffectReceipt)
    expected_workspace = _validate_workspace_id(workspace_id, "workspace_id")
    expected_run = _validate_identifier(run_id, "run_id")
    _require_scoped(receipt.workspace_id, expected_workspace, f"{label}.workspace_id", "workspace")
    _require_scoped(receipt.run_id, expected_run, f"{label}.run_id", "run")
    _validate_identifier(receipt.effect_receipt_id, f"{label}.effect_receipt_id")
    intent_id = _validate_identifier(receipt.effect_intent_id, f"{label}.effect_intent_id")
    observed_at = _parse_timestamp(receipt.observed_at, f"{label}.observed_at")
    _validate_content_checksum(receipt.response_digest, f"{label}.response_digest")
    if receipt.external_reference is not None:
        validate_external_reference(
            receipt.external_reference,
            workspace_id=expected_workspace,
            label=f"{label}.external_reference",
        )

    intent = _intents_by_id(intents).get(intent_id)
    if intent is None:
        raise ContractSemanticError(
            f"{label}: effect_intent_id {intent_id!r} names no declared intent; there is no "
            "effect receipt without an effect intent"
        )
    if observed_at < _parse_timestamp(intent.declared_at, f"{label}.intent.declared_at"):
        raise ContractSemanticError(
            f"{label}: observed_at precedes the intent's declared_at; an effect is never "
            "observed before it was intended"
        )


def validate_effect_settlement(
    settlement: object,
    *,
    run_id: object,
    workspace_id: object,
    intents: object,
    receipts: object,
    label: str = "effect_settlement",
) -> None:
    """Raise unless `settlement` is a well-formed answer about an effect this run intended.

    Three rules. No settlement without its intent, and none before it was declared. A
    `committed` outcome names the receipt that proves it, and that receipt must be evidence
    for the same intent -- a settlement pointing at some other effect's receipt is not proof
    of this one. And only `committed` names a receipt at all: a `not_committed` or `unknown`
    settlement that carried one would be claiming and denying the same observation.
    """
    _require_type(settlement, EffectSettlement, label)
    assert isinstance(settlement, EffectSettlement)
    expected_workspace = _validate_workspace_id(workspace_id, "workspace_id")
    expected_run = _validate_identifier(run_id, "run_id")
    _require_scoped(
        settlement.workspace_id, expected_workspace, f"{label}.workspace_id", "workspace"
    )
    _require_scoped(settlement.run_id, expected_run, f"{label}.run_id", "run")
    _validate_identifier(settlement.effect_settlement_id, f"{label}.effect_settlement_id")
    intent_id = _validate_identifier(settlement.effect_intent_id, f"{label}.effect_intent_id")
    outcome = _require_member(settlement.outcome, EFFECT_OUTCOMES, f"{label}.outcome", "EffectOutcome")
    settled_at = _parse_timestamp(settlement.settled_at, f"{label}.settled_at")
    _validate_open_code(settlement.reason, f"{label}.reason")
    _validate_audit_reference(settlement.audit_reference, f"{label}.audit_reference")

    intent = _intents_by_id(intents).get(intent_id)
    if intent is None:
        raise ContractSemanticError(
            f"{label}: effect_intent_id {intent_id!r} names no declared intent; there is no "
            "effect settlement without an effect intent"
        )
    if settled_at < _parse_timestamp(intent.declared_at, f"{label}.intent.declared_at"):
        raise ContractSemanticError(
            f"{label}: settled_at precedes the intent's declared_at; an effect is never "
            "settled before it was intended"
        )

    observed = _require_sequence(receipts, "receipts", _MAX_EFFECTS)
    by_id: dict[str, EffectReceipt] = {}
    for index, receipt in enumerate(observed):
        _require_type(receipt, EffectReceipt, f"receipts[{index}]")
        assert isinstance(receipt, EffectReceipt)
        observed_id = _require_str(receipt.effect_receipt_id, f"receipts[{index}].effect_receipt_id")
        if observed_id in by_id:
            raise ContractSemanticError(
                f"receipts[{index}]: effect_receipt_id {observed_id!r} appears more than once; a "
                "duplicated receipt id makes the receipt a settlement rests on ambiguous"
            )
        by_id[observed_id] = receipt
    if outcome != EFFECT_OUTCOME_COMMITTED:
        if settlement.effect_receipt_id is not None:
            raise ContractSemanticError(
                f"{label}: a {outcome!r} settlement names no receipt"
            )
        return
    if settlement.effect_receipt_id is None:
        raise ContractSemanticError(
            f"{label}: a committed settlement must name the receipt that proves it"
        )
    receipt_id = _validate_identifier(settlement.effect_receipt_id, f"{label}.effect_receipt_id")
    receipt = by_id.get(receipt_id)
    if receipt is None:
        raise ContractSemanticError(
            f"{label}: effect_receipt_id {receipt_id!r} names no observed receipt"
        )
    if receipt.effect_intent_id != intent_id:
        raise ContractSemanticError(
            f"{label}: receipt {receipt_id!r} is evidence for {receipt.effect_intent_id!r}, "
            f"not for {intent_id!r}"
        )


# --- events, artifacts, evidence and cleanup -----------------------------------


def validate_runtime_event_stream(
    events: object, *, run_id: object, workspace_id: object, label: str = "events"
) -> None:
    """Raise unless `events` is a contiguous, non-regressing stream for this run.

    Sequences run `0..N` with no gap and no repeat, instants never regress, and each
    consecutive pair of *known* statuses must be a legal transition. A step out of, or into,
    a status this build has never seen is not judged at all: an unknown status implies no
    successor, and inventing one would either reject a newer peer's legitimate lifecycle or
    bless a transition this build cannot reason about.
    """
    entries = _require_sequence(events, label, _MAX_EVENTS)
    expected_workspace = _validate_workspace_id(workspace_id, "workspace_id")
    expected_run = _validate_identifier(run_id, "run_id")
    previous_at: datetime | None = None
    previous_status: str | None = None
    for index, event in enumerate(entries):
        item_label = f"{label}[{index}]"
        _require_type(event, RuntimeEvent, item_label)
        assert isinstance(event, RuntimeEvent)
        _require_scoped(
            event.workspace_id, expected_workspace, f"{item_label}.workspace_id", "workspace"
        )
        _require_scoped(event.run_id, expected_run, f"{item_label}.run_id", "run")
        _validate_identifier(event.runtime_event_id, f"{item_label}.runtime_event_id")
        if _require_at_least(event.sequence, 0, f"{item_label}.sequence") != index:
            raise ContractSemanticError(
                f"{item_label}: sequence {event.sequence} breaks the contiguous 0..N stream "
                f"(expected {index})"
            )
        occurred_at = _parse_timestamp(event.occurred_at, f"{item_label}.occurred_at")
        if previous_at is not None and occurred_at < previous_at:
            raise ContractSemanticError(f"{item_label}: occurred_at regresses")
        _validate_open_code(event.event_kind, f"{item_label}.event_kind")
        status = _require_str(event.run_status, f"{item_label}.run_status")
        if previous_status is not None:
            validate_run_status_transition(
                previous_status, status, label=f"{item_label}.run_status"
            )
        if event.run_step_id is not None:
            _validate_identifier(event.run_step_id, f"{item_label}.run_step_id")
        if event.message is not None:
            _validate_message(event.message, f"{item_label}.message")
        previous_at = occurred_at
        previous_status = status
    _require_unique(
        [event.runtime_event_id for event in entries if isinstance(event, RuntimeEvent)],
        label,
        "event",
    )


def validate_run_status_transition(
    previous: object, current: object, label: str = "status"
) -> None:
    """Raise unless a run may move from `previous` to `current` in one step.

    Only known-to-known steps are judged, for the same reason the job contract judges only
    those: an unrecognized status implies neither terminality nor any particular successor.
    A repeat of the same status is always allowed -- two observations of a run that has not
    moved are not an illegal transition.
    """
    before = _require_str(previous, f"{label} (previous)")
    after = _require_str(current, f"{label} (current)")
    if before not in RUN_STATUS_TRANSITIONS or after not in RUN_STATUSES:
        return
    if after == before:
        return
    if after not in RUN_STATUS_TRANSITIONS[before]:
        raise ContractSemanticError(f"{label}: a run may not move from {before!r} to {after!r}")


def validate_artifact(
    artifact: object, *, run_id: object, workspace_id: object, label: str = "artifact"
) -> None:
    """Raise unless `artifact` is a well-formed, content-addressed output of this run."""
    _require_type(artifact, Artifact, label)
    assert isinstance(artifact, Artifact)
    expected_workspace = _validate_workspace_id(workspace_id, "workspace_id")
    expected_run = _validate_identifier(run_id, "run_id")
    _require_scoped(artifact.workspace_id, expected_workspace, f"{label}.workspace_id", "workspace")
    _require_scoped(artifact.run_id, expected_run, f"{label}.run_id", "run")
    _validate_identifier(artifact.artifact_id, f"{label}.artifact_id")
    if artifact.run_step_id is not None:
        _validate_identifier(artifact.run_step_id, f"{label}.run_step_id")
    _validate_open_code(artifact.artifact_kind, f"{label}.artifact_kind")
    _bounded(
        artifact.media_type, f"{label}.media_type", _MEDIA_TYPE_RE, "MediaType",
        _MEDIA_TYPE_MAX_LENGTH,
    )
    _validate_content_checksum(artifact.content_checksum, f"{label}.content_checksum")
    _require_at_least(artifact.content_length_bytes, 0, f"{label}.content_length_bytes")
    _parse_timestamp(artifact.produced_at, f"{label}.produced_at")


def validate_evidence_item(
    item: object, *, run_id: object, workspace_id: object, label: str = "evidence"
) -> None:
    """Raise unless `item` is well-formed evidence captured by this run.

    The load-bearing rule is the last one: only evidence the runtime itself recorded may
    claim to be authoritative. An external log, an agent-lane ledger and a control-plane
    projection are corroboration however complete they look, and letting one claim authority
    would make a subordinate record the answer to a question only the runtime's own record
    can settle.
    """
    _require_type(item, EvidenceItem, label)
    assert isinstance(item, EvidenceItem)
    expected_workspace = _validate_workspace_id(workspace_id, "workspace_id")
    expected_run = _validate_identifier(run_id, "run_id")
    _require_scoped(item.workspace_id, expected_workspace, f"{label}.workspace_id", "workspace")
    _require_scoped(item.run_id, expected_run, f"{label}.run_id", "run")
    _validate_identifier(item.evidence_item_id, f"{label}.evidence_item_id")
    if item.run_step_id is not None:
        _validate_identifier(item.run_step_id, f"{label}.run_step_id")
    if item.artifact_id is not None:
        _validate_identifier(item.artifact_id, f"{label}.artifact_id")
    _validate_open_code(item.evidence_kind, f"{label}.evidence_kind")
    validate_external_reference(
        item.source, workspace_id=expected_workspace, label=f"{label}.source"
    )
    _validate_content_checksum(item.content_checksum, f"{label}.content_checksum")
    _parse_timestamp(item.captured_at, f"{label}.captured_at")
    _require_bool(item.retained, f"{label}.retained")
    if _require_bool(item.authoritative, f"{label}.authoritative") and not is_authoritative_source(
        item.source.source_kind
    ):
        raise ContractSemanticError(
            f"{label}: evidence from {item.source.source_kind!r} is subordinate and may not "
            "be authoritative"
        )


def validate_cleanup_receipt(
    receipt: object, *, run_id: object, workspace_id: object, label: str = "cleanup_receipt"
) -> None:
    """Raise unless `receipt` is a well-formed record of one cleanup this run performed."""
    _require_type(receipt, CleanupReceipt, label)
    assert isinstance(receipt, CleanupReceipt)
    expected_workspace = _validate_workspace_id(workspace_id, "workspace_id")
    expected_run = _validate_identifier(run_id, "run_id")
    _require_scoped(receipt.workspace_id, expected_workspace, f"{label}.workspace_id", "workspace")
    _require_scoped(receipt.run_id, expected_run, f"{label}.run_id", "run")
    _validate_identifier(receipt.cleanup_receipt_id, f"{label}.cleanup_receipt_id")
    _validate_open_code(receipt.resource_kind, f"{label}.resource_kind")
    _require_member(receipt.outcome, CLEANUP_OUTCOMES, f"{label}.outcome", "CleanupOutcome")
    _validate_open_code(receipt.reason, f"{label}.reason")
    _parse_timestamp(receipt.performed_at, f"{label}.performed_at")
    _validate_audit_reference(receipt.audit_reference, f"{label}.audit_reference")


# --- the whole run -------------------------------------------------------------


def validate_run(run: object, *, workspace_id: object, label: str = "run") -> None:
    """Raise unless `run` is a coherent canonical run in `workspace_id`.

    Every record hanging off the run is validated against the run's own workspace and run
    identifiers, so a history assembled from records belonging to a different run -- or to
    the same run id in a different workspace -- is refused rather than merged. On top of
    that, the cross-record rules a single record cannot state:

    - steps are ordered `1..N` contiguously, and every named wait, step and artifact actually
      exists in this run -- including the optional `run_step_id` an event, an artifact or an
      evidence item may carry -- and the references that come in pairs must agree, so a step
      and the wait holding it name each other, a wait and the approval it names are the same
      pairing read from both ends, an approval is requested on a wait that is actually an
      `approval` wait rather than a timer or a signal, and an effect's attempt is one of the
      attempts of the very step the effect names, rather than each merely existing somewhere
      in the run;
    - no effect receipt or settlement exists without its intent;
    - a run holding an `unknown` settlement has not closed -- uncertainty is not failure, so
      a terminal status over an unreconciled effect is a contradiction;
    - a cancelled run keeps its evidence, and a cancelled run that actually executed keeps
      something to show for it;
    - a terminal run states its cleanup, because cleanup is observable rather than assumed;
    - `finished_at` is present exactly when a *known* status is terminal.

    `workspace_id` is trusted context supplied by the caller. An unrecognized `RunStatus` is
    preserved and never rejected here: the status-dependent rules are simply not applied to
    a status this build cannot read, and :func:`validate_terminal_run` is what refuses to
    call such a run finished.
    """
    _require_type(run, Run, label)
    assert isinstance(run, Run)
    expected_workspace = _validate_workspace_id(workspace_id, "workspace_id")
    _require_scoped(run.workspace_id, expected_workspace, f"{label}.workspace_id", "workspace")
    run_id = _validate_identifier(run.run_id, f"{label}.run_id")
    _validate_run_definition_ref(run.definition, f"{label}.definition")
    status = _require_str(run.status, f"{label}.status")
    _validate_idempotency_key(run.logical_key, f"{label}.logical_key")
    _bounded(
        run.originating_operation, f"{label}.originating_operation", _OPERATION_NAME_RE,
        "OperationName", _BOUNDED_VALUE_MAX_LENGTH,
    )
    _validate_audit_reference(run.audit_reference, f"{label}.audit_reference")
    created_at = _parse_timestamp(run.created_at, f"{label}.created_at")
    if _parse_timestamp(run.updated_at, f"{label}.updated_at") < created_at:
        raise ContractSemanticError(f"{label}: updated_at precedes created_at")

    scope = {"run_id": run_id, "workspace_id": expected_workspace}
    validate_policy_snapshot(run.policy, **scope, label=f"{label}.policy")
    validate_budget_snapshot(run.budget, **scope, label=f"{label}.budget")

    grants = _require_sequence(run.capability_grants, f"{label}.capability_grants", _MAX_GRANTS)
    for index, grant in enumerate(grants):
        validate_capability_grant(
            grant, **scope, policy=run.policy, label=f"{label}.capability_grants[{index}]"
        )
    _require_unique(
        [grant.capability_grant_id for grant in grants if isinstance(grant, CapabilityGrant)],
        f"{label}.capability_grants",
        "grant",
    )

    waits = _require_sequence(run.waits, f"{label}.waits", _MAX_WAITS)
    for index, wait in enumerate(waits):
        validate_wait(wait, **scope, label=f"{label}.waits[{index}]")
    waits_by_id = {wait.wait_id: wait for wait in waits if isinstance(wait, Wait)}
    wait_ids = [wait.wait_id for wait in waits if isinstance(wait, Wait)]
    _require_unique(wait_ids, f"{label}.waits", "wait")

    steps = _require_sequence(run.steps, f"{label}.steps", _MAX_STEPS)
    for index, step in enumerate(steps):
        step_label = f"{label}.steps[{index}]"
        validate_run_step(step, **scope, label=step_label)
        assert isinstance(step, RunStep)
        if step.ordinal != index + 1:
            raise ContractSemanticError(
                f"{step_label}: ordinal {step.ordinal} breaks the contiguous 1..N sequence "
                f"(expected {index + 1})"
            )
        if step.wait_id is not None:
            held_by = waits_by_id.get(step.wait_id)
            if held_by is None:
                raise ContractSemanticError(
                    f"{step_label}: wait_id {step.wait_id!r} names no wait of this run"
                )
            if held_by.run_step_id != step.run_step_id:
                raise ContractSemanticError(
                    f"{step_label}: wait {step.wait_id!r} suspends step "
                    f"{held_by.run_step_id!r}, not this one; a step and the wait holding it "
                    "name each other or neither is suspended on the other"
                )
    steps_by_id = {step.run_step_id: step for step in steps if isinstance(step, RunStep)}
    step_ids = [step.run_step_id for step in steps if isinstance(step, RunStep)]
    _require_unique(step_ids, f"{label}.steps", "step")
    attempt_ids = [
        attempt.attempt_id
        for step in steps
        if isinstance(step, RunStep)
        for attempt in step.attempts
    ]
    _require_unique(attempt_ids, f"{label}.steps", "attempt")
    step_of_attempt = {
        attempt.attempt_id: step.run_step_id
        for step in steps
        if isinstance(step, RunStep)
        for attempt in step.attempts
    }

    approvals = _require_sequence(run.approvals, f"{label}.approvals", _MAX_APPROVALS)
    for index, approval in enumerate(approvals):
        approval_label = f"{label}.approvals[{index}]"
        validate_approval(approval, **scope, label=approval_label)
        assert isinstance(approval, Approval)
        requested_on = waits_by_id.get(approval.wait_id)
        if requested_on is None:
            raise ContractSemanticError(
                f"{approval_label}: wait_id {approval.wait_id!r} names no wait of this run"
            )
        if requested_on.kind != "approval":
            raise ContractSemanticError(
                f"{approval_label}: wait {approval.wait_id!r} is a {requested_on.kind!r} wait, "
                "not an approval wait; a decision is requested on a wait that is waiting for "
                "one, and a timer or a signal is not"
            )
    approvals_by_id = {
        approval.approval_id: approval for approval in approvals if isinstance(approval, Approval)
    }
    approval_ids = [approval.approval_id for approval in approvals if isinstance(approval, Approval)]
    _require_unique(approval_ids, f"{label}.approvals", "approval")
    for index, wait in enumerate(waits):
        assert isinstance(wait, Wait)
        suspended = steps_by_id.get(wait.run_step_id)
        if suspended is None:
            raise ContractSemanticError(
                f"{label}.waits[{index}]: run_step_id {wait.run_step_id!r} names no step of "
                "this run"
            )
        if suspended.wait_id != wait.wait_id:
            raise ContractSemanticError(
                f"{label}.waits[{index}]: step {wait.run_step_id!r} is held by "
                f"{suspended.wait_id!r}, not by this wait; a wait and the step it suspends "
                "name each other or neither is suspended on the other"
            )
        if wait.approval_id is not None:
            recorded = approvals_by_id.get(wait.approval_id)
            if recorded is None:
                raise ContractSemanticError(
                    f"{label}.waits[{index}]: approval_id {wait.approval_id!r} names no approval "
                    "of this run"
                )
            if recorded.wait_id != wait.wait_id:
                raise ContractSemanticError(
                    f"{label}.waits[{index}]: approval {wait.approval_id!r} was requested on wait "
                    f"{recorded.wait_id!r}, not on this one; a wait and the approval resolving it "
                    "name each other or neither is paired with the other"
                )

    intents = _require_sequence(run.effect_intents, f"{label}.effect_intents", _MAX_EFFECTS)
    for index, intent in enumerate(intents):
        intent_label = f"{label}.effect_intents[{index}]"
        validate_effect_intent(intent, **scope, grants=grants, label=intent_label)
        assert isinstance(intent, EffectIntent)
        if intent.run_step_id not in step_ids:
            raise ContractSemanticError(
                f"{intent_label}: run_step_id {intent.run_step_id!r} names no step of this run"
            )
        attempted_in = step_of_attempt.get(intent.attempt_id)
        if attempted_in is None:
            raise ContractSemanticError(
                f"{intent_label}: attempt_id {intent.attempt_id!r} names no attempt of this run"
            )
        if attempted_in != intent.run_step_id:
            raise ContractSemanticError(
                f"{intent_label}: attempt {intent.attempt_id!r} belongs to step "
                f"{attempted_in!r}, not to {intent.run_step_id!r}; an effect is attempted "
                "within the step that declared it"
            )
    _require_unique(
        [intent.effect_intent_id for intent in intents if isinstance(intent, EffectIntent)],
        f"{label}.effect_intents",
        "effect intent",
    )

    receipts = _require_sequence(run.effect_receipts, f"{label}.effect_receipts", _MAX_EFFECTS)
    for index, receipt in enumerate(receipts):
        validate_effect_receipt(
            receipt, **scope, intents=intents, label=f"{label}.effect_receipts[{index}]"
        )
    _require_unique(
        [receipt.effect_receipt_id for receipt in receipts if isinstance(receipt, EffectReceipt)],
        f"{label}.effect_receipts",
        "effect receipt",
    )

    settlements = _require_sequence(
        run.effect_settlements, f"{label}.effect_settlements", _MAX_EFFECTS
    )
    for index, settlement in enumerate(settlements):
        validate_effect_settlement(
            settlement,
            **scope,
            intents=intents,
            receipts=receipts,
            label=f"{label}.effect_settlements[{index}]",
        )
    _require_unique(
        [
            settlement.effect_settlement_id
            for settlement in settlements
            if isinstance(settlement, EffectSettlement)
        ],
        f"{label}.effect_settlements",
        "effect settlement",
    )

    validate_runtime_event_stream(run.events, **scope, label=f"{label}.events")
    for index, event in enumerate(run.events):
        _require_known_step(event.run_step_id, step_ids, f"{label}.events[{index}]")

    artifacts = _require_sequence(run.artifacts, f"{label}.artifacts", _MAX_ARTIFACTS)
    for index, artifact in enumerate(artifacts):
        artifact_label = f"{label}.artifacts[{index}]"
        validate_artifact(artifact, **scope, label=artifact_label)
        assert isinstance(artifact, Artifact)
        _require_known_step(artifact.run_step_id, step_ids, artifact_label)
    artifact_ids = [artifact.artifact_id for artifact in artifacts if isinstance(artifact, Artifact)]
    _require_unique(artifact_ids, f"{label}.artifacts", "artifact")

    evidence = _require_sequence(run.evidence, f"{label}.evidence", _MAX_EVIDENCE)
    for index, item in enumerate(evidence):
        item_label = f"{label}.evidence[{index}]"
        validate_evidence_item(item, **scope, label=item_label)
        assert isinstance(item, EvidenceItem)
        _require_known_step(item.run_step_id, step_ids, item_label)
        if item.artifact_id is not None and item.artifact_id not in artifact_ids:
            raise ContractSemanticError(
                f"{item_label}: artifact_id {item.artifact_id!r} names no artifact of this run"
            )
    _require_unique(
        [item.evidence_item_id for item in evidence if isinstance(item, EvidenceItem)],
        f"{label}.evidence",
        "evidence item",
    )

    cleanup = _require_sequence(
        run.cleanup_receipts, f"{label}.cleanup_receipts", _MAX_CLEANUP_RECEIPTS
    )
    for index, receipt in enumerate(cleanup):
        validate_cleanup_receipt(receipt, **scope, label=f"{label}.cleanup_receipts[{index}]")
    _require_unique(
        [receipt.cleanup_receipt_id for receipt in cleanup if isinstance(receipt, CleanupReceipt)],
        f"{label}.cleanup_receipts",
        "cleanup receipt",
    )

    correlations = _require_sequence(run.correlations, f"{label}.correlations", _MAX_CORRELATIONS)
    for index, correlation in enumerate(correlations):
        correlation_label = f"{label}.correlations[{index}]"
        validate_external_reference(
            correlation, workspace_id=expected_workspace, label=correlation_label
        )
        assert isinstance(correlation, ExternalReference)
        if is_authoritative_source(correlation.source_kind):
            raise ContractSemanticError(
                f"{correlation_label}: a run does not correlate to itself; the canonical "
                "identity of this run is its own run_id, and a second authoritative spelling "
                "of it could be joined to the wrong one"
            )

    unreconciled = [
        settlement.effect_settlement_id
        for settlement in settlements
        if isinstance(settlement, EffectSettlement)
        and settlement.outcome == EFFECT_OUTCOME_UNKNOWN
    ]
    if unreconciled and is_terminal_run_status(status):
        raise ContractSemanticError(
            f"{label}: status {status!r} closes the run while {unreconciled} are unreconciled; "
            "uncertainty is not failure, and an unsettled effect keeps the run 'uncertain'"
        )

    if status == RUN_STATUS_CANCELLED:
        discarded = [
            item.evidence_item_id
            for item in evidence
            if isinstance(item, EvidenceItem) and not item.retained
        ]
        if discarded:
            raise ContractSemanticError(
                f"{label}: cancellation discarded evidence {discarded}; cancelling a run stops "
                "the work, never the record"
            )
        if attempt_ids and not evidence:
            raise ContractSemanticError(
                f"{label}: a cancelled run that executed {len(attempt_ids)} attempt(s) retains "
                "no evidence of them"
            )

    if is_terminal_run_status(status):
        if not cleanup:
            raise ContractSemanticError(
                f"{label}: a {status!r} run states no cleanup receipt; cleanup is observable, "
                "not assumed"
            )
        if run.finished_at is None:
            raise ContractSemanticError(f"{label}: a {status!r} run must state finished_at")
        if _parse_timestamp(run.finished_at, f"{label}.finished_at") < created_at:
            raise ContractSemanticError(f"{label}: finished_at precedes created_at")
    elif is_known_run_status(status) and run.finished_at is not None:
        raise ContractSemanticError(f"{label}: a {status!r} run has not finished")


def validate_terminal_run(run: object, *, workspace_id: object, label: str = "run") -> None:
    """Raise unless `run` is a valid run that has actually finished.

    The fail-safe reading of an open vocabulary, as a callable rule. A caller that needs to
    act on a finished run -- to archive it, to release what it held, to report it as done --
    asks here, and a run in a status this build does not recognize is refused rather than
    guessed at. `uncertain` is refused for the same reason: an unreconciled effect is an open
    question, not an outcome.
    """
    validate_run(run, workspace_id=workspace_id, label=label)
    assert isinstance(run, Run)
    if not is_terminal_run_status(run.status):
        known = "" if is_known_run_status(run.status) else " (an unrecognized status)"
        raise ContractSemanticError(
            f"{label}: status {run.status!r}{known} is not a terminal run status, so this run "
            "may not be treated as finished"
        )


# --- logical idempotency -------------------------------------------------------


def classify_run_replay(first: object, replay: object) -> str:
    """Classify `replay` against `first` as a replay, a conflict, or distinct work.

    Stable logical idempotency at run level. The `logical_key` is the identity of the *work*,
    so two admissions carrying the same key are one run and must agree on everything that
    defines what that run is: its workspace, its identifier, and the exact definition and
    version it executes. Same key with any of those different is an `idempotency_conflict`,
    reusing the classification the application wire already publishes rather than inventing a
    second vocabulary for the same idea. Different keys are simply different work.
    """
    _require_type(first, Run, "first")
    _require_type(replay, Run, "replay")
    assert isinstance(first, Run)
    assert isinstance(replay, Run)
    if _require_str(first.logical_key, "first.logical_key") != _require_str(
        replay.logical_key, "replay.logical_key"
    ):
        return IDEMPOTENCY_DISTINCT
    _validate_run_definition_ref(first.definition, "first.definition")
    _validate_run_definition_ref(replay.definition, "replay.definition")
    same = (
        first.workspace_id == replay.workspace_id
        and first.run_id == replay.run_id
        and first.definition == replay.definition
    )
    return IDEMPOTENCY_REPLAY if same else IDEMPOTENCY_CONFLICT


def classify_effect_replay(first: object, replay: object) -> str:
    """Classify `replay` against `first` as a replay, a conflict, or distinct work.

    The same rule one level down, and the one that makes an at-least-once delivery safe: the
    same `idempotency_key` over the same `request_digest`, in the same workspace and against
    the same capability, is one effect however many times it is delivered. The same key over
    a different digest is an `idempotency_conflict` -- two different effects claiming one
    identity -- and must never be settled as a replay of each other.
    """
    _require_type(first, EffectIntent, "first")
    _require_type(replay, EffectIntent, "replay")
    assert isinstance(first, EffectIntent)
    assert isinstance(replay, EffectIntent)
    if _validate_idempotency_key(
        first.idempotency_key, "first.idempotency_key"
    ) != _validate_idempotency_key(replay.idempotency_key, "replay.idempotency_key"):
        return IDEMPOTENCY_DISTINCT
    same = (
        first.workspace_id == replay.workspace_id
        and first.capability_id == replay.capability_id
        and _validate_content_checksum(first.request_digest, "first.request_digest")
        == _validate_content_checksum(replay.request_digest, "replay.request_digest")
    )
    return IDEMPOTENCY_REPLAY if same else IDEMPOTENCY_CONFLICT


# --- ResolveWait ---------------------------------------------------------------


def validate_resolve_wait_shape(command: object, label: str = "command") -> None:
    """Raise unless `command` is a shape-valid `ResolveWait`.

    The document-only half, published under its own name rather than reached by omitting the
    trusted `wait`: an argument that can be left out is a rule that can be skipped. What it
    can check without the wait is that the command is internally coherent -- an approval
    decision names its approval, and nothing else does, because a timer expiry carrying an
    approver is describing a decision nobody made.
    """
    _require_type(command, ResolveWait, label)
    assert isinstance(command, ResolveWait)
    _validate_workspace_id(command.workspace_id, f"{label}.workspace_id")
    _validate_identifier(command.run_id, f"{label}.run_id")
    _validate_identifier(command.wait_id, f"{label}.wait_id")
    resolution = _require_member(
        command.resolution, WAIT_RESOLUTIONS, f"{label}.resolution", "WaitResolution"
    )
    _validate_content_checksum(command.resume_digest, f"{label}.resume_digest")
    _parse_timestamp(command.requested_at, f"{label}.requested_at")
    _validate_open_code(command.reason, f"{label}.reason")
    approval_decision = resolution == _WAIT_RESOLUTION_APPROVAL_DECISION
    if approval_decision and command.approval_id is None:
        raise ContractSemanticError(
            f"{label}: an approval decision must name the approval carrying it"
        )
    if not approval_decision and command.approval_id is not None:
        raise ContractSemanticError(f"{label}: a {resolution!r} resolution names no approval")
    if command.approval_id is not None:
        _validate_identifier(command.approval_id, f"{label}.approval_id")


def validate_resolve_wait(
    command: object,
    *,
    wait: object,
    approval: object,
    run_status: object,
    label: str = "command",
) -> None:
    """Raise unless `command` may resolve `wait` on a run in `run_status`.

    `wait`, `approval` and `run_status` are trusted context, not fields of the command:
    whether a resolution is legal is a fact about the wait it claims to resolve and the
    decision it claims to carry, and a command that could answer either question about
    itself would be authorizing itself. All three are required keywords with no default,
    because a context argument a caller may omit is a rule a caller may skip.

    Four rules about the wait. The command and the wait must be the same wait, in the same
    run and workspace. The wait must still be pending -- a resolved, expired or cancelled
    wait is not resolvable again, and accepting a second resolution would resume a run twice.
    The resolution must match the wait's kind, beside `cancelled`, which releases any wait.
    And the resume digest must be the one the wait published, so a resolution proves it is
    resuming the state that was suspended rather than some later one.

    One rule about the decision, and it is the one that makes `approval_id` mean something.
    An `approval_decision` is checked against the `Approval` actually recorded: it is passed
    as `approval`, it must be an approval of *this* wait in this run and workspace, it must
    be the approval the command names, and it must itself be a well-formed *decided*
    approval. Both recorded decisions terminate the approval and so resolve the wait:
    `approved` and `rejected` are the two answers an approver can give, and a rejected one
    resumes the run to act on the refusal. What a rejection is not is authorization -- it
    creates no grant and licences no effect, which is a question for
    :func:`validate_capability_grant` and :func:`validate_effect_intent`, not for resolving
    the wait. Only an approval carrying *no* decision is refused here: a command quoting an
    approval id nobody decided, or one decided for another run's wait, resumes a run on a
    decision that was never made. Every other resolution -- a signal, a timer expiry, a
    cancellation -- carries no decision at all, so `approval` must be `None`; supplying one
    is a contradiction rather than surplus context, and is refused as such.

    The run must be `waiting`. An unrecognized run status is refused rather than assumed to
    be waiting, which is the fail-safe reading of an open vocabulary: this is not `job.retry`
    and it does not requeue anything, so there is no recovery to fall back on.
    """
    validate_resolve_wait_shape(command, label)
    assert isinstance(command, ResolveWait)
    _require_type(wait, Wait, "wait")
    assert isinstance(wait, Wait)
    if not is_waiting_run_status(run_status):
        raise ContractSemanticError(
            f"{label}: a run in status {run_status!r} is not waiting, so it has no wait to "
            "resolve"
        )
    _require_scoped(
        command.workspace_id,
        _validate_workspace_id(wait.workspace_id, "wait.workspace_id"),
        f"{label}.workspace_id",
        "wait's workspace",
    )
    _require_scoped(
        command.run_id, _validate_identifier(wait.run_id, "wait.run_id"), f"{label}.run_id", "run"
    )
    _require_scoped(
        command.wait_id,
        _validate_identifier(wait.wait_id, "wait.wait_id"),
        f"{label}.wait_id",
        "wait",
    )
    if _require_str(wait.status, "wait.status") != WAIT_STATUS_PENDING:
        raise ContractSemanticError(
            f"{label}: wait {wait.wait_id!r} is {wait.status!r} and has already stopped being "
            "pending; a wait is resolved exactly once"
        )
    kind = _require_member(wait.kind, WAIT_KINDS, "wait.kind", "WaitKind")
    if command.resolution != WAIT_RESOLUTION_CANCELLED:
        expected = WAIT_RESOLUTION_FOR_KIND[kind]
        if command.resolution != expected:
            raise ContractSemanticError(
                f"{label}: resolution {command.resolution!r} does not resolve a {kind!r} wait "
                f"(expected {expected!r} or {WAIT_RESOLUTION_CANCELLED!r})"
            )
    if command.resume_digest != _validate_content_checksum(
        wait.resume_digest, "wait.resume_digest"
    ):
        raise ContractSemanticError(
            f"{label}: resume_digest does not match the digest wait {wait.wait_id!r} published; "
            "a resolution resumes the state that was suspended, not a later one"
        )
    if command.resolution != _WAIT_RESOLUTION_APPROVAL_DECISION:
        if approval is not None:
            raise ContractSemanticError(
                f"{label}: a {command.resolution!r} resolution carries no approval, so no "
                "approval context may be supplied for it"
            )
        return
    _require_type(approval, Approval, "approval")
    assert isinstance(approval, Approval)
    validate_approval(
        approval, run_id=wait.run_id, workspace_id=wait.workspace_id, label="approval"
    )
    _require_scoped(approval.wait_id, wait.wait_id, "approval.wait_id", "wait")
    _require_scoped(
        command.approval_id,
        _require_str(approval.approval_id, "approval.approval_id"),
        f"{label}.approval_id",
        "recorded approval",
    )
    if approval.decision is None:
        raise ContractSemanticError(
            f"{label}: approval {approval.approval_id!r} records no decision; a wait is "
            f"resolved by a decision somebody made, either of {APPROVAL_DECISIONS}"
        )


def decode_resolve_wait(payload: object, path: str = "ResolveWait") -> ResolveWait:
    """Decode `payload` into a shape-valid `ResolveWait`.

    Shape only: whether the command may resolve anything is a question about the wait it
    names, which :func:`validate_resolve_wait` answers with that wait in hand.
    """
    command = ResolveWait.from_wire(payload, path)
    validate_resolve_wait_shape(command, path)
    return command
