"""Tests for the generated Application Contract v1 dataclasses (ADR-038).

Covers structural decode/encode round trips, tolerance of unknown fields,
strict rejection of missing/malformed required fields, and the
``ResponseEnvelope`` discriminator dispatch.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.contracts.v1.generated import (
    OPERATION_CATALOGUE,
    ApiError,
    CapabilityRef,
    ClientIdentity,
    ContractDecodeError,
    ErrorResponseEnvelope,
    GrantedAuthority,
    MutationPrecondition,
    OperationMetadata,
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


def test_the_operation_catalogue_is_a_generated_dataclass_graph() -> None:
    """The catalogue is emitted as typed values, not as untyped dictionaries.

    A dict-shaped catalogue would decode and compare just as well while giving
    up every guarantee the generated types carry: a consumer reading
    `entry.pagination.max_page_size` would get an `AttributeError` at runtime
    instead of a type error at build time.
    """
    from omnivia_core.contracts.v1.generated import __all__ as generated_all

    assert "OPERATION_CATALOGUE" in generated_all
    assert isinstance(OPERATION_CATALOGUE, tuple)
    assert len(OPERATION_CATALOGUE) == 22
    for entry in OPERATION_CATALOGUE:
        assert isinstance(entry, OperationMetadata)
        assert isinstance(entry.allowed_errors, tuple)
        assert OperationMetadata.from_wire(entry.to_wire()) == entry


def test_operation_metadata_decodes_tolerantly() -> None:
    """ADR-038 tolerance applies to catalogue entries like any other v1 object."""
    entry = OPERATION_CATALOGUE[0]
    wire = {**entry.to_wire(), "x_added_by_a_later_version": True}
    assert OperationMetadata.from_wire(wire) == entry


# --------------------------------------------------------------------------
# The generator's catalogue parser.
#
# `x-omnivia-operation-catalogue` is hand-authored JSON sitting inside a schema
# document that does not validate it, and both emitters render it directly into
# published artifacts. The parser walking it against the very definitions it
# claims to materialize is the only thing standing between a malformed
# annotation and wrong generated code, so it is exercised here directly rather
# than only through the artifacts it happens to have produced correctly.
#
# The real definitions are used, and the real canonical annotation is the base
# every case mutates. Hand-writing a second catalogue here would test the parser
# against a fixture nobody maintains instead of against the contract.
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate-application-contracts.py"


def _load_generator() -> Any:
    """Load the generator by path -- its filename is not a Python identifier."""
    spec = importlib.util.spec_from_file_location("generate_application_contracts", GENERATOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


generator = _load_generator()


def _definitions_by_name() -> dict[str, Any]:
    """The generator's own parsed definitions, keyed by name.

    This is the same mapping `build_contract()` hands the catalogue parser in
    production, assembled from the same loader and the same `parse_definition`,
    so the parser under test is judged against the real contract rather than a
    stand-in of it.
    """
    by_name: dict[str, Any] = {}
    for source in generator.SOURCE_SCHEMAS:
        document = generator.load_schema(source)
        for order, (name, node) in enumerate(document["$defs"].items()):
            by_name[name] = generator.parse_definition(name, node, source, order)
    return by_name


BY_NAME = _definitions_by_name()
CATALOGUE_ANNOTATION = generator.OPERATION_CATALOGUE_ANNOTATION


def _operations_document() -> dict[str, Any]:
    return copy.deepcopy(generator.load_schema("operations"))


def _paginated_entry(document: dict[str, Any]) -> dict[str, Any]:
    """The first entry that carries `max_page_size`, so the integer cases have a real
    integer field to corrupt rather than one invented for the test.
    """
    return next(
        entry
        for entry in document[CATALOGUE_ANNOTATION]
        if "max_page_size" in entry.get("pagination", {})
    )


def test_the_parser_accepts_the_canonical_annotation() -> None:
    """The positive control: every negative case below must fail for its own
    reason, not because the parser rejects the real catalogue too.
    """
    assert generator.OPERATION_METADATA_DEFINITION in BY_NAME
    parsed = generator.parse_operation_catalogue(_operations_document(), BY_NAME)
    assert len(parsed) == 22
    assert all(value.kind == "object" for value in parsed)
    assert all(value.name == generator.OPERATION_METADATA_DEFINITION for value in parsed)


#: Each case pairs a mutation of the canonical operations document with the
#: substring the refusal must contain, so a case cannot be credited to an
#: unrelated failure that happens to raise the same exception type.
CATALOGUE_PARSER_CASES: dict[str, tuple[Any, str]] = {
    "non-array-annotation": (
        lambda d: d.__setitem__(CATALOGUE_ANNOTATION, {"not": "an array"}),
        "must be an array",
    ),
    "absent-annotation": (
        lambda d: d.pop(CATALOGUE_ANNOTATION),
        "must be an array",
    ),
    "non-object-entry": (
        lambda d: d[CATALOGUE_ANNOTATION].__setitem__(0, ["memory", "get"]),
        "expected a OperationMetadata object, got list",
    ),
    "scalar-entry": (
        lambda d: d[CATALOGUE_ANNOTATION].__setitem__(0, 7),
        "expected a OperationMetadata object, got int",
    ),
    "null-entry": (
        lambda d: d[CATALOGUE_ANNOTATION].__setitem__(0, None),
        "expected a OperationMetadata object, got NoneType",
    ),
    "non-object-nested-value": (
        lambda d: d[CATALOGUE_ANNOTATION][0].__setitem__("scope", 7),
        "expected a OperationScope object, got int",
    ),
    "missing-required-field": (
        lambda d: d[CATALOGUE_ANNOTATION][0].pop("audit"),
        "OperationMetadata is missing required field 'audit'",
    ),
    "missing-required-nested-field": (
        lambda d: d[CATALOGUE_ANNOTATION][0]["scope"].pop("scope_kind"),
        "OperationScope is missing required field 'scope_kind'",
    ),
    "undeclared-field": (
        lambda d: d[CATALOGUE_ANNOTATION][0].__setitem__("surprise", "hello"),
        "OperationMetadata declares no field(s) ['surprise']",
    ),
    "undeclared-nested-field": (
        lambda d: d[CATALOGUE_ANNOTATION][0]["audit"].__setitem__("surprise", "hello"),
        "OperationAuditMetadata declares no field(s) ['surprise']",
    ),
    "wrong-scalar-type-string": (
        lambda d: d[CATALOGUE_ANNOTATION][0].__setitem__("name", 7),
        "expected a string, got int",
    ),
    "wrong-scalar-type-boolean": (
        lambda d: d[CATALOGUE_ANNOTATION][0]["audit"].__setitem__("audited", "true"),
        "expected a boolean, got str",
    ),
    # A JSON boolean is an `int` in Python, so an integer field that accepted one
    # would emit `True` where the contract declares a page size.
    "wrong-scalar-type-integer": (
        lambda d: _paginated_entry(d)["pagination"].__setitem__("max_page_size", True),
        "expected an integer, got bool",
    ),
    "integer-field-given-a-string": (
        lambda d: _paginated_entry(d)["pagination"].__setitem__("max_page_size", "1000"),
        "expected an integer, got str",
    ),
    "wrong-array-type": (
        lambda d: d[CATALOGUE_ANNOTATION][0].__setitem__("allowed_errors", "invalid_request"),
        "expected an array, got str",
    ),
    "wrong-nested-array-type": (
        lambda d: d[CATALOGUE_ANNOTATION][0]["scope"].__setitem__("required_scopes", {}),
        "expected an array, got dict",
    ),
    "wrong-array-element-type": (
        lambda d: d[CATALOGUE_ANNOTATION][0]["allowed_errors"].__setitem__(0, 7),
        "expected a string, got int",
    ),
}


@pytest.mark.parametrize(
    "label", sorted(CATALOGUE_PARSER_CASES), ids=sorted(CATALOGUE_PARSER_CASES)
)
def test_the_parser_refuses_a_malformed_catalogue_annotation(label: str) -> None:
    """A malformed annotation must fail at generation time, not become artifacts.

    `UnsupportedSchemaError` specifically: the generator's `main()` catches that
    one and reports it as a generation failure, so anything else would surface as
    an unhandled traceback with no indication of which entry is at fault.
    """
    mutate, expected = CATALOGUE_PARSER_CASES[label]
    document = _operations_document()
    mutate(document)
    with pytest.raises(generator.UnsupportedSchemaError) as caught:
        generator.parse_operation_catalogue(document, BY_NAME)
    assert expected in str(caught.value), f"{label}: got {caught.value}"


def test_a_refusal_names_the_entry_and_the_field_it_refused() -> None:
    """A location, not just a diagnosis: with 22 near-identical entries, "expected a
    string" that does not say *where* leaves a reader diffing the annotation by eye.
    """
    document = _operations_document()
    document[CATALOGUE_ANNOTATION][3]["scope"]["required_scopes"][0] = 7
    with pytest.raises(generator.UnsupportedSchemaError) as caught:
        generator.parse_operation_catalogue(document, BY_NAME)
    assert f"{CATALOGUE_ANNOTATION}[3].scope.required_scopes[0]" in str(caught.value)


def test_the_parser_requires_the_definition_the_catalogue_materializes() -> None:
    """Without `OperationMetadata` there is nothing to validate entries against, so
    the parser refuses rather than emitting an unchecked catalogue.
    """
    without_metadata = {
        name: definition
        for name, definition in BY_NAME.items()
        if name != generator.OPERATION_METADATA_DEFINITION
    }
    with pytest.raises(generator.UnsupportedSchemaError, match="OperationMetadata"):
        generator.parse_operation_catalogue(_operations_document(), without_metadata)
