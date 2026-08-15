"""Tests for the nine Q1-A evidence schemas under `schemas/`.

Covers: exact schema-file inventory, duplicate-member-rejecting JSON loads,
Draft 2020-12 self-validity, one minimal valid instance per schema, rejection
of unknown fields / bad hashes / duplicate ordered IDs, absence of generated
JSON under `examples/`, recursive closure (`additionalProperties: false`) of
every semantic object subschema, and forbidden private-path/network/credential
content in schema authority and reproduction arguments.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, ValidationError

_Q1_DIR = Path(__file__).resolve().parents[1]
_SCHEMA_DIR = _Q1_DIR / "schemas"
_EXAMPLES_DIR = _Q1_DIR / "examples"

_ZERO_HASH = "0" * 64

EXPECTED_SCHEMA_FILES = frozenset(
    {
        "campaign.schema.json",
        "dataset-manifest.schema.json",
        "policy-input.schema.json",
        "policy-decision.schema.json",
        "frontier.schema.json",
        "ground-truth.schema.json",
        "operation-trace.schema.json",
        "reproduction.schema.json",
        "root-manifest.schema.json",
    }
)


def _sha(letter: str) -> str:
    return f"sha256:{letter * 64}"


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key!r}")
        result[key] = value
    return result


def _strict_json_loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_reject_duplicate_members)


def _load_schema(name: str) -> dict[str, Any]:
    value = _strict_json_loads((_SCHEMA_DIR / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"schema {name!r} is not a JSON object")
    return cast(dict[str, Any], value)


_CAMPAIGN_INSTANCE: dict[str, Any] = {
    "schema_id": "https://schemas.omnivia.dev/qualification/semantic-index/q1/v1/campaign.schema.json",
    "schema_version": "1.0",
    "noncanonical": True,
    "campaign_id": _sha("a"),
    "q0_decision_hash": _sha("b"),
    "authority_hashes": {"q0_decision": _sha("c")},
    "harness_commits": ["0" * 40],
    "schema_commits": ["1" * 40],
    "owner": "codex",
    "created_at": "2026-08-02T00:00:00Z",
    "admitted_platforms": [
        {
            "platform_id": "linux-x86_64",
            "os": "linux",
            "arch": "x86_64",
            "admitted_at": "2026-08-02T00:00:00Z",
            "result_hash": _sha("d"),
        }
    ],
}

_DATASET_MANIFEST_INSTANCE: dict[str, Any] = {
    "schema_id": "https://schemas.omnivia.dev/qualification/semantic-index/q1/v1/dataset-manifest.schema.json",
    "schema_version": "1.0",
    "noncanonical": True,
    "dataset_manifest_id": _sha("a"),
    "profile_id": "q1.synthetic-768-cosine-v1",
    "root_seed": "omnivia-core-semantic-index-qualification-2026-08-02-v1",
    "generator_version": "1.0.0",
    "runtime_versions": [
        {"name": "python", "version": "3.11.9", "distribution_hash": _sha("f")}
    ],
    "active_record_count": 10,
    "setup_record_count": 0,
    "partition_count": 1,
    "query_count": 3,
    "total_byte_length": 1024,
    "partitions": [
        {
            "partition_index": 0,
            "record_count": 10,
            "byte_length": 1024,
            "child_hash": _sha("b"),
        }
    ],
    "artifact_hashes": [
        {
            "path": "qualification/semantic_index/q1/output/partition-000000.jsonl",
            "hash": _sha("c"),
        }
    ],
}

_POLICY_INPUT_INSTANCE: dict[str, Any] = {
    "schema_id": "https://schemas.omnivia.dev/qualification/semantic-index/q1/v1/policy-input.schema.json",
    "schema_version": "1.0",
    "noncanonical": True,
    "policy_input_id": _sha("a"),
    "fixture_policy_schema_id": "omnivia.semantic-index.fixture-policy.v1",
    "policy_set_id": "q1.synthetic.current-canonical.v1",
    "policy_epoch": 7,
    "expected_policy_epoch": 7,
    "policy_digest": _sha("b"),
    "source_digests": {
        "authority": _sha("d"),
        "operation_catalogue": _sha("e"),
        "resources": _sha("f"),
    },
    "operation": "evidence.search",
    "authority": {
        "principal_id": "principal.reader_internal",
        "role_ids": ["reader"],
        "workspace_grant": {
            "workspace_id": "workspace.alpha",
            "scopes": ["memory:read"],
        },
        "effective_capabilities": [
            {"capability_id": "evidence.read", "version": "1.0"}
        ],
        "clearance": "internal",
    },
    "request": {
        "request_id": _sha("c"),
        "workspace_id": "workspace.alpha",
        "scopes": ["memory:read"],
        "purpose": "user_initiated",
        "v_us": 1_000_000,
        "t_us": 2_000_000,
    },
    "resources": [
        {
            "record_id": "q1-record-evidence-000000",
            "kind": "evidence",
            "workspace_id": "workspace.alpha",
            "sensitivity": "internal",
            "recorded_at_us": 1_000_000,
            "label_events": [
                {
                    "event_id": "label-001",
                    "sequence": 1,
                    "label": "acl.allow.role.reader",
                    "action": "attached",
                }
            ],
            "label_events_digest": _sha("1"),
            "provenance_events": [
                {
                    "event_id": "provenance-001",
                    "sequence": 1,
                    "action": "created",
                    "tombstoned_observation": None,
                }
            ],
            "provenance_events_digest": _sha("2"),
        }
    ],
}

_POLICY_DECISION_INSTANCE: dict[str, Any] = {
    "schema_id": "https://schemas.omnivia.dev/qualification/semantic-index/q1/v1/policy-decision.schema.json",
    "schema_version": "1.0",
    "noncanonical": True,
    "policy_decision_id": _sha("a"),
    "policy_input_id": _sha("b"),
    "applicable_rule_ids": ["q1.evidence.search.allow.role.reader"],
    "matched_allow_rule_ids": ["q1.evidence.search.allow.role.reader"],
    "matched_deny_rule_ids": [],
    "mandatory_gate_results": [{"gate_id": "clearance_at_least", "passed": True}],
    "disposition": "allow",
    "evaluator_id": "q1.policy.evaluator.v1",
    "evaluator_version_digest": _sha("c"),
}

_FRONTIER_INSTANCE: dict[str, Any] = {
    "schema_id": "https://schemas.omnivia.dev/qualification/semantic-index/q1/v1/frontier.schema.json",
    "schema_version": "1.0",
    "noncanonical": True,
    "frontier_digest": _sha("a"),
    "operation": "evidence.search",
    "query_coordinate": {"query_id": _sha("b"), "k": 10},
    "authorized_external_ids": ["q1-record-evidence-000000"],
    "authorized_count": 1,
    "policy_input_digest": _sha("d"),
    "policy_decision_digest": _sha("e"),
    "policy_epoch": 1,
}

_GROUND_TRUTH_INSTANCE: dict[str, Any] = {
    "schema_id": "https://schemas.omnivia.dev/qualification/semantic-index/q1/v1/ground-truth.schema.json",
    "schema_version": "1.0",
    "noncanonical": True,
    "ground_truth_id": _sha("a"),
    "checkpoint_id": _sha("b"),
    "query_id": _sha("c"),
    "filter_id": None,
    "filter": None,
    "k": 10,
    "eligible_count": 1,
    "eligible_frontier_digest": _sha("d"),
    "ranked_ids": ["q1-record-evidence-000000"],
    "scores": [0.9],
    "oracle_a": {
        "implementation_id": "oracle.brute_force.v1",
        "version_digest": _sha("f"),
    },
    "oracle_b": {
        "implementation_id": "oracle.reference.v1",
        "version_digest": f"sha256:{_ZERO_HASH}",
    },
    "exact_agreement": True,
}

_OPERATION_TRACE_INSTANCE: dict[str, Any] = {
    "schema_id": "https://schemas.omnivia.dev/qualification/semantic-index/q1/v1/operation-trace.schema.json",
    "schema_version": "1.0",
    "noncanonical": True,
    "operation_trace_id": _sha("a"),
    "ordinal": 0,
    "operation_id": "op.insert.000000",
    "operation_digest": _sha("b"),
    "mutation": "insert",
    "affected_ids": ["q1-record-evidence-000000"],
    "expected_outcome": "applied",
    "watermark_microseconds": 1000,
    "visibility_barrier": 1000,
    "replay_binding": {
        "source_operation_trace_digest": _sha("d"),
        "replay_of_ordinal": 0,
        "expected_outcome": "noop",
    },
}

_REPRODUCTION_INSTANCE: dict[str, Any] = {
    "schema_id": "https://schemas.omnivia.dev/qualification/semantic-index/q1/v1/reproduction.schema.json",
    "schema_version": "1.0",
    "noncanonical": True,
    "reproduction_id": _sha("a"),
    "command": ["python3", "qualification/semantic_index/q1/generate.py"],
    "working_directory": "qualification/semantic_index/q1",
    "environment": {"platform_id": "linux-x86_64", "os": "linux", "arch": "x86_64"},
    "interpreter_versions": {"python": "3.11.9"},
    "input_hashes": [
        {"path": "qualification/semantic_index/q1/generate.py", "hash": _sha("b")}
    ],
    "output_hashes": [
        {"path": "qualification/semantic_index/q1/tests/output.json", "hash": _sha("c")}
    ],
    "exit_code": 0,
    "stdout_hash": _sha("d"),
    "stderr_hash": f"sha256:{_ZERO_HASH}",
}

_ROOT_MANIFEST_INSTANCE: dict[str, Any] = {
    "schema_id": "https://schemas.omnivia.dev/qualification/semantic-index/q1/v1/root-manifest.schema.json",
    "schema_version": "1.0",
    "noncanonical": True,
    "aggregate_digest": _sha("a"),
    "children": [
        {
            "child_type": child_type,
            "path": f"qualification/semantic_index/q1/output/{child_type}.json",
            "hash": _sha(hash_letter),
            "byte_length": 100,
        }
        for child_type, hash_letter in zip(
            (
                "campaign",
                "dataset_manifest",
                "policy_input",
                "policy_decision",
                "frontier",
                "ground_truth",
                "operation_trace",
                "reproduction",
            ),
            "01234567",
            strict=True,
        )
    ],
    "reviewer_attestations": [
        {
            "reviewer_id": "codex",
            "attestation_digest": _sha("c"),
            "attested_at": "2026-08-02T00:00:00Z",
        }
    ],
}

# (schema filename, minimal valid instance, JSON-pointer-style path to a
# uniqueItems array to prove duplicate-ordered-ID rejection, one sha256 field
# path to prove bad-hash rejection).
CASES: list[tuple[str, dict[str, Any], tuple[Any, ...], tuple[Any, ...]]] = [
    (
        "campaign.schema.json",
        _CAMPAIGN_INSTANCE,
        ("harness_commits",),
        ("campaign_id",),
    ),
    (
        "dataset-manifest.schema.json",
        _DATASET_MANIFEST_INSTANCE,
        ("partitions",),
        ("dataset_manifest_id",),
    ),
    (
        "policy-input.schema.json",
        _POLICY_INPUT_INSTANCE,
        ("resources",),
        ("request", "request_id"),
    ),
    (
        "policy-decision.schema.json",
        _POLICY_DECISION_INSTANCE,
        ("applicable_rule_ids",),
        ("policy_decision_id",),
    ),
    (
        "frontier.schema.json",
        _FRONTIER_INSTANCE,
        ("authorized_external_ids",),
        ("frontier_digest",),
    ),
    (
        "ground-truth.schema.json",
        _GROUND_TRUTH_INSTANCE,
        ("ranked_ids",),
        ("ground_truth_id",),
    ),
    (
        "operation-trace.schema.json",
        _OPERATION_TRACE_INSTANCE,
        ("affected_ids",),
        ("operation_trace_id",),
    ),
    (
        "reproduction.schema.json",
        _REPRODUCTION_INSTANCE,
        ("input_hashes",),
        ("reproduction_id",),
    ),
    (
        "root-manifest.schema.json",
        _ROOT_MANIFEST_INSTANCE,
        ("children",),
        ("aggregate_digest",),
    ),
]

CASE_IDS = [name for name, *_ in CASES]


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def test_exact_schema_file_inventory() -> None:
    actual = {path.name for path in _SCHEMA_DIR.glob("*.json")}
    assert actual == EXPECTED_SCHEMA_FILES


def test_strict_json_loader_rejects_duplicate_members() -> None:
    with pytest.raises(ValueError, match="duplicate JSON member"):
        _strict_json_loads('{"schema_version":"1.0","schema_version":"2.0"}')


# ---------------------------------------------------------------------------
# Draft 2020-12 self-validity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", sorted(EXPECTED_SCHEMA_FILES))
def test_schema_is_valid_draft_2020_12(filename: str) -> None:
    schema = _load_schema(filename)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("filename", sorted(EXPECTED_SCHEMA_FILES))
def test_schema_id_is_versioned_https_under_q1(filename: str) -> None:
    schema = _load_schema(filename)
    schema_id = schema["$id"]
    assert schema_id.startswith(
        "https://schemas.omnivia.dev/qualification/semantic-index/q1/"
    )
    assert "/v1/" in schema_id
    assert schema["properties"]["schema_id"] == {"const": schema_id}


# ---------------------------------------------------------------------------
# Minimal instance validity + negative cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,instance,dup_path,bad_hash_path", CASES, ids=CASE_IDS
)
def test_minimal_instance_is_valid(
    filename: str,
    instance: dict[str, Any],
    dup_path: tuple[Any, ...],
    bad_hash_path: tuple[Any, ...],
) -> None:
    schema = _load_schema(filename)
    Draft202012Validator(schema).validate(instance)


@pytest.mark.parametrize(
    "filename,instance,dup_path,bad_hash_path", CASES, ids=CASE_IDS
)
def test_unknown_field_is_rejected(
    filename: str,
    instance: dict[str, Any],
    dup_path: tuple[Any, ...],
    bad_hash_path: tuple[Any, ...],
) -> None:
    schema = _load_schema(filename)
    tampered = copy.deepcopy(instance)
    tampered["unexpected_field"] = "not allowed"
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(tampered)


@pytest.mark.parametrize(
    "filename,instance,dup_path,bad_hash_path", CASES, ids=CASE_IDS
)
def test_bad_hash_is_rejected(
    filename: str,
    instance: dict[str, Any],
    dup_path: tuple[Any, ...],
    bad_hash_path: tuple[Any, ...],
) -> None:
    schema = _load_schema(filename)
    tampered = copy.deepcopy(instance)
    node: Any = tampered
    for key in bad_hash_path[:-1]:
        node = node[key]
    node[bad_hash_path[-1]] = "sha256:NOTVALIDHEX"
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(tampered)


@pytest.mark.parametrize(
    "filename,instance,dup_path,bad_hash_path", CASES, ids=CASE_IDS
)
def test_duplicate_ordered_ids_are_rejected(
    filename: str,
    instance: dict[str, Any],
    dup_path: tuple[Any, ...],
    bad_hash_path: tuple[Any, ...],
) -> None:
    schema = _load_schema(filename)
    tampered = copy.deepcopy(instance)
    node: Any = tampered
    for key in dup_path[:-1]:
        node = node[key]
    array = node[dup_path[-1]]
    assert len(array) >= 1
    array.append(copy.deepcopy(array[0]))
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(tampered)


def test_ground_truth_requires_exact_oracle_agreement() -> None:
    schema = _load_schema("ground-truth.schema.json")
    tampered = copy.deepcopy(_GROUND_TRUTH_INSTANCE)
    tampered["exact_agreement"] = False
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(tampered)


def test_policy_input_binds_operation_to_required_capability() -> None:
    schema = _load_schema("policy-input.schema.json")
    tampered = copy.deepcopy(_POLICY_INPUT_INSTANCE)
    tampered["authority"]["effective_capabilities"][0]["capability_id"] = "memory.read"
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(tampered)


@pytest.mark.parametrize(
    "filename,field",
    [
        ("frontier.schema.json", "authorized_external_ids"),
        ("ground-truth.schema.json", "ranked_ids"),
        ("operation-trace.schema.json", "affected_ids"),
    ],
)
def test_external_record_ids_require_fixed_width_ascii_ordinals(
    filename: str, field: str
) -> None:
    instances = {
        "frontier.schema.json": _FRONTIER_INSTANCE,
        "ground-truth.schema.json": _GROUND_TRUTH_INSTANCE,
        "operation-trace.schema.json": _OPERATION_TRACE_INSTANCE,
    }
    schema = _load_schema(filename)
    tampered = copy.deepcopy(instances[filename])
    tampered[field][0] = _sha("f")
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(tampered)


@pytest.mark.parametrize(
    "argument",
    [
        "/Users/founder/private.py",
        "../outside-repository.json",
        "https://private.example.invalid/input",
        "token=not-an-allowed-value",
        r"C:\\Users\\founder\\private.py",
    ],
)
def test_reproduction_rejects_private_network_or_credential_arguments(
    argument: str,
) -> None:
    schema = _load_schema("reproduction.schema.json")
    tampered = copy.deepcopy(_REPRODUCTION_INSTANCE)
    tampered["command"].append(argument)
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(tampered)


# ---------------------------------------------------------------------------
# No generated example JSON yet
# ---------------------------------------------------------------------------


def test_no_generated_json_exists_under_examples() -> None:
    assert _EXAMPLES_DIR.is_dir()
    json_files = list(_EXAMPLES_DIR.rglob("*.json"))
    assert json_files == []


def test_noncanonical_examples_readme_exists_and_is_not_json() -> None:
    readme = _EXAMPLES_DIR / "noncanonical" / "README.md"
    assert readme.is_file()
    text = readme.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "must not be read as" in lowered
    assert "no example json bytes" in lowered


# ---------------------------------------------------------------------------
# Recursive closure: every semantic object subschema is additionalProperties: false
# ---------------------------------------------------------------------------


def _walk(node: Any) -> Any:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


@pytest.mark.parametrize("filename", sorted(EXPECTED_SCHEMA_FILES))
def test_object_subschemas_are_recursively_closed(filename: str) -> None:
    schema = _load_schema(filename)
    object_subschemas = [node for node in _walk(schema) if node.get("type") == "object"]
    assert object_subschemas, "expected at least one object subschema"
    for subschema in object_subschemas:
        assert subschema.get("additionalProperties") is False, subschema


# ---------------------------------------------------------------------------
# Forbidden credential/endpoint/absolute-private-path authority scan
# ---------------------------------------------------------------------------

_FORBIDDEN_FIELD_NAME_SUBSTRINGS = (
    "password",
    "secret",
    "credential",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "token",
    "endpoint_url",
    "endpoint_uri",
    "network_endpoint",
)

_FORBIDDEN_TEXT_SUBSTRINGS = (
    "/home/",
    "/users/",
    "/private/",
    "/etc/",
    "c:\\",
)


def _iter_property_names(schema: dict[str, Any]) -> Any:
    for node in _walk(schema):
        for key in ("properties", "patternProperties"):
            yield from node.get(key, {})


def _iter_text_fields(schema: dict[str, Any]) -> Any:
    for node in _walk(schema):
        for key in ("title", "description"):
            value = node.get(key)
            if isinstance(value, str):
                yield value


@pytest.mark.parametrize("filename", sorted(EXPECTED_SCHEMA_FILES))
def test_no_forbidden_field_names(filename: str) -> None:
    schema = _load_schema(filename)
    for name in _iter_property_names(schema):
        lowered = name.lower()
        for forbidden in _FORBIDDEN_FIELD_NAME_SUBSTRINGS:
            assert forbidden not in lowered, (
                f"{filename}: forbidden field name {name!r}"
            )


@pytest.mark.parametrize("filename", sorted(EXPECTED_SCHEMA_FILES))
def test_no_forbidden_text_in_titles_and_descriptions(filename: str) -> None:
    schema = _load_schema(filename)
    for text in _iter_text_fields(schema):
        lowered = text.lower()
        for forbidden in _FORBIDDEN_TEXT_SUBSTRINGS:
            assert forbidden not in lowered, f"{filename}: forbidden text {text!r}"
