"""Round-5 adversarial regressions for host-owned trust and package admission."""

from __future__ import annotations

import dataclasses
import io
import json
import stat
import subprocess
import sys
import tarfile
import traceback
import warnings
import zipfile
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.host_contract import v1
from omnivia_core.host_contract.v1 import codec
from omnivia_core.host_contract.v1 import generated as gen
from omnivia_core.host_contract.v1 import publication as pub
from omnivia_core.host_contract.v1 import registration as reg

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "contracts" / "host" / "v1" / "fixtures"
SCANNER = REPO_ROOT / "scripts" / "scan-release-package.py"


def load(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


def inventory(path: str, data: bytes) -> pub.PackageInventory:
    return pub.PackageInventory(
        members=(pub.PackageMember(path=path, data=data),),
        source_symbols=(),
        source_graph_exhaustive=True,
        exhaustive=True,
    )


def zip_bytes(entries: list[tuple[str, bytes]], *, symlink: bool = False) -> bytes:
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="Duplicate name: .*", category=UserWarning
        )
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, data in entries:
                if symlink:
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, data)
                else:
                    archive.writestr(name, data)
    return buffer.getvalue()


def tar_bytes(entries: list[tuple[str, bytes]], *, gzip: bool = False) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz" if gzip else "w") as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def finding_codes(data: bytes, path: str = "outer.zip") -> set[str]:
    return {finding.code for finding in pub.scan_release_package(inventory(path, data))}


# ---------------------------------------------------------------------------
# Recursive fail-closed package inventory
# ---------------------------------------------------------------------------


def test_nested_zip_escape_is_rejected() -> None:
    assert "escaping_member" in finding_codes(zip_bytes([("../escape.js", b"ok")]))


def test_nested_zip_duplicate_is_rejected() -> None:
    nested = zip_bytes([("app.js", b"first"), ("app.js", b"second")])
    assert "duplicate_member" in finding_codes(nested)


def test_nested_zip_symlink_is_rejected_from_external_attributes() -> None:
    nested = zip_bytes([("link.js", b"target.js")], symlink=True)
    assert "symlinked_member" in finding_codes(nested)


@pytest.mark.parametrize(
    ("path", "nested"),
    [
        ("inner.tar", tar_bytes([("app.js", b"TestDriverRequest")])),
        ("inner.tar.gz", tar_bytes([("app.js", b"TestDriverRequest")], gzip=True)),
        ("inner.tgz", tar_bytes([("app.js", b"TestDriverRequest")], gzip=True)),
    ],
)
def test_nested_tar_variants_are_scanned(path: str, nested: bytes) -> None:
    assert "development_control_in_packaged_bytes" in finding_codes(nested, path)


def test_mixed_zip_tar_nesting_is_scanned_to_the_governed_bound() -> None:
    deepest = zip_bytes([("app.js", b"TestDriverResult")])
    middle = tar_bytes([("payload.zip", deepest)])
    outer = zip_bytes([("payload.tar", middle)])
    assert "development_control_in_packaged_bytes" in finding_codes(outer)


def test_archive_nesting_past_the_governed_bound_fails_closed() -> None:
    nested = b"clean"
    for depth in range(pub.MAX_ARCHIVE_DEPTH + 2):
        nested = zip_bytes([(f"level-{depth}.zip", nested)])
    assert "incomplete_inventory" in finding_codes(nested)


@pytest.mark.parametrize(
    "symbols",
    [
        ["HostRequest"],
        ("HostRequest", object()),
    ],
)
def test_source_graph_requires_an_exact_tuple_of_exact_strings(symbols: object) -> None:
    findings = pub.scan_release_package(
        pub.PackageInventory(
            members=(),
            source_symbols=symbols,  # type: ignore[arg-type]
            source_graph_exhaustive=True,
            exhaustive=True,
        )
    )
    assert "incomplete_inventory" in {finding.code for finding in findings}


def test_source_graph_exact_tuple_of_exact_strings_is_the_clean_control() -> None:
    assert (
        pub.scan_release_package(
            pub.PackageInventory(
                members=(),
                source_symbols=("HostRequest", "HostResult"),
                source_graph_exhaustive=True,
                exhaustive=True,
            )
        )
        == ()
    )


