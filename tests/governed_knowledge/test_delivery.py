"""Tests for KI-03 `ContextDeliveryReceipt` (spec 8.3).

Covers transport receipt states, illegal transitions, additional-source
attribution, unverified external issuer, and timeout/unknown outcomes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnivia_core.governed_knowledge.delivery import (
    ContextDeliveryReceipt,
    DeliveryEvent,
    DeliveryTransportState,
    ReceiptIssuerClass,
)
from omnivia_core.governed_knowledge.errors import GovernedKnowledgeValidationError
from omnivia_core.governed_knowledge.profile_content import (
    delivery_receipt_from_content,
    delivery_receipt_to_content,
)
from omnivia_core.semantic_registry.temporal import (
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
)


def _instant(second: int = 0) -> TemporalInstant:
    return TemporalInstant(
        value=datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC),
        precision=TemporalPrecision.SECOND,
        provenance=TemporalProvenance.STATED,
    )


def _receipt(
    *states: DeliveryTransportState, **kwargs: object
) -> ContextDeliveryReceipt:
    events = tuple(
        DeliveryEvent(state=state, observed_at=_instant(i))
        for i, state in enumerate(states)
    )
    kwargs.setdefault(
        "issuer_class", ReceiptIssuerClass.INSTRUMENTED_FIRST_PARTY_CONSUMER
    )
    return ContextDeliveryReceipt(
        receipt_id="receipt-1",
        workspace_id="ws-1",
        consumer_ref="consumer-1",
        manifest_ref="manifest-1",
        events=events,
        **kwargs,  # type: ignore[arg-type]
    )


def test_full_successful_transport_sequence() -> None:
    receipt = _receipt(
        DeliveryTransportState.ASSEMBLED,
        DeliveryTransportState.DISPATCH_ATTEMPTED,
        DeliveryTransportState.PROVIDER_ACKNOWLEDGED,
        DeliveryTransportState.RESPONSE_RECEIVED,
    )
    assert receipt.current_state is DeliveryTransportState.RESPONSE_RECEIVED
    assert (
        delivery_receipt_from_content(delivery_receipt_to_content(receipt)) == receipt
    )


def test_failed_before_dispatch_terminal_state() -> None:
    receipt = _receipt(
        DeliveryTransportState.ASSEMBLED, DeliveryTransportState.FAILED_BEFORE_DISPATCH
    )
    assert receipt.current_state is DeliveryTransportState.FAILED_BEFORE_DISPATCH


def test_outcome_unknown_from_timeout() -> None:
    receipt = _receipt(
        DeliveryTransportState.ASSEMBLED,
        DeliveryTransportState.DISPATCH_ATTEMPTED,
        DeliveryTransportState.OUTCOME_UNKNOWN,
    )
    assert receipt.current_state is DeliveryTransportState.OUTCOME_UNKNOWN


def test_illegal_transition_is_rejected() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        _receipt(
            DeliveryTransportState.RESPONSE_RECEIVED,
            DeliveryTransportState.DISPATCH_ATTEMPTED,
        )


def test_events_must_be_non_empty() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        ContextDeliveryReceipt(
            receipt_id="receipt-1",
            workspace_id="ws-1",
            consumer_ref="consumer-1",
            manifest_ref="manifest-1",
            events=(),
            issuer_class=ReceiptIssuerClass.CORE_ISSUED,
        )


def test_additional_source_refs_are_distinct_from_manifest_selection() -> None:
    receipt = _receipt(
        DeliveryTransportState.ASSEMBLED,
        additional_source_refs=("local-file-1", "user-pasted-text"),
    )
    assert "local-file-1" in receipt.additional_source_refs
    # Additional sources are never folded into manifest_ref/selected content.
    assert receipt.manifest_ref == "manifest-1"


def test_unverified_external_issuer_is_labelled_incomplete() -> None:
    receipt = _receipt(
        DeliveryTransportState.ASSEMBLED,
        DeliveryTransportState.OUTCOME_UNKNOWN,
    )
    unverified = ContextDeliveryReceipt(
        receipt_id="receipt-2",
        workspace_id="ws-1",
        consumer_ref="consumer-1",
        manifest_ref="manifest-1",
        events=(
            DeliveryEvent(
                state=DeliveryTransportState.ASSEMBLED, observed_at=_instant()
            ),
        ),
        issuer_class=ReceiptIssuerClass.INCOMPLETE_UNVERIFIED,
        instrumentation_complete=False,
    )
    assert unverified.issuer_class is ReceiptIssuerClass.INCOMPLETE_UNVERIFIED
    assert receipt.issuer_class is not ReceiptIssuerClass.EXTERNALLY_ATTESTED


def test_externally_attested_issuer_class() -> None:
    receipt = _receipt(
        DeliveryTransportState.ASSEMBLED,
        issuer_class=ReceiptIssuerClass.EXTERNALLY_ATTESTED,
    )
    assert receipt.issuer_class is ReceiptIssuerClass.EXTERNALLY_ATTESTED


def test_receipt_has_no_model_reliance_field() -> None:
    fields = set(ContextDeliveryReceipt.__dataclass_fields__)
    assert not any("model_reliance" in name or "reliance" in name for name in fields)
