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
2. bind any continuation page to the original request and frozen resolution instant;
3. require the authoritative connection;
4. fix **one** resolution instant -- the request's own `as_of` when it named one, otherwise
   one clock read, and the only one on this path;
5. read the whole snapshot at that instant, under **one SQLite read transaction** -- the
   resolved view, its hydrated records, the sealed relation endpoints, the authoritative
   supersessions and the watermark. That single transaction is what makes those facts one
   database state; the authorizer
   fence is not one continuous scope over them but several read-only ones, opened and closed
   around the statements inside it (`storage/governed.py` fences its own reads, and
   `storage/graph.py` fences the endpoint and watermark queries in a second scope). The
   transaction is the snapshot property; each fence is the separate, narrower guarantee that
   no statement inside it can write;
6. walk it with a pure function that holds no connection, so nothing outside the snapshot
   can enter the answer;
7. refuse if any requested seed is not a version this view holds -- a traversal owes every
   seed at depth 0 and may not quietly answer a narrower question;
8. slice the deterministic node order into snapshot-bound continuation pages, preserving
   crossing relations as explicit page boundaries; refuse only an edge page that exceeds
   its independent edge budget;
9. validate the completed result against the original typed request, the authorized
   workspace, that same instant, and the server's own view grant, before it goes to wire.

**Both kinds of edge are served here (Amendment 010, ratified 2026-08-10).** An ordinary
edge is the one a sealed `knowledge.relation` record asserts; a derived `record.superseded`
edge is one authoritative supersession, from the superseded version to its replacement, with
that replacement as both the relation reference and the edge record. Step 5 reads the
endpoints and the supersessions in the *same* transaction and materializes both in one pass,
so a `record.superseded` edge is subject to every rule below without exception: the seed
closure of step 7, the page budget of step 8, and the filters, direction, depth
and ordering the walk applies. Where a replacement `knowledge.relation` version would make
an ordinary edge and a derived edge share one `relation_reference` -- which the contract's
result validator refuses as a duplicate -- the explicit edge wins and only that one derived
edge is dropped. `storage/graph.py` states the rule and applies it.

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
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    GRAPH_BOUNDARY_REASON_PAGE,
    GRAPH_DEFAULT_DEPTH_LIMIT,
    MAX_PAGE_LIMIT,
    ContractDecodeError,
    ContractSemanticError,
    GraphEdge,
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
from omnivia_core_runtime.service.pagination import (
    PROCESS_CONTINUATION_TOKENS,
    token_digest,
)
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
#: the catalogue's maximum page size. Smaller request budgets are served through signed
#: continuation tokens rather than by truncation.
DEFAULT_NODE_LIMIT: Final = MAX_PAGE_LIMIT
DEFAULT_EDGE_LIMIT: Final = MAX_PAGE_LIMIT

