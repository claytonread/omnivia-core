"""Tests for the A2 slice 1 foundational wire DTOs (T-0631, ADR-038, ADR-039).

Covers structural round trips for the service/records/jobs/operations/
compatibility-matrix definitions, proves that runtime probes are a distinct
shape from application request/response envelopes rather than a disguised
copy of them, and proves the semantic repairs made after architecture
review: an unauthenticated probe cannot fabricate policy authority, governed
records carry an honest governance/evidence/supersession shape, durable jobs
carry immutable audit linkage and a mutually exclusive terminal outcome, and
operation/compatibility-matrix metadata is complete enough to grow without
later required-field breaks.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from omnivia_core.contracts.v1.generated import (
    ApiError,
    CapabilityCompatibilityEntry,
    CapabilityRef,
    CapabilityRequirement,
    CompatibilityMatrix,
    ContractDecodeError,
    EvidenceReference,
    JobAttempt,
    JobCancellationOutcome,
    JobControl,
    JobEvent,
    JobHandle,
    JobIdentity,
    JobProgress,
    JobTerminalCancellation,
    JobTerminalFailure,
    JobTerminalResult,
    JobTerminalSuccess,
    OperationAuditMetadata,
    OperationCompatibilityEntry,
    OperationIdempotencyMetadata,
    OperationJobMetadata,
    OperationMetadata,
    OperationPaginationMetadata,
    OperationPreconditionMetadata,
    OperationScope,
    ProvenanceEntry,
    RecordIdentity,
    RecordProvenance,
    RecordTemporalMetadata,
    ReleaseCompatibilityEntry,
    RequestEnvelope,
    ResponseEnvelope,
    ServiceComponentStatus,
    ServiceEndpointDescriptor,
    ServiceProbeRequest,
    ServiceProbeResult,
    ServiceProcessEvidence,
    SourceReference,
    SourceSpan,
    SupersessionReference,
    VersionWindow,
    job_terminal_result_from_wire,
    job_terminal_result_to_wire,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
BASE_URI = "https://contracts.omnivia.dev/application/v1/"

ALL_SCHEMAS = (
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
    # `jobs` reaches `evidence.MediaType` from `ImportSourceDescriptor`, so the
    # evidence document has to be resolvable here even though nothing in this
    # file validates an evidence shape directly.
    "evidence",
    "compatibility-matrix",
    "application-v1",
)


def _registry() -> Registry:
    entries: list[tuple[str, Resource[Any]]] = []
    for name in ALL_SCHEMAS:
        document = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
        resource = Resource.from_contents(document)
        resource_id = resource.id()
        assert resource_id is not None
        entries.append((resource_id, resource))
    return Registry().with_resources(entries)


REGISTRY = _registry()


def _strict_validator(schema_file: str, def_name: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": f"{BASE_URI}{schema_file}.schema.json#/$defs/{def_name}"},
        registry=REGISTRY,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def _assert_schema_valid(schema_file: str, def_name: str, document: Any) -> None:
    validator = _strict_validator(schema_file, def_name)
    errors = list(validator.iter_errors(document))
    assert not errors, f"expected {def_name} to be schema-valid, found {errors}"


def _assert_schema_invalid(schema_file: str, def_name: str, document: Any) -> None:
    validator = _strict_validator(schema_file, def_name)
    errors = list(validator.iter_errors(document))
    assert errors, f"expected {def_name} to be schema-invalid, found none"


# --------------------------------------------------------------------------
# Runtime probes are distinct from application envelopes: no operation, no
# input, no workspace/authority scoping, no result/error branch, and no
# fabricated granted/effective capability authority.
# --------------------------------------------------------------------------


def test_service_probe_request_carries_no_envelope_fields() -> None:
    probe_field_names = {field.name for field in fields(ServiceProbeRequest)}
    envelope_field_names = {field.name for field in fields(RequestEnvelope)}
    assert probe_field_names.isdisjoint(envelope_field_names - {"metadata"} | {"operation", "input"})
    assert "operation" not in probe_field_names
    assert "input" not in probe_field_names
    assert "workspace_id" not in probe_field_names


def test_service_probe_result_carries_no_response_envelope_fields() -> None:
    result_field_names = {field.name for field in fields(ServiceProbeResult)}
    assert "result" not in result_field_names
    assert "error" not in result_field_names
    assert "metadata" not in result_field_names


def test_service_probe_result_capabilities_are_supported_only_not_a_capability_set() -> None:
    """A discovery probe has no caller and no workspace to authorize against, so it must
    never be able to state `granted`/`effective` policy authority -- only what the server
    build supports."""
    result_field_names = {field.name for field in fields(ServiceProbeResult)}
    assert "supported_capabilities" in result_field_names
    assert "capabilities" not in result_field_names

    capability_ref_field_names = {field.name for field in fields(CapabilityRef)}
    assert capability_ref_field_names == {"id", "version"}
    assert "granted" not in capability_ref_field_names
    assert "effective" not in capability_ref_field_names


def test_response_envelope_is_still_a_union_of_the_two_response_branches() -> None:
    # A regression guard: adding ServiceProbeResult must not fold it into the
    # existing ResponseEnvelope discriminated union.
    from omnivia_core.contracts.v1.generated import (
        ErrorResponseEnvelope,
        SuccessResponseEnvelope,
    )

    assert ResponseEnvelope == (SuccessResponseEnvelope | ErrorResponseEnvelope)


def test_service_probe_request_round_trip() -> None:
    original = ServiceProbeRequest(probe="service.health", request_id="probe-1")
    wire = original.to_wire()
    assert wire == {"probe": "service.health", "request_id": "probe-1"}
    assert ServiceProbeRequest.from_wire(wire) == original


def test_service_probe_result_round_trip_with_components_and_supported_capabilities() -> None:
    original = ServiceProbeResult(
        probe="service.readiness",
        status="pass",
        server_version="1.2.5",
        api_version="1.2",
        observed_at="2026-07-30T00:00:00Z",
        components=(
            ServiceComponentStatus(id="storage", status="pass", observed_at="2026-07-30T00:00:00Z"),
        ),
        supported_capabilities=(CapabilityRef(id="memory.read", version="1.0"),),
    )
    wire = original.to_wire()
    assert "granted" not in wire
    assert "effective" not in wire
    assert ServiceProbeResult.from_wire(wire) == original


@pytest.mark.parametrize("probe", ["service.health", "service.readiness", "service.discover"])
def test_each_frozen_service_probe_round_trips(probe: str) -> None:
    """The three frozen runtime probe names round-trip through both request and result
    shapes exactly as spelled -- `service.discover`, never the shortened `discovery`."""
    request = ServiceProbeRequest(probe=probe, request_id="probe-1")
    assert ServiceProbeRequest.from_wire(request.to_wire()) == request

    result = ServiceProbeResult(
        probe=probe,
        status="pass",
        server_version="1.2.5",
        api_version="1.2",
        observed_at="2026-07-30T00:00:00Z",
    )
    wire = result.to_wire()
    assert wire["probe"] == probe
    assert ServiceProbeResult.from_wire(wire) == result


def test_probe_kind_known_examples_never_use_shortened_spellings() -> None:
    """The canonical description must name the exact frozen wire values -- `service.health`,
    `service.readiness`, `service.discover` -- never a shortened or unnamespaced spelling
    such as `health`, `readiness`, or `discovery`."""
    document = json.loads((SCHEMA_DIR / "service.schema.json").read_text(encoding="utf-8"))
    description = document["$defs"]["ProbeKind"]["description"]
    for canonical in ("service.health", "service.readiness", "service.discover"):
        assert canonical in description
    assert "discovery" not in description
    assert "`health`" not in description
    assert "`readiness`" not in description


def test_service_probe_request_missing_probe_raises() -> None:
    with pytest.raises(ContractDecodeError, match="missing required field 'probe'"):
        ServiceProbeRequest.from_wire({})


def test_service_probe_result_strict_schema_valid() -> None:
    document = {
        "probe": "service.discover",
        "status": "pass",
        "server_version": "1.2.5",
        "api_version": "1.2",
        "observed_at": "2026-07-30T00:00:00Z",
        "supported_capabilities": [{"id": "memory.read", "version": "1.0"}],
    }
    _assert_schema_valid("service", "ServiceProbeResult", document)


def test_service_probe_result_rejects_legacy_capability_set_shape() -> None:
    """A `granted`/`effective` capability set would fabricate policy authority
    an unauthenticated probe never had; the field no longer exists at all."""
    document = {
        "probe": "service.discover",
        "status": "pass",
        "server_version": "1.2.5",
        "api_version": "1.2",
        "observed_at": "2026-07-30T00:00:00Z",
        "capabilities": {"supported": [], "granted": [], "effective": []},
    }
    _assert_schema_invalid("service", "ServiceProbeResult", document)


# --------------------------------------------------------------------------
# The service endpoint descriptor: the coordination facts a `service.discover`
# probe publishes so a client can find one instance and decide whether it can
# talk to it. Coordination data only -- it must never grow a credential, a
# granted or effective capability, lease ownership, or a storage location, and
# its local process evidence is all-or-none rather than three loose fields.
# --------------------------------------------------------------------------

#: Every field the descriptor publishes. Asserted as an exact set, so adding
#: anything at all -- an authority fact most of all -- fails here and has to
#: arrive as a deliberate contract decision rather than as a quiet widening.
DESCRIPTOR_FIELD_NAMES = {
    "descriptor_version",
    "workspace_id",
    "service_instance_id",
    "installation_id",
    "endpoint_uri",
    "protocol_version",
    "server_version",
    "supported_api_versions",
    "supported_workspace_versions",
    "workspace_format_version",
    "ready",
    "lifecycle_state",
    "fencing_generation",
    "published_at",
    "process",
}

DESCRIPTOR_REQUIRED_FIELD_NAMES = sorted(DESCRIPTOR_FIELD_NAMES - {"process"})

#: Names a descriptor must never carry. A probe that answers before
#: authentication has no caller and no workspace to authorize against, so a
#: descriptor that stated any of these would be fabricating authority it never
#: negotiated -- and a storage location would hand out a way in rather than an
#: address to dial.
FORBIDDEN_DESCRIPTOR_FIELD_NAMES = (
    # Bearer credentials and tokens.
    "token",
    "access_token",
    "auth_token",
    "bearer_token",
    "session_token",
    "credential",
    "credentials",
    "secret",
    "api_key",
    "password",
    # Granted / effective capability authority.
    "granted",
    "effective",
    "capabilities",
    "granted_capabilities",
    "effective_capabilities",
    "capability_set",
    "scopes",
    "roles",
    "principal",
    "authority",
    "permissions",
    # Lease ownership.
    "lease",
    "lease_id",
    "lease_owner",
    "lease_token",
    "owner",
    "owner_id",
    "holder",
    # Database and filesystem locations.
    "path",
    "paths",
    "database_path",
    "db_path",
    "data_dir",
    "data_directory",
    "workspace_path",
    "storage_path",
    "socket_path",
    "directory",
)


def _process_evidence_document() -> dict[str, Any]:
    return {"pid": 4321, "start_time": "132984093840000000", "boot_id": "boot-7f3a"}


def _descriptor_document() -> dict[str, Any]:
    """A fully populated, schema-valid descriptor, minus the optional evidence."""
    return {
        "descriptor_version": "1.0",
        "workspace_id": "workspace-1",
        "service_instance_id": "instance-9c21",
        "installation_id": "installation-4b70",
        "endpoint_uri": "http://127.0.0.1:8731/",
        "protocol_version": "1.2",
        "server_version": "1.2.5",
        "supported_api_versions": {"minimum": "1.0", "maximum": "1.2"},
        "supported_workspace_versions": {"minimum": "1.0", "maximum": "1.1"},
        "workspace_format_version": "1.1",
        "ready": True,
        "lifecycle_state": "serving",
        "fencing_generation": 7,
        "published_at": "2026-07-30T00:00:00Z",
    }


def _descriptor_value(**overrides: Any) -> ServiceEndpointDescriptor:
    """The same descriptor as a decoded value, for round-trip assertions."""
    fields_: dict[str, Any] = {
        "descriptor_version": "1.0",
        "workspace_id": "workspace-1",
        "service_instance_id": "instance-9c21",
        "installation_id": "installation-4b70",
        "endpoint_uri": "http://127.0.0.1:8731/",
        "protocol_version": "1.2",
        "server_version": "1.2.5",
        "supported_api_versions": VersionWindow(minimum="1.0", maximum="1.2"),
        "supported_workspace_versions": VersionWindow(minimum="1.0", maximum="1.1"),
        "workspace_format_version": "1.1",
        "ready": True,
        "lifecycle_state": "serving",
        "fencing_generation": 7,
        "published_at": "2026-07-30T00:00:00Z",
    }
    fields_.update(overrides)
    return ServiceEndpointDescriptor(**fields_)


# --- shape: what a descriptor is, and what it may never become ------------


def test_service_endpoint_descriptor_publishes_exactly_the_accepted_fields() -> None:
    assert {field.name for field in fields(ServiceEndpointDescriptor)} == DESCRIPTOR_FIELD_NAMES


@pytest.mark.parametrize("forbidden", FORBIDDEN_DESCRIPTOR_FIELD_NAMES)
def test_service_endpoint_descriptor_carries_no_authority_or_storage_field(
    forbidden: str,
) -> None:
    """A descriptor is coordination data: an address, a version window, a lifecycle
    state. It never carries a bearer credential, granted or effective capability
    authority, lease ownership, or a database or filesystem location."""
    assert forbidden not in {field.name for field in fields(ServiceEndpointDescriptor)}
    assert forbidden not in {field.name for field in fields(ServiceProcessEvidence)}


@pytest.mark.parametrize(
    "forbidden",
    ["access_token", "granted", "effective", "capabilities", "lease_owner", "database_path"],
)
def test_service_endpoint_descriptor_schema_rejects_an_authority_field(forbidden: str) -> None:
    """The dataclass surface is not the only guard: `unevaluatedProperties: false`
    makes a smuggled authority field a schema violation on the wire too."""
    document = _descriptor_document()
    document[forbidden] = "anything"
    _assert_schema_invalid("service", "ServiceEndpointDescriptor", document)


def test_service_endpoint_descriptor_states_its_exclusions_in_the_canonical_description() -> None:
    """The canonical description is the contract a reviewer reads. It has to say what
    a descriptor never carries, or the exclusion survives only as a test."""
    document = json.loads((SCHEMA_DIR / "service.schema.json").read_text(encoding="utf-8"))
    description = document["$defs"]["ServiceEndpointDescriptor"]["description"]
    for excluded in ("credential", "token", "granted", "effective", "lease", "filesystem"):
        assert excluded in description
    assert "authorizes nothing" in description


def test_service_endpoint_descriptor_does_not_restate_supported_capabilities() -> None:
    """`ServiceProbeResult.supported_capabilities` stays the one place a discovery probe
    says what the build supports, and it stays supported-only."""
    descriptor_field_names = {field.name for field in fields(ServiceEndpointDescriptor)}
    assert "supported_capabilities" not in descriptor_field_names

    result_field_names = {field.name for field in fields(ServiceProbeResult)}
    assert "supported_capabilities" in result_field_names
    assert "capabilities" not in result_field_names
    assert {field.name for field in fields(CapabilityRef)} == {"id", "version"}


# --- the descriptor's optional placement on ServiceProbeResult ------------


def test_service_probe_result_descriptor_is_optional_and_typed() -> None:
    (descriptor_field,) = [
        field for field in fields(ServiceProbeResult) if field.name == "descriptor"
    ]
    assert descriptor_field.default is None
    assert descriptor_field.type == "ServiceEndpointDescriptor | None"


def test_service_probe_result_without_a_descriptor_is_still_valid() -> None:
    """A health or readiness probe answers without one, so the field stays optional."""
    document = {
        "probe": "service.health",
        "status": "pass",
        "server_version": "1.2.5",
        "api_version": "1.2",
        "observed_at": "2026-07-30T00:00:00Z",
    }
    _assert_schema_valid("service", "ServiceProbeResult", document)
    assert "descriptor" not in ServiceProbeResult.from_wire(document).to_wire()


def test_service_discover_result_carrying_a_descriptor_is_valid() -> None:
    document = {
        "probe": "service.discover",
        "status": "pass",
        "server_version": "1.2.5",
        "api_version": "1.2",
        "observed_at": "2026-07-30T00:00:00Z",
        "supported_capabilities": [{"id": "memory.read", "version": "1.0"}],
        "descriptor": _descriptor_document(),
    }
    _assert_schema_valid("service", "ServiceProbeResult", document)


def test_service_probe_result_round_trips_a_descriptor() -> None:
    original = ServiceProbeResult(
        probe="service.discover",
        status="pass",
        server_version="1.2.5",
        api_version="1.2",
        observed_at="2026-07-30T00:00:00Z",
        supported_capabilities=(CapabilityRef(id="memory.read", version="1.0"),),
        descriptor=_descriptor_value(),
    )
    wire = original.to_wire()
    assert wire["descriptor"] == _descriptor_document()
    assert "granted" not in wire["descriptor"]
    assert "effective" not in wire["descriptor"]
    _assert_schema_valid("service", "ServiceProbeResult", wire)
    assert ServiceProbeResult.from_wire(wire) == original


def test_service_probe_result_rejects_a_null_descriptor() -> None:
    """Absent and explicitly null are different: an omitted optional round-trips, a
    null does not, so decoding refuses it rather than silently reading it as absent."""
    document = {
        "probe": "service.discover",
        "status": "pass",
        "server_version": "1.2.5",
        "api_version": "1.2",
        "observed_at": "2026-07-30T00:00:00Z",
        "descriptor": None,
    }
    with pytest.raises(ContractDecodeError, match="descriptor: null is not a valid value"):
        ServiceProbeResult.from_wire(document)
    _assert_schema_invalid("service", "ServiceProbeResult", document)


# --- round trips ----------------------------------------------------------


def test_service_endpoint_descriptor_round_trip_without_process_evidence() -> None:
    original = _descriptor_value()
    wire = original.to_wire()
    assert wire == _descriptor_document()
    assert "process" not in wire
    _assert_schema_valid("service", "ServiceEndpointDescriptor", wire)
    assert ServiceEndpointDescriptor.from_wire(wire) == original


def test_service_endpoint_descriptor_round_trip_with_process_evidence() -> None:
    original = _descriptor_value(
        process=ServiceProcessEvidence(
            pid=4321, start_time="132984093840000000", boot_id="boot-7f3a"
        )
    )
    wire = original.to_wire()
    assert wire["process"] == _process_evidence_document()
    _assert_schema_valid("service", "ServiceEndpointDescriptor", wire)
    assert ServiceEndpointDescriptor.from_wire(wire) == original


def test_service_process_evidence_round_trip() -> None:
    original = ServiceProcessEvidence(
        pid=4321, start_time="132984093840000000", boot_id="boot-7f3a"
    )
    wire = original.to_wire()
    assert wire == _process_evidence_document()
    _assert_schema_valid("service", "ServiceProcessEvidence", wire)
    assert ServiceProcessEvidence.from_wire(wire) == original


# --- required fields ------------------------------------------------------


@pytest.mark.parametrize("omitted", DESCRIPTOR_REQUIRED_FIELD_NAMES)
def test_service_endpoint_descriptor_requires_every_coordination_fact(omitted: str) -> None:
    """Each of these answers a question a client must settle before it sends anything,
    so none of them may be left out and inferred."""
    document = _descriptor_document()
    del document[omitted]
    _assert_schema_invalid("service", "ServiceEndpointDescriptor", document)
    with pytest.raises(ContractDecodeError, match=f"missing required field '{omitted}'"):
        ServiceEndpointDescriptor.from_wire(document)


def test_service_endpoint_descriptor_rejects_an_unknown_field() -> None:
    document = _descriptor_document()
    document["extra_fact"] = "unexpected"
    _assert_schema_invalid("service", "ServiceEndpointDescriptor", document)


# --- process evidence is all-or-none --------------------------------------


def test_process_evidence_is_one_optional_object_not_three_loose_fields() -> None:
    """The three facts only identify a process together, so they are nested behind one
    optional field. Three independently optional top-level fields would let a producer
    publish a pid with no way to tell whether it had been reused."""
    descriptor_field_names = {field.name for field in fields(ServiceEndpointDescriptor)}
    for loose in ("pid", "start_time", "boot_id"):
        assert loose not in descriptor_field_names
    assert {field.name for field in fields(ServiceProcessEvidence)} == {
        "pid",
        "start_time",
        "boot_id",
    }


@pytest.mark.parametrize("omitted", ["pid", "start_time", "boot_id"])
def test_process_evidence_is_rejected_when_partially_present(omitted: str) -> None:
    evidence = _process_evidence_document()
    del evidence[omitted]
    _assert_schema_invalid("service", "ServiceProcessEvidence", evidence)
    with pytest.raises(ContractDecodeError, match=f"missing required field '{omitted}'"):
        ServiceProcessEvidence.from_wire(evidence)

    document = _descriptor_document()
    document["process"] = evidence
    _assert_schema_invalid("service", "ServiceEndpointDescriptor", document)
    with pytest.raises(ContractDecodeError, match=f"missing required field '{omitted}'"):
        ServiceEndpointDescriptor.from_wire(document)


def test_process_evidence_rejects_an_unknown_field() -> None:
    evidence = _process_evidence_document()
    evidence["executable_path"] = "/usr/local/bin/omnivia"
    _assert_schema_invalid("service", "ServiceProcessEvidence", evidence)


# --- bounded and typed values --------------------------------------------


@pytest.mark.parametrize(
    "endpoint_uri",
    [
        "/var/run/omnivia.sock",  # a relative reference, not an absolute URI
        "127.0.0.1:8731",  # a host:port, still no scheme
        "not a uri at all",
        "",  # violates minLength
        "https://example.invalid/" + "a" * 2048,  # violates maxLength
        8731,  # not a string
    ],
)
def test_endpoint_uri_must_be_a_bounded_absolute_uri(endpoint_uri: object) -> None:
    document = _descriptor_document()
    document["endpoint_uri"] = endpoint_uri
    _assert_schema_invalid("service", "ServiceEndpointDescriptor", document)


@pytest.mark.parametrize(
    "endpoint_uri",
    ["http://127.0.0.1:8731/", "https://127.0.0.1:8731/v1", "unix:///run/omnivia/core.sock"],
)
def test_endpoint_uri_accepts_an_absolute_uri_in_any_scheme(endpoint_uri: str) -> None:
    document = _descriptor_document()
    document["endpoint_uri"] = endpoint_uri
    _assert_schema_valid("service", "ServiceEndpointDescriptor", document)


@pytest.mark.parametrize("fencing_generation", [0, -1, 1.5, "7", True])
def test_fencing_generation_must_be_a_positive_integer(fencing_generation: object) -> None:
    document = _descriptor_document()
    document["fencing_generation"] = fencing_generation
    _assert_schema_invalid("service", "ServiceEndpointDescriptor", document)


@pytest.mark.parametrize("pid", [0, -1, 1.5, "4321", True])
def test_process_evidence_pid_must_be_a_positive_integer(pid: object) -> None:
    evidence = _process_evidence_document()
    evidence["pid"] = pid
    _assert_schema_invalid("service", "ServiceProcessEvidence", evidence)


@pytest.mark.parametrize("start_time", ["", "x" * 129, 132984093840000000])
def test_process_evidence_start_time_must_be_a_bounded_non_empty_string(
    start_time: object,
) -> None:
    evidence = _process_evidence_document()
    evidence["start_time"] = start_time
    _assert_schema_invalid("service", "ServiceProcessEvidence", evidence)


@pytest.mark.parametrize("boot_id", ["", "-leading-dash", "x" * 129, 7])
def test_process_evidence_boot_id_must_be_a_bounded_identifier(boot_id: object) -> None:
    evidence = _process_evidence_document()
    evidence["boot_id"] = boot_id
    _assert_schema_invalid("service", "ServiceProcessEvidence", evidence)


@pytest.mark.parametrize(
    "field_name",
    ["descriptor_version", "protocol_version", "workspace_format_version"],
)
@pytest.mark.parametrize("value", ["1", "1.2.5", "v1.2", "01.2", ""])
def test_public_contract_version_fields_reject_a_non_major_minor_value(
    field_name: str, value: str
) -> None:
    document = _descriptor_document()
    document[field_name] = value
    _assert_schema_invalid("service", "ServiceEndpointDescriptor", document)


@pytest.mark.parametrize("server_version", ["1.2", "v1.2.5", "latest", ""])
def test_descriptor_server_version_must_be_a_release_version(server_version: str) -> None:
    document = _descriptor_document()
    document["server_version"] = server_version
    _assert_schema_invalid("service", "ServiceEndpointDescriptor", document)


@pytest.mark.parametrize("lifecycle_state", ["Serving", "service.", "", "draining!", 1])
def test_descriptor_lifecycle_state_must_be_an_open_code(lifecycle_state: object) -> None:
    document = _descriptor_document()
    document["lifecycle_state"] = lifecycle_state
    _assert_schema_invalid("service", "ServiceEndpointDescriptor", document)


def test_descriptor_lifecycle_state_preserves_an_unrecognized_code() -> None:
    """The state is an open code, so a value this build has never seen is valid and
    must survive a round trip rather than being coerced to a known one."""
    document = _descriptor_document()
    document["lifecycle_state"] = "quiescing.for_backup"
    _assert_schema_valid("service", "ServiceEndpointDescriptor", document)
    decoded = ServiceEndpointDescriptor.from_wire(document)
    assert decoded.lifecycle_state == "quiescing.for_backup"
    assert decoded.to_wire()["lifecycle_state"] == "quiescing.for_backup"


@pytest.mark.parametrize("ready", ["true", 1, "yes", None])
def test_descriptor_ready_must_be_a_boolean(ready: object) -> None:
    document = _descriptor_document()
    document["ready"] = ready
    _assert_schema_invalid("service", "ServiceEndpointDescriptor", document)


@pytest.mark.parametrize("published_at", ["2026-07-30", "2026-13-01T00:00:00Z", "yesterday", 0])
def test_descriptor_published_at_must_be_a_timestamp(published_at: object) -> None:
    document = _descriptor_document()
    document["published_at"] = published_at
    _assert_schema_invalid("service", "ServiceEndpointDescriptor", document)


@pytest.mark.parametrize(
    "field_name", ["supported_api_versions", "supported_workspace_versions"]
)
@pytest.mark.parametrize(
    "window",
    [
        {"minimum": "1.0"},  # a window needs both bounds
        {"maximum": "1.2"},
        {"minimum": "1.0", "maximum": "1.2", "preferred": "1.1"},
        "1.0-1.2",
    ],
)
def test_descriptor_version_windows_must_be_complete_version_windows(
    field_name: str, window: object
) -> None:
    document = _descriptor_document()
    document[field_name] = window
    _assert_schema_invalid("service", "ServiceEndpointDescriptor", document)


@pytest.mark.parametrize("field_name", ["workspace_id", "service_instance_id", "installation_id"])
@pytest.mark.parametrize("value", ["", "-leading-dash", "x" * 129, "has space", 7])
def test_descriptor_identifier_fields_must_be_bounded_identifiers(
    field_name: str, value: object
) -> None:
    document = _descriptor_document()
    document[field_name] = value
    _assert_schema_invalid("service", "ServiceEndpointDescriptor", document)


# --------------------------------------------------------------------------
# Records: identity, version, governance layer/state/currentness, evidence
# disposition/source reference/span, provenance, observed/ingested/recorded/
# valid time, and a direction-neutral supersession.
# --------------------------------------------------------------------------


def test_record_identity_requires_governance_state_distinct_from_layer_and_currentness() -> None:
    with pytest.raises(ContractDecodeError, match="missing required field 'governance_state'"):
        RecordIdentity.from_wire(
            {"record_id": "record-1", "version": "v-1", "layer": "l2", "currentness": "current"}
        )


def test_record_identity_round_trip_with_supersession() -> None:
    """A newer record's `supersedes` points at the older record it replaces -- the
    direction the reversed pre-repair `superseded_by` shape got backwards."""
    newer = RecordIdentity(
        record_id="record-1",
        version="v-2",
        layer="l2",
        governance_state="accepted",
        currentness="current",
        supersedes=SupersessionReference(record_id="record-0", version="v-1", reason="corrected"),
    )
    wire = newer.to_wire()
    assert wire["supersedes"] == {"record_id": "record-0", "version": "v-1", "reason": "corrected"}
    assert "superseded_by" not in wire
    assert RecordIdentity.from_wire(wire) == newer

    older = RecordIdentity(
        record_id="record-0",
        version="v-1",
        layer="l2",
        governance_state="accepted",
        currentness="superseded",
        superseded_by=SupersessionReference(record_id="record-1", version="v-2", reason="corrected"),
    )
    older_wire = older.to_wire()
    assert older_wire["superseded_by"] == {"record_id": "record-1", "version": "v-2", "reason": "corrected"}
    assert "supersedes" not in older_wire
    assert RecordIdentity.from_wire(older_wire) == older


def test_supersession_reference_is_direction_neutral() -> None:
    field_names = {field.name for field in fields(SupersessionReference)}
    assert field_names == {"record_id", "version", "reason"}
    assert "superseded_record_id" not in field_names
    assert "superseded_version" not in field_names


@pytest.mark.parametrize("layer", ["l0", "l1", "l2", "l3", "l4"])
def test_each_governance_layer_round_trips(layer: str) -> None:
    """Each layer of the accepted L0-L4 knowledge-governance model -- evidence, candidate
    observations, governed records, context models, organisational model -- round-trips."""
    original = RecordIdentity(
        record_id="record-1",
        version="v-1",
        layer=layer,
        governance_state="accepted",
        currentness="current",
    )
    wire = original.to_wire()
    assert wire["layer"] == layer
    assert RecordIdentity.from_wire(wire) == original


def test_governance_layer_unknown_future_value_is_preserved() -> None:
    """`GovernanceLayer` is open by design: a layer beyond the frozen L0-L4 model must be
    preserved, not coerced to a known one."""
    wire = {
        "record_id": "record-1",
        "version": "v-1",
        "layer": "l5.future_layer",
        "governance_state": "accepted",
        "currentness": "current",
    }
    decoded = RecordIdentity.from_wire(wire)
    assert decoded.layer == "l5.future_layer"
    assert decoded.to_wire()["layer"] == "l5.future_layer"


def test_record_provenance_requires_evidence_disposition_and_sources() -> None:
    identity = RecordIdentity(
        record_id="record-1", version="v-1", layer="l1", governance_state="accepted", currentness="current"
    )
    temporal = RecordTemporalMetadata(ingested_at="2026-07-30T00:00:00Z", recorded_at="2026-07-30T00:00:01Z")
    with pytest.raises(ContractDecodeError, match="missing required field 'evidence_disposition'"):
        RecordProvenance.from_wire(
            {"identity": identity.to_wire(), "temporal": temporal.to_wire(), "history": []}
        )
    with pytest.raises(ContractDecodeError, match="missing required field 'sources'"):
        RecordProvenance.from_wire(
            {
                "identity": identity.to_wire(),
                "temporal": temporal.to_wire(),
                "history": [],
                "evidence_disposition": "available",
            }
        )


def test_record_provenance_may_have_empty_sources_when_evidence_is_unavailable() -> None:
    original = RecordProvenance(
        identity=RecordIdentity(
            record_id="record-1",
            version="v-1",
            layer="l1",
            governance_state="proposed",
            currentness="current",
        ),
        temporal=RecordTemporalMetadata(ingested_at="2026-07-30T00:00:00Z", recorded_at="2026-07-30T00:00:01Z"),
        history=(),
        evidence_disposition="unavailable",
        sources=(),
    )
    wire = original.to_wire()
    assert RecordProvenance.from_wire(wire) == original


def test_record_provenance_round_trip_with_evidence_span() -> None:
    original = RecordProvenance(
        identity=RecordIdentity(
            record_id="record-1", version="v-1", layer="l1", governance_state="accepted", currentness="current"
        ),
        temporal=RecordTemporalMetadata(
            ingested_at="2026-07-30T00:00:00Z",
            recorded_at="2026-07-30T00:00:01Z",
            observed_at="2026-07-29T12:00:00Z",
        ),
        history=(
            ProvenanceEntry(
                actor_id="user-1",
                actor_kind="user",
                action="created",
                occurred_at="2026-07-30T00:00:01Z",
                evidence=(
                    EvidenceReference(
                        source=SourceReference(kind="document", source_id="doc-1"),
                        span=SourceSpan(pointer="/body", start_offset=0, end_offset=19),
                        excerpt="the quick brown fox",
                    ),
                ),
            ),
        ),
        evidence_disposition="available",
        sources=(SourceReference(kind="document", source_id="doc-1"),),
    )
    wire = original.to_wire()
    assert RecordProvenance.from_wire(wire) == original


def test_record_temporal_metadata_requires_ingested_and_recorded() -> None:
    with pytest.raises(ContractDecodeError, match="missing required field 'recorded_at'"):
        RecordTemporalMetadata.from_wire({"ingested_at": "2026-07-30T00:00:00Z"})


def test_record_identity_strict_schema_rejects_missing_governance_state() -> None:
    document = {"record_id": "record-1", "version": "v-1", "layer": "l2", "currentness": "current"}
    _assert_schema_invalid("records", "RecordIdentity", document)


def test_record_provenance_strict_schema_rejects_empty_sources_without_disposition_field() -> None:
    """Schema-wise, `sources` may always be an empty array; whether that agrees with
    `evidence_disposition` is deferred to semantic validation, not the wire shape."""
    document = {
        "identity": {
            "record_id": "record-1",
            "version": "v-1",
            "layer": "l1",
            "governance_state": "proposed",
            "currentness": "current",
        },
        "temporal": {"ingested_at": "2026-07-30T00:00:00Z", "recorded_at": "2026-07-30T00:00:01Z"},
        "history": [],
        "evidence_disposition": "unavailable",
        "sources": [],
    }
    _assert_schema_valid("records", "RecordProvenance", document)


def test_governance_state_unknown_value_is_preserved() -> None:
    wire = {
        "record_id": "record-1",
        "version": "v-1",
        "layer": "l2",
        "governance_state": "future_state.not_yet_known",
        "currentness": "current",
    }
    decoded = RecordIdentity.from_wire(wire)
    assert decoded.governance_state == "future_state.not_yet_known"
    assert decoded.to_wire()["governance_state"] == "future_state.not_yet_known"


def test_evidence_disposition_unknown_value_is_preserved() -> None:
    identity = RecordIdentity(
        record_id="record-1", version="v-1", layer="l1", governance_state="accepted", currentness="current"
    )
    temporal = RecordTemporalMetadata(ingested_at="2026-07-30T00:00:00Z", recorded_at="2026-07-30T00:00:01Z")
    wire = {
        "identity": identity.to_wire(),
        "temporal": temporal.to_wire(),
        "history": [],
        "evidence_disposition": "future_disposition.unseen",
        "sources": [],
    }
    decoded = RecordProvenance.from_wire(wire)
    assert decoded.evidence_disposition == "future_disposition.unseen"


# --------------------------------------------------------------------------
# Jobs: identity with audit linkage/originating operation, control
# dispositions, progress unit, attempt, event, handle, and a mutually
# exclusive terminal result (success xor failure xor cancellation).
# --------------------------------------------------------------------------


def test_job_identity_requires_audit_linkage_and_originating_operation() -> None:
    with pytest.raises(ContractDecodeError, match="missing required field 'originating_operation'"):
        JobIdentity.from_wire({"job_id": "job-1", "job_kind": "ingestion.import"})
    with pytest.raises(ContractDecodeError, match="missing required field 'audit_reference'"):
        JobIdentity.from_wire(
            {"job_id": "job-1", "job_kind": "ingestion.import", "originating_operation": "import.start"}
        )


def _job_identity() -> JobIdentity:
    return JobIdentity(
        job_id="job-1",
        job_kind="ingestion.import",
        originating_operation="import.start",
        audit_reference="audit-1",
        workspace_id="workspace-1",
    )


def test_job_handle_round_trip_with_control() -> None:
    original = JobHandle(
        identity=_job_identity(),
        state="running",
        created_at="2026-07-30T00:00:00Z",
        updated_at="2026-07-30T00:00:05Z",
        control=JobControl(cancellation="cancellable", recovery="not_retryable"),
        progress=JobProgress(unit="item", completed_units=3, total_units=10),
        latest_attempt=JobAttempt(attempt_number=1, started_at="2026-07-30T00:00:00Z", state="running"),
    )
    wire = original.to_wire()
    assert JobHandle.from_wire(wire) == original


def test_job_handle_requires_control() -> None:
    identity_wire = _job_identity().to_wire()
    with pytest.raises(ContractDecodeError, match="missing required field 'control'"):
        JobHandle.from_wire(
            {
                "identity": identity_wire,
                "state": "running",
                "created_at": "2026-07-30T00:00:00Z",
                "updated_at": "2026-07-30T00:00:05Z",
            }
        )


def test_job_control_carries_no_scheduler_worker_lease_or_persistence_fields() -> None:
    field_names = {field.name for field in fields(JobControl)}
    assert field_names == {"cancellation", "recovery"}
    forbidden_substrings = ("scheduler", "worker", "lease", "persistence", "queue")
    for name in field_names:
        assert not any(substring in name for substring in forbidden_substrings)


def test_job_progress_requires_unit() -> None:
    with pytest.raises(ContractDecodeError, match="missing required field 'unit'"):
        JobProgress.from_wire({"completed_units": 3})


def test_job_progress_unit_unknown_value_is_preserved() -> None:
    decoded = JobProgress.from_wire({"unit": "future_unit.not_yet_known", "completed_units": 0})
    assert decoded.unit == "future_unit.not_yet_known"


@pytest.mark.parametrize("field_name", ["cancellation", "recovery"])
def test_job_control_disposition_unknown_value_is_preserved(field_name: str) -> None:
    wire = {"cancellation": "cancellable", "recovery": "retryable"}
    wire[field_name] = "future_disposition.not_yet_known"
    decoded = JobControl.from_wire(wire)
    assert getattr(decoded, field_name) == "future_disposition.not_yet_known"


def test_job_terminal_result_success_round_trip() -> None:
    original: JobTerminalResult = JobTerminalSuccess(
        identity=_job_identity(),
        state="succeeded",
        finished_at="2026-07-30T00:01:00Z",
        attempts=(JobAttempt(attempt_number=1, started_at="2026-07-30T00:00:00Z", state="succeeded"),),
        result_kind="future_kind.not_yet_known",
        result={"imported": 3},
    )
    wire = job_terminal_result_to_wire(original)
    assert "error" not in wire
    assert "cancellation" not in wire
    assert job_terminal_result_from_wire(wire) == original


def test_job_terminal_result_failure_round_trip() -> None:
    original: JobTerminalResult = JobTerminalFailure(
        identity=_job_identity(),
        state="failed",
        finished_at="2026-07-30T00:01:00Z",
        attempts=(
            JobAttempt(
                attempt_number=1,
                started_at="2026-07-30T00:00:00Z",
                finished_at="2026-07-30T00:01:00Z",
                state="failed",
                error=ApiError(code="internal_recoverable", message="transient", retry_class="retryable"),
            ),
        ),
        error=ApiError(code="internal_recoverable", message="transient", retry_class="retryable"),
    )
    wire = job_terminal_result_to_wire(original)
    assert "result" not in wire
    assert "cancellation" not in wire
    assert job_terminal_result_from_wire(wire) == original


def test_job_terminal_result_cancellation_round_trip() -> None:
    original: JobTerminalResult = JobTerminalCancellation(
        identity=_job_identity(),
        state="cancelled",
        finished_at="2026-07-30T00:01:00Z",
        attempts=(),
        cancellation=JobCancellationOutcome(reason="caller_requested"),
    )
    wire = job_terminal_result_to_wire(original)
    assert "result" not in wire
    assert "error" not in wire
    assert job_terminal_result_from_wire(wire) == original


def test_job_terminal_result_rejects_both_result_and_error() -> None:
    document = {
        "identity": _job_identity().to_wire(),
        "state": "succeeded",
        "finished_at": "2026-07-30T00:01:00Z",
        "attempts": [],
        "result": {},
        "error": {"code": "internal_recoverable", "message": "x", "retry_class": "retryable"},
    }
    with pytest.raises(ContractDecodeError, match="expected exactly one of"):
        job_terminal_result_from_wire(document)
    _assert_schema_invalid("jobs", "JobTerminalResult", document)


def test_job_terminal_result_rejects_neither_result_nor_error_nor_cancellation() -> None:
    document = {
        "identity": _job_identity().to_wire(),
        "state": "succeeded",
        "finished_at": "2026-07-30T00:01:00Z",
        "attempts": [],
    }
    with pytest.raises(ContractDecodeError, match="expected exactly one of"):
        job_terminal_result_from_wire(document)
    _assert_schema_invalid("jobs", "JobTerminalResult", document)


@pytest.mark.parametrize(
    ("discriminator", "extra"),
    [
        ("result", {"result_kind": "future_kind.not_yet_known", "result": {}}),
        ("error", {"error": {"code": "internal_recoverable", "message": "x", "retry_class": "retryable"}}),
        ("cancellation", {"cancellation": {"reason": "caller_requested"}}),
    ],
)
def test_job_terminal_result_strict_schema_accepts_exactly_one_branch(
    discriminator: str, extra: dict[str, Any]
) -> None:
    document = {
        "identity": _job_identity().to_wire(),
        "state": "succeeded" if discriminator == "result" else discriminator,
        "finished_at": "2026-07-30T00:01:00Z",
        "attempts": [],
        **extra,
    }
    _assert_schema_valid("jobs", "JobTerminalResult", document)


def test_job_event_round_trip() -> None:
    original = JobEvent(sequence=0, occurred_at="2026-07-30T00:00:00Z", state="queued")
    wire = original.to_wire()
    assert JobEvent.from_wire(wire) == original


# --------------------------------------------------------------------------
# Operations: scope kind (not a workspace_scoped boolean), completion mode
# independent of side effect, and a catalogue-complete metadata shape.
# --------------------------------------------------------------------------


def test_operation_scope_has_scope_kind_not_workspace_scoped_boolean() -> None:
    field_names = {field.name for field in fields(OperationScope)}
    assert "scope_kind" in field_names
    assert "workspace_scoped" not in field_names


def test_operation_job_metadata_has_completion_mode_not_may_return_job_boolean() -> None:
    field_names = {field.name for field in fields(OperationJobMetadata)}
    assert "completion_mode" in field_names
    assert "may_return_job" not in field_names


def _operation_metadata() -> OperationMetadata:
    return OperationMetadata(
        name="memory.get",
        scope=OperationScope(required_scopes=("memory:read",), side_effect="none", scope_kind="workspace"),
        input_schema_ref="https://contracts.omnivia.dev/application/v1/operations/memory-get-input.schema.json",
        result_schema_ref="https://contracts.omnivia.dev/application/v1/operations/memory-get-result.schema.json",
        required_capability=CapabilityRequirement(id="memory.read", minimum_version="1.0", required=True),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=False),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=False, required=False, safe_to_retry=True
        ),
        precondition=OperationPreconditionMetadata(supports_mutation_precondition=False, required=False),
        audit=OperationAuditMetadata(audited=True, audit_category="read"),
        allowed_errors=("not_found", "authorization_denied"),
    )


def test_operation_metadata_round_trip() -> None:
    original = _operation_metadata()
    wire = original.to_wire()
    assert OperationMetadata.from_wire(wire) == original


def test_import_start_is_representable_as_a_mutation_plus_durable_job() -> None:
    """The completion mode is independent of the side effect: `import.start` mutates
    state (`side_effect`) and always starts a durable job (`completion_mode`) at once."""
    original = OperationMetadata(
        name="import.start",
        scope=OperationScope(required_scopes=("memory:write",), side_effect="create", scope_kind="workspace"),
        input_schema_ref="https://contracts.omnivia.dev/application/v1/operations/import-start-input.schema.json",
        result_schema_ref="https://contracts.omnivia.dev/application/v1/operations/import-start-result.schema.json",
        required_capability=CapabilityRequirement(id="ingestion.import", minimum_version="1.0", required=True),
        job=OperationJobMetadata(completion_mode="always_returns_job", job_kind="ingestion.import"),
        pagination=OperationPaginationMetadata(paginated=False),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=True, required=True, safe_to_retry=False
        ),
        precondition=OperationPreconditionMetadata(supports_mutation_precondition=False, required=False),
        audit=OperationAuditMetadata(audited=True, audit_category="mutation"),
        allowed_errors=("workspace_not_granted",),
    )
    wire = original.to_wire()
    assert wire["scope"]["side_effect"] == "create"
    assert wire["job"]["completion_mode"] == "always_returns_job"
    assert OperationMetadata.from_wire(wire) == original


def test_operation_metadata_catalogue_fields_are_all_required() -> None:
    required_field_names = {
        "name",
        "scope",
        "input_schema_ref",
        "result_schema_ref",
        "required_capability",
        "job",
        "pagination",
        "idempotency",
        "precondition",
        "audit",
        "allowed_errors",
    }
    for field_name in required_field_names:
        with pytest.raises(ContractDecodeError, match=f"missing required field '{field_name}'"):
            wire = _operation_metadata().to_wire()
            del wire[field_name]
            OperationMetadata.from_wire(wire)


def test_operation_scope_kind_unknown_value_is_preserved() -> None:
    decoded = OperationScope.from_wire(
        {"required_scopes": [], "side_effect": "none", "scope_kind": "future_scope.not_yet_known"}
    )
    assert decoded.scope_kind == "future_scope.not_yet_known"


def test_operation_completion_mode_unknown_value_is_preserved() -> None:
    decoded = OperationJobMetadata.from_wire({"completion_mode": "future_mode.not_yet_known"})
    assert decoded.completion_mode == "future_mode.not_yet_known"


# --------------------------------------------------------------------------
# Compatibility matrix: qualification state, component/release identity with
# both API and workspace-format support windows, and capability entries.
# --------------------------------------------------------------------------


def _release_entry() -> ReleaseCompatibilityEntry:
    from omnivia_core.contracts.v1.generated import VersionWindow

    return ReleaseCompatibilityEntry(
        component="core",
        release_version="1.2.5",
        api_version="1.2",
        supported_api_versions=VersionWindow(minimum="1.0", maximum="1.3"),
        supported_workspace_versions=VersionWindow(minimum="1.0", maximum="1.0"),
        qualification_state="qualified",
    )


def _operation_compatibility_entry() -> OperationCompatibilityEntry:
    return OperationCompatibilityEntry(
        operation="memory.get", introduced_in="1.0", state="stable", qualification_state="qualified"
    )


def test_compatibility_matrix_round_trip() -> None:
    original = CompatibilityMatrix(
        releases=(_release_entry(),),
        operations=(_operation_compatibility_entry(),),
        capabilities=(
            CapabilityCompatibilityEntry(
                capability=CapabilityRef(id="memory.read", version="1.0"),
                introduced_in="1.0",
                qualification_state="qualified",
            ),
        ),
    )
    wire = original.to_wire()
    assert wire["operations"][0]["qualification_state"] == "qualified"
    assert CompatibilityMatrix.from_wire(wire) == original


def test_operation_compatibility_entry_requires_state_and_qualification_state_separately() -> None:
    """`state` (lifecycle) and `qualification_state` (verification depth) are distinct
    required axes: an operation's mere presence, or a `stable` lifecycle state, must never
    stand in for qualification evidence."""
    with pytest.raises(ContractDecodeError, match="missing required field 'qualification_state'"):
        OperationCompatibilityEntry.from_wire(
            {"operation": "memory.get", "introduced_in": "1.0", "state": "stable"}
        )
    with pytest.raises(ContractDecodeError, match="missing required field 'state'"):
        OperationCompatibilityEntry.from_wire(
            {"operation": "memory.get", "introduced_in": "1.0", "qualification_state": "qualified"}
        )


def test_operation_compatibility_entry_strict_schema_rejects_missing_qualification_state() -> None:
    document = _operation_compatibility_entry().to_wire()
    del document["qualification_state"]
    _assert_schema_invalid("compatibility-matrix", "OperationCompatibilityEntry", document)


def test_operation_compatibility_entry_strict_schema_rejects_missing_state() -> None:
    document = _operation_compatibility_entry().to_wire()
    del document["state"]
    _assert_schema_invalid("compatibility-matrix", "OperationCompatibilityEntry", document)


def test_compatibility_matrix_requires_capabilities() -> None:
    with pytest.raises(ContractDecodeError, match="missing required field 'capabilities'"):
        CompatibilityMatrix.from_wire({"releases": [], "operations": []})


def test_release_compatibility_entry_requires_component_and_workspace_window() -> None:
    field_names = {field.name for field in fields(ReleaseCompatibilityEntry)}
    assert {"component", "supported_workspace_versions", "qualification_state"} <= field_names


def test_component_kind_unknown_value_is_preserved() -> None:
    entry = _release_entry()
    wire = entry.to_wire()
    wire["component"] = "future_component.not_yet_known"
    decoded = ReleaseCompatibilityEntry.from_wire(wire)
    assert decoded.component == "future_component.not_yet_known"


def test_qualification_state_unknown_value_is_preserved() -> None:
    entry = _release_entry()
    wire = entry.to_wire()
    wire["qualification_state"] = "future_state.not_yet_known"
    decoded = ReleaseCompatibilityEntry.from_wire(wire)
    assert decoded.qualification_state == "future_state.not_yet_known"


def test_compatibility_matrix_strict_schema_valid() -> None:
    document = {
        "releases": [_release_entry().to_wire()],
        "operations": [_operation_compatibility_entry().to_wire()],
        "capabilities": [
            {
                "capability": {"id": "memory.read", "version": "1.0"},
                "introduced_in": "1.0",
                "qualification_state": "qualified",
            }
        ],
    }
    _assert_schema_valid("compatibility-matrix", "CompatibilityMatrix", document)


def test_compatibility_matrix_strict_schema_rejects_missing_capabilities() -> None:
    document = {
        "releases": [_release_entry().to_wire()],
        "operations": [],
    }
    _assert_schema_invalid("compatibility-matrix", "CompatibilityMatrix", document)


def test_release_compatibility_entry_strict_schema_rejects_missing_qualification_state() -> None:
    document = _release_entry().to_wire()
    del document["qualification_state"]
    _assert_schema_invalid("compatibility-matrix", "ReleaseCompatibilityEntry", document)
