"""Tests exercising the canonical Application Contract v1 fixtures (ADR-038).

These are independent of ``scripts/check-application-contracts.py`` (which
enforces the same manifest as a repo-wide gate): this module re-validates the
fixtures directly against the schemas with ``jsonschema`` and re-decodes them
with the production codec, so a regression in either the fixtures or the
check script's own logic is still caught.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from omnivia_core.contracts.v1 import codec, generated
from omnivia_core.contracts.v1 import compatibility as compat

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
FIXTURES_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "fixtures"

BASE_URI = "https://contracts.omnivia.dev/application/v1/"
_ENVELOPE_REFS = {
    "RequestEnvelope": f"{BASE_URI}envelopes.schema.json#/$defs/RequestEnvelope",
    "ResponseEnvelope": f"{BASE_URI}envelopes.schema.json#/$defs/ResponseEnvelope",
}


def _target_ref(entry: dict[str, Any]) -> str:
    """The reference one manifest entry is an example of.

    Most fixtures are whole wire envelopes. A fixture that is an example of a single
    published record instead -- a canonical `Run`, a `ResolveWait` -- names that type in
    `definition` and is validated against the registry's published reference for it.
    """
    definition = entry.get("definition")
    if isinstance(definition, str):
        return f"{BASE_URI}application-v1.schema.json#/$defs/{definition}"
    return _ENVELOPE_REFS[entry["envelope"]]


def _load_manifest() -> list[dict[str, Any]]:
    manifest: dict[str, Any] = json.loads((FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))
    fixtures: list[dict[str, Any]] = manifest["fixtures"]
    return fixtures


def _load_fixture(file_name: str) -> Any:
    return json.loads((FIXTURES_DIR / file_name).read_text(encoding="utf-8"))


def _decode(entry: dict[str, Any], document: Any) -> Any:
    """Decode one fixture through the tolerant decoder its manifest entry names."""
    definition = entry.get("definition")
    if isinstance(definition, str):
        return getattr(generated, definition).from_wire(document)
    if entry["envelope"] == "RequestEnvelope":
        return codec.decode_request(document)
    return codec.decode_response(document)


def _registry() -> Registry:
    names = tuple(
        sorted(path.name.removesuffix(".schema.json") for path in SCHEMA_DIR.glob("*.schema.json"))
    )
    documents = [
        json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8")) for name in names
    ]
    resources = [Resource.from_contents(document) for document in documents]
    entries: list[tuple[str, Resource[Any]]] = []
    for resource in resources:
        resource_id = resource.id()
        assert resource_id is not None
        entries.append((resource_id, resource))
    return Registry().with_resources(entries)


MANIFEST = _load_manifest()
REGISTRY = _registry()


def _strict_validator(reference: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": reference},
        registry=REGISTRY,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def test_manifest_lists_exactly_eighteen_fixtures() -> None:
    """Thirteen envelope fixtures, plus the five canonical Runtime records RT-101 added."""
    assert len(MANIFEST) == 18
    assert sum(1 for entry in MANIFEST if entry.get("definition")) == 5


def test_manifest_ids_are_unique() -> None:
    ids = [entry["id"] for entry in MANIFEST]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda entry: entry["id"])
def test_fixture_matches_declared_schema_validity(entry: dict[str, Any]) -> None:
    document = _load_fixture(entry["file"])
    errors = list(_strict_validator(_target_ref(entry)).iter_errors(document))
    assert (not errors) == entry["schema_valid"]


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda entry: entry["id"])
def test_fixture_matches_declared_tolerant_decode(entry: dict[str, Any]) -> None:
    document = _load_fixture(entry["file"])
    try:
        _decode(entry, document)
        decoded = True
    except codec.ContractDecodeError:
        decoded = False
    assert decoded == entry["tolerant_decode"]


@pytest.mark.parametrize(
    "entry",
    [entry for entry in MANIFEST if entry["schema_valid"] and entry["tolerant_decode"]],
    ids=lambda entry: entry["id"],
)
def test_schema_valid_fixture_round_trips_through_codec(entry: dict[str, Any]) -> None:
    document = _load_fixture(entry["file"])
    if entry.get("definition"):
        record = _decode(entry, document)
        assert record.to_wire() == document
    elif entry["envelope"] == "RequestEnvelope":
        request = codec.decode_request(document)
        assert codec.encode_request(request) == document
    else:
        response = codec.decode_response(document)
        assert codec.encode_response(response) == document


def test_additive_unknown_optional_field_decodes_tolerantly_despite_failing_strict_schema() -> None:
    document = _load_fixture("additive-unknown-optional-field.json")
    assert list(
        _strict_validator(_ENVELOPE_REFS["ResponseEnvelope"]).iter_errors(document)
    ), "fixture must be strict-schema invalid"
    envelope = codec.decode_response(document)
    result = codec.response_result(envelope)
    assert result is not None
    assert result["count"] == 0
    assert tuple(result["items"]) == ()


def test_both_result_and_error_is_rejected_by_schema_and_codec() -> None:
    document = _load_fixture("both-result-and-error.json")
    assert list(
        _strict_validator(_ENVELOPE_REFS["ResponseEnvelope"]).iter_errors(document)
    ), "fixture must be strict-schema invalid"
    with pytest.raises(codec.ContractDecodeError):
        codec.decode_response(document)


def test_neither_result_nor_error_is_rejected_by_schema_and_codec() -> None:
    document = _load_fixture("neither-result-nor-error.json")
    assert list(
        _strict_validator(_ENVELOPE_REFS["ResponseEnvelope"]).iter_errors(document)
    ), "fixture must be strict-schema invalid"
    with pytest.raises(codec.ContractDecodeError):
        codec.decode_response(document)


def test_calendar_invalid_timestamp_fails_strict_schema_but_decodes_tolerantly() -> None:
    document = _load_fixture("calendar-invalid-timestamp.json")
    assert list(
        _strict_validator(_ENVELOPE_REFS["ResponseEnvelope"]).iter_errors(document)
    ), "fixture must be strict-schema invalid"
    envelope = codec.decode_response(document)
    assert envelope.metadata.canonical_resolution_time == "2026-02-30T00:00:00Z"


def test_unknown_open_value_is_preserved_and_fails_safe() -> None:
    document = _load_fixture("unknown-open-value.json")
    envelope = codec.decode_error_response(document)
    assert envelope.error.code == "quota_soft_limit_reached"
    assert envelope.error.retry_class == "retryable_experimental"
    assert envelope.error.code not in codec.FROZEN_ERROR_CODES
    assert envelope.error.retry_class not in codec.FROZEN_RETRY_CLASSES
    assert codec.is_error_retryable(envelope.error) is False


def test_duplicate_capability_ids_fixture_is_flagged_by_compatibility() -> None:
    document = _load_fixture("duplicate-capability-ids.json")
    # The production codec now rejects this fixture outright (see the test below); go
    # through the structural generated decoder directly to inspect the raw dataclass.
    envelope = codec.response_envelope_from_wire(document)
    supported = envelope.metadata.version.capabilities.supported
    assert compat.duplicate_capability_ids(supported) == ("memory.search",)


def test_duplicate_capability_ids_fixture_is_rejected_by_the_production_codec() -> None:
    document = _load_fixture("duplicate-capability-ids.json")
    with pytest.raises(compat.ContractSemanticError):
        codec.decode_success_response(document)


def test_incompatible_major_fixture_classifies_as_incompatible() -> None:
    document = _load_fixture("incompatible-major.json")
    envelope = codec.decode_error_response(document)
    assert envelope.error.code == "incompatible_version"
    window = envelope.metadata.version.compatibility.supported_api_versions
    details = envelope.error.details
    assert details is not None
    requested = details["requested_api_version"]
    assert compat.classify_version_compatibility(requested, window) == "incompatible"


def test_capability_denial_fixture_excludes_denied_capability_from_effective() -> None:
    document = _load_fixture("capability-denial.json")
    envelope = codec.decode_error_response(document)
    assert envelope.error.code == "capability_not_granted"
    caps = envelope.metadata.version.capabilities
    assert "memory.export" in {ref.id for ref in caps.supported}
    assert "memory.export" not in {ref.id for ref in caps.effective}


def test_minimal_request_fixture_has_every_optional_field_absent() -> None:
    document = _load_fixture("minimal-request.json")
    envelope = codec.decode_request(document)
    metadata = envelope.metadata
    assert metadata.deadline_ms is None
    assert metadata.idempotency_key is None
    assert metadata.mutation_precondition is None
    assert metadata.principal_claim is None
    assert metadata.scopes == ()
    assert metadata.required_capabilities == ()
