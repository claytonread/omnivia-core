"""The `graph.traverse` handler -- governed relations, walked over one fenced snapshot.

The fifth production application operation, and the governed twin of `knowledge.search`
rather than of `evidence.search`: it reads sealed 0009 rows directly, inside one read
transaction, so it is *fresh at its own transaction point* (§20.7). No projection is built,
attached, consulted or refused for -- this module imports none, and no branch below falls
back to one.

`GraphTraversalResult` nevertheless carries a mandatory `ProjectionFreshness`, and that is
not a contradiction to be papered over. The contract requires a read to say which
projection it was served from and how far behind the write model that projection is, so
this handler states the honest answer for a direct authoritative read: one stable logical
projection id, a version fixed by this build, a watermark taken from the *same fenced
snapshot* the nodes and edges came from, and `stale=False` -- because a read of the write
model cannot lag itself. The watermark is deliberately not a clock read and not a second
query: a value fetched after the transaction closed would describe a database state this
answer was never built from, which is precisely the silent staleness §20.7 forbids.

The order below is the security property, and it is the same order the two governed
searches beside it state:

1. decode and fully validate the payload; take the workspace from the *authorized* context;
2. refuse a continuation page, which this build cannot have issued;
3. require the authoritative connection;
4. fix **one** resolution instant -- the request's own `as_of` when it named one, otherwise
   one clock read, and the only one on this path;
5. read the whole snapshot at that instant, under **one SQLite read transaction** -- the
   resolved view, its hydrated records, the sealed relation endpoints and the watermark.
   That single transaction is what makes the five facts one database state; the authorizer
   fence is not one continuous scope over them but several read-only ones, opened and closed
   around the statements inside it (`storage/governed.py` fences its own reads, and
   `storage/graph.py` fences the endpoint and watermark queries in a second scope). The
   transaction is the snapshot property; each fence is the separate, narrower guarantee that
   no statement inside it can write;
6. walk it with a pure function that holds no connection, so nothing outside the snapshot
   can enter the answer;
7. refuse if any requested seed is not a version this view holds -- a traversal owes every
   seed at depth 0 and may not quietly answer a narrower question;
8. refuse if the complete answer does not fit the requested node and edge limits, because
   this build has no continuation token to hand the remainder back with;
9. validate the completed result against the original typed request, the authorized
   workspace, that same instant, and the server's own view grant, before it goes to wire.

**Lane E is a partial checkpoint.** Ordinary governed relations are assembled and served
here. Amendment 010's derived `record.superseded` edges are not. Its base supersession
mapping and the history/current behaviour it prescribes are **already approved**; they are
simply *unimplemented in this checkpoint*, along with their watermark contribution, and
nothing on this path approximates any of them. Exactly one question remains open and
pending owner ratification: the precedence rule for an ordinary edge and a derived edge that
would share one `relation_reference`, which the contract's result validator refuses as a
duplicate. `storage/graph.py` states the same division.

Step 5 is this operation's freeze. `GraphSnapshot` carries no connection, no cursor and no
id to resolve, so there is no post-freeze read to forbid -- the material to widen an answer
with is absent rather than merely out of bounds.

**The view grant is the server's, not the request's.** `validate_graph_traversal_result`
takes an `authorized_views` set and refuses an explicitly named view outside it. That set is
`knowledge.LOCAL_OWNER_VIEW_GRANT`, reused rather than restated: a second copy would be a
second place for `graph.traverse` and `knowledge.search` to disagree about which views this
build's local owner may reach, which is exactly the side channel the contract's own
`_validate_graph_record_under_view` exists to close.

**The workspace is the authorized one.** `context.workspace_id` comes from the session grant
and the endpoint binding, after the seam refused every workspace they disagreed on.
`GraphTraversalInput` carries no workspace or principal field by contract, and neither is read.

**Refusals carry no caller value.** Every message is a frozen module constant, and the decode
failure is contained rather than chained: the contract's decode errors quote the payload they
rejected, so the sentinel is set inside the helper and the refusal raised after it exits,
leaving `__context__` genuinely `None`. `scripts/check-raise-discipline.py` enforces that
over this directory.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    GRAPH_DEFAULT_DEPTH_LIMIT,
    MAX_PAGE_LIMIT,
    ContractDecodeError,
    ContractSemanticError,
    GraphTraversalInput,
    GraphTraversalResult,
    PageMetadata,
    ProjectionFreshness,
    resolve_graph_direction,
    validate_graph_traversal_input,
    validate_graph_traversal_result,
)
from omnivia_core.contracts.v1.semantics_knowledge import (
    GRAPH_ORDERING_BASIS_DEPTH_RECORD_VERSION_ASC,
)
from omnivia_core_runtime.service.handlers.knowledge import LOCAL_OWNER_VIEW_GRANT
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage.graph import (
    RecordKey,
    read_graph_snapshot,
    traverse,
)

#: The one logical projection this operation reports itself as served from. Stable across
#: every answer this build gives, because the contract's freshness statement is keyed by
#: projection name and a name that moved would make two answers incomparable. An `OpenCode`,
#: as `ProjectionFreshness`'s own map keys require.
GRAPH_PROJECTION: Final = "graph.governed_relations"

#: This build's version of that logical projection: the shape of the traversal it serves,
#: not a data version. The *watermark* is what moves with the workspace, and the two are
#: separate statements on purpose -- a caller compares versions to know whether the projection
#: means the same thing, and watermarks to know how far behind the write model it is.
GRAPH_PROJECTION_VERSION: Final = "1"

#: The node and edge budgets a request that names none gets. The catalogue's own
#: `max_page_size` for this operation, and deliberately not a smaller convenience default:
#: this build issues no continuation token, so a default below the ceiling would silently
#: drop nodes a caller had no way to ask for again.
DEFAULT_NODE_LIMIT: Final = MAX_PAGE_LIMIT
DEFAULT_EDGE_LIMIT: Final = MAX_PAGE_LIMIT

#: Refusal messages, frozen as constants for the reason every refusal on this path is: a
#: handler failure becomes a wire `ApiError` a caller reads, and nothing about this server's
#: state or this caller's own values may travel there.
_MESSAGE_INVALID_INPUT: Final = "the request payload is not a valid graph traversal"
_MESSAGE_NO_CONTINUATION: Final = (
    "this build issues no graph traversal continuation token, so none can be continued from"
)
_MESSAGE_NO_STORAGE: Final = "this service instance is not serving authoritative storage"
_MESSAGE_UNKNOWN_START: Final = (
    "the requested traversal start is not a record version this view holds"
)
_MESSAGE_TOO_LARGE: Final = (
    "the complete traversal at the requested depth exceeds the requested node or edge "
    "limit, and this build issues no continuation token to return the remainder from"
)

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def graph_traverse(context: OperationContext) -> Mapping[str, Any]:
    """Answer one `graph.traverse` over one fenced governed snapshot."""
    request_input = _input(context)
    if request_input.page is not None:
        # A token this build never issued cannot name a position it can continue from, and
        # the contract reads a present request page as a positive statement that this
        # traversal's seeds were already returned by an earlier page -- which would be
        # false. Refused rather than ignored.
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_NO_CONTINUATION)

    connection = getattr(getattr(context, "service", None), "connection", None)
    if connection is None:
        raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)

    resolved_at_us, resolved_at = _instant(request_input)
    depth_limit = (
        GRAPH_DEFAULT_DEPTH_LIMIT
        if request_input.depth_limit is None
        else int(request_input.depth_limit)
    )
    node_limit = _limit(request_input.node_limit, DEFAULT_NODE_LIMIT)
    edge_limit = _limit(request_input.edge_limit, DEFAULT_EDGE_LIMIT)

    # The freeze. Everything below this call sees frozen values and nothing else: the view,
    # the hydrated records, the sealed relations and the watermark, all from one snapshot.
    snapshot = read_graph_snapshot(
        connection,
        workspace_id=context.workspace_id,
        resolution_instant_us=resolved_at_us,
        view=request_input.view,
    )
    seeds = tuple(
        (reference.record_id, reference.version) for reference in request_input.start
    )
    nodes, edges = traverse(
        snapshot,
        seeds=seeds,
        direction=resolve_graph_direction(request_input.direction),
        relation_types=(
            None
            if request_input.relation_types is None
            else frozenset(request_input.relation_types)
        ),
        domain_scope=request_input.domain_scope,
        depth_limit=depth_limit,
    )

    # Every seed, at depth 0, or nothing. A seed the resolved view does not hold -- because
    # it never existed, because it is superseded, because it is in another workspace, or
    # because the requested `domain_scope` excludes it -- makes the whole question
    # unanswerable rather than partially answerable, and the contract says so: a first page
    # owes every identity it was asked to start from.
    returned: frozenset[RecordKey] = frozenset(
        (node.reference.record_id, node.reference.version) for node in nodes
    )
    if not returned.issuperset(seeds):
        raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_UNKNOWN_START)

    # The complete deterministic answer, or none of it. `traverse` applies no budget, so
    # what came back is the whole result at this depth; if it does not fit the limits this
    # request asked for, there is no honest way to return it. This build issues no
    # continuation token, so a sliced page would be a positive claim of exhaustion
    # (`PageMetadata()`) over content silently withheld, and the caller would have no field
    # to read it off and no token to ask for the rest. Refused instead.
    if len(nodes) > node_limit or len(edges) > edge_limit:
        raise OperationError(ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_TOO_LARGE)

    result = GraphTraversalResult(
        nodes=nodes,
        edges=edges,
        applied_depth_limit=depth_limit,
        applied_node_limit=node_limit,
        applied_edge_limit=edge_limit,
        freshness=ProjectionFreshness(
            as_of=resolved_at,
            projection_versions={GRAPH_PROJECTION: GRAPH_PROJECTION_VERSION},
            # The same snapshot's own high-water instant. One key, identical to the version
            # map's, because they are one statement about one projection.
            projection_watermarks={GRAPH_PROJECTION: str(snapshot.watermark_us)},
            # A direct authoritative read cannot lag itself. This is a claim about the
            # snapshot above, not an optimistic default.
            stale=False,
        ),
        ordering_basis=GRAPH_ORDERING_BASIS_DEPTH_RECORD_VERSION_ASC,
        # Exhaustion is stated, never implied by an absent field -- and it is true rather
        # than convenient, because the refusal above is what rules out the case where it
        # would not be. This build issues no continuation tokens, which is also why no edge
        # here carries a `page_boundary`; the depth boundaries it does carry need none.
        page=PageMetadata(),
    )
    validate_graph_traversal_result(
        result,
        request_input,
        context.workspace_id,
        resolved_at,
        LOCAL_OWNER_VIEW_GRANT,
    )
    return result.to_wire()


def _input(context: OperationContext) -> GraphTraversalInput:
    """The payload as a validated `GraphTraversalInput`, or a refusal that quotes nothing.

    There is no `decode_graph_traversal_input`; that is the contract's shape rather than an
    oversight, so the decode and the semantic validation are spelled out here exactly as
    `memory.search`'s twin spells them. Both are required: `from_wire` alone accepts an
    unrecognized `direction` or `view`, a 65-seed start list and an out-of-range depth, all
    of which `validate_graph_traversal_input` fails closed on. The intermediate value is
    promoted to `decoded` only once *both* have passed.

    The sentinel-then-raise shape is this tree's convention and it is load-bearing: both
    contract errors quote the payload they rejected, and raising inside the handler would
    leave that text reachable through `__context__` on the exception a caller catches.
    """
    decoded: GraphTraversalInput | None
    try:
        candidate = GraphTraversalInput.from_wire(context.request.input)
        validate_graph_traversal_input(candidate)
        decoded = candidate
    except (ContractDecodeError, ContractSemanticError):
        decoded = None
    if decoded is None:
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_INPUT)
    return decoded


def _instant(request_input: GraphTraversalInput) -> tuple[int, str]:
    """The one instant this traversal resolves at, as microseconds and as a `Timestamp`.

    A requested `as_of` *is* the resolution instant -- the contract requires the result to
    be served at the instant it was asked for, and returning the caller's own string
    unchanged makes the two equal by construction rather than by a rendering that has to
    round-trip exactly. It has already passed `validate_graph_traversal_input`, so it parses.

    With no `as_of`, one clock read, before anything is resolved, and the only one on this
    path. That read fixes *when* the traversal resolves; it is emphatically not where the
    freshness watermark comes from, which is the snapshot itself.
    """
    if request_input.as_of is not None:
        return _microseconds(request_input.as_of), request_input.as_of
    now_us = time.time_ns() // 1000
    return now_us, _timestamp(now_us)


def _limit(requested: int | None, default: int) -> int:
    """One page budget, clamped to what the catalogue allows this operation to return."""
    if requested is None:
        return default
    return min(int(requested), MAX_PAGE_LIMIT)


def _microseconds(timestamp: str) -> int:
    """One contract `Timestamp` as microseconds since the epoch, by integer arithmetic.

    `//` on a `timedelta` rather than a float multiplication of `.timestamp()`, because the
    float form is not exact at microsecond resolution and a resolution instant that drifted
    by one microsecond would select a different frontier than the one the caller asked for.
    """
    return (datetime.fromisoformat(timestamp) - _EPOCH) // timedelta(microseconds=1)


def _timestamp(microseconds: int) -> str:
    """One microsecond instant as the contract's `Timestamp`.

    Millisecond precision with a `Z` suffix, sub-millisecond digits truncated rather than
    rounded -- the same rendering `storage/governed.py` gives every instant it publishes,
    which is what makes the validator's comparisons comparisons between values rendered the
    same way.
    """
    moment = datetime.fromtimestamp(microseconds / 1_000_000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


__all__ = [
    "DEFAULT_EDGE_LIMIT",
    "DEFAULT_NODE_LIMIT",
    "GRAPH_PROJECTION",
    "GRAPH_PROJECTION_VERSION",
    "graph_traverse",
]
