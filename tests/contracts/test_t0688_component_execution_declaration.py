"""Adversarial tests for the T-0688 IP-10 `ComponentExecutionPackageDeclaration` lane.

Independently authored OmniVia examples throughout; no external source, schema, fixture,
or test is read or reused. The frozen G3 fixture manifest under `tests/contracts/fixtures`
is not touched by this suite.
"""

from __future__ import annotations

import copy
from types import MappingProxyType
from typing import Any

import pytest

from omnivia_core.contracts.v1 import semantics_component as component
from omnivia_core.contracts.v1.compatibility import ContractSemanticError

_DIGEST = "sha256:" + "b" * 64
_REQUIRED_DIMENSIONS = (
    "declarationSchemaVersion",
    "componentId",
    "metadata",
    "ports",
    "configurationSchema",
    "implementationDigest",
    "tests",
    "executionPlane",
    "credentialRequirements",
    "networkRequirements",
    "destinationRequirements",
    "effectClass",
    "evidenceObligations",
    "resourceRequirements",
)


def _port(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "portId": "port-t0688-lantern-1",
        "direction": "input",
        "semanticType": "lantern-cargo-manifest",
        "physicalSchema": {"schemaRef": "schema-t0688-lantern-1"},
        "cardinality": "single",
        "presence": "present",
        "classification": {"level": "internal"},
        "lineage": {"sourceRef": "component-t0688-lantern-1"},
    }
    record.update(overrides)
    return record


def _metadata(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "name": "lantern-ingest",
        "description": "Ingests lantern manifests into the ledger.",
        "owner": "team-lantern",
        "category": "ingestion",
        "version": "1.0.0",
    }
    record.update(overrides)
    return record


def _configuration_schema(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"schemaRef": "config-schema-t0688-lantern-1"}
    record.update(overrides)
    return record


def _tests(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "conformanceTestIds": ["test-lantern-1", "test-lantern-2"],
        "recordedOutcomeRef": "outcome-t0688-lantern-1",
    }
    record.update(overrides)
    return record


def _credential_requirement(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "credentialClass": "service-account",
        "permission": "read-only",
        "acquisitionPath": "vault://lantern/service-account",
    }
    record.update(overrides)
    return record


def _network_requirement(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"protocolClass": "https", "purpose": "manifest-fetch"}
    record.update(overrides)
    return record


def _destination_requirement(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"destinationClass": "internal-ledger"}
    record.update(overrides)
    return record


def _evidence_obligation(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"evidenceClass": "ingest-receipt"}
    record.update(overrides)
    return record


def _resource_requirement(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "resourceClass": "queue",
        "lifecycleExpectations": ["creation", "destruction"],
        "satisfiedByExistingGovernedResource": False,
    }
    record.update(overrides)
    return record


def _declaration(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "declarationSchemaVersion": "1.0.0",
        "componentId": "component-t0688-lantern-1",
        "metadata": _metadata(),
        "ports": [_port()],
        "configurationSchema": _configuration_schema(),
        "implementationDigest": _DIGEST,
        "tests": _tests(),
        "executionPlane": "worker",
        "credentialRequirements": [_credential_requirement()],
        "networkRequirements": [_network_requirement()],
        "destinationRequirements": [_destination_requirement()],
        "effectClass": "externalEffect",
        "evidenceObligations": [_evidence_obligation()],
        "resourceRequirements": [_resource_requirement()],
    }
    record.update(overrides)
    return record


# --------------------------------------------------------------------------
# Valid declaration: full closed record, every plane, every effect class
# --------------------------------------------------------------------------


def test_full_valid_declaration_validates() -> None:
    component.validate_component_execution_package_declaration(_declaration())


@pytest.mark.parametrize("plane", component.EXECUTION_PLANES)
def test_every_execution_plane_validates(plane: str) -> None:
    component.validate_component_execution_package_declaration(
        _declaration(executionPlane=plane)
    )


@pytest.mark.parametrize("effect_class", component.EFFECT_CLASSES)
def test_every_effect_class_validates(effect_class: str) -> None:
    component.validate_component_execution_package_declaration(
        _declaration(effectClass=effect_class)
    )


# --------------------------------------------------------------------------
# Missing top-level dimensions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("dimension", _REQUIRED_DIMENSIONS)
def test_missing_top_level_dimension_rejected(dimension: str) -> None:
    declaration = _declaration()
    del declaration[dimension]
    with pytest.raises(
        ContractSemanticError, match=rf"{dimension}: missing required field"
    ):
        component.validate_component_execution_package_declaration(declaration)


