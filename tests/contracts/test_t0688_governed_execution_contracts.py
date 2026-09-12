"""Clean-room conformance for T-0688 governed-execution wire records."""

from __future__ import annotations

import copy

import pytest

from omnivia_core.contracts.v1 import semantics_governed_execution as governed
from omnivia_core.contracts.v1.compatibility import ContractSemanticError

DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
AT = "2026-09-01T00:00:00Z"
LATER = "2026-09-01T00:01:00Z"


def ref(value: str) -> dict[str, str]:
    return {"id": value}


def human_request() -> dict[str, object]:
    return {
        "requestSchemaVersion": "1.0.0",
        "requestId": "request-1",
        "correlationId": "correlation-1",
        "idempotencyKey": "idempotency-1",
        "requestVersion": 1,
        "interactionKind": "approval",
        "ownerKind": "humanTask",
        "ownerSubjectRef": ref("task-1"),
        "ownerRevision": ref("revision-4"),
        "workspace": "workspace-1",
        "responseSchemaRef": ref("schema-1"),
        "presentationRef": ref("presentation-1"),
        "eligibilityPolicyRef": ref("policy-1"),
        "requestedAt": AT,
        "expiresAt": LATER,
        "runRef": ref("run-1"),
        "waitRef": ref("wait-1"),
    }


def submission() -> dict[str, object]:
    return {
        "submissionSchemaVersion": "1.0.0",
        "requestId": "request-1",
        "requestVersion": 1,
        "actorRef": ref("actor-1"),
        "deviceSessionRef": ref("device-session-1"),
        "responseDigest": DIGEST,
        "responseRef": ref("response-1"),
        "reason": "approved",
        "idempotencyKey": "idempotency-1",
        "submittedAt": AT,
    }


def autonomy_profile(source_kind: str = "policy") -> dict[str, object]:
    return {
        "profileSchemaVersion": "1.0.0",
        "sourceKind": source_kind,
        "sourceRef": ref(f"source-{source_kind}"),
        "allowedActionScopes": ["action.read", "action.write"],
        "allowedResourceScopes": ["resource.one"],
        "allowedModelScopes": ["model.one"],
        "allowedKnowledgeScopes": ["knowledge.one"],
        "approvalRequiredEffectClasses": ["externalEffect", "internalWrite"],
        "timeBoundMs": 1000,
        "tokenBound": 100,
        "costBound": 2.5,
        "stepBound": 10,
        "snapshotRef": ref(f"snapshot-{source_kind}"),
        "snapshotDigest": DIGEST,
    }


def contribution(source_kind: str) -> dict[str, object]:
    return {
        "sourceKind": source_kind,
        "snapshotRef": ref(f"snapshot-{source_kind}"),
        "snapshotDigest": DIGEST,
    }


def effective_profile() -> dict[str, object]:
    return {
        "resolutionSchemaVersion": "1.0.0",
        "effectiveActionScopes": ["action.read"],
        "effectiveResourceScopes": ["resource.one"],
        "effectiveModelScopes": ["model.one"],
        "effectiveKnowledgeScopes": ["knowledge.one"],
        "effectiveApprovalRequiredEffectClasses": ["externalEffect"],
        "effectiveTimeBoundMs": 1000,
        "effectiveTokenBound": 100,
        "effectiveCostBound": 1.5,
        "effectiveStepBound": 10,
        "sourceContributions": [
            contribution(kind) for kind in governed.AUTONOMY_SOURCE_KINDS
        ],
        "resolutionDigest": OTHER_DIGEST,
    }


def owner_lease(role: str = "scheduler") -> dict[str, object]:
    return {
        "leaseSchemaVersion": "1.0.0",
        "subjectRef": ref("subject-1"),
        "planeRole": role,
        "ownerActorRef": ref("actor-1"),
        "fencingToken": 2,
        "acquiredAt": AT,
        "expiresAt": LATER,
        "previousFencingToken": 1,
    }


def dispatch() -> dict[str, object]:
    return {
        "envelopeSchemaVersion": "1.0.0",
        "dispatchId": "dispatch-1",
        "subjectRef": ref("subject-1"),
        "fencingToken": 2,
        "boundedExecutionPackageRef": ref("package-1"),
        "dispatchedAt": AT,
        "dispatchedBy": ref("scheduler-1"),
    }


def completion(outcome: str = "succeeded") -> dict[str, object]:
    record: dict[str, object] = {
        "reportSchemaVersion": "1.0.0",
        "dispatchId": "dispatch-1",
        "subjectRef": ref("subject-1"),
        "fencingToken": 2,
        "completedAt": AT,
        "outcomeClass": outcome,
        "evidenceRef": ref("evidence-1"),
    }
    if outcome == "succeeded":
        record["resultRef"] = ref("result-1")
    elif outcome == "failed":
        record["failureRef"] = ref("failure-1")
    return record


def listener(decision: str = "admitted") -> dict[str, object]:
    record: dict[str, object] = {
        "decisionSchemaVersion": "1.0.0",
        "deliveryId": "delivery-1",
        "sourceRef": ref("source-1"),
        "decisionKind": decision,
        "recordedAt": AT,
    }
    if decision == "admitted":
        record["admittedToRef"] = ref("admission-1")
    return record


