"""Pre-activation proof for Host Contract v1 Module contribution registration.

Registration claims to validate identity, Release integrity and target
compatibility *before* activation. It can only do that against facts the host
established for itself, so it takes a :class:`RegistrationEvidence` -- a
trusted, immutable, implementation-local input that is never on the wire and
that a Module cannot author.

This module is the fail-closed proof: with any single proof absent, mismatched
or malformed, the declaration is rejected. A declaration is never accepted
because nothing contradicted it.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from _host_fixtures import bound_host

from omnivia_core.host_contract.v1 import bound
from omnivia_core.host_contract.v1 import generated as gen
from omnivia_core.host_contract.v1 import registration as reg

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "contracts" / "host" / "v1" / "fixtures"

MANIFEST_DIGEST = "a" * 64
PAYLOAD_DIGEST = "b" * 64


def load(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


def declaration(**overrides: Any) -> gen.ModuleContributionDeclaration:
    document = json.loads(json.dumps(load("valid/module-contribution.json")))
    document.update(overrides)
    return gen.ModuleContributionDeclaration.from_wire(document)


def release_identity(**overrides: Any) -> gen.ReleasePackageIdentity:
    document = json.loads(json.dumps(load("valid/release-package-identity.json")))
    document.update(overrides)
    return gen.ReleasePackageIdentity.from_wire(document)


def evidence(**overrides: Any) -> reg.RegistrationEvidence:
    base = reg.RegistrationEvidence(
        release_identity=release_identity(),
        verified_module_id="module.documents",
        verified_module_release_id="release-documents-1",
        recomputed_manifest_sha256=MANIFEST_DIGEST,
        recomputed_payload_sha256=PAYLOAD_DIGEST,
        publisher_verified=True,
        verified_publisher_id="publisher.omnivia",
        signing_key_verified=True,
        verified_signing_key_id="key.release.2026",
        host_contract_version="1.0.0",
    )
    return dataclasses.replace(base, **overrides)


HOST = reg.ContributionRegistry(
    available_payload_schema_ids=frozenset({"schema.documents.route.v1"}),
    available_capabilities=frozenset({"navigation.register", "diagnostics.trace"}),
    declared_entitlements=frozenset({"documents.use"}),
    host_target="desktop",
    trusted_publisher_ids=frozenset({"publisher.omnivia"}),
    trusted_signing_key_ids=frozenset({"key.release.2026"}),
)


def _register(
    declaration: object,
    registry: reg.ContributionRegistry,
    evidence: object,
) -> reg.ContributionRegistrationResult:
    return bound_host(registry=registry, evidence=(evidence,)).register(  # type: ignore[arg-type]
        declaration  # type: ignore[arg-type]
    )


def codes(result: reg.ContributionRegistrationResult) -> set[str]:
    return {rejection.code for rejection in result.rejections}


# --------------------------------------------------------------------------
# The proven case still activates
# --------------------------------------------------------------------------


def test_a_declaration_backed_by_complete_evidence_activates() -> None:
    result = _register(declaration(), HOST, evidence())
    assert result.status == "accepted"
    assert result.activated == ("documents.route.home",)
    assert result.rejections == ()


def test_activation_is_never_the_default_when_evidence_is_absent() -> None:
    """Registration has no two-argument form: there is no call that decides
    activation without authoritative evidence to decide it against.
    """
    with pytest.raises(TypeError):
        _register(declaration(), HOST)  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# Malformed or absent proof
# --------------------------------------------------------------------------


def test_evidence_of_the_wrong_type_is_rejected() -> None:
    result = _register(declaration(), HOST, None)  # type: ignore[arg-type]
    assert result.status == "rejected"
    assert codes(result) == {"malformed_registration_evidence"}
    assert result.rejections == (
        reg.RegistrationRejection(
            code="malformed_registration_evidence",
            detail="field=registrationEvidence",
        ),
    )


def test_evidence_carrying_a_malformed_release_identity_is_rejected() -> None:
    hostile = evidence(
        release_identity=gen.ReleasePackageIdentity(
            app_id="app.documents",
            release_id="release-documents-1",
            version="not-a-version",
            manifest_sha256=MANIFEST_DIGEST,
            payload_sha256=PAYLOAD_DIGEST,
            publisher_id="publisher.omnivia",
            signing_key_id="key.release.2026",
            target_entries=(gen.TargetEntry(target="desktop", entry="index.html"),),
        )
    )
    result = _register(declaration(), HOST, hostile)
    assert result.status == "rejected"
    assert codes(result) == {"malformed_registration_evidence"}


def test_evidence_with_a_non_string_identity_is_rejected() -> None:
    result = _register(
        declaration(), HOST, evidence(verified_module_id=object())  # type: ignore[arg-type]
    )
    assert result.status == "rejected"
    assert codes(result) == {"malformed_registration_evidence"}


# --------------------------------------------------------------------------
# The declaration itself must be strictly valid
# --------------------------------------------------------------------------


def test_an_empty_declaration_cannot_activate() -> None:
    """The governed schema requires at least one contribution. The exact false
    pass the independent review executed.
    """
    empty = gen.ModuleContributionDeclaration(
        declaration_version="1.0.0",
        module_id="module.documents",
        module_release_id="release-documents-1",
        contributions=(),
        required_entitlements=(),
    )
    result = _register(empty, HOST, evidence())
    assert result.status == "rejected"
    assert codes(result) == {"invalid_declaration"}
    assert result.activated == ()


def test_a_direct_constructor_invalid_declaration_cannot_activate() -> None:
    hostile = gen.ModuleContributionDeclaration(
        declaration_version="1.0.0",
        module_id="module.documents",
        module_release_id="release-documents-1",
        contributions=(
            gen.Contribution(
                contribution_id="documents route home",
                kind="route",
                target_id="documents.home",
                required_capabilities=("navigation.register",),
                optional_capabilities=(),
                payload_schema_id="schema.documents.route.v1",
            ),
        ),
        required_entitlements=("documents.use",),
    )
    result = _register(hostile, HOST, evidence())
    assert result.status == "rejected"
    assert codes(result) == {"invalid_declaration"}


def test_something_that_is_not_a_declaration_at_all_cannot_activate() -> None:
    result = _register(
        {"moduleId": "module.documents"},  # type: ignore[arg-type]
        HOST,
        evidence(),
    )
    assert result.status == "rejected"
    assert codes(result) == {"invalid_declaration"}


# --------------------------------------------------------------------------
# Identity must match verified evidence exactly
# --------------------------------------------------------------------------


def test_an_arbitrary_module_release_id_cannot_activate() -> None:
    """The second false pass the review executed: a declaration naming a
    release nobody verified.
    """
    result = _register(
        declaration(moduleReleaseId="release-anything-i-like"), HOST, evidence()
    )
    assert result.status == "rejected"
    assert "unverified_release_identity" in codes(result)


def test_a_mismatched_module_id_cannot_activate() -> None:
    result = _register(
        declaration(moduleId="module.other"), HOST, evidence()
    )
    assert result.status == "rejected"
    assert "unverified_module_identity" in codes(result)


def test_a_sole_evidence_entry_is_never_the_proof_for_another_release() -> None:
    """Selection is an exact match, not "the only thing we have".

    A one-entry inventory is the ordinary single-Module host. Handing that entry
    to a declaration naming a different release would make the host's own
    verification record the proof for a release it never verified, and would
    leave the mismatch to be noticed downstream. Selection therefore fails here,
    which is observable: the downstream conflict checks never run, so the
    already-owned ``targetId`` below is not reported alongside.
    """
    owned = dataclasses.replace(
        HOST, registered_target_ids=frozenset({"documents.home"})
    )
    result = _register(
        declaration(moduleReleaseId="release-nobody-verified"), owned, evidence()
    )
    assert result.status == "rejected"
    assert result.activated == ()
    assert result.rejections == (
        reg.RegistrationRejection(
            code="unverified_release_identity", detail="field=moduleReleaseId"
        ),
    )


def test_a_sole_evidence_entry_is_not_selected_for_a_mismatched_module_either() -> None:
    result = _register(
        declaration(moduleId="module.other", moduleReleaseId="release-other"),
        HOST,
        evidence(),
    )
    assert result.status == "rejected"
    assert codes(result) == {"unverified_release_identity"}


def test_evidence_selection_itself_refuses_a_release_it_did_not_verify() -> None:
    """The same rule, asserted where the decision is actually made."""
    inventory = (evidence(),)
    assert bound._evidence_for(declaration(), inventory) is inventory[0]
    assert (
        bound._evidence_for(declaration(moduleReleaseId="release-other"), inventory)
        is None
    )
    assert bound._evidence_for(declaration(), ()) is None


def test_evidence_selection_picks_the_verified_entry_out_of_several() -> None:
    other = evidence(
        verified_module_release_id="release-documents-2",
        release_identity=release_identity(releaseId="release-documents-2"),
    )
    inventory = (other, evidence())
    assert bound._evidence_for(declaration(), inventory) is inventory[1]


def test_evidence_whose_release_identity_names_another_release_cannot_activate() -> None:
    result = _register(
        declaration(),
        HOST,
        evidence(release_identity=release_identity(releaseId="release-documents-9")),
    )
    assert result.status == "rejected"
    assert "unverified_release_identity" in codes(result)


# --------------------------------------------------------------------------
# Release integrity and publisher identity
# --------------------------------------------------------------------------


def test_a_manifest_digest_that_was_not_reproduced_cannot_activate() -> None:
    result = _register(
        declaration(), HOST, evidence(recomputed_manifest_sha256="c" * 64)
    )
    assert result.status == "rejected"
    assert "unverified_release_integrity" in codes(result)


def test_a_payload_digest_that_was_not_reproduced_cannot_activate() -> None:
    result = _register(
        declaration(), HOST, evidence(recomputed_payload_sha256="d" * 64)
    )
    assert result.status == "rejected"
    assert "unverified_release_integrity" in codes(result)


def test_a_malformed_digest_cannot_activate() -> None:
    result = _register(
        declaration(), HOST, evidence(recomputed_manifest_sha256="not-a-digest")
    )
    assert result.status == "rejected"
    assert codes(result) == {"malformed_registration_evidence"}


def test_an_unverified_publisher_cannot_activate() -> None:
    result = _register(
        declaration(), HOST, evidence(publisher_verified=False)
    )
    assert result.status == "rejected"
    assert "unverified_publisher" in codes(result)


def test_an_untrusted_publisher_cannot_activate() -> None:
    result = _register(
        declaration(),
        HOST,
        evidence(
            verified_publisher_id="publisher.someone-else",
            release_identity=release_identity(publisherId="publisher.someone-else"),
        ),
    )
    assert result.status == "rejected"
    assert "unverified_publisher" in codes(result)


def test_an_unchecked_signature_cannot_activate() -> None:
    result = _register(
        declaration(), HOST, evidence(signing_key_verified=False)
    )
    assert result.status == "rejected"
    assert "unverified_publisher" in codes(result)


def test_a_signing_key_the_package_does_not_name_cannot_activate() -> None:
    result = _register(
        declaration(), HOST, evidence(verified_signing_key_id="key.release.2027")
    )
    assert result.status == "rejected"
    assert "unverified_publisher" in codes(result)


def test_a_host_that_does_not_require_publisher_verification_still_activates() -> None:
    """The rule is 'verified where required'. A conformance host that publishes
    no publisher trust set says so explicitly rather than by omission.
    """
    permissive = dataclasses.replace(
        HOST,
        require_publisher_verification=False,
        trusted_publisher_ids=frozenset(),
        trusted_signing_key_ids=frozenset(),
    )
    result = _register(
        declaration(), permissive, evidence(publisher_verified=False, signing_key_verified=False)
    )
    assert result.status == "accepted"


# --------------------------------------------------------------------------
# Target and contract compatibility
# --------------------------------------------------------------------------


def test_a_release_with_no_entry_for_the_host_target_cannot_activate() -> None:
    result = _register(
        declaration(), dataclasses.replace(HOST, host_target="cloud"), evidence()
    )
    assert result.status == "rejected"
    assert "incompatible_host_target" in codes(result)


def test_a_host_target_outside_the_governed_vocabulary_cannot_activate() -> None:
    result = _register(
        declaration(), dataclasses.replace(HOST, host_target="mainframe"), evidence()
    )
    assert result.status == "rejected"
    assert "incompatible_host_target" in codes(result)


def test_an_unsupported_host_contract_version_cannot_activate() -> None:
    result = _register(
        declaration(), HOST, evidence(host_contract_version="2.0.0")
    )
    assert result.status == "rejected"
    assert "incompatible_contract_version" in codes(result)


def test_a_declaration_from_an_unsupported_minor_cannot_activate() -> None:
    result = _register(
        declaration(declarationVersion="1.9.0"), HOST, evidence()
    )
    assert result.status == "rejected"
    assert "incompatible_contract_version" in codes(result)


def test_a_payload_schema_major_the_host_does_not_publish_cannot_activate() -> None:
    document = json.loads(json.dumps(load("valid/module-contribution.json")))
    document["contributions"][0]["payloadSchemaId"] = "schema.documents.route.v2"
    result = _register(
        gen.ModuleContributionDeclaration.from_wire(document), HOST, evidence()
    )
    assert result.status == "rejected"
    assert "payload_schema_unavailable" in codes(result)


# --------------------------------------------------------------------------
# The decision stays atomic
# --------------------------------------------------------------------------


def test_every_failed_proof_is_reported_together() -> None:
    """Once evidence *has* been selected, no single failure short-circuits the rest.

    The declaration keeps the release ID the evidence verified, because a
    declaration naming an unverified release is refused at selection and never
    reaches these comparisons at all. The release-identity failure here is the
    one that survives selection: evidence whose own Release Package identity
    names a different release than the one it claims to have verified.
    """
    result = _register(
        declaration(moduleId="module.other"),
        dataclasses.replace(HOST, host_target="cloud"),
        evidence(
            publisher_verified=False,
            host_contract_version="2.0.0",
            release_identity=release_identity(releaseId="release-documents-9"),
        ),
    )
    assert result.status == "rejected"
    assert codes(result) == {
        "unverified_module_identity",
        "unverified_release_identity",
        "unverified_publisher",
        "incompatible_host_target",
        "incompatible_contract_version",
    }
    assert result.activated == ()


def test_a_failed_proof_prevents_activating_an_otherwise_clean_declaration() -> None:
    result = _register(declaration(), HOST, evidence(publisher_verified=False))
    assert result.activated == ()


def test_the_pre_activation_codes_are_declared_and_disjoint_from_the_governed_set() -> None:
    """The governed specification names the conflict reasons. The pre-activation
    proof codes are this implementation's, and are kept separately so the two
    are never confused for one another.
    """
    assert reg.REGISTRATION_EVIDENCE_CODES == (
        "malformed_registration_evidence",
        "invalid_declaration",
        "unverified_module_identity",
        "unverified_release_identity",
        "unverified_release_integrity",
        "unverified_publisher",
        "incompatible_host_target",
        "incompatible_contract_version",
    )
    assert set(reg.REGISTRATION_EVIDENCE_CODES) & set(reg.REJECTION_CODES) == set()


def test_registration_evidence_is_immutable() -> None:
    proof = evidence()
    with pytest.raises(dataclasses.FrozenInstanceError):
        proof.verified_module_id = "module.other"  # type: ignore[misc]


def test_registration_evidence_is_not_a_wire_record() -> None:
    """Evidence is a local trust input. It has no wire spelling, no decoder and
    no place in the governed schema, so a Module cannot supply its own.
    """
    assert not hasattr(reg.RegistrationEvidence, "from_wire")
    assert not hasattr(reg.RegistrationEvidence, "to_wire")
    assert "RegistrationEvidence" not in gen.HOST_CONTRACT_RECORDS
