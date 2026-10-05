"""Schema-aware Workflow Check at the publication and runtime_load gates."""

from __future__ import annotations

import copy
import dataclasses
import json
from hashlib import sha256
from typing import Any

import pytest

from omnivia_core.contracts.v1 import conformance
from omnivia_core.contracts.v1.canonical_json import canonical_bytes
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.semantics_workflow_check import (
    CONTRACT_VERSION_INCOMPATIBLE,
    CONTRACT_VERSION_UNRESOLVED,
    FLOATING_REFERENCE_PROHIBITED,
    SCHEMA_INVALID,
    SCHEMA_UNSUPPORTED,
    ResolvedSchema,
    _is_absolute_uri,
    check_workflow_value_schema,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name", "count"],
    "properties": {
        "name": {"type": "string", "minLength": 2, "maxLength": 8},
        "count": {"type": "integer", "minimum": 0, "maximum": 10},
    },
    "unevaluatedProperties": False,
}
PIN = ("https://schemas.example.test/order/item", "1.2.0")
SECRET = "S3CR3T-LITERAL"


def _resolver(schema: dict[str, Any] = SCHEMA, pin: tuple[str, str] = PIN):  # type: ignore[no-untyped-def]
    def resolve(schema_id: str, version: str) -> ResolvedSchema | None:
        if (schema_id, version) != pin:
            return None
        return ResolvedSchema(schema_id, version, schema)

    return resolve


def _value(value: Any = None, presence: str = "present", **ref: Any) -> dict[str, Any]:
    physical = {"schemaId": PIN[0], "schemaVersion": PIN[1], **ref}
    record: dict[str, Any] = {
        "contractName": "WorkflowValue",
        "valueId": "value-1",
        "semanticType": "order.item",
        "physicalSchema": physical,
        "cardinality": "single",
        "presence": presence,
        "classification": {"id": "public"},
        "lineage": {"id": "lineage-1"},
    }
    if presence in {"present", "null_value", "empty"}:
        record["value"] = value
    if presence in {"redacted", "unavailable", "failed"}:
        record["diagnostic"] = {"code": "X"}
    return record


GOOD = {"name": "widget", "count": 3}


@pytest.mark.parametrize("profile", ["publication", "runtime_load"])
def test_valid_present_value_passes_both_profiles(profile: str) -> None:
    result = check_workflow_value_schema(_value(GOOD), profile, _resolver())
    assert result.profile == profile
    assert result.definition_valid and not result.deferred_to_runtime
    assert result.diagnostics == ()


@pytest.mark.parametrize("profile", ["draft_save", "version_creation", "other"])
def test_other_profiles_are_refused(profile: str) -> None:
    with pytest.raises(ContractSemanticError):
        check_workflow_value_schema(_value(GOOD), profile, _resolver())


def test_malformed_workflow_value_is_refused_before_schema_evaluation() -> None:
    record = _value(GOOD)
    del record["value"]
    with pytest.raises(ContractSemanticError):
        check_workflow_value_schema(record, "publication", _resolver())


@pytest.mark.parametrize(
    "bad",
    [
        {"name": 5, "count": 3},  # type
        {"name": "x", "count": 3},  # minLength
        {"name": "widget", "count": 11},  # maximum
        {"name": "widget", "count": 3, "extra": SECRET},  # closed object
        {"name": "widget", "count": True},  # boolean is not integer
        {"name": "widget"},  # required
    ],
)
@pytest.mark.parametrize("profile", ["publication", "runtime_load"])
def test_schema_invalid_values(bad: dict[str, Any], profile: str) -> None:
    result = check_workflow_value_schema(_value(bad), profile, _resolver())
    assert result.profile == profile
    assert not result.definition_valid
    (diagnostic,) = result.diagnostics
    assert diagnostic.code == SCHEMA_INVALID
    assert diagnostic.subject == "value-1"
    assert (diagnostic.schema_id, diagnostic.schema_version) == PIN
    assert diagnostic.finding_count >= 1


@pytest.mark.parametrize(
    "reference",
    [
        {"schemaId": PIN[0]},
        {"schemaVersion": "1.2.0"},
        {"schemaId": PIN[0], "schemaVersion": "latest"},
        {"schemaId": PIN[0], "schemaVersion": "1.x"},
        {"schemaId": PIN[0], "schemaVersion": "^1.2.0"},
        {"schemaId": "order.item", "schemaVersion": "1.2.0"},  # not an absolute URI
        {"schemaId": "/schemas/order", "schemaVersion": "1.2.0"},  # relative
        {"id": PIN[0]},
        {"schemaId": PIN[0], "schemaDigest": "sha256:" + "0" * 64},  # non-canonical name only
    ],
)
def test_floating_reference(reference: dict[str, Any]) -> None:
    record = _value(GOOD)
    record["physicalSchema"] = reference
    called: list[object] = []
    result = check_workflow_value_schema(
        record, "runtime_load", lambda *a: called.append(a)  # type: ignore[arg-type,return-value]
    )
    assert [d.code for d in result.diagnostics] == [FLOATING_REFERENCE_PROHIBITED]
    assert called == []  # a floating reference never reaches the resolver