def reconciliation() -> dict[str, object]:
    return {
        "leaseSchemaVersion": "1.0.0",
        "resourceRef": ref("resource-1"),
        "ownerActorRef": ref("supervisor-1"),
        "fencingToken": 3,
        "acquiredAt": AT,
        "expiresAt": LATER,
        "desiredStateDigest": DIGEST,
        "observedStateDigest": OTHER_DIGEST,
    }


VALID_RECORDS = {
    "HumanInteractionRequest": human_request,
    "HumanInteractionOutcomeSubmission": submission,
    "AutonomyProfile": autonomy_profile,
    "EffectiveAutonomyProfile": effective_profile,
    "ExecutionPlaneOwnerLease": owner_lease,
    "PlaneDispatchEnvelope": dispatch,
    "PlaneCompletionReport": completion,
    "ListenerDeliveryDecision": listener,
    "ResourceReconciliationLease": reconciliation,
}


@pytest.mark.parametrize(("name", "factory"), VALID_RECORDS.items())
def test_every_closed_record_validates_and_registry_dispatches(name, factory) -> None:
    record = factory()
    governed.GOVERNED_EXECUTION_RECORD_VALIDATORS[name](record)
    governed.validate_governed_execution_record(name, record)


@pytest.mark.parametrize(("name", "factory"), VALID_RECORDS.items())
def test_every_closed_record_refuses_an_unknown_member(name, factory) -> None:
    record = factory()
    record["invented"] = True
    with pytest.raises(ContractSemanticError, match="unknown fields"):
        governed.validate_governed_execution_record(name, record)


def test_human_request_enforces_version_owner_and_wait_constraints() -> None:
    for key in ("requestSchemaVersion", "workspace", "ownerRevision"):
        record = human_request()
        record.pop(key)
        with pytest.raises(ContractSemanticError, match="missing"):
            governed.validate_human_interaction_request(record)

    for interaction, owner in (
        ("input", "humanTask"),
        ("approval", "humanTask"),
        ("review", "review"),
        ("handoff", "handoff"),
    ):
        record = human_request()
        record["interactionKind"] = interaction
        record["ownerKind"] = owner
        governed.validate_human_interaction_request(record)

    record = human_request()
    record["interactionKind"] = "review"
    with pytest.raises(ContractSemanticError, match="ownerKind"):
        governed.validate_human_interaction_request(record)

    record = human_request()
    record.pop("runRef")
    with pytest.raises(ContractSemanticError, match="waitRef requires runRef"):
        governed.validate_human_interaction_request(record)

    record = human_request()
    record["requestVersion"] = 0
    with pytest.raises(ContractSemanticError, match="positive"):
        governed.validate_human_interaction_request(record)


def test_autonomy_profiles_enforce_order_uniqueness_bounds_and_exact_provenance() -> (
    None
):
    record = autonomy_profile()
    record["allowedActionScopes"] = ["action.write", "action.read"]
    with pytest.raises(ContractSemanticError, match="canonical order"):
        governed.validate_autonomy_profile(record)

    record = autonomy_profile()
    record["allowedActionScopes"] = ["action.read", "action.read"]
    with pytest.raises(ContractSemanticError, match="repeat"):
        governed.validate_autonomy_profile(record)

    record = autonomy_profile()
    record["costBound"] = -0.01
    with pytest.raises(ContractSemanticError, match="non-negative"):
        governed.validate_autonomy_profile(record)

    record = effective_profile()
    record["sourceContributions"][0]["sourceRef"] = ref("not-canonical")
    with pytest.raises(ContractSemanticError, match="unknown fields"):
        governed.validate_effective_autonomy_profile(record)

    record = effective_profile()
    record["sourceContributions"].reverse()
    with pytest.raises(ContractSemanticError, match="canonical source order"):
        governed.validate_effective_autonomy_profile(record)


@pytest.mark.parametrize("role", governed.PLANE_ROLES)
def test_owner_lease_accepts_each_of_the_five_plane_roles(role: str) -> None:
    governed.validate_execution_plane_owner_lease(owner_lease(role))


def test_plane_conditional_members_and_fencing_are_closed() -> None:
    record = owner_lease()
    record["previousFencingToken"] = 2
    with pytest.raises(ContractSemanticError, match="greater"):
        governed.validate_execution_plane_owner_lease(record)

    for outcome in governed.PLANE_COMPLETION_OUTCOMES:
        governed.validate_plane_completion_report(completion(outcome))

    record = completion("failed")
    record["resultRef"] = ref("result-not-allowed")
    with pytest.raises(ContractSemanticError, match="permitted only"):
        governed.validate_plane_completion_report(record)

    for decision in governed.LISTENER_DELIVERY_DISPOSITIONS:
        governed.validate_listener_delivery_decision(listener(decision))

    record = listener("duplicateAcknowledged")
    record["admittedToRef"] = ref("not-admitted")
    with pytest.raises(ContractSemanticError, match="permitted only"):
        governed.validate_listener_delivery_decision(record)


def test_unknown_contract_name_is_refused() -> None:
    with pytest.raises(ContractSemanticError, match="not a known"):
        governed.validate_governed_execution_record("InventedRecord", {})


def test_fixture_factories_do_not_share_mutable_state() -> None:
    assert copy.deepcopy(effective_profile()) == effective_profile()
