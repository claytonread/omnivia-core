"""Tests for Host Contract v1 publication semantics.

Release Package identity, promotion and rollback; environment binding secret
exclusion and Workspace scope; and the development/conformance production
exclusion boundary.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from omnivia_core.host_contract.v1 import generated as gen
from omnivia_core.host_contract.v1 import publication as pub

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO_ROOT / "contracts" / "host" / "v1" / "fixtures"


def load(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


def identity(**overrides: Any) -> gen.ReleasePackageIdentity:
    document = json.loads(json.dumps(load("valid/release-package-identity.json")))
    document.update(overrides)
    return gen.ReleasePackageIdentity.from_wire(document)


def promotion(**overrides: Any) -> gen.PromotionRecord:
    document = dict(load("valid/promotion-record.json"))
    document.update(overrides)
    return gen.PromotionRecord.from_wire(document)


def assert_non_disclosing_refusal(
    error: BaseException, secret: str, expected: str
) -> None:
    assert str(error) == expected
    assert error.args == (expected,)
    assert secret not in repr(error.args)
    assert secret not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    assert error.__context__ is None


def rollback(**overrides: Any) -> gen.RollbackRecord:
    document = dict(load("valid/rollback-record.json"))
    document.update(overrides)
    return gen.RollbackRecord.from_wire(document)


def binding(**overrides: Any) -> gen.EnvironmentBinding:
    document = json.loads(json.dumps(load("valid/environment-binding.json")))
    document.update(overrides)
    return gen.EnvironmentBinding.from_wire(document)


# --------------------------------------------------------------------------
# Release Package identity carries no environment value and no secret
# --------------------------------------------------------------------------


def test_release_package_identity_has_no_environment_or_secret_field() -> None:
    """A portable Release Package must stay environment-neutral, and the record
    proves it structurally: there is nowhere to put an environment value.
    """
    assert gen.ReleasePackageIdentity.WIRE_FIELDS == frozenset(
        {
            "appId",
            "releaseId",
            "version",
            "manifestSha256",
            "payloadSha256",
            "publisherId",
            "signingKeyId",
            "targetEntries",
        }
    )
    forbidden = {
        "environmentId",
        "environmentBindingId",
        "workspaceId",
        "credentialRefs",
        "credentials",
        "secrets",
        "token",
        "privateKey",
    }
    assert gen.ReleasePackageIdentity.WIRE_FIELDS & forbidden == frozenset()


def test_release_package_identity_refuses_an_injected_environment_field() -> None:
    document = dict(load("valid/release-package-identity.json"))
    document["environmentId"] = "environment-production"
    with pytest.raises(gen.HostContractDecodeError, match="environmentId"):
        gen.ReleasePackageIdentity.from_wire(document)


# --------------------------------------------------------------------------
# Promotion copies the same verified bytes
# --------------------------------------------------------------------------


def test_a_promotion_bound_to_the_verified_release_is_valid() -> None:
    pub.validate_promotion(promotion(), identity())


def test_a_promotion_naming_another_release_is_refused() -> None:
    secret = "ghp_" + "p" * 36
    with pytest.raises(pub.ReleaseIntegrityError) as excinfo:
        pub.validate_promotion(promotion(releaseId=secret), identity())
    assert_non_disclosing_refusal(
        excinfo.value, secret, "promotion does not name the verified Release"
    )


def test_a_promotion_whose_verified_digest_differs_is_refused() -> None:
    with pytest.raises(pub.ReleaseIntegrityError, match="manifest"):
        pub.validate_promotion(promotion(verifiedManifestSha256="b" * 64), identity())


def test_a_rejected_promotion_is_still_a_valid_record_of_the_refusal() -> None:
    pub.validate_promotion(promotion(result="rejected"), identity())


def test_promotion_between_registries_must_move_the_identical_package() -> None:
    pub.validate_same_package(identity(), identity())


def test_a_rebuilt_package_is_not_the_same_package() -> None:
    with pytest.raises(pub.ReleaseIntegrityError, match="payloadSha256"):
        pub.validate_same_package(identity(), identity(payloadSha256="c" * 64))


def test_a_repackaged_entry_point_is_not_the_same_package() -> None:
    with pytest.raises(pub.ReleaseIntegrityError, match="targetEntries"):
        pub.validate_same_package(
            identity(),
            identity(targetEntries=[{"target": "cloud", "entry": "index.html"}]),
        )


def test_a_resigned_package_is_not_the_same_package() -> None:
    with pytest.raises(pub.ReleaseIntegrityError, match="signingKeyId"):
        pub.validate_same_package(identity(), identity(signingKeyId="key.release.2027"))


# --------------------------------------------------------------------------
# Rollback selects a previously verified Release
# --------------------------------------------------------------------------


def test_rollback_to_a_previously_verified_release_is_valid() -> None:
    pub.validate_rollback(
        rollback(), frozenset({"release-documents-1", "release-documents-0"})
    )


def test_rollback_to_a_release_that_was_never_verified_is_refused() -> None:
    secret = "ghp_" + "r" * 36
    with pytest.raises(pub.ReleaseIntegrityError) as excinfo:
        pub.validate_rollback(
            rollback(restoredReleaseId=secret), frozenset({"release-documents-0"})
        )
    assert_non_disclosing_refusal(
        excinfo.value,
        secret,
        "rollback does not restore a previously verified Release",
    )


def test_rollback_may_not_restore_the_release_its_failed_promotion_carried() -> None:
    secret = "ghp_" + "f" * 36
    with pytest.raises(pub.ReleaseIntegrityError) as excinfo:
        pub.validate_rollback(
            rollback(failedPromotionId=secret),
            frozenset({"release-documents-1"}),
            failed_promotion=promotion(
                promotionId=secret, releaseId="release-documents-1"
            ),
        )
    assert_non_disclosing_refusal(
        excinfo.value,
        secret,
        "rollback would restore the Release whose promotion failed",
    )


def test_rollback_against_the_matching_failed_promotion_is_valid() -> None:
    pub.validate_rollback(
        rollback(),
        frozenset({"release-documents-1"}),
        failed_promotion=promotion(
            promotionId="promotion-002", releaseId="release-documents-9"
        ),
    )


# --------------------------------------------------------------------------
# Environment bindings hold references only
# --------------------------------------------------------------------------


#: The trusted resolver the canonical binding fixture's references live in.
#: Reference provenance itself is covered by ``test_host_binding_provenance.py``.
RESOLVER = pub.ReferenceRegistry(
    references=tuple(
        pub.ApprovedReference(
            reference_id=reference_id, kind=kind, workspace_id="workspace-acme"
        )
        for reference_id, kind in (
            ("credential.documents.storage", "credential"),
            ("integration.documents.index", "integration"),
            ("storage.documents.primary", "storage"),
            ("domain.documents.example", "domain"),
            ("policy.residency.au", "data_residency_policy"),
            ("limits.standard", "runtime_limits"),
            ("placement.eu-west", "execution_placement"),
        )
    )
)


def test_a_reference_only_binding_is_valid() -> None:
    pub.validate_environment_binding(binding(), RESOLVER)


def test_the_inline_secret_fixture_never_decodes_at_all() -> None:
    with pytest.raises(gen.HostContractDecodeError, match="rawToken"):
        gen.EnvironmentBinding.from_wire(
            load("invalid/environment-binding-inline-secret.json")
        )


def test_a_reference_field_carrying_a_raw_secret_never_decodes_at_all() -> None:
    """A PEM block is not an identifier, so the closed binding record refuses it
    at the contract boundary -- before any semantic check gets to see it.
    """
    document = json.loads(json.dumps(load("valid/environment-binding.json")))
    document["credentialRefs"] = ["-----BEGIN PRIVATE KEY-----\nMIIEv"]
    with pytest.raises(gen.HostContractDecodeError, match="credentialRefs") as excinfo:
        gen.EnvironmentBinding.from_wire(document)
    assert "BEGIN PRIVATE KEY" not in str(excinfo.value)


def test_a_reference_field_carrying_an_oversized_opaque_blob_is_refused() -> None:
    document = json.loads(json.dumps(load("valid/environment-binding.json")))
    document["credentialRefs"] = ["a" * 129]
    with pytest.raises(gen.HostContractDecodeError, match="credentialRefs"):
        gen.EnvironmentBinding.from_wire(document)


def test_a_binding_revision_must_advance_its_version() -> None:
    with pytest.raises(pub.ReleaseIntegrityError, match="bindingVersion"):
        pub.validate_binding_revision(
            binding(), binding(environmentId="environment-staging")
        )


def test_a_binding_revision_that_advances_is_accepted() -> None:
    pub.validate_binding_revision(
        binding(), binding(bindingVersion=2, environmentId="environment-staging")
    )


def test_a_binding_may_not_move_between_workspaces() -> None:
    secret = "ghp_" + "w" * 36
    with pytest.raises(pub.ReleaseIntegrityError) as excinfo:
        pub.validate_binding_revision(
            binding(), binding(bindingVersion=2, workspaceId=secret)
        )
    assert_non_disclosing_refusal(
        excinfo.value,
        secret,
        "binding revision cannot move between Workspaces",
    )


def test_a_different_binding_id_is_not_a_revision() -> None:
    pub.validate_binding_revision(binding(), binding(bindingId="binding-production-2"))


# --------------------------------------------------------------------------
# Release archives exclude bindings and environment authority
# --------------------------------------------------------------------------


def test_a_clean_release_archive_reports_nothing() -> None:
    assert (
        pub.release_archive_findings(
            {
                "manifest.json": load("valid/release-package-identity.json"),
                "assets/routes.json": {"routeId": "documents.home"},
            }
        )
        == ()
    )


def test_an_archive_carrying_an_environment_binding_is_a_finding() -> None:
    findings = pub.release_archive_findings(
        {"config/binding.json": load("valid/environment-binding.json")}
    )
    assert len(findings) == 1
    assert findings[0].startswith("member[0]:")
    assert "config/binding.json" not in findings[0]
    assert "environment binding" in findings[0]


def test_an_archive_carrying_environment_authority_is_a_finding() -> None:
    findings = pub.release_archive_findings(
        {"config/app.json": {"defaults": {"workspaceRef": "workspace-acme"}}}
    )
    assert len(findings) == 1
    assert "workspaceRef" in findings[0]


def test_release_archive_findings_never_disclose_untrusted_member_names() -> None:
    secret = "ghp_" + "a" * 36
    findings = pub.release_archive_findings(
        {f"{secret}.json": load("valid/environment-binding.json")}
    )
    assert len(findings) == 1
    assert findings[0].startswith("member[0]:")
    assert secret not in repr(findings)


def test_archive_findings_are_reported_for_every_member() -> None:
    findings = pub.release_archive_findings(
        {
            "a.json": load("valid/environment-binding.json"),
            "b.json": {"environmentId": "environment-production"},
        }
    )
    assert len(findings) == 2


@pytest.mark.parametrize(
    "malformed",
    [None, [], ["manifest.json"], "manifest.json", 0, {b"manifest.json": {}}, {1: {}}],
)
def test_a_malformed_member_inventory_is_a_finding_not_an_exception(
    malformed: object,
) -> None:
    """``members`` comes from an archive this gate has not cleared yet.

    Every other refusal in this module answers untrusted input with a finding or
    a contract error. An inventory of the wrong shape used to raise a bare
    ``TypeError`` out of ``sorted``/``__getitem__`` -- or, for an empty non-mapping,
    return ``()`` and read as *scanned and clean*. Both are the wrong answer for a
    gate whose empty tuple means the archive passed.
    """
    findings = pub.release_archive_findings(malformed)  # type: ignore[arg-type]

    assert len(findings) == 1
    assert findings[0].startswith("inventory:")


def test_a_mapping_proxy_inventory_is_still_scanned() -> None:
    """Read-only is not malformed: a resolver may hand this gate an immutable view."""
    members = MappingProxyType({"config/binding.json": load("valid/environment-binding.json")})

    findings = pub.release_archive_findings(members)

    assert len(findings) == 1
    assert "environment binding" in findings[0]


# --------------------------------------------------------------------------
# Development and conformance are contracts, never production runtime
# --------------------------------------------------------------------------


def test_the_development_only_records_are_the_governed_set() -> None:
    assert pub.DEVELOPMENT_ONLY_RECORDS == (
        "DevelopmentProfile",
        "ShellScenario",
        "TestDriverRequest",
        "TestDriverResult",
    )


def test_a_production_build_must_exclude_them_from_the_source_graph() -> None:
    findings = pub.scan_release_package(
        pub.PackageInventory(
            members=(pub.PackageMember("app.js", b"ok"),),
            source_symbols=("HostRequest", "TestDriverRequest"),
            source_graph_exhaustive=True,
            exhaustive=True,
        )
    )
    assert len(findings) == 1
    assert findings[0].code == "development_control_in_source_graph"
    assert "TestDriverRequest" in findings[0].detail


def test_a_production_build_must_exclude_them_from_the_packaged_bytes() -> None:
    findings = pub.scan_release_package(
        pub.PackageInventory(
            members=(
                pub.PackageMember("app.js", b'const fixture = "shell-scenario";'),
            ),
            source_symbols=("HostRequest",),
            source_graph_exhaustive=True,
            exhaustive=True,
        )
    )
    assert len(findings) == 1
    assert findings[0].code == "development_control_in_packaged_bytes"


def test_both_halves_must_pass_for_a_clean_production_build() -> None:
    assert (
        pub.scan_release_package(
            pub.PackageInventory(
                members=(pub.PackageMember("app.js", b"ok"),),
                source_symbols=("HostRequest", "HostResult"),
                source_graph_exhaustive=True,
                exhaustive=True,
            )
        )
        == ()
    )


def test_a_runtime_flag_alone_does_not_satisfy_production_exclusion() -> None:
    """The symbol is still in the source graph, so the build still fails."""
    findings = pub.scan_release_package(
        pub.PackageInventory(
            members=(pub.PackageMember("app.js", b"ok"),),
            source_symbols=("DevelopmentProfile",),
            source_graph_exhaustive=True,
            exhaustive=True,
            development_flag_enabled=False,
        )
    )
    assert len(findings) == 1


def test_this_package_ships_the_development_records_but_no_development_runtime() -> (
    None
):
    """Development and conformance controls are represented as contracts here and
    never as a production runtime that mounts sources, injects fixtures or
    drives a shell.
    """
    import inspect

    from omnivia_core.host_contract import v1

    # The records themselves are contracts and are meant to be here, including
    # the ones whose names describe a development action (SourceMount,
    # TestDriverRequest). What must not exist is a *function* that performs one.
    for record in (*pub.DEVELOPMENT_ONLY_RECORDS, "SourceMount", "ScenarioAssertion"):
        assert hasattr(v1, record), record
        assert inspect.isclass(getattr(v1, record)), record

    forbidden = ("mount", "inject", "launch", "spawn", "drive", "execute", "run_")
    functions = [
        name for name in v1.__all__ if inspect.isfunction(getattr(v1, name, None))
    ]
    offending = [
        name
        for name in functions
        if any(marker in name.lower() for marker in forbidden)
    ]
    assert offending == []
