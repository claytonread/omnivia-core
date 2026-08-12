"""The governed relation graph, read once and walked as a value (V06-3 Lane E).

`governed.py` resolves *which versions* a workspace's view holds at an instant and
hydrates each one into a `GovernedRecord`. This module adds the two facts that resolver
does not carry as edges -- 0009's `omnivia_governed_relation_endpoints`, the exact two
versions a sealed `knowledge.relation` record names, and 0009's
`omnivia_record_supersessions`, the exact replacement one version received -- and nothing
else. There is no second resolver here, no second view rule, no second governance
predicate and no DTO lineage of its own: every node record and every relation record below
is a `GovernedRecord` that `read_governed_record_values` produced, under its rules, at this
instant.

**One snapshot, one transaction.** The frontier, its provenance, its evidence, the
relation endpoints, the supersessions and every freshness fact are read inside one explicit
read transaction opened here, so a correction sealed while the answer is being assembled is
either wholly visible or wholly invisible. Read in separate autocommit statements they are
not: an edge could name a version the frontier query had already stopped returning, and the
traversal would answer from two different databases at once. The transaction is opened here
rather than in `read_governed_record_values` because the endpoint and supersession queries
have to be inside the same one -- that resolver declines to end a transaction it did not
open, which is exactly what makes it composable here, and this function declines the same
way for a caller that already holds one.

**Read-only, structurally.** Every statement this module issues runs inside
`authorised(connection, mutations=False, ddl=False)`, so SQLite's own authorizer refuses
a write before it reaches a row. There is no writer, no seal, no rebuild and no repair.

**The watermark is the snapshot's, not a clock's.** `GraphSnapshot.watermark_us` is the
high-water instant over *all three* authoritative facts an answer rests on -- the sealed
versions, the sealed relation endpoint rows and the supersessions -- taken from this same
fenced read. All three are needed: 0009 records an endpoint row's own `recorded_at_us`
separately from its assembly's and a supersession's effective instant separately again, so
a workspace whose newest fact is either one would otherwise state a freshness that predates
a row the answer was actually built from. A supersession contributes the *effective*
instant `governed.py` computed for it -- the later of the edge row and its sealed target
assembly, which is the same fact that decided the view -- rather than a second reading of
the same row under a different rule. It describes the read point rather than the wall
clock, so a fact sealed after the transaction closed cannot appear in the watermark of an
answer it also cannot appear in. A second, later query for it would be exactly the drift
this whole shape prevents.

**Two kinds of edge, one materialization (Amendment 010).** An *ordinary* edge comes from
`omnivia_governed_relation_endpoints`: the endpoints a sealed `knowledge.relation` record
names. A *derived* edge comes from `omnivia_record_supersessions`, and is exactly the
authoritative replacement stated as a graph edge -- source is the superseded version,
target is the replacement version, the relation type is `record.superseded`, and the
relation reference *is* the replacement target, whose sealed `GovernedRecord` is the edge's
own record. Nothing is synthesized to make that shape: no relation record is fabricated, no
provenance is reconstructed, no relation id is invented and no writer exists here. The
replacement version is already in this snapshot as a hydrated record, so naming it as both
the target and the relation reference is a reference to a record the answer already carries.

Which of them a view returns follows from the view alone. `current_canonical` holds no
superseded version, so the old source of every supersession is absent from it and the
derived edge is simply not there -- absent, not a boundary a larger `depth_limit` could
reveal. `history` holds superseded versions, so a derived edge appears exactly when its old
source, its replacement target and therefore its relation record are all versions that view
holds. From there on a `record.superseded` edge is an edge like any other: it filters by
relation type and by domain scope, it carries the walk in whichever direction the request
asked for, it counts against the node and edge budgets, and it sorts by the same total key.

**Precedence when a replacement is itself a relation record.** A `knowledge.relation`
record that is corrected produces both an ordinary edge -- the replacement version asserts
its own endpoints -- and a derived edge naming that same replacement version as its
relation reference. The contract's result validator refuses two edges naming one relation
record version, so exactly one may be kept, and the ratified rule (Amendment 010, ratified
2026-08-10) keeps the explicit governed-relation edge: it is the assertion a
`knowledge.relation` record actually makes, and the supersession it collides with is still
fully stated by the *other* end of the same correction. Only the colliding derived edge is
dropped; every other supersession, including one for the very same record's other versions,
is materialized untouched.

**The walk is a pure function over the snapshot.** `traverse` holds no connection, no
cursor and no callback -- it is handed frozen values and can reach nothing else, so a node
or an edge the snapshot did not capture is unreachable by construction rather than by
discipline. Its result is a total function of the snapshot and the request's selectors:
depth is shortest-path depth, which does not depend on visit order, and both output
sequences are sorted by the exact total keys the contract's own result validator checks.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from omnivia_core.contracts.v1 import (
    GovernedRecord,
    GraphEdge,
    GraphNode,
    RecordVersionReference,
)
from omnivia_core.contracts.v1.semantics_knowledge import (
    GRAPH_BOUNDARY_REASON_DEPTH,
    GRAPH_DIRECTION_BOTH,
    GRAPH_DIRECTION_INBOUND,
    GRAPH_DIRECTION_OUTBOUND,
)
from omnivia_core_runtime.storage.connection import authorised
from omnivia_core_runtime.storage.governed import (
    read_governed_record_values,
    read_governed_supersessions,
)

#: One exact governed record version, as every map and set below keys on it. The same pair
#: the contract's `RecordVersionReference` names and the same pair its result validator
#: checks node identity, seed closure and edge endpoints against.
RecordKey = tuple[str, str]

#: The relation type every derived supersession edge carries. 0009's own provenance action
#: for the same fact, spelled the way it spells it, so a caller filtering a traversal by
#: relation type and a caller reading a record's provenance are naming one thing.
RELATION_TYPE_SUPERSEDED: Final = "record.superseded"

_VIEW: Final = "omnivia_authoritative_governed_versions"
_ENDPOINTS: Final = "omnivia_governed_relation_endpoints"


@dataclass(frozen=True, slots=True)
class GraphRelation:
    """One edge candidate whose relation record the resolved view actually holds.

    `relation` is the relation record's own identity and `record` is that record, hydrated
    by `governed.py`. They are not two accounts of one thing: the contract requires an
    edge's `relation_reference` to identify its `record`'s identity exactly, and holding the
    pair together here is what makes that true by construction rather than by a later
    assignment that could name a different record.

    One dataclass for both kinds of edge, because past this point there is nothing to tell
    apart: an ordinary edge's relation record is the `knowledge.relation` record that
    asserted it, a derived supersession edge's is the replacement version itself, and the
    walk applies the same filters, the same direction rule and the same ordering to each.
    """

    relation_type: str
    source: RecordKey
    target: RecordKey
    relation: RecordKey
    record: GovernedRecord


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    """One workspace's view, its relations and its watermark, from one database state.

    Frozen values and nothing else: no connection, no cursor and no callback, so the walk
    below cannot reach back into the database for a sixth fact the other five never saw.
    """

    watermark_us: int
    records: Mapping[RecordKey, GovernedRecord]
    relations: tuple[GraphRelation, ...]


def read_graph_snapshot(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
    view: str | None = None,
) -> GraphSnapshot:
    """One workspace's `view` at `resolution_instant_us`, with its relations and watermark.

    The endpoint query joins the authoritative view on the relation's own assembly, so an
    unsealed relation assembly contributes nothing -- 0009's own words, *"it is not in the
    view, it is not ancestry anything else may name"* -- and `recorded_at_us <=
    resolution_instant_us` on both sides keeps a relation recorded after the instant out of
    an answer asked before it. Which of the remaining relations may actually be *seen* is
    then decided by the resolved view alone: a row survives only if its relation record is
    one `read_governed_record_values` returned, which is the governance, layer, temporal and
    supersession rule that module owns, applied once.

    The supersessions are read through `read_governed_supersessions`, inside this same
    transaction, rather than restated here: what counts as a replacement -- the seal join,
    the exact-target join and the effective instant -- is settled in `governed.py`, and this
    is the *same* rule that already decided which versions the view holds. A second copy
    would be a second place for the edge and the view to disagree about one correction. It
    opens its own read-only fence around its own statement, which is the narrower guarantee;
    the transaction opened above is what makes it the same snapshot as everything else here.

    `watermark_us` is the high-water instant over the sealed versions, the sealed endpoint
    rows *and* the supersessions this one fenced read saw, which is every authoritative fact
    an answer rests on. Every supersession the snapshot saw contributes, not only the ones
    that became edges: a replacement whose old source this view does not hold is still a
    fact this read observed and answered around.

    Ownership follows `governed.py`'s rule for the same reason: a caller already inside a
    transaction is reading this as one fact among several and gets its transaction back
    untouched; only a transaction begun here is committed or rolled back here.
    """
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        values = read_governed_record_values(
            connection,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
            view=view,
        )
        with authorised(connection, mutations=False, ddl=False) as fenced:
            endpoint_rows = fenced.execute(
                "SELECT a.governed_record_id, a.governed_record_version_id, "
                "       r.relation_type, r.source_record_id, r.source_version_id, "
                "       r.target_record_id, r.target_version_id, r.recorded_at_us "
                f"FROM {_ENDPOINTS} r "
                f"JOIN {_VIEW} a ON a.workspace_id = r.workspace_id "
                "               AND a.assembly_id = r.assembly_id "
                "WHERE r.workspace_id = ? "
                "  AND r.recorded_at_us <= ? "
                "  AND a.recorded_at_us <= ? "
                "ORDER BY r.relation_type ASC, r.source_record_id ASC, "
                "         r.source_version_id ASC, r.target_record_id ASC, "
                "         r.target_version_id ASC, r.assembly_id ASC",
                (workspace_id, resolution_instant_us, resolution_instant_us),
            ).fetchall()
            # The version half of the read point this answer states its freshness against,
            # taken from the same fenced snapshot as everything above it. `0` is a real
            # watermark for a workspace that holds nothing sealed yet, not a missing value.
            # The endpoint half is folded in below, off the rows already fetched, because
            # 0009 stamps an endpoint row's own `recorded_at_us` independently of its
            # assembly's and a later one is a fact this answer was built from.
            version_watermark = fenced.execute(
                "SELECT COALESCE(MAX(recorded_at_us), 0) "
                f"FROM {_VIEW} WHERE workspace_id = ? AND recorded_at_us <= ?",
                (workspace_id, resolution_instant_us),
            ).fetchone()
        # Inside the transaction, outside that fence: it opens its own read-only one. Both
        # queries above and this one therefore read from the single snapshot this
        # transaction pinned, which is what makes a mid-read correction wholly visible or
        # wholly invisible rather than half of each.
        supersessions = read_governed_supersessions(
            connection,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
        )
    except BaseException:
        if owns_transaction:
            connection.execute("ROLLBACK")
        raise
    if owns_transaction:
        connection.execute("COMMIT")

    records = {
        (
            value.record.provenance.identity.record_id,
            value.record.provenance.identity.version,
        ): value.record
        for value in values
    }
    explicit = tuple(
        GraphRelation(
            relation_type=str(row[2]),
            source=(str(row[3]), str(row[4])),
            target=(str(row[5]), str(row[6])),
            relation=relation,
            record=records[relation],
        )
        for row in endpoint_rows
        if (relation := (str(row[0]), str(row[1]))) in records
    )
    # The ratified precedence rule, applied once, over the explicit candidates that actually
    # became edges. An endpoint row the view dropped names no relation record this answer
    # can carry, so it cannot collide with anything and must not suppress anything: the set
    # is built from `explicit`, not from `endpoint_rows`.
    asserted = {relation.relation for relation in explicit}
    derived = tuple(
        GraphRelation(
            relation_type=RELATION_TYPE_SUPERSEDED,
            source=(fact.governed_record_id, fact.source_version_id),
            target=replacement,
            # The replacement version is the edge's relation reference and its record. Not
            # a synthesized relation: it is the sealed version this snapshot already holds,
            # named once so the two can never identify different things.
            relation=replacement,
            record=records[replacement],
        )
        for fact in supersessions
        if (replacement := (fact.governed_record_id, fact.target_version_id)) in records
        and replacement not in asserted
    )
    return GraphSnapshot(
        watermark_us=max(
            int(version_watermark[0]),
            *(int(row[7]) for row in endpoint_rows),
            *(fact.effective_at_us for fact in supersessions),
            0,
        ),
        records=MappingProxyType(records),
        relations=explicit + derived,
    )


def traverse(
    snapshot: GraphSnapshot,
    *,
    seeds: tuple[RecordKey, ...],
    direction: str,
    relation_types: frozenset[str] | None,
    domain_scope: str | None,
    depth_limit: int,
) -> tuple[tuple[GraphNode, ...], tuple[GraphEdge, ...]]:
    """Breadth-first from `seeds`, as the nodes and edges one traversal result carries.

    Pure, and total on the snapshot: it is handed values, holds no store in a parameter, a
    global, a closure or a callback, and therefore cannot return a record the snapshot did
    not capture. That is the same isolation property `rank_governed` has, and it is why the
    read above and the walk here are two functions rather than one.

    `domain_scope` narrows both the records a node may be and the relation records an edge
    may rest on, because the contract's result validator applies it to both -- a relation in
    another scope linking two in-scope records is a relation this request did not ask about.

    A derived `record.superseded` edge is walked by exactly these rules and no others: it is
    kept or dropped by `relation_types` under that name, by `domain_scope` through the
    replacement version's own scope, and by whether both of its ends are records this view
    holds. Nothing below tests what kind of edge it is looking at.

    **The complete result, before pagination.** There is no node or edge budget here. The
    handler receives the deterministic total value and alone slices it into signed,
    snapshot-bound pages; keeping pagination above this pure function means a continuation
    can never change what the traversal itself reached.

    **Depth-boundary edges.** An edge whose two endpoints are both returned nodes is fully
    materialized and carries no `boundary_reason`. An in-scope relation the walk *reached*
    -- one endpoint returned at exactly `depth_limit`, the other withheld only because it
    lies past that limit and the direction would have carried the walk to it -- is stated as
    one edge anchored at the returned endpoint, with the unreached endpoint omitted and
    `depth_boundary` given as the reason. Source and target keep their semantic orientation;
    which one is omitted follows from the graph, not from the walk's direction.

    Nothing else produces a boundary. An endpoint the view, the workspace, the
    `domain_scope` filter, the `relation_types` filter, sealing or the temporal rules
    excluded is not *past the depth limit*, it is not in the answer's universe at all --
    which is exactly why a supersession whose old source `current_canonical` does not hold
    yields no edge there rather than a boundary anchored at the replacement -- and
    a relation whose direction never pointed at it was never reached either. In both cases
    the relation contributes no edge, because a fabricated boundary would tell a caller
    "raise `depth_limit` and you will see it" about a record no depth could reveal. The
    other recognized reason, `page_boundary`, is introduced only by the handler when it
    slices this complete result and issues a continuation token.

    Determinism does not rest on iteration order. Depth is shortest-path depth, which is a
    property of the graph rather than of the visit sequence, and both sequences are sorted
    by the exact total keys the result validator checks -- `(depth, record_id, version)` for
    nodes and, for edges, the validator's own complete tuple with an absent endpoint spelled
    `("", "")` exactly as it spells one.
    """
    records = {
        key: record
        for key, record in snapshot.records.items()
        if domain_scope is None or record.domain_scope == domain_scope
    }
    relations = tuple(
        relation
        for relation in snapshot.relations
        if (relation_types is None or relation.relation_type in relation_types)
        and (domain_scope is None or relation.record.domain_scope == domain_scope)
        and relation.source in records
        and relation.target in records
    )

    # The two halves of `direction`, named once and reused by both the walk and the boundary
    # rule, so "the walk could have gone this way" and "a boundary may be claimed this way"
    # can never fall out of step.
    follows_source = direction in (GRAPH_DIRECTION_OUTBOUND, GRAPH_DIRECTION_BOTH)
    follows_target = direction in (GRAPH_DIRECTION_INBOUND, GRAPH_DIRECTION_BOTH)

    adjacent: dict[RecordKey, list[RecordKey]] = {}
    for relation in relations:
        if follows_source:
            adjacent.setdefault(relation.source, []).append(relation.target)
        if follows_target:
            adjacent.setdefault(relation.target, []).append(relation.source)

    # Shortest-path depth from the seed set. A key already in `depths` is never revisited,
    # so a cycle terminates and a record reachable by two paths keeps the shorter one.
    depths: dict[RecordKey, int] = {seed: 0 for seed in seeds if seed in records}
    current = tuple(depths)
    for depth in range(1, depth_limit + 1):
        following: list[RecordKey] = []
        for key in current:
            for neighbour in adjacent.get(key, ()):
                if neighbour not in depths:
                    depths[neighbour] = depth
                    following.append(neighbour)
        if not following:
            break
        current = tuple(following)

    nodes = tuple(
        GraphNode(
            reference=_reference(key),
            record=records[key],
            depth=depths[key],
        )
        for key in sorted(depths, key=lambda key: (depths[key], key[0], key[1]))
    )

    unsorted: list[GraphEdge] = []
    for relation in relations:
        source_reached = relation.source in depths
        target_reached = relation.target in depths
        if source_reached and target_reached:
            source, target, reason = _reference(relation.source), _reference(relation.target), None
        elif (
            source_reached
            and follows_source
            and depths[relation.source] == depth_limit
        ):
            source, target, reason = _reference(relation.source), None, GRAPH_BOUNDARY_REASON_DEPTH
        elif (
            target_reached
            and follows_target
            and depths[relation.target] == depth_limit
        ):
            source, target, reason = None, _reference(relation.target), GRAPH_BOUNDARY_REASON_DEPTH
        else:
            # Not reached at all: an endpoint some earlier filter removed, or one the
            # direction never pointed at. Neither is a depth boundary.
            continue
        unsorted.append(
            GraphEdge(
                relation_type=relation.relation_type,
                record=relation.record,
                relation_reference=_reference(relation.relation),
                source=source,
                target=target,
                boundary_reason=reason,
            )
        )
    return nodes, tuple(sorted(unsorted, key=_edge_order))


def _reference(key: RecordKey) -> RecordVersionReference:
    return RecordVersionReference(record_id=key[0], version=key[1])


def _edge_order(edge: GraphEdge) -> tuple[str, ...]:
    """The result validator's own edge sort key, spelled the way it spells it.

    An absent endpoint is `("", "")` there, so it is `("", "")` here: computing the order
    from anything else would produce a sequence the validator reads as out of order, which
    is how a boundary edge would otherwise fail the very check it was added to satisfy.
    """
    source = ("", "") if edge.source is None else (edge.source.record_id, edge.source.version)
    target = ("", "") if edge.target is None else (edge.target.record_id, edge.target.version)
    return (
        *source,
        edge.relation_type,
        *target,
        edge.relation_reference.record_id,
        edge.relation_reference.version,
    )


__all__ = [
    "RELATION_TYPE_SUPERSEDED",
    "GraphRelation",
    "GraphSnapshot",
    "RecordKey",
    "read_graph_snapshot",
    "traverse",
]