@pytest.mark.parametrize("dimension", _REQUIRED_DIMENSIONS)
def test_missing_top_level_dimension_readiness_diagnostic_names_it(
    dimension: str,
) -> None:
    declaration = _declaration()
    del declaration[dimension]
    result = component.evaluate_component_publication_readiness(
        declaration, "R0", "new"
    )
    assert len(result["diagnostics"]) == 1
    diagnostic = result["diagnostics"][0]
    assert diagnostic["code"] == component.COMPONENT_EXECUTION_DECLARATION_INCOMPLETE
    assert diagnostic["dimension"] == dimension
    assert diagnostic["componentId"] == declaration.get("componentId")


# --------------------------------------------------------------------------
# Unknown fields, malformed/nonmapping/empty nested fields
# --------------------------------------------------------------------------


def test_unknown_top_level_field_rejected() -> None:
    declaration = _declaration(unexpectedField="nope")
    with pytest.raises(ContractSemanticError, match="unexpectedField: unknown field"):
        component.validate_component_execution_package_declaration(declaration)


def test_unknown_nested_metadata_field_rejected() -> None:
    declaration = _declaration(metadata=_metadata(extra="nope"))
    with pytest.raises(ContractSemanticError, match="metadata.extra: unknown field"):
        component.validate_component_execution_package_declaration(declaration)


def test_unknown_nested_credential_requirement_field_rejected() -> None:
    declaration = _declaration(
        credentialRequirements=[_credential_requirement(extra="nope")]
    )
    with pytest.raises(ContractSemanticError, match="unknown field"):
        component.validate_component_execution_package_declaration(declaration)


@pytest.mark.parametrize(
    "field",
    ["metadata", "configurationSchema", "tests"],
)
def test_nonmapping_nested_field_rejected(field: str) -> None:
    declaration = _declaration(**{field: "not-a-mapping"})
    with pytest.raises(ContractSemanticError, match="expected a mapping"):
        component.validate_component_execution_package_declaration(declaration)


@pytest.mark.parametrize(
    "field",
    ["metadata", "configurationSchema", "tests"],
)
def test_empty_mapping_nested_field_rejected(field: str) -> None:
    declaration = _declaration(**{field: {}})
    with pytest.raises(ContractSemanticError, match="missing required field"):
        component.validate_component_execution_package_declaration(declaration)


@pytest.mark.parametrize(
    "field",
    [
        "credentialRequirements",
        "networkRequirements",
        "destinationRequirements",
        "evidenceObligations",
        "resourceRequirements",
    ],
)
def test_malformed_requirement_array_shape_rejected(field: str) -> None:
    declaration = _declaration(**{field: "not-an-array"})
    with pytest.raises(ContractSemanticError, match="must be an ordered array"):
        component.validate_component_execution_package_declaration(declaration)


@pytest.mark.parametrize(
    "field, entry_builder",
    [
        ("credentialRequirements", _credential_requirement),
        ("networkRequirements", _network_requirement),
        ("destinationRequirements", _destination_requirement),
        ("evidenceObligations", _evidence_obligation),
        ("resourceRequirements", _resource_requirement),
    ],
)
def test_nonmapping_requirement_entry_rejected(field: str, entry_builder: Any) -> None:
    declaration = _declaration(**{field: ["not-a-mapping"]})
    with pytest.raises(ContractSemanticError, match="expected a mapping"):
        component.validate_component_execution_package_declaration(declaration)


@pytest.mark.parametrize(
    "field",
    [
        "credentialRequirements",
        "networkRequirements",
        "destinationRequirements",
        "evidenceObligations",
        "resourceRequirements",
    ],
)
def test_empty_mapping_requirement_entry_rejected(field: str) -> None:
    declaration = _declaration(**{field: [{}]})
    with pytest.raises(ContractSemanticError, match="missing required field"):
        component.validate_component_execution_package_declaration(declaration)


# --------------------------------------------------------------------------
# Bad versions / digest / identifiers
# --------------------------------------------------------------------------


def test_bad_declaration_schema_version_rejected() -> None:
    declaration = _declaration(declarationSchemaVersion="not-a-version")
    with pytest.raises(ContractSemanticError, match="declarationSchemaVersion"):
        component.validate_component_execution_package_declaration(declaration)


