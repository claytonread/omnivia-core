"""Tests for the generated Host Contract v1 types (DOC-004 §AA).

The canonical schema is the source of truth; this module pins the behaviour the
governed specification requires of the decoders built from it: closed-vocabulary
fail-closed decoding, closed-record unknown-field refusal, duplicate-identifier
refusal, immutability of decoded values, and exact canonical field spelling.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.host_contract.v1 import generated as gen

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO_ROOT / "contracts" / "host" / "v1" / "fixtures"


def load(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Contract identity and version pinning
# --------------------------------------------------------------------------


def test_host_contract_version_is_the_approved_1_0_0() -> None:
    assert gen.HOST_CONTRACT_VERSION == "1.0.0"
    assert gen.HOST_CONTRACT_MAJOR == 1
    assert gen.HOST_CONTRACT_SCHEMA_ID == "urn:omnivia:host-contract:1.0.0"


def test_generated_module_records_its_exact_byte_approval() -> None:
    assert gen.HOST_CONTRACT_APPROVAL_ID == "GOV-HOST-CONTRACT-APPROVAL-001"
    assert gen.HOST_CONTRACT_PROPOSAL_COMMIT == "2568f0049256b86fa58dc2387e8302aa4da94ae1"
    assert gen.HOST_CONTRACT_CONTENT_SET_SHA256 == (
        "31da9251944079ea1bc8db61ed0a393a05e0ce18602f31f6771def4ab243f0ac"
    )


def test_every_governed_record_is_a_top_level_schema_branch() -> None:
    assert gen.HOST_CONTRACT_RECORDS == (
        "HostRequest",
        "HostResult",
        "ShellContext",
        "CompatibilityResult",
        "CapabilityAvailability",
        "CapabilityDenial",
        "ModuleContributionDeclaration",
        "ReleasePackageIdentity",
        "PromotionRecord",
        "RollbackRecord",
        "EnvironmentBinding",
        "DevelopmentProfile",
        "ShellScenario",
        "TestDriverRequest",
        "TestDriverResult",
    )


# --------------------------------------------------------------------------
# Closed vocabulary
# --------------------------------------------------------------------------


def test_all_seventeen_fixed_namespaces_are_present_in_canonical_order() -> None:
    assert gen.HOST_NAMESPACES == (
        "identity",
        "organisation",
        "workspace",
        "data",
        "knowledge",
        "files",
        "workflows",
        "agents",
        "integrations",
        "credentials",
        "notifications",
        "approvals",
        "audit",
        "evidence",
        "entitlements",
        "navigation",
        "diagnostics",
    )
    assert len(gen.HOST_NAMESPACES) == 17


def test_targets_availability_states_and_denial_categories_are_the_governed_sets() -> None:
    assert gen.TARGETS == ("desktop", "cloud", "conformance")
    assert gen.AVAILABILITY_STATES == (
        "available",
        "unavailable_on_target",
        "available_after_configuration",
        "available_through_companion",
        "permission_denied",
        "entitlement_unavailable",
        "temporarily_unhealthy",
    )
    assert gen.DENIAL_CATEGORIES == (
        "authentication_required",
        "membership_required",
        "permission_denied",
        "approval_required",
        "entitlement_required",
        "unavailable_on_target",
        "configuration_required",
        "companion_required",
        "blocked_by_policy",
        "temporarily_unhealthy",
    )
    assert gen.OUTCOMES == ("success", "denied", "unavailable", "failed")
    assert gen.REMEDIATION_KINDS == (
        "sign_in",
        "request_membership",
        "request_permission",
        "request_approval",
        "manage_entitlement",
        "configure",
        "install_companion",
        "retry_later",
        "contact_administrator",
        "none",
    )
    assert gen.COMPATIBILITY_STATUSES == (
        "compatible",
        "compatible_with_degradation",
        "compatible_after_configuration",
        "requires_companion",
        "incompatible",
        "blocked_by_policy",
    )
    assert gen.CONTRIBUTION_KINDS == (
        "route",
        "navigation",
        "command",
        "settings_section",
        "context_item_type",
        "workflow_component",
        "runtime_adapter",
        "diagnostic",
        "entitlement",
    )
    assert gen.TEST_DRIVER_OPERATIONS == (
        "navigate",
        "invoke_command",
        "capture_screenshot",
        "run_accessibility_check",
        "capture_trace",
        "inspect_state",
    )
    assert gen.TEST_DRIVER_OUTCOMES == ("passed", "failed", "unavailable")
    assert gen.PROMOTION_RESULTS == ("promoted", "rejected")


def test_development_target_excludes_cloud() -> None:
    """A DevelopmentProfile is not expressible against a production Cloud target."""
    assert gen.DEVELOPMENT_TARGETS == ("desktop", "conformance")
    assert "cloud" not in gen.DEVELOPMENT_TARGETS


# --------------------------------------------------------------------------
# Decoding: positive
# --------------------------------------------------------------------------


def test_valid_shell_context_decodes_with_canonical_field_spelling() -> None:
    context = gen.ShellContext.from_wire(load("valid/shell-context.json"))
    assert context.context_id == "ctx-001"
    assert context.context_version == 1
    assert context.workspace_ref == "workspace-001"
    assert context.target == "desktop"
    assert context.expires_at is None
    assert context.to_wire() == load("valid/shell-context.json")


def test_valid_host_request_decodes_and_round_trips() -> None:
    request = gen.HostRequest.from_wire(load("valid/host-request.json"))
    assert request.namespace == "workspace"
    assert request.operation == "get_active"
    assert request.context_id == "ctx-001"
    assert request.payload == {}
    assert request.to_wire() == load("valid/host-request.json")


def test_valid_host_result_decodes_and_round_trips() -> None:
    result = gen.HostResult.from_wire(load("valid/host-result.json"))
    assert result.outcome == "success"
    assert result.data == {"routeId": "workspace.home"}
    assert result.denial is None
    assert result.availability is None
    assert result.failure is None
    assert result.to_wire() == load("valid/host-result.json")


@pytest.mark.parametrize(
    ("relative", "record"),
    [
        ("valid/shell-context.json", "ShellContext"),
        ("valid/host-request.json", "HostRequest"),
        ("valid/host-result.json", "HostResult"),
        ("valid/module-contribution.json", "ModuleContributionDeclaration"),
        ("valid/release-package-identity.json", "ReleasePackageIdentity"),
        ("valid/promotion-record.json", "PromotionRecord"),
        ("valid/rollback-record.json", "RollbackRecord"),
        ("valid/environment-binding.json", "EnvironmentBinding"),
        ("valid/development-profile.json", "DevelopmentProfile"),
        ("valid/shell-scenario.json", "ShellScenario"),
        ("valid/test-driver-request.json", "TestDriverRequest"),
        ("valid/test-driver-result.json", "TestDriverResult"),
        ("denial/permission-denied.json", "CapabilityDenial"),
        ("degradation/optional-capability-missing.json", "CompatibilityResult"),
    ],
)
def test_every_valid_fixture_round_trips_byte_for_byte(relative: str, record: str) -> None:
    document = load(relative)
    decoded = getattr(gen, record).from_wire(document)
    assert decoded.to_wire() == document


# --------------------------------------------------------------------------
# Decoding: fail closed
# --------------------------------------------------------------------------


def test_unknown_namespace_fails_closed() -> None:
    with pytest.raises(gen.HostContractDecodeError, match="unsupported_contract_value"):
        gen.HostRequest.from_wire(load("invalid/unknown-namespace.json"))


def test_unknown_availability_state_fails_closed_and_is_never_available() -> None:
    payload = {"state": "probably_fine", "capabilityId": "files.export", "target": "desktop"}
    with pytest.raises(gen.HostContractDecodeError, match="unsupported_contract_value"):
        gen.CapabilityAvailability.from_wire(payload)


def test_unknown_denial_category_fails_closed_and_surfaces_the_correlation_id() -> None:
    payload = dict(load("denial/permission-denied.json"))
    payload["category"] = "vendor_specific_refusal"
    with pytest.raises(gen.HostContractDecodeError) as excinfo:
        gen.CapabilityDenial.from_wire(payload)
    assert "unsupported_contract_value" in str(excinfo.value)


def test_unknown_outcome_fails_closed() -> None:
    payload = dict(load("valid/host-result.json"))
    payload["outcome"] = "partially_ok"
    with pytest.raises(gen.HostContractDecodeError, match="unsupported_contract_value"):
        gen.HostResult.from_wire(payload)


def test_development_profile_rejects_a_cloud_target() -> None:
    with pytest.raises(gen.HostContractDecodeError, match="unsupported_contract_value"):
        gen.DevelopmentProfile.from_wire(load("invalid/development-profile-cloud-target.json"))


# --------------------------------------------------------------------------
# Closed records: unknown fields, secrets, redaction
# --------------------------------------------------------------------------


def test_environment_binding_refuses_an_inline_secret() -> None:
    with pytest.raises(gen.HostContractDecodeError, match="rawToken"):
        gen.EnvironmentBinding.from_wire(load("invalid/environment-binding-inline-secret.json"))


def test_denial_refuses_an_unapproved_policy_detail_field() -> None:
    payload = dict(load("denial/permission-denied.json"))
    payload["policyExpression"] = "workspace.role == 'owner'"
    with pytest.raises(gen.HostContractDecodeError, match="policyExpression"):
        gen.CapabilityDenial.from_wire(payload)


def test_shell_context_is_closed_against_unknown_authority_fields() -> None:
    payload = dict(load("valid/shell-context.json"))
    payload["impersonatedPrincipalRef"] = "principal-admin"
    with pytest.raises(gen.HostContractDecodeError, match="impersonatedPrincipalRef"):
        gen.ShellContext.from_wire(payload)


def test_host_request_is_the_only_open_envelope() -> None:
    """The canonical schema leaves HostRequest open so intermediaries can forward
    safe unknown fields; every other record is closed.
    """
    assert gen.HostRequest.CLOSED is False
    for name in gen.HOST_CONTRACT_RECORDS:
        if name != "HostRequest":
            assert getattr(gen, name).CLOSED is True, name


def test_host_request_ignores_unknown_fields_rather_than_failing() -> None:
    payload = dict(load("valid/host-request.json"))
    payload["vendorHint"] = "ignore-me"
    request = gen.HostRequest.from_wire(payload)
    assert request.to_wire() == load("valid/host-request.json")


def test_the_open_envelope_does_not_document_an_exact_document_round_trip() -> None:
    """The dropped unknown field above is exactly what the emitter must not claim.

    ``to_wire`` reproduces the *original document* only for a closed record,
    where there is nothing a decode could have discarded. HostRequest is open,
    so a forwarded ``vendorHint`` is gone by the time the record exists and the
    emitted mapping is the declared fields alone. The two docstrings are pinned
    together so the open one cannot quietly inherit the closed one's promise.
    """
    open_doc = " ".join((gen.HostRequest.to_wire.__doc__ or "").split())
    closed_doc = " ".join((gen.ShellContext.to_wire.__doc__ or "").split())

    assert "reproduces the original document exactly" in closed_doc
    assert "reproduces the original document exactly" not in open_doc
    assert "reproduces every field this record declares exactly" in open_doc
    assert "this record is open" in open_doc

    payload = dict(load("valid/host-request.json"))
    payload["vendorHint"] = "ignore-me"
    assert gen.HostRequest.from_wire(payload).to_wire() != payload


def test_every_closed_record_still_documents_the_exact_document_round_trip() -> None:
    for name in gen.HOST_CONTRACT_RECORDS:
        record = getattr(gen, name)
        documentation = " ".join((record.to_wire.__doc__ or "").split())
        expected = (
            "reproduces the original document exactly"
            if record.CLOSED
            else "reproduces every field this record declares exactly"
        )
        assert expected in documentation, name


# --------------------------------------------------------------------------
# Duplicates and immutability
# --------------------------------------------------------------------------


def test_duplicate_identifiers_in_a_reference_array_are_refused() -> None:
    """The refusal names the index, never the repeated value: a reference array is
    exactly where a credential-shaped string would be smuggled.
    """
    payload = dict(load("valid/environment-binding.json"))
    payload["credentialRefs"] = ["credential.a", "credential.a"]
    with pytest.raises(gen.HostContractDecodeError) as excinfo:
        gen.EnvironmentBinding.from_wire(payload)
    assert "credentialRefs[1]: duplicate value" in str(excinfo.value)
    assert "credential.a" not in str(excinfo.value)


def test_duplicate_required_entitlements_are_refused() -> None:
    payload = json.loads(json.dumps(load("valid/module-contribution.json")))
    payload["requiredEntitlements"] = ["documents.use", "documents.use"]
    with pytest.raises(gen.HostContractDecodeError, match=r"requiredEntitlements\[1\]: duplicate"):
        gen.ModuleContributionDeclaration.from_wire(payload)


def test_a_repeated_development_port_is_refused() -> None:
    """``uniqueItems`` is enforced wherever the canonical schema declares it, so
    the refusal is not limited to identifier arrays.
    """
    payload = json.loads(json.dumps(load("valid/development-profile.json")))
    payload["ports"] = [4173, 4173]
    with pytest.raises(gen.HostContractDecodeError, match=r"ports\[1\]: duplicate"):
        gen.DevelopmentProfile.from_wire(payload)


def test_decoded_records_are_frozen() -> None:
    context = gen.ShellContext.from_wire(load("valid/shell-context.json"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.workspace_ref = "workspace-other"  # type: ignore[misc]


def test_decoded_opaque_payloads_are_deeply_immutable() -> None:
    request = gen.HostRequest.from_wire(
        {
            "contractVersion": "1.0.0",
            "requestId": "req-1",
            "namespace": "data",
            "operation": "read",
            "contextId": "ctx-001",
            "payload": {"filter": {"tags": ["a", "b"]}},
        }
    )
    with pytest.raises(TypeError):
        request.payload["filter"] = {}  # type: ignore[index]
    nested = request.payload["filter"]
    with pytest.raises(TypeError):
        nested["tags"] = []
    assert nested["tags"] == ("a", "b")


def test_decoded_arrays_are_tuples_not_lists() -> None:
    binding = gen.EnvironmentBinding.from_wire(load("valid/environment-binding.json"))
    assert isinstance(binding.credential_refs, tuple)
    assert binding.credential_refs == ("credential.documents.storage",)


# --------------------------------------------------------------------------
# Required arrays and absent-vs-empty
# --------------------------------------------------------------------------


def test_required_array_may_be_empty_but_may_not_be_absent() -> None:
    payload = dict(load("valid/environment-binding.json"))
    payload["integrationRefs"] = []
    assert gen.EnvironmentBinding.from_wire(payload).integration_refs == ()

    del payload["integrationRefs"]
    with pytest.raises(gen.HostContractDecodeError, match="integrationRefs"):
        gen.EnvironmentBinding.from_wire(payload)


def test_missing_required_scalar_is_refused() -> None:
    payload = dict(load("valid/shell-context.json"))
    del payload["workspaceRef"]
    with pytest.raises(gen.HostContractDecodeError, match="workspaceRef"):
        gen.ShellContext.from_wire(payload)


def test_wrongly_typed_value_is_refused() -> None:
    payload = dict(load("valid/shell-context.json"))
    payload["contextVersion"] = "1"
    with pytest.raises(gen.HostContractDecodeError, match="expected an integer"):
        gen.ShellContext.from_wire(payload)


def test_boolean_is_not_accepted_as_an_integer() -> None:
    payload = dict(load("valid/shell-context.json"))
    payload["contextVersion"] = True
    with pytest.raises(gen.HostContractDecodeError, match="expected an integer"):
        gen.ShellContext.from_wire(payload)


# --------------------------------------------------------------------------
# Canonical field spelling
# --------------------------------------------------------------------------


def test_wire_field_sets_preserve_the_canonical_camel_case_spelling() -> None:
    assert gen.ShellContext.WIRE_FIELDS == frozenset(
        {
            "contextId",
            "contextVersion",
            "principalRef",
            "organisationRef",
            "workspaceRef",
            "entitlementSummaryRef",
            "permissionSummaryRef",
            "runtimeId",
            "environmentId",
            "target",
            "issuedAt",
            "expiresAt",
        }
    )
    assert gen.EnvironmentBinding.WIRE_FIELDS == frozenset(
        {
            "bindingId",
            "bindingVersion",
            "releaseId",
            "environmentId",
            "workspaceId",
            "target",
            "credentialRefs",
            "integrationRefs",
            "storageRefs",
            "domainRefs",
            "dataResidencyPolicyRef",
            "runtimeLimitsRef",
            "executionPlacementRef",
            "createdAt",
            "actorRef",
        }
    )


def test_organisation_keeps_the_canonical_british_spelling() -> None:
    assert "organisationRef" in gen.ShellContext.WIRE_FIELDS
    assert "organizationRef" not in gen.ShellContext.WIRE_FIELDS
    assert "organisation" in gen.HOST_NAMESPACES
