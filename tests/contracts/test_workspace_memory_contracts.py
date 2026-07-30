"""Tests for the A2.2 workspace/governed-memory application contract slice.

Covers the seven `workspace.*` / `memory.*` input/result DTO pairs (schema and
tolerant-decoder round trips, additive-unknown-field tolerance vs. strict
schema rejection, installation/workspace scope-id absence, empty-object DTO
handling), the `GovernedRecord` domain-scope/authority/reviewer fields, the
`CandidateAssertion` / `CandidateExtractionMetadata` candidate-provenance
shapes, and the semantic hardening in :mod:`omnivia_core.contracts.v1.semantics`:
temporal ordering, evidence/currentness/supersession/authority agreement,
`memory.create` proposed-only result-tuple enforcement (including the
reserved-field decode guard and workspace-match check), candidate
evidence/provenance coherence, and scope/paging validation.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from omnivia_core.contracts.v1 import generated
from omnivia_core.contracts.v1 import semantics as sem
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    CandidateAssertion,
    CandidateExtractionMetadata,
    ContractDecodeError,
    GovernedRecord,
    MemoryCreateInput,
    MemoryCreateResult,
    MemoryGetInput,
    MemoryGetResult,
    MemoryListInput,
    MemoryListResult,
    MemorySearchInput,
    MemorySearchResult,
    PageMetadata,
    RecordIdentity,
    RecordProvenance,
    RecordTemporalMetadata,
    SourceReference,
    WorkspaceCompatibility,
    WorkspaceCreateInput,
    WorkspaceCreateResult,
    WorkspaceDescriptor,
    WorkspaceInspectInput,
    WorkspaceInspectResult,
    WorkspaceListInput,
    WorkspaceListResult,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
BASE_URI = "https://contracts.omnivia.dev/application/v1/"

_SCHEMA_NAMES = (
    "common",
    "compatibility",
    "errors",
    "envelopes",
    "service",
    "records",
    "jobs",
    "operations",
    "workspace",
    "memory",
    "compatibility-matrix",
)


def _registry() -> Registry:
    entries: list[tuple[str, Resource[Any]]] = []
    for name in _SCHEMA_NAMES:
        document = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
        resource = Resource.from_contents(document)
        resource_id = resource.id()
        assert resource_id is not None
        entries.append((resource_id, resource))
    return Registry().with_resources(entries)


REGISTRY = _registry()

# Which canonical schema document declares each $defs entry under test.
_DEF_SOURCE: dict[str, str] = {
    "WorkspaceListInput": "workspace",
    "WorkspaceListResult": "workspace",
    "WorkspaceCreateInput": "workspace",
    "WorkspaceCreateResult": "workspace",
    "WorkspaceInspectInput": "workspace",
    "WorkspaceInspectResult": "workspace",
    "MemoryCreateInput": "memory",
    "MemoryCreateResult": "memory",
    "CandidateAssertion": "memory",
    "CandidateExtractionMetadata": "memory",
    "MemoryGetInput": "memory",
    "MemoryGetResult": "memory",
    "MemoryListInput": "memory",
    "MemoryListResult": "memory",
    "MemorySearchInput": "memory",
    "MemorySearchResult": "memory",
}


def _strict_validator(def_name: str) -> Draft202012Validator:
    source = _DEF_SOURCE[def_name]
    return Draft202012Validator(
        {"$ref": f"{BASE_URI}{source}.schema.json#/$defs/{def_name}"},
        registry=REGISTRY,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def _is_schema_valid(def_name: str, document: Any) -> bool:
    return not list(_strict_validator(def_name).iter_errors(document))


# --------------------------------------------------------------------------
# Wire-document builders
# --------------------------------------------------------------------------

T0 = "2024-01-01T00:00:00Z"
T1 = "2024-01-02T00:00:00Z"


def _workspace_descriptor_wire(
    *,
    workspace_id: str = "ws-1",
    display_name: str = "Acme Workspace",
    status: str = "active",
    workspace_format_version: str = "1.0",
    supported_window: tuple[str, str] = ("1.0", "1.0"),
    compat_status: str = "compatible",
    created_at: str = T0,
    updated_at: str | None = None,
) -> dict[str, Any]:
    minimum, maximum = supported_window
    document: dict[str, Any] = {
        "workspace_id": workspace_id,
        "display_name": display_name,
        "status": status,
        "compatibility": {
            "workspace_format_version": workspace_format_version,
            "supported_workspace_versions": {"minimum": minimum, "maximum": maximum},
            "status": compat_status,
        },
        "created_at": created_at,
    }
    if updated_at is not None:
        document["updated_at"] = updated_at
    return document


def _identity_wire(
    *,
    record_id: str = "rec-1",
    version: str = "v1",
    layer: str = "l1",
    governance_state: str = "proposed",
    currentness: str = "current",
    supersedes: dict[str, Any] | None = None,
    superseded_by: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "record_id": record_id,
        "version": version,
        "layer": layer,
        "governance_state": governance_state,
        "currentness": currentness,
    }
    if supersedes is not None:
        document["supersedes"] = supersedes
    if superseded_by is not None:
        document["superseded_by"] = superseded_by
    return document


def _temporal_wire(
    *,
    ingested_at: str = T0,
    recorded_at: str = T0,
    valid_from: str | None = None,
    valid_until: str | None = None,
    superseded_at: str | None = None,
    event_at: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {"ingested_at": ingested_at, "recorded_at": recorded_at}
    if valid_from is not None:
        document["valid_from"] = valid_from
    if valid_until is not None:
        document["valid_until"] = valid_until
    if superseded_at is not None:
        document["superseded_at"] = superseded_at
    if event_at is not None:
        document["event_at"] = event_at
    if observed_at is not None:
        document["observed_at"] = observed_at
    return document


def _history_entry_wire(
    *,
    actor_id: str = "actor-1",
    actor_kind: str = "user",
    action: str = "created",
    occurred_at: str = T0,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "actor_id": actor_id,
        "actor_kind": actor_kind,
        "action": action,
        "occurred_at": occurred_at,
    }
    if evidence is not None:
        document["evidence"] = evidence
    return document


def _provenance_wire(
    *,
    identity: dict[str, Any] | None = None,
    temporal: dict[str, Any] | None = None,
    evidence_disposition: str = "available",
    sources: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if sources is None:
        sources = [{"kind": "document", "source_id": "doc-1"}]
    return {
        "identity": identity if identity is not None else _identity_wire(),
        "temporal": temporal if temporal is not None else _temporal_wire(),
        "history": history if history is not None else [],
        "evidence_disposition": evidence_disposition,
        "sources": sources,
    }


def _governed_record_wire(
    *,
    workspace_id: str = "ws-1",
    record_type: str = "memory.fact",
    domain_scope: str = "personal.preferences",
    authority_level: str = "proposed",
    reviewer: str | None = None,
    provenance: dict[str, Any] | None = None,
    content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "workspace_id": workspace_id,
        "record_type": record_type,
        "domain_scope": domain_scope,
        "authority_level": authority_level,
        "provenance": provenance if provenance is not None else _provenance_wire(),
        "content": content if content is not None else {"fact": "hello"},
    }
    if reviewer is not None:
        document["reviewer"] = reviewer
    return document


def _candidate_assertion_wire(
    *,
    actor_id: str = "actor-1",
    actor_kind: str = "user",
    actor_role: str = "owner",
    asserted_at: str = T0,
    proposed_valid_from: str | None = None,
    proposed_valid_until: str | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "actor_id": actor_id,
        "actor_kind": actor_kind,
        "actor_role": actor_role,
        "asserted_at": asserted_at,
        "evidence": evidence if evidence is not None else [{"source": {"kind": "document", "source_id": "doc-1"}}],
    }
    if proposed_valid_from is not None:
        document["proposed_valid_from"] = proposed_valid_from
    if proposed_valid_until is not None:
        document["proposed_valid_until"] = proposed_valid_until
    return document


def _candidate_extraction_metadata_wire(
    *,
    extractor_id: str = "extractor-1",
    extractor_version: str | None = None,
    model_version: str | None = None,
    prompt_version: str | None = None,
    extracted_at: str = T0,
    confidence: float | None = None,
    reconciliation_state: str | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {"extractor_id": extractor_id, "extracted_at": extracted_at}
    if extractor_version is not None:
        document["extractor_version"] = extractor_version
    if model_version is not None:
        document["model_version"] = model_version
    if prompt_version is not None:
        document["prompt_version"] = prompt_version
    if confidence is not None:
        document["confidence"] = confidence
    if reconciliation_state is not None:
        document["reconciliation_state"] = reconciliation_state
    return document


def _memory_create_input_wire(
    *,
    record_type: str = "memory.fact",
    domain_scope: str = "personal.preferences",
    content: dict[str, Any] | None = None,
    evidence_disposition: str = "available",
    sources: list[dict[str, Any]] | None = None,
    assertion: dict[str, Any] | None = None,
    extraction: dict[str, Any] | None = None,
    event_at: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "record_type": record_type,
        "domain_scope": domain_scope,
        "content": content if content is not None else {"fact": "hello"},
        "evidence_disposition": evidence_disposition,
        "sources": sources if sources is not None else [{"kind": "document", "source_id": "doc-1"}],
        "assertion": assertion if assertion is not None else _candidate_assertion_wire(),
    }
    if extraction is not None:
        document["extraction"] = extraction
    if event_at is not None:
        document["event_at"] = event_at
    if observed_at is not None:
        document["observed_at"] = observed_at
    return document


def _record_temporal(**overrides: Any) -> RecordTemporalMetadata:
    return RecordTemporalMetadata.from_wire(_temporal_wire(**overrides))


def _record_identity(**overrides: Any) -> RecordIdentity:
    return RecordIdentity.from_wire(_identity_wire(**overrides))


def _governed_record(**overrides: Any) -> GovernedRecord:
    return GovernedRecord.from_wire(_governed_record_wire(**overrides))


def _record_provenance(**overrides: Any) -> RecordProvenance:
    return RecordProvenance.from_wire(_provenance_wire(**overrides))


# --------------------------------------------------------------------------
# 1. The seven operation pairs are public and generated
# --------------------------------------------------------------------------

OPERATION_PAIRS: tuple[tuple[str, str], ...] = (
    ("WorkspaceListInput", "WorkspaceListResult"),
    ("WorkspaceCreateInput", "WorkspaceCreateResult"),
    ("WorkspaceInspectInput", "WorkspaceInspectResult"),
    ("MemoryCreateInput", "MemoryCreateResult"),
    ("MemoryGetInput", "MemoryGetResult"),
    ("MemoryListInput", "MemoryListResult"),
    ("MemorySearchInput", "MemorySearchResult"),
)


def test_exactly_seven_operation_pairs_are_public_and_generated() -> None:
    assert len(OPERATION_PAIRS) == 7
    for input_name, result_name in OPERATION_PAIRS:
        assert input_name in generated.__all__
        assert result_name in generated.__all__
        assert dataclasses.is_dataclass(getattr(generated, input_name))
        assert dataclasses.is_dataclass(getattr(generated, result_name))


INSTALLATION_SCOPED_INPUTS = ("WorkspaceListInput", "WorkspaceCreateInput")
WORKSPACE_SCOPED_INPUTS = (
    "WorkspaceInspectInput",
    "MemoryCreateInput",
    "MemoryGetInput",
    "MemoryListInput",
    "MemorySearchInput",
)


@pytest.mark.parametrize("name", INSTALLATION_SCOPED_INPUTS + WORKSPACE_SCOPED_INPUTS)
def test_operation_inputs_carry_no_workspace_id_field(name: str) -> None:
    dataclass = getattr(generated, name)
    field_names = {field.name for field in dataclasses.fields(dataclass)}
    assert "workspace_id" not in field_names


# --------------------------------------------------------------------------
# 2. Strict schema and tolerant DTO round trips for every pair
# --------------------------------------------------------------------------

_WS_DESCRIPTOR = _workspace_descriptor_wire()
_GOVERNED_RECORD = _governed_record_wire()

ROUND_TRIP_CASES: tuple[tuple[str, type, dict[str, Any]], ...] = (
    ("WorkspaceListInput", WorkspaceListInput, {}),
    (
        "WorkspaceListInput",
        WorkspaceListInput,
        {"limit": 10, "page": {"continuation_token": "tok-1"}},
    ),
    ("WorkspaceListResult", WorkspaceListResult, {"workspaces": [_WS_DESCRIPTOR], "page": {}}),
    ("WorkspaceCreateInput", WorkspaceCreateInput, {"display_name": "Acme"}),
    ("WorkspaceCreateResult", WorkspaceCreateResult, {"workspace": _WS_DESCRIPTOR}),
    ("WorkspaceInspectInput", WorkspaceInspectInput, {}),
    ("WorkspaceInspectResult", WorkspaceInspectResult, {"workspace": _WS_DESCRIPTOR}),
    ("MemoryCreateInput", MemoryCreateInput, _memory_create_input_wire()),
    ("MemoryCreateResult", MemoryCreateResult, {"record": _GOVERNED_RECORD}),
    ("MemoryGetInput", MemoryGetInput, {"record_id": "rec-1"}),
    ("MemoryGetResult", MemoryGetResult, {"record": _GOVERNED_RECORD}),
    ("MemoryListInput", MemoryListInput, {}),
    ("MemoryListResult", MemoryListResult, {"records": [_GOVERNED_RECORD], "page": {}}),
    ("MemorySearchInput", MemorySearchInput, {"query": "hello world"}),
    ("MemorySearchResult", MemorySearchResult, {"records": [_GOVERNED_RECORD], "page": {}}),
)


@pytest.mark.parametrize("def_name,dataclass,wire", ROUND_TRIP_CASES)
def test_strict_schema_accepts_canonical_document(
    def_name: str, dataclass: type, wire: dict[str, Any]
) -> None:
    assert _is_schema_valid(def_name, wire)


@pytest.mark.parametrize("def_name,dataclass,wire", ROUND_TRIP_CASES)
def test_tolerant_dto_round_trips_canonical_document(
    def_name: str, dataclass: type, wire: dict[str, Any]
) -> None:
    value = dataclass.from_wire(wire)
    assert value.to_wire() == wire


# --------------------------------------------------------------------------
# 3. Tolerant decode ignores additive unknown fields; strict schema rejects them
# --------------------------------------------------------------------------


@pytest.mark.parametrize("def_name,dataclass,wire", ROUND_TRIP_CASES)
def test_additive_unknown_field_tolerated_by_dto_rejected_by_schema(
    def_name: str, dataclass: type, wire: dict[str, Any]
) -> None:
    augmented = {**wire, "future_hint_field": "some-value"}
    assert not _is_schema_valid(def_name, augmented)
    baseline = dataclass.from_wire(wire)
    assert dataclass.from_wire(augmented) == baseline


# --------------------------------------------------------------------------
# 4. Empty-object DTOs accept {} and reject a non-mapping payload
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dataclass", [WorkspaceListInput, WorkspaceInspectInput, MemoryListInput]
)
def test_all_optional_or_empty_dto_accepts_empty_mapping(dataclass: type) -> None:
    dataclass.from_wire({})


@pytest.mark.parametrize(
    "dataclass",
    [
        WorkspaceListInput,
        WorkspaceCreateInput,
        WorkspaceInspectInput,
        MemoryCreateInput,
        MemoryGetInput,
        MemoryListInput,
        MemorySearchInput,
    ],
)
@pytest.mark.parametrize("bad_payload", [None, [], "not-a-mapping", 42])
def test_dto_rejects_non_mapping_payload(dataclass: type, bad_payload: Any) -> None:
    with pytest.raises(ContractDecodeError, match="expected an object"):
        dataclass.from_wire(bad_payload)


# --------------------------------------------------------------------------
# 5. Workspace descriptor server-issued identity and compatibility
# --------------------------------------------------------------------------


def test_workspace_descriptor_has_stable_id_and_compatibility_window() -> None:
    descriptor = WorkspaceDescriptor.from_wire(_WS_DESCRIPTOR)
    assert descriptor.workspace_id == "ws-1"
    compat = descriptor.compatibility
    assert isinstance(compat, WorkspaceCompatibility)
    assert compat.workspace_format_version == "1.0"
    assert compat.supported_workspace_versions.minimum == "1.0"
    assert compat.supported_workspace_versions.maximum == "1.0"
    assert compat.status == "compatible"
    sem.validate_workspace_compatibility(compat)


def test_workspace_compatibility_rejects_version_outside_window() -> None:
    compat = WorkspaceCompatibility.from_wire(
        {
            "workspace_format_version": "2.0",
            "supported_workspace_versions": {"minimum": "1.0", "maximum": "1.5"},
            "status": "compatible",
        }
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_workspace_compatibility(compat)


# --------------------------------------------------------------------------
# 6. GovernedRecord: scope, authority_level, reviewer
# --------------------------------------------------------------------------


def test_governed_record_carries_domain_scope_authority_and_optional_reviewer() -> None:
    record = _governed_record(domain_scope="personal.preferences", reviewer=None)
    assert record.domain_scope == "personal.preferences"
    assert record.authority_level == "proposed"
    assert record.reviewer is None
    assert record.workspace_id == "ws-1"
    wire = record.to_wire()
    assert wire["domain_scope"] == "personal.preferences"


def test_governed_record_reviewer_is_optional() -> None:
    record = _governed_record()
    assert record.reviewer is None
    assert "reviewer" not in record.to_wire()


def test_governed_record_schema_requires_domain_scope() -> None:
    empty_scope = _governed_record_wire(domain_scope="")
    assert not _is_schema_valid("MemoryGetResult", {"record": empty_scope})
    missing_scope = _governed_record_wire()
    del missing_scope["domain_scope"]
    assert not _is_schema_valid("MemoryGetResult", {"record": missing_scope})


def test_domain_scope_is_distinct_from_authorization_scope() -> None:
    """`domain_scope` is a record classification, not a `Scope` authorization token."""
    field_names = {field.name for field in dataclasses.fields(GovernedRecord)}
    assert "domain_scope" in field_names
    assert "scope" not in field_names
    input_field_names = {field.name for field in dataclasses.fields(MemoryCreateInput)}
    assert "domain_scope" in input_field_names
    assert "scope" not in input_field_names


def test_memory_create_input_cannot_assert_authority_level_or_reviewer() -> None:
    field_names = {field.name for field in dataclasses.fields(MemoryCreateInput)}
    assert "authority_level" not in field_names
    assert "reviewer" not in field_names
    assert "governance_state" not in field_names
    assert "record_id" not in field_names
    assert "version" not in field_names
    assert "supersedes" not in field_names
    assert "superseded_by" not in field_names
    assert "superseded_at" not in field_names
    assert "recorded_at" not in field_names
    assert "workspace_id" not in field_names
    assert "layer" not in field_names
    assert "provenance" not in field_names
    assert "identity" not in field_names
    assert "history" not in field_names
    assert "temporal" not in field_names
    assert "ingested_at" not in field_names


def test_memory_create_input_domain_scope_is_required() -> None:
    field_names = {field.name for field in dataclasses.fields(MemoryCreateInput)}
    assert "domain_scope" in field_names
    value = MemoryCreateInput.from_wire(_memory_create_input_wire(domain_scope="project.roadmap"))
    assert value.domain_scope == "project.roadmap"
    wire = _memory_create_input_wire()
    del wire["domain_scope"]
    with pytest.raises(ContractDecodeError):
        MemoryCreateInput.from_wire(wire)


def test_open_values_are_preserved_verbatim() -> None:
    wire = _governed_record_wire(
        record_type="memory.some_future_kind",
        authority_level="some_future_authority",
    )
    wire["provenance"]["identity"]["currentness"] = "some_future_currentness"
    wire["provenance"]["identity"]["governance_state"] = "some_future_state"
    record = GovernedRecord.from_wire(wire)
    assert record.record_type == "memory.some_future_kind"
    assert record.authority_level == "some_future_authority"
    assert record.provenance.identity.currentness == "some_future_currentness"
    assert record.provenance.identity.governance_state == "some_future_state"
    assert record.to_wire() == wire


# --------------------------------------------------------------------------
# 7. Temporal validation
# --------------------------------------------------------------------------


def test_temporal_metadata_round_trips_every_field() -> None:
    wire = _temporal_wire(
        valid_from=T0,
        valid_until=T1,
        superseded_at=T1,
        event_at=T0,
        observed_at=T0,
        recorded_at=T1,
    )
    temporal = RecordTemporalMetadata.from_wire(wire)
    assert temporal.to_wire() == wire
    sem.validate_record_temporal_metadata(temporal)


def test_valid_from_after_valid_until_fails() -> None:
    temporal = _record_temporal(valid_from=T1, valid_until=T0)
    with pytest.raises(ContractSemanticError):
        sem.validate_record_temporal_metadata(temporal)


def test_ingested_at_after_recorded_at_fails() -> None:
    temporal = _record_temporal(ingested_at=T1, recorded_at=T0)
    with pytest.raises(ContractSemanticError):
        sem.validate_record_temporal_metadata(temporal)


def test_superseded_at_before_recorded_at_fails() -> None:
    temporal = _record_temporal(recorded_at=T1, superseded_at=T0)
    with pytest.raises(ContractSemanticError):
        sem.validate_record_temporal_metadata(temporal)


@pytest.mark.parametrize(
    "bad_timestamp",
    [
        "2024-01-01T00:00:00",  # naive: no offset at all
        "2024-01-01T00:00:00+00:00",  # numeric offset, not literal Z
        "not-a-timestamp",  # malformed
        "2024-02-30T00:00:00Z",  # pattern-valid, calendar-invalid
    ],
)
def test_malformed_or_non_utc_timestamps_are_rejected(bad_timestamp: str) -> None:
    temporal = _record_temporal(recorded_at=bad_timestamp)
    with pytest.raises(ContractSemanticError):
        sem.validate_record_temporal_metadata(temporal)


def test_temporal_equality_boundaries_pass() -> None:
    temporal = _record_temporal(
        valid_from=T0,
        valid_until=T0,
        ingested_at=T0,
        recorded_at=T0,
        superseded_at=T0,
    )
    sem.validate_record_temporal_metadata(temporal)


# --------------------------------------------------------------------------
# 8. Evidence disposition / sources agreement
# --------------------------------------------------------------------------


def test_available_requires_at_least_one_source() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_evidence_disposition_sources("available", ())


def test_available_with_sources_passes() -> None:
    source = SourceReference.from_wire({"kind": "document", "source_id": "doc-1"})
    sem.validate_evidence_disposition_sources("available", (source,))


@pytest.mark.parametrize("disposition", ["unavailable", "redacted"])
def test_disposition_permitting_empty_sources_passes(disposition: str) -> None:
    sem.validate_evidence_disposition_sources(disposition, ())


def test_unknown_disposition_with_empty_sources_fails_closed() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_evidence_disposition_sources("some_future_disposition", ())


# --------------------------------------------------------------------------
# 9. Currentness / supersession agreement
# --------------------------------------------------------------------------


def test_current_with_no_supersession_passes() -> None:
    identity = _record_identity(currentness="current")
    temporal = _record_temporal()
    sem.validate_record_currentness_consistency(identity, temporal)


def test_current_must_not_carry_superseded_by() -> None:
    identity = _record_identity(
        currentness="current",
        superseded_by={"record_id": "rec-1", "version": "v2"},
    )
    temporal = _record_temporal()
    with pytest.raises(ContractSemanticError):
        sem.validate_record_currentness_consistency(identity, temporal)


def test_current_must_not_carry_superseded_at() -> None:
    identity = _record_identity(currentness="current")
    temporal = _record_temporal(superseded_at=T0)
    with pytest.raises(ContractSemanticError):
        sem.validate_record_currentness_consistency(identity, temporal)


def test_superseded_requires_both_superseded_by_and_superseded_at() -> None:
    identity_missing_pointer = _record_identity(currentness="superseded")
    temporal_with_at = _record_temporal(superseded_at=T0)
    with pytest.raises(ContractSemanticError):
        sem.validate_record_currentness_consistency(identity_missing_pointer, temporal_with_at)

    identity_with_pointer = _record_identity(
        currentness="superseded",
        superseded_by={"record_id": "rec-1", "version": "v2"},
    )
    temporal_missing_at = _record_temporal()
    with pytest.raises(ContractSemanticError):
        sem.validate_record_currentness_consistency(identity_with_pointer, temporal_missing_at)


def test_superseded_with_both_present_passes() -> None:
    identity = _record_identity(
        currentness="superseded",
        superseded_by={"record_id": "rec-1", "version": "v2"},
    )
    temporal = _record_temporal(superseded_at=T0)
    sem.validate_record_currentness_consistency(identity, temporal)


def test_record_must_not_supersede_itself() -> None:
    identity = _record_identity(
        record_id="rec-1",
        version="v1",
        supersedes={"record_id": "rec-1", "version": "v1"},
    )
    temporal = _record_temporal()
    with pytest.raises(ContractSemanticError):
        sem.validate_record_currentness_consistency(identity, temporal)


def test_record_must_not_be_superseded_by_itself() -> None:
    identity = _record_identity(
        record_id="rec-1",
        version="v1",
        currentness="superseded",
        superseded_by={"record_id": "rec-1", "version": "v1"},
    )
    temporal = _record_temporal(superseded_at=T0)
    with pytest.raises(ContractSemanticError):
        sem.validate_record_currentness_consistency(identity, temporal)


def test_supersedes_points_backward_and_superseded_by_points_forward() -> None:
    # Older version v1: current no more, points forward to its replacement v2.
    older_identity = _record_identity(
        record_id="rec-1",
        version="v1",
        currentness="superseded",
        superseded_by={"record_id": "rec-1", "version": "v2"},
    )
    older_temporal = _record_temporal(recorded_at=T0, superseded_at=T1)
    sem.validate_record_currentness_consistency(older_identity, older_temporal)

    # Newer version v2: current, points backward to what it replaced, v1.
    newer_identity = _record_identity(
        record_id="rec-1",
        version="v2",
        currentness="current",
        supersedes={"record_id": "rec-1", "version": "v1"},
    )
    newer_temporal = _record_temporal(recorded_at=T1)
    sem.validate_record_currentness_consistency(newer_identity, newer_temporal)


def test_validate_governed_record_composes_all_three_checks() -> None:
    record = _governed_record(
        provenance=_provenance_wire(evidence_disposition="available", sources=[])
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)


def test_validate_governed_record_passes_for_consistent_record() -> None:
    sem.validate_governed_record(_governed_record())


# --------------------------------------------------------------------------
# 10. memory.create hardening
# --------------------------------------------------------------------------


def _valid_memory_create_input_wire() -> dict[str, Any]:
    return _memory_create_input_wire()


def test_decode_memory_create_input_accepts_clean_payload() -> None:
    wire = _valid_memory_create_input_wire()
    value = sem.decode_memory_create_input(wire)
    assert value == MemoryCreateInput.from_wire(wire)


def test_decode_memory_create_input_ignores_genuinely_unknown_field() -> None:
    wire = {**_valid_memory_create_input_wire(), "some_future_optional_field": "x"}
    value = sem.decode_memory_create_input(wire)
    assert value == MemoryCreateInput.from_wire(_valid_memory_create_input_wire())


@pytest.mark.parametrize("reserved_field", sorted(sem.MEMORY_CREATE_RESERVED_INPUT_FIELDS))
def test_decode_memory_create_input_rejects_each_reserved_field(reserved_field: str) -> None:
    wire = {**_valid_memory_create_input_wire(), reserved_field: "smuggled-value"}
    with pytest.raises(ContractSemanticError):
        sem.decode_memory_create_input(wire)


def test_reserved_input_fields_cover_every_server_owned_governance_name() -> None:
    assert sem.MEMORY_CREATE_RESERVED_INPUT_FIELDS == {
        "workspace_id",
        "layer",
        "provenance",
        "identity",
        "history",
        "temporal",
        "ingested_at",
        "recorded_at",
        "record_id",
        "version",
        "governance_state",
        "currentness",
        "authority_level",
        "reviewer",
        "supersedes",
        "superseded_by",
        "superseded_at",
        "valid_from",
        "valid_until",
    }


def _valid_memory_create_result(**overrides: Any) -> MemoryCreateResult:
    return MemoryCreateResult.from_wire({"record": _governed_record_wire(**overrides)})


def test_validate_memory_create_result_accepts_fresh_proposal() -> None:
    sem.validate_memory_create_result(_valid_memory_create_result(), expected_workspace_id="ws-1")


def test_validate_memory_create_result_rejects_non_proposed_governance_state() -> None:
    result = MemoryCreateResult.from_wire(
        {"record": _governed_record_wire(provenance=_provenance_wire(identity=_identity_wire(governance_state="accepted")))}
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_result(result, expected_workspace_id="ws-1")


@pytest.mark.parametrize("layer", ["l0", "l2", "l3", "l4", "some_future_layer"])
def test_validate_memory_create_result_rejects_non_candidate_layer(layer: str) -> None:
    result = MemoryCreateResult.from_wire(
        {"record": _governed_record_wire(provenance=_provenance_wire(identity=_identity_wire(layer=layer)))}
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_result(result, expected_workspace_id="ws-1")


@pytest.mark.parametrize("authority_level", ["reviewed", "canonical", "accepted", "some_future_authority"])
def test_validate_memory_create_result_rejects_non_proposed_authority_level(authority_level: str) -> None:
    result = MemoryCreateResult.from_wire({"record": _governed_record_wire(authority_level=authority_level)})
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_result(result, expected_workspace_id="ws-1")


def test_validate_memory_create_result_rejects_reviewer() -> None:
    result = MemoryCreateResult.from_wire({"record": _governed_record_wire(reviewer="reviewer-1")})
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_result(result, expected_workspace_id="ws-1")


def test_validate_memory_create_result_rejects_empty_domain_scope() -> None:
    result = MemoryCreateResult.from_wire({"record": _governed_record_wire(domain_scope="")})
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_result(result, expected_workspace_id="ws-1")


@pytest.mark.parametrize("expected_workspace_id", [None, "", "   ", "!!!not-valid!!!"])
def test_validate_memory_create_result_rejects_malformed_expected_workspace_id(
    expected_workspace_id: str | None,
) -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_result(
            _valid_memory_create_result(),
            expected_workspace_id=expected_workspace_id,  # type: ignore[arg-type]
        )


def test_validate_memory_create_result_rejects_mismatched_workspace_id() -> None:
    result = _valid_memory_create_result(workspace_id="ws-1")
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_result(result, expected_workspace_id="ws-2")


@pytest.mark.parametrize("currentness", ["superseded", "retracted", "some_future_currentness"])
def test_validate_memory_create_result_rejects_non_fresh_currentness(currentness: str) -> None:
    result = MemoryCreateResult.from_wire(
        {"record": _governed_record_wire(provenance=_provenance_wire(identity=_identity_wire(currentness=currentness)))}
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_result(result, expected_workspace_id="ws-1")


def test_validate_memory_create_result_rejects_supersedes() -> None:
    result = MemoryCreateResult.from_wire(
        {
            "record": _governed_record_wire(
                provenance=_provenance_wire(
                    identity=_identity_wire(
                        supersedes={"record_id": "rec-0", "version": "v0"}
                    )
                )
            )
        }
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_result(result, expected_workspace_id="ws-1")


def test_validate_memory_create_result_rejects_superseded_by() -> None:
    result = MemoryCreateResult.from_wire(
        {
            "record": _governed_record_wire(
                provenance=_provenance_wire(
                    identity=_identity_wire(
                        currentness="superseded",
                        superseded_by={"record_id": "rec-2", "version": "v2"},
                    ),
                    temporal=_temporal_wire(superseded_at=T0),
                )
            )
        }
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_result(result, expected_workspace_id="ws-1")


def test_validate_memory_create_result_rejects_superseded_at() -> None:
    result = MemoryCreateResult.from_wire(
        {
            "record": _governed_record_wire(
                provenance=_provenance_wire(temporal=_temporal_wire(superseded_at=T0))
            )
        }
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_result(result, expected_workspace_id="ws-1")


def test_validate_memory_create_result_composes_governed_record_validation() -> None:
    result = MemoryCreateResult.from_wire(
        {
            "record": _governed_record_wire(
                provenance=_provenance_wire(evidence_disposition="available", sources=[])
            )
        }
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_result(result, expected_workspace_id="ws-1")


# --------------------------------------------------------------------------
# 10a. RecordDomainScope semantic validation
# --------------------------------------------------------------------------


def test_validate_record_domain_scope_accepts_pattern_valid_value() -> None:
    sem.validate_record_domain_scope("personal.preferences")


def test_validate_record_domain_scope_accepts_unrecognized_but_well_formed_value() -> None:
    sem.validate_record_domain_scope("some_future.domain_classification")


@pytest.mark.parametrize(
    "bad_domain_scope",
    [
        "",
        "   ",
        "!!!",
        "Upper.Case",
        "personal..preferences",
        ".leading.dot",
        "trailing.dot.",
        "a" * 129,
    ],
)
def test_validate_record_domain_scope_rejects_malformed_values(bad_domain_scope: str) -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_record_domain_scope(bad_domain_scope)


@pytest.mark.parametrize(
    "bad_domain_scope",
    ["", "   ", "!!!", "Upper.Case", "personal..preferences"],
)
def test_validate_governed_record_rejects_malformed_domain_scope(bad_domain_scope: str) -> None:
    record = _governed_record(domain_scope=bad_domain_scope)
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)


@pytest.mark.parametrize(
    "bad_domain_scope",
    ["", "   ", "!!!", "Upper.Case", "personal..preferences"],
)
def test_validate_memory_create_input_rejects_malformed_domain_scope(bad_domain_scope: str) -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(domain_scope=bad_domain_scope)
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


@pytest.mark.parametrize(
    "bad_domain_scope",
    ["", "   ", "!!!", "Upper.Case", "personal..preferences"],
)
def test_validate_memory_create_result_rejects_malformed_domain_scope_value(
    bad_domain_scope: str,
) -> None:
    result = MemoryCreateResult.from_wire({"record": _governed_record_wire(domain_scope=bad_domain_scope)})
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_result(result, expected_workspace_id="ws-1")


# --------------------------------------------------------------------------
# 10b. General governed-record authority coherence
# --------------------------------------------------------------------------


def test_authority_coherence_accepts_proposed_with_no_reviewer() -> None:
    sem.validate_governed_record_authority_coherence("proposed", "proposed", None)


@pytest.mark.parametrize("authority_level", ["reviewed", "canonical"])
def test_authority_coherence_rejects_proposed_state_with_accepted_authority(authority_level: str) -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record_authority_coherence("proposed", authority_level, "reviewer-1")


def test_authority_coherence_rejects_proposed_state_with_reviewer() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record_authority_coherence("proposed", "proposed", "reviewer-1")


@pytest.mark.parametrize("authority_level", sorted(sem.AUTHORITY_LEVELS_REQUIRING_REVIEWER))
def test_authority_coherence_rejects_accepted_authority_without_reviewer(authority_level: str) -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record_authority_coherence("accepted", authority_level, None)


@pytest.mark.parametrize("authority_level", sorted(sem.AUTHORITY_LEVELS_REQUIRING_REVIEWER))
def test_authority_coherence_accepts_accepted_authority_with_reviewer(authority_level: str) -> None:
    sem.validate_governed_record_authority_coherence("accepted", authority_level, "reviewer-1")


def test_authority_coherence_accepted_state_requires_reviewer_even_with_unknown_authority() -> None:
    """A known decision-bearing `governance_state` such as `accepted` requires a
    reviewer/policy identity even when `authority_level` is an unrecognized-but-valid
    open code: the unknown authority level must never be read as exempting it."""
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record_authority_coherence("accepted", "some_future_authority", None)


def test_authority_coherence_unknown_governance_state_and_authority_are_preserved() -> None:
    """An unrecognized `governance_state` (not itself decision-bearing per
    `GOVERNANCE_STATES_REQUIRING_REVIEWER`) paired with an unrecognized `authority_level`
    is preserved as-is and never promoted to `accepted`, so it does not require a
    reviewer."""
    assert "some_future_state" not in sem.GOVERNANCE_STATES_REQUIRING_REVIEWER
    sem.validate_governed_record_authority_coherence("some_future_state", "some_future_authority", None)


def test_validate_governed_record_rejects_incoherent_authority() -> None:
    record = _governed_record(reviewer="reviewer-1")
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)


# --------------------------------------------------------------------------
# 10c. memory.create candidate evidence/provenance coherence
# --------------------------------------------------------------------------


def test_validate_memory_create_input_accepts_coherent_candidate() -> None:
    sem.validate_memory_create_input(MemoryCreateInput.from_wire(_memory_create_input_wire()))


def test_candidate_assertion_and_extraction_round_trip_losslessly() -> None:
    wire = _memory_create_input_wire(
        assertion=_candidate_assertion_wire(
            proposed_valid_from=T0,
            proposed_valid_until=T1,
        ),
        extraction=_candidate_extraction_metadata_wire(
            extractor_version="1.0.0",
            model_version="model-x",
            prompt_version="prompt-y",
            confidence=0.75,
            reconciliation_state="novel",
        ),
    )
    value = MemoryCreateInput.from_wire(wire)
    assert isinstance(value.assertion, CandidateAssertion)
    assert isinstance(value.extraction, CandidateExtractionMetadata)
    assert value.assertion.proposed_valid_from == T0
    assert value.assertion.proposed_valid_until == T1
    assert value.extraction.confidence == 0.75
    assert value.extraction.reconciliation_state == "novel"
    assert value.to_wire() == wire


def test_available_requires_at_least_one_concrete_evidence_reference_on_assertion() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            evidence_disposition="available",
            assertion=_candidate_assertion_wire(evidence=[]),
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


@pytest.mark.parametrize("disposition", ["unavailable", "redacted"])
def test_disposition_excusing_empty_sources_also_excuses_empty_assertion_evidence(disposition: str) -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            evidence_disposition=disposition,
            sources=[],
            assertion=_candidate_assertion_wire(evidence=[]),
        )
    )
    sem.validate_memory_create_input(candidate)


def test_assertion_evidence_source_must_be_among_declared_sources() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            sources=[{"kind": "document", "source_id": "doc-1"}],
            assertion=_candidate_assertion_wire(
                evidence=[{"source": {"kind": "document", "source_id": "doc-2"}}]
            ),
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_proposed_valid_from_after_valid_until_fails() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            assertion=_candidate_assertion_wire(proposed_valid_from=T1, proposed_valid_until=T0)
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_proposed_validity_equality_boundary_passes() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            assertion=_candidate_assertion_wire(proposed_valid_from=T0, proposed_valid_until=T0)
        )
    )
    sem.validate_memory_create_input(candidate)


def test_empty_actor_role_fails() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(assertion=_candidate_assertion_wire(actor_role="   "))
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_empty_domain_scope_fails_semantic_validation() -> None:
    candidate = MemoryCreateInput.from_wire(_memory_create_input_wire(domain_scope="   "))
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_extraction_confidence_out_of_range_fails(confidence: float) -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            extraction=_candidate_extraction_metadata_wire(confidence=confidence)
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_extraction_confidence_boundary_values_pass(confidence: float) -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            extraction=_candidate_extraction_metadata_wire(confidence=confidence)
        )
    )
    sem.validate_memory_create_input(candidate)


def test_unknown_reconciliation_state_is_preserved_and_does_not_fail_validation() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            extraction=_candidate_extraction_metadata_wire(reconciliation_state="some_future_state")
        )
    )
    assert candidate.extraction is not None
    assert candidate.extraction.reconciliation_state == "some_future_state"
    sem.validate_memory_create_input(candidate)


def test_extraction_metadata_is_optional() -> None:
    candidate = MemoryCreateInput.from_wire(_memory_create_input_wire())
    assert candidate.extraction is None
    sem.validate_memory_create_input(candidate)


@pytest.mark.parametrize("bad_actor_id", ["", "   ", "!!!", "a" * 129])
def test_malformed_actor_id_fails(bad_actor_id: str) -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(assertion=_candidate_assertion_wire(actor_id=bad_actor_id))
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


@pytest.mark.parametrize("bad_actor_kind", ["", "   ", "Upper", "not..valid"])
def test_malformed_actor_kind_fails(bad_actor_kind: str) -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(assertion=_candidate_assertion_wire(actor_kind=bad_actor_kind))
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


@pytest.mark.parametrize(
    "bad_timestamp",
    ["2024-01-01T00:00:00", "2024-01-01T00:00:00+00:00", "not-a-timestamp", "2024-02-30T00:00:00Z"],
)
def test_malformed_asserted_at_fails(bad_timestamp: str) -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(assertion=_candidate_assertion_wire(asserted_at=bad_timestamp))
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


@pytest.mark.parametrize("field", ["event_at", "observed_at"])
def test_malformed_optional_top_level_timestamp_fails(field: str) -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(**{field: "not-a-timestamp"})
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_malformed_proposed_valid_from_fails_even_when_valid_until_absent() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            assertion=_candidate_assertion_wire(proposed_valid_from="not-a-timestamp")
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_malformed_proposed_valid_until_fails_even_when_valid_from_absent() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            assertion=_candidate_assertion_wire(proposed_valid_until="not-a-timestamp")
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


@pytest.mark.parametrize(
    "field", ["extractor_id", "extractor_version", "model_version", "prompt_version"]
)
def test_malformed_extraction_identifier_fails(field: str) -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            extraction=_candidate_extraction_metadata_wire(**{field: "!!!"})
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_malformed_extraction_extracted_at_fails() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            extraction=_candidate_extraction_metadata_wire(extracted_at="not-a-timestamp")
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_malformed_extraction_reconciliation_state_fails() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            extraction=_candidate_extraction_metadata_wire(reconciliation_state="Not-Valid")
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


# --------------------------------------------------------------------------
# 10d. Evidence-source coherence and exact evidence coverage
# --------------------------------------------------------------------------


def test_candidate_evidence_round_trips_losslessly_with_locator_span_and_excerpt() -> None:
    wire = _memory_create_input_wire(
        sources=[
            {
                "kind": "document",
                "source_id": "doc-1",
                "locator": "https://example.test/doc-1",
                "retrieved_at": T0,
            }
        ],
        assertion=_candidate_assertion_wire(
            evidence=[
                {
                    "source": {
                        "kind": "document",
                        "source_id": "doc-1",
                        "locator": "https://example.test/doc-1",
                        "retrieved_at": T0,
                    },
                    "span": {"pointer": "/content", "start_offset": 0, "end_offset": 10},
                    "excerpt": "hello world",
                }
            ]
        ),
    )
    candidate = MemoryCreateInput.from_wire(wire)
    sem.validate_memory_create_input(candidate)
    assert candidate.to_wire() == wire


def test_assertion_evidence_conflicting_locator_fails() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            sources=[{"kind": "document", "source_id": "doc-1", "locator": "https://a.test"}],
            assertion=_candidate_assertion_wire(
                evidence=[
                    {
                        "source": {
                            "kind": "document",
                            "source_id": "doc-1",
                            "locator": "https://b.test",
                        }
                    }
                ]
            ),
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_assertion_evidence_conflicting_retrieved_at_fails() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            sources=[{"kind": "document", "source_id": "doc-1", "retrieved_at": T0}],
            assertion=_candidate_assertion_wire(
                evidence=[
                    {
                        "source": {
                            "kind": "document",
                            "source_id": "doc-1",
                            "retrieved_at": T1,
                        }
                    }
                ]
            ),
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_assertion_evidence_additive_locator_absent_from_declared_source_passes() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            sources=[{"kind": "document", "source_id": "doc-1"}],
            assertion=_candidate_assertion_wire(
                evidence=[
                    {
                        "source": {
                            "kind": "document",
                            "source_id": "doc-1",
                            "locator": "https://a.test",
                        }
                    }
                ]
            ),
        )
    )
    sem.validate_memory_create_input(candidate)


def test_assertion_evidence_need_not_repeat_declared_source_locator() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            sources=[{"kind": "document", "source_id": "doc-1", "locator": "https://a.test"}],
            assertion=_candidate_assertion_wire(
                evidence=[{"source": {"kind": "document", "source_id": "doc-1"}}]
            ),
        )
    )
    sem.validate_memory_create_input(candidate)


# --------------------------------------------------------------------------
# 11. Scope and paging validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("workspace_id", [None, "", "   ", "!!!not-valid!!!"])
def test_workspace_scope_rejects_missing_or_malformed_workspace_id(workspace_id: str | None) -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_operation_scope_workspace_id(sem.SCOPE_KIND_WORKSPACE, workspace_id)


def test_workspace_scope_accepts_valid_workspace_id() -> None:
    sem.validate_operation_scope_workspace_id(sem.SCOPE_KIND_WORKSPACE, "ws-123")


def test_installation_scope_accepts_absent_workspace_id() -> None:
    sem.validate_operation_scope_workspace_id(sem.SCOPE_KIND_INSTALLATION, None)


def test_installation_scope_rejects_present_workspace_id() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_operation_scope_workspace_id(sem.SCOPE_KIND_INSTALLATION, "ws-123")


def test_unknown_scope_kind_fails_closed() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_operation_scope_workspace_id("tenant", None)
    with pytest.raises(ContractSemanticError):
        sem.validate_operation_scope_workspace_id("tenant", "ws-123")


@pytest.mark.parametrize("limit", [None, 1, sem.MAX_PAGE_LIMIT])
def test_page_limit_accepts_boundary_and_absent_values(limit: int | None) -> None:
    sem.validate_page_limit(limit)


@pytest.mark.parametrize("limit", [0, -1, sem.MAX_PAGE_LIMIT + 1])
def test_page_limit_rejects_out_of_range_values(limit: int) -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_page_limit(limit)


def test_continuation_token_is_opaque_and_round_trips_verbatim() -> None:
    token = "opaque~Token.123:ABC-xyz"
    page = PageMetadata.from_wire({"continuation_token": token})
    assert page.continuation_token == token
    assert page.to_wire() == {"continuation_token": token}


# --------------------------------------------------------------------------
# 12. decode_memory_create_input is fully semantic, not just structural
# --------------------------------------------------------------------------


def test_decode_memory_create_input_rejects_malformed_domain_scope() -> None:
    wire = _memory_create_input_wire(domain_scope="Upper.Case")
    with pytest.raises(ContractSemanticError):
        sem.decode_memory_create_input(wire)


def test_decode_memory_create_input_rejects_incoherent_candidate_evidence_source() -> None:
    wire = _memory_create_input_wire(
        sources=[{"kind": "document", "source_id": "doc-1"}],
        assertion=_candidate_assertion_wire(
            evidence=[{"source": {"kind": "document", "source_id": "doc-2"}}]
        ),
    )
    with pytest.raises(ContractSemanticError):
        sem.decode_memory_create_input(wire)


def test_decode_memory_create_input_rejects_malformed_asserted_at() -> None:
    wire = _memory_create_input_wire(
        assertion=_candidate_assertion_wire(asserted_at="not-a-timestamp")
    )
    with pytest.raises(ContractSemanticError):
        sem.decode_memory_create_input(wire)


def test_decode_memory_create_input_rejects_out_of_range_confidence() -> None:
    wire = _memory_create_input_wire(
        extraction=_candidate_extraction_metadata_wire(confidence=1.5)
    )
    with pytest.raises(ContractSemanticError):
        sem.decode_memory_create_input(wire)


def test_decode_memory_create_input_still_tolerates_genuinely_additive_field() -> None:
    wire = {**_valid_memory_create_input_wire(), "some_future_optional_field": "x"}
    value = sem.decode_memory_create_input(wire)
    assert value == MemoryCreateInput.from_wire(_valid_memory_create_input_wire())


# --------------------------------------------------------------------------
# 13. Concrete source/evidence detail semantic validation
# --------------------------------------------------------------------------


def test_evidence_span_rejects_negative_start_offset() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            assertion=_candidate_assertion_wire(
                evidence=[
                    {
                        "source": {"kind": "document", "source_id": "doc-1"},
                        "span": {"pointer": "/content", "start_offset": -1, "end_offset": 5},
                    }
                ]
            )
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_evidence_span_rejects_negative_end_offset() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            assertion=_candidate_assertion_wire(
                evidence=[
                    {
                        "source": {"kind": "document", "source_id": "doc-1"},
                        "span": {"pointer": "/content", "end_offset": -1},
                    }
                ]
            )
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_evidence_span_rejects_reversed_offsets() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            assertion=_candidate_assertion_wire(
                evidence=[
                    {
                        "source": {"kind": "document", "source_id": "doc-1"},
                        "span": {"pointer": "/content", "start_offset": 10, "end_offset": 5},
                    }
                ]
            )
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_evidence_span_offsets_equal_boundary_passes() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            assertion=_candidate_assertion_wire(
                evidence=[
                    {
                        "source": {"kind": "document", "source_id": "doc-1"},
                        "span": {"pointer": "/content", "start_offset": 5, "end_offset": 5},
                    }
                ]
            )
        )
    )
    sem.validate_memory_create_input(candidate)


def test_evidence_source_rejects_malformed_retrieved_at() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            assertion=_candidate_assertion_wire(
                evidence=[
                    {
                        "source": {
                            "kind": "document",
                            "source_id": "doc-1",
                            "retrieved_at": "not-a-timestamp",
                        }
                    }
                ]
            )
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_declared_source_rejects_malformed_retrieved_at() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            sources=[
                {"kind": "document", "source_id": "doc-1", "retrieved_at": "not-a-timestamp"}
            ]
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_declared_source_rejects_overlong_locator() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            sources=[{"kind": "document", "source_id": "doc-1", "locator": "x" * 2049}]
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_evidence_span_rejects_overlong_pointer() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            assertion=_candidate_assertion_wire(
                evidence=[
                    {
                        "source": {"kind": "document", "source_id": "doc-1"},
                        "span": {"pointer": "x" * 2049},
                    }
                ]
            )
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_evidence_rejects_overlong_excerpt() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            assertion=_candidate_assertion_wire(
                evidence=[
                    {
                        "source": {"kind": "document", "source_id": "doc-1"},
                        "excerpt": "x" * 4097,
                    }
                ]
            )
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_unknown_non_excusing_disposition_with_zero_assertion_evidence_fails_closed() -> None:
    assert "some_future_disposition" not in sem.EVIDENCE_DISPOSITIONS_PERMITTING_EMPTY_SOURCES
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            evidence_disposition="some_future_disposition",
            sources=[{"kind": "document", "source_id": "doc-1"}],
            assertion=_candidate_assertion_wire(evidence=[]),
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


# --------------------------------------------------------------------------
# 14. Authority/governance contradictions closed
# --------------------------------------------------------------------------


def test_authority_coherence_rejects_blank_reviewer() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record_authority_coherence("accepted", "accepted", "   ")


def test_authority_coherence_rejects_malformed_reviewer() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record_authority_coherence(
            "accepted", "accepted", "!!!not-valid!!!"
        )


def test_authority_coherence_rejects_blank_governance_state() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record_authority_coherence("   ", "proposed", None)


def test_authority_coherence_rejects_malformed_governance_state() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record_authority_coherence("!!!not-valid!!!", "proposed", None)


def test_authority_coherence_rejects_overlong_governance_state() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record_authority_coherence("a" * 129, "proposed", None)


def test_authority_coherence_rejects_blank_authority_level() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record_authority_coherence("proposed", "   ", None)


def test_authority_coherence_rejects_malformed_authority_level() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record_authority_coherence("proposed", "!!!not-valid!!!", None)


def test_authority_coherence_rejects_overlong_authority_level() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record_authority_coherence("proposed", "a" * 129, None)


def test_authority_coherence_accepts_unknown_well_formed_governance_state_and_authority() -> None:
    sem.validate_governed_record_authority_coherence(
        "some_future_state", "some_future_authority", None
    )


@pytest.mark.parametrize(
    "governance_state", sorted(sem.GOVERNANCE_STATES_KNOWN_NON_ACCEPTED - {"proposed"})
)
@pytest.mark.parametrize("authority_level", sorted(sem.AUTHORITY_LEVELS_REQUIRING_REVIEWER))
def test_authority_coherence_rejects_non_accepted_known_governance_with_decision_bearing_authority(
    governance_state: str, authority_level: str
) -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record_authority_coherence(
            governance_state, authority_level, "reviewer-1"
        )


def test_authority_coherence_accepts_unknown_governance_with_decision_bearing_authority() -> None:
    assert "some_future_state" not in sem.GOVERNANCE_STATES_KNOWN_NON_ACCEPTED
    sem.validate_governed_record_authority_coherence(
        "some_future_state", "accepted", "reviewer-1"
    )


def test_validate_governed_record_rejects_reviewer_with_blank_value() -> None:
    record = _governed_record(
        provenance=_provenance_wire(identity=_identity_wire(governance_state="accepted")),
        authority_level="accepted",
        reviewer="   ",
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)


# --------------------------------------------------------------------------
# 15. Structural/schema rejection of missing required fields
# --------------------------------------------------------------------------


def test_memory_create_input_missing_assertion_fails_structural_decode() -> None:
    wire = _memory_create_input_wire()
    del wire["assertion"]
    with pytest.raises(ContractDecodeError, match="assertion"):
        MemoryCreateInput.from_wire(wire)
    assert not _is_schema_valid("MemoryCreateInput", wire)


def test_candidate_assertion_missing_actor_role_fails_structural_decode() -> None:
    wire = _candidate_assertion_wire()
    del wire["actor_role"]
    with pytest.raises(ContractDecodeError, match="actor_role"):
        CandidateAssertion.from_wire(wire)
    assert not _is_schema_valid("CandidateAssertion", wire)


def test_evidence_reference_missing_source_fails_structural_decode() -> None:
    wire = _memory_create_input_wire(
        assertion=_candidate_assertion_wire(evidence=[{"span": {"pointer": "/content"}}])
    )
    with pytest.raises(ContractDecodeError, match="source"):
        MemoryCreateInput.from_wire(wire)


# --------------------------------------------------------------------------
# 16. `_decode_number` overflow is normalized to `ContractDecodeError`
# --------------------------------------------------------------------------


def test_decode_number_rejects_huge_integer_instead_of_leaking_overflow_error() -> None:
    huge_integer = 10**400
    with pytest.raises(ContractDecodeError):
        CandidateExtractionMetadata.from_wire(
            {**_candidate_extraction_metadata_wire(), "confidence": huge_integer}
        )


def test_decode_number_rejects_bool() -> None:
    with pytest.raises(ContractDecodeError):
        CandidateExtractionMetadata.from_wire(
            {**_candidate_extraction_metadata_wire(), "confidence": True}
        )


def test_decode_number_rejects_nan() -> None:
    with pytest.raises(ContractDecodeError):
        CandidateExtractionMetadata.from_wire(
            {**_candidate_extraction_metadata_wire(), "confidence": float("nan")}
        )


@pytest.mark.parametrize("infinity", [float("inf"), float("-inf")])
def test_decode_number_rejects_infinity(infinity: float) -> None:
    with pytest.raises(ContractDecodeError):
        CandidateExtractionMetadata.from_wire(
            {**_candidate_extraction_metadata_wire(), "confidence": infinity}
        )


@pytest.mark.parametrize("boundary", [0, 1])
def test_decode_number_accepts_valid_boundary_values(boundary: int) -> None:
    decoded = CandidateExtractionMetadata.from_wire(
        {**_candidate_extraction_metadata_wire(), "confidence": boundary}
    )
    assert decoded.confidence == float(boundary)


# --------------------------------------------------------------------------
# 12. A2.2 governed-record trust closure: complete temporal validation,
#     canonical bounded-value validators, provenance/history validation, and
#     layer/state/authority contradiction closure.
# --------------------------------------------------------------------------

_OVERLONG_ID = "a" * 129

# --------------------------------------------------------------------------
# 12a. Complete general temporal validation
# --------------------------------------------------------------------------


def test_temporal_metadata_rejects_malformed_event_at() -> None:
    temporal = _record_temporal(event_at="not-a-timestamp")
    with pytest.raises(ContractSemanticError):
        sem.validate_record_temporal_metadata(temporal)


def test_temporal_metadata_rejects_malformed_observed_at() -> None:
    temporal = _record_temporal(observed_at="not-a-timestamp")
    with pytest.raises(ContractSemanticError):
        sem.validate_record_temporal_metadata(temporal)


def test_temporal_metadata_rejects_one_sided_malformed_valid_from() -> None:
    temporal = _record_temporal(valid_from="not-a-timestamp")
    with pytest.raises(ContractSemanticError):
        sem.validate_record_temporal_metadata(temporal)


def test_temporal_metadata_rejects_one_sided_malformed_valid_until() -> None:
    temporal = _record_temporal(valid_until="not-a-timestamp")
    with pytest.raises(ContractSemanticError):
        sem.validate_record_temporal_metadata(temporal)


def test_temporal_metadata_accepts_one_sided_valid_from() -> None:
    temporal = _record_temporal(valid_from=T0)
    sem.validate_record_temporal_metadata(temporal)


def test_temporal_metadata_accepts_one_sided_valid_until() -> None:
    temporal = _record_temporal(valid_until=T1)
    sem.validate_record_temporal_metadata(temporal)


# --------------------------------------------------------------------------
# 12b. Canonical bounded value validators (fullmatch, not .match)
# --------------------------------------------------------------------------


def test_operation_scope_workspace_id_rejects_trailing_newline() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_operation_scope_workspace_id(sem.SCOPE_KIND_WORKSPACE, "ws-1\n")


def test_operation_scope_workspace_id_rejects_overlong_value() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_operation_scope_workspace_id(sem.SCOPE_KIND_WORKSPACE, _OVERLONG_ID)


def test_memory_create_result_rejects_expected_workspace_id_trailing_newline() -> None:
    result = _valid_memory_create_result()
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_result(result, expected_workspace_id="ws-1\n")


def test_memory_create_result_rejects_expected_workspace_id_overlong() -> None:
    result = _valid_memory_create_result()
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_result(result, expected_workspace_id=_OVERLONG_ID)


def test_memory_create_result_rejects_record_workspace_id_trailing_newline() -> None:
    record = GovernedRecord.from_wire(
        _governed_record_wire(
            workspace_id="ws-1\n",
            provenance=_provenance_wire(
                identity=_identity_wire(governance_state="proposed"),
            ),
            authority_level="proposed",
        )
    )
    result = MemoryCreateResult(record=record)
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_result(result, expected_workspace_id="ws-1\n")


def test_validate_memory_create_input_rejects_malformed_record_type() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(record_type="Not-Valid!!!")
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_validate_memory_create_input_rejects_overlong_record_type() -> None:
    candidate = MemoryCreateInput.from_wire(_memory_create_input_wire(record_type="a" * 129))
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_validate_memory_create_input_rejects_malformed_evidence_disposition() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(evidence_disposition="Not-Valid!!!")
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_validate_memory_create_input_rejects_overlong_evidence_disposition() -> None:
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(evidence_disposition="a" * 129)
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_validate_governed_record_rejects_malformed_workspace_id() -> None:
    record = _governed_record(workspace_id="ws-1\n")
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)


def test_validate_governed_record_rejects_overlong_workspace_id() -> None:
    record = _governed_record(workspace_id=_OVERLONG_ID)
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)


def test_validate_governed_record_rejects_malformed_record_type() -> None:
    record = _governed_record(record_type="Not-Valid!!!")
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)


def test_validate_governed_record_rejects_malformed_authority_level() -> None:
    record = _governed_record(authority_level="Not-Valid!!!")
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)


def test_validate_governed_record_rejects_malformed_identity_record_id() -> None:
    record = _governed_record(
        provenance=_provenance_wire(identity=_identity_wire(record_id=""))
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)


def test_validate_governed_record_rejects_malformed_identity_version() -> None:
    record = _governed_record(
        provenance=_provenance_wire(identity=_identity_wire(version="a" * 513))
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)


def test_validate_governed_record_rejects_malformed_identity_layer() -> None:
    record = _governed_record(
        provenance=_provenance_wire(identity=_identity_wire(layer="Not-Valid!!!"))
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)


def test_validate_governed_record_rejects_malformed_identity_governance_state() -> None:
    record = _governed_record(
        provenance=_provenance_wire(identity=_identity_wire(governance_state="Not-Valid!!!"))
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)


def test_validate_governed_record_rejects_malformed_identity_currentness() -> None:
    record = _governed_record(
        provenance=_provenance_wire(identity=_identity_wire(currentness="Not-Valid!!!"))
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)


def test_validate_governed_record_rejects_malformed_evidence_disposition() -> None:
    record = _governed_record(
        provenance=_provenance_wire(evidence_disposition="Not-Valid!!!")
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)


def test_validate_governed_record_accepts_unrecognized_but_well_formed_open_codes() -> None:
    record = _governed_record(
        record_type="some_future.record_type",
        authority_level="some_future_authority",
        provenance=_provenance_wire(
            identity=_identity_wire(
                layer="some_future_layer",
                governance_state="some_future_state",
                currentness="some_future_currentness",
            ),
            evidence_disposition="unavailable",
            sources=[],
        ),
    )
    sem.validate_governed_record(record)


# --------------------------------------------------------------------------
# 12c. Governed-record provenance validation
# --------------------------------------------------------------------------


def test_validate_record_provenance_accepts_empty_history() -> None:
    provenance = _record_provenance()
    sem.validate_record_provenance(provenance)


def test_validate_record_provenance_rejects_duplicate_sources() -> None:
    sources = [
        {"kind": "document", "source_id": "doc-1", "locator": "loc-a"},
        {"kind": "document", "source_id": "doc-1", "locator": "loc-b"},
    ]
    provenance = _record_provenance(sources=sources)
    with pytest.raises(ContractSemanticError):
        sem.validate_record_provenance(provenance)


def test_validate_record_provenance_rejects_duplicate_sources_reverse_order() -> None:
    sources = [
        {"kind": "document", "source_id": "doc-1", "locator": "loc-b"},
        {"kind": "document", "source_id": "doc-1", "locator": "loc-a"},
    ]
    provenance = _record_provenance(sources=sources)
    with pytest.raises(ContractSemanticError):
        sem.validate_record_provenance(provenance)


def test_validate_memory_create_input_rejects_duplicate_declared_sources() -> None:
    sources = [
        {"kind": "document", "source_id": "doc-1"},
        {"kind": "document", "source_id": "doc-1"},
    ]
    candidate = MemoryCreateInput.from_wire(
        _memory_create_input_wire(
            sources=sources,
            assertion=_candidate_assertion_wire(evidence=[]),
            evidence_disposition="unavailable",
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_memory_create_input(candidate)


def test_validate_record_provenance_rejects_malformed_history_actor_id() -> None:
    provenance = _record_provenance(
        history=[_history_entry_wire(actor_id="")],
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_record_provenance(provenance)


def test_validate_record_provenance_rejects_malformed_history_actor_kind() -> None:
    provenance = _record_provenance(
        history=[_history_entry_wire(actor_kind="Not-Valid!!!")],
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_record_provenance(provenance)


def test_validate_record_provenance_rejects_malformed_history_action() -> None:
    provenance = _record_provenance(
        history=[_history_entry_wire(action="Not-Valid!!!")],
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_record_provenance(provenance)


def test_validate_record_provenance_rejects_malformed_history_occurred_at() -> None:
    provenance = _record_provenance(
        history=[_history_entry_wire(occurred_at="not-a-timestamp")],
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_record_provenance(provenance)


def test_validate_record_provenance_rejects_undeclared_history_evidence_source() -> None:
    provenance = _record_provenance(
        sources=[{"kind": "document", "source_id": "doc-1"}],
        history=[
            _history_entry_wire(
                evidence=[{"source": {"kind": "document", "source_id": "doc-2"}}]
            )
        ],
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_record_provenance(provenance)


def test_validate_record_provenance_accepts_history_evidence_matching_declared_source() -> None:
    provenance = _record_provenance(
        sources=[{"kind": "document", "source_id": "doc-1"}],
        history=[
            _history_entry_wire(
                evidence=[{"source": {"kind": "document", "source_id": "doc-1"}}]
            )
        ],
    )
    sem.validate_record_provenance(provenance)


def test_validate_record_provenance_rejects_history_evidence_reversed_span() -> None:
    provenance = _record_provenance(
        sources=[{"kind": "document", "source_id": "doc-1"}],
        history=[
            _history_entry_wire(
                evidence=[
                    {
                        "source": {"kind": "document", "source_id": "doc-1"},
                        "span": {"pointer": "p", "start_offset": 10, "end_offset": 1},
                    }
                ]
            )
        ],
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_record_provenance(provenance)


def test_validate_governed_record_composes_provenance_validation() -> None:
    record = _governed_record(
        provenance=_provenance_wire(
            history=[_history_entry_wire(actor_id="")],
        )
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)


# --------------------------------------------------------------------------
# 12d. Layer/state/authority contradiction closure
# --------------------------------------------------------------------------


def test_authority_coherence_rejects_accepted_governance_with_proposed_authority() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record_authority_coherence("accepted", "proposed", None)


def test_authority_coherence_accepts_accepted_governance_with_non_proposed_authority() -> None:
    sem.validate_governed_record_authority_coherence("accepted", "reviewed", "reviewer-1")


@pytest.mark.parametrize("authority_level", ["reviewed", "canonical", "accepted"])
def test_layer_coherence_rejects_l1_candidate_layer_with_decision_bearing_authority(
    authority_level: str,
) -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record_layer_coherence("l1", "proposed", authority_level)


def test_layer_coherence_rejects_l1_candidate_layer_with_accepted_governance() -> None:
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record_layer_coherence("l1", "accepted", "reviewed")


def test_layer_coherence_accepts_l1_layer_with_proposed_governance_and_authority() -> None:
    sem.validate_governed_record_layer_coherence("l1", "proposed", "proposed")


def test_layer_coherence_preserves_unknown_layer_values() -> None:
    sem.validate_governed_record_layer_coherence("some_future_layer", "accepted", "canonical")


def test_layer_coherence_ignores_non_candidate_known_layer() -> None:
    sem.validate_governed_record_layer_coherence("l2", "accepted", "canonical")


def test_validate_governed_record_rejects_l1_candidate_with_accepted_governance() -> None:
    record = _governed_record(
        authority_level="reviewed",
        reviewer="reviewer-1",
        provenance=_provenance_wire(
            identity=_identity_wire(layer="l1", governance_state="accepted"),
        ),
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)


def test_validate_governed_record_rejects_l1_candidate_with_canonical_authority() -> None:
    # `governance_state` is deliberately an unrecognized-but-well-formed open value here,
    # so `validate_governed_record_authority_coherence` has no known-state contradiction
    # to raise on and this genuinely isolates the new layer-vs-authority rule.
    record = _governed_record(
        authority_level="canonical",
        reviewer="reviewer-1",
        provenance=_provenance_wire(
            identity=_identity_wire(layer="l1", governance_state="some_future_state"),
        ),
    )
    with pytest.raises(ContractSemanticError):
        sem.validate_governed_record(record)