@pytest.mark.parametrize("field", ("exhaustive", "development_flag_enabled"))
def test_inventory_flags_require_exact_booleans(field: str) -> None:
    values: dict[str, object] = {
        "members": (),
        "source_symbols": (),
        "source_graph_exhaustive": True,
        "exhaustive": True,
        "development_flag_enabled": False,
    }
    values[field] = 1
    findings = pub.scan_release_package(pub.PackageInventory(**values))  # type: ignore[arg-type]
    assert "incomplete_inventory" in {finding.code for finding in findings}


def test_nested_archive_directories_count_toward_the_member_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pub, "MAX_SCANNED_MEMBERS", 2)
    nested = zip_bytes([("one/", b""), ("two/", b"")])
    assert "incomplete_inventory" in finding_codes(nested)


def test_hostile_string_subclass_in_source_graph_fails_closed() -> None:
    class HostileString(str):
        pass

    findings = pub.scan_release_package(
        pub.PackageInventory(
            members=(),
            source_symbols=(HostileString("TestDriverRequest"),),
            source_graph_exhaustive=True,
            exhaustive=True,
        )
    )
    assert "incomplete_inventory" in {finding.code for finding in findings}


def test_secret_shaped_nested_member_name_is_never_disclosed() -> None:
    secret = "ghp_" + "z" * 36
    nested = zip_bytes([(f"{secret}.js", b"TestDriverRequest")])
    findings = pub.scan_release_package(inventory("outer.zip", nested))
    assert findings
    rendered = "\n".join(
        f"{finding.member} {finding.detail} {finding.detector}" for finding in findings
    )
    assert secret not in rendered
    for finding in findings:
        assert secret not in repr(finding)


