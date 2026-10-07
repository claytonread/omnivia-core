"""Engineering Memory captured-source application contract.

`EngineeringSourceCaptureCommitInput`/`Result` are the accepted
`engineering.source.capture.commit` mutation's wire shapes (SPEC-CORE-ENGMEM-001,
plan P0-04; spec §6.3, §15).
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from omnivia_core.contracts.v1.generated import (
    OPERATION_CATALOGUE,
    EngineeringSourceCaptureCommitInput,
    EngineeringSourceCaptureCommitResult,
    EngineeringSourcePredecessor,
    EngineeringSourceStreamCoverage,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = REPO_ROOT / "contracts" / "application" / "v1" / "schemas"
BASE_URI = "https://contracts.omnivia.dev/application/v1/"

_SCHEMAS = ("common", "jobs", "engineering")


def _registry() -> Registry:
    entries: list[tuple[str, Resource[Any]]] = []
    for name in _SCHEMAS:
        document = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
        resource = Resource.from_contents(document)
        resource_id = resource.id()
        assert resource_id is not None
        entries.append((resource_id, resource))
    return Registry().with_resources(entries)


REGISTRY = _registry()


def _validator(def_name: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": f"{BASE_URI}engineering.schema.json#/$defs/{def_name}"},
        registry=REGISTRY,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def _valid(def_name: str, document: Any) -> None:
    errors = list(_validator(def_name).iter_errors(document))
    assert not errors, f"expected {def_name} to be schema-valid, found {errors}"


def _invalid(def_name: str, document: Any) -> None:
    errors = list(_validator(def_name).iter_errors(document))
    assert errors, f"expected {def_name} to be schema-invalid, found none"


_DIGEST = "sha256:" + "a" * 64


def _minimal_input(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "repository_id": "repo-1",
        "stream_id": "stream-1",
        "sequence": 1,
        "snapshot_id": "snap-1",
    }
    document.update(overrides)
    return document


def _result(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "repository_id": "repo-1",
        "stream_id": "stream-1",
        "sequence": 2,
        "snapshot_id": "snap-2",
        "rich_manifest_digest": _DIGEST,
        "coverage_digest": _DIGEST,
        "capture_status": "complete",
        "file_count": 3,
        "disposition": "recorded",
        "coverage": {"state": "current", "covered_sequence": 2, "announced_sequence": 2},
        "recorded_at": "2026-01-01T00:00:00.000000Z",
        "audit_reference": "audit-1",
    }
    document.update(overrides)
    return document


# --- registry publication and generation -----------------------------------------


def test_the_registry_publishes_both_definitions() -> None:
    document = json.loads(
        (SCHEMA_DIR / "application-v1.schema.json").read_text(encoding="utf-8")
    )
    for name in (
        "EngineeringSourceCaptureCommitInput",
        "EngineeringSourceCaptureCommitResult",
    ):
        assert document["$defs"][name] == {
            "$ref": f"{BASE_URI}engineering.schema.json#/$defs/{name}"
        }


def test_both_definitions_are_generated_in_python() -> None:
    assert {f.name for f in fields(EngineeringSourceCaptureCommitInput)} == {
        "repository_id",
        "stream_id",
        "sequence",
        "predecessor",
        "snapshot_id",
        "expected_manifest_digest",
    }
    assert {f.name for f in fields(EngineeringSourceCaptureCommitResult)} == {
        "repository_id",
        "stream_id",
        "sequence",
        "snapshot_id",
        "rich_manifest_digest",
        "coverage_digest",
        "capture_status",
        "file_count",
        "disposition",
        "coverage",
        "recorded_at",
        "audit_reference",
    }


def test_both_definitions_are_generated_in_typescript() -> None:
    text = (
        REPO_ROOT / "generated" / "typescript" / "application" / "v1" / "index.ts"
    ).read_text(encoding="utf-8")
    assert "interface EngineeringSourceCaptureCommitInput" in text
    assert "interface EngineeringSourceCaptureCommitResult" in text


# --- round trips -------------------------------------------------------------------


def test_input_round_trips_with_a_minimal_valid_example() -> None:
    document = _minimal_input()
    _valid("EngineeringSourceCaptureCommitInput", document)
    value = EngineeringSourceCaptureCommitInput.from_wire(document)
    assert value == EngineeringSourceCaptureCommitInput(
        repository_id="repo-1", stream_id="stream-1", sequence=1, snapshot_id="snap-1"
    )
    assert value.to_wire() == document


def test_input_round_trips_with_every_optional_field_present() -> None:
    document = _minimal_input(
        sequence=2,
        predecessor={"sequence": 1, "snapshot_id": "snap-1"},
        expected_manifest_digest=_DIGEST,
    )
    _valid("EngineeringSourceCaptureCommitInput", document)
    value = EngineeringSourceCaptureCommitInput.from_wire(document)
    assert value == EngineeringSourceCaptureCommitInput(
        repository_id="repo-1",
        stream_id="stream-1",
        sequence=2,
        snapshot_id="snap-1",
        predecessor=EngineeringSourcePredecessor(sequence=1, snapshot_id="snap-1"),
        expected_manifest_digest=_DIGEST,
    )
    assert value.to_wire() == document


def test_result_round_trips_with_a_valid_example() -> None:
    document = _result()
    _valid("EngineeringSourceCaptureCommitResult", document)
    value = EngineeringSourceCaptureCommitResult.from_wire(document)
    assert value == EngineeringSourceCaptureCommitResult(
        repository_id="repo-1",
        stream_id="stream-1",
        sequence=2,
        snapshot_id="snap-2",
        rich_manifest_digest=_DIGEST,
        coverage_digest=_DIGEST,
        capture_status="complete",
        file_count=3,
        disposition="recorded",
        coverage=EngineeringSourceStreamCoverage(
            state="current", covered_sequence=2, announced_sequence=2
        ),
        recorded_at="2026-01-01T00:00:00.000000Z",
        audit_reference="audit-1",
    )
    assert value.to_wire() == document


# --- no path, manifest body, content, installation, principal, workspace, --------
# --- authority or caller-owned capture facts -------------------------------------


def test_the_input_carries_no_path_manifest_body_or_authority_field() -> None:
    field_names = {f.name for f in fields(EngineeringSourceCaptureCommitInput)}
    forbidden = {
        "checkout_path",
        "repository_path",
        "path",
        "manifest",
        "manifest_json",
        "content",
        "bytes",
        "command",
        "checkout_id",
        "installation_id",
        "workspace_id",
        "principal_id",
        "purpose",
        "scope",
        "role",
        "capability",
        "capture_status",
        "file_count",
        "coverage_digest",
        "audit_reference",
        "audit_ref",
    }
    assert field_names.isdisjoint(forbidden)


def test_the_input_refuses_every_forbidden_field() -> None:
    for key, value in (
        ("checkout_path", "/tmp/repo"),
        ("repository_path", "/tmp/repo"),
        ("manifest", []),
        ("manifest_json", "{}"),
        ("content", "raw bytes"),
        ("command", "git status"),
        ("checkout_id", "checkout-1"),
        ("installation_id", "install-1"),
        ("workspace_id", "workspace-1"),
        ("principal_id", "principal-1"),
        ("purpose", "read"),
        ("scope", "engineering:source"),
        ("role", "contributor"),
        ("capability", "engineering.source"),
        ("capture_status", "complete"),
        ("file_count", 3),
        ("coverage_digest", _DIGEST),
        ("audit_reference", "audit-1"),
    ):
        _invalid("EngineeringSourceCaptureCommitInput", _minimal_input(**{key: value}))


def test_the_result_exposes_no_local_path_checkout_hint_file_list_or_raw_manifest() -> None:
    field_names = {f.name for f in fields(EngineeringSourceCaptureCommitResult)}
    forbidden = {
        "checkout_path",
        "checkout_hint",
        "checkout_id",
        "path",
        "files",
        "manifest",
        "manifest_json",
    }
    assert field_names.isdisjoint(forbidden)


# --- required fields and numeric bounds --------------------------------------------


def test_input_requires_repository_stream_sequence_and_snapshot() -> None:
    for key in ("repository_id", "stream_id", "sequence", "snapshot_id"):
        document = _minimal_input()
        del document[key]
        _invalid("EngineeringSourceCaptureCommitInput", document)


def test_input_sequence_bounds() -> None:
    _valid("EngineeringSourceCaptureCommitInput", _minimal_input(sequence=1))
    _valid(
        "EngineeringSourceCaptureCommitInput", _minimal_input(sequence=2147483647)
    )
    _invalid("EngineeringSourceCaptureCommitInput", _minimal_input(sequence=0))
    _invalid(
        "EngineeringSourceCaptureCommitInput", _minimal_input(sequence=2147483648)
    )


def test_input_unknown_keys_are_refused() -> None:
    _invalid(
        "EngineeringSourceCaptureCommitInput",
        _minimal_input(unexpected_future_field="value"),
    )


def test_result_requires_every_field() -> None:
    for key in _result():
        document = _result()
        del document[key]
        _invalid("EngineeringSourceCaptureCommitResult", document)


def test_result_file_count_bounds() -> None:
    _valid("EngineeringSourceCaptureCommitResult", _result(file_count=0))
    _valid("EngineeringSourceCaptureCommitResult", _result(file_count=10000))
    _invalid("EngineeringSourceCaptureCommitResult", _result(file_count=-1))
    _invalid("EngineeringSourceCaptureCommitResult", _result(file_count=10001))


def test_result_sequence_minimum() -> None:
    _valid("EngineeringSourceCaptureCommitResult", _result(sequence=1))
    _invalid("EngineeringSourceCaptureCommitResult", _result(sequence=0))


def test_result_unknown_keys_are_refused() -> None:
    _invalid(
        "EngineeringSourceCaptureCommitResult",
        _result(unexpected_future_field="value"),
    )


# --- accepted operation metadata ---------------------------------------------------


def test_the_operation_catalogue_accepts_capture_commit_as_entry_53() -> None:
    assert len(OPERATION_CATALOGUE) == 69
    entry = next(
        item
        for item in OPERATION_CATALOGUE
        if item.name == "engineering.source.capture.commit"
    )
    assert entry.scope.scope_kind == "workspace"
    assert entry.scope.side_effect == "create"
    assert entry.scope.required_scopes == ("engineering:source",)
    assert entry.required_capability.id == "engineering.source"
    assert entry.required_capability.minimum_version == "1.0"
    assert entry.job.completion_mode == "synchronous"
    assert not entry.pagination.paginated
    assert entry.idempotency.required
    assert not entry.precondition.supports_mutation_precondition
    assert entry.audit.audited
    assert {
        "invalid_request",
        "not_found",
        "authorization_denied",
        "conflict",
        "mutation_precondition_failed",
        "size_limit_exceeded",
        "idempotency_conflict",
    } <= set(entry.allowed_errors)
