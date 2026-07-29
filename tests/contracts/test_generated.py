"""Tests for the generated Application Contract v1 dataclasses (ADR-038).

Covers structural decode/encode round trips, tolerance of unknown fields,
strict rejection of missing/malformed required fields, and the
``ResponseEnvelope`` discriminator dispatch.
"""

from __future__ import annotations

import pytest

from omnivia_core.contracts.v1.generated import (
    ApiError,
    CapabilityRef,
    ClientIdentity,
    ContractDecodeError,
    ErrorResponseEnvelope,
    GrantedAuthority,
    MutationPrecondition,
    PrincipalClaim,
    RequestEnvelope,
    RequestMetadata,
    ResponseMetadata,
    SuccessResponseEnvelope,
    VersionCapabilityEnvelope,
    Warning,
    response_envelope_from_wire,
    response_envelope_to_wire,
)


def _version_envelope() -> VersionCapabilityEnvelope:
    from omnivia_core.contracts.v1.generated import (
        CapabilitySet,
        CompatibilityMetadata,
        UpgradeState,
        VersionWindow,
    )

    return VersionCapabilityEnvelope(
        api_version="1.2",
        server_version="1.2.5",
        workspace_format_version="1.0",
        compatibility=CompatibilityMetadata(
            selected_api_version="1.2",
            selected_workspace_version="1.0",
            supported_api_versions=VersionWindow(minimum="1.0", maximum="1.3"),
            supported_workspace_versions=VersionWindow(minimum="1.0", maximum="1.0"),
            status="compatible",
            upgrade_state=UpgradeState(value="none"),
            deprecations=(),
        ),
        capabilities=CapabilitySet(supported=(), granted=(), effective=()),
    )


def test_client_identity_round_trip() -> None:
    original = ClientIdentity(id="omnivia.cli", version="1.4.2")
    wire = original.to_wire()
    assert wire == {"id": "omnivia.cli", "version": "1.4.2"}
    assert ClientIdentity.from_wire(wire) == original


def test_from_wire_ignores_unknown_fields() -> None:
    payload = {"id": "omnivia.cli", "version": "1.4.2", "unexpected_future_field": "value"}
    assert ClientIdentity.from_wire(payload) == ClientIdentity(id="omnivia.cli", version="1.4.2")


def test_from_wire_missing_required_field_raises() -> None:
    with pytest.raises(ContractDecodeError, match="missing required field 'version'"):
        ClientIdentity.from_wire({"id": "omnivia.cli"})


def test_from_wire_wrong_type_raises() -> None:
    with pytest.raises(ContractDecodeError, match="expected a string"):
        ClientIdentity.from_wire({"id": "omnivia.cli", "version": 142})


def test_from_wire_requires_a_mapping() -> None:
    with pytest.raises(ContractDecodeError, match="expected an object"):
        ClientIdentity.from_wire(["not", "a", "mapping"])


def test_optional_fields_round_trip_as_absent_not_null() -> None:
    metadata = RequestMetadata(
        request_id="req-1",
        correlation_id="corr-1",
        trace_id="trace-1",
        api_version="1.2",
        client=ClientIdentity(id="omnivia.cli", version="1.4.2"),
        workspace_id="workspace-1",
        scopes=(),
        purpose="user_initiated",
        required_capabilities=(),
    )
    wire = metadata.to_wire()
    for optional_field in ("deadline_ms", "idempotency_key", "mutation_precondition", "principal_claim"):
        assert optional_field not in wire
    assert RequestMetadata.from_wire(wire) == metadata


def test_request_envelope_round_trip() -> None:
    envelope = RequestEnvelope(
        operation="memory.get",
        metadata=RequestMetadata(
            request_id="req-1",
            correlation_id="corr-1",
            trace_id="trace-1",
            api_version="1.2",
            client=ClientIdentity(id="omnivia.cli", version="1.4.2"),
            workspace_id="workspace-1",
            scopes=("memory:read",),
            purpose="user_initiated",
            required_capabilities=(),
        ),
        input={"note_id": "note-1"},
    )
    wire = envelope.to_wire()
    assert RequestEnvelope.from_wire(wire) == envelope


