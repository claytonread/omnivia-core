"""Pure semantic validation for the T-0688 IP-11 governed execution records.

Three questions, one module, and no dispatch of any kind:

- **who was asked, and what came back** -- `HumanInteractionRequest` and
  `HumanInteractionOutcomeSubmission`. A request names the interaction, its owner and the
  policy that decides who may answer; a submission carries one answer against one exact
  request version. Neither record settles anything: holding a well-formed submission is
  never the same as being allowed to answer, which is why nothing here reads or grants
  Permission.
- **how much autonomy a Run has** -- `AutonomyProfile` and `EffectiveAutonomyProfile`. A
  profile is one *source's* contribution; the effective profile is the frozen resolution of
  all seven sources. Both only describe a bound; neither confers one.
- **who currently owns an execution plane** -- `ExecutionPlaneOwnerLease`,
  `PlaneDispatchEnvelope`, `PlaneCompletionReport`, `ListenerDeliveryDecision` and
  `ResourceReconciliationLease`. Each carries the fencing data that makes stale authority
  detectable; none of them detects it, because that is a runtime decision over live state
  and this module holds no state.

Every record is an exact closed mapping: an unknown member and a missing member are both
refused, and each member is checked against a Core scalar helper or a closed vocabulary
declared here. The three vocabularies this lane shares with the IP-10 Component declaration
-- the five execution planes and five effect classes -- are imported from
:mod:`semantics_component` rather than respelled, so those members mean the same thing on
both sides of the seam.

Standard library only. Nothing here may depend on runtime, storage, HTTP, MCP, CLI,
Platform, Dev, or a validation framework, and nothing here is generated: there is no schema
generator behind this module and no new dependency.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Final

from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    is_content_checksum,
    is_correlation_id,
    is_idempotency_key,
    is_identifier,
    is_release_version,
    is_request_id,
    is_timestamp,
    is_workspace_id,
)
from omnivia_core.contracts.v1.semantics_component import (
    EFFECT_CLASSES,
    EXECUTION_PLANES,
)

__all__ = [
    "AUTONOMY_PROFILE_REFUSED",
    "AUTONOMY_SOURCE_KINDS",
    "EXECUTION_PLANE_LATE_RESULT",
    "EXECUTION_PLANE_STALE_AUTHORITY",
    "GOVERNED_EXECUTION_RECORD_VALIDATORS",
    "HUMAN_INTERACTION_KINDS",
    "HUMAN_INTERACTION_OUTCOME_CONFLICT",
    "HUMAN_INTERACTION_OWNER_KINDS",
    "HUMAN_TASK_INTERACTION_KINDS",
    "LISTENER_DELIVERY_DISPOSITIONS",
    "LISTENER_SATURATED",
    "NON_HUMAN_TASK_INTERACTION_KINDS",
    "PLANE_COMPLETION_OUTCOMES",
    "PLANE_ROLES",
    "GovernedExecutionRecordValidator",
    "validate_autonomy_profile",
    "validate_effective_autonomy_profile",
    "validate_execution_plane_owner_lease",
    "validate_governed_execution_record",
    "validate_human_interaction_outcome_submission",
    "validate_human_interaction_request",
    "validate_listener_delivery_decision",
    "validate_plane_completion_report",
    "validate_plane_dispatch_envelope",
    "validate_resource_reconciliation_lease",
]

GovernedExecutionRecordValidator = Callable[[object], None]

#: The four refusal codes this lane names. They are the vocabulary a Runtime refuses *with*;
#: this module never raises them, because deciding a conflict, a bound, stale authority or
#: saturation all require live state that a pure validator does not hold.
HUMAN_INTERACTION_OUTCOME_CONFLICT: Final = "HUMAN_INTERACTION_OUTCOME_CONFLICT"
AUTONOMY_PROFILE_REFUSED: Final = "AUTONOMY_PROFILE_REFUSED"
EXECUTION_PLANE_STALE_AUTHORITY: Final = "EXECUTION_PLANE_STALE_AUTHORITY"
EXECUTION_PLANE_LATE_RESULT: Final = "EXECUTION_PLANE_LATE_RESULT"
LISTENER_SATURATED: Final = "LISTENER_SATURATED"

HUMAN_INTERACTION_KINDS: Final[tuple[str, ...]] = (
    "input",
    "approval",
    "review",
    "handoff",
)

#: `input` and `approval` remain owned by Human Task Service. `review` and `handoff` retain
#: their respective authoritative owner kinds; settlement never transfers ownership to the
#: responder.
HUMAN_TASK_INTERACTION_KINDS: Final[tuple[str, ...]] = ("input", "approval")
NON_HUMAN_TASK_INTERACTION_KINDS: Final[tuple[str, ...]] = ("review", "handoff")

HUMAN_INTERACTION_OWNER_KINDS: Final[tuple[str, ...]] = (
    "humanTask",
    "review",
    "handoff",
)

#: The seven autonomy sources, in the exact order an `EffectiveAutonomyProfile` must record
#: their contributions. The order is part of the contract: a resolution that reordered its
#: provenance would digest differently while describing the same inputs.
AUTONOMY_SOURCE_KINDS: Final[tuple[str, ...]] = (
    "componentImplementation",
    "workflowSettings",
    "policy",
    "resourceScope",
    "modelScope",
    "knowledgeScope",
    "approvalLimits",
)

#: The five plane roles are the IP-10 declaration's execution planes, not a second list.
PLANE_ROLES: Final[tuple[str, ...]] = EXECUTION_PLANES

PLANE_COMPLETION_OUTCOMES: Final[tuple[str, ...]] = ("succeeded", "failed", "cancelled")
LISTENER_DELIVERY_DISPOSITIONS: Final[tuple[str, ...]] = (
    "admitted",
    "duplicateAcknowledged",
    "refusedSaturated",
    "shed",
)

_HUMAN_INTERACTION_REQUEST_FIELDS: Final = frozenset(
    {
        "requestSchemaVersion",
        "requestId",
        "correlationId",
        "idempotencyKey",
        "requestVersion",
        "interactionKind",
        "ownerKind",
        "ownerSubjectRef",
        "ownerRevision",
        "workspace",
        "responseSchemaRef",
        "presentationRef",
        "eligibilityPolicyRef",
        "requestedAt",
        "expiresAt",
        "runRef",
        "waitRef",
    }
)
_HUMAN_INTERACTION_REQUEST_OPTIONAL: Final = frozenset(
    {"expiresAt", "runRef", "waitRef"}
)

_OUTCOME_SUBMISSION_FIELDS: Final = frozenset(
    {
        "submissionSchemaVersion",
        "requestId",
        "requestVersion",
        "actorRef",
        "deviceSessionRef",
        "responseDigest",
        "responseRef",
        "reason",
        "idempotencyKey",
        "submittedAt",
    }
)
_OUTCOME_SUBMISSION_OPTIONAL: Final = frozenset({"reason"})

_AUTONOMY_BOUND_FIELDS: Final[tuple[str, ...]] = (
    "timeBoundMs",
    "tokenBound",
    "costBound",
    "stepBound",
)
_AUTONOMY_SCOPE_FIELDS: Final[tuple[str, ...]] = (
    "allowedActionScopes",
    "allowedResourceScopes",
    "allowedModelScopes",
    "allowedKnowledgeScopes",
)

_AUTONOMY_PROFILE_FIELDS: Final = frozenset(
    {
        "profileSchemaVersion",
        "sourceKind",
        "sourceRef",
        "snapshotRef",
        "snapshotDigest",
        "approvalRequiredEffectClasses",
        *_AUTONOMY_SCOPE_FIELDS,
        *_AUTONOMY_BOUND_FIELDS,
    }
)
_EFFECTIVE_AUTONOMY_PROFILE_FIELDS: Final = frozenset(
    {
        "resolutionSchemaVersion",
        "effectiveActionScopes",
        "effectiveResourceScopes",
        "effectiveModelScopes",
        "effectiveKnowledgeScopes",
        "effectiveApprovalRequiredEffectClasses",
        "effectiveTimeBoundMs",
        "effectiveTokenBound",
        "effectiveCostBound",
        "effectiveStepBound",
        "sourceContributions",
        "resolutionDigest",
    }
)
_SOURCE_CONTRIBUTION_FIELDS: Final = frozenset(
    {"sourceKind", "snapshotRef", "snapshotDigest"}
)

_OWNER_LEASE_FIELDS: Final = frozenset(
    {
        "leaseSchemaVersion",
        "subjectRef",
        "planeRole",
        "ownerActorRef",
        "fencingToken",
        "acquiredAt",
        "expiresAt",
        "previousFencingToken",
    }
)
_DISPATCH_ENVELOPE_FIELDS: Final = frozenset(
    {
        "envelopeSchemaVersion",
        "dispatchId",
        "subjectRef",
        "fencingToken",
        "boundedExecutionPackageRef",
        "dispatchedAt",
        "dispatchedBy",
    }
)
_COMPLETION_REPORT_FIELDS: Final = frozenset(
    {
        "reportSchemaVersion",
        "dispatchId",
        "subjectRef",
        "fencingToken",
        "completedAt",
        "outcomeClass",
        "resultRef",
        "failureRef",
        "evidenceRef",
    }
)
_LISTENER_DECISION_FIELDS: Final = frozenset(
    {
        "decisionSchemaVersion",
        "deliveryId",
        "sourceRef",
        "decisionKind",
        "recordedAt",
        "admittedToRef",
    }
)
_RECONCILIATION_LEASE_FIELDS: Final = frozenset(
    {
        "leaseSchemaVersion",
        "resourceRef",
        "ownerActorRef",
        "fencingToken",
        "acquiredAt",
        "expiresAt",
        "desiredStateDigest",
        "observedStateDigest",
    }
)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContractSemanticError(f"{label}: expected a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ContractSemanticError(f"{label}: field names must be strings")
    return value


def _exact_fields(
    fields: Mapping[str, object],
    allowed: frozenset[str],
    label: str,
    *,
    optional: frozenset[str] = frozenset(),
) -> None:
    """Refuse an unknown member and a missing required member, in that order."""
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise ContractSemanticError(f"{label}: unknown fields {unknown!r}")
    missing = sorted((allowed - optional) - set(fields))
    if missing:
        raise ContractSemanticError(f"{label}: missing {missing[0]}")


def _present(fields: Mapping[str, object], key: str, label: str) -> object:
    if key not in fields:
        raise ContractSemanticError(f"{label}: missing {key}")
    return fields[key]


def _identifier(fields: Mapping[str, object], key: str, label: str) -> str:
    value = _present(fields, key, label)
    if not is_identifier(value):
        raise ContractSemanticError(f"{label}: {key} is not a well-formed Identifier")
    assert isinstance(value, str)
    return value


def _digest(fields: Mapping[str, object], key: str, label: str) -> str:
    value = _present(fields, key, label)
    if not is_content_checksum(value):
        raise ContractSemanticError(f"{label}: {key} is not a well-formed Digest")
    assert isinstance(value, str)
    return value


def _timestamp(fields: Mapping[str, object], key: str, label: str) -> str:
    value = _present(fields, key, label)
    if not is_timestamp(value):
        raise ContractSemanticError(f"{label}: {key} is not a well-formed Timestamp")
    assert isinstance(value, str)
    return value


def _release_version(fields: Mapping[str, object], key: str, label: str) -> str:
    value = _present(fields, key, label)
    if not is_release_version(value):
        raise ContractSemanticError(
            f"{label}: {key} is not a well-formed ReleaseVersion"
        )
    assert isinstance(value, str)
    return value


def _member(
    fields: Mapping[str, object], key: str, label: str, allowed: tuple[str, ...]
) -> str:
    value = _present(fields, key, label)
    if not isinstance(value, str) or value not in allowed:
        raise ContractSemanticError(f"{label}: {key} is not one of {allowed!r}")
    return value


def _boolean(fields: Mapping[str, object], key: str, label: str) -> bool:
    value = _present(fields, key, label)
    if not isinstance(value, bool):
        raise ContractSemanticError(f"{label}: {key} is not a boolean")
    return value


def _non_empty_string(fields: Mapping[str, object], key: str, label: str) -> str:
    value = _present(fields, key, label)
    if isinstance(value, bool) or not isinstance(value, str) or not value:
        raise ContractSemanticError(f"{label}: {key} is not a non-empty string")
    return value


def _non_negative_int(fields: Mapping[str, object], key: str, label: str) -> int:
    value = _present(fields, key, label)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractSemanticError(f"{label}: {key} is not a non-negative integer")
    return value


def _positive_int(fields: Mapping[str, object], key: str, label: str) -> int:
    value = _present(fields, key, label)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractSemanticError(f"{label}: {key} is not a positive integer")
    return value


def _non_negative_number(
    fields: Mapping[str, object], key: str, label: str
) -> int | float:
    value = _present(fields, key, label)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ContractSemanticError(f"{label}: {key} is not a non-negative number")
    return value


def _reference(
    fields: Mapping[str, object], key: str, label: str
) -> Mapping[str, object]:
    """A reference is a non-empty mapping: an exact pointer, never a bare string."""
    reference = _mapping(_present(fields, key, label), f"{label}.{key}")
    if not reference:
        raise ContractSemanticError(f"{label}: {key} must not be empty")
    return reference


def _canonical_unique_members(
    fields: Mapping[str, object],
    key: str,
    label: str,
    allowed: tuple[str, ...] | None,
) -> tuple[str, ...]:
    """Require an array that is sorted ascending and free of duplicates.

    Canonical order is what makes two independently produced scope lists comparable and
    digestible; a merely set-equal list in another order is refused rather than sorted, so
    the producer -- not this validator -- is the one that fixed the order.
    """
    value = _present(fields, key, label)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractSemanticError(f"{label}: {key} is not an array")
    entries: list[str] = []
    for index, entry in enumerate(value):
        if allowed is None:
            if not is_identifier(entry):
                raise ContractSemanticError(
                    f"{label}: {key}[{index}] is not a well-formed Identifier"
                )
        elif not isinstance(entry, str) or entry not in allowed:
            raise ContractSemanticError(
                f"{label}: {key}[{index}] is not one of {allowed!r}"
            )
        assert isinstance(entry, str)
        entries.append(entry)
    if len(set(entries)) != len(entries):
        raise ContractSemanticError(f"{label}: {key} must not repeat an entry")
    if list(entries) != sorted(entries):
        raise ContractSemanticError(f"{label}: {key} is not in canonical order")
    return tuple(entries)


def _optional_non_negative_int(
    fields: Mapping[str, object], key: str, label: str
) -> int | None:
    if key not in fields:
        return None
    return _non_negative_int(fields, key, label)


def _ordered_after(
    fields: Mapping[str, object], earlier: str, later: str, label: str
) -> None:
    """Require `later` to name a strictly later instant than `earlier`.

    Both values are already canonical UTC `Timestamp` strings. Normalise omitted fractional
    seconds before comparing so ``...00Z`` orders before ``...00.1Z``.
    """

    def sortable(value: object) -> str:
        text = str(value)
        whole, separator, fraction = text[:-1].partition(".")
        return whole + "." + (fraction if separator else "").ljust(9, "0")

    if sortable(fields[later]) <= sortable(fields[earlier]):
        raise ContractSemanticError(f"{label}: {later} must be later than {earlier}")


def _conditional_member(
    fields: Mapping[str, object],
    key: str,
    label: str,
    *,
    required: bool,
    condition: str,
) -> None:
    """Require a member exactly when the record's own discriminator calls for it."""
    if required and key not in fields:
        raise ContractSemanticError(f"{label}: {key} is required for {condition}")
    if not required and key in fields:
        raise ContractSemanticError(f"{label}: {key} is permitted only for {condition}")


