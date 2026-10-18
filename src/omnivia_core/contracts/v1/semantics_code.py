"""Pure semantic validation for `code.query` (TECH-CORE-RETRIEVAL-CODE-001, W13-A).

The generated decoder is structural and tolerant: it accepts any string as an intent, any
string as a semantic key and any combination of the optional fields. This module is the
closed part of the contract. The intent vocabulary is closed, a subject must be named, the
traversal knobs are accepted only by the intents they mean something to, and every
patterned value is checked with the generated guard rather than a local copy of its
pattern.

`CodeSemanticKey` publishes no pattern, because ECMAScript and Python disagree on what a
word character is; its grammar is stated once, here, and it is the producers' own
(`storage/code_analysis_candidates._ID`).
"""

from __future__ import annotations

import re
from typing import Final

from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    CodeQueryInput,
    is_content_checksum,
    is_identifier,
)

__all__ = [
    "CODE_QUERY_DEFAULT_DEPTH",
    "CODE_QUERY_DEFAULT_LIMIT",
    "CODE_QUERY_INTENTS",
    "CODE_QUERY_INTENT_CALLEES",
    "CODE_QUERY_INTENT_CALLERS",
    "CODE_QUERY_INTENT_DEFINITION",
    "CODE_QUERY_INTENT_DEPENDENCIES",
    "CODE_QUERY_INTENT_REFERENCES",
    "CODE_QUERY_MAX_DEPTH",
    "CODE_QUERY_MAX_LIMIT",
    "is_code_semantic_key",
    "validate_code_query_input",
]

CODE_QUERY_INTENT_DEFINITION: Final = "definition"
CODE_QUERY_INTENT_REFERENCES: Final = "references"
CODE_QUERY_INTENT_CALLERS: Final = "callers"
CODE_QUERY_INTENT_CALLEES: Final = "callees"
CODE_QUERY_INTENT_DEPENDENCIES: Final = "dependencies"
CODE_QUERY_INTENTS: Final = frozenset(
    {
        CODE_QUERY_INTENT_DEFINITION,
        CODE_QUERY_INTENT_REFERENCES,
        CODE_QUERY_INTENT_CALLERS,
        CODE_QUERY_INTENT_CALLEES,
        CODE_QUERY_INTENT_DEPENDENCIES,
    }
)
#: The intents that walk relations and therefore take a depth.
_TRAVERSING: Final = frozenset(
    {CODE_QUERY_INTENT_CALLERS, CODE_QUERY_INTENT_CALLEES, CODE_QUERY_INTENT_DEPENDENCIES}
)

CODE_QUERY_DEFAULT_DEPTH: Final = 1
CODE_QUERY_MAX_DEPTH: Final = 8
CODE_QUERY_DEFAULT_LIMIT: Final = 100
CODE_QUERY_MAX_LIMIT: Final = 1000
_MAX_NAME: Final = 512

_SEMANTIC_KEY: Final = re.compile(r"[\w$#][\w._:/@+$#-]{0,255}")


def is_code_semantic_key(value: object) -> bool:
    """Whether `value` is a producer key the code-analysis candidates admit."""
    return isinstance(value, str) and _SEMANTIC_KEY.fullmatch(value) is not None


def _bounded_int(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def validate_code_query_input(value: CodeQueryInput) -> None:
    """Raise `ContractSemanticError` unless `value` is a `code.query` this contract admits.

    Messages name the field and never quote the value, so a refusal built from one
    carries nothing the caller sent.
    """
    if value.intent not in CODE_QUERY_INTENTS:
        raise ContractSemanticError("intent is not a code.query intent")
    for name in ("repository_id", "snapshot_id", "producer_id"):
        if not is_identifier(getattr(value, name)):
            raise ContractSemanticError(f"{name} is not an Identifier")
    if not is_content_checksum(value.profile_digest):
        raise ContractSemanticError("profile_digest is not a ContentChecksum")
    if value.semantic_key is None and value.name is None:
        raise ContractSemanticError("a code.query names its subject by semantic_key or name")
    if value.semantic_key is not None and not is_code_semantic_key(value.semantic_key):
        raise ContractSemanticError("semantic_key is not a code semantic key")
    if value.name is not None and not (
        isinstance(value.name, str) and 1 <= len(value.name) <= _MAX_NAME
    ):
        raise ContractSemanticError("name must contain 1-512 characters")
    if value.depth is not None and (
        value.intent not in _TRAVERSING
        or not _bounded_int(value.depth, 1, CODE_QUERY_MAX_DEPTH)
    ):
        raise ContractSemanticError("depth is 1-8 and only for callers, callees or dependencies")
    if value.runtime_only is not None and (
        value.intent != CODE_QUERY_INTENT_DEPENDENCIES or not isinstance(value.runtime_only, bool)
    ):
        raise ContractSemanticError("runtime_only is a boolean and only for dependencies")
    if value.limit is not None and not _bounded_int(value.limit, 1, CODE_QUERY_MAX_LIMIT):
        raise ContractSemanticError("limit is outside 1-1000")
