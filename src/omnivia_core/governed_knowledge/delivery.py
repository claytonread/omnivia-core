"""KI-03: `ContextDeliveryReceipt` -- a consumer/harness transport observation (spec 8.3).

Distinct from `ContextSelectionManifest` (Core's own selection record): a
delivery receipt is issued by the *consumer/runtime*, and can establish only
what that instrumented consumer assembled and attempted to send, plus any
observed provider acknowledgement (spec 8.1). It never claims model reliance --
there is no field anywhere on this type that could assert it, by construction,
not by a runtime check.

Transport observation is an ordered, append-only sequence of
:class:`DeliveryEvent`: state is never overwritten to "simplify display" (spec
8.3). Each event's state must be a legal successor of the previous one under
:data:`ALLOWED_TRANSITIONS`, so a receipt can only ever represent an
observation history that could actually have happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

from omnivia_core.governed_knowledge.errors import (
    GovernedKnowledgeErrorCode,
    require,
)
from omnivia_core.semantic_registry.temporal import TemporalInstant

DELIVERY_RECEIPT_VERSION = "governed-knowledge-delivery-receipt-v1"


def _require_id(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and value.strip() != "",
        GovernedKnowledgeErrorCode.MISSING_FIELD,
        f"{field_name} is required",
    )


def _require_str_tuple(field_name: str, value: tuple[str, ...]) -> None:
    require(
        isinstance(value, tuple),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        f"{field_name} must be a tuple",
    )
    for index, item in enumerate(value):
        require(
            isinstance(item, str) and item.strip() != "",
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            f"{field_name}[{index}] must be a non-empty string",
        )


class DeliveryTransportState(str, Enum):
    """The logical transport observation states (spec 8.3)."""

    ASSEMBLED = "assembled"
    DISPATCH_ATTEMPTED = "dispatch_attempted"
    PROVIDER_ACKNOWLEDGED = "provider_acknowledged"
    RESPONSE_RECEIVED = "response_received"
    FAILED_BEFORE_DISPATCH = "failed_before_dispatch"
    OUTCOME_UNKNOWN = "outcome_unknown"


#: Legal next states from each state. `RESPONSE_RECEIVED`, `FAILED_BEFORE_DISPATCH`
#: and `OUTCOME_UNKNOWN` are terminal *for one receipt's own event log*: a later
#: resolution (e.g. an initially unknown outcome later confirmed) is recorded as
#: a new, separately issued receipt referencing this one, never as a rewrite of
#: this history (spec 8.3, 8.6).
ALLOWED_TRANSITIONS: MappingProxyType[
    DeliveryTransportState, frozenset[DeliveryTransportState]
] = MappingProxyType(
    {
        DeliveryTransportState.ASSEMBLED: frozenset(
            {
                DeliveryTransportState.DISPATCH_ATTEMPTED,
                DeliveryTransportState.FAILED_BEFORE_DISPATCH,
                DeliveryTransportState.OUTCOME_UNKNOWN,
            }
        ),
        DeliveryTransportState.DISPATCH_ATTEMPTED: frozenset(
            {
                DeliveryTransportState.PROVIDER_ACKNOWLEDGED,
                DeliveryTransportState.OUTCOME_UNKNOWN,
            }
        ),
        DeliveryTransportState.PROVIDER_ACKNOWLEDGED: frozenset(
            {
                DeliveryTransportState.RESPONSE_RECEIVED,
                DeliveryTransportState.OUTCOME_UNKNOWN,
            }
        ),
        DeliveryTransportState.RESPONSE_RECEIVED: frozenset(),
        DeliveryTransportState.FAILED_BEFORE_DISPATCH: frozenset(),
        DeliveryTransportState.OUTCOME_UNKNOWN: frozenset(),
    }
)


class ReceiptIssuerClass(str, Enum):
    """Issuer/evidence-level trust label a receipt must carry (spec 8.1)."""

    CORE_ISSUED = "core_issued"
    INSTRUMENTED_FIRST_PARTY_CONSUMER = "instrumented_first_party_consumer"
    EXTERNALLY_ATTESTED = "externally_attested"
    INCOMPLETE_UNVERIFIED = "incomplete_unverified"


class SegmentProvenanceClass(str, Enum):
    CORE_SELECTED = "core_selected"
    USER_SUPPLIED = "user_supplied"
    SYSTEM_INSTRUCTION = "system_instruction"
    SKILL_OR_WORKFLOW = "skill_or_workflow"
    LOCAL_FILE = "local_file"
    EXTERNAL_RETRIEVAL = "external_retrieval"
    TOOL_RESULT = "tool_result"


@dataclass(frozen=True, slots=True)
class DeliverySegment:
    segment_ref: str
    representation_ref: str
    provenance_class: SegmentProvenanceClass
    original_selected_item_ref: str | None = None
    transformation_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_id("segment_ref", self.segment_ref)
        _require_id("representation_ref", self.representation_ref)
        require(
            isinstance(self.provenance_class, SegmentProvenanceClass),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "provenance_class must be a SegmentProvenanceClass",
        )
        if self.original_selected_item_ref is not None:
            _require_id("original_selected_item_ref", self.original_selected_item_ref)
        _require_str_tuple("transformation_refs", self.transformation_refs)


@dataclass(frozen=True, slots=True)
class DeliveryEvent:
    """One observed transport event."""

    state: DeliveryTransportState
    observed_at: TemporalInstant
    detail: str | None = None

    def __post_init__(self) -> None:
        require(
            isinstance(self.state, DeliveryTransportState),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "DeliveryEvent.state must be a DeliveryTransportState",
        )
        require(
            isinstance(self.observed_at, TemporalInstant),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "DeliveryEvent.observed_at must be a TemporalInstant",
        )
        if self.detail is not None:
            require(
                isinstance(self.detail, str) and self.detail.strip() != "",
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "DeliveryEvent.detail must be a non-empty string when present",
            )


@dataclass(frozen=True, slots=True)
class ContextDeliveryReceipt:
    """A consumer/harness-issued transport observation (spec 8.3).

    `additional_source_refs` names context the consumer supplied itself
    (extra local files, user text, system instructions, external retrieval)
    that must never be silently attributed to Core (spec 8.3); keeping it as
    its own field, disjoint from `manifest_ref`'s selected items, is what makes
    that attribution boundary structural rather than a documentation promise.
    """

    receipt_id: str
    workspace_id: str
    consumer_ref: str
    manifest_ref: str
    events: tuple[DeliveryEvent, ...]
    issuer_class: ReceiptIssuerClass
    run_ref: str | None = None
    step_ref: str | None = None
    attempt_ref: str | None = None
    pack_ref: str | None = None
    supplied_segments: tuple[DeliverySegment, ...] = ()
    additional_source_refs: tuple[str, ...] = ()
    transformations_after_core: tuple[str, ...] = ()
    instruction_version_refs: tuple[str, ...] = ()
    skill_workflow_version_refs: tuple[str, ...] = ()
    tool_result_refs: tuple[str, ...] = ()
    provider_ref: str | None = None
    model_ref: str | None = None
    effective_configuration_refs: tuple[str, ...] = ()
    instrumentation_complete: bool = True
    result_ref: str | None = None
    receipt_version: str = field(init=False, default=DELIVERY_RECEIPT_VERSION)

    def __post_init__(self) -> None:
        _require_id("receipt_id", self.receipt_id)
        _require_id("workspace_id", self.workspace_id)
        _require_id("consumer_ref", self.consumer_ref)
        _require_id("manifest_ref", self.manifest_ref)
        require(
            isinstance(self.issuer_class, ReceiptIssuerClass),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "issuer_class must be a ReceiptIssuerClass",
        )
        require(
            isinstance(self.events, tuple) and len(self.events) >= 1,
            GovernedKnowledgeErrorCode.MISSING_FIELD,
            "events must be a non-empty tuple of DeliveryEvent",
        )
        for index, event in enumerate(self.events):
            require(
                isinstance(event, DeliveryEvent),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"events[{index}] must be a DeliveryEvent",
            )
        require(
            self.events[0].state is DeliveryTransportState.ASSEMBLED,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "the first delivery event must be assembled",
        )
        for index in range(1, len(self.events)):
            previous = self.events[index - 1].state
            current = self.events[index].state
            require(
                current in ALLOWED_TRANSITIONS[previous],
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"events[{index}]: {current.value!r} is not a legal successor of "
                f"{previous.value!r}",
            )
        _require_str_tuple("additional_source_refs", self.additional_source_refs)
        _require_str_tuple(
            "transformations_after_core", self.transformations_after_core
        )
        for index, segment in enumerate(self.supplied_segments):
            require(
                isinstance(segment, DeliverySegment),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"supplied_segments[{index}] must be a DeliverySegment",
            )
        for name in (
            "instruction_version_refs",
            "skill_workflow_version_refs",
            "tool_result_refs",
            "effective_configuration_refs",
        ):
            _require_str_tuple(name, getattr(self, name))
        for name in ("provider_ref", "model_ref", "result_ref"):
            value = getattr(self, name)
            if value is not None:
                _require_id(name, value)
        require(
            isinstance(self.instrumentation_complete, bool),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "instrumentation_complete must be a boolean",
        )
        if self.issuer_class is ReceiptIssuerClass.INCOMPLETE_UNVERIFIED:
            require(
                not self.instrumentation_complete,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "an incomplete/unverified receipt cannot claim complete instrumentation",
            )

    @property
    def current_state(self) -> DeliveryTransportState:
        return self.events[-1].state


__all__ = [
    "ALLOWED_TRANSITIONS",
    "DELIVERY_RECEIPT_VERSION",
    "ContextDeliveryReceipt",
    "DeliveryEvent",
    "DeliverySegment",
    "DeliveryTransportState",
    "ReceiptIssuerClass",
    "SegmentProvenanceClass",
]