def validate_human_interaction_request(record: object) -> None:
    """Validate the exact closed `HumanInteractionRequest` record.

    Identity is three separate things and all three are required: `requestId` names the
    request, `correlationId` ties it to the work that raised it, and `idempotencyKey` is what
    makes a retried *raise* the same raise. `requestVersion` and `ownerRevision` are exact
    non-negative ordinals, never floating pins -- a submission is checked against the
    version, and the owner is pinned to one exact revision of the subject it names.

    Two conditional rules. The interaction kind fixes the retained authoritative owner kind:
    `input` and `approval` belong to Human Task Service, while `review` and `handoff` keep
    their existing owner kinds. A `waitRef` without a `runRef` is refused: a wait belongs to
    a run, so naming one without the other describes nothing.

    Well-formedness only. This grants no eligibility and confers no Permission.
    """
    label = "HumanInteractionRequest"
    fields = _mapping(record, label)
    _exact_fields(
        fields,
        _HUMAN_INTERACTION_REQUEST_FIELDS,
        label,
        optional=_HUMAN_INTERACTION_REQUEST_OPTIONAL,
    )

    _release_version(fields, "requestSchemaVersion", label)

    request_id = _present(fields, "requestId", label)
    if not is_request_id(request_id):
        raise ContractSemanticError(
            f"{label}: requestId is not a well-formed RequestId"
        )
    correlation_id = _present(fields, "correlationId", label)
    if not is_correlation_id(correlation_id):
        raise ContractSemanticError(
            f"{label}: correlationId is not a well-formed CorrelationId"
        )
    idempotency_key = _present(fields, "idempotencyKey", label)
    if not is_idempotency_key(idempotency_key):
        raise ContractSemanticError(
            f"{label}: idempotencyKey is not a well-formed IdempotencyKey"
        )
    _positive_int(fields, "requestVersion", label)

    interaction_kind = _member(
        fields, "interactionKind", label, HUMAN_INTERACTION_KINDS
    )
    owner_kind = _member(fields, "ownerKind", label, HUMAN_INTERACTION_OWNER_KINDS)
    if interaction_kind in HUMAN_TASK_INTERACTION_KINDS and owner_kind != "humanTask":
        raise ContractSemanticError(
            f"{label}: ownerKind must be 'humanTask' for an {interaction_kind} interaction"
        )
    if (
        interaction_kind in NON_HUMAN_TASK_INTERACTION_KINDS
        and owner_kind != interaction_kind
    ):
        raise ContractSemanticError(
            f"{label}: ownerKind must be {interaction_kind!r} for a {interaction_kind} interaction"
        )
    _reference(fields, "ownerSubjectRef", label)
    _reference(fields, "ownerRevision", label)

    workspace = _present(fields, "workspace", label)
    if not is_workspace_id(workspace):
        raise ContractSemanticError(
            f"{label}: workspace is not a well-formed WorkspaceId"
        )

    _reference(fields, "responseSchemaRef", label)
    _reference(fields, "presentationRef", label)
    _reference(fields, "eligibilityPolicyRef", label)

    _timestamp(fields, "requestedAt", label)
    if "expiresAt" in fields:
        _timestamp(fields, "expiresAt", label)
        _ordered_after(fields, "requestedAt", "expiresAt", label)
    if "runRef" in fields:
        _reference(fields, "runRef", label)
    if "waitRef" in fields:
        _reference(fields, "waitRef", label)
        if "runRef" not in fields:
            raise ContractSemanticError(f"{label}: waitRef requires runRef")


