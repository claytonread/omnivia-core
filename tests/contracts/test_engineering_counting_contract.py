"""Focused contract checks for engineering context counting negotiation."""

from __future__ import annotations

import inspect
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
    EngineeringBudget,
    EngineeringBudgetOutcome,
    EngineeringContextBuildInput,
    EngineeringRendering,
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


def _v2_golden_pack() -> dict[str, Any]:
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
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


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


def test_generated_python_constructors_preserve_legacy_positional_order() -> None:
    rendering_parameters = inspect.signature(EngineeringRendering).parameters
    assert tuple(rendering_parameters) == (
        "text",
        "renderer_version",
        "token_count",
        "byte_count",
    )
    assert all(
        parameter.default is inspect.Parameter.empty
        for parameter in rendering_parameters.values()
    )
    assert EngineeringRendering.__match_args__ == tuple(rendering_parameters)

    outcome_parameters = inspect.signature(EngineeringBudgetOutcome).parameters
    assert tuple(outcome_parameters) == (
        "effective",
        "rendered_tokens",
        "rendered_bytes",
        "source_bytes_read",
        "hydrations",
        "requested",
    )
    assert all(
        parameter.default is inspect.Parameter.empty
        for parameter in tuple(outcome_parameters.values())[:5]
    )
    assert outcome_parameters["requested"].default is None
    assert EngineeringBudgetOutcome.__match_args__ == tuple(outcome_parameters)

    rendering = EngineeringRendering("text", "renderer-v1", 7, 4)
    assert (rendering.token_count, rendering.byte_count) == (7, 4)
    effective = EngineeringBudget(model_tokens=10, model_bytes=20)
    outcome = EngineeringBudgetOutcome(effective, 7, 4, 2, 3)
    assert (
        outcome.rendered_tokens,
        outcome.rendered_bytes,
        outcome.source_bytes_read,
        outcome.hydrations,
        outcome.requested,
    ) == (7, 4, 2, 3, None)


def test_generated_python_v2_values_use_none_in_the_legacy_token_slots() -> None:
    rendering_wire = {
        "text": "text",
        "renderer_version": "renderer-v2",
        "byte_count": 4,
    }
    rendering = EngineeringRendering("text", "renderer-v2", None, 4)
    assert rendering.to_wire() == rendering_wire
    assert EngineeringRendering.from_wire(rendering_wire) == rendering

    effective = EngineeringBudget(model_bytes=20)
    outcome_wire = {
        "effective": {"model_bytes": 20},
        "rendered_bytes": 4,
        "source_bytes_read": 2,
        "hydrations": 3,
    }
    outcome = EngineeringBudgetOutcome(effective, None, 4, 2, 3)
    assert outcome.to_wire() == outcome_wire
    assert EngineeringBudgetOutcome.from_wire(outcome_wire) == outcome


def test_v2_schema_accepts_the_pinned_byte_only_pack() -> None:
    pack = _v2_golden_pack()
    validator = _validator("EngineeringContextBuildResult")
    assert list(validator.iter_errors({"pack": pack})) == []


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("rendering", "token_count"), 1),
        (("budget", "rendered_tokens"), 1),
        (("budget", "effective", "model_tokens"), 1),
        (("budget", "requested", "model_tokens"), 1),
    ],
)
def test_v2_schema_rejects_token_budget_and_rendering_fields(
    path: tuple[str, ...], value: object
) -> None:
    contaminated = _v2_golden_pack()
    target: dict[str, Any] = contaminated
    for part in path[:-1]:
        child = target.setdefault(part, {})
        assert isinstance(child, dict)
        target = child
    target[path[-1]] = value
    assert list(
        _validator("EngineeringContextBuildResult").iter_errors({"pack": contaminated})
    )


_FORBIDDEN_BYTE_ONLY_REPLAY_FIELDS: tuple[tuple[str, object], ...] = (
    ("tokenizer", {"tokenizer_id": "model-tokenizer", "tokenizer_version": "v1"}),
    ("tokenizer_id", "model-tokenizer"),
    ("tokenizer_version", "v1"),
    ("tokenizer_note", "unavailable"),
    ("token_count", 1),
    ("model_tokens", 1),
    ("rendered_tokens", 1),
)


@pytest.mark.parametrize(
    ("container", "field", "value"),
    [
        (container, field, value)
        for container in ("normalized_request", "reproducibility")
        for field, value in _FORBIDDEN_BYTE_ONLY_REPLAY_FIELDS
    ],
)
def test_v2_schema_rejects_each_replay_token_field_independently(
    container: str, field: str, value: object
) -> None:
    contaminated = _v2_golden_pack()
    replay_data = contaminated[container]
    assert isinstance(replay_data, dict)
    replay_data[field] = value
    assert list(
        _validator("EngineeringContextBuildResult").iter_errors({"pack": contaminated})
    )


@pytest.mark.parametrize("container", ["normalized_request", "reproducibility"])
def test_v2_schema_requires_counting_mode_in_each_replay_container(
    container: str,
) -> None:
    missing_replay_mode = _v2_golden_pack()
    replay_data = missing_replay_mode[container]
    assert isinstance(replay_data, dict)
    replay_data.pop("counting_mode")
    assert list(
        _validator("EngineeringContextBuildResult").iter_errors(
            {"pack": missing_replay_mode}
        )
    )


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
    assert "readonly token_count: number;" in source
    assert "readonly rendered_tokens: number;" in source
    assert (
        'export type EngineeringRenderingV2 = Omit<EngineeringRendering, "token_count">;'
        in source
    )
    assert "export type EngineeringBudgetOutcomeV2 = Omit<" in source
    assert "export type EngineeringContextPackV2 = Omit<" in source
    assert 'readonly format_version: "engineering_context.v2";' in source
    assert "readonly rendering: EngineeringRenderingV2;" in source
    assert "readonly budget: EngineeringBudgetOutcomeV2;" in source
    assert "export type EngineeringContextBuildResultV2 = Omit<" in source
    assert "readonly pack: EngineeringContextPackV2;" in source
