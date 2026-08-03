"""Round-7 adversarial registration regressions.

The declaration passed to ``BoundHost.register`` remains caller-owned.  These
checks prove registration takes one detached semantic snapshot and never emits
unverified declaration values in rejection diagnostics.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.host_contract import v1
from omnivia_core.host_contract.v1 import bound as bound_module
from omnivia_core.host_contract.v1 import codec
from omnivia_core.host_contract.v1 import generated as gen
from omnivia_core.host_contract.v1 import registration as reg

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "contracts" / "host" / "v1" / "fixtures"


def load(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


def evidence() -> reg.RegistrationEvidence:
    identity = gen.ReleasePackageIdentity.from_wire(load("valid/release-package-identity.json"))
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


def registry(**overrides: Any) -> reg.ContributionRegistry:
    values: dict[str, Any] = {
        "available_payload_schema_ids": frozenset({"schema.documents.route.v1"}),
        "available_capabilities": frozenset({"navigation.register"}),
        "declared_entitlements": frozenset({"documents.use"}),
        "host_target": "desktop",
        "trusted_publisher_ids": frozenset({"publisher.omnivia"}),
        "trusted_signing_key_ids": frozenset({"key.release.2026"}),
    }
    values.update(overrides)
    return reg.ContributionRegistry(**values)


def host(registry_state: reg.ContributionRegistry | None = None) -> v1.BoundHost:
    return v1.BoundHost(
        operation_catalogue=codec.OperationCatalogue(operations=()),
        contribution_registry=registry_state or registry(),
        registration_evidence=(evidence(),),
    )


def declaration(
    *,
    contribution_id: str = "documents.route.home",
    target_id: str = "documents.home",
    required_capabilities: tuple[str, ...] = ("navigation.register",),
) -> gen.ModuleContributionDeclaration:
    source = gen.ModuleContributionDeclaration.from_wire(
        load("valid/module-contribution.json")
    )
    contribution = dataclasses.replace(
        source.contributions[0],
        contribution_id=contribution_id,
        target_id=target_id,
        required_capabilities=required_capabilities,
    )
    return dataclasses.replace(source, contributions=(contribution,))


def test_mutation_after_evaluation_cannot_change_the_activated_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce the reviewer's exact validation-to-activation TOCTOU seam."""
    boundary = host()
    caller_owned = declaration()
    original_evaluate = reg._evaluate_registration

    def mutate_after_evaluation(
        candidate: gen.ModuleContributionDeclaration,
        registry_state: reg.ContributionRegistry,
        proof: reg.RegistrationEvidence,
    ) -> reg.ContributionRegistrationResult:
        result = original_evaluate(candidate, registry_state, proof)
        object.__setattr__(caller_owned.contributions[0], "target_id", "documents.stolen")
        return result

    monkeypatch.setattr(reg, "_evaluate_registration", mutate_after_evaluation)

    assert boundary.register(caller_owned).status == "accepted"
    assert boundary.register(
        declaration(contribution_id="documents.route.stolen", target_id="documents.stolen")
    ).status == "accepted"