def validate_human_interaction_outcome_submission(record: object) -> None:
    """Validate the exact closed `HumanInteractionOutcomeSubmission` record.

    A submission names the exact request *version* it answers, so a submission raised against
    a superseded request is detectable rather than silently applied. It carries both the
    responding actor and the device session the response arrived on, because "who answered"
    and "from where" are two separate facts and a settlement needs both. The response itself
    is carried by digest and by reference: the digest is what makes a replay identical, the
    reference is where the payload lives. `reason` is the only optional member.
    """
    label = "HumanInteractionOutcomeSubmission"
    fields = _mapping(record, label)
    _exact_fields(
        fields,
        _OUTCOME_SUBMISSION_FIELDS,
        label,
        optional=_OUTCOME_SUBMISSION_OPTIONAL,
    )

    _release_version(fields, "submissionSchemaVersion", label)
    request_id = _present(fields, "requestId", label)
    if not is_request_id(request_id):
        raise ContractSemanticError(
            f"{label}: requestId is not a well-formed RequestId"
        )
    _positive_int(fields, "requestVersion", label)
    _reference(fields, "actorRef", label)
    _reference(fields, "deviceSessionRef", label)
    _digest(fields, "responseDigest", label)
    _reference(fields, "responseRef", label)
    if "reason" in fields:
        _non_empty_string(fields, "reason", label)
    idempotency_key = _present(fields, "idempotencyKey", label)
    if not is_idempotency_key(idempotency_key):
        raise ContractSemanticError(
            f"{label}: idempotencyKey is not a well-formed IdempotencyKey"
        )
    _timestamp(fields, "submittedAt", label)


