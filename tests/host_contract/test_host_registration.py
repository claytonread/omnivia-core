"""Tests for Host Contract v1 Module contribution registration.

Registration validates identity, Release integrity, target compatibility,
entitlement declaration, schema availability and conflicts *before* activation.
Partial activation of one declaration is prohibited, and priority never
resolves an identity or ownership conflict.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from _host_fixtures import bound_host

from omnivia_core.host_contract.v1 import generated as gen
from omnivia_core.host_contract.v1 import registration as reg

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO_ROOT / "contracts" / "host" / "v1" / "fixtures"


def load(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


def declaration(*contributions: dict[str, Any], entitlements: list[str] | None = None) -> Any:
    document = json.loads(json.dumps(load("valid/module-contribution.json")))
    if contributions:
        document["contributions"] = list(contributions)
    if entitlements is not None:
        document["requiredEntitlements"] = entitlements
    return gen.ModuleContributionDeclaration.from_wire(document)


def contribution(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "contributionId": "documents.route.home",
        "kind": "route",
        "targetId": "documents.home",
        "requiredCapabilities": ["navigation.register"],
        "optionalCapabilities": [],
        "payloadSchemaId": "schema.documents.route.v1",
    }
    base.update(overrides)
    return base


HOST = reg.ContributionRegistry(
    available_payload_schema_ids=frozenset({"schema.documents.route.v1"}),
    available_capabilities=frozenset({"navigation.register", "diagnostics.trace"}),
    declared_entitlements=frozenset({"documents.use"}),
    host_target="desktop",
)

#: Complete, verified pre-activation proof for the canonical declaration
#: fixture. Every conflict test below holds this constant so that the only
#: thing under test is the conflict itself; the proof half is covered by
#: ``test_host_registration_evidence.py``.
EVIDENCE = reg.RegistrationEvidence(
    release_identity=gen.ReleasePackageIdentity.from_wire(
        load("valid/release-package-identity.json")
    ),
    verified_module_id="module.documents",
    verified_module_release_id="release-documents-1",
    recomputed_manifest_sha256="a" * 64,
    recomputed_payload_sha256="b" * 64,
    publisher_verified=True,
    verified_publisher_id="publisher.omnivia",
    signing_key_verified=True,
    verified_signing_key_id="key.release.2026",
    host_contract_version="1.0.0",
)


def _register(
    declaration: object,
    registry: reg.ContributionRegistry,
    evidence: object,
) -> reg.ContributionRegistrationResult:
    return bound_host(registry=registry, evidence=(evidence,)).register(  # type: ignore[arg-type]
        declaration  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# Accepted registration
# --------------------------------------------------------------------------


def test_registration_statuses_are_exactly_two() -> None:
    assert reg.REGISTRATION_STATUSES == ("accepted", "rejected")


def test_a_well_formed_declaration_is_accepted_whole() -> None:
    result = _register(declaration(), HOST, EVIDENCE)
    assert result.status == "accepted"
    assert result.activated == ("documents.route.home",)
    assert result.rejections == ()


def test_the_canonical_declaration_fixture_registers() -> None:
    result = _register(
        gen.ModuleContributionDeclaration.from_wire(load("valid/module-contribution.json")),
        HOST,
        EVIDENCE,
    )
    assert result.status == "accepted"


# --------------------------------------------------------------------------
# Conflicts: fail before activation, all-or-nothing
# --------------------------------------------------------------------------


def test_a_duplicate_target_id_within_one_declaration_is_rejected() -> None:
    result = _register(
        declaration(
            contribution(),
            contribution(contributionId="documents.route.home.alt"),
        ),
        HOST,
        EVIDENCE,
    )
    assert result.status == "rejected"
    assert [rejection.code for rejection in result.rejections] == ["duplicate_target_id"]


def test_a_duplicate_contribution_id_is_rejected() -> None:
    result = _register(
        declaration(contribution(), contribution(targetId="documents.other")), HOST, EVIDENCE
    )
    assert result.status == "rejected"
    assert "duplicate_contribution_id" in {rejection.code for rejection in result.rejections}


def test_a_target_id_another_release_already_owns_is_rejected() -> None:
    registry = dataclasses.replace(HOST, registered_target_ids=frozenset({"documents.home"}))
    result = _register(declaration(), registry, EVIDENCE)
    assert result.status == "rejected"
    assert [rejection.code for rejection in result.rejections] == ["duplicate_target_id"]


def test_a_reserved_shell_target_id_is_rejected() -> None:
    registry = dataclasses.replace(HOST, reserved_target_ids=frozenset({"documents.home"}))
    result = _register(declaration(), registry, EVIDENCE)
    assert result.status == "rejected"
    assert [rejection.code for rejection in result.rejections] == ["reserved_target_id"]


def test_an_unavailable_payload_schema_is_rejected() -> None:
    """A schema major the host does not publish is simply an unavailable schema ID:
    ``schema.documents.route.v2`` is a different identifier from ``...v1``.
    """
    result = _register(
        declaration(contribution(payloadSchemaId="schema.documents.route.v2")), HOST, EVIDENCE
    )
    assert result.status == "rejected"
    assert [rejection.code for rejection in result.rejections] == ["payload_schema_unavailable"]


def test_a_missing_required_capability_is_rejected() -> None:
    result = _register(
        declaration(contribution(requiredCapabilities=["files.export"])), HOST, EVIDENCE
    )
    assert result.status == "rejected"
    assert [rejection.code for rejection in result.rejections] == ["missing_required_capability"]


def test_a_missing_optional_capability_does_not_reject() -> None:
    result = _register(
        declaration(contribution(optionalCapabilities=["notifications.native"])), HOST, EVIDENCE
    )
    assert result.status == "accepted"


def test_an_undeclared_entitlement_is_rejected() -> None:
    result = _register(declaration(entitlements=["documents.admin"]), HOST, EVIDENCE)
    assert result.status == "rejected"
    assert [rejection.code for rejection in result.rejections] == ["undeclared_entitlement"]


def test_priority_cannot_override_an_identity_conflict() -> None:
    result = _register(
        declaration(
            contribution(priority=1),
            contribution(contributionId="documents.route.home.alt", priority=99),
        ),
        HOST,
        EVIDENCE,
    )
    assert result.status == "rejected"
    assert [rejection.code for rejection in result.rejections] == ["duplicate_target_id"]


def test_priority_orders_accepted_contributions_without_deciding_them() -> None:
    result = _register(
        declaration(
            contribution(contributionId="a", targetId="documents.a", priority=10),
            contribution(contributionId="b", targetId="documents.b", priority=1),
        ),
        HOST,
        EVIDENCE,
    )
    assert result.status == "accepted"
    assert result.activated == ("a", "b")


def test_one_bad_contribution_prevents_activating_the_good_ones() -> None:
    """Partial activation of a declaration is prohibited."""
    result = _register(
        declaration(
            contribution(contributionId="good", targetId="documents.good"),
            contribution(
                contributionId="bad",
                targetId="documents.bad",
                requiredCapabilities=["files.export"],
            ),
        ),
        HOST,
        EVIDENCE,
    )
    assert result.status == "rejected"
    assert result.activated == ()


def test_every_conflict_is_reported_rather_than_only_the_first() -> None:
    result = _register(
        declaration(
            contribution(
                contributionId="bad",
                targetId="documents.bad",
                requiredCapabilities=["files.export"],
                payloadSchemaId="schema.missing.v1",
            ),
            entitlements=["documents.admin"],
        ),
        HOST,
        EVIDENCE,
    )
    assert result.status == "rejected"
    assert {rejection.code for rejection in result.rejections} == {
        "missing_required_capability",
        "payload_schema_unavailable",
        "undeclared_entitlement",
    }


def test_a_rejection_names_the_contribution_it_belongs_to() -> None:
    result = _register(
        declaration(contribution(requiredCapabilities=["files.export"])), HOST, EVIDENCE
    )
    assert result.rejections[0].contribution_id == "contributions[0]"


def test_a_declaration_level_rejection_names_no_contribution() -> None:
    result = _register(declaration(entitlements=["documents.admin"]), HOST, EVIDENCE)
    assert result.rejections[0].contribution_id is None


def test_rejection_codes_are_the_governed_set() -> None:
    assert reg.REJECTION_CODES == (
        "duplicate_contribution_id",
        "duplicate_target_id",
        "reserved_target_id",
        "payload_schema_unavailable",
        "missing_required_capability",
        "undeclared_entitlement",
    )