def test_response_envelope_dispatches_success_branch() -> None:
    envelope = SuccessResponseEnvelope(
        metadata=ResponseMetadata(
            request_id="req-1",
            correlation_id="corr-1",
            version=_version_envelope(),
            authority=GrantedAuthority(principal_id="user-1", roles=(), capabilities=()),
        ),
        result={"ok": True},
    )
    wire = response_envelope_to_wire(envelope)
    assert "result" in wire and "error" not in wire
    decoded = response_envelope_from_wire(wire)
    assert isinstance(decoded, SuccessResponseEnvelope)
    assert decoded == envelope


def test_response_envelope_dispatches_error_branch() -> None:
    envelope = ErrorResponseEnvelope(
        metadata=ResponseMetadata(
            request_id="req-1",
            correlation_id="corr-1",
            version=_version_envelope(),
            authority=GrantedAuthority(principal_id="user-1", roles=(), capabilities=()),
        ),
        error=ApiError(code="not_found", message="missing", retry_class="non_retryable"),
    )
    wire = response_envelope_to_wire(envelope)
    assert "error" in wire and "result" not in wire
    decoded = response_envelope_from_wire(wire)
    assert isinstance(decoded, ErrorResponseEnvelope)
    assert decoded == envelope


def test_response_envelope_rejects_both_branches() -> None:
    wire = {
        "metadata": {},
        "result": {},
        "error": {"code": "not_found", "message": "x", "retry_class": "non_retryable"},
    }
    with pytest.raises(ContractDecodeError, match="expected exactly one of"):
        response_envelope_from_wire(wire)


def test_response_envelope_rejects_neither_branch() -> None:
    with pytest.raises(ContractDecodeError, match="expected exactly one of"):
        response_envelope_from_wire({"metadata": {}})


def test_capability_ref_round_trip() -> None:
    ref = CapabilityRef(id="memory.read", version="1.0")
    assert CapabilityRef.from_wire(ref.to_wire()) == ref


def test_explicit_null_is_rejected_for_an_optional_scalar_field() -> None:
    """An absent key still decodes as `None`, but a present JSON `null` for a non-nullable
    optional field must be rejected rather than silently treated the same as absent.
    """
    payload = {"claimed_principal_id": None}
    with pytest.raises(ContractDecodeError, match="claimed_principal_id: null is not a valid value"):
        PrincipalClaim.from_wire(payload)


def test_explicit_null_is_rejected_for_an_optional_array_field() -> None:
    payload = {"claimed_roles": None}
    with pytest.raises(ContractDecodeError, match="claimed_roles: null is not a valid value"):
        PrincipalClaim.from_wire(payload)


def test_explicit_null_is_rejected_for_an_optional_object_field() -> None:
    metadata = RequestMetadata(
        request_id="req-1",
        correlation_id="corr-1",
        trace_id="trace-1",
        api_version="1.2",
        client=ClientIdentity(id="omnivia.cli", version="1.4.2"),
        workspace_id="workspace-1",
        scopes=(),
        purpose="user_initiated",
        required_capabilities=(),
        mutation_precondition=MutationPrecondition(record_version="etag-1"),
    )
    wire = metadata.to_wire()
    wire["mutation_precondition"] = None
    with pytest.raises(
        ContractDecodeError, match="mutation_precondition: null is not a valid value"
    ):
        RequestMetadata.from_wire(wire)


def test_explicit_null_is_rejected_for_an_optional_array_of_objects_field() -> None:
    metadata = ResponseMetadata(
        request_id="req-1",
        correlation_id="corr-1",
        version=_version_envelope(),
        authority=GrantedAuthority(principal_id="user-1", roles=(), capabilities=()),
        warnings=(Warning(code="soft_deprecation", message="switch soon"),),
    )
    wire = metadata.to_wire()
    wire["warnings"] = None
    with pytest.raises(ContractDecodeError, match="warnings: null is not a valid value"):
        ResponseMetadata.from_wire(wire)


def test_absent_optional_fields_still_decode_as_none() -> None:
    """A regression guard alongside the explicit-null tests above: omitting the key entirely
    must still decode as `None`, not be swept up by the new null rejection.
    """
    assert PrincipalClaim.from_wire({}) == PrincipalClaim()