def _validate_autonomy_dimensions(fields: Mapping[str, object], label: str) -> None:
    """The five list dimensions and four numeric bounds every autonomy record carries."""
    for key in _AUTONOMY_SCOPE_FIELDS:
        _canonical_unique_members(fields, key, label, None)
    _canonical_unique_members(
        fields, "approvalRequiredEffectClasses", label, EFFECT_CLASSES
    )
    for key in _AUTONOMY_BOUND_FIELDS:
        if key not in fields:
            continue
        if key == "costBound":
            _non_negative_number(fields, key, label)
        else:
            _non_negative_int(fields, key, label)


def validate_autonomy_profile(record: object) -> None:
    """Validate the exact closed `AutonomyProfile` -- one source's contribution.

    A profile names exactly one of the seven source kinds and pins that source by exact
    reference, exact snapshot reference and snapshot digest, so the contribution can be
    reproduced rather than merely attributed. Its four scope dimensions and its
    approval-required effect classes are canonically ordered and duplicate-free; the four
    numeric bounds are optional, and absent means "this source declares no bound on that
    dimension", which is not the same as declaring zero.

    An empty scope list is a legitimate declaration and is accepted here: a source that
    narrows a dimension to nothing is well-formed, and refusing the resulting empty
    intersection is a resolution decision, not a shape one.
    """
    label = "AutonomyProfile"
    fields = _mapping(record, label)
    _exact_fields(
        fields,
        _AUTONOMY_PROFILE_FIELDS,
        label,
        optional=frozenset(_AUTONOMY_BOUND_FIELDS),
    )
    _release_version(fields, "profileSchemaVersion", label)
    _member(fields, "sourceKind", label, AUTONOMY_SOURCE_KINDS)
    _reference(fields, "sourceRef", label)
    _reference(fields, "snapshotRef", label)
    _digest(fields, "snapshotDigest", label)
    _validate_autonomy_dimensions(fields, label)


