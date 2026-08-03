"""Schema conformance for the twenty governed Host Contract v1 fixtures.

The canonical schema is the normative JSON Schema 2020-12 representation of
Host Contract v1.0.0 (DOC-004 §AA). This module validates every packaged
fixture against it with ``jsonschema``, and then enforces the governed
fixture-expectations table -- including the two expectations that schema
validation alone can never decide:

- ``invalid/forged-shell-context-request.json`` is a *valid* envelope whose
  *operation* must be denied, because payload authority fields are ignored;
- ``migration/v0-display-context-to-v1.json`` is a migration plan and is never
  accepted as a Host Contract instance.

``jsonschema`` is a development-only dependency: it is a conformance gate, not
part of the standard-library-only contract package.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _host_fixtures import bound_host
from jsonschema import Draft202012Validator

from omnivia_core.host_contract.v1 import (
    codec,
    resources,
)
from omnivia_core.host_contract.v1 import (
    compatibility as compat,
)
from omnivia_core.host_contract.v1 import (
    generated as gen,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_PATH = REPO_ROOT / "contracts" / "host" / "v1" / "schemas" / "host-contract-v1.schema.json"
FIXTURES_DIR = REPO_ROOT / "contracts" / "host" / "v1" / "fixtures"

SCHEMA: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=Draft202012Validator.FORMAT_CHECKER)

_TEST_OPERATION_ADMISSION = codec.OperationCatalogue(
    operations=(
        codec.PublishedOperation(
            namespace="workspace",
            operation_id="get_active",
            request_schema_id="schema.workspace.get-active.request.v1",
            result_schema_id="schema.workspace.get-active.result.v1",
            effect_class="read",
            idempotency_required=False,
            required_capabilities=(),
            minimum_contract_minor=0,
        ),
        codec.PublishedOperation(
            namespace="data",
            operation_id="mutate",
            request_schema_id="schema.data.mutate.request.v1",
            result_schema_id="schema.data.mutate.result.v1",
            effect_class="local_mutation",
            idempotency_required=False,
            required_capabilities=(),
            minimum_contract_minor=0,
        ),
    )
)
TEST_HOST = bound_host(_TEST_OPERATION_ADMISSION)

FIXTURE_PATHS = tuple(
    sorted(path.relative_to(FIXTURES_DIR).as_posix() for path in FIXTURES_DIR.rglob("*.json"))
)


def load(relative: str) -> Any:
    return json.loads((FIXTURES_DIR / relative).read_text(encoding="utf-8"))


def is_schema_valid(document: Any) -> bool:
    return VALIDATOR.is_valid(document)


# --------------------------------------------------------------------------
# The canonical schema itself
# --------------------------------------------------------------------------


def test_the_canonical_schema_is_a_valid_draft_2020_12_schema() -> None:
    Draft202012Validator.check_schema(SCHEMA)


def test_the_schema_publishes_the_governed_identity() -> None:
    assert SCHEMA["$id"] == "urn:omnivia:host-contract:1.0.0"
    assert SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_the_schema_root_accepts_exactly_the_fifteen_governed_records() -> None:
    assert len(SCHEMA["oneOf"]) == 15
    assert len(gen.HOST_CONTRACT_RECORDS) == 15


def test_the_schema_pins_the_contract_to_major_one() -> None:
    assert SCHEMA["$defs"]["contractVersion"]["pattern"] == r"^1\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
    assert not VALIDATOR.is_valid({**load("valid/host-request.json"), "contractVersion": "2.0.0"})


def test_the_schema_declares_all_seventeen_namespaces() -> None:
    assert SCHEMA["$defs"]["namespace"]["enum"] == list(gen.HOST_NAMESPACES)
    assert len(SCHEMA["$defs"]["namespace"]["enum"]) == 17


# --------------------------------------------------------------------------
# Fixture inventory and expectations
# --------------------------------------------------------------------------


def test_exactly_the_twenty_governed_fixtures_are_present() -> None:
    assert len(FIXTURE_PATHS) == 20
    assert set(FIXTURE_PATHS) == set(resources.FIXTURE_EXPECTATIONS)


@pytest.mark.parametrize("relative", FIXTURE_PATHS)
def test_every_fixture_meets_its_governed_schema_expectation(relative: str) -> None:
    expectation = resources.FIXTURE_EXPECTATIONS[relative]
    document = load(relative)
    if expectation in {"schema_valid", "schema_valid_display_safe"}:
        errors = sorted(VALIDATOR.iter_errors(document), key=lambda error: list(error.path))
        assert errors == [], [error.message for error in errors]
    elif expectation == "rejected":
        assert not is_schema_valid(document)
    elif expectation == "envelope_accepted_operation_denied":
        assert is_schema_valid(document)
    elif expectation == "migration_plan_only":
        assert not is_schema_valid(document)
    else:  # pragma: no cover - guarded by test_every_expectation_is_governed
        raise AssertionError(f"unhandled expectation {expectation!r}")


def test_every_expectation_is_governed() -> None:
    assert set(resources.FIXTURE_EXPECTATIONS.values()) <= set(resources.FIXTURE_OUTCOMES)


# --------------------------------------------------------------------------
# Named negative cases, each with its governed enforcement layer
# --------------------------------------------------------------------------


def test_unknown_namespace_is_rejected_by_the_schema_and_by_the_decoder() -> None:
    document = load("invalid/unknown-namespace.json")
    assert not is_schema_valid(document)
    with pytest.raises(gen.HostContractDecodeError):
        TEST_HOST.admit_request(document)


def test_result_envelope_conflict_is_rejected_by_the_schema_and_the_totality_rule() -> None:
    document = load("invalid/result-envelope-conflict.json")
    assert not is_schema_valid(document)
    with pytest.raises(gen.HostContractDecodeError):
        codec.decode_host_result(document)


def test_inline_secret_binding_is_rejected_by_the_closed_binding_schema() -> None:
    document = load("invalid/environment-binding-inline-secret.json")
    assert not is_schema_valid(document)
    with pytest.raises(gen.HostContractDecodeError):
        gen.EnvironmentBinding.from_wire(document)


def test_cloud_development_profile_is_rejected_by_the_target_restriction() -> None:
    document = load("invalid/development-profile-cloud-target.json")
    assert not is_schema_valid(document)
    with pytest.raises(gen.HostContractDecodeError):
        gen.DevelopmentProfile.from_wire(document)


def test_forged_shell_context_request_passes_the_schema_and_still_grants_nothing() -> None:
    """Schema acceptance alone never grants capability authority."""
    document = load("invalid/forged-shell-context-request.json")
    assert is_schema_valid(document)
    request = TEST_HOST.admit_request(document)
    assert codec.payload_authority_claims(request) == ("principalRef", "workspaceRef")
    trusted = gen.ShellContext.from_wire(load("valid/shell-context.json"))
    assert codec.resolve_trusted_context(request, trusted).workspace_ref == "workspace-001"


def test_migration_plan_is_not_a_host_contract_instance() -> None:
    document = load("migration/v0-display-context-to-v1.json")
    assert not is_schema_valid(document)
    assert compat.is_host_contract_instance(document) is False
    assert compat.load_migration_plan(document).result == "requires_trusted_context_service"


def test_the_denial_fixture_is_display_safe() -> None:
    """Schema closure blocks extra fields; trusted host policy admits display text."""
    document = load("denial/permission-denied.json")
    assert is_schema_valid(document)
    assert set(document) <= gen.CapabilityDenial.WIRE_FIELDS
    for forbidden in ("policyExpression", "ruleId", "token", "secret", "otherWorkspaceRef"):
        assert not is_schema_valid({**document, forbidden: "x"})
    admitted = codec.admit_display_safe_denial(
        document,
        lambda denial: denial.safe_reason
        if denial.safe_reason == document["safeReason"]
        else None,
    )
    assert admitted.to_wire() == document


def test_the_degradation_fixture_is_a_compatible_with_degradation_result() -> None:
    document = load("degradation/optional-capability-missing.json")
    assert is_schema_valid(document)
    result = gen.CompatibilityResult.from_wire(document)
    assert result.status == "compatible_with_degradation"
    assert result.required_missing == ()
    assert result.optional_missing == ("notifications.native",)


# --------------------------------------------------------------------------
# Structural parity between the schema and the generated surface
# --------------------------------------------------------------------------


_DEFS_TO_RECORD = {
    "request": "HostRequest",
    "result": "HostResult",
    "shellContext": "ShellContext",
    "compatibilityResult": "CompatibilityResult",
    "availability": "CapabilityAvailability",
    "denial": "CapabilityDenial",
    "moduleContributionDeclaration": "ModuleContributionDeclaration",
    "releasePackageIdentity": "ReleasePackageIdentity",
    "promotionRecord": "PromotionRecord",
    "rollbackRecord": "RollbackRecord",
    "environmentBinding": "EnvironmentBinding",
    "developmentProfile": "DevelopmentProfile",
    "shellScenario": "ShellScenario",
    "testDriverRequest": "TestDriverRequest",
    "testDriverResult": "TestDriverResult",
    "remediation": "Remediation",
}


@pytest.mark.parametrize(("definition", "record"), sorted(_DEFS_TO_RECORD.items()))
def test_each_record_declares_exactly_the_schema_property_spelling(
    definition: str, record: str
) -> None:
    declared = set(SCHEMA["$defs"][definition]["properties"])
    assert getattr(gen, record).WIRE_FIELDS == declared


@pytest.mark.parametrize(("definition", "record"), sorted(_DEFS_TO_RECORD.items()))
def test_each_record_matches_the_schema_closure(definition: str, record: str) -> None:
    closed = SCHEMA["$defs"][definition].get("additionalProperties") is False
    assert getattr(gen, record).CLOSED is closed


@pytest.mark.parametrize(("definition", "record"), sorted(_DEFS_TO_RECORD.items()))
def test_each_record_requires_exactly_the_schema_required_set(
    definition: str, record: str
) -> None:
    required = set(SCHEMA["$defs"][definition].get("required", []))
    cls = getattr(gen, record)
    optional = {
        name for name in cls.WIRE_FIELDS if name not in required
    }
    document = _minimal_document(definition)
    cls.from_wire(document)
    for name in sorted(required):
        with pytest.raises(gen.HostContractDecodeError, match=name):
            cls.from_wire({key: value for key, value in document.items() if key != name})
    assert optional == cls.WIRE_FIELDS - required


_MINIMAL_SOURCE = {
    "request": "valid/host-request.json",
    "result": "valid/host-result.json",
    "shellContext": "valid/shell-context.json",
    "compatibilityResult": "degradation/optional-capability-missing.json",
    "denial": "denial/permission-denied.json",
    "moduleContributionDeclaration": "valid/module-contribution.json",
    "releasePackageIdentity": "valid/release-package-identity.json",
    "promotionRecord": "valid/promotion-record.json",
    "rollbackRecord": "valid/rollback-record.json",
    "environmentBinding": "valid/environment-binding.json",
    "developmentProfile": "valid/development-profile.json",
    "shellScenario": "valid/shell-scenario.json",
    "testDriverRequest": "valid/test-driver-request.json",
    "testDriverResult": "valid/test-driver-result.json",
}


def _conditionally_required(definition: str, document: dict[str, Any]) -> set[str]:
    """Return what the definition's ``allOf`` additionally requires of this document.

    ``required`` is not the whole story for the result envelope: the canonical
    schema's ``allOf`` also requires the branch matching ``outcome``. A
    "minimal" document that dropped it would not be schema-valid at all, so it
    is not a fair probe of the required-field rule.
    """
    names: set[str] = set()
    for conditional in SCHEMA["$defs"][definition].get("allOf", []):
        tested = conditional["if"]["properties"]
        if all(document.get(name) == rule["const"] for name, rule in tested.items()):
            names.update(conditional["then"].get("required", []))
    return names


def _minimal_document(definition: str) -> dict[str, Any]:
    """Return a document holding exactly the schema's required fields."""
    if definition == "availability":
        full: dict[str, Any] = {
            "state": "available",
            "capabilityId": "files.export",
            "target": "desktop",
            "safeReason": "ok",
        }
    elif definition == "remediation":
        full = {"kind": "retry_later", "routeId": "documents.home"}
    else:
        full = dict(load(_MINIMAL_SOURCE[definition]))
    required = set(SCHEMA["$defs"][definition].get("required", []))
    required |= _conditionally_required(definition, full)
    return {key: value for key, value in full.items() if key in required}