def test_bad_component_id_rejected() -> None:
    declaration = _declaration(componentId="has a space")
    with pytest.raises(ContractSemanticError, match="componentId"):
        component.validate_component_execution_package_declaration(declaration)


def test_bad_metadata_version_rejected() -> None:
    declaration = _declaration(metadata=_metadata(version="v1"))
    with pytest.raises(ContractSemanticError, match="version"):
        component.validate_component_execution_package_declaration(declaration)


def test_bad_implementation_digest_rejected() -> None:
    declaration = _declaration(implementationDigest="md5:notasha256digest")
    with pytest.raises(ContractSemanticError, match="implementationDigest"):
        component.validate_component_execution_package_declaration(declaration)


# --------------------------------------------------------------------------
# Invalid enums
# --------------------------------------------------------------------------


def test_invalid_execution_plane_rejected() -> None:
    declaration = _declaration(executionPlane="mainframe")
    with pytest.raises(ContractSemanticError, match="executionPlane"):
        component.validate_component_execution_package_declaration(declaration)


def test_invalid_effect_class_rejected() -> None:
    declaration = _declaration(effectClass="sideEffect")
    with pytest.raises(ContractSemanticError, match="effectClass"):
        component.validate_component_execution_package_declaration(declaration)


def test_invalid_lifecycle_expectation_rejected() -> None:
    declaration = _declaration(
        resourceRequirements=[
            _resource_requirement(lifecycleExpectations=["teleportation"])
        ]
    )
    with pytest.raises(ContractSemanticError, match="lifecycleExpectations"):
        component.validate_component_execution_package_declaration(declaration)


# --------------------------------------------------------------------------
# Duplicate test IDs
# --------------------------------------------------------------------------


def test_duplicate_conformance_test_ids_rejected() -> None:
    declaration = _declaration(
        tests=_tests(conformanceTestIds=["test-lantern-1", "test-lantern-1"])
    )
    with pytest.raises(ContractSemanticError, match="must not repeat"):
        component.validate_component_execution_package_declaration(declaration)


def test_empty_conformance_test_ids_rejected() -> None:
    declaration = _declaration(tests=_tests(conformanceTestIds=[]))
    with pytest.raises(ContractSemanticError, match="must not be empty"):
        component.validate_component_execution_package_declaration(declaration)


# --------------------------------------------------------------------------
# Invalid ports
# --------------------------------------------------------------------------


def test_duplicate_port_ids_rejected() -> None:
    declaration = _declaration(ports=[_port(), _port()])
    with pytest.raises(ContractSemanticError, match="repeat a portId"):
        component.validate_component_execution_package_declaration(declaration)


def test_multiple_driver_input_ports_rejected() -> None:
    declaration = _declaration(
        ports=[
            _port(portId="port-t0688-lantern-1", driver=True),
            _port(portId="port-t0688-lantern-2", driver=True),
        ]
    )
    with pytest.raises(ContractSemanticError, match="at most one input port"):
        component.validate_component_execution_package_declaration(declaration)


def test_empty_ports_array_rejected() -> None:
    """The shared DOC-004 AD.4 ComponentPortSet contract is non-empty."""
    declaration = _declaration(ports=[])
    with pytest.raises(ContractSemanticError, match="must not be empty"):
        component.validate_component_execution_package_declaration(declaration)


def test_malformed_port_entry_rejected() -> None:
    declaration = _declaration(ports=[_port(direction="sideways")])
    with pytest.raises(ContractSemanticError, match="direction"):
        component.validate_component_execution_package_declaration(declaration)


# --------------------------------------------------------------------------
# Malformed requirement entries
# --------------------------------------------------------------------------


def test_malformed_credential_requirement_missing_field_rejected() -> None:
    declaration = _declaration(
        credentialRequirements=[
            {"credentialClass": "service-account", "permission": "read-only"}
        ]
    )
    with pytest.raises(
        ContractSemanticError, match="acquisitionPath: missing required field"
    ):
        component.validate_component_execution_package_declaration(declaration)


def test_malformed_network_requirement_wrong_type_rejected() -> None:
    declaration = _declaration(
        networkRequirements=[_network_requirement(protocolClass=123)]
    )
    with pytest.raises(ContractSemanticError, match="protocolClass"):
        component.validate_component_execution_package_declaration(declaration)


