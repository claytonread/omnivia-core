"""The `evidence.search` handler -- the first application handler that reads storage.

`workspace.inspect` before it opened nothing. This one does, and the order of what it
does is the security property of the whole V06-3 stage, so it is written to be read
top to bottom:

1. decode the request payload, and take the workspace from the *authorized* context;
2. refuse if a contributing projection is missing or lags the read point;
3. read every candidate in the workspace, under a fenced read;
4. build the evidence-label grant for this principal in this workspace;
5. **freeze the authorized frontier** -- workspace, ACL, sensitivity, tombstone and
   temporal filters, all of them, before anything below this line;
6. rank, which is the first step that selects or orders, and the first step that sees
   the query;
7. map frontier members to the result page.

Step 5 is the line packet §7.2 draws, and steps 6 and 7 are on the far side of it. The
ranker is handed `frontier` and cannot reach anything else -- see
`storage/retrieval.py`, whose import block is the proof. The page is built by mapping
over ranked frontier members, so "every item in any result is a member of the frozen
frontier" holds by construction rather than by a check that could be skipped.

**The workspace is the authorized one.** `context.workspace_id` comes from the session
grant and the endpoint binding after the seam refused every workspace they disagreed
on. `EvidenceSearchInput` carries no workspace field by contract, and none is read.

**Two things `retry_class` and this handler get right that no test enforces.**
`OperationError` defaults to `non_retryable`, while `stale_projection` and
`projection_unavailable` are contractually retryable after a delay, and nothing in this
tree validates that the two agree at runtime. Every raise site below passes
`retry_class` explicitly for that reason -- an omission would be silently wrong on the
wire and green in CI.

**Refusals carry no caller value.** Every message is a frozen module constant. The
decode failure in particular is contained rather than chained: the contract's own
decode errors quote the payload they rejected, so the sentinel is set inside the
handler and the refusal raised after it exits, leaving `__context__` genuinely `None`.
That is this tree's stated convention and `scripts/check-raise-discipline.py` enforces
it over exactly this directory.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_PROJECTION_UNAVAILABLE,
    ERROR_CODE_STALE_PROJECTION,
    RETRY_CLASS_RETRYABLE_AFTER_DELAY,
    ContractDecodeError,
    ContractSemanticError,
    EvidenceSearchInput,
    EvidenceSearchResult,
    PageMetadata,
    decode_evidence_search_input,
)
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage.repository import (
    CONTRIBUTING_PROJECTIONS,
    authoritative_checkpoint,
    projection_readiness,
    read_evidence_candidates,
)
from omnivia_core_runtime.storage.retrieval import (
    authorized_frontier,
    local_owner_label_grant,
    rank_candidates,
)

#: The page size a request that names none gets. Well under the catalogue's
#: `max_page_size` of 1000, and stated here rather than defaulted implicitly so that a
#: result page's size is always a decision this build made.
DEFAULT_PAGE_LIMIT: Final = 50

#: The ceiling the frozen catalogue fixes for this operation. A request may ask for
#: less; it cannot ask for more, and the schema refuses a larger value before this
#: handler sees it.
MAX_PAGE_LIMIT: Final = 1000

#: Refusal messages, frozen as constants for the same reason the authorization seam's
#: are: a handler failure becomes a wire `ApiError` a caller reads, and nothing about
#: this server's state or this caller's own values may travel there.
_MESSAGE_INVALID_INPUT: Final = "the request payload is not a valid evidence search"
_MESSAGE_NO_STORAGE: Final = "this service instance is not serving authoritative storage"
_MESSAGE_STALE_PROJECTION: Final = (
    "a projection this search reads lags the authoritative source checkpoint"
)
_MESSAGE_PROJECTION_UNAVAILABLE: Final = (
    "this build has no active compatible projection for this search"
)


def evidence_search(context: OperationContext) -> Mapping[str, Any]:
    """Answer one `evidence.search` over the authorized frontier."""
    request_input = _decode(context)

    connection = getattr(getattr(context, "service", None), "connection", None)
    if connection is None:
        raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)

    workspace_id = context.workspace_id

    # Freshness, before any read that would answer the caller. Packet §20.7's clause
    # resolves to *refuse* rather than report for this operation, because
    # `EvidenceSearchResult` has no freshness field to report in and succeeding
    # silently from a lagging projection is the thing the clause forbids.
    checkpoint = authoritative_checkpoint(connection, workspace_id=workspace_id)
    readiness = projection_readiness(
        connection,
        workspace_id=workspace_id,
        source_checkpoint=checkpoint,
        contributing=CONTRIBUTING_PROJECTIONS,
    )
    if readiness.missing:
        raise OperationError(
            ERROR_CODE_PROJECTION_UNAVAILABLE,
            _MESSAGE_PROJECTION_UNAVAILABLE,
            retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        )
    if readiness.stale:
        raise OperationError(
            ERROR_CODE_STALE_PROJECTION,
            _MESSAGE_STALE_PROJECTION,
            retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        )

    candidates = read_evidence_candidates(connection, workspace_id=workspace_id)

    # The ACL stage's input: an explicit effective grant, evaluated per candidate by
    # the filter chain below. Not an absent check, not a bypass, not a default for an
    # unknown principal -- packet §20.3 forbids all three by name.
    #
    # `configured_principal` is left at its default, which is the constant in
    # `storage/retrieval.py` rather than anything reachable from this request. Passing
    # `context.principal` for both sides would compare a value with itself and admit
    # every principal, which is the bypass that decision names. A session for any other
    # principal reaches here and gets the empty grant.
    grant = local_owner_label_grant(
        principal_id=context.principal,
        workspace_id=workspace_id,
        granted_workspace=workspace_id,
    )

    # The freeze. Everything below this call sees a frozen value and nothing else.
    frontier = authorized_frontier(
        candidates,
        workspace_id=workspace_id,
        grant=grant,
        sensitivity=request_input.sensitivity,
        include_tombstoned=bool(request_input.include_tombstoned),
        # The read point. `authoritative_checkpoint` is this workspace's high-water
        # `recorded_at_us`, so resolving at it admits everything the fenced read saw
        # and nothing written after it -- which is what "fresh at their transaction
        # read point" means for a direct authoritative read (§20.7).
        resolution_time_us=int(checkpoint),
    )

    ranked = rank_candidates(
        frontier,
        request_input.query,
        limit=_limit(request_input),
    )
    return EvidenceSearchResult(
        evidence=tuple(candidate.artifact for candidate in ranked),
        # Exhaustion is stated, never implied by an absent field. This build issues no
        # continuation tokens -- pagination beyond the first page is not Lane A's, and
        # a token this build cannot honour would be a worse answer than an honest `{}`.
        page=PageMetadata(),
    ).to_wire()


def _decode(context: OperationContext) -> EvidenceSearchInput:
    """The request payload as a validated input, or a refusal that quotes nothing.

    The sentinel-then-raise shape is this tree's convention and it is load-bearing
    here: both contract errors quote the payload they rejected, and raising inside the
    handler would leave that text reachable through `__context__` on the exception a
    caller catches.
    """
    decoded: EvidenceSearchInput | None
    try:
        decoded = decode_evidence_search_input(context.request.input)
    except (ContractDecodeError, ContractSemanticError):
        decoded = None
    if decoded is None:
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_INPUT)
    return decoded


def _limit(request_input: EvidenceSearchInput) -> int:
    """The page size this request gets, clamped to what the catalogue allows."""
    requested = request_input.limit
    if requested is None:
        return DEFAULT_PAGE_LIMIT
    return min(int(requested), MAX_PAGE_LIMIT)


__all__ = ["DEFAULT_PAGE_LIMIT", "MAX_PAGE_LIMIT", "evidence_search"]