def test_thread_mutation_after_evaluation_cannot_change_the_activated_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same activation seam stays closed under deterministic thread mutation."""
    boundary = host()
    caller_owned = declaration()
    evaluated = threading.Event()
    mutated = threading.Event()
    original_evaluate = reg._evaluate_registration

    def pause_after_evaluation(
        candidate: gen.ModuleContributionDeclaration,
        registry_state: reg.ContributionRegistry,
        proof: reg.RegistrationEvidence,
    ) -> reg.ContributionRegistrationResult:
        result = original_evaluate(candidate, registry_state, proof)
        evaluated.set()
        assert mutated.wait(timeout=5)
        return result

    monkeypatch.setattr(reg, "_evaluate_registration", pause_after_evaluation)
    result: list[reg.ContributionRegistrationResult] = []
    worker = threading.Thread(target=lambda: result.append(boundary.register(caller_owned)))
    worker.start()
    assert evaluated.wait(timeout=5)
    object.__setattr__(caller_owned.contributions[0], "target_id", "documents.thread-stolen")
    mutated.set()
    worker.join(timeout=5)
    assert not worker.is_alive()

    assert result[0].status == "accepted"
    assert boundary.register(
        declaration(
            contribution_id="documents.route.thread-stolen",
            target_id="documents.thread-stolen",
        )
    ).status == "accepted"


def test_mutation_before_evidence_selection_cannot_change_the_release_being_evaluated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = host()
    caller_owned = declaration()
    original_evidence_for = bound_module._evidence_for

    def mutate_before_selection(
        candidate: object, inventory: tuple[object, ...]
    ) -> reg.RegistrationEvidence | None:
        object.__setattr__(caller_owned, "module_release_id", "release-attacker")
        return original_evidence_for(candidate, inventory)

    monkeypatch.setattr(bound_module, "_evidence_for", mutate_before_selection)

    result = boundary.register(caller_owned)
    assert result.status == "accepted"
    assert result.module_release_id == "release-documents-1"


def test_nested_mutation_during_evaluation_cannot_remove_a_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = host()
    caller_owned = declaration(required_capabilities=("files.export",))
    original_identity_rejections = reg._identity_rejections

    def mutate_after_identity_check(
        candidate: gen.ModuleContributionDeclaration,
        proof: reg.RegistrationEvidence,
    ) -> list[reg.RegistrationRejection]:
        result = original_identity_rejections(candidate, proof)
        object.__setattr__(caller_owned.contributions[0], "required_capabilities", [])
        return result

    monkeypatch.setattr(reg, "_identity_rejections", mutate_after_identity_check)

    result = boundary.register(caller_owned)
    assert result.status == "rejected"
    assert {item.code for item in result.rejections} == {"missing_required_capability"}
    assert result.activated == ()


def _assert_secret_absent(
    result: reg.ContributionRegistrationResult, secret: str
) -> None:
    surfaces = [str(result), repr(result)]
    for rejection in result.rejections:
        surfaces.extend((rejection.detail, str(rejection), repr(rejection)))
    assert secret not in "\n".join(surfaces)


def test_rejection_diagnostics_never_disclose_valid_secret_shaped_identifiers() -> None:
    secret = "ghp_" + "s" * 36
    source = declaration()
    source_contribution = source.contributions[0]
    cases = (
        (dataclasses.replace(source, module_id=secret), registry()),
        (dataclasses.replace(source, module_release_id=secret), registry()),
        (
            dataclasses.replace(
                source,
                contributions=(dataclasses.replace(source_contribution, contribution_id=secret),),
            ),
            dataclasses.replace(
                registry(), registered_target_ids=frozenset({source_contribution.target_id})
            ),
        ),
        (
            dataclasses.replace(
                source,
                contributions=(dataclasses.replace(source_contribution, target_id=secret),),
            ),
            dataclasses.replace(registry(), registered_target_ids=frozenset({secret})),
        ),
        (
            dataclasses.replace(
                source,
                contributions=(
                    dataclasses.replace(source_contribution, payload_schema_id=secret),
                ),
            ),
            registry(),
        ),
        (
            dataclasses.replace(
                source,
                contributions=(
                    dataclasses.replace(source_contribution, required_capabilities=(secret,)),
                ),
            ),
            registry(),
        ),
        (dataclasses.replace(source, required_entitlements=(secret,)), registry()),
    )

    for candidate, registry_state in cases:
        result = host(registry_state).register(candidate)
        assert result.status == "rejected"
        assert result.activated == ()
        _assert_secret_absent(result, secret)


def test_contribution_rejections_expose_only_fixed_field_and_index_metadata() -> None:
    result = host(
        dataclasses.replace(
            registry(), registered_target_ids=frozenset({"documents.home"})
        )
    ).register(declaration())

    assert result.module_release_id == ""
    assert result.rejections == (
        reg.RegistrationRejection(
            code="duplicate_target_id",
            detail="field=contributions[0].targetId",
            contribution_id="contributions[0]",
        ),
    )


def test_malformed_mutable_nested_values_are_rejected_without_running_hostile_code() -> None:
    secret = "ghp_" + "x" * 36

    class HostileString(str):
        def __hash__(self) -> int:
            raise RuntimeError(secret)

        def __eq__(self, other: object) -> bool:
            del other
            raise RuntimeError(secret)

        def __repr__(self) -> str:
            return secret

    candidate = declaration()
    object.__setattr__(candidate, "required_entitlements", [HostileString(secret)])

    result: reg.ContributionRegistrationResult | None = None
    try:
        result = host().register(candidate)
    except RuntimeError as error:  # pragma: no cover - permanent disclosure tripwire
        rendered = "\n".join(
            (
                str(error),
                repr(error),
                repr(error.args),
                repr(error.__cause__),
                repr(error.__context__),
            )
        )
        assert secret not in rendered
        pytest.fail("registration must reject malformed declarations without raising")

    assert result is not None
    assert result.status == "rejected"
    assert {item.code for item in result.rejections} == {"invalid_declaration"}
    _assert_secret_absent(result, secret)
