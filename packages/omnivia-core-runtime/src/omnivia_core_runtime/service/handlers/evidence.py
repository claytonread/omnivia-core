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
6. narrow the projection's materialised token material to exactly that frontier's ids,
   producing an immutable projected frontier;
7. rank, which is the first step that selects or orders, and the first step that sees
   the query;
8. map frontier members to the result page.

Step 5 is the line packet §7.2 draws, and steps 6 to 8 are on the far side of it. The
page is built by mapping over ranked frontier members, so "every item in any result is
a member of the frozen frontier" holds by construction rather than by a check that
could be skipped.

**Steps 6 and 7 are two steps on purpose, and that is the one architectural claim of
this lane a reviewer should check rather than accept.** §20.12's proof about Lane A's
ranker was an *import boundary*: `storage/retrieval.py` holds no `sqlite3`, no
connection and no callback, so an unfiltered candidate was unreachable rather than
merely forbidden. The obvious way to add an FTS5 ordering gives that up -- the index is
in SQLite, so a ranker that reads it holds a connection -- and it gives up more than the
proof, because `bm25()` computes its inverse document frequency and its average document
length over the whole index. An artifact the filter chain excluded then moves the
relative order of the members that *are* returned, invisibly, because every id in the
page is authorized either way.

So the projection is narrowed before anything is ranked. `SearchProjection.project`
addresses the session's material by the frozen frontier's ids and hands back a value
carrying those candidates and their token sequences; `retrieval.rank_projected` takes
that value and recomputes every statistic from it. The ranker keeps Lane A's import
boundary exactly, and an excluded artifact is absent from the numbers rather than merely
absent from the page.

What the handler owns is that there is no second path: the material is produced by the
service's own startup before this endpoint accepts anything, a request that finds none
is refused, and no branch below falls back to an unindexed scan or to the artifact's
authoritative `search_text`. Falling back would answer a caller successfully from an
ordering this build does not claim to serve, which is worse than the retryable refusal
it replaces.

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
from omnivia_core_runtime.storage.projections.fts import (
    ProjectionUnavailable,
    SearchProjection,
    StaleProjection,
    require_current,
    session_search_projection,
)
from omnivia_core_runtime.storage.repository import (
    CONTRIBUTING_PROJECTIONS,
    authoritative_checkpoint,
    projection_readiness,
    read_evidence_candidates,
)
from omnivia_core_runtime.storage.retrieval import (
    AuthorizedFrontier,
    ProjectedFrontier,
    authorized_frontier,
    local_owner_label_grant,
    rank_projected,
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

    # Ordering, from the projection this build actually serves and from nothing else.
    # The material was produced by the service's own startup path before this endpoint
    # accepted anything, so what is reached for here either exists or the request is
    # refused -- there is no Lane A ordering behind this call to fall back to, and a
    # fallback would answer from an unindexed substring scan while reporting success.
    projection = session_search_projection(connection)
    if projection is None:
        raise OperationError(
            ERROR_CODE_PROJECTION_UNAVAILABLE,
            _MESSAGE_PROJECTION_UNAVAILABLE,
            retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        )
    # Two statements, and the order of the two is the architecture. The adapter narrows
    # the projection's material *by the frozen frontier's ids* and returns a value; the
    # pure ranker then computes every score and every statistic from that value alone.
    # Neither half can be handed the corpus: the first is given the frontier, and the
    # second is given only what the first returned.
    projected = _projected(connection, projection, frontier)
    ranked = rank_projected(
        projected, request_input.query, limit=_limit(request_input)
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


def _projected(
    connection: Any,
    projection: SearchProjection,
    frontier: AuthorizedFrontier,
) -> ProjectedFrontier:
    """The frozen frontier with its projection material, or the same two refusals.

    The freshness gate above already ran, and `require_current` is not a repeat of it: it
    re-proves per request that the run this material was built from is still the
    activated one and still level with the workspace, so a projection that moved between
    the gate and this line refuses here instead of answering from material that no longer
    describes the workspace. Refusing twice costs two queries; serving once from stale
    material is the silent staleness §20.7 exists to forbid.

    `project` refuses too, for the other reason: an authorized candidate the material has
    no document for. That is retryable and it is deliberately not a fallback -- the
    artifact's authoritative `search_text` would answer the request from outside the
    projection this build claims to serve.

    The sentinel-then-raise shape is this tree's convention and it is load-bearing:
    raising inside the `except` would leave the projection's own message -- which names
    run ids and checkpoints -- reachable through `__context__` on the error a caller
    catches. `scripts/check-raise-discipline.py` enforces it over this directory.
    """
    refused: str | None = None
    try:
        require_current(connection, projection)
        return projection.project(frontier)
    except ProjectionUnavailable:
        refused = ERROR_CODE_PROJECTION_UNAVAILABLE
    except StaleProjection:
        refused = ERROR_CODE_STALE_PROJECTION
    if refused == ERROR_CODE_STALE_PROJECTION:
        raise OperationError(
            ERROR_CODE_STALE_PROJECTION,
            _MESSAGE_STALE_PROJECTION,
            retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        )
    raise OperationError(
        ERROR_CODE_PROJECTION_UNAVAILABLE,
        _MESSAGE_PROJECTION_UNAVAILABLE,
        retry_class=RETRY_CLASS_RETRYABLE_AFTER_DELAY,
    )


def _limit(request_input: EvidenceSearchInput) -> int:
    """The page size this request gets, clamped to what the catalogue allows."""
    requested = request_input.limit
    if requested is None:
        return DEFAULT_PAGE_LIMIT
    return min(int(requested), MAX_PAGE_LIMIT)


__all__ = ["DEFAULT_PAGE_LIMIT", "MAX_PAGE_LIMIT", "evidence_search"]
