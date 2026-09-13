"""Stable error codes and exceptions for the Governed Knowledge domain layer.

Stage 1 of `SPEC-CORE-KNOWLEDGE-IMPROVEMENT-001` (KI-01..KI-04): pure logical
profiles over existing governed records, plus the applicability evaluator.
Every construction/validation failure here raises a
:class:`GovernedKnowledgeError` carrying one of these frozen codes, never a
bare `ValueError`/`TypeError`/`KeyError` -- the same discipline
`semantic_registry.errors` uses, kept as its own domain vocabulary rather than
overloading unrelated `SemanticErrorCode` members with new meanings.
"""

from __future__ import annotations

from enum import Enum


class GovernedKnowledgeErrorCode(str, Enum):
    """Frozen, stable reasons a governed-knowledge value is refused."""

    MISSING_FIELD = "missing_field"
    INVALID_FIELD = "invalid_field"
    UNSUPPORTED_VALUE = "unsupported_value"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    NODE_LIMIT_EXCEEDED = "node_limit_exceeded"
    DEPTH_LIMIT_EXCEEDED = "depth_limit_exceeded"
    SET_LIMIT_EXCEEDED = "set_limit_exceeded"


class GovernedKnowledgeError(ValueError):
    """Base error for every public failure raised by ``governed_knowledge``."""

    def __init__(self, code: GovernedKnowledgeErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"[{code.value}] {message}")


class GovernedKnowledgeValidationError(GovernedKnowledgeError):
    """A value failed its own invariants at construction/validation time."""


def require(condition: object, code: GovernedKnowledgeErrorCode, message: str) -> None:
    """Raise :class:`GovernedKnowledgeValidationError` unless `condition` is truthy."""
    if not condition:
        raise GovernedKnowledgeValidationError(code, message)


__all__ = [
    "GovernedKnowledgeError",
    "GovernedKnowledgeErrorCode",
    "GovernedKnowledgeValidationError",
    "require",
]
