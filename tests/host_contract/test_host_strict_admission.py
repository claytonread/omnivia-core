"""Strict public wire admission and emission for the Host Contract v1.

The governed JSON Schema is the normative definition of a valid Host Contract
value. This module is the differential proof that the *public* Python surface
admits exactly what that schema admits -- no more:

- **admission parity** -- for every case below, ``Record.from_wire`` accepts a
  document if and only if the canonical schema's matching ``$defs`` subschema
  does. Patterns, integer bounds, string lengths, RFC 3339 timestamps, array
  cardinality, uniqueness and the result-branch invariants are all included;
- **no structural bypass** -- ``from_wire`` is contract-validating rather than
  merely structural, so there is no public entry point that skips the governed
  constraints;
- **direct-constructor attacks** -- a type annotation is not a runtime
  guarantee. A record built by calling its constructor with hostile values is
  refused by ``validate()`` and by every public encoder;
- **encoders validate before emission** -- an invalid record can never be
  turned into a wire document, and ``encode_host_result`` enforces the total
  envelope in particular;
- **supported-version admission** -- the codec speaks exactly Host Contract
  1.0.0. A schema-valid but unsupported minor is refused before an operation
  runs, and a v2 document never reaches an operation at all.

``jsonschema`` is a development-only conformance gate here, never a runtime
dependency of the contract package.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _host_fixtures import bound_host
from jsonschema import Draft202012Validator

from omnivia_core.host_contract.v1 import codec, compatibility
from omnivia_core.host_contract.v1 import generated as gen

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_ROOT = REPO_ROOT / "contracts" / "host" / "v1"
SCHEMA: dict[str, Any] = json.loads(
    (CANONICAL_ROOT / "schemas" / "host-contract-v1.schema.json").read_text(encoding="utf-8")
)
FIXTURES = CANONICAL_ROOT / "fixtures"

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
    )
)
TEST_HOST = bound_host(_TEST_OPERATION_ADMISSION)


def load(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


def _validator(definition: str) -> Draft202012Validator:
    """Return a validator for exactly one ``$defs`` subschema.

    The root schema is a ``oneOf`` across every governed record, so validating
    a mutated document there answers "is this *any* record", not "is this still
    the record it claims to be". Pinning the definition is what makes the
    comparison against a specific ``from_wire`` meaningful.
    """
    return Draft202012Validator(
        {
            "$schema": SCHEMA["$schema"],
            "$id": f"{SCHEMA['$id']}#{definition}",
            "$defs": SCHEMA["$defs"],
            "$ref": f"#/$defs/{definition}",
        },
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


VALIDATORS = {name: _validator(name) for name in SCHEMA["$defs"]}

#: ``$defs`` key -> the generated record that decodes it.
RECORDS: dict[str, type[Any]] = {
    "request": gen.HostRequest,
    "result": gen.HostResult,
    "shellContext": gen.ShellContext,
    "compatibilityResult": gen.CompatibilityResult,
    "availability": gen.CapabilityAvailability,
    "denial": gen.CapabilityDenial,
    "remediation": gen.Remediation,
    "moduleContributionDeclaration": gen.ModuleContributionDeclaration,
    "releasePackageIdentity": gen.ReleasePackageIdentity,
    "promotionRecord": gen.PromotionRecord,
    "rollbackRecord": gen.RollbackRecord,
    "environmentBinding": gen.EnvironmentBinding,
    "developmentProfile": gen.DevelopmentProfile,
    "shellScenario": gen.ShellScenario,
    "testDriverRequest": gen.TestDriverRequest,
    "testDriverResult": gen.TestDriverResult,
}

BASE = {
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


def document(definition: str, **overrides: Any) -> dict[str, Any]:
    """Return a governed fixture with ``overrides`` applied; ``None`` removes a field."""
    if definition == "availability":
        base: dict[str, Any] = {
            "state": "available",
            "capabilityId": "files.export",
            "target": "desktop",
        }
    else:
        base = json.loads(json.dumps(load(BASE[definition])))
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not None}


def admits(definition: str, payload: Any) -> bool:
    """Return whether the public decoder for ``definition`` accepts ``payload``."""
    try:
        RECORDS[definition].from_wire(payload)
    except gen.HostContractDecodeError:
        return False
    return True


LONG_ID = "a" * 128
OVERLONG_ID = "a" * 129
LONG_REASON = "r" * 500
OVERLONG_REASON = "r" * 501

#: ``(label, definition, document)``. Each is validated against the canonical
#: subschema and against the public decoder, and the two verdicts must agree.
#: The expected verdict is deliberately *not* written down here: the schema is
#: the authority, so the test cannot drift from it by mis-transcribing a rule.
CASES: list[tuple[str, str, Any]] = [
    # -- baselines ---------------------------------------------------------
    *[(f"fixture:{key}", key, document(key)) for key in sorted(BASE)],
    ("availability:minimal", "availability", document("availability")),
    # -- contract version --------------------------------------------------
    ("request:contract-version-v2", "request", document("request", contractVersion="2.0.0")),
    ("request:contract-version-partial", "request", document("request", contractVersion="1.0")),
    ("request:contract-version-prerelease", "request", document("request", contractVersion="1.0.0-rc1")),
    ("request:contract-version-leading-zero", "request", document("request", contractVersion="1.01.0")),
    ("request:contract-version-additive-minor", "request", document("request", contractVersion="1.7.0")),
    # -- identifier pattern ------------------------------------------------
    ("request:empty-request-id", "request", document("request", requestId="")),
    ("request:leading-dash-request-id", "request", document("request", requestId="-nope")),
    ("request:spaced-operation", "request", document("request", operation="get active")),
    ("request:slashed-context-id", "request", document("request", contextId="ctx/001")),
    ("request:max-length-request-id", "request", document("request", requestId=LONG_ID)),
    ("request:overlong-request-id", "request", document("request", requestId=OVERLONG_ID)),
    # -- integer bounds ----------------------------------------------------
    ("request:negative-expected-state-version", "request", document("request", expectedStateVersion=-1)),
    ("request:zero-expected-state-version", "request", document("request", expectedStateVersion=0)),
    ("context:zero-context-version", "shellContext", document("shellContext", contextVersion=0)),
    ("context:one-context-version", "shellContext", document("shellContext", contextVersion=1)),
    ("availability:negative-retry", "availability", document("availability", retryAfterSeconds=-1)),
    ("availability:zero-retry", "availability", document("availability", retryAfterSeconds=0)),
    ("profile:zero-port", "developmentProfile", document("developmentProfile", ports=[0])),
    ("profile:max-port", "developmentProfile", document("developmentProfile", ports=[65535])),
    ("profile:overmax-port", "developmentProfile", document("developmentProfile", ports=[65536])),
    ("binding:zero-binding-version", "environmentBinding", document("environmentBinding", bindingVersion=0)),
    # -- RFC 3339 timestamps ----------------------------------------------
    ("context:issued-at-garbage", "shellContext", document("shellContext", issuedAt="not-a-time")),
    ("context:issued-at-no-zone", "shellContext", document("shellContext", issuedAt="2026-08-03T00:00:00")),
    ("context:issued-at-date-only", "shellContext", document("shellContext", issuedAt="2026-08-03")),
    ("context:issued-at-month-13", "shellContext", document("shellContext", issuedAt="2026-13-03T00:00:00Z")),
    ("context:issued-at-day-32", "shellContext", document("shellContext", issuedAt="2026-01-32T00:00:00Z")),
    ("context:issued-at-hour-24", "shellContext", document("shellContext", issuedAt="2026-01-01T24:00:00Z")),
    ("context:issued-at-offset", "shellContext", document("shellContext", issuedAt="2026-08-03T10:00:00+10:00")),
    ("context:issued-at-fraction", "shellContext", document("shellContext", issuedAt="2026-08-03T10:00:00.123456Z")),
    ("context:issued-at-lowercase-t", "shellContext", document("shellContext", issuedAt="2026-08-03t10:00:00z")),
    ("context:issued-at-bad-offset", "shellContext", document("shellContext", issuedAt="2026-08-03T10:00:00+25:00")),
    ("context:expires-at-valid", "shellContext", document("shellContext", expiresAt="2026-08-04T00:00:00Z")),
    ("context:expires-at-invalid", "shellContext", document("shellContext", expiresAt="tomorrow")),
    ("binding:created-at-date-only", "environmentBinding", document("environmentBinding", createdAt="2026-08-03")),
    # -- string lengths ----------------------------------------------------
    ("denial:empty-safe-reason", "denial", document("denial", safeReason="")),
    ("denial:max-safe-reason", "denial", document("denial", safeReason=LONG_REASON)),
    ("denial:overlong-safe-reason", "denial", document("denial", safeReason=OVERLONG_REASON)),
    ("availability:overlong-safe-reason", "availability", document("availability", safeReason=OVERLONG_REASON)),
    ("driver-result:empty-safe-reason", "testDriverResult", document("testDriverResult", safeReason="")),
    (
        "release:empty-target-entry",
        "releasePackageIdentity",
        document("releasePackageIdentity", targetEntries=[{"target": "desktop", "entry": ""}]),
    ),
    # -- SHA-256 and SemVer patterns --------------------------------------
    ("release:uppercase-digest", "releasePackageIdentity", document("releasePackageIdentity", manifestSha256="A" * 64)),
    ("release:short-digest", "releasePackageIdentity", document("releasePackageIdentity", manifestSha256="a" * 63)),
    ("release:version-partial", "releasePackageIdentity", document("releasePackageIdentity", version="1.0")),
    ("release:version-leading-zero", "releasePackageIdentity", document("releasePackageIdentity", version="01.0.0")),
    ("release:version-valid", "releasePackageIdentity", document("releasePackageIdentity", version="10.20.30")),
    # -- array cardinality and uniqueness ---------------------------------
    (
        "declaration:no-contributions",
        "moduleContributionDeclaration",
        document("moduleContributionDeclaration", contributions=[]),
    ),
    ("release:no-target-entries", "releasePackageIdentity", document("releasePackageIdentity", targetEntries=[])),
    ("binding:duplicate-credential-refs", "environmentBinding", document("environmentBinding", credentialRefs=["a", "a"])),
    ("binding:empty-credential-refs", "environmentBinding", document("environmentBinding", credentialRefs=[])),
    ("profile:duplicate-ports", "developmentProfile", document("developmentProfile", ports=[4173, 4173])),
    (
        "compat:duplicate-required-missing",
        "compatibilityResult",
        document("compatibilityResult", requiredMissing=["files.export", "files.export"]),
    ),
    (
        "compat:duplicate-safe-reasons",
        "compatibilityResult",
        document("compatibilityResult", safeReasons=["same", "same"]),
    ),
    (
        "declaration:duplicate-required-capabilities",
        "moduleContributionDeclaration",
        document(
            "moduleContributionDeclaration",
            contributions=[
                {
                    "contributionId": "documents.route.home",
                    "kind": "route",
                    "targetId": "documents.home",
                    "requiredCapabilities": ["navigation.register", "navigation.register"],
                    "optionalCapabilities": [],
                    "payloadSchemaId": "schema.documents.route.v1",
                }
            ],
        ),
    ),
    # -- nested records ----------------------------------------------------
    (
        "result:nested-denial-empty-reason",
        "result",
        document(
            "result",
            outcome="denied",
            data=None,
            denial={**load("denial/permission-denied.json"), "safeReason": ""},
        ),
    ),
    (
        "availability:nested-remediation-unknown-kind",
        "availability",
        document("availability", remediation={"kind": "reboot"}),
    ),
    (
        "availability:nested-remediation-bad-route",
        "availability",
        document("availability", remediation={"kind": "configure", "routeId": "bad route"}),
    ),
    # -- total result envelope --------------------------------------------
    ("result:success-without-data", "result", document("result", data=None)),
    (
        "result:success-with-denial",
        "result",
        document("result", denial=load("denial/permission-denied.json")),
    ),
    (
        "result:degraded-success",
        "result",
        document(
            "result",
            availability={
                "state": "unavailable_on_target",
                "capabilityId": "notifications.native",
                "target": "cloud",
            },
        ),
    ),
    (
        "result:denied-with-data",
        "result",
        document("result", outcome="denied", denial=load("denial/permission-denied.json")),
    ),
    (
        "result:unavailable-without-availability",
        "result",
        document("result", outcome="unavailable", data=None),
    ),
    (
        "result:failed-with-availability",
        "result",
        document(
            "result",
            outcome="failed",
            data=None,
            failure={"code": "internal_error", "safeReason": "Boom."},
            availability={
                "state": "temporarily_unhealthy",
                "capabilityId": "files.export",
                "target": "desktop",
            },
        ),
    ),
]


@pytest.mark.parametrize(("label", "definition", "payload"), CASES, ids=[case[0] for case in CASES])
def test_public_admission_matches_the_canonical_schema(
    label: str, definition: str, payload: Any
) -> None:
    """The public decoder accepts exactly what the governed schema accepts."""
    schema_valid = VALIDATORS[definition].is_valid(payload)
    public_accepted = admits(definition, payload)
    assert public_accepted is schema_valid, (
        f"{label}: schema_valid={schema_valid} public_accepted={public_accepted}; "
        f"errors={[error.message for error in VALIDATORS[definition].iter_errors(payload)]}"
    )


def test_the_differential_corpus_covers_both_verdicts() -> None:
    """A corpus that only ever agreed on ``True`` would prove nothing."""
    verdicts = {VALIDATORS[definition].is_valid(payload) for _, definition, payload in CASES}
    assert verdicts == {True, False}


# --------------------------------------------------------------------------
# Direct-constructor attacks: a type annotation is not a runtime guarantee
# --------------------------------------------------------------------------


def valid_context() -> gen.ShellContext:
    return gen.ShellContext.from_wire(load("valid/shell-context.json"))


def test_a_directly_constructed_record_with_a_hostile_value_is_refused() -> None:
    hostile = gen.ShellContext(
        context_id="ctx-001",
        context_version=0,
        principal_ref="principal-local-001",
        organisation_ref="org-001",
        workspace_ref="workspace-001",
        entitlement_summary_ref="entitlements-001",
        permission_summary_ref="permissions-001",
        runtime_id="desktop-runtime-001",
        environment_id="local-environment-001",
        target="desktop",
        issued_at="whenever",
    )
    with pytest.raises(gen.HostContractDecodeError):
        hostile.validate()
    with pytest.raises(gen.HostContractDecodeError):
        hostile.to_wire()
    with pytest.raises(gen.HostContractDecodeError):
        codec.encode_shell_context(hostile)


def test_a_directly_constructed_record_with_a_wrongly_typed_value_is_refused() -> None:
    hostile = gen.CapabilityAvailability(
        state="available",
        capability_id=object(),  # type: ignore[arg-type]
        target="desktop",
    )
    with pytest.raises(gen.HostContractDecodeError):
        hostile.to_wire()


def test_a_string_subclass_cannot_bypass_direct_record_validation() -> None:
    class EqualitySpoofString(str):
        def __eq__(self, other: object) -> bool:
            return True

        def __repr__(self) -> str:
            raise AssertionError("attacker repr must not be called")

    hostile = gen.ShellContext(
        context_id=EqualitySpoofString("attacker-controlled-secret"),
        context_version=1,
        principal_ref="principal-local-001",
        organisation_ref="org-001",
        workspace_ref="workspace-001",
        entitlement_summary_ref="entitlements-001",
        permission_summary_ref="permissions-001",
        runtime_id="desktop-runtime-001",
        environment_id="local-environment-001",
        target="desktop",
        issued_at="2026-08-03T00:00:00Z",
    )

    with pytest.raises(gen.HostContractDecodeError, match="contextId") as excinfo:
        hostile.validate()
    assert "attacker-controlled-secret" not in str(excinfo.value)


def test_a_directly_constructed_record_with_an_unknown_enum_member_is_refused() -> None:
    hostile = gen.CapabilityAvailability(
        state="probably_fine", capability_id="files.export", target="desktop"
    )
    with pytest.raises(gen.HostContractDecodeError, match="unsupported_contract_value"):
        hostile.to_wire()


def test_a_directly_constructed_nested_record_is_validated_too() -> None:
    hostile = gen.HostResult(
        contract_version="1.0.0",
        request_id="req-001",
        outcome="denied",
        correlation_id="corr-001",
        denial=gen.CapabilityDenial(
            category="permission_denied",
            capability_id="files.export",
            safe_reason="",
            approval_available=False,
            correlation_id="corr-001",
        ),
    )
    with pytest.raises(gen.HostContractDecodeError, match="safeReason"):
        hostile.to_wire()


def test_a_directly_constructed_array_of_records_is_validated_element_by_element() -> None:
    hostile = gen.ReleasePackageIdentity(
        app_id="app.documents",
        release_id="release-documents-1",
        version="1.0.0",
        manifest_sha256="a" * 64,
        payload_sha256="b" * 64,
        publisher_id="publisher.omnivia",
        signing_key_id="key.release.2026",
        target_entries=(gen.TargetEntry(target="desktop", entry=""),),
    )
    with pytest.raises(gen.HostContractDecodeError, match="entry"):
        hostile.to_wire()


def test_a_directly_constructed_record_with_a_non_json_payload_is_refused() -> None:
    hostile = gen.HostRequest(
        contract_version="1.0.0",
        request_id="req-001",
        namespace="workspace",
        operation="get_active",
        context_id="ctx-001",
        payload={"handle": object()},
    )
    with pytest.raises(gen.HostContractDecodeError, match="payload"):
        hostile.to_wire()


def test_a_directly_constructed_record_may_not_drop_a_required_array() -> None:
    hostile = gen.ModuleContributionDeclaration(
        declaration_version="1.0.0",
        module_id="module.documents",
        module_release_id="release-documents-1",
        contributions=(),
        required_entitlements=(),
    )
    with pytest.raises(gen.HostContractDecodeError, match="contributions"):
        hostile.to_wire()


# --------------------------------------------------------------------------
# Every public encoder validates before emission
# --------------------------------------------------------------------------


def test_encode_host_result_refuses_a_success_with_no_data_branch() -> None:
    """The exact false pass the independent review executed."""
    hostile = gen.HostResult(
        contract_version="1.0.0",
        request_id="r",
        outcome="success",
        correlation_id="c",
    )
    with pytest.raises(gen.HostContractDecodeError, match="exactly one"):
        codec.encode_host_result(hostile)


def test_encode_host_result_refuses_a_branch_belonging_to_another_outcome() -> None:
    hostile = gen.HostResult(
        contract_version="1.0.0",
        request_id="req-001",
        outcome="denied",
        correlation_id="corr-001",
        data={"routeId": "workspace.home"},
        denial=gen.CapabilityDenial.from_wire(load("denial/permission-denied.json")),
    )
    with pytest.raises(gen.HostContractDecodeError, match="exactly one"):
        codec.encode_host_result(hostile)


def test_encode_host_request_refuses_an_unsupported_contract_version() -> None:
    request = gen.HostRequest(
        contract_version="2.0.0",
        request_id="req-001",
        namespace="workspace",
        operation="get_active",
        context_id="ctx-001",
        payload={},
    )
    with pytest.raises(gen.HostContractDecodeError):
        codec.encode_host_request(request)


def test_request_admission_without_a_host_owned_catalogue_fails_closed() -> None:
    with pytest.raises(gen.HostContractDecodeError, match="unsupported_contract_value"):
        bound_host().admit_request(load("valid/host-request.json"))


def test_every_valid_fixture_still_encodes_through_its_public_encoder() -> None:
    request = TEST_HOST.admit_request(load("valid/host-request.json"))
    assert codec.encode_host_request(request) == load(
        "valid/host-request.json"
    )
    result = codec.decode_host_result(load("valid/host-result.json"))
    assert codec.encode_host_result(result) == load("valid/host-result.json")
    context = codec.decode_shell_context(load("valid/shell-context.json"))
    assert codec.encode_shell_context(context) == load("valid/shell-context.json")


# --------------------------------------------------------------------------
# The codec speaks exactly the supported Host Contract version
# --------------------------------------------------------------------------


def test_the_codec_refuses_a_v2_document_before_any_operation() -> None:
    with pytest.raises(gen.HostContractDecodeError):
        TEST_HOST.admit_request(document("request", contractVersion="2.0.0"))
    with pytest.raises(gen.HostContractDecodeError):
        codec.decode_host_result(document("result", contractVersion="2.0.0"))


def test_the_codec_refuses_a_schema_valid_but_unsupported_minor() -> None:
    """``1.7.0`` is schema-valid and still outside this build's support window."""
    payload = document("request", contractVersion="1.7.0")
    assert VALIDATORS["request"].is_valid(payload)
    with pytest.raises(compatibility.ContractNegotiationError):
        TEST_HOST.admit_request(payload)


def test_the_codec_admits_exactly_the_approved_version() -> None:
    assert (
        TEST_HOST.admit_request(document("request")).contract_version
        == "1.0.0"
    )
    assert gen.HOST_CONTRACT_VERSION == "1.0.0"
