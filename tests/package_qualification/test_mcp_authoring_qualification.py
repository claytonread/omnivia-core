"""Guards for the installed-wheel MCP authoring qualification record."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
JOURNEY = REPO_ROOT / "scripts" / "run-mcp-authoring-qualification.py"
BUILDER = REPO_ROOT / "scripts" / "build-standard-candidate.py"
SCHEMA = (
    REPO_ROOT
    / "docs"
    / "distribution"
    / "schemas"
    / "mcp-authoring-qualification-record-v1.schema.json"
)
RETAINED_RECORD = (
    REPO_ROOT
    / "docs"
    / "development"
    / "qualification"
    / "mcp-authoring-installed-wheel-qualification-2026-10-03.json"
)


def _module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _record() -> dict[str, object]:
    return {
        "format": "omnivia.mcp-authoring-qualification.v1",
        "verdict": "pass",
        "profile": "authoring",
        "protocol_version": "2025-06-18",
        "tool_count": 18,
        "tools": [
            "workspace_inspect",
            "evidence_search",
            "knowledge_search",
            "memory_search",
            "graph_traverse",
            "context_pack_build",
            "engineering_search",
            "engineering_expand",
            "engineering_context_build",
            "decision_evaluate",
            "decision_record_get",
            "decision_record_list",
            "decision_status",
            "memory_create",
            "evidence_capture",
            "import_start",
            "job_get",
            "job_events",
        ],
        "sdk_versions": {"mcp": "2.0.0", "mcp-types": "2.0.0"},
        "environment": {
            "system": "darwin",
            "machine": "arm64",
            "release": "26.0.0",
            "python": "3.11.9",
        },
        "journeys": {
            "empty_workspace": {
                "empty_workspace": True,
                "tool_discovery": True,
                "capture_and_search": True,
                "proposed_memory": True,
                "candidate_visibility": True,
                "replay_and_conflict": True,
                "core_restart_recovery": True,
                "revocation_fail_closed": True,
                "service_healthy": True,
            },
            "import": {
                "trusted_staging": True,
                "import_start": True,
                "job_observation": True,
                "import_replay_and_conflict": True,
                "revocation_preserved_job": True,
                "service_healthy": True,
            },
        },
        "redaction": {
            "credentials_recorded": False,
            "private_paths_recorded": False,
            "private_identifiers_recorded": False,
            "submitted_content_recorded": False,
            "prompts_or_transcripts_recorded": False,
            "endpoints_or_processes_recorded": False,
            "stdio_recorded": False,
            "model_responses_recorded": False,
        },
    }


def test_journey_imports_no_omnivia_package_and_uses_only_installed_entry_points() -> None:
    tree = ast.parse(JOURNEY.read_text(encoding="utf-8"), filename=str(JOURNEY))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any(name.startswith("omnivia") for name in imports)
    constants = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {"omnivia-core-service", "omnivia", "omnivia-core-mcp"} <= constants


def test_journey_names_every_required_authoring_and_recovery_step() -> None:
    source = JOURNEY.read_text(encoding="utf-8")
    for token in (
        "evidence_capture",
        "evidence_search",
        "memory_create",
        "view\": \"candidates",
        "import_start",
        "job_get",
        "job_events",
        "idempotency_conflict",
        "MCP authoring revoke",
        "managed-local restart",
    ):
        assert token in source


def test_closed_schema_and_builder_accept_the_exact_redacted_record() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(schema["required"])
    builder = _module(BUILDER, "build_standard_candidate_authoring_test")
    assert builder._require_authoring_qualification(_record()) == _record()


def test_retained_installed_record_is_the_closed_schema_valid_result() -> None:
    builder = _module(BUILDER, "build_standard_candidate_retained_authoring")
    retained = json.loads(RETAINED_RECORD.read_text(encoding="utf-8"))

    assert builder._require_authoring_qualification(retained) == retained


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ((), {"workspace_id": "private"}),
        (("redaction",), {"credentials_recorded": True}),
        (("sdk_versions",), {"mcp": "2.2.0"}),
        (("environment",), {"workspace_path": "/Users/private/workspace"}),
        (("journeys", "empty_workspace"), {"model_response": "pass"}),
    ],
)
def test_builder_refuses_extra_sensitive_fields_and_false_redaction_claims(
    path: tuple[str, ...], value: dict[str, object]
) -> None:
    builder = _module(BUILDER, "build_standard_candidate_authoring_negative")
    record = deepcopy(_record())
    target: dict[str, object] = record
    for member in path:
        child = target[member]
        assert isinstance(child, dict)
        target = child
    target.update(value)

    with pytest.raises(builder.CandidateError, match="accepted redacted shape"):
        builder._require_authoring_qualification(record)


def test_record_contains_no_field_that_can_carry_private_run_material() -> None:
    record = _record()

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {key for child in value.values() for key in keys(child)}
        if isinstance(value, list):
            return {key for child in value for key in keys(child)}
        return set()

    assert not keys(record) & {
        "workspace_id",
        "principal_id",
        "credential_reference",
        "bearer",
        "grant",
        "endpoint",
        "process_id",
        "prompt",
        "transcript",
        "stdout",
        "stderr",
        "model_response",
    }
    serialized = json.dumps(record, sort_keys=True)
    for forbidden in ("/Users/", "C:\\\\Users\\\\"):
        assert forbidden not in serialized