def test_unresolved_exact_reference() -> None:
    result = check_workflow_value_schema(
        _value(GOOD, schemaVersion="9.9.9"), "publication", _resolver()
    )
    (diagnostic,) = result.diagnostics
    assert diagnostic.code == CONTRACT_VERSION_UNRESOLVED
    assert diagnostic.schema_version == "9.9.9"


def test_resolver_answering_for_another_pin_is_unresolved() -> None:
    result = check_workflow_value_schema(
        _value(GOOD), "publication", lambda i, v: ResolvedSchema(i, "1.2.1", SCHEMA)
    )
    assert [d.code for d in result.diagnostics] == [CONTRACT_VERSION_UNRESOLVED]


@pytest.mark.parametrize(
    "extra",
    [
        {"schemaDigest": "sha256:" + "0" * 64},
        {"extra": "x"},
        {"digest": "sha256:" + "0" * 64, "schemaDigest": "sha256:" + "0" * 64},
    ],
)
def test_unknown_reference_member_is_floating_even_with_required_fields(
    extra: dict[str, Any],
) -> None:
    # JsonSchemaReference is closed: schemaId, schemaVersion and optional digest only.
    record = _value(GOOD, **extra)
    called: list[object] = []
    result = check_workflow_value_schema(
        record, "publication", lambda *a: called.append(a)  # type: ignore[arg-type,return-value]
    )
    assert not result.definition_valid
    assert [d.code for d in result.diagnostics] == [FLOATING_REFERENCE_PROHIBITED]
    assert called == []


def test_digest_match_and_mismatch() -> None:
    digest = f"sha256:{sha256(canonical_bytes(SCHEMA)).hexdigest()}"
    ok = check_workflow_value_schema(
        _value(GOOD, digest=digest), "publication", _resolver()
    )
    assert ok.definition_valid
    for wrong in ("sha256:" + "0" * 64, "not-a-digest"):
        result = check_workflow_value_schema(
            _value(GOOD, digest=wrong), "publication", _resolver()
        )
        assert [d.code for d in result.diagnostics] == [CONTRACT_VERSION_INCOMPATIBLE]


DEFS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name"],
    "properties": {"name": {"$ref": "#/$defs/Name"}},
    "$defs": {"Name": {"type": "string", "minLength": 2}},
}


@pytest.mark.parametrize("profile", ["publication", "runtime_load"])
def test_local_defs_reference_validates_and_rejects(profile: str) -> None:
    ok = check_workflow_value_schema(_value({"name": "ab"}), profile, _resolver(DEFS_SCHEMA))
    assert ok.definition_valid and ok.diagnostics == ()
    bad = check_workflow_value_schema(_value({"name": "a"}), profile, _resolver(DEFS_SCHEMA))
    assert [d.code for d in bad.diagnostics] == [SCHEMA_INVALID]


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "multipleOf": 2},
        {"type": "object", "properties": {"a": {"patternProperties": {}}}},
        {"$ref": "#/$defs/Missing"},  # unresolved local
        {"$ref": "#/$defs/Missing", "$defs": {"Other": {"type": "string"}}},
        {"$ref": "#/$defs/A/properties/x", "$defs": {"A": {"type": "string"}}},  # nested pointer
        {"$ref": "#/properties/x", "properties": {"x": {"type": "string"}}},  # not $defs
        {"$ref": "https://schemas.example.test/other.json#/$defs/A"},  # external absolute
        {"$ref": "other.json#/$defs/A"},  # external relative
        {"$ref": "#/$defs/A", "$defs": {"A": {"$ref": "#/$defs/A"}}},  # cycle
        {"$ref": "#/$defs/A", "$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}}},
        {"$ref": "#/$defs/A", "$defs": {"A": {"type": "string", "multipleOf": 2}}},
    ],
)
def test_unsupported_schema_keyword_and_ref(schema: dict[str, Any]) -> None:
    result = check_workflow_value_schema(_value("x"), "publication", _resolver(schema))
    assert [d.code for d in result.diagnostics] == [SCHEMA_UNSUPPORTED]


@pytest.mark.parametrize(
    "schema",
    [
        {1: "x"},  # non-string key
        {"type": "string", 2: {"type": "string"}},
        {"properties": {3: {"type": "string"}}},
        {"$defs": {4: {"type": "string"}}},
        {"enum": [{1: "x"}]},
        ["not", "a", "mapping"],
        "not a schema",
        None,
    ],
)
def test_unevaluable_resolved_schema_is_unsupported(schema: Any) -> None:
    for presence in ("present", "absent"):
        result = check_workflow_value_schema(
            _value({"a": 1}, presence), "publication", _resolver(schema)
        )
        assert [d.code for d in result.diagnostics] == [SCHEMA_UNSUPPORTED]