def validate_effective_autonomy_profile(record: object) -> None:
    """Validate the exact closed `EffectiveAutonomyProfile` -- the frozen resolution.

    The effective profile carries the same nine dimensions as a source profile plus the two
    things that make it a *resolution*: `sourceContributions`, which must name all seven
    source kinds exactly once and in exactly `AUTONOMY_SOURCE_KINDS` order, and
    `resolutionDigest`, which freezes the result. Requiring all seven -- with an explicit
    bounded default standing in for a source that contributed nothing -- is what stops a
    resolution from being silently wider because a source was simply never consulted.

    The profile records a bound. It does not confer one, and it never carries Permission.
    """
    label = "EffectiveAutonomyProfile"
    fields = _mapping(record, label)
    effective_bounds = frozenset(
        {
            "effectiveTimeBoundMs",
            "effectiveTokenBound",
            "effectiveCostBound",
            "effectiveStepBound",
        }
    )
    _exact_fields(
        fields, _EFFECTIVE_AUTONOMY_PROFILE_FIELDS, label, optional=effective_bounds
    )
    _release_version(fields, "resolutionSchemaVersion", label)
    for key in (
        "effectiveActionScopes",
        "effectiveResourceScopes",
        "effectiveModelScopes",
        "effectiveKnowledgeScopes",
    ):
        _canonical_unique_members(fields, key, label, None)
    _canonical_unique_members(
        fields, "effectiveApprovalRequiredEffectClasses", label, EFFECT_CLASSES
    )
    for key in effective_bounds:
        if key not in fields:
            continue
        if key == "effectiveCostBound":
            _non_negative_number(fields, key, label)
        else:
            _non_negative_int(fields, key, label)

    contributions = _present(fields, "sourceContributions", label)
    if isinstance(contributions, (str, bytes)) or not isinstance(
        contributions, Sequence
    ):
        raise ContractSemanticError(f"{label}: sourceContributions is not an array")
    if len(contributions) != len(AUTONOMY_SOURCE_KINDS):
        raise ContractSemanticError(
            f"{label}: sourceContributions must carry all {len(AUTONOMY_SOURCE_KINDS)} sources"
        )
    for index, (entry, expected) in enumerate(
        zip(contributions, AUTONOMY_SOURCE_KINDS, strict=True)
    ):
        entry_label = f"{label}.sourceContributions[{index}]"
        entry_fields = _mapping(entry, entry_label)
        _exact_fields(entry_fields, _SOURCE_CONTRIBUTION_FIELDS, entry_label)
        if entry_fields.get("sourceKind") != expected:
            raise ContractSemanticError(
                f"{entry_label}: sourceKind must be {expected!r} in canonical source order"
            )
        _reference(entry_fields, "snapshotRef", entry_label)
        _digest(entry_fields, "snapshotDigest", entry_label)

    _digest(fields, "resolutionDigest", label)