def test_malformed_destination_requirement_empty_string_rejected() -> None:
    declaration = _declaration(
        destinationRequirements=[_destination_requirement(destinationClass="")]
    )
    with pytest.raises(ContractSemanticError, match="destinationClass"):
        component.validate_component_execution_package_declaration(declaration)


def test_malformed_evidence_obligation_boolean_rejected() -> None:
    declaration = _declaration(
        evidenceObligations=[_evidence_obligation(evidenceClass=True)]
    )
    with pytest.raises(ContractSemanticError, match="evidenceClass"):
        component.validate_component_execution_package_declaration(declaration)


def test_malformed_resource_requirement_missing_satisfied_flag_rejected() -> None:
    declaration = _declaration(
        resourceRequirements=[
            {
                "resourceClass": "queue",
                "lifecycleExpectations": ["creation"],
            }
        ]
    )
    with pytest.raises(
        ContractSemanticError,
        match="satisfiedByExistingGovernedResource: missing required field",
    ):
        component.validate_component_execution_package_declaration(declaration)


def test_resource_requirement_satisfied_flag_must_be_boolean() -> None:
    declaration = _declaration(
        resourceRequirements=[
            _resource_requirement(satisfiedByExistingGovernedResource="yes")
        ]
    )
    with pytest.raises(
        ContractSemanticError, match="satisfiedByExistingGovernedResource"
    ):
        component.validate_component_execution_package_declaration(declaration)


# --------------------------------------------------------------------------
# Lifecycle expectations: duplicate / empty / invalid
# --------------------------------------------------------------------------


def test_duplicate_lifecycle_expectations_rejected() -> None:
    declaration = _declaration(
        resourceRequirements=[
            _resource_requirement(lifecycleExpectations=["creation", "creation"])
        ]
    )
    with pytest.raises(ContractSemanticError, match="must not repeat"):
        component.validate_component_execution_package_declaration(declaration)


def test_empty_lifecycle_expectations_rejected() -> None:
    declaration = _declaration(
        resourceRequirements=[_resource_requirement(lifecycleExpectations=[])]
    )
    with pytest.raises(
        ContractSemanticError, match="lifecycleExpectations must not be empty"
    ):
        component.validate_component_execution_package_declaration(declaration)


@pytest.mark.parametrize("action", component.RESOURCE_LIFECYCLE_ACTIONS)
def test_every_lifecycle_action_individually_validates(action: str) -> None:
    declaration = _declaration(
        resourceRequirements=[_resource_requirement(lifecycleExpectations=[action])]
    )
    component.validate_component_execution_package_declaration(declaration)


# --------------------------------------------------------------------------
# Literal empty arrays: positive declarations of none
# --------------------------------------------------------------------------


def test_empty_requirement_evidence_resource_arrays_accepted() -> None:
    declaration = _declaration(
        credentialRequirements=[],
        networkRequirements=[],
        destinationRequirements=[],
        evidenceObligations=[],
        resourceRequirements=[],
        effectClass="pure",
    )
    component.validate_component_execution_package_declaration(declaration)


# --------------------------------------------------------------------------
# Publication readiness: R0 never blocks; R1 blocks new/republished only
# --------------------------------------------------------------------------


def test_r0_never_blocks_regardless_of_posture() -> None:
    invalid_declaration = _declaration()
    del invalid_declaration["metadata"]
    for posture in component.PUBLICATION_POSTURES:
        result = component.evaluate_component_publication_readiness(
            invalid_declaration, "R0", posture
        )
        assert result["publicationBlocked"] is False
        assert len(result["diagnostics"]) == 1


def test_r1_blocks_new_posture_on_incomplete_declaration() -> None:
    invalid_declaration = _declaration()
    del invalid_declaration["metadata"]
    result = component.evaluate_component_publication_readiness(
        invalid_declaration, "R1", "new"
    )
    assert result["publicationBlocked"] is True


def test_r1_blocks_republished_posture_on_incomplete_declaration() -> None:
    invalid_declaration = _declaration()
    del invalid_declaration["metadata"]
    result = component.evaluate_component_publication_readiness(
        invalid_declaration, "R1", "republished"
    )
    assert result["publicationBlocked"] is True


def test_r1_does_not_block_already_published_posture() -> None:
    invalid_declaration = _declaration()
    del invalid_declaration["metadata"]
    result = component.evaluate_component_publication_readiness(
        invalid_declaration, "R1", "alreadyPublished"
    )
    assert result["publicationBlocked"] is False
    assert len(result["diagnostics"]) == 1


