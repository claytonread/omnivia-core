"""Focused contract checks for engineering context counting negotiation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from omnivia_core.contracts.v1 import (
    DEFAULT_RETRY_CLASSIFICATION,
    ERROR_CODE_CONTEXT_BUDGET_INSUFFICIENT,
    ERROR_CODE_TOKENIZER_UNAVAILABLE,
    OPERATION_CATALOGUE,
    ContractSemanticError,
    EngineeringContextBuildInput,
    decode_engineering_context_build_input,
    get_operation_metadata,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "contracts" / "application" / "v1" / "schemas"


def _validator(definition: str) -> Draft202012Validator:
    resources = []
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        resource = Resource.from_contents(json.loads(path.read_text(encoding="utf-8")))
        resource_id = resource.id()
        assert resource_id is not None
        resources.append((resource_id, resource))
    return Draft202012Validator(
        {
            "$ref": (
                "https://contracts.omnivia.dev/application/v1/engineering.schema.json"
                f"#/$defs/{definition}"
            )
        },
        registry=Registry().with_resources(resources),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def _base_input() -> dict[str, Any]:
    return {
        "query": "auth",
        "targets": [{"snapshot_id": "snap-1", "snapshot_kind": "git_commit"}],
        "profile": "investigate",
    }


def test_generated_round_trip_preserves_each_negotiated_counting_shape() -> None:
    byte_only = _base_input() | {
        "counting_mode": "byte_only.v1",
        "budget": {"model_bytes": 16384},
    }
    exact = _base_input() | {
        "counting_mode": "exact_tokens.v1",
        "budget": {"model_tokens": 4000, "model_bytes": 16384},
        "tokenizer": {"tokenizer_id": "model-tokenizer", "tokenizer_version": "v1"},
    }
    for payload in (byte_only, exact):
        decoded = decode_engineering_context_build_input(payload)
        assert isinstance(decoded, EngineeringContextBuildInput)
        assert decoded.to_wire() == payload


@pytest.mark.parametrize(
    "payload",
    [
        _base_input()
        | {"counting_mode": "byte_only.v1", "budget": {"model_bytes": 16384}},
        _base_input()
        | {
            "counting_mode": "exact_tokens.v1",
            "budget": {"model_tokens": 4000, "model_bytes": 16384},
            "tokenizer": {
                "tokenizer_id": "model-tokenizer",
                "tokenizer_version": "v1",
            },
        },
    ],
)
def test_strict_schema_accepts_the_two_negotiated_input_shapes(
    payload: dict[str, Any],
) -> None:
    assert list(_validator("EngineeringContextBuildInput").iter_errors(payload)) == []


@pytest.mark.parametrize(
    "payload",
    [
        _base_input() | {"counting_mode": "byte_only.v1", "budget": {}},
        _base_input()
        | {
            "counting_mode": "byte_only.v1",
            "budget": {"model_bytes": 100, "model_tokens": 10},
        },
        _base_input()
        | {
            "counting_mode": "exact_tokens.v1",
            "budget": {"model_tokens": 10, "model_bytes": 100},
        },
        _base_input() | {"counting_mode": "unknown.v1", "budget": {"model_bytes": 100}},
        _base_input()
        | {"tokenizer": {"tokenizer_id": "tok", "tokenizer_version": "v1"}},
    ],
)
def test_strict_schema_rejects_invalid_counting_combinations(
    payload: dict[str, Any],
) -> None:
    assert list(_validator("EngineeringContextBuildInput").iter_errors(payload))


def test_negotiated_semantics_reject_unknown_members_the_decoder_would_ignore() -> None:
    payload = _base_input() | {
        "counting_mode": "byte_only.v1",
        "budget": {"model_bytes": 1024, "future_limit": 5},
    }
    with pytest.raises(ContractSemanticError):
        decode_engineering_context_build_input(payload)


def test_v2_schema_requires_byte_only_replay_data_and_forbids_token_metadata() -> None:
    path = (
        ROOT
        / "packages"
        / "omnivia-core-runtime"
        / "tests"
        / "phase3"
        / "runtime"
        / "fixtures"
        / "engineering_context_v2_byte_only_golden.json"
    )
    pack = json.loads(path.read_text(encoding="utf-8"))
    validator = _validator("EngineeringContextBuildResult")
    assert list(validator.iter_errors({"pack": pack})) == []

    contaminated = json.loads(json.dumps(pack))
    contaminated["rendering"]["token_count"] = 1
    contaminated["budget"]["effective"]["model_tokens"] = 1
    contaminated["budget"]["rendered_tokens"] = 1
    contaminated["reproducibility"]["tokenizer_id"] = "legacy-tokenizer"
    assert list(validator.iter_errors({"pack": contaminated}))

    missing_replay_mode = json.loads(json.dumps(pack))
    missing_replay_mode["normalized_request"].pop("counting_mode")
    missing_replay_mode["reproducibility"].pop("counting_mode")
    assert list(validator.iter_errors({"pack": missing_replay_mode}))


def test_counting_errors_are_non_retryable_and_isolated_to_engineering_build() -> None:
    errors = {
        ERROR_CODE_CONTEXT_BUDGET_INSUFFICIENT,
        ERROR_CODE_TOKENIZER_UNAVAILABLE,
    }
    assert {DEFAULT_RETRY_CLASSIFICATION[code] for code in errors} == {
        "non_retryable"
    }
    advertisers = {
        entry.name
        for entry in OPERATION_CATALOGUE
        if errors.intersection(entry.allowed_errors)
    }
    assert advertisers == {"engineering.context.build"}
    assert errors <= set(
        get_operation_metadata("engineering.context.build").allowed_errors
    )
    assert errors.isdisjoint(get_operation_metadata("context_pack.build").allowed_errors)


def test_generated_typescript_exports_the_counting_contract() -> None:
    source = (
        ROOT / "generated" / "typescript" / "application" / "v1" / "index.ts"
    ).read_text(encoding="utf-8")
    assert "export type EngineeringCountingMode = string;" in source
    assert '"byte_only.v1"' in source
    assert '"exact_tokens.v1"' in source
    assert "export interface EngineeringTokenizerReference" in source
    assert "counting_mode?: EngineeringCountingMode;" in source
    assert "tokenizer?: EngineeringTokenizerReference;" in source
