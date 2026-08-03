"""Bound-host regressions: importer-owned state never becomes authentic host state."""

from __future__ import annotations

import copy
import dataclasses
import inspect
import json
import pickle
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.host_contract import v1
from omnivia_core.host_contract.v1 import codec
from omnivia_core.host_contract.v1 import generated as gen
from omnivia_core.host_contract.v1 import registration as reg

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "contracts" / "host" / "v1" / "fixtures"


def load(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


def operation(operation_id: str = "get_active") -> codec.PublishedOperation:
    return codec.PublishedOperation(
        namespace="workspace",
        operation_id=operation_id,
        request_schema_id="schema.workspace.request.v1",
        result_schema_id="schema.workspace.result.v1",
        effect_class="read",
        idempotency_required=False,
        required_capabilities=(),
        minimum_contract_minor=0,
    )


def registry() -> reg.ContributionRegistry:
    return reg.ContributionRegistry(
        available_payload_schema_ids=frozenset({"schema.documents.route.v1"}),
        available_capabilities=frozenset({"navigation.register"}),
        declared_entitlements=frozenset({"documents.use"}),
        host_target="desktop",
        trusted_publisher_ids=frozenset({"publisher.omnivia"}),
        trusted_signing_key_ids=frozenset({"key.release.2026"}),
    )


def evidence(
    release_identity: gen.ReleasePackageIdentity | None = None,
) -> reg.RegistrationEvidence:
    identity = release_identity or gen.ReleasePackageIdentity.from_wire(
        load("valid/release-package-identity.json")
    )
    return reg.RegistrationEvidence(
        release_identity=identity,
        verified_module_id="module.documents",
        verified_module_release_id=identity.release_id,
        recomputed_manifest_sha256=identity.manifest_sha256,
        recomputed_payload_sha256=identity.payload_sha256,
        publisher_verified=True,
        verified_publisher_id=identity.publisher_id,
        signing_key_verified=True,
        verified_signing_key_id=identity.signing_key_id,
        host_contract_version="1.0.0",
    )


def boundary(
    *operations: codec.PublishedOperation,
    registration_evidence: tuple[reg.RegistrationEvidence, ...] | None = None,
) -> v1.BoundHost:
    return v1.BoundHost(
        operation_catalogue=codec.OperationCatalogue(operations=operations),
        contribution_registry=registry(),
        registration_evidence=registration_evidence or (evidence(),),
    )


def declaration() -> gen.ModuleContributionDeclaration:
    return gen.ModuleContributionDeclaration.from_wire(load("valid/module-contribution.json"))


def test_installed_modules_expose_no_operation_or_registration_mint() -> None:
    for module in (codec, reg):
        names = vars(module)
        assert all("mint" not in name.casefold() for name in names)
        assert all("admission" not in name.casefold() for name in names)
    assert not hasattr(codec, "_OPERATION_ADMISSIONS")
    assert not hasattr(reg, "_REGISTRATION_ADMISSIONS")


def test_caller_cannot_pass_trust_state_to_a_global_admission_function() -> None:
    assert tuple(inspect.signature(codec.decode_host_request).parameters) == ("payload",)
    assert tuple(inspect.signature(codec.encode_host_request).parameters) == ("request",)
    assert not hasattr(reg, "register_contributions")
    assert tuple(inspect.signature(v1.BoundHost.admit_request).parameters) == ("self", "payload")
    assert tuple(inspect.signature(v1.BoundHost.register).parameters) == ("self", "declaration")


def test_separate_caller_boundary_cannot_publish_into_the_authentic_host() -> None:
    authentic = boundary(operation())
    attacker = boundary(operation("attacker_operation"))
    hostile = load("valid/host-request.json")
    hostile["operation"] = "attacker_operation"

    assert attacker.admit_request(hostile).operation == "attacker_operation"
    with pytest.raises(gen.HostContractDecodeError, match="unsupported_contract_value"):
        authentic.admit_request(hostile)


def test_self_authored_registration_state_cannot_activate_on_the_authentic_host() -> None:
    authentic = boundary(operation())
    attacker_identity = dataclasses.replace(
        gen.ReleasePackageIdentity.from_wire(load("valid/release-package-identity.json")),
        release_id="release-attacker-1",
    )
    attacker = boundary(
        operation(), registration_evidence=(evidence(attacker_identity),)
    )
    hostile_declaration = dataclasses.replace(
        declaration(), module_release_id="release-attacker-1"
    )

    assert attacker.register(hostile_declaration).status == "accepted"
    refused = authentic.register(hostile_declaration)
    assert refused.status == "rejected"
    assert refused.activated == ()
    assert "unverified_release_identity" in {item.code for item in refused.rejections}


def test_bound_host_cannot_be_copied_pickled_or_reconstructed_without_state() -> None:
    authentic = boundary(operation())
    for copier in (copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            copier(authentic)
    with pytest.raises(TypeError):
        pickle.dumps(authentic)

    reconstructed = object.__new__(type(authentic))
    with pytest.raises(gen.HostContractDecodeError, match="bound host"):
        reconstructed.admit_request(load("valid/host-request.json"))
    assert reconstructed.register(declaration()).status == "rejected"


def test_caller_owned_registration_state_is_separate_from_authentic_activation() -> None:
    authentic = boundary(operation())
    attacker = boundary(operation())

    assert attacker.register(declaration()).status == "accepted"
    assert authentic.register(declaration()).status == "accepted"
    repeated = authentic.register(declaration())
    assert repeated.status == "rejected"
    assert repeated.activated == ()
    assert {item.code for item in repeated.rejections} == {"duplicate_target_id"}
