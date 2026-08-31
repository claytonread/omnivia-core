"""Pure semantic validation for the T-0688 IP-10 Component execution declaration.

A `ComponentExecutionPackageDeclaration` is a closed, static record a Component author
publishes: what it is, how it is versioned, which execution plane it runs on, and which
credential/network/destination/evidence/resource surfaces it *may* touch. Declaring a
surface is never the same as being allowed to use it at call time -- this module keeps
that separation explicit in every public function:

- :func:`validate_component_execution_package_declaration` only checks the declaration
  is well-formed. It grants nothing.
- :func:`evaluate_component_publication_readiness` only diagnoses whether the
  declaration is fit to publish at a given rollout stage/posture. It performs no
  publication.
- :func:`evaluate_component_execution_admission` decides whether a call may proceed. A
  declaration can never substitute for live Permission, and it never performs execution.
- :func:`evaluate_resource_lifecycle_request` decides whether a lifecycle action on a
  resource may proceed. It never performs provisioning.

Standard library only. Nothing here may depend on runtime, storage, HTTP, MCP, CLI,
Platform, Dev, or a validation framework, and this module never imports or is imported
by the legacy `omnivia_core.component_contract` package.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final

from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    is_content_checksum,
    is_identifier,
    is_release_version,
)
from omnivia_core.contracts.v1.semantics_workflow import validate_component_port_set

__all__ = [
    "COMPONENT_EXECUTION_DECLARATION_INCOMPLETE",
    "EFFECT_CLASSES",
    "EXECUTION_PLANES",
    "PUBLICATION_POSTURES",
    "PUBLICATION_ROLLOUT_STAGES",
    "RESOURCE_LIFECYCLE_ACTIONS",
    "evaluate_component_execution_admission",
    "evaluate_component_publication_readiness",
    "evaluate_resource_lifecycle_request",
    "validate_component_execution_package_declaration",
]

COMPONENT_EXECUTION_DECLARATION_INCOMPLETE: Final = (
    "COMPONENT_EXECUTION_DECLARATION_INCOMPLETE"
)

EXECUTION_PLANES: Final[tuple[str, ...]] = (
    "scheduler",
    "listener",
    "resourceSupervisor",
    "worker",
    "capabilityGateway",
)
EFFECT_CLASSES: Final[tuple[str, ...]] = (
    "pure",
    "recomputable",
    "snapshotBoundRead",
    "internalWrite",
    "externalEffect",
)
PUBLICATION_ROLLOUT_STAGES: Final[tuple[str, ...]] = ("R0", "R1")
PUBLICATION_POSTURES: Final[tuple[str, ...]] = (
    "alreadyPublished",
    "new",
    "republished",
)
RESOURCE_LIFECYCLE_ACTIONS: Final[tuple[str, ...]] = (
    "creation",
    "scaling",
    "reconfiguration",
    "suspension",
    "destruction",
)

_DECLARATION_FIELDS: Final = frozenset(
    {
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
    }
)
_METADATA_FIELDS: Final = frozenset(
    {"name", "description", "owner", "category", "version"}
)
_CONFIGURATION_SCHEMA_FIELDS: Final = frozenset({"schemaRef"})
_TESTS_FIELDS: Final = frozenset({"conformanceTestIds", "recordedOutcomeRef"})
_CREDENTIAL_FIELDS: Final = frozenset(
    {"credentialClass", "permission", "acquisitionPath"}
)
_NETWORK_FIELDS: Final = frozenset({"protocolClass", "purpose"})
_DESTINATION_FIELDS: Final = frozenset({"destinationClass"})
_EVIDENCE_FIELDS: Final = frozenset({"evidenceClass"})
_RESOURCE_FIELDS: Final = frozenset(
    {"resourceClass", "lifecycleExpectations", "satisfiedByExistingGovernedResource"}
)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    raise ContractSemanticError(f"{label}: expected a mapping")


def _only_fields(
    fields: Mapping[str, object], allowed: frozenset[str], label: str
) -> None:
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise ContractSemanticError(f"{label}.{unknown[0]}: unknown field")
    missing = sorted(allowed - set(fields))
    if missing:
        raise ContractSemanticError(f"{label}.{missing[0]}: missing required field")


def _non_empty_string(fields: Mapping[str, object], key: str, label: str) -> str:
    value = fields.get(key)
    if isinstance(value, bool) or not isinstance(value, str) or not value:
        raise ContractSemanticError(f"{label}: {key} is not a non-empty string")
    return value


def _release_version(fields: Mapping[str, object], key: str, label: str) -> str:
    value = fields.get(key)
    if not is_release_version(value):
        raise ContractSemanticError(
            f"{label}: {key} is not a well-formed semantic version"
        )
    assert isinstance(value, str)
    return value


def _identifier(fields: Mapping[str, object], key: str, label: str) -> str:
    value = fields.get(key)
    if not is_identifier(value):
        raise ContractSemanticError(f"{label}: {key} is not a well-formed Identifier")
    assert isinstance(value, str)
    return value


def _digest(fields: Mapping[str, object], key: str, label: str) -> str:
    value = fields.get(key)
    if not is_content_checksum(value):
        raise ContractSemanticError(
            f"{label}: {key} is not a well-formed sha256 ContentChecksum"
        )
    assert isinstance(value, str)
    return value


def _boolean(fields: Mapping[str, object], key: str, label: str) -> bool:
    value = fields.get(key)
    if not isinstance(value, bool):
        raise ContractSemanticError(f"{label}: {key} is not a boolean")
    return value


def _member(
    fields: Mapping[str, object], key: str, label: str, allowed: tuple[str, ...]
) -> str:
    value = fields.get(key)
    if value not in allowed:
        raise ContractSemanticError(f"{label}: {key} must be one of {allowed!r}")
    assert isinstance(value, str)
    return value


def _ordered_array(
    fields: Mapping[str, object], key: str, label: str
) -> Sequence[object]:
    """Require an ordered array field; `[]` is a positive declaration of none."""
    value = fields.get(key)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractSemanticError(f"{label}: {key} must be an ordered array")
    return value


def _unique_identifier_array(
    fields: Mapping[str, object], key: str, label: str
) -> Sequence[str]:
    values = _ordered_array(fields, key, label)
    if not values:
        raise ContractSemanticError(f"{label}: {key} must not be empty")
    identifiers: list[str] = []
    for index, item in enumerate(values):
        if not is_identifier(item):
            raise ContractSemanticError(
                f"{label}: {key}[{index}] is not a well-formed Identifier"
            )
        assert isinstance(item, str)
        identifiers.append(item)
    if len(identifiers) != len(set(identifiers)):
        raise ContractSemanticError(f"{label}: {key} must not repeat an entry")
    return tuple(identifiers)


def _validate_metadata(value: object, label: str) -> None:
    fields = _mapping(value, label)
    _only_fields(fields, _METADATA_FIELDS, label)
    _non_empty_string(fields, "name", label)
    _non_empty_string(fields, "description", label)
    _non_empty_string(fields, "owner", label)
    _non_empty_string(fields, "category", label)
    _release_version(fields, "version", label)


def _validate_configuration_schema(value: object, label: str) -> None:
    fields = _mapping(value, label)
    _only_fields(fields, _CONFIGURATION_SCHEMA_FIELDS, label)
    _non_empty_string(fields, "schemaRef", label)


def _validate_tests(value: object, label: str) -> None:
    fields = _mapping(value, label)
    _only_fields(fields, _TESTS_FIELDS, label)
    _unique_identifier_array(fields, "conformanceTestIds", label)
    _non_empty_string(fields, "recordedOutcomeRef", label)


def _validate_credential_requirements(value: object, label: str) -> None:
    for index, entry in enumerate(
        _ordered_array(
            {"credentialRequirements": value}, "credentialRequirements", label
        )
    ):
        entry_label = f"{label}[{index}]"
        fields = _mapping(entry, entry_label)
        _only_fields(fields, _CREDENTIAL_FIELDS, entry_label)
        _non_empty_string(fields, "credentialClass", entry_label)
        _non_empty_string(fields, "permission", entry_label)
        _non_empty_string(fields, "acquisitionPath", entry_label)


def _validate_network_requirements(value: object, label: str) -> None:
    for index, entry in enumerate(
        _ordered_array({"networkRequirements": value}, "networkRequirements", label)
    ):
        entry_label = f"{label}[{index}]"
        fields = _mapping(entry, entry_label)
        _only_fields(fields, _NETWORK_FIELDS, entry_label)
        _non_empty_string(fields, "protocolClass", entry_label)
        _non_empty_string(fields, "purpose", entry_label)


def _validate_destination_requirements(value: object, label: str) -> None:
    for index, entry in enumerate(
        _ordered_array(
            {"destinationRequirements": value}, "destinationRequirements", label
        )
    ):
        entry_label = f"{label}[{index}]"
        fields = _mapping(entry, entry_label)
        _only_fields(fields, _DESTINATION_FIELDS, entry_label)
        _non_empty_string(fields, "destinationClass", entry_label)


def _validate_evidence_obligations(value: object, label: str) -> None:
    for index, entry in enumerate(
        _ordered_array({"evidenceObligations": value}, "evidenceObligations", label)
    ):
        entry_label = f"{label}[{index}]"
        fields = _mapping(entry, entry_label)
        _only_fields(fields, _EVIDENCE_FIELDS, entry_label)
        _non_empty_string(fields, "evidenceClass", entry_label)


def _validate_resource_requirements(value: object, label: str) -> None:
    for index, entry in enumerate(
        _ordered_array({"resourceRequirements": value}, "resourceRequirements", label)
    ):
        entry_label = f"{label}[{index}]"
        fields = _mapping(entry, entry_label)
        _only_fields(fields, _RESOURCE_FIELDS, entry_label)
        _non_empty_string(fields, "resourceClass", entry_label)
        lifecycle = _ordered_array(fields, "lifecycleExpectations", entry_label)
        if not lifecycle:
            raise ContractSemanticError(
                f"{entry_label}: lifecycleExpectations must not be empty"
            )
        seen: list[str] = []
        for action_index, action in enumerate(lifecycle):
            if action not in RESOURCE_LIFECYCLE_ACTIONS:
                raise ContractSemanticError(
                    f"{entry_label}: lifecycleExpectations[{action_index}] must be one of "
                    f"{RESOURCE_LIFECYCLE_ACTIONS!r}"
                )
            assert isinstance(action, str)
            seen.append(action)
        if len(seen) != len(set(seen)):
            raise ContractSemanticError(
                f"{entry_label}: lifecycleExpectations must not repeat an entry"
            )
        _boolean(fields, "satisfiedByExistingGovernedResource", entry_label)


def validate_component_execution_package_declaration(value: object) -> None:
    """Validate the T-0688 IP-10 `ComponentExecutionPackageDeclaration` closed record.

    This is a static-authoring check only: it proves the declaration is well-formed and
    internally consistent, never that any credential, network, destination, or resource
    surface it names is actually authorized. The shared `ComponentPortSet` contract
    requires at least one port; literal `[]` is accepted as a positive declaration of
    none only for the requirement/evidence/resource arrays, distinct from a field being
    absent.
    """
    label = "ComponentExecutionPackageDeclaration"
    fields = _mapping(value, label)
    _only_fields(fields, _DECLARATION_FIELDS, label)

    _release_version(fields, "declarationSchemaVersion", label)
    _identifier(fields, "componentId", label)
    _validate_metadata(fields["metadata"], f"{label}.metadata")
    validate_component_port_set(fields["ports"])
    _validate_configuration_schema(
        fields["configurationSchema"], f"{label}.configurationSchema"
    )
    _digest(fields, "implementationDigest", label)
    _validate_tests(fields["tests"], f"{label}.tests")
    _member(fields, "executionPlane", label, EXECUTION_PLANES)
    _validate_credential_requirements(
        fields["credentialRequirements"], f"{label}.credentialRequirements"
    )
    _validate_network_requirements(
        fields["networkRequirements"], f"{label}.networkRequirements"
    )
    _validate_destination_requirements(
        fields["destinationRequirements"], f"{label}.destinationRequirements"
    )
    _member(fields, "effectClass", label, EFFECT_CLASSES)
    _validate_evidence_obligations(
        fields["evidenceObligations"], f"{label}.evidenceObligations"
    )
    _validate_resource_requirements(
        fields["resourceRequirements"], f"{label}.resourceRequirements"
    )


def _incomplete_diagnostic(
    component_id: str | None, dimension: str
) -> Mapping[str, object]:
    return MappingProxyType(
        {
            "code": COMPONENT_EXECUTION_DECLARATION_INCOMPLETE,
            "componentId": component_id,
            "dimension": dimension,
        }
    )


def _top_level_dimension(error: ContractSemanticError, label: str) -> str:
    message = str(error)
    prefix = f"{label}."
    if message.startswith(prefix):
        remainder = message[len(prefix) :]
        return remainder.split(":", 1)[0].split("[", 1)[0]
    if message.startswith(f"{label}:"):
        remainder = message[len(label) + 1 :].strip()
        return remainder.split(" ", 1)[0]
    return label


def evaluate_component_publication_readiness(
    value: object,
    rollout_stage: str,
    publication_posture: str,
) -> Mapping[str, object]:
    """Diagnose whether a declaration is fit to publish; never publishes anything.

    Catches every validation failure -- structural or semantic -- and reports it as a
    single `COMPONENT_EXECUTION_DECLARATION_INCOMPLETE` diagnostic naming the top-level
    dimension that failed and, when recoverable, `componentId`. `R0` is always
    diagnostic-only and never blocks. At `R1`, only `new` and `republished` postures are
    blocked by an incomplete declaration; `alreadyPublished` stays diagnostic-only so a
    Component already live is never retroactively unpublished by a readiness check.
    """
    label = "ComponentExecutionPackageDeclaration"
    if rollout_stage not in PUBLICATION_ROLLOUT_STAGES:
        raise ContractSemanticError(
            f"{label}: rollout_stage must be one of {PUBLICATION_ROLLOUT_STAGES!r}"
        )
    if publication_posture not in PUBLICATION_POSTURES:
        raise ContractSemanticError(
            f"{label}: publication_posture must be one of {PUBLICATION_POSTURES!r}"
        )

    component_id: str | None = None
    if isinstance(value, Mapping):
        candidate = value.get("componentId")
        if isinstance(candidate, str):
            component_id = candidate

    try:
        validate_component_execution_package_declaration(value)
    except ContractSemanticError as error:
        diagnostics = (
            _incomplete_diagnostic(component_id, _top_level_dimension(error, label)),
        )
        blocking = rollout_stage == "R1" and publication_posture in {
            "new",
            "republished",
        }
        return MappingProxyType(
            {"diagnostics": diagnostics, "publicationBlocked": blocking}
        )

    return MappingProxyType({"diagnostics": (), "publicationBlocked": False})


def evaluate_component_execution_admission(
    value: object,
    permission_allowed: bool,
    capability_gateway_allowed: bool,
) -> Mapping[str, object]:
    """Decide whether a live call against a declared Component may proceed.

    Validates the declaration first, since an invalid declaration can never be admitted.
    Live call-time Permission is always required, unconditionally: the declaration only
    ever describes what a Component *may* touch, it never grants authority by itself.
    Capability Gateway approval is additionally required whenever the declaration names
    any credential, network, or destination requirement, or declares `effectClass:
    externalEffect` -- the surfaces that can reach outside the process. This function
    only decides; it never performs execution.
    """
    validate_component_execution_package_declaration(value)
    fields = _mapping(value, "ComponentExecutionPackageDeclaration")

    if not isinstance(permission_allowed, bool):
        raise ContractSemanticError("permission_allowed must be a boolean")
    if not isinstance(capability_gateway_allowed, bool):
        raise ContractSemanticError("capability_gateway_allowed must be a boolean")

    gateway_required = (
        bool(fields["credentialRequirements"])
        or bool(fields["networkRequirements"])
        or bool(fields["destinationRequirements"])
        or fields["effectClass"] == "externalEffect"
    )
    admitted = permission_allowed and (
        not gateway_required or capability_gateway_allowed
    )
    return MappingProxyType(
        {
            "admitted": admitted,
            "permissionRequired": True,
            "capabilityGatewayRequired": gateway_required,
            "executed": False,
        }
    )


def evaluate_resource_lifecycle_request(
    action: str,
    explicit_request: bool,
    approved: bool,
    attributable: bool,
    observable: bool,
) -> Mapping[str, object]:
    """Decide whether a resource lifecycle action may proceed; never provisions anything.

    An implicit request is refused outright: `explicit_request` must be `True`, and the
    action is admitted only when it is also `approved`, `attributable`, and `observable`.
    This function only decides; it never performs provisioning, scaling, reconfiguration,
    suspension, or destruction.
    """
    label = "ResourceLifecycleRequest"
    if action not in RESOURCE_LIFECYCLE_ACTIONS:
        raise ContractSemanticError(
            f"{label}: action must be one of {RESOURCE_LIFECYCLE_ACTIONS!r}"
        )
    for name, flag in (
        ("explicit_request", explicit_request),
        ("approved", approved),
        ("attributable", attributable),
        ("observable", observable),
    ):
        if not isinstance(flag, bool):
            raise ContractSemanticError(f"{label}: {name} must be a boolean")

    admitted = explicit_request and approved and attributable and observable
    return MappingProxyType(
        {
            "action": action,
            "admitted": admitted,
            "provisioned": False,
        }
    )