def test_valid_declaration_never_blocked_at_any_stage_or_posture() -> None:
    declaration = _declaration()
    for stage in component.PUBLICATION_ROLLOUT_STAGES:
        for posture in component.PUBLICATION_POSTURES:
            result = component.evaluate_component_publication_readiness(
                declaration, stage, posture
            )
            assert result["publicationBlocked"] is False
            assert result["diagnostics"] == ()


def test_invalid_rollout_stage_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="rollout_stage"):
        component.evaluate_component_publication_readiness(_declaration(), "R2", "new")


def test_invalid_publication_posture_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="publication_posture"):
        component.evaluate_component_publication_readiness(
            _declaration(), "R0", "everPublished"
        )


# --------------------------------------------------------------------------
# Execution admission: Permission always required; declaration never grants it;
# Capability Gateway required for credential/network/destination/externalEffect
# --------------------------------------------------------------------------


def test_admission_requires_permission_even_when_gateway_allowed() -> None:
    declaration = _declaration(
        credentialRequirements=[],
        networkRequirements=[],
        destinationRequirements=[],
        effectClass="pure",
    )
    result = component.evaluate_component_execution_admission(declaration, False, True)
    assert result["admitted"] is False
    assert result["permissionRequired"] is True
    assert result["executed"] is False


def test_declaration_alone_never_grants_permission() -> None:
    declaration = _declaration(
        credentialRequirements=[],
        networkRequirements=[],
        destinationRequirements=[],
        effectClass="pure",
    )
    result = component.evaluate_component_execution_admission(declaration, False, False)
    assert result["admitted"] is False


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "credentialRequirements": [_credential_requirement()],
            "networkRequirements": [],
            "destinationRequirements": [],
            "effectClass": "pure",
        },
        {
            "credentialRequirements": [],
            "networkRequirements": [_network_requirement()],
            "destinationRequirements": [],
            "effectClass": "pure",
        },
        {
            "credentialRequirements": [],
            "networkRequirements": [],
            "destinationRequirements": [_destination_requirement()],
            "effectClass": "pure",
        },
        {
            "credentialRequirements": [],
            "networkRequirements": [],
            "destinationRequirements": [],
            "effectClass": "externalEffect",
        },
    ],
)
def test_gateway_required_for_each_reach_out_surface(overrides: dict[str, Any]) -> None:
    declaration = _declaration(**overrides)
    result = component.evaluate_component_execution_admission(declaration, True, False)
    assert result["capabilityGatewayRequired"] is True
    assert result["admitted"] is False

    admitted_result = component.evaluate_component_execution_admission(
        declaration, True, True
    )
    assert admitted_result["admitted"] is True
    assert admitted_result["executed"] is False


def test_gateway_not_required_when_no_reach_out_surface_declared() -> None:
    declaration = _declaration(
        credentialRequirements=[],
        networkRequirements=[],
        destinationRequirements=[],
        effectClass="pure",
    )
    result = component.evaluate_component_execution_admission(declaration, True, False)
    assert result["capabilityGatewayRequired"] is False
    assert result["admitted"] is True
    assert result["executed"] is False


def test_admission_result_always_says_executed_false() -> None:
    declaration = _declaration()
    for permission in (True, False):
        for gateway in (True, False):
            result = component.evaluate_component_execution_admission(
                declaration, permission, gateway
            )
            assert result["executed"] is False


def test_admission_validates_declaration_first() -> None:
    invalid_declaration = _declaration()
    del invalid_declaration["metadata"]
    with pytest.raises(ContractSemanticError):
        component.evaluate_component_execution_admission(
            invalid_declaration, True, True
        )


def test_admission_rejects_nonboolean_permission_allowed() -> None:
    with pytest.raises(
        ContractSemanticError, match="permission_allowed must be a boolean"
    ):
        component.evaluate_component_execution_admission(_declaration(), "yes", True)


def test_admission_rejects_nonboolean_capability_gateway_allowed() -> None:
    with pytest.raises(
        ContractSemanticError, match="capability_gateway_allowed must be a boolean"
    ):
        component.evaluate_component_execution_admission(_declaration(), True, "yes")