def validate_execution_plane_owner_lease(record: object) -> None:
    """Validate the exact closed `ExecutionPlaneOwnerLease`.

    A lease is one plane role's ownership of one scope for one bounded window, carrying the
    fencing token that makes a later writer distinguishable from an earlier one. The token
    is a positive integer: zero is not a lease, it is the absence of one.

    `previousFencingToken` is present when the record represents a takeover, and when present
    must be strictly lower than the new token. Takeover eligibility itself (expired or revoked
    predecessor) is a live-state decision and is therefore enforced by the runtime oracle.
    """
    label = "ExecutionPlaneOwnerLease"
    fields = _mapping(record, label)
    _exact_fields(
        fields, _OWNER_LEASE_FIELDS, label, optional=frozenset({"previousFencingToken"})
    )
    _release_version(fields, "leaseSchemaVersion", label)
    _reference(fields, "subjectRef", label)
    _member(fields, "planeRole", label, PLANE_ROLES)
    _reference(fields, "ownerActorRef", label)
    token = _positive_int(fields, "fencingToken", label)
    _timestamp(fields, "acquiredAt", label)
    _timestamp(fields, "expiresAt", label)
    _ordered_after(fields, "acquiredAt", "expiresAt", label)
    if "previousFencingToken" in fields:
        previous = _positive_int(fields, "previousFencingToken", label)
        if previous >= token:
            raise ContractSemanticError(
                f"{label}: fencingToken must be greater than previousFencingToken"
            )