def test_unevaluable_schema_with_digest_is_unsupported() -> None:
    result = check_workflow_value_schema(
        _value(GOOD, digest="sha256:" + "0" * 64), "publication", _resolver({1: "x"})  # type: ignore[dict-item]
    )
    assert [d.code for d in result.diagnostics] == [SCHEMA_UNSUPPORTED]


def test_public_evaluator_wrapper_raises_only_schema_evaluation_error() -> None:
    assert conformance.SchemaEvaluationError.__name__ == "SchemaEvaluationError"
    assert not conformance.SchemaEvaluationError.__name__.startswith("_")
    for bad in ({1: "x"}, ["x"], {"$ref": "#/$defs/Nope"}):
        with pytest.raises(conformance.SchemaEvaluationError):
            conformance.require_supported_json_schema(bad)  # type: ignore[arg-type]
        with pytest.raises(conformance.SchemaEvaluationError):
            conformance.evaluate_json_schema("x", bad)  # type: ignore[arg-type]
    assert conformance.evaluate_json_schema("ab", DEFS_SCHEMA["$defs"]["Name"]) == ()


@pytest.mark.parametrize(
    "uri",
    [
        "https://schemas.example.test/order/item",
        "http://example.test/a?b=c#frag",
        "urn:omnivia:schema:order-item",
        "tag:example.test,2026:order",
        "file:///schemas/order.json",
        "a+b-c.d:rest",
        "https://example.test/a%20b",
    ],
)
def test_absolute_uri_accepted(uri: str) -> None:
    assert _is_absolute_uri(uri)


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "order.item",
        "/schemas/order",
        "//example.test/order",
        "../order",
        "#frag",
        "?q=1",
        ":no-scheme",
        "1http://example.test",
        "https:",
        "https://example.test/a b",
        " https://example.test",
        "https://example.test ",
        "https://example.test/\tx",
        "https://example.test/\nx",
        "https://example.test/\x00",
        "https://example.test/\x7f",
        "https://example.test/é",
        "https://example.test/<x>",
        "https://example.test/%zz",
        "https://example.test/a#b#c",
        None,
        5,
        b"https://example.test",
    ],
)
def test_absolute_uri_rejected(uri: object) -> None:
    assert not _is_absolute_uri(uri)


def test_unsupported_schema_is_not_hidden_when_value_is_deferred() -> None:
    result = check_workflow_value_schema(
        _value(presence="absent"), "publication", _resolver({"type": "string", "multipleOf": 2})
    )
    assert [d.code for d in result.diagnostics] == [SCHEMA_UNSUPPORTED]
    assert result.deferred_to_runtime


def test_null_and_empty_concrete_values_are_evaluated() -> None:
    null_ok = check_workflow_value_schema(
        _value(None, "null_value"), "publication", _resolver({"type": "null"})
    )
    assert null_ok.definition_valid and not null_ok.deferred_to_runtime
    null_bad = check_workflow_value_schema(_value(None, "null_value"), "publication", _resolver())
    assert [d.code for d in null_bad.diagnostics] == [SCHEMA_INVALID]
    empty_bad = check_workflow_value_schema(_value({}, "empty"), "runtime_load", _resolver())
    assert [d.code for d in empty_bad.diagnostics] == [SCHEMA_INVALID]
    empty_ok = check_workflow_value_schema(
        _value("", "empty"), "runtime_load", _resolver({"type": "string"})
    )
    assert empty_ok.definition_valid and not empty_ok.deferred_to_runtime


@pytest.mark.parametrize("presence", ["absent", "redacted", "unavailable", "failed"])
def test_non_inspectable_presence_is_deferred_not_validated(presence: str) -> None:
    result = check_workflow_value_schema(_value(presence=presence), "publication", _resolver())
    assert result.deferred_to_runtime
    assert result.definition_valid and result.diagnostics == ()


def test_deferred_value_still_requires_exact_resolvable_reference() -> None:
    result = check_workflow_value_schema(
        _value(presence="absent", schemaVersion="9.9.9"), "publication", _resolver()
    )
    assert [d.code for d in result.diagnostics] == [CONTRACT_VERSION_UNRESOLVED]
    assert result.deferred_to_runtime


def test_diagnostics_are_deterministic_and_do_not_leak_values() -> None:
    bad = {"name": SECRET, "count": 3, SECRET: SECRET}
    first = check_workflow_value_schema(_value(copy.deepcopy(bad)), "publication", _resolver())
    second = check_workflow_value_schema(_value(copy.deepcopy(bad)), "publication", _resolver())
    assert first == second
    assert SECRET not in repr(first)
    enum_schema = {"enum": ["a", "b"]}
    leaky = check_workflow_value_schema(_value(SECRET), "publication", _resolver(enum_schema))
    assert [d.code for d in leaky.diagnostics] == [SCHEMA_INVALID]
    assert SECRET not in repr(leaky) and SECRET not in json.dumps(dataclasses.asdict(leaky))


def test_result_is_frozen() -> None:
    result = check_workflow_value_schema(_value(GOOD), "publication", _resolver())
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.definition_valid = False  # type: ignore[misc]
