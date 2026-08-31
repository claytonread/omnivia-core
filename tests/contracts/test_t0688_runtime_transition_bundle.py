"""Clean-room tests for the T-0688 IP-07 `RuntimeTransitionBundle` public contract lane.

`RuntimeTransitionBundle` is the single write a Runtime Run applies against the aggregate
revision it expected. Like `RuntimeDefinitionBinding` (T-0688 IP-06), it is deliberately not a
T-0679 `contractName`-tagged record, so these tests live alongside
`test_t0688_runtime_definition_binding.py` rather than the fixture-backed
`test_workflow_hardening_contracts.py` suite.

Independently authored OmniVia examples throughout; no external source, schema, or test is
read or reused.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnivia_core.contracts.v1 import semantics_workflow as workflow
from omnivia_core.contracts.v1.compatibility import ContractSemanticError

_DIGEST = "sha256:" + "a" * 64
_OTHER_DIGEST = "sha256:" + "b" * 64
_GENESIS_LINK = "sha256:" + "0" * 64


def _event(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "eventId": "event-t0688-harbour-1",
        "runId": "run-t0688-harbour-1",
        "sequence": 0,
        "previousIntegrityLink": _GENESIS_LINK,
        "eventKind": "run.step.dispatched",
        "recordedAt": "2026-01-01T00:00:00Z",
        "payloadDigest": _DIGEST,
    }
    record.update(overrides)
    return record


def _bundle(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "bundleSchemaVersion": "1.0.0",
        "bundleId": "bundle-t0688-harbour-1",
        "runId": "run-t0688-harbour-1",
        "expectedAggregateRevision": 0,
        "event": _event(),
        "producedAggregateRevision": 1,
    }
    record.update(overrides)
    record["payloadDigest"] = workflow.compute_transition_bundle_payload_digest(record)
    return record


def _full_bundle(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "bundleSchemaVersion": "1.0.0",
        "bundleId": "bundle-t0688-harbour-2",
        "runId": "run-t0688-harbour-2",
        "attemptRef": {"attemptId": "attempt-t0688-harbour-2"},
        "expectedAggregateRevision": 4,
        "event": _event(
            eventId="event-t0688-harbour-2",
            runId="run-t0688-harbour-2",
            sequence=5,
        ),
        "boundaryResults": [
            {"boundaryId": "boundary-t0688-harbour-1", "outcome": "reached"}
        ],
        "activations": [
            {"activationId": "activation-t0688-harbour-1", "componentId": "component-t0688-fetch"}
        ],
        "schedulingIntents": [
            {"intentId": "intent-t0688-harbour-1", "targetAt": "2026-01-01T01:00:00Z"}
        ],
        "evidenceRefs": [{"evidenceId": "evidence-t0688-harbour-1"}],
        "waitConsequences": [
            {"waitId": "wait-t0688-harbour-1", "resolution": "satisfied"}
        ],
        "loopConsequences": [
            {"loopPlanId": "loop-t0688-harbour-1", "iteration": 2}
        ],
        "effectSettlements": [
            {
                "effectRequestId": "effect-t0688-harbour-1",
                "settlementClass": "committed",
                "verifiedReceiptRef": {"receiptId": "receipt-t0688-harbour-1"},
                "completionContribution": {"contributes": True},
            }
        ],
        "producedAggregateRevision": 5,
    }
    record.update(overrides)
    record["payloadDigest"] = workflow.compute_transition_bundle_payload_digest(record)
    return record


# --------------------------------------------------------------------------
# Minimal and full valid bundles
# --------------------------------------------------------------------------


def test_minimal_valid_bundle_at_revision_zero_validates() -> None:
    bundle = _bundle()
    assert bundle["expectedAggregateRevision"] == 0
    assert bundle["producedAggregateRevision"] == 1
    assert bundle["event"]["sequence"] == 0
    workflow.validate_transition_bundle(bundle)


def test_full_bundle_with_every_consequence_family_validates() -> None:
    workflow.validate_transition_bundle(_full_bundle())


# --------------------------------------------------------------------------
# payloadDigest: exact recomputation, and excludes only payloadDigest
# --------------------------------------------------------------------------


def test_payload_digest_matches_recomputation() -> None:
    bundle = _bundle()
    assert bundle["payloadDigest"] == workflow.compute_transition_bundle_payload_digest(bundle)


def test_payload_digest_refused_after_member_changed_without_recompute() -> None:
    bundle = _bundle()
    tampered = {**bundle, "bundleId": "bundle-t0688-harbour-tampered"}
    with pytest.raises(ContractSemanticError, match="does not match its recomputation"):
        workflow.validate_transition_bundle(tampered)


def test_payload_digest_computation_excludes_only_payload_digest() -> None:
    bundle = _bundle()
    without_digest = {k: v for k, v in bundle.items() if k != "payloadDigest"}
    digest_a = workflow.compute_transition_bundle_payload_digest(without_digest)
    digest_b = workflow.compute_transition_bundle_payload_digest(
        {**without_digest, "payloadDigest": "sha256:" + "f" * 64}
    )
    assert digest_a == digest_b == bundle["payloadDigest"]


# --------------------------------------------------------------------------
# bundleSchemaVersion
# --------------------------------------------------------------------------


def test_unsupported_bundle_schema_major_is_refused() -> None:
    with pytest.raises(ContractSemanticError, match="major version 2 is not supported"):
        workflow.validate_transition_bundle(_bundle(bundleSchemaVersion="2.0.0"))


@pytest.mark.parametrize("malformed", ["1.0", "1", "v1.0.0", "1.x.0", ""])
def test_malformed_bundle_schema_version_is_refused(malformed: str) -> None:
    with pytest.raises(ContractSemanticError, match="not a well-formed exact ReleaseVersion"):
        workflow.validate_transition_bundle(_bundle(bundleSchemaVersion=malformed))


# --------------------------------------------------------------------------
# revision arithmetic
# --------------------------------------------------------------------------


def test_negative_expected_revision_is_refused() -> None:
    with pytest.raises(ContractSemanticError, match="is not a non-negative integer"):
        workflow.validate_transition_bundle(
            _bundle(expectedAggregateRevision=-1, producedAggregateRevision=0)
        )


def test_negative_produced_revision_is_refused() -> None:
    bundle = _bundle()
    bundle["producedAggregateRevision"] = -1
    bundle["payloadDigest"] = workflow.compute_transition_bundle_payload_digest(bundle)
    with pytest.raises(ContractSemanticError, match="is not a non-negative integer"):
        workflow.validate_transition_bundle(bundle)


@pytest.mark.parametrize("produced", [0, 2])
def test_produced_revision_not_exactly_expected_plus_one_is_refused(produced: int) -> None:
    bundle = _bundle()
    bundle["producedAggregateRevision"] = produced
    bundle["payloadDigest"] = workflow.compute_transition_bundle_payload_digest(bundle)
    with pytest.raises(
        ContractSemanticError, match="producedAggregateRevision must be exactly"
    ):
        workflow.validate_transition_bundle(bundle)


# --------------------------------------------------------------------------
# RuntimeJournalEvent
# --------------------------------------------------------------------------


def test_journal_event_closed_shape_refuses_unknown_fields() -> None:
    with pytest.raises(ContractSemanticError, match="unknown fields"):
        workflow.validate_runtime_journal_event(_event(extra="field"))


def test_journal_event_zero_sequence_is_genesis_and_valid() -> None:
    workflow.validate_runtime_journal_event(_event(sequence=0))


def test_journal_event_negative_sequence_is_refused() -> None:
    with pytest.raises(ContractSemanticError, match="is not a non-negative integer"):
        workflow.validate_runtime_journal_event(_event(sequence=-1))


def test_journal_event_run_mismatch_is_refused() -> None:
    with pytest.raises(ContractSemanticError, match="runId does not match"):
        workflow.validate_runtime_journal_event(_event(), run_id="run-t0688-other")


def test_journal_event_run_mismatch_is_refused_within_bundle() -> None:
    bundle = _bundle(event=_event(runId="run-t0688-other"))
    with pytest.raises(ContractSemanticError, match="runId does not match"):
        workflow.validate_transition_bundle(bundle)


def test_journal_event_malformed_event_kind_is_refused() -> None:
    with pytest.raises(
        ContractSemanticError, match="eventKind is not a well-formed DiagnosticCode"
    ):
        workflow.validate_runtime_journal_event(_event(eventKind="RunStepDispatched"))


def test_journal_event_malformed_digests_are_refused() -> None:
    with pytest.raises(ContractSemanticError, match="not a well-formed Digest"):
        workflow.validate_runtime_journal_event(_event(previousIntegrityLink="not-a-digest"))
    with pytest.raises(ContractSemanticError, match="not a well-formed Digest"):
        workflow.validate_runtime_journal_event(_event(payloadDigest="not-a-digest"))


def test_journal_event_malformed_timestamp_is_refused() -> None:
    with pytest.raises(ContractSemanticError, match="not a well-formed Timestamp"):
        workflow.validate_runtime_journal_event(_event(recordedAt="not-a-timestamp"))


# --------------------------------------------------------------------------
# Optional ordered arrays: plain mapping items, and evidenceRefs
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["boundaryResults", "activations", "schedulingIntents", "waitConsequences", "loopConsequences"],
)
def test_plain_array_items_must_be_non_empty_mappings(field: str) -> None:
    with pytest.raises(ContractSemanticError, match="expected a mapping"):
        workflow.validate_transition_bundle(_bundle(**{field: ["not-a-mapping"]}))
    with pytest.raises(ContractSemanticError, match="must not be empty"):
        workflow.validate_transition_bundle(_bundle(**{field: [{}]}))


def test_evidence_refs_items_must_be_non_empty_reference_mappings() -> None:
    with pytest.raises(ContractSemanticError, match="expected a mapping"):
        workflow.validate_transition_bundle(_bundle(evidenceRefs=["not-a-mapping"]))
    with pytest.raises(ContractSemanticError, match="evidenceRefs\\[0\\] must not be empty"):
        workflow.validate_transition_bundle(_bundle(evidenceRefs=[{}]))


# --------------------------------------------------------------------------
# EffectSettlement
# --------------------------------------------------------------------------


def _settlement(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "effectRequestId": "effect-t0688-harbour-1",
        "settlementClass": "unknown",
        "completionContribution": {"contributes": False},
    }
    record.update(overrides)
    return record


def test_settlement_class_is_closed() -> None:
    with pytest.raises(ContractSemanticError, match="not one of"):
        workflow.validate_transition_bundle(
            _bundle(effectSettlements=[_settlement(settlementClass="succeeded")])
        )


def test_committed_settlement_requires_receipt() -> None:
    with pytest.raises(
        ContractSemanticError, match="committed requires verifiedReceiptRef"
    ):
        workflow.validate_transition_bundle(
            _bundle(effectSettlements=[_settlement(settlementClass="committed")])
        )


@pytest.mark.parametrize("settlement_class", ["not_committed", "unknown"])
def test_non_committed_settlement_forbids_receipt(settlement_class: str) -> None:
    with pytest.raises(
        ContractSemanticError, match="must not carry verifiedReceiptRef"
    ):
        workflow.validate_transition_bundle(
            _bundle(
                effectSettlements=[
                    _settlement(
                        settlementClass=settlement_class,
                        verifiedReceiptRef={"receiptId": "receipt-t0688-harbour-1"},
                    )
                ]
            )
        )


def test_settlement_completion_contribution_is_required_and_non_empty() -> None:
    missing = {
        "effectRequestId": "effect-t0688-harbour-1",
        "settlementClass": "unknown",
    }
    with pytest.raises(ContractSemanticError, match="missing completionContribution"):
        workflow.validate_transition_bundle(_bundle(effectSettlements=[missing]))
    with pytest.raises(ContractSemanticError, match="must not be empty"):
        workflow.validate_transition_bundle(
            _bundle(effectSettlements=[_settlement(completionContribution={})])
        )


def test_committed_settlement_with_receipt_validates() -> None:
    workflow.validate_transition_bundle(
        _bundle(
            effectSettlements=[
                _settlement(
                    settlementClass="committed",
                    verifiedReceiptRef={"receiptId": "receipt-t0688-harbour-1"},
                )
            ]
        )
    )


# --------------------------------------------------------------------------
# Closed shape and registry posture
# --------------------------------------------------------------------------


def test_unknown_fields_are_refused() -> None:
    for field in ("contractName", "extra", "legacyBundle"):
        with pytest.raises(ContractSemanticError, match="unknown fields"):
            workflow.validate_transition_bundle(_bundle(**{field: "x"}))


def test_bundle_is_never_registered_and_workflow_transition_bundle_probe_stays_refused() -> None:
    """`RuntimeTransitionBundle` carries no `contractName` and is not a T-0679 registrant.

    The invented `WorkflowTransitionBundle` shape the FX-WEFT-BUNDLE red probe exercises is a
    different, unrelated payload; it is refused by the T-0679 registry today for the same
    reason it always has been (unknown `contractName`), independent of this contract's
    addition.
    """
    assert "RuntimeTransitionBundle" not in workflow.WORKFLOW_RECORD_VALIDATORS
    assert "WorkflowTransitionBundle" not in workflow.WORKFLOW_RECORD_VALIDATORS
    with pytest.raises(ContractSemanticError, match="not a known T-0679 contract"):
        workflow.validate_workflow_record(_bundle(contractName="RuntimeTransitionBundle"))
    with pytest.raises(ContractSemanticError, match="not a known T-0679 contract"):
        workflow.validate_workflow_record(
            {
                "contractName": "WorkflowTransitionBundle",
                "bundleId": "bundle-t0688-harbour-09",
                "idempotencyKey": "idem-t0688-harbour-09",
                "expectedRevision": "revision-harbour-run-14",
                "producedRevision": "revision-harbour-run-15",
                "transitions": [],
                "replayDisposition": "no_op",
                "appliedAtomically": True,
            }
        )