def validate_plane_dispatch_envelope(record: object) -> None:
    """Validate the exact closed `PlaneDispatchEnvelope`.

    An envelope names the subject and fencing token it was sent under, so a receiver can
    refuse a dispatch from superseded authority. The bounded execution package travels by
    exact reference and the dispatch records both its instant and issuing actor.
    """
    label = "PlaneDispatchEnvelope"
    fields = _mapping(record, label)
    _exact_fields(fields, _DISPATCH_ENVELOPE_FIELDS, label)
    _release_version(fields, "envelopeSchemaVersion", label)
    _identifier(fields, "dispatchId", label)
    _reference(fields, "subjectRef", label)
    _positive_int(fields, "fencingToken", label)
    _reference(fields, "boundedExecutionPackageRef", label)
    _timestamp(fields, "dispatchedAt", label)
    _reference(fields, "dispatchedBy", label)


def validate_plane_completion_report(record: object) -> None:
    """Validate the exact closed `PlaneCompletionReport`.

    `outcomeClass` is the discriminator: a `succeeded` report carries a `resultRef` and no
    `failureRef`, a `failed` report the reverse, and a `cancelled` report neither -- a
    cancellation has no result to digest and no failure to reference.

    Every report carries `evidenceRef`; the referenced Evidence records whether the report was
    accepted or treated as late. The contract record never duplicates that disposition.
    """
    label = "PlaneCompletionReport"
    fields = _mapping(record, label)
    optional = frozenset({"resultRef", "failureRef"})
    _exact_fields(fields, _COMPLETION_REPORT_FIELDS, label, optional=optional)
    _release_version(fields, "reportSchemaVersion", label)
    _identifier(fields, "dispatchId", label)
    _reference(fields, "subjectRef", label)
    _positive_int(fields, "fencingToken", label)
    _timestamp(fields, "completedAt", label)
    outcome = _member(fields, "outcomeClass", label, PLANE_COMPLETION_OUTCOMES)
    _conditional_member(
        fields,
        "resultRef",
        label,
        required=outcome == "succeeded",
        condition="a succeeded outcome",
    )
    _conditional_member(
        fields,
        "failureRef",
        label,
        required=outcome == "failed",
        condition="a failed outcome",
    )
    if outcome == "succeeded":
        _reference(fields, "resultRef", label)
    if outcome == "failed":
        _reference(fields, "failureRef", label)
    _reference(fields, "evidenceRef", label)


