"""Pure semantic validation for `code.query` (TECH-CORE-RETRIEVAL-CODE-001, W13-A/W13-B).

The generated decoder is structural and tolerant: it accepts any string as an intent, any
string as a semantic key and any combination of the optional fields. This module is the
closed part of the contract. The intent vocabulary is closed, a subject must be named, the
traversal knobs are accepted only by the intents they mean something to, and every
patterned value is checked with the generated guard rather than a local copy of its
pattern.

W13-B adds `code_context` and the two fields only it accepts. A seed is identity plus the
binding it was selected against, so every one of its five binding members is checked here
with the same guards the request's own binding gets: a seed that is not a well-formed
binding can never reach the comparison that decides whether it may expand. The task-context
request is delegated whole to the Context Pack authority's own validator rather than
re-checked here, because a second opinion about that payload is a second contract.

`CodeSemanticKey` publishes no pattern, because ECMAScript and Python disagree on what a
word character is; its grammar is stated once, here, and it is the producers' own
(`storage/code_analysis_candidates._ID`).
"""

from __future__ import annotations

import re
from typing import Final

from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    CodeContextSeed,
    CodeQueryInput,
    is_content_checksum,
    is_identifier,
)
from omnivia_core.contracts.v1.semantics_knowledge import (
    validate_context_pack_build_input,
)

__all__ = [
    "CODE_CONTEXT_MAX_SEEDS",
    "CODE_CONTEXT_MAX_SPAN_BYTES",
    "CODE_CONTEXT_SEED_ORIGINS",
    "CODE_CONTEXT_SEED_ORIGIN_LEXICAL",
    "CODE_CONTEXT_SEED_ORIGIN_SEMANTIC",
    "CODE_QUERY_DEFAULT_DEPTH",
    "CODE_QUERY_DEFAULT_LIMIT",
    "CODE_QUERY_INTENTS",
    "CODE_QUERY_INTENT_CALLEES",
    "CODE_QUERY_INTENT_CALLERS",
    "CODE_QUERY_INTENT_CODE_CONTEXT",
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
CODE_QUERY_INTENT_CODE_CONTEXT: Final = "code_context"
CODE_QUERY_INTENTS: Final = frozenset(
    {
        CODE_QUERY_INTENT_DEFINITION,
        CODE_QUERY_INTENT_REFERENCES,
        CODE_QUERY_INTENT_CALLERS,
        CODE_QUERY_INTENT_CALLEES,
        CODE_QUERY_INTENT_DEPENDENCIES,
        CODE_QUERY_INTENT_CODE_CONTEXT,
    }
)
#: The intents that walk relations and therefore take a depth.
_TRAVERSING: Final = frozenset(
    {
        CODE_QUERY_INTENT_CALLERS,
        CODE_QUERY_INTENT_CALLEES,
        CODE_QUERY_INTENT_DEPENDENCIES,
        CODE_QUERY_INTENT_CODE_CONTEXT,
    }
)

CODE_CONTEXT_SEED_ORIGIN_LEXICAL: Final = "lexical"
CODE_CONTEXT_SEED_ORIGIN_SEMANTIC: Final = "semantic"
#: How a seed may have been selected. Closed: an origin this contract cannot name is an
#: origin whose binding discipline it cannot vouch for.
CODE_CONTEXT_SEED_ORIGINS: Final = frozenset(
    {CODE_CONTEXT_SEED_ORIGIN_LEXICAL, CODE_CONTEXT_SEED_ORIGIN_SEMANTIC}
)
CODE_CONTEXT_MAX_SEEDS: Final = 64
#: The ceiling on one disclosed span's captured bytes, matching `CodeSpanContent.text`'s
#: own `maxLength`. A span longer than this is returned truncated and says so, never cut
#: silently and never returned whole.
CODE_CONTEXT_MAX_SPAN_BYTES: Final = 4096

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
    _validate_context_fields(value)
    if value.semantic_key is None and value.name is None and value.seed is None:
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


def _validate_context_fields(value: CodeQueryInput) -> None:
    """The two fields only `code_context` accepts, and the one it requires.

    `context` is required by `code_context` and refused by every other intent, because a
    task-context request an intent would not act on is a request this answer cannot
    account for. `seed` is optional even for `code_context`: a caller that already knows
    its subject names it directly, and only a caller whose subject came out of a search
    has a binding to prove.
    """
    contextual = value.intent == CODE_QUERY_INTENT_CODE_CONTEXT
    if value.seed is not None:
        if not contextual:
            raise ContractSemanticError("seed is only for code_context")
        _validate_seed(value.seed)
    if value.context is None:
        if contextual:
            raise ContractSemanticError("code_context requires a context request")
        return
    if not contextual:
        raise ContractSemanticError("context is only for code_context")
    validate_context_pack_build_input(value.context)


def _validate_seed(seed: CodeContextSeed) -> None:
    """Every member of a seed's own binding, under the guards the request's binding gets."""
    if seed.origin not in CODE_CONTEXT_SEED_ORIGINS:
        raise ContractSemanticError("seed origin is not lexical or semantic")
    for name in ("repository_id", "snapshot_id", "producer_id"):
        if not is_identifier(getattr(seed, name)):
            raise ContractSemanticError(f"seed {name} is not an Identifier")
    if not is_content_checksum(seed.profile_digest):
        raise ContractSemanticError("seed profile_digest is not a ContentChecksum")
    if not 1 <= len(seed.semantic_keys) <= CODE_CONTEXT_MAX_SEEDS:
        raise ContractSemanticError("a seed names 1-64 semantic keys")
    if not all(is_code_semantic_key(key) for key in seed.semantic_keys):
        raise ContractSemanticError("a seed semantic key is not a code semantic key")
