"""Tests for the Host Contract v1 codec: serialization, envelope totality, trust.

Covers the governed rules the generated structural decoders deliberately do not
own: deterministic canonical JSON, the total result envelope, availability and
health reading, and the trusted-host ShellContext authority boundary.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _host_fixtures import bound_host

from omnivia_core.host_contract.v1 import codec
from omnivia_core.host_contract.v1 import generated as gen

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO_ROOT / "contracts" / "host" / "v1" / "fixtures"


def load(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


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


def make_result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "contractVersion": "1.0.0",
        "requestId": "req-001",
        "outcome": "success",
        "data": {"routeId": "workspace.home"},
        "correlationId": "corr-001",
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not None}


# --------------------------------------------------------------------------
# Deterministic serialization
# --------------------------------------------------------------------------


def test_canonical_json_sorts_keys_and_is_compact() -> None:
    assert codec.to_canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_canonical_json_is_independent_of_insertion_order() -> None:
    first = {"contractVersion": "1.0.0", "requestId": "req-1"}
    second = {"requestId": "req-1", "contractVersion": "1.0.0"}
    assert codec.to_canonical_json(first) == codec.to_canonical_json(second)


def test_canonical_json_rejects_non_standard_float_literals() -> None:
    with pytest.raises(ValueError):
        codec.to_canonical_json({"value": float("nan")})


def test_canonical_json_document_is_indented_and_newline_terminated() -> None:
    document = codec.to_canonical_json_document({"b": 1, "a": 2})
    assert document == '{\n  "a": 2,\n  "b": 1\n}\n'


def test_decode_json_document_rejects_the_nan_literal() -> None:
    with pytest.raises(gen.HostContractDecodeError):
        codec.decode_json_document('{"value": NaN}')


def test_canonical_json_round_trips_every_valid_fixture() -> None:
    for path in sorted((FIXTURES / "valid").glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        assert codec.decode_json_document(codec.to_canonical_json(document)) == document


# --------------------------------------------------------------------------
# Total result envelope
# --------------------------------------------------------------------------


def test_success_result_decodes_on_the_data_branch() -> None:
    result = codec.decode_host_result(load("valid/host-result.json"))
    assert codec.result_branch(result) == "data"


def test_result_envelope_conflict_fixture_is_rejected() -> None:
    with pytest.raises(gen.HostContractDecodeError, match="exactly one"):
        codec.decode_host_result(load("invalid/result-envelope-conflict.json"))


def test_success_without_data_is_rejected() -> None:
    with pytest.raises(gen.HostContractDecodeError, match="exactly one"):
        codec.decode_host_result(make_result(data=None))


def test_success_may_not_carry_a_denial_or_a_failure() -> None:
    denial = load("denial/permission-denied.json")
    with pytest.raises(gen.HostContractDecodeError, match="exactly one"):
        codec.decode_host_result(make_result(denial=denial))
    with pytest.raises(gen.HostContractDecodeError, match="exactly one"):
        codec.decode_host_result(make_result(failure={"code": "boom", "safeReason": "Boom."}))


def test_denied_requires_a_denial_and_forbids_every_other_branch() -> None:
    denial = load("denial/permission-denied.json")
    result = codec.decode_host_result(make_result(outcome="denied", data=None, denial=denial))
    assert codec.result_branch(result) == "denial"

    with pytest.raises(gen.HostContractDecodeError, match="exactly one"):
        codec.decode_host_result(make_result(outcome="denied", data=None))
    with pytest.raises(gen.HostContractDecodeError, match="exactly one"):
        codec.decode_host_result(make_result(outcome="denied", denial=denial))


def test_unavailable_requires_availability_and_forbids_every_other_branch() -> None:
    availability = {
        "state": "unavailable_on_target",
        "capabilityId": "files.export",
        "target": "cloud",
    }
    result = codec.decode_host_result(
        make_result(outcome="unavailable", data=None, availability=availability)
    )
    assert codec.result_branch(result) == "availability"

    with pytest.raises(gen.HostContractDecodeError, match="exactly one"):
        codec.decode_host_result(make_result(outcome="unavailable", data=None))
    with pytest.raises(gen.HostContractDecodeError, match="exactly one"):
        codec.decode_host_result(make_result(outcome="unavailable", availability=availability))


def test_failed_requires_a_failure_and_forbids_every_other_branch() -> None:
    failure = {"code": "internal_error", "safeReason": "Something went wrong."}
    result = codec.decode_host_result(make_result(outcome="failed", data=None, failure=failure))
    assert codec.result_branch(result) == "failure"

    with pytest.raises(gen.HostContractDecodeError, match="exactly one"):
        codec.decode_host_result(make_result(outcome="failed", data=None))
    with pytest.raises(gen.HostContractDecodeError, match="exactly one"):
        codec.decode_host_result(make_result(outcome="failed", failure=failure))


def test_degraded_success_carries_data_and_availability_together() -> None:
    """The one governed pairing: a success that is degraded still answers with data,
    and says which optional capability was unavailable.
    """
    availability = {
        "state": "unavailable_on_target",
        "capabilityId": "notifications.native",
        "target": "cloud",
        "safeReason": "Native notifications are unavailable; in-App notifications remain available.",
    }
    result = codec.decode_host_result(make_result(availability=availability))
    assert codec.result_branch(result) == "data"
    assert codec.is_degraded_success(result) is True
    assert result.availability is not None
    assert result.availability.capability_id == "notifications.native"


def test_plain_success_is_not_a_degraded_success() -> None:
    assert codec.is_degraded_success(codec.decode_host_result(load("valid/host-result.json"))) is False


def test_result_round_trips_through_the_codec() -> None:
    document = load("valid/host-result.json")
    assert codec.encode_host_result(codec.decode_host_result(document)) == document


# --------------------------------------------------------------------------
# Availability and health
# --------------------------------------------------------------------------


def test_only_the_available_state_counts_as_available() -> None:
    for state in gen.AVAILABILITY_STATES:
        availability = gen.CapabilityAvailability.from_wire(
            {"state": state, "capabilityId": "files.export", "target": "desktop"}
        )
        assert codec.is_available(availability) is (state == "available")


def test_temporarily_unhealthy_reports_its_retry_window() -> None:
    availability = gen.CapabilityAvailability.from_wire(
        {
            "state": "temporarily_unhealthy",
            "capabilityId": "knowledge.search",
            "target": "desktop",
            "retryAfterSeconds": 30,
        }
    )
    assert codec.is_available(availability) is False
    assert codec.retry_after_seconds(availability) == 30


def test_retry_window_is_absent_when_the_host_did_not_state_one() -> None:
    availability = gen.CapabilityAvailability.from_wire(
        {"state": "temporarily_unhealthy", "capabilityId": "knowledge.search", "target": "desktop"}
    )
    assert codec.retry_after_seconds(availability) is None


# --------------------------------------------------------------------------
# Display-safe denial admission
# --------------------------------------------------------------------------


def _approved_fixture_reason(denial: gen.CapabilityDenial) -> str | None:
    approved = load("denial/permission-denied.json")
    return denial.safe_reason if denial.safe_reason == approved["safeReason"] else None


def test_a_trusted_display_policy_admits_the_approved_denial_fixture() -> None:
    admitted = codec.admit_display_safe_denial(
        load("denial/permission-denied.json"), _approved_fixture_reason
    )
    assert admitted.to_wire() == load("denial/permission-denied.json")


@pytest.mark.parametrize(
    "unsafe_reason",
    [
        "Credential sk_live_0123456789abcdef must be used.",
        "Token ghp_012345678901234567890123456789012345 must be renewed.",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_unsafe_denial_content_becomes_a_fixed_correlation_bound_refusal(
    unsafe_reason: str,
) -> None:
    payload = load("denial/permission-denied.json")
    payload["safeReason"] = unsafe_reason
    admitted = codec.admit_display_safe_denial(payload, _approved_fixture_reason)
    rendered = codec.to_canonical_json(admitted.to_wire())

    assert admitted.safe_reason == codec.GENERIC_DENIAL_SAFE_REASON
    assert admitted.correlation_id == payload["correlationId"]
    assert unsafe_reason not in rendered
    assert payload["category"] not in rendered
    assert payload["capabilityId"] not in rendered


def test_unknown_category_and_hostile_string_subclasses_are_never_echoed() -> None:
    class HostileString(str):
        def __str__(self) -> str:
            raise AssertionError("attacker-controlled text must not be rendered")

        def __repr__(self) -> str:
            raise AssertionError("attacker-controlled text must not be rendered")

    unknown = load("denial/permission-denied.json")
    unknown["category"] = "secret_unknown_category"
    refused_unknown = codec.admit_display_safe_denial(unknown, _approved_fixture_reason)

    hostile = load("denial/permission-denied.json")
    hostile["safeReason"] = HostileString("credential-private-token")
    hostile["correlationId"] = HostileString("corr-attacker")
    refused_hostile = codec.admit_display_safe_denial(hostile, _approved_fixture_reason)

    assert refused_unknown.safe_reason == codec.GENERIC_DENIAL_SAFE_REASON
    assert refused_unknown.correlation_id == unknown["correlationId"]
    assert refused_hostile.safe_reason == codec.GENERIC_DENIAL_SAFE_REASON
    assert refused_hostile.correlation_id == codec.GENERIC_DENIAL_CORRELATION_ID


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("the redaction service is unreachable"),
        KeyError("ghp_" + "a" * 36),
        ValueError("-----BEGIN PRIVATE KEY-----"),
    ],
)
def test_a_display_policy_that_raises_still_yields_a_safe_denial(error: Exception) -> None:
    """A raised policy is a policy that did not admit the text.

    The whole reason to call this is to always have something safe to display, so
    a host policy that blows up -- an unreachable redaction service, a lookup miss,
    a bug -- must not turn into an exception escaping into a display path. It is
    answered with the same generic refusal every other rejection gets, and the
    exception's own message never reaches the caller: a policy raised over a
    denial can name the very material the denial exists to withhold.
    """
    payload = load("denial/permission-denied.json")

    def raising_policy(denial: gen.CapabilityDenial) -> str | None:
        del denial
        raise error

    admitted = codec.admit_display_safe_denial(payload, raising_policy)
    rendered = codec.to_canonical_json(admitted.to_wire())

    assert admitted.safe_reason == codec.GENERIC_DENIAL_SAFE_REASON
    assert admitted.correlation_id == payload["correlationId"]
    assert str(error) not in rendered
    assert payload["safeReason"] not in rendered


def test_a_display_policy_may_still_be_interrupted() -> None:
    """``BaseException`` is not a policy verdict, so it is not swallowed.

    Catching ``KeyboardInterrupt`` or ``SystemExit`` here would make a host
    process unkillable through its own denial-rendering path.
    """
    def interrupted_policy(denial: gen.CapabilityDenial) -> str | None:
        del denial
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        codec.admit_display_safe_denial(load("denial/permission-denied.json"), interrupted_policy)


# --------------------------------------------------------------------------
# Namespaces and requests
# --------------------------------------------------------------------------


def test_an_operation_absent_from_the_host_catalogue_fails_closed() -> None:
    document = load("valid/host-request.json")
    document["operation"] = "not_in_the_published_catalogue"

    with pytest.raises(gen.HostContractDecodeError, match="unsupported_contract_value"):
        TEST_HOST.admit_request(document)


def test_unknown_namespace_request_fails_closed_through_the_codec() -> None:
    with pytest.raises(gen.HostContractDecodeError, match="unsupported_contract_value"):
        TEST_HOST.admit_request(load("invalid/unknown-namespace.json"))


def test_request_round_trips_through_the_codec() -> None:
    document = load("valid/host-request.json")
    request = TEST_HOST.admit_request(document)
    assert codec.encode_host_request(request) == document


# --------------------------------------------------------------------------
# Trusted-host ShellContext authority
# --------------------------------------------------------------------------


def test_the_prohibited_payload_authority_fields_are_the_governed_set() -> None:
    assert codec.PROHIBITED_PAYLOAD_AUTHORITY_FIELDS == frozenset(
        {
            "principalRef",
            "organisationRef",
            "workspaceRef",
            "permissionSummaryRef",
            "entitlementSummaryRef",
            "environmentId",
        }
    )


def test_forged_shell_context_request_is_a_valid_envelope_with_ignored_payload_authority() -> None:
    """FIXTURE-EXPECTATIONS: envelope accepted, operation denied."""
    request = TEST_HOST.admit_request(load("invalid/forged-shell-context-request.json"))
    assert request.context_id == "ctx-001"
    assert codec.payload_authority_claims(request) == ("principalRef", "workspaceRef")


def test_an_honest_payload_claims_no_authority() -> None:
    assert codec.payload_authority_claims(TEST_HOST.admit_request(load("valid/host-request.json"))) == ()


def test_nested_payload_authority_claims_are_audited_recursively() -> None:
    request = gen.HostRequest(
        contract_version="1.0.0",
        request_id="req-nested-audit",
        namespace="data",
        operation="mutate",
        context_id="ctx-001",
        payload={
            "records": [
                {"workspaceRef": "workspace-other"},
                {"nested": {"principalRef": "principal-admin"}},
            ]
        },
    )

    assert codec.payload_authority_claims(request) == ("principalRef", "workspaceRef")


def test_trusted_context_resolution_ignores_the_payload_entirely() -> None:
    request = TEST_HOST.admit_request(load("invalid/forged-shell-context-request.json"))
    context = gen.ShellContext.from_wire(load("valid/shell-context.json"))
    resolved = codec.resolve_trusted_context(request, context)
    assert resolved is context
    assert resolved.workspace_ref == "workspace-001"
    assert resolved.principal_ref == "principal-local-001"


def test_a_context_that_does_not_match_the_request_is_refused() -> None:
    request = TEST_HOST.admit_request(load("valid/host-request.json"))
    other = dict(load("valid/shell-context.json"))
    other["contextId"] = "ctx-002"
    with pytest.raises(
        codec.HostAuthorityError,
        match="trusted context does not match the request context",
    ) as excinfo:
        codec.resolve_trusted_context(request, gen.ShellContext.from_wire(other))
    assert "ctx-002" not in str(excinfo.value)


def test_an_equality_spoof_cannot_establish_a_trusted_context() -> None:
    class EqualitySpoof:
        def __eq__(self, other: object) -> bool:
            return True

        def __repr__(self) -> str:
            raise AssertionError("attacker repr must not be called")

        def __str__(self) -> str:
            raise AssertionError("attacker str must not be called")

    request = TEST_HOST.admit_request(load("valid/host-request.json"))
    context = replace(
        gen.ShellContext.from_wire(load("valid/shell-context.json")),
        context_id=EqualitySpoof(),  # type: ignore[arg-type]
    )

    with pytest.raises(codec.HostAuthorityError, match="invalid trusted record"):
        codec.resolve_trusted_context(request, context)


def test_context_authority_fields_are_the_governed_rotation_triggers() -> None:
    assert codec.CONTEXT_AUTHORITY_FIELDS == (
        "principalRef",
        "workspaceRef",
        "entitlementSummaryRef",
        "permissionSummaryRef",
        "runtimeId",
        "environmentId",
    )


def test_an_authority_change_must_produce_a_new_context_id() -> None:
    original = gen.ShellContext.from_wire(load("valid/shell-context.json"))
    mutated = dict(load("valid/shell-context.json"))
    mutated["workspaceRef"] = "workspace-002"
    with pytest.raises(codec.HostAuthorityError, match="contextId"):
        codec.validate_context_rotation(original, gen.ShellContext.from_wire(mutated))


def test_a_rotated_context_must_advance_its_version() -> None:
    original = gen.ShellContext.from_wire(load("valid/shell-context.json"))
    rotated = dict(load("valid/shell-context.json"))
    rotated["contextId"] = "ctx-002"
    rotated["workspaceRef"] = "workspace-002"
    with pytest.raises(codec.HostAuthorityError, match="contextVersion"):
        codec.validate_context_rotation(original, gen.ShellContext.from_wire(rotated))


def test_a_correctly_rotated_context_is_accepted() -> None:
    original = gen.ShellContext.from_wire(load("valid/shell-context.json"))
    rotated = dict(load("valid/shell-context.json"))
    rotated["contextId"] = "ctx-002"
    rotated["contextVersion"] = 2
    rotated["workspaceRef"] = "workspace-002"
    new = gen.ShellContext.from_wire(rotated)
    codec.validate_context_rotation(original, new)
    assert codec.changed_authority_fields(original, new) == ("workspaceRef",)


def test_reissuing_the_same_context_id_with_identical_content_is_accepted() -> None:
    original = gen.ShellContext.from_wire(load("valid/shell-context.json"))
    codec.validate_context_rotation(original, gen.ShellContext.from_wire(load("valid/shell-context.json")))


def test_a_handle_tied_to_a_superseded_context_is_invalid() -> None:
    rotated = dict(load("valid/shell-context.json"))
    rotated["contextId"] = "ctx-002"
    rotated["contextVersion"] = 2
    current = gen.ShellContext.from_wire(rotated)
    assert codec.is_handle_valid(current, "ctx-002") is True
    assert codec.is_handle_valid(current, "ctx-001") is False


# --------------------------------------------------------------------------
# Context expiry at the authority boundary
# --------------------------------------------------------------------------

#: The instant every deterministic expiry check below is made at.
CHECK_TIME = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


def expiring_context(expires_at: str | None) -> gen.ShellContext:
    document = dict(load("valid/shell-context.json"))
    if expires_at is not None:
        document["expiresAt"] = expires_at
    return gen.ShellContext.from_wire(document)


def trusted_request() -> gen.HostRequest:
    return TEST_HOST.admit_request(load("valid/host-request.json"))


@pytest.mark.parametrize(
    "expires_at",
    [
        "2026-08-04T11:59:59Z",  # already past
        "2026-08-04T12:00:00Z",  # the boundary instant itself
        "2026-08-04T13:00:00+01:00",  # the boundary instant, written as an offset
        "2026-08-04T06:59:59-05:00",  # past, and only past once the offset is read
    ],
)
def test_an_expired_trusted_context_resolves_to_nothing(expires_at: str) -> None:
    context = expiring_context(expires_at)

    with pytest.raises(codec.HostAuthorityError, match="trusted context has expired"):
        codec.resolve_trusted_context(trusted_request(), context, now=CHECK_TIME)

    assert codec.is_handle_valid(context, "ctx-001", now=CHECK_TIME) is False


@pytest.mark.parametrize(
    "expires_at",
    [
        "2026-08-04T12:00:01Z",  # one second of validity left
        "2026-08-04T13:00:00+00:30",  # still ahead once the offset is read
        "2026-12-31T23:59:60Z",  # the leap second RFC 3339 permits
        None,  # no expiry stated at all
    ],
)
def test_an_unexpired_trusted_context_still_resolves(expires_at: str | None) -> None:
    context = expiring_context(expires_at)

    assert (
        codec.resolve_trusted_context(trusted_request(), context, now=CHECK_TIME)
        is context
    )
    assert codec.is_handle_valid(context, "ctx-001", now=CHECK_TIME) is True


def test_a_context_without_an_expiry_never_expires_against_the_default_clock() -> None:
    context = expiring_context(None)

    assert codec.resolve_trusted_context(trusted_request(), context) is context
    assert codec.is_handle_valid(context, "ctx-001") is True


def test_expiry_is_checked_against_the_real_clock_when_none_is_supplied() -> None:
    past = expiring_context("2020-01-01T00:00:00Z")
    future = expiring_context("2999-01-01T00:00:00Z")

    with pytest.raises(codec.HostAuthorityError, match="trusted context has expired"):
        codec.resolve_trusted_context(trusted_request(), past)
    assert codec.is_handle_valid(past, "ctx-001") is False
    assert codec.resolve_trusted_context(trusted_request(), future) is future
    assert codec.is_handle_valid(future, "ctx-001") is True


def test_an_authority_check_cannot_be_made_against_a_floating_clock() -> None:
    context = expiring_context("2026-08-04T12:00:00Z")
    naive = datetime(2026, 8, 4, 12, 0, 0)  # noqa: DTZ001 -- the refusal under test

    for check in (
        lambda: codec.resolve_trusted_context(trusted_request(), context, now=naive),
        lambda: codec.is_handle_valid(context, "ctx-001", now=naive),
    ):
        with pytest.raises(
            codec.HostAuthorityError, match="time-zone-aware instant"
        ):
            check()


@pytest.mark.parametrize("expires_at", ["tomorrow", "2026-08-04", 1754308800])
def test_a_malformed_resolver_expiry_is_refused_rather_than_read_as_open_ended(
    expires_at: object,
) -> None:
    context = replace(
        expiring_context(None),
        expires_at=expires_at,  # type: ignore[arg-type]
    )

    with pytest.raises(codec.HostAuthorityError, match="invalid trusted record"):
        codec.resolve_trusted_context(trusted_request(), context, now=CHECK_TIME)
    with pytest.raises(codec.HostAuthorityError, match="invalid trusted record"):
        codec.is_handle_valid(context, "ctx-001", now=CHECK_TIME)


def test_expiry_is_an_authority_rule_and_not_a_decoding_rule() -> None:
    document = dict(load("valid/shell-context.json"))
    document["expiresAt"] = "2020-01-01T00:00:00Z"

    decoded = codec.decode_shell_context(document)

    assert decoded.expires_at == "2020-01-01T00:00:00Z"
    assert codec.encode_shell_context(decoded) == document