def validate_listener_delivery_decision(record: object) -> None:
    """Validate the exact closed `ListenerDeliveryDecision`.

    One closed decision is recorded for each stable delivery identity. `decisionKind` names
    one of admission, duplicate acknowledgement, saturated refusal, or shedding. Only an
    admitted decision names the Scheduler input it became via `admittedToRef`.
    """
    label = "ListenerDeliveryDecision"
    fields = _mapping(record, label)
    _exact_fields(
        fields,
        _LISTENER_DECISION_FIELDS,
        label,
        optional=frozenset({"admittedToRef"}),
    )
    _release_version(fields, "decisionSchemaVersion", label)
    _identifier(fields, "deliveryId", label)
    _reference(fields, "sourceRef", label)
    disposition = _member(fields, "decisionKind", label, LISTENER_DELIVERY_DISPOSITIONS)
    _conditional_member(
        fields,
        "admittedToRef",
        label,
        required=disposition == "admitted",
        condition="an admitted decision",
    )
    if disposition == "admitted":
        _reference(fields, "admittedToRef", label)
    _timestamp(fields, "recordedAt", label)


def validate_resource_reconciliation_lease(record: object) -> None:
    """Validate the exact closed `ResourceReconciliationLease`.

    Reconciliation is the plane action that acts on a fresh observation of the world. The
    record therefore carries both desired and observed state digests alongside its owner,
    bounded lease window, and fencing token. Freshness relative to the Supervisor interval is
    a live runtime decision rather than an extra wire member.
    """
    label = "ResourceReconciliationLease"
    fields = _mapping(record, label)
    _exact_fields(fields, _RECONCILIATION_LEASE_FIELDS, label)

    _release_version(fields, "leaseSchemaVersion", label)
    _reference(fields, "resourceRef", label)
    _reference(fields, "ownerActorRef", label)
    _positive_int(fields, "fencingToken", label)
    _timestamp(fields, "acquiredAt", label)
    _timestamp(fields, "expiresAt", label)
    _ordered_after(fields, "acquiredAt", "expiresAt", label)
    _digest(fields, "desiredStateDigest", label)
    _digest(fields, "observedStateDigest", label)


GOVERNED_EXECUTION_RECORD_VALIDATORS: Final[
    Mapping[str, GovernedExecutionRecordValidator]
] = MappingProxyType(
    {
        "HumanInteractionRequest": validate_human_interaction_request,
        "HumanInteractionOutcomeSubmission": (
            validate_human_interaction_outcome_submission
        ),
        "AutonomyProfile": validate_autonomy_profile,
        "EffectiveAutonomyProfile": validate_effective_autonomy_profile,
        "ExecutionPlaneOwnerLease": validate_execution_plane_owner_lease,
        "PlaneDispatchEnvelope": validate_plane_dispatch_envelope,
        "PlaneCompletionReport": validate_plane_completion_report,
        "ListenerDeliveryDecision": validate_listener_delivery_decision,
        "ResourceReconciliationLease": validate_resource_reconciliation_lease,
    }
)


def validate_governed_execution_record(record_name: str, record: object) -> None:
    """Validate any IP-11 governed execution record by its contract name.

    The name is supplied by the caller rather than read out of the record, because none of
    these records carries a `contractName` member and adding one would make every payload
    self-describing in a way a wire reader could trust more than it should.
    """
    validator = GOVERNED_EXECUTION_RECORD_VALIDATORS.get(record_name)
    if validator is None:
        raise ContractSemanticError(
            f"governed execution record: {record_name!r} is not a known IP-11 contract"
        )
    validator(record)