# --------------------------------------------------------------------------
# No-hidden-provisioning: every lifecycle action needs an explicit, separate
# request, and only proceeds when approved, attributable, and observable too.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", component.RESOURCE_LIFECYCLE_ACTIONS)
def test_lifecycle_request_admitted_when_all_flags_true(action: str) -> None:
    result = component.evaluate_resource_lifecycle_request(
        action, True, True, True, True
    )
    assert result["admitted"] is True
    assert result["provisioned"] is False
    assert result["action"] == action


@pytest.mark.parametrize(
    "flags",
    [
        (False, True, True, True),
        (True, False, True, True),
        (True, True, False, True),
        (True, True, True, False),
    ],
)
def test_lifecycle_request_refused_when_any_single_flag_false(
    flags: tuple[bool, bool, bool, bool],
) -> None:
    explicit_request, approved, attributable, observable = flags
    result = component.evaluate_resource_lifecycle_request(
        "creation", explicit_request, approved, attributable, observable
    )
    assert result["admitted"] is False
    assert result["provisioned"] is False


def test_implicit_request_refused_outright_even_if_everything_else_true() -> None:
    result = component.evaluate_resource_lifecycle_request(
        "destruction", False, True, True, True
    )
    assert result["admitted"] is False
    assert result["provisioned"] is False


def test_lifecycle_request_result_always_says_provisioned_false() -> None:
    for explicit in (True, False):
        result = component.evaluate_resource_lifecycle_request(
            "scaling", explicit, True, True, True
        )
        assert result["provisioned"] is False


def test_lifecycle_request_invalid_action_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="action must be one of"):
        component.evaluate_resource_lifecycle_request(
            "teleportation", True, True, True, True
        )


@pytest.mark.parametrize("field_index", [0, 1, 2, 3])
def test_lifecycle_request_nonboolean_flag_rejected(field_index: int) -> None:
    flags = [True, True, True, True]
    flags[field_index] = "yes"  # type: ignore[list-item]
    with pytest.raises(ContractSemanticError, match="must be a boolean"):
        component.evaluate_resource_lifecycle_request("reconfiguration", *flags)


# --------------------------------------------------------------------------
# Immutability and determinism
# --------------------------------------------------------------------------


def test_readiness_diagnostics_are_immutable() -> None:
    invalid_declaration = _declaration()
    del invalid_declaration["metadata"]
    result = component.evaluate_component_publication_readiness(
        invalid_declaration, "R0", "new"
    )
    assert isinstance(result, MappingProxyType)
    assert isinstance(result["diagnostics"][0], MappingProxyType)
    with pytest.raises(TypeError):
        result["publicationBlocked"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        result["diagnostics"][0]["dimension"] = "tampered"  # type: ignore[index]


def test_admission_result_is_immutable() -> None:
    result = component.evaluate_component_execution_admission(
        _declaration(), True, True
    )
    assert isinstance(result, MappingProxyType)
    with pytest.raises(TypeError):
        result["admitted"] = False  # type: ignore[index]


def test_lifecycle_request_result_is_immutable() -> None:
    result = component.evaluate_resource_lifecycle_request(
        "creation", True, True, True, True
    )
    assert isinstance(result, MappingProxyType)
    with pytest.raises(TypeError):
        result["provisioned"] = True  # type: ignore[index]


def test_readiness_is_deterministic_across_repeated_calls() -> None:
    invalid_declaration = _declaration()
    del invalid_declaration["metadata"]
    first = component.evaluate_component_publication_readiness(
        invalid_declaration, "R1", "new"
    )
    second = component.evaluate_component_publication_readiness(
        invalid_declaration, "R1", "new"
    )
    assert dict(first) == dict(second)
    assert dict(first["diagnostics"][0]) == dict(second["diagnostics"][0])


def test_admission_is_deterministic_across_repeated_calls() -> None:
    declaration = _declaration()
    first = component.evaluate_component_execution_admission(declaration, True, True)
    second = component.evaluate_component_execution_admission(declaration, True, True)
    assert dict(first) == dict(second)


def test_lifecycle_request_is_deterministic_across_repeated_calls() -> None:
    first = component.evaluate_resource_lifecycle_request(
        "suspension", True, True, True, True
    )
    second = component.evaluate_resource_lifecycle_request(
        "suspension", True, True, True, True
    )
    assert dict(first) == dict(second)


def test_validate_does_not_mutate_input() -> None:
    declaration = _declaration()
    snapshot = copy.deepcopy(declaration)
    component.validate_component_execution_package_declaration(declaration)
    assert declaration == snapshot