def test_cli_rejects_nested_escape_and_does_not_disclose_member_names(
    tmp_path: Path,
) -> None:
    secret = "ghp_" + "q" * 36
    archive_path = tmp_path / "release.zip"
    archive_path.write_bytes(zip_bytes([(f"../{secret}.js", b"ok")]))
    result = subprocess.run(
        [sys.executable, str(SCANNER), str(archive_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 1
    assert "escaping_member" in result.stdout
    assert secret not in result.stdout
    assert secret not in result.stderr


# ---------------------------------------------------------------------------
# Container bytes that belong to no member
# ---------------------------------------------------------------------------

CONTAINER_SECRET = "ghp_" + "c" * 36


def zip_comment_bytes(planted: bytes) -> bytes:
    """A readable ZIP whose archive comment carries the planted bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("app.js", b"clean")
        archive.comment = planted
    return buffer.getvalue()


def zip_trailing_bytes(planted: bytes) -> bytes:
    """A readable ZIP with the planted bytes appended after its EOCD record."""
    return zip_bytes([("app.js", b"clean")]) + planted


def tar_padding_bytes(planted: bytes) -> bytes:
    """A readable tar with the planted bytes written over its trailing padding."""
    data = bytearray(tar_bytes([("app.js", b"clean")]))
    data[-len(planted) :] = planted
    return bytes(data)


CONTAINER_VECTORS = {
    "zip_comment": (zip_comment_bytes, "release.zip"),
    "zip_trailing": (zip_trailing_bytes, "release.zip"),
    "tar_padding": (tar_padding_bytes, "release.tar"),
}


@pytest.mark.parametrize("vector", sorted(CONTAINER_VECTORS))
@pytest.mark.parametrize(
    ("planted", "expected_code", "expected_detector"),
    [
        (
            CONTAINER_SECRET.encode("ascii"),
            "likely_secret_in_packaged_bytes",
            "github_token",
        ),
        (
            b"TestDriverRequest",
            "development_control_in_packaged_bytes",
            "TestDriverRequest",
        ),
    ],
)
def test_container_bytes_outside_every_member_are_scanned(
    vector: str, planted: bytes, expected_code: str, expected_detector: str
) -> None:
    build, path = CONTAINER_VECTORS[vector]
    data = build(planted)

    findings = pub.scan_release_package(inventory(path, data))

    assert [(finding.code, finding.detector) for finding in findings] == [
        (expected_code, expected_detector)
    ]
    assert findings[0].member == "member[0]"
    assert findings[0].offset >= 0


@pytest.mark.parametrize("vector", sorted(CONTAINER_VECTORS))
def test_a_container_carrying_nothing_planted_stays_clean(vector: str) -> None:
    build, path = CONTAINER_VECTORS[vector]
    assert pub.scan_release_package(inventory(path, build(b"harmless"))) == ()


def test_container_scanning_does_not_duplicate_a_finding_its_member_reported() -> None:
    findings = pub.scan_release_package(
        inventory("release.zip", zip_bytes([("app.js", b"TestDriverRequest")]))
    )

    assert [(finding.code, finding.member) for finding in findings] == [
        ("development_control_in_packaged_bytes", "member[0]!member[0]")
    ]


def test_container_byte_findings_never_disclose_the_bytes_they_refused() -> None:
    findings = pub.scan_release_package(
        inventory("release.zip", zip_comment_bytes(CONTAINER_SECRET.encode("ascii")))
    )

    assert findings
    for finding in findings:
        assert CONTAINER_SECRET not in repr(finding)


def test_cli_rejects_a_secret_parked_in_a_packaged_archive_comment(
    tmp_path: Path,
) -> None:
    """The CLI reaches container bytes through the member scan it already runs.

    A packaged archive is a member, and a member's bytes are scanned whole, so
    the comment travels into the gate with them. The CLI's own reader
    decomposes the *top-level* package instead of handing over its bytes, so
    the bytes outside that outermost container's members never become
    inventory -- a separate reader-side gap, not this one.
    """
    secret = "ghp_" + "m" * 36
    package = tmp_path / "package"
    (package / "vendor").mkdir(parents=True)
    (package / "index.html").write_bytes(b"<!doctype html>")
    (package / "vendor" / "bundle.zip").write_bytes(
        zip_comment_bytes(secret.encode("ascii"))
    )

    result = subprocess.run(
        [sys.executable, str(SCANNER), str(package)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 1
    assert "likely_secret_in_packaged_bytes" in result.stdout
    assert secret not in result.stdout
    assert secret not in result.stderr


# ---------------------------------------------------------------------------
# Host-owned registration capability
# ---------------------------------------------------------------------------


def declaration() -> gen.ModuleContributionDeclaration:
    return gen.ModuleContributionDeclaration.from_wire(
        load("valid/module-contribution.json")
    )


def release_identity() -> gen.ReleasePackageIdentity:
    return gen.ReleasePackageIdentity.from_wire(
        load("valid/release-package-identity.json")
    )


def caller_registry() -> reg.ContributionRegistry:
    return reg.ContributionRegistry(
        available_payload_schema_ids=frozenset({"schema.documents.route.v1"}),
        available_capabilities=frozenset({"navigation.register"}),
        declared_entitlements=frozenset({"documents.use"}),
        host_target="desktop",
        trusted_publisher_ids=frozenset({"publisher.omnivia"}),
        trusted_signing_key_ids=frozenset({"key.release.2026"}),
    )


def caller_evidence() -> reg.RegistrationEvidence:
    return reg.RegistrationEvidence(
        release_identity=release_identity(),
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


def test_caller_authored_registration_assertions_cannot_self_register() -> None:
    assert not hasattr(reg, "register_contributions")


def test_registration_trust_inputs_are_not_public_facade_exports() -> None:
    for name in ("ContributionRegistry", "RegistrationEvidence"):
        assert name not in reg.__all__
        assert name not in v1.__all__


# ---------------------------------------------------------------------------
# Host-owned operation boundary
# ---------------------------------------------------------------------------


def published_operation(**overrides: Any) -> codec.PublishedOperation:
    values: dict[str, Any] = {
        "namespace": "workspace",
        "operation_id": "get_active",
        "request_schema_id": "schema.workspace.get-active.request.v1",
        "result_schema_id": "schema.workspace.get-active.result.v1",
        "effect_class": "read",
        "idempotency_required": False,
        "required_capabilities": (),
        "minimum_contract_minor": 0,
    }
    values.update(overrides)
    return codec.PublishedOperation(**values)


def catalogue(*operations: codec.PublishedOperation) -> codec.OperationCatalogue:
    return codec.OperationCatalogue(operations=operations)


@pytest.mark.parametrize(
    "operation",
    [
        published_operation(request_schema_id="schema.false.request.v1"),
        published_operation(result_schema_id="schema.false.result.v1"),
        published_operation(effect_class="consequential_external"),
        published_operation(idempotency_required=True),
        published_operation(required_capabilities=("attacker.admin",)),
        published_operation(minimum_contract_minor=9),
    ],
)
def test_caller_authored_metadata_cannot_redefine_a_known_operation(
    operation: codec.PublishedOperation,
) -> None:
    authentic = v1.BoundHost(
        operation_catalogue=catalogue(published_operation()),
        contribution_registry=caller_registry(),
        registration_evidence=(caller_evidence(),),
    )
    v1.BoundHost(
        operation_catalogue=catalogue(operation),
        contribution_registry=caller_registry(),
        registration_evidence=(caller_evidence(),),
    )
    assert (
        authentic.admit_request(load("valid/host-request.json")).operation
        == "get_active"
    )


def test_caller_cannot_self_publish_an_unknown_operation() -> None:
    payload = load("valid/host-request.json")
    payload["operation"] = "attacker_operation"
    authentic = v1.BoundHost(
        operation_catalogue=catalogue(published_operation()),
        contribution_registry=caller_registry(),
        registration_evidence=(caller_evidence(),),
    )
    caller_owned = v1.BoundHost(
        operation_catalogue=catalogue(
            published_operation(operation_id="attacker_operation")
        ),
        contribution_registry=caller_registry(),
        registration_evidence=(caller_evidence(),),
    )
    assert caller_owned.admit_request(payload).operation == "attacker_operation"
    with pytest.raises(gen.HostContractDecodeError, match="unsupported_contract_value"):
        authentic.admit_request(payload)


def test_operation_catalogue_constructors_are_not_public_facade_exports() -> None:
    for name in ("PublishedOperation", "OperationCatalogue"):
        assert name not in codec.__all__
        assert name not in v1.__all__


def test_hostile_runtime_operation_metadata_cannot_be_published() -> None:
    class EqualitySpoof(str):
        def __eq__(self, other: object) -> bool:
            return True

        __hash__ = str.__hash__

    hostile = published_operation(operation_id=EqualitySpoof("attacker_operation"))
    with pytest.raises(gen.HostContractDecodeError, match="malformed operation entry"):
        v1.BoundHost(
            operation_catalogue=catalogue(hostile),
            contribution_registry=caller_registry(),
            registration_evidence=(caller_evidence(),),
        )


# ---------------------------------------------------------------------------
# Optional logical-reference provenance
# ---------------------------------------------------------------------------


OPTIONAL_REFERENCES = {
    "dataResidencyPolicyRef": ("policy.residency.eu", "data_residency_policy"),
    "runtimeLimitsRef": ("limits.standard", "runtime_limits"),
    "executionPlacementRef": ("placement.eu-west", "execution_placement"),
}


def binding(**overrides: Any) -> gen.EnvironmentBinding:
    document = json.loads(json.dumps(load("valid/environment-binding.json")))
    document.update(overrides)
    return gen.EnvironmentBinding.from_wire(document)


def base_references() -> list[pub.ApprovedReference]:
    return [
        pub.ApprovedReference(
            "credential.documents.storage", "credential", "workspace-acme"
        ),
        pub.ApprovedReference(
            "integration.documents.index", "integration", "workspace-acme"
        ),
        pub.ApprovedReference("storage.documents.primary", "storage", "workspace-acme"),
        pub.ApprovedReference("domain.documents.example", "domain", "workspace-acme"),
        pub.ApprovedReference(
            "policy.residency.au", "data_residency_policy", "workspace-acme"
        ),
    ]


@pytest.mark.parametrize("field", OPTIONAL_REFERENCES)
def test_each_optional_logical_reference_must_resolve(field: str) -> None:
    with pytest.raises(pub.BindingReferenceError) as excinfo:
        pub.validate_environment_binding(
            binding(**{field: "token-shaped-but-unresolved"}),
            pub.ReferenceRegistry(tuple(base_references())),
        )
    assert excinfo.value.code == "unresolved_reference"
    assert excinfo.value.field == field


@pytest.mark.parametrize("field", OPTIONAL_REFERENCES)
@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"kind": "credential"}, "wrong_reference_kind"),
        ({"workspace_id": "workspace-other"}, "cross_workspace_reference"),
        ({"verified": False}, "unverified_reference"),
        ({"revoked": True}, "revoked_reference"),
    ],
)
def test_optional_logical_reference_provenance_is_fully_checked(
    field: str, mutation: dict[str, Any], expected: str
) -> None:
    reference_id, kind = OPTIONAL_REFERENCES[field]
    entry = pub.ApprovedReference(reference_id, kind, "workspace-acme")
    entry = dataclasses.replace(entry, **mutation)
    resolver = pub.ReferenceRegistry((*base_references(), entry))
    with pytest.raises(pub.BindingReferenceError) as excinfo:
        pub.validate_environment_binding(binding(**{field: reference_id}), resolver)
    assert excinfo.value.code == expected


@pytest.mark.parametrize("field", OPTIONAL_REFERENCES)
def test_a_valid_optional_logical_reference_is_admitted(field: str) -> None:
    reference_id, kind = OPTIONAL_REFERENCES[field]
    resolver = pub.ReferenceRegistry(
        (
            *base_references(),
            pub.ApprovedReference(reference_id, kind, "workspace-acme"),
        )
    )
    pub.validate_environment_binding(binding(**{field: reference_id}), resolver)


def test_hostile_optional_reference_type_is_refused_without_disclosure() -> None:
    class EqualitySpoof(str):
        def __eq__(self, other: object) -> bool:
            return True

        __hash__ = str.__hash__

    secret = EqualitySpoof("ghp_" + "s" * 36)
    hostile = dataclasses.replace(binding(), runtime_limits_ref=secret)
    with pytest.raises(gen.HostContractDecodeError) as excinfo:
        pub.validate_environment_binding(
            hostile, pub.ReferenceRegistry(tuple(base_references()))
        )
    assert str(secret) not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Publication equality spoofing and direct-construction admission
# ---------------------------------------------------------------------------


class EqualitySpoof(str):
    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False

    def __hash__(self) -> int:
        return hash("release-documents-1")


def promotion() -> gen.PromotionRecord:
    return gen.PromotionRecord.from_wire(load("valid/promotion-record.json"))


def rollback() -> gen.RollbackRecord:
    return gen.RollbackRecord.from_wire(load("valid/rollback-record.json"))


def test_promotion_equality_spoof_is_refused() -> None:
    secret = EqualitySpoof("ghp_" + "p" * 36)
    hostile = dataclasses.replace(promotion(), release_id=secret)
    with pytest.raises(
        pub.ReleaseIntegrityError, match="invalid publication record"
    ) as excinfo:
        pub.validate_promotion(hostile, release_identity())
    assert str(secret) not in str(excinfo.value)


def test_binding_cross_workspace_equality_spoof_is_refused() -> None:
    hostile = dataclasses.replace(
        binding(bindingVersion=2), workspace_id=EqualitySpoof("workspace-attacker")
    )
    with pytest.raises(pub.ReleaseIntegrityError, match="invalid publication record"):
        pub.validate_binding_revision(binding(), hostile)


def test_rollback_release_membership_equality_spoof_is_refused() -> None:
    hostile = dataclasses.replace(
        rollback(), restored_release_id=EqualitySpoof("release-attacker")
    )
    with pytest.raises(pub.ReleaseIntegrityError, match="invalid publication record"):
        pub.validate_rollback(hostile, frozenset({"release-documents-1"}))


def test_rollback_failed_promotion_equality_spoof_is_refused() -> None:
    hostile = dataclasses.replace(
        promotion(), promotion_id=EqualitySpoof("promotion-attacker")
    )
    with pytest.raises(pub.ReleaseIntegrityError, match="invalid publication record"):
        pub.validate_rollback(
            rollback(), frozenset({"release-documents-1"}), failed_promotion=hostile
        )


def test_publication_validators_refuse_runtime_subclasses_and_malformed_records() -> (
    None
):
    class PromotionSubclass(gen.PromotionRecord):
        pass

    source = promotion()
    hostile = PromotionSubclass(**dataclasses.asdict(source))
    with pytest.raises(pub.ReleaseIntegrityError, match="invalid publication record"):
        pub.validate_promotion(hostile, release_identity())

    malformed = dataclasses.replace(release_identity(), version="not-semver")
    with pytest.raises(pub.ReleaseIntegrityError, match="invalid publication record"):
        pub.validate_same_package(malformed, release_identity())


def test_publication_refusals_do_not_retain_decoder_context() -> None:
    malformed = dataclasses.replace(promotion(), release_id="not valid")
    with pytest.raises(pub.ReleaseIntegrityError) as excinfo:
        pub.validate_promotion(malformed, release_identity())
    error = excinfo.value
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    assert "not valid" not in rendered
