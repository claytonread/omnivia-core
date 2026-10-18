"""Pure semantic checks for the engineering context counting contract.

The generated decoder stays tolerant of additive fields for Application Contract
compatibility.  An explicitly negotiated counting mode is different: every counting
input participates in replay and budgeting, so this module validates the complete raw
shape before the runtime can read storage.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    EngineeringBudget,
    EngineeringContextBuildInput,
    EngineeringTokenizerReference,
    is_identifier,
)

ENGINEERING_COUNTING_MODE_BYTE_ONLY: Final = "byte_only.v1"
ENGINEERING_COUNTING_MODE_EXACT_TOKENS: Final = "exact_tokens.v1"
ENGINEERING_COUNTING_MODES: Final[frozenset[str]] = frozenset(
    {
        ENGINEERING_COUNTING_MODE_BYTE_ONLY,
        ENGINEERING_COUNTING_MODE_EXACT_TOKENS,
    }
)

ENGINEERING_HARD_LIMIT_MODEL_TOKENS: Final = 16_000
ENGINEERING_HARD_LIMIT_MODEL_BYTES: Final = 65_536
ENGINEERING_HARD_LIMIT_HYDRATIONS: Final = 32
ENGINEERING_HARD_LIMIT_EVIDENCE_BYTES: Final = 1_048_576
ENGINEERING_HARD_LIMIT_AUTHORIZED_CANDIDATES: Final = 10_000

_INPUT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "query",
        "targets",
        "profile",
        "topic_refs",
        "checkpoint_refs",
        "budget",
        "applicability_mode",
        "counting_mode",
        "tokenizer",
    }
)
_BUDGET_KEYS: Final[frozenset[str]] = frozenset(
    {
        "model_tokens",
        "model_bytes",
        "hydrations",
        "evidence_bytes",
        "authorized_candidates",
    }
)
_TOKENIZER_KEYS: Final[frozenset[str]] = frozenset(
    {"tokenizer_id", "tokenizer_version"}
)
_TARGET_KEYS: Final[frozenset[str]] = frozenset(
    {"snapshot_id", "repository_id", "snapshot_kind", "branch_label"}
)
_TOPIC_KEYS: Final[frozenset[str]] = frozenset({"record_id", "proposed_key"})


def _reject_unknown(mapping: Mapping[object, object], allowed: frozenset[str], label: str) -> None:
    unknown = [key for key in mapping if not isinstance(key, str) or key not in allowed]
    if unknown:
        raise ContractSemanticError(f"{label}: unknown members are not permitted")


def _require_mapping(value: object, label: str) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise ContractSemanticError(f"{label}: expected an object")
    return value


def _require_sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ContractSemanticError(f"{label}: expected an array")
    return value


def _validate_raw_negotiated_shape(payload: object) -> None:
    mapping = _require_mapping(payload, "EngineeringContextBuildInput")
    _reject_unknown(mapping, _INPUT_KEYS, "EngineeringContextBuildInput")

    budget = mapping.get("budget")
    if budget is not None:
        _reject_unknown(
            _require_mapping(budget, "EngineeringContextBuildInput.budget"),
            _BUDGET_KEYS,
            "EngineeringContextBuildInput.budget",
        )

    tokenizer = mapping.get("tokenizer")
    if tokenizer is not None:
        _reject_unknown(
            _require_mapping(tokenizer, "EngineeringContextBuildInput.tokenizer"),
            _TOKENIZER_KEYS,
            "EngineeringContextBuildInput.tokenizer",
        )

    targets = mapping.get("targets")
    if targets is not None:
        for index, target in enumerate(
            _require_sequence(targets, "EngineeringContextBuildInput.targets")
        ):
            label = f"EngineeringContextBuildInput.targets[{index}]"
            _reject_unknown(_require_mapping(target, label), _TARGET_KEYS, label)

    topic_refs = mapping.get("topic_refs")
    if topic_refs is not None:
        for index, topic in enumerate(
            _require_sequence(topic_refs, "EngineeringContextBuildInput.topic_refs")
        ):
            label = f"EngineeringContextBuildInput.topic_refs[{index}]"
            _reject_unknown(_require_mapping(topic, label), _TOPIC_KEYS, label)


def _require_positive_limit(value: object, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractSemanticError(f"{label}: expected an integer")
    if value < 1 or value > maximum:
        raise ContractSemanticError(f"{label}: outside the supported range")
    return value


def _validate_budget(budget: EngineeringBudget) -> None:
    for value, maximum, label in (
        (
            budget.model_tokens,
            ENGINEERING_HARD_LIMIT_MODEL_TOKENS,
            "EngineeringContextBuildInput.budget.model_tokens",
        ),
        (
            budget.model_bytes,
            ENGINEERING_HARD_LIMIT_MODEL_BYTES,
            "EngineeringContextBuildInput.budget.model_bytes",
        ),
        (
            budget.hydrations,
            ENGINEERING_HARD_LIMIT_HYDRATIONS,
            "EngineeringContextBuildInput.budget.hydrations",
        ),
        (
            budget.evidence_bytes,
            ENGINEERING_HARD_LIMIT_EVIDENCE_BYTES,
            "EngineeringContextBuildInput.budget.evidence_bytes",
        ),
        (
            budget.authorized_candidates,
            ENGINEERING_HARD_LIMIT_AUTHORIZED_CANDIDATES,
            "EngineeringContextBuildInput.budget.authorized_candidates",
        ),
    ):
        if value is not None:
            _require_positive_limit(value, maximum, label)


def _validate_tokenizer(tokenizer: EngineeringTokenizerReference) -> None:
    if not is_identifier(tokenizer.tokenizer_id):
        raise ContractSemanticError(
            "EngineeringContextBuildInput.tokenizer.tokenizer_id: invalid identifier"
        )
    if not is_identifier(tokenizer.tokenizer_version):
        raise ContractSemanticError(
            "EngineeringContextBuildInput.tokenizer.tokenizer_version: invalid identifier"
        )


def validate_engineering_context_build_input(
    value: EngineeringContextBuildInput,
) -> None:
    """Validate cross-field semantics for one decoded engineering build input."""

    if not isinstance(value, EngineeringContextBuildInput):
        raise ContractSemanticError(
            "EngineeringContextBuildInput: expected EngineeringContextBuildInput"
        )

    mode = value.counting_mode
    if mode is None:
        if value.tokenizer is not None:
            raise ContractSemanticError(
                "EngineeringContextBuildInput.tokenizer: counting_mode is required"
            )
        return
    if mode not in ENGINEERING_COUNTING_MODES:
        raise ContractSemanticError(
            "EngineeringContextBuildInput.counting_mode: unsupported counting mode"
        )
    if value.budget is None:
        raise ContractSemanticError(
            "EngineeringContextBuildInput.budget: required for negotiated counting"
        )
    if not isinstance(value.budget, EngineeringBudget):
        raise ContractSemanticError(
            "EngineeringContextBuildInput.budget: expected EngineeringBudget"
        )
    _validate_budget(value.budget)

    if mode == ENGINEERING_COUNTING_MODE_BYTE_ONLY:
        if value.budget.model_bytes is None:
            raise ContractSemanticError(
                "EngineeringContextBuildInput.budget.model_bytes: required"
            )
        if value.budget.model_tokens is not None:
            raise ContractSemanticError(
                "EngineeringContextBuildInput.budget.model_tokens: forbidden in byte-only mode"
            )
        if value.tokenizer is not None:
            raise ContractSemanticError(
                "EngineeringContextBuildInput.tokenizer: forbidden in byte-only mode"
            )
        return

    if value.budget.model_tokens is None or value.budget.model_bytes is None:
        raise ContractSemanticError(
            "EngineeringContextBuildInput.budget: exact-token mode requires token and byte limits"
        )
    if value.tokenizer is None or not isinstance(
        value.tokenizer, EngineeringTokenizerReference
    ):
        raise ContractSemanticError(
            "EngineeringContextBuildInput.tokenizer: required for exact-token mode"
        )
    _validate_tokenizer(value.tokenizer)


def decode_engineering_context_build_input(
    payload: object,
) -> EngineeringContextBuildInput:
    """Decode, then strictly validate explicitly negotiated counting input."""

    value = EngineeringContextBuildInput.from_wire(payload)
    if value.counting_mode is not None:
        _validate_raw_negotiated_shape(payload)
    validate_engineering_context_build_input(value)
    return value


__all__ = [
    "ENGINEERING_COUNTING_MODES",
    "ENGINEERING_COUNTING_MODE_BYTE_ONLY",
    "ENGINEERING_COUNTING_MODE_EXACT_TOKENS",
    "ENGINEERING_HARD_LIMIT_AUTHORIZED_CANDIDATES",
    "ENGINEERING_HARD_LIMIT_EVIDENCE_BYTES",
    "ENGINEERING_HARD_LIMIT_HYDRATIONS",
    "ENGINEERING_HARD_LIMIT_MODEL_BYTES",
    "ENGINEERING_HARD_LIMIT_MODEL_TOKENS",
    "decode_engineering_context_build_input",
    "validate_engineering_context_build_input",
]
