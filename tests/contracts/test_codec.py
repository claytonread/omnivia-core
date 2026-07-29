"""Tests for the Application Contract v1 wire codec (ADR-038).

Covers deterministic canonical JSON, tolerant response decoding/dispatch, and
the frozen retry-class semantics, including the fail-safe rule for unknown
retry classes.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnivia_core.contracts.v1 import codec
from omnivia_core.contracts.v1.generated import ApiError


def test_to_canonical_json_sorts_keys_and_is_compact() -> None:
    payload = {"b": 1, "a": 2, "nested": {"z": 1, "y": 2}}
    assert codec.to_canonical_json(payload) == '{"a":2,"b":1,"nested":{"y":2,"z":1}}'


def test_to_canonical_json_is_order_independent() -> None:
    first = {"a": 1, "b": 2}
    second = {"b": 2, "a": 1}
    assert codec.to_canonical_json(first) == codec.to_canonical_json(second)


def test_to_canonical_json_rejects_non_finite_floats() -> None:
    with pytest.raises(ValueError, match="not JSON compliant"):
        codec.to_canonical_json({"value": float("nan")})


def test_to_canonical_json_document_is_indented_and_newline_terminated() -> None:
    text = codec.to_canonical_json_document({"a": 1})
    assert text == '{\n  "a": 1\n}\n'


def test_decode_json_document_rejects_non_standard_constants() -> None:
    with pytest.raises(codec.ContractDecodeError):
        codec.decode_json_document("NaN")


def test_decode_json_document_parses_standard_json() -> None:
    assert codec.decode_json_document('{"a": 1}') == {"a": 1}


def test_decode_response_dispatches_success() -> None:
    payload = {
        "metadata": {
            "request_id": "req-1",
            "correlation_id": "corr-1",
            "version": _minimal_version_wire(),
            "authority": {"principal_id": "user-1", "roles": [], "capabilities": []},
        },
        "result": {"ok": True},
    }
    envelope = codec.decode_response(payload)
    assert codec.response_result(envelope) == {"ok": True}
    assert codec.response_error(envelope) is None


def test_decode_success_response_rejects_an_error_payload() -> None:
    payload = {
        "metadata": {
            "request_id": "req-1",
            "correlation_id": "corr-1",
            "version": _minimal_version_wire(),
            "authority": {"principal_id": "user-1", "roles": [], "capabilities": []},
        },
        "error": {"code": "not_found", "message": "x", "retry_class": "non_retryable"},
    }
    with pytest.raises(codec.ContractDecodeError, match="expected the success branch"):
        codec.decode_success_response(payload)


def test_decode_error_response_rejects_a_success_payload() -> None:
    payload = {
        "metadata": {
            "request_id": "req-1",
            "correlation_id": "corr-1",
            "version": _minimal_version_wire(),
            "authority": {"principal_id": "user-1", "roles": [], "capabilities": []},
        },
        "result": {},
    }
    with pytest.raises(codec.ContractDecodeError, match="expected the error branch"):
        codec.decode_error_response(payload)


def test_decode_response_rejects_effective_capability_wider_than_supported_and_granted() -> None:
    version = _minimal_version_wire()
    version["capabilities"] = {
        "supported": [{"id": "memory.read", "version": "1.0"}],
        "granted": [{"id": "memory.read", "version": "1.0"}],
        "effective": [
            {"id": "memory.read", "version": "1.0"},
            {"id": "memory.write", "version": "1.0"},
        ],
    }
    payload = _success_payload(version, result={"ok": True})
    with pytest.raises(codec.ContractDecodeError, match="effective"):
        codec.decode_response(payload)


def test_decode_response_rejects_duplicate_capability_ids() -> None:
    version = _minimal_version_wire()
    version["capabilities"] = {
        "supported": [
            {"id": "memory.search", "version": "1.0"},
            {"id": "memory.search", "version": "1.1"},
        ],
        "granted": [],
        "effective": [],
    }
    payload = _success_payload(version, result={"ok": True})
    with pytest.raises(codec.ContractDecodeError, match="duplicate"):
        codec.decode_response(payload)


def test_decode_response_rejects_a_reversed_supported_version_window() -> None:
    version = _minimal_version_wire()
    version["compatibility"]["supported_api_versions"] = {"minimum": "1.5", "maximum": "1.0"}
    payload = _success_payload(version, result={"ok": True})
    with pytest.raises(codec.ContractDecodeError, match="reversed"):
        codec.decode_response(payload)


def test_decode_response_rejects_a_supported_window_bound_that_is_not_a_version() -> None:
    version = _minimal_version_wire()
    version["compatibility"]["supported_api_versions"] = {"minimum": "1.0", "maximum": "latest"}
    payload = _success_payload(version, result={"ok": True})
    with pytest.raises(codec.ContractDecodeError, match="version window maximum"):
        codec.decode_response(payload)


def test_decode_response_rejects_an_api_version_contradicting_the_selected_one() -> None:
    """An envelope may state `api_version` 9.9 while negotiation selected 1.2 only because
    both are open strings structurally. The production decoder must not let a caller that
    reads one field and a caller that reads the other reach different answers.
    """
    version = _minimal_version_wire()
    version["api_version"] = "9.9"
    payload = _success_payload(version, result={"ok": True})
    with pytest.raises(codec.ContractDecodeError, match="api_version '9.9' disagrees"):
        codec.decode_response(payload)


def test_decode_response_rejects_a_selected_api_version_the_envelope_does_not_echo() -> None:
    version = _minimal_version_wire()
    version["compatibility"]["selected_api_version"] = "1.3"
    payload = _success_payload(version, result={"ok": True})
    with pytest.raises(codec.ContractDecodeError, match="disagrees"):
        codec.decode_response(payload)


def test_decode_response_rejects_a_workspace_version_contradicting_the_selected_one() -> None:
    version = _minimal_version_wire()
    version["workspace_format_version"] = "2.0"
    payload = _success_payload(version, result={"ok": True})
    with pytest.raises(codec.ContractDecodeError, match="workspace_format_version '2.0' disagrees"):
        codec.decode_response(payload)


@pytest.mark.parametrize("field", ["api_version", "workspace_format_version"])
@pytest.mark.parametrize("malformed", ["9", "", "1.2.3", "latest"])
def test_decode_response_rejects_a_malformed_version_in_force(field: str, malformed: str) -> None:
    selected = {
        "api_version": "selected_api_version",
        "workspace_format_version": "selected_workspace_version",
    }[field]
    version = _minimal_version_wire()
    version[field] = malformed
    version["compatibility"][selected] = malformed
    payload = _success_payload(version, result={"ok": True})
    with pytest.raises(codec.ContractDecodeError, match="is not a `major.minor` contract version"):
        codec.decode_response(payload)


@pytest.mark.parametrize(
    "selected_field", ["selected_api_version", "selected_workspace_version"]
)
@pytest.mark.parametrize("malformed", ["9", "", "1.2.3", "latest"])
def test_decode_response_rejects_a_malformed_selected_version(
    selected_field: str, malformed: str
) -> None:
    version = _minimal_version_wire()
    version["compatibility"][selected_field] = malformed
    payload = _success_payload(version, result={"ok": True})
    with pytest.raises(codec.ContractDecodeError, match="is not a `major.minor` contract version"):
        codec.decode_response(payload)


def test_decode_response_rejects_a_selected_api_version_outside_the_supported_window() -> None:
    version = _minimal_version_wire()
    version["api_version"] = "1.9"
    version["compatibility"]["selected_api_version"] = "1.9"
    payload = _success_payload(version, result={"ok": True})
    with pytest.raises(codec.ContractDecodeError, match="falls outside the declared"):
        codec.decode_response(payload)


def test_decode_response_rejects_a_selected_workspace_version_outside_the_supported_window() -> None:
    version = _minimal_version_wire()
    version["workspace_format_version"] = "1.1"
    version["compatibility"]["selected_workspace_version"] = "1.1"
    payload = _success_payload(version, result={"ok": True})
    with pytest.raises(codec.ContractDecodeError, match="supported_workspace_versions"):
        codec.decode_response(payload)


def test_decode_response_preserves_an_unknown_compatibility_status() -> None:
    """The status vocabulary stays open: a newer peer's unseen status is not a semantic
    failure, and must survive the decode unchanged.
    """
    version = _minimal_version_wire()
    version["compatibility"]["status"] = "a_status_this_build_has_never_seen"
    envelope = codec.decode_response(_success_payload(version, result={"ok": True}))
    assert (
        envelope.metadata.version.compatibility.status == "a_status_this_build_has_never_seen"
    )


# --------------------------------------------------------------------------
# Trusted authority must never exceed effective negotiated authority. The
# capability set can be perfectly self-consistent while `metadata.authority`
# claims more than the negotiation made effective, and `authority` is the field
# a caller actually acts on -- so the production decoder has to validate both.
# --------------------------------------------------------------------------

READ_WIRE = {"id": "memory.read", "version": "1.0"}
WRITE_WIRE = {"id": "memory.write", "version": "1.0"}
NEWER_READ_WIRE = {"id": "memory.read", "version": "1.1"}


def _version_wire_with_effective(*capabilities: dict[str, Any]) -> dict[str, Any]:
    """A version envelope whose capability set is self-consistent and makes exactly
    ``capabilities`` effective, so any authority failure is authority's alone.
    """
    version = _minimal_version_wire()
    version["capabilities"] = {
        "supported": list(capabilities),
        "granted": list(capabilities),
        "effective": list(capabilities),
    }
    return version


def test_decode_response_rejects_authority_wider_than_the_effective_capabilities() -> None:
    payload = _success_payload(
        _version_wire_with_effective(READ_WIRE),
        result={"ok": True},
        authority=_authority_wire(READ_WIRE, WRITE_WIRE),
    )
    with pytest.raises(codec.ContractDecodeError, match="authority"):
        codec.decode_response(payload)


def test_decode_response_rejects_authority_when_nothing_was_negotiated() -> None:
    payload = _success_payload(
        _minimal_version_wire(), result={"ok": True}, authority=_authority_wire(READ_WIRE)
    )
    with pytest.raises(codec.ContractDecodeError, match="not effective"):
        codec.decode_response(payload)


def test_decode_response_rejects_authority_at_a_version_that_is_not_effective() -> None:
    payload = _success_payload(
        _version_wire_with_effective(READ_WIRE),
        result={"ok": True},
        authority=_authority_wire(NEWER_READ_WIRE),
    )
    with pytest.raises(codec.ContractDecodeError, match="not effective"):
        codec.decode_response(payload)


def test_decode_response_rejects_duplicate_authority_capability_ids() -> None:
    payload = _success_payload(
        _version_wire_with_effective(READ_WIRE),
        result={"ok": True},
        authority=_authority_wire(READ_WIRE, NEWER_READ_WIRE),
    )
    with pytest.raises(codec.ContractDecodeError, match="duplicate"):
        codec.decode_response(payload)


def test_decode_response_accepts_authority_that_is_a_subset_of_effective() -> None:
    payload = _success_payload(
        _version_wire_with_effective(READ_WIRE, WRITE_WIRE),
        result={"ok": True},
        authority=_authority_wire(READ_WIRE),
    )
    envelope = codec.decode_response(payload)
    assert [ref.id for ref in envelope.metadata.authority.capabilities] == ["memory.read"]


def test_decode_response_accepts_authority_equal_to_effective() -> None:
    payload = _success_payload(
        _version_wire_with_effective(READ_WIRE, WRITE_WIRE),
        result={"ok": True},
        authority=_authority_wire(READ_WIRE, WRITE_WIRE),
    )
    envelope = codec.decode_response(payload)
    assert len(envelope.metadata.authority.capabilities) == 2


def test_decode_response_accepts_an_empty_authority_set() -> None:
    payload = _success_payload(
        _version_wire_with_effective(READ_WIRE, WRITE_WIRE),
        result={"ok": True},
        authority=_authority_wire(),
    )
    envelope = codec.decode_response(payload)
    assert envelope.metadata.authority.capabilities == ()


def test_decode_response_accepts_an_empty_authority_set_on_an_error_response() -> None:
    """An error response may establish no authority at all -- including one rejecting the
    request as incompatible, where nothing was ever granted.
    """
    version = _version_wire_with_effective(READ_WIRE)
    version["compatibility"]["status"] = "incompatible"
    error = {
        "code": "incompatible_version",
        "message": "requested api_version 2.0 is not supported",
        "retry_class": "non_retryable",
        "details": {"requested_api_version": "2.0"},
    }
    envelope = codec.decode_response(_error_payload(version, error, _authority_wire()))
    assert envelope.metadata.authority.capabilities == ()


def test_decode_response_rejects_widened_authority_on_an_error_response() -> None:
    """The authority rule holds on the error branch too: a rejected request must not come
    back carrying authority the exchange never made effective.
    """
    error = {"code": "not_found", "message": "x", "retry_class": "non_retryable"}
    payload = _error_payload(
        _version_wire_with_effective(READ_WIRE), error, _authority_wire(READ_WIRE, WRITE_WIRE)
    )
    with pytest.raises(codec.ContractDecodeError, match="authority"):
        codec.decode_response(payload)


# --------------------------------------------------------------------------
# ContractVersion is only a string on the wire, so the production decoder is
# what decides which spellings are versions at all. A noncanonical spelling a
# looser parser would still read a number out of -- a leading zero, a non-ASCII
# digit, surrounding whitespace, an explicit sign -- must be rejected wherever a
# version appears. Each is a value `int` would happily accept, which is exactly
# why the decoder cannot fall back on it: two peers that disagree on whether
# `"01.2"` and `"1.2"` name the same version cannot negotiate at all.
# --------------------------------------------------------------------------

NONCANONICAL_VERSIONS = [
    "01.2",  # leading zero, major
    "1.02",  # leading zero, minor
    "0001.0002",
    "١.٢",  # Arabic-Indic digits
    "１.２",  # fullwidth digits
    " 1.2",  # leading whitespace
    "1.2 ",  # trailing whitespace
    "\n1.2",
    "+1.2",  # explicit sign
    "-1.2",
    "1_0.2",  # underscore digit grouping `int` accepts
]


@pytest.mark.parametrize("field", ["api_version", "workspace_format_version"])
@pytest.mark.parametrize("noncanonical", NONCANONICAL_VERSIONS)
def test_decode_response_rejects_a_noncanonical_version_in_force(
    field: str, noncanonical: str
) -> None:
    selected = {
        "api_version": "selected_api_version",
        "workspace_format_version": "selected_workspace_version",
    }[field]
    version = _minimal_version_wire()
    version[field] = noncanonical
    version["compatibility"][selected] = noncanonical
    payload = _success_payload(version, result={"ok": True})
    with pytest.raises(codec.ContractDecodeError, match="is not a `major.minor` contract version"):
        codec.decode_response(payload)


@pytest.mark.parametrize(
    "selected_field", ["selected_api_version", "selected_workspace_version"]
)
@pytest.mark.parametrize("noncanonical", NONCANONICAL_VERSIONS)
def test_decode_response_rejects_a_noncanonical_selected_version(
    selected_field: str, noncanonical: str
) -> None:
    version = _minimal_version_wire()
    version["compatibility"][selected_field] = noncanonical
    payload = _success_payload(version, result={"ok": True})
    with pytest.raises(codec.ContractDecodeError, match="is not a `major.minor` contract version"):
        codec.decode_response(payload)


@pytest.mark.parametrize("bound", ["minimum", "maximum"])
@pytest.mark.parametrize("noncanonical", NONCANONICAL_VERSIONS)
def test_decode_response_rejects_a_noncanonical_supported_window_bound(
    bound: str, noncanonical: str
) -> None:
    version = _minimal_version_wire()
    version["compatibility"]["supported_api_versions"][bound] = noncanonical
    payload = _success_payload(version, result={"ok": True})
    with pytest.raises(codec.ContractDecodeError, match=f"version window {bound}"):
        codec.decode_response(payload)


def test_decode_response_rejects_a_noncanonical_workspace_window_bound() -> None:
    version = _minimal_version_wire()
    version["compatibility"]["supported_workspace_versions"]["maximum"] = "1.00"
    payload = _success_payload(version, result={"ok": True})
    with pytest.raises(codec.ContractDecodeError, match="version window maximum"):
        codec.decode_response(payload)


@pytest.mark.parametrize("bound", ["minimum", "maximum"])
@pytest.mark.parametrize("noncanonical", NONCANONICAL_VERSIONS)
def test_decode_response_rejects_a_noncanonical_supported_workspace_window_bound(
    bound: str, noncanonical: str
) -> None:
    """The workspace window is held to the same grammar as the api window.

    Both windows are decoded by the same `VersionWindow` structural pair, which only
    checks that each bound is a string -- so nothing below this layer stops a
    leading-zero or non-ASCII-digit spelling reaching a caller on the workspace side
    alone.
    """
    version = _minimal_version_wire()
    version["compatibility"]["supported_workspace_versions"][bound] = noncanonical
    payload = _success_payload(version, result={"ok": True})
    with pytest.raises(codec.ContractDecodeError, match=f"version window {bound}"):
        codec.decode_response(payload)


def test_decode_response_rejects_a_known_error_codes_mismatched_retry_class() -> None:
    version = _minimal_version_wire()
    error = {
        "code": "mutation_precondition_failed",
        "message": "stale record_version",
        "retry_class": "retryable",
    }
    payload = _error_payload(version, error)
    with pytest.raises(codec.RetryClassMismatchError, match="mutation_precondition_failed"):
        codec.decode_response(payload)


def test_decode_response_accepts_a_valid_success_envelope() -> None:
    payload = _success_payload(_minimal_version_wire(), result={"ok": True})
    envelope = codec.decode_response(payload)
    assert codec.response_result(envelope) == {"ok": True}


def test_decode_response_accepts_a_forward_compatible_unknown_error_code() -> None:
    version = _minimal_version_wire()
    error = {
        "code": "quota_soft_limit_reached",
        "message": "soft limit reached",
        "retry_class": "retryable_experimental",
    }
    payload = _error_payload(version, error)
    envelope = codec.decode_response(payload)
    decoded_error = codec.response_error(envelope)
    assert decoded_error is not None
    assert decoded_error.code == "quota_soft_limit_reached"


@pytest.mark.parametrize(
    ("code", "expected_class"),
    [
        ("mutation_precondition_failed", "retryable_after_precondition_refresh"),
        ("rate_limited", "retryable_after_delay"),
        ("deadline_exceeded", "retryable"),
        ("not_found", "non_retryable"),
    ],
)
def test_retry_class_for_known_codes(code: str, expected_class: str) -> None:
    assert codec.retry_class_for(code) == expected_class


def test_retry_class_for_unknown_code_fails_safe_to_non_retryable() -> None:
    assert codec.retry_class_for("a_code_this_build_has_never_seen") == "non_retryable"


@pytest.mark.parametrize(
    ("retry_class", "expected"),
    [
        ("retryable", True),
        ("retryable_after_delay", True),
        ("retryable_after_precondition_refresh", False),
        ("non_retryable", False),
        ("an_unrecognized_class", False),
    ],
)
def test_is_retryable(retry_class: str, expected: bool) -> None:
    assert codec.is_retryable(retry_class) is expected


def test_is_error_retryable_uses_the_stated_retry_class() -> None:
    error = ApiError(code="rate_limited", message="slow down", retry_class="retryable_after_delay")
    assert codec.is_error_retryable(error) is True


def test_is_error_retryable_ignores_a_known_codes_mismatched_claim_of_retryable() -> None:
    """A known code's frozen classification can never be overridden by the wire value.

    `mutation_precondition_failed` is frozen to
    `retryable_after_precondition_refresh`, which is not blind-retryable. A
    peer that mislabels it `retryable` must not make it retryable here.
    """
    error = ApiError(
        code="mutation_precondition_failed",
        message="stale record_version",
        retry_class="retryable",
    )
    assert codec.is_error_retryable(error) is False


def test_is_error_retryable_ignores_a_known_codes_mismatched_claim_of_non_retryable() -> None:
    """The reverse mismatch also fails safe: a known retryable code mislabeled non-retryable
    must not become retryable just because the frozen class disagrees with the claim.
    """
    error = ApiError(code="deadline_exceeded", message="timed out", retry_class="non_retryable")
    assert codec.is_error_retryable(error) is False


def test_is_error_retryable_trusts_stated_class_for_an_unknown_code() -> None:
    error = ApiError(
        code="a_code_this_build_has_never_seen",
        message="?",
        retry_class="retryable",
    )
    assert codec.is_error_retryable(error) is True


def test_validate_error_retry_class_accepts_a_matching_known_code() -> None:
    error = ApiError(code="not_found", message="x", retry_class="non_retryable")
    codec.validate_error_retry_class(error)  # must not raise


def test_validate_error_retry_class_rejects_mutation_precondition_mismatch() -> None:
    error = ApiError(
        code="mutation_precondition_failed",
        message="stale record_version",
        retry_class="retryable",
    )
    with pytest.raises(codec.RetryClassMismatchError, match="mutation_precondition_failed"):
        codec.validate_error_retry_class(error)


def test_validate_error_retry_class_accepts_any_class_for_an_unknown_code() -> None:
    error = ApiError(code="an_unrecognized_code", message="?", retry_class="anything_goes")
    codec.validate_error_retry_class(error)  # must not raise


def test_retry_after_ms_passthrough() -> None:
    error = ApiError(
        code="rate_limited",
        message="slow down",
        retry_class="retryable_after_delay",
        retry_after_ms=250,
    )
    assert codec.retry_after_ms(error) == 250
    bare = ApiError(code="not_found", message="x", retry_class="non_retryable")
    assert codec.retry_after_ms(bare) is None


def _minimal_metadata_wire(
    version: dict[str, Any], authority: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "request_id": "req-1",
        "correlation_id": "corr-1",
        "version": version,
        "authority": authority if authority is not None else _authority_wire(),
    }


def _success_payload(
    version: dict[str, Any],
    *,
    result: dict[str, Any],
    authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {"metadata": _minimal_metadata_wire(version, authority), "result": result}


def _error_payload(
    version: dict[str, Any],
    error: dict[str, Any],
    authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {"metadata": _minimal_metadata_wire(version, authority), "error": error}


def _authority_wire(*capabilities: dict[str, Any]) -> dict[str, Any]:
    return {
        "principal_id": "user-1",
        "roles": ["member"],
        "capabilities": list(capabilities),
    }


def _minimal_version_wire() -> dict[str, Any]:
    return {
        "api_version": "1.2",
        "server_version": "1.2.5",
        "workspace_format_version": "1.0",
        "compatibility": {
            "selected_api_version": "1.2",
            "selected_workspace_version": "1.0",
            "supported_api_versions": {"minimum": "1.0", "maximum": "1.3"},
            "supported_workspace_versions": {"minimum": "1.0", "maximum": "1.0"},
            "status": "compatible",
            "upgrade_state": {"value": "none"},
            "deprecations": [],
        },
        "capabilities": {"supported": [], "granted": [], "effective": []},
    }