#: Refusal messages, frozen as constants for the reason every refusal on this path is: a
#: handler failure becomes a wire `ApiError` a caller reads, and nothing about this server's
#: state or this caller's own values may travel there.
_MESSAGE_INVALID_INPUT: Final = "the request payload is not a valid graph traversal"
_MESSAGE_NO_STORAGE: Final = "this service instance is not serving authoritative storage"
_MESSAGE_UNKNOWN_START: Final = (
    "the requested traversal start is not a record version this view holds"
)
_MESSAGE_TOO_LARGE: Final = (
    "the traversal page exceeds the requested edge limit"
)
_TOKEN_KEYS: Final = frozenset({"b", "o", "s", "t", "v"})

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def graph_traverse(context: OperationContext) -> Mapping[str, Any]:
    """Answer one `graph.traverse` over one fenced governed snapshot."""
    request_input = _input(context)
    connection = getattr(getattr(context, "service", None), "connection", None)
    if connection is None:
        raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)

    depth_limit = (
        GRAPH_DEFAULT_DEPTH_LIMIT
        if request_input.depth_limit is None
        else int(request_input.depth_limit)
    )
    node_limit = _limit(request_input.node_limit, DEFAULT_NODE_LIMIT)
    edge_limit = _limit(request_input.edge_limit, DEFAULT_EDGE_LIMIT)
    binding = request_input.to_wire()
    binding.pop("page", None)
    binding_digest = token_digest(
        {
            "principal": context.principal,
            "workspace": context.workspace_id,
            "operation": "graph.traverse",
            "input": binding,
            "depth_limit": depth_limit,
            "node_limit": node_limit,
            "edge_limit": edge_limit,
        }
    )
    supplied: Mapping[str, Any] | None = None
    if request_input.page is not None:
        token = request_input.page.continuation_token
        assert token is not None
        try:
            supplied = PROCESS_CONTINUATION_TOKENS.decode(token)
        except (ValueError, ContractDecodeError, ContractSemanticError):
            pass
        if (
            supplied is None
            or set(supplied) != _TOKEN_KEYS
            or supplied.get("v") != 1
            or supplied.get("b") != binding_digest
        ):
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_INPUT)
        instant = supplied.get("t")
        if type(instant) is not int or instant <= 0:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_INPUT)
        resolved_at_us = instant
        resolved_at = (
            request_input.as_of
            if request_input.as_of is not None
            else _timestamp(resolved_at_us)
        )
    else:
        resolved_at_us, resolved_at = _instant(request_input)

    # The freeze. Everything below this call sees frozen values and nothing else: the view,
    # the hydrated records, the ordinary and derived relations and the watermark, all from
    # one snapshot.
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

    snapshot_digest = token_digest(
        {
            "watermark": snapshot.watermark_us,
            "nodes": [node.to_wire() for node in nodes],
            "edges": [edge.to_wire() for edge in edges],
        }
    )
    start = 0
    if supplied is not None:
        if supplied.get("s") != snapshot_digest:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_INPUT)
        offset = supplied.get("o")
        if type(offset) is not int or not 0 < offset < len(nodes):
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID_INPUT)
        start = offset

    page_nodes = nodes[start : start + node_limit]
    end = start + len(page_nodes)
    continuation = None
    if end < len(nodes):
        continuation = PROCESS_CONTINUATION_TOKENS.encode(
            {
                "b": binding_digest,
                "o": end,
                "s": snapshot_digest,
                "t": resolved_at_us,
                "v": 1,
            }
        )
    page_edges = _page_edges(
        edges,
        nodes=page_nodes,
        has_continuation=continuation is not None,
    )
    if len(page_edges) > edge_limit:
        raise OperationError(ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_TOO_LARGE)

    result = GraphTraversalResult(
        nodes=page_nodes,
        edges=page_edges,
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
        page=PageMetadata(continuation_token=continuation),
    )
    validate_graph_traversal_result(
        result,
        request_input,
        context.workspace_id,
        resolved_at,
        LOCAL_OWNER_VIEW_GRANT,
    )
    return result.to_wire()


def _page_edges(
    edges: tuple[GraphEdge, ...],
    *,
    nodes: tuple[Any, ...],
    has_continuation: bool,
) -> tuple[GraphEdge, ...]:
    """Edges anchored in this node page, with forward crossings stated as boundaries."""

    present = frozenset(
        (node.reference.record_id, node.reference.version) for node in nodes
    )
    page: list[GraphEdge] = []
    for edge in edges:
        source_key = (
            None
            if edge.source is None
            else (edge.source.record_id, edge.source.version)
        )
        target_key = (
            None
            if edge.target is None
            else (edge.target.record_id, edge.target.version)
        )
        source_here = source_key in present
        target_here = target_key in present
        if (source_here and target_here) or (
            source_here and edge.target is None
        ) or (target_here and edge.source is None):
            page.append(edge)
        elif has_continuation and source_here:
            page.append(
                replace(edge, target=None, boundary_reason=GRAPH_BOUNDARY_REASON_PAGE)
            )
        elif has_continuation and target_here:
            page.append(
                replace(edge, source=None, boundary_reason=GRAPH_BOUNDARY_REASON_PAGE)
            )
    return tuple(sorted(page, key=_edge_order))


def _edge_order(edge: GraphEdge) -> tuple[str, ...]:
    source = ("", "") if edge.source is None else (edge.source.record_id, edge.source.version)
    target = ("", "") if edge.target is None else (edge.target.record_id, edge.target.version)
    return (
        *source,
        edge.relation_type,
        *target,
        edge.relation_reference.record_id,
        edge.relation_reference.version,
    )


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
