"""The `knowledge.search` and `memory.search` handlers -- governed truth, read directly.

`evidence.search` beside this one reads L0 artifacts through a projection and refuses when
that projection is missing or lagging. These two do neither, and the difference is the
architectural claim of this lane rather than an omission: a governed read resolves sealed
0009 rows inside one read transaction, so it is *fresh at its own transaction point*
(§20.7). There is no index to be level with, nothing to report staleness against, and
therefore nothing to refuse for. This module imports no projection, and no branch below
falls back to one -- a fallback would make an authoritative read depend on a derived
artifact that is allowed to lag it.

Both operations are one path, because `MemorySearchInput` is `KnowledgeSearchInput` minus
`domain_scope` and the contract validates their results through one shared rule. Two
handlers spelling that path twice is two places for the view semantics to drift, which is
the failure `_validate_governed_search_result` exists in the contract to prevent. What each
handler owns is its own decoder, its own frozen refusal message, its own result type and
its own validator; `_search` owns the read, the filters, the freeze and the ranking.

The order below is the security property, and it is the governed twin of the one
`evidence.py` states:

1. decode and fully validate the payload; take the workspace from the *authorized* context;
2. require the authoritative connection;
3. fix **one** resolution instant, in microseconds, from one clock read;
4. resolve and hydrate the governed view at that instant -- which is where the workspace,
   view, governance and temporal filters run, in `storage/governed.py`, under one snapshot;
5. apply the request's own selectors: `record_type`, and `domain_scope` for the operation
   that has one;
6. **freeze the governed frontier**, naming every filter that ran;
7. rank, which is the first step that selects or orders and the first that sees the query;
8. validate the completed result against the original typed request, the authorized
   workspace, that same instant, and the server's own view grant, before it goes to wire.

Steps 6 and 7 are §7.2's line. `GovernedFrontier` carries no connection, no cursor and no
id to resolve, so there is no post-freeze read to forbid -- the material to widen a page
with is absent rather than merely out of bounds, and `rank_governed`'s own module cannot
import a store (§20.12, proven in `test_knowledge_search_vertical.py`).

**One instant, three jobs.** The same microsecond value resolves the view, decides temporal
validity inside that resolution, and -- rendered once, from the value already held -- is
what the contract validator checks each returned record's validity window against. A second
clock read would let a record be selected at one instant and judged at another, which under
a window closing between the two is a refusal of a page that was correct when it was built.

**The view grant is the server's, not the request's.** `validate_*_search_result` takes an
`authorized_views` set and refuses an explicitly named view that is not in it. That set is
:data:`LOCAL_OWNER_VIEW_GRANT`, a module constant defined without reference to the request,
so naming `history` cannot authorize `history`: the string selects *which* view, and this
constant decides whether it may be reached at all.

**The workspace is the authorized one.** `context.workspace_id` comes from the session grant
and the endpoint binding, after the seam refused every workspace they disagreed on. Neither
input carries a workspace or a principal field by contract, and neither is read.

**Refusals carry no caller value.** Every message is a frozen module constant, and the
decode failures are contained rather than chained: the contract's decode errors quote the
payload they rejected, so the sentinel is set inside the handler and the refusal raised
after it exits, leaving `__context__` genuinely `None`. `scripts/check-raise-discipline.py`
enforces that over this directory.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    GOVERNED_RECORD_VIEW_CANDIDATES,
    GOVERNED_RECORD_VIEW_HISTORY,
    KNOWLEDGE_SEARCH_DEFAULT_RESULT_LIMIT,
    ContractDecodeError,
    ContractSemanticError,
    GovernedRecord,
    KnowledgeSearchInput,
    KnowledgeSearchResult,
    MemorySearchInput,
    MemorySearchResult,
    PageMetadata,
    decode_knowledge_search_input,
    validate_knowledge_search_result,
)

#: `omnivia_core.contracts.v1` re-exports `knowledge.search`'s validators but not
#: `memory.search`'s twins, so they are reached through the module that declares them in
#: its `__all__`. Closing that export gap is a contracts change and this lane owns none.
from omnivia_core.contracts.v1.semantics_knowledge import (
    validate_memory_search_input,
    validate_memory_search_result,
)
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage.governed import read_governed_record_values
from omnivia_core_runtime.storage.retrieval import (
    GOVERNED_FRONTIER_FILTERS,
    GovernedCandidate,
    GovernedFrontier,
    rank_governed,
)

#: The page size a request that names none gets, and the ceiling every request is clamped
#: to. One number doing both jobs because the contract states it once:
#: :data:`~omnivia_core.contracts.v1.KNOWLEDGE_SEARCH_DEFAULT_RESULT_LIMIT` *is* the
#: `maxItems` of `KnowledgeSearchResult.records` and `MemorySearchResult.records`. The clamp
#: is not decoration -- `PageLimit` admits up to 1000, so a valid request can ask for twice
#: what its own result document is allowed to hold.
MAX_RESULT_LIMIT: Final = KNOWLEDGE_SEARCH_DEFAULT_RESULT_LIMIT

#: The views this build's local owner may reach by naming one. Defined here, from the
#: contract's own two constants and from nothing about the request, because it is the answer
#: to "may this caller see that view" -- a question the request cannot be allowed to answer
#: for itself. `current_canonical` is absent deliberately: it is the resolution of an absent
#: selector and the contract never gates it on this set, so listing it would suggest a gate
#: that does not exist.
LOCAL_OWNER_VIEW_GRANT: Final[frozenset[str]] = frozenset(
    {GOVERNED_RECORD_VIEW_CANDIDATES, GOVERNED_RECORD_VIEW_HISTORY}
)

#: Refusal messages, frozen as constants for the reason every refusal on this path is: a
#: handler failure becomes a wire `ApiError` a caller reads, and nothing about this server's
#: state or this caller's own values may travel there. The two decode messages are separate
#: strings so a refusal says which operation refused without quoting anything to prove it.
_MESSAGE_INVALID_KNOWLEDGE_INPUT: Final = (
    "the request payload is not a valid knowledge search"
)
_MESSAGE_INVALID_MEMORY_INPUT: Final = "the request payload is not a valid memory search"
_MESSAGE_NO_STORAGE: Final = "this service instance is not serving authoritative storage"


def knowledge_search(context: OperationContext) -> Mapping[str, Any]:
    """Answer one `knowledge.search` over the frozen governed frontier."""
    request_input = _knowledge_input(context)
    resolved_at_us, records = _search(
        context, request_input, domain_scope=request_input.domain_scope
    )
    result = KnowledgeSearchResult(
        records=records,
        # Exhaustion is stated, never implied by an absent field. This build issues no
        # continuation tokens, and an empty page is a successful answer: a workspace whose
        # canonical view holds nothing knows nothing yet, which is not a `not_found`.
        page=PageMetadata(),
    )
    validate_knowledge_search_result(
        result,
        request_input,
        context.workspace_id,
        _timestamp(resolved_at_us),
        LOCAL_OWNER_VIEW_GRANT,
    )
    return result.to_wire()


def memory_search(context: OperationContext) -> Mapping[str, Any]:
    """Answer one `memory.search`: `knowledge.search` without a domain selector."""
    request_input = _memory_input(context)
    resolved_at_us, records = _search(context, request_input, domain_scope=None)
    result = MemorySearchResult(records=records, page=PageMetadata())
    validate_memory_search_result(
        result,
        request_input,
        context.workspace_id,
        _timestamp(resolved_at_us),
        LOCAL_OWNER_VIEW_GRANT,
    )
    return result.to_wire()


def _search(
    context: OperationContext,
    request_input: KnowledgeSearchInput | MemorySearchInput,
    *,
    domain_scope: str | None,
) -> tuple[int, tuple[GovernedRecord, ...]]:
    """The read, the filters, the freeze and the ranking -- shared by both operations.

    `domain_scope` is a parameter rather than an attribute read off `request_input` because
    only one of the two inputs declares the field. Passing `None` for `memory.search` states
    that it has no domain selector, rather than leaving a `getattr` to make the absence look
    like an unset value.

    Returns the resolution instant alongside the page, because the caller has to validate
    the result at *that* instant and there is exactly one of it per request.
    """
    connection = getattr(getattr(context, "service", None), "connection", None)
    if connection is None:
        raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)

    # The canonical resolution instant. One clock read, before anything is resolved, and
    # the only one on this path.
    resolved_at_us = time.time_ns() // 1000

    # Workspace, view, governance and temporal validity, all four inside one read
    # transaction. They are the resolver's rather than this handler's on purpose: they are
    # decided by the same instant that selects the versions, and re-applying any of them
    # here would be a second opinion about a snapshot this module cannot re-read.
    values = read_governed_record_values(
        connection,
        workspace_id=context.workspace_id,
        resolution_instant_us=resolved_at_us,
        view=request_input.view,
    )

    # The freeze, and the request's own two selectors immediately before it. Everything
    # below this statement sees a frozen value and nothing else.
    frontier = GovernedFrontier(
        workspace_id=context.workspace_id,
        candidates=tuple(
            GovernedCandidate(recorded_at_us=value.recorded_at_us, record=value.record)
            for value in values
            if (
                request_input.record_type is None
                or value.record.record_type == request_input.record_type
            )
            and (domain_scope is None or value.record.domain_scope == domain_scope)
        ),
        filters_applied=GOVERNED_FRONTIER_FILTERS,
    )
    return resolved_at_us, rank_governed(
        frontier,
        request_input.query,
        order=request_input.order,
        limit=_limit(request_input.limit),
    )


def _knowledge_input(context: OperationContext) -> KnowledgeSearchInput:
    """The payload as a validated `KnowledgeSearchInput`, or a refusal that quotes nothing.

    The sentinel-then-raise shape is this tree's convention and it is load-bearing: both
    contract errors quote the payload they rejected, and raising inside the handler would
    leave that text reachable through `__context__` on the exception a caller catches.
    """
    decoded: KnowledgeSearchInput | None
    try:
        decoded = decode_knowledge_search_input(context.request.input)
    except (ContractDecodeError, ContractSemanticError):
        decoded = None
    if decoded is None:
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_KNOWLEDGE_INPUT)
    return decoded


def _memory_input(context: OperationContext) -> MemorySearchInput:
    """The same, for `memory.search`, in two statements rather than one.

    There is no `decode_memory_search_input`, and that is the contract's shape rather than
    an oversight, so the decode and the semantic validation are spelled out here. Both are
    required: `from_wire` alone accepts an unrecognized `view` or `order`, which is exactly
    the fail-closed check `validate_memory_search_input` performs. The intermediate value is
    only promoted to `decoded` once *both* have passed, so a payload that decodes and then
    fails validation refuses like one that never decoded.
    """
    decoded: MemorySearchInput | None
    try:
        candidate = MemorySearchInput.from_wire(context.request.input)
        validate_memory_search_input(candidate)
        decoded = candidate
    except (ContractDecodeError, ContractSemanticError):
        decoded = None
    if decoded is None:
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_MEMORY_INPUT)
    return decoded


def _limit(requested: int | None) -> int:
    """The page size this request gets, clamped to what the result document may hold."""
    if requested is None:
        return MAX_RESULT_LIMIT
    return min(int(requested), MAX_RESULT_LIMIT)


def _timestamp(microseconds: int) -> str:
    """One microsecond instant as the contract's `Timestamp`.

    Millisecond precision with a `Z` suffix, sub-millisecond digits truncated rather than
    rounded -- the same rendering `storage/governed.py` gives every instant it publishes,
    which is what makes the comparison the result validator performs a comparison between
    two values rendered the same way. Truncation is monotone, so a record the resolver
    admitted as valid at this instant stays valid against the rendered form of it.
    """
    moment = datetime.fromtimestamp(microseconds / 1_000_000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


__all__ = [
    "LOCAL_OWNER_VIEW_GRANT",
    "MAX_RESULT_LIMIT",
    "knowledge_search",
    "memory_search",
]
