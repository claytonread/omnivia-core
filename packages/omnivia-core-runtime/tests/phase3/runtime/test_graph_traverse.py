"""V06-3 Lane E: `graph.traverse` over governed relations and supersessions, end to end.

Nothing here is stubbed. A real workspace database migrated through 0009, every row
written through the accepted fenced writer and 0009's own seal trigger, the real
production `ApplicationDispatcher`, the real registered handler, the real
`read_graph_snapshot`/`traverse` pair, and a conformant `GraphTraversalResult` on the way
out -- the handler runs the contract's own `validate_graph_traversal_result` before it
returns, so every page below has already satisfied the result validator by the time a test
sees it.

**The fixture is one small graph, and every member of it exists to make exactly one thing
falsifiable.** Eight ordinary nodes and ten relation records, listed in `graph`'s docstring.
The three-node cycle `rec-a -> rec-b -> rec-c -> rec-a` is what makes shortest-path depth
observable: `rec-c` sits at depth 2 from `rec-a` outbound and at depth 1 with `both`,
because the cycle's closing edge is a one-hop inbound path. A traversal that measured depth
by visit order rather than by shortest path could not produce both numbers from one graph.

**The second half of the fixture is four corrected records, and it is disconnected from the
first on purpose.** `history` holds exactly the versions that were superseded, so the chains
`rec-h`, `rec-o`, `rel-x` and `rel-z` are the whole of that view and the eight ordinary nodes
are the whole of `current_canonical` -- neither half can move the other's assertions. A
complete derived edge needs a chain of three, because `v1 -> v2` is returnable only when `v2`
is itself in the view and it is there only because `v3` replaced it. Section 10 covers
Amendment 010: the exact mapping in both orientations, the current view's omission without a
fabricated boundary, relation-type and domain filtering, the all-or-nothing budget, the
ratified collision precedence beside a non-colliding supersession on the same page, the
watermark contribution with its `as_of` exclusion, one deterministic order over both kinds of
edge, and a two-connection WAL interleaving falsifier that commits a supersession while a
read is in flight.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_governed_truth_and_relations_migration as m3
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service import authorization
from omnivia_core_runtime.service.application import (
    EVIDENCE_SEARCH_OPERATION,
    GRAPH_TRAVERSE_OPERATION,
    KNOWLEDGE_RETRIEVAL_PURPOSE,
    KNOWLEDGE_SEARCH_OPERATION,
    LOCAL_TRANSPORT_ADAPTER,
    MEMORY_SEARCH_OPERATION,
    WORKSPACE_INSPECT_OPERATION,
    ApplicationDispatcher,
    build_application_registry,
    local_owner_session,
)
from omnivia_core_runtime.service.authorization import Grant, ServiceBinding
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers import graph as graph_handlers
from omnivia_core_runtime.service.main import LOCAL_PRINCIPAL
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    build_service_registry,
    server_capability_snapshot,
)
from omnivia_core_runtime.storage import graph as graph_storage
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    install_authorizer,
    mark_authorizer_installed,
    open_database,
)
from omnivia_core_runtime.storage.graph import read_graph_snapshot

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    GRAPH_BOUNDARY_REASON_DEPTH,
    GRAPH_DEFAULT_DEPTH_LIMIT,
    CapabilityRequirement,
    ClientIdentity,
    ErrorResponseEnvelope,
    GraphTraversalResult,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    encode_response,
    get_operation_metadata,
)
from omnivia_core.contracts.v1.semantics_knowledge import (
    GRAPH_DIRECTION_BOTH,
    GRAPH_DIRECTION_INBOUND,
    GRAPH_DIRECTION_OUTBOUND,
)

WORKSPACE_ID = m3.WORKSPACE_ID
INSTALLATION_ID = "inst-graph-0001"
CLIENT = ClientIdentity(id="omnivia-core-cli", version="0.1.0")
FOREIGN_WORKSPACE_ID = "ws-graph-foreign-0001"

#: The fixture's own instants, kept clear of the M2/M3 chain the base workspace already
#: holds. `LATE_US` is the second, much later write: it is what an `as_of` before it must
#: exclude, and its *endpoint* row is stamped later still, which is what makes "the
#: watermark includes the endpoint half" a measurement rather than a restatement.
BASE_US = m3.BASE_US + 1_000
LATE_US = m3.BASE_US + 10_000_000
LATE_ENDPOINT_US = LATE_US + 5

#: Before `LATE_US` and after everything else, so one `as_of` separates the two writes.
CUTOFF_AS_OF = "2023-11-14T22:13:25.000Z"
CUTOFF_US = 1_700_000_005_000_000

#: The supersession half of the fixture, two seconds in: after the M2/M3 chain, before
#: `CUTOFF_US` and far before `LATE_US`, so it moves neither the ordinary pages above nor the
#: watermark they pin. Every instant here is a whole millisecond, which is the resolution a
#: contract `Timestamp` carries -- an `as_of` naming a finer instant could not be spelled.
HISTORY_US = 1_700_000_002_000_000

#: `rec-o`'s last supersession *edge* row, stamped long after the version it retires. It is
#: the newest authoritative fact in this workspace before the `LATE_US` writes, which is what
#: makes the supersession's watermark contribution measurable rather than merely asserted.
LATE_SUPERSESSION_US = HISTORY_US + 500_000

#: The two instants that separate it. Before: every version is written and no supersession
#: has taken effect at `LATE_SUPERSESSION_US`. After: it has.
BEFORE_SUPERSESSION_AS_OF = "2023-11-14T22:13:22.100Z"
BEFORE_SUPERSESSION_US = HISTORY_US + 100_000
AFTER_SUPERSESSION_AS_OF = "2023-11-14T22:13:22.900Z"
AFTER_SUPERSESSION_US = HISTORY_US + 900_000

#: The two corrections the two-connection falsifier commits *during* a read, and the only
#: writes in this file that do not come from the fixture.
MIDREAD_US = HISTORY_US + 800_000
MIDREAD_LAST_US = MIDREAD_US + 1_000

#: Amendment 010's derived relation type, read off the production module below and pinned as
#: a literal here for the same reason the projection id is: a constant read through itself
#: agrees with whatever it was renamed to.
SUPERSEDED = "record.superseded"

#: Far enough ahead that no wall-clock resolution instant reaches it, which is what keeps
#: `rec-future` out of every view this file resolves.
NOT_YET_US = 4_000_000_000_000_000

#: The two relation types 0009's seal trigger accepts that this fixture uses.
#: `reconciliation.supports` is the only one exempt from the trigger's canonical
#: source-before-target normalization, so it is the only one an arbitrarily oriented edge
#: (the cycle's closing `rec-c -> rec-a`) can be written with.
SUPPORTS = "reconciliation.supports"
RELATED_TO = "reconciliation.related_to"

#: One content document for every seeded record. `statement` is the primary member 0009's
#: frozen schema catalogue declares for both `knowledge.claim` and `knowledge.relation`.
CONTENT = json.dumps({"statement": "a governed graph fixture record"})

#: A distinctive value that appears nowhere in this build, so its absence from a refusal can
#: only mean the refusal never carried it. Spelled twice because the two fields it is
#: planted in have different domains: `Identifier` for a record id, `OpenCode` for a
#: relation type and a domain scope.
SENTINEL_ID = "zz-caller-sentinel-9f3c1d"
SENTINEL_CODE = "zzcallersentinel.deadbeef"

#: The production grant as `service.main.serve` wires it after Lane E's additive edit.
#: `test_workspace_inspect_refusals.py` is what pins this against `main.py`'s own syntax.
PRODUCTION_OPERATIONS = frozenset(
    {
        WORKSPACE_INSPECT_OPERATION,
        EVIDENCE_SEARCH_OPERATION,
        KNOWLEDGE_SEARCH_OPERATION,
        MEMORY_SEARCH_OPERATION,
        GRAPH_TRAVERSE_OPERATION,
    }
)


def key(record_id: str) -> tuple[str, str]:
    """One seeded record's exact `(record_id, version)` pair."""
    return (record_id, f"ver-{record_id}")


def reference(record_id: str) -> dict[str, str]:
    """One seeded record as the wire `RecordVersionReference` a request starts from."""
    return {"record_id": record_id, "version": f"ver-{record_id}"}


def step_key(record_id: str, step: int) -> tuple[str, str]:
    """One exact version of a corrected record: chains are several versions of one id."""
    return (record_id, f"ver-{record_id}-{step}")


def step_reference(record_id: str, step: int) -> dict[str, str]:
    """One chain version as the wire reference a request starts from."""
    return {"record_id": record_id, "version": f"ver-{record_id}-{step}"}


# --- the fixture --------------------------------------------------------------


def seal(
    holder: m2.Owned,
    *,
    record_id: str,
    audit_ref: str,
    record_type: str = "knowledge.claim",
    scope: str = "product.core",
    recorded_at_us: int,
    valid_from_us: int = -1,
    endpoint: tuple[str, str, str] | None = None,
    endpoint_recorded_at_us: int | None = None,
    seal_governed: bool = True,
    candidate_layer: bool = True,
) -> None:
    """One record's whole accepted lineage: a sealed candidate, then the sealed, accepted,
    canonical governed version promoted from it.

    Adapted from `test_knowledge_search_vertical.seal_record`, narrowed to what a graph
    needs and widened by the one thing that file has no use for: `endpoint`, which makes
    this a `knowledge.relation` record carrying an `omnivia_governed_relation_endpoints`
    row on *both* layers, exactly as `m3.seed_accepted_relation` writes one.

    Every row goes through `fenced_transaction` and 0009's seal trigger, so nothing seeded
    here is a shape the accepted writer would refuse. That trigger is also why the fixture
    reads the way it does: it accepts five relation types and no others, it requires a
    relation's two endpoints to be *sealed governed accepted* versions, and it requires
    canonical source-before-target ordering for every type but `reconciliation.supports`.

    `seal_governed=False` leaves a committed but unsealed governed assembly -- 0009's own
    inert row, "not in the view, not ancestry anything else may name". `candidate_layer` is
    dropped with it, because an unsealed assembly fires no trigger and therefore needs no
    predecessor to be legal; keeping the candidate would prove the view rule instead of the
    sealing rule, and this file wants the two separated.
    """
    candidate_assembly, candidate_version = f"asm-{record_id}-cand", f"ver-{record_id}-cand"
    governed_assembly, governed_version = f"asm-{record_id}", f"ver-{record_id}"
    endpoint_at = recorded_at_us if endpoint_recorded_at_us is None else endpoint_recorded_at_us

    def write_endpoint(assembly_id: str, event_id: str) -> None:
        assert endpoint is not None
        relation_type, source_id, target_id = endpoint
        m3.insert(
            holder.connection,
            m3.ENDPOINTS,
            {
                "workspace_id": WORKSPACE_ID,
                "assembly_id": assembly_id,
                "provenance_event_id": event_id,
                "correlation_kind": "m1_audit",
                "correlation_id": audit_ref,
                "relation_type": relation_type,
                "source_record_id": source_id,
                "source_version_id": f"ver-{source_id}",
                "target_record_id": target_id,
                "target_version_id": f"ver-{target_id}",
                "recorded_at_us": endpoint_at,
            },
        )

    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        m3.insert(
            holder.connection,
            m3.RECORDS,
            m3.record_row(record_id, record_type=record_type, scope=scope),
        )

        if candidate_layer:
            row = m3.assembly_row(
                candidate_assembly,
                candidate_version,
                record_id,
                record_type=record_type,
                scope=scope,
                audit_ref=audit_ref,
                correlation_id=audit_ref,
                ordinal=1,
            )
            row["content_json"] = CONTENT
            m3.insert(holder.connection, m3.ASSEMBLIES, row)
            proposed = f"ev-{candidate_assembly}-proposed"
            m3.insert(
                holder.connection,
                m3.EVENTS,
                m3.event_row(
                    proposed,
                    candidate_assembly,
                    candidate_version,
                    "candidate.human_proposed",
                    audit_ref=audit_ref,
                    correlation_id=audit_ref,
                ),
            )
            m3.insert(holder.connection, m3.LINKS, m3.link_row(proposed, candidate_assembly))
            if endpoint is not None:
                asserted = f"ev-{candidate_assembly}-asserted"
                m3.insert(
                    holder.connection,
                    m3.EVENTS,
                    m3.event_row(
                        asserted,
                        candidate_assembly,
                        candidate_version,
                        "relation.asserted",
                        sequence=2,
                        audit_ref=audit_ref,
                        correlation_id=audit_ref,
                    ),
                )
                m3.insert(holder.connection, m3.LINKS, m3.link_row(asserted, candidate_assembly))
                write_endpoint(candidate_assembly, asserted)
            m3.insert(
                holder.connection,
                m3.SEALS,
                m3.seal_row(
                    candidate_assembly,
                    candidate_version,
                    seal_id=f"seal-{candidate_assembly}",
                    correlation_id=audit_ref,
                ),
            )

        governed = m3.assembly_row(
            governed_assembly,
            governed_version,
            record_id,
            record_type=record_type,
            scope=scope,
            layer="governed",
            origin=None,
            disposition="accepted",
            authority="canonical",
            decision_kind="human_reviewer",
            decision_id="reviewer-1",
            audit_ref=audit_ref,
            correlation_id=audit_ref,
            ordinal=2,
            digest=m3.DIGEST_B,
        )
        governed["content_json"] = CONTENT
        governed["valid_from_us"] = valid_from_us
        governed["recorded_at_us"] = recorded_at_us
        m3.insert(holder.connection, m3.ASSEMBLIES, governed)
        accepted = f"ev-{governed_assembly}-accepted"
        m3.insert(
            holder.connection,
            m3.EVENTS,
            m3.event_row(
                accepted,
                governed_assembly,
                governed_version,
                "governance.accepted",
                audit_ref=audit_ref,
                correlation_id=audit_ref,
                actor_id="reviewer-1",
                actor_kind="human",
                actor_role="reviewer",
                predecessor_record_id=record_id,
                predecessor_version_id=candidate_version,
            ),
        )
        m3.insert(holder.connection, m3.LINKS, m3.link_row(accepted, governed_assembly))
        if endpoint is not None:
            asserted = f"ev-{governed_assembly}-asserted"
            m3.insert(
                holder.connection,
                m3.EVENTS,
                m3.event_row(
                    asserted,
                    governed_assembly,
                    governed_version,
                    "relation.asserted",
                    sequence=2,
                    audit_ref=audit_ref,
                    correlation_id=audit_ref,
                    actor_id="reviewer-1",
                    actor_kind="human",
                    actor_role="reviewer",
                ),
            )
            m3.insert(holder.connection, m3.LINKS, m3.link_row(asserted, governed_assembly))
            write_endpoint(governed_assembly, asserted)
        if seal_governed:
            seal_row = m3.seal_row(
                governed_assembly,
                governed_version,
                seal_id=f"seal-{governed_assembly}",
                correlation_id=audit_ref,
            )
            # Last, and after every instant this lineage carries: 0009 refuses a seal that
            # precedes its own assembly, and the late relation's endpoint is stamped later
            # than the assembly it hangs off.
            seal_row["sealed_at_us"] = max(recorded_at_us, endpoint_at, m3.BASE_US + 50) + 1
            m3.insert(holder.connection, m3.SEALS, seal_row)


def governed_version(
    holder: m2.Owned,
    *,
    record_id: str,
    version_id: str,
    audit_ref: str,
    ordinal: int,
    recorded_at_us: int,
    digest: str,
    record_type: str = "knowledge.claim",
    scope: str = "product.core",
    candidate: bool = False,
    promoted_from: str | None = None,
    supersedes: str | None = None,
    supersession_recorded_at_us: int | None = None,
    endpoint: tuple[str, str, str, str, str] | None = None,
    connection: sqlite3.Connection | None = None,
) -> None:
    """One sealed version of one record, written exactly as the accepted writer writes it.

    `seal` above writes a *record's whole lineage* under one fixed naming rule -- one
    candidate and one governed version, both named after the record -- which is all an
    ordinary relation graph needs and is precisely what a supersession chain cannot use: a
    chain is several governed versions of *one* record id, and a relation in it names an
    exact endpoint version rather than "the" version of an endpoint record. So this writes
    one version at a time, and the caller composes the chain.

    The three shapes 0009's seal trigger recognizes are selected by the arguments rather
    than by a mode flag, because each one *is* its own event set: a candidate carries
    `candidate.human_proposed`, a promoted root carries `governance.accepted` naming its
    sealed candidate predecessor, and a correction carries `governance.corrected` plus the
    `record.superseded` event its supersession row points at. A `knowledge.relation`
    assembly carries `relation.asserted` and exactly one endpoint row on top of whichever of
    those it is -- 0009 requires all of it, on both layers, which is why the counts are
    derived here rather than passed in.

    `connection` is the holder's own unless a caller names another. Only the two-connection
    snapshot falsifier does, and it passes a second connection to the same file under the
    same lease: authority lives in rows, not in a handle, so the fenced writer validates it
    the same way it validates the first.
    """
    target = holder.connection if connection is None else connection
    assembly_id = f"asm-{version_id}"
    events: list[tuple[str, str, int, str | None]] = []
    if candidate:
        events.append((f"ev-{assembly_id}-proposed", "candidate.human_proposed", 1, None))
    elif supersedes is None:
        events.append((f"ev-{assembly_id}-accepted", "governance.accepted", 1, promoted_from))
    else:
        events.append((f"ev-{assembly_id}-corrected", "governance.corrected", 1, supersedes))
        events.append((f"ev-{assembly_id}-superseded", "record.superseded", 2, supersedes))
    if endpoint is not None:
        events.append((f"ev-{assembly_id}-asserted", "relation.asserted", len(events) + 1, None))

    with fenced_transaction(
        target,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        if candidate:
            m3.insert(
                target, m3.RECORDS,
                m3.record_row(record_id, record_type=record_type, scope=scope),
            )
        row = m3.assembly_row(
            assembly_id,
            version_id,
            record_id,
            record_type=record_type,
            scope=scope,
            layer="candidate" if candidate else "governed",
            origin="human_proposed" if candidate else None,
            disposition=None if candidate else "accepted",
            authority="proposed" if candidate else "canonical",
            decision_kind=None if candidate else "human_reviewer",
            decision_id=None if candidate else "reviewer-1",
            audit_ref=audit_ref,
            correlation_id=audit_ref,
            ordinal=ordinal,
            digest=digest,
        )
        row["content_json"] = CONTENT
        row["recorded_at_us"] = recorded_at_us
        m3.insert(target, m3.ASSEMBLIES, row)
        for event_id, action, sequence, predecessor in events:
            m3.insert(
                target,
                m3.EVENTS,
                m3.event_row(
                    event_id,
                    assembly_id,
                    version_id,
                    action,
                    sequence=sequence,
                    audit_ref=audit_ref,
                    correlation_id=audit_ref,
                    actor_id="principal-1" if candidate else "reviewer-1",
                    actor_kind="human",
                    actor_role="author" if candidate else "reviewer",
                    predecessor_record_id=None if predecessor is None else record_id,
                    predecessor_version_id=predecessor,
                ),
            )
            m3.insert(target, m3.LINKS, m3.link_row(event_id, assembly_id))
        if endpoint is not None:
            relation_type, source_id, source_version, target_id, target_version = endpoint
            m3.insert(
                target,
                m3.ENDPOINTS,
                {
                    "workspace_id": WORKSPACE_ID,
                    "assembly_id": assembly_id,
                    "provenance_event_id": f"ev-{assembly_id}-asserted",
                    "correlation_kind": "m1_audit",
                    "correlation_id": audit_ref,
                    "relation_type": relation_type,
                    "source_record_id": source_id,
                    "source_version_id": source_version,
                    "target_record_id": target_id,
                    "target_version_id": target_version,
                    "recorded_at_us": recorded_at_us,
                },
            )
        if supersedes is not None:
            m3.insert(
                target,
                m3.SUPERSESSIONS,
                {
                    "workspace_id": WORKSPACE_ID,
                    "supersession_id": f"sup-{version_id}",
                    "assembly_id": assembly_id,
                    "governed_record_id": record_id,
                    "source_version_id": supersedes,
                    "target_version_id": version_id,
                    "provenance_event_id": f"ev-{assembly_id}-superseded",
                    "correlation_kind": "m1_audit",
                    "correlation_id": audit_ref,
                    "recorded_at_us": (
                        recorded_at_us
                        if supersession_recorded_at_us is None
                        else supersession_recorded_at_us
                    ),
                },
            )
        seal_row = m3.seal_row(
            assembly_id, version_id, seal_id=f"seal-{version_id}", correlation_id=audit_ref
        )
        seal_row["sealed_at_us"] = max(recorded_at_us, m3.BASE_US + 50) + 1
        m3.insert(target, m3.SEALS, seal_row)


def chain(
    holder: m2.Owned,
    *,
    record_id: str,
    audit_ref: str,
    length: int,
    first_us: int,
    record_type: str = "knowledge.claim",
    scope: str = "product.core",
    endpoint: tuple[str, str, str, str, str] | None = None,
    last_supersession_us: int | None = None,
) -> None:
    """One record corrected `length - 1` times: `ver-{record_id}-1` .. `-{length}`.

    Each version supersedes the one before it, so the `history` view holds every version but
    the last -- that view is *the superseded ones*, not "all of them", which is exactly why a
    complete derived edge needs a chain of three: `v1 -> v2` is returnable only when `v2` is
    itself in the view, and it is in the view only because `v3` replaced it.

    A relation chain asserts the *same* endpoint from every version, which is what makes the
    collision case a real one rather than a contrived one: correcting a `knowledge.relation`
    record is the ordinary way a replacement version comes to be both an endpoint assertion
    and a supersession target. Each version carries its own content digest, because 0009
    refuses a second sealed assembly stating identical relation semantics *and* an identical
    digest -- a correction that changed nothing is not a correction.

    The candidate `ver-{record_id}-0` is written here rather than by the caller, so a chain
    is always written in the one order 0009 permits: a governed root names a *sealed*
    candidate predecessor, and a relation's endpoint versions must already be sealed governed
    accepted versions when the relation is sealed.
    """
    governed_version(
        holder,
        record_id=record_id,
        version_id=f"ver-{record_id}-0",
        audit_ref=audit_ref,
        ordinal=1,
        recorded_at_us=HISTORY_US,
        digest=m3.DIGEST_A,
        record_type=record_type,
        scope=scope,
        candidate=True,
        endpoint=endpoint,
    )
    for step in range(1, length + 1):
        governed_version(
            holder,
            record_id=record_id,
            version_id=f"ver-{record_id}-{step}",
            audit_ref=audit_ref,
            ordinal=step + 1,
            recorded_at_us=first_us + step - 1,
            digest="sha256:" + str(step + 3) * 64,
            record_type=record_type,
            scope=scope,
            endpoint=endpoint,
            promoted_from=f"ver-{record_id}-0" if step == 1 else None,
            supersedes=None if step == 1 else f"ver-{record_id}-{step - 1}",
            supersession_recorded_at_us=(
                last_supersession_us if step == length else None
            ),
        )


def seed_supersessions(holder: m2.Owned) -> None:
    """The second half of the fixture: four records that were corrected, and one that was not.

    Disconnected from the eight ordinary nodes above on purpose. Every version here is either
    superseded or the survivor of a chain, so the `history` view holds exactly this subgraph
    and nothing of the ordinary one, and the ordinary `current_canonical` pages above cannot
    reach any of it -- neither half can move the other's assertions.

    * `rec-h` -- three claim versions, `product.core`. `v1 -> v2` is the one complete derived
      edge: both ends are in `history` and `v3` is what put `v2` there.
    * `rec-o` -- the same shape in `product.other`, so the domain filter has a supersession to
      exclude while an in-scope one survives the same request. Its last correction's edge row
      is stamped at `LATE_SUPERSESSION_US`, later than every version and every endpoint row in
      this workspace before the `LATE_US` writes -- that is what makes "the watermark includes
      the supersession half" a measurement rather than a restatement, and it is what one
      `as_of` separates: before it, `rec-o/v2` is not yet history and its derived edge does not
      exist.
    * `rel-x` -- three `knowledge.relation` versions, each asserting `rec-h/v1 -> rec-o/v1`
      (0009 refuses a relation whose two endpoint *records* are the same one). Its `v2` is
      therefore both an explicit edge's relation record and the target of the `v1 -> v2`
      supersession: the ratified collision, in the shape the amendment names. It is also the
      only path from the `product.core` chain to the `product.other` one, which is what lets
      one seed reach both and makes the domain filter's two halves comparable.
    * `rec-w` -- one uncorrected version, and the subject of the two-connection falsifier: its
      correction is committed *during* a read, from a second connection, and must be wholly
      absent from the answer that read was already assembling.

    The candidate assemblies are named `ver-{record}-0` rather than `seal`'s `-cand`, so the
    ordinary fixture's own count of candidate endpoint rows keeps counting the ordinary
    fixture. These chains are a separate statement and must not silently change that one.

    The order is 0009's, not a preference: `rel-x` names exact `rec-h`/`rec-o` versions as
    endpoints, and the seal trigger requires those to be sealed governed accepted versions
    already, so the two claim chains are written first.
    """
    chain(holder, record_id="rec-h", audit_ref="audit-27", length=3, first_us=HISTORY_US + 1_000)
    chain(
        holder, record_id="rec-o", audit_ref="audit-28", length=3,
        first_us=HISTORY_US + 4_000, scope="product.other",
        last_supersession_us=LATE_SUPERSESSION_US,
    )
    chain(
        holder, record_id="rel-x", audit_ref="audit-29", length=3,
        first_us=HISTORY_US + 7_000, record_type="knowledge.relation",
        endpoint=(SUPPORTS, "rec-h", "ver-rec-h-1", "rec-o", "ver-rec-o-1"),
    )
    chain(holder, record_id="rec-w", audit_ref="audit-31", length=1, first_us=HISTORY_US + 12_000)


@pytest.fixture
def graph(m3_owned: m2.Owned) -> Iterator[m2.Owned]:
    """The one workspace every property below is read out of.

    Nodes (`knowledge.claim`, `product.core` unless noted):

    * `rec-a`, `rec-b`, `rec-c` -- the three-node cycle. Its closing edge is what makes
      `rec-c` sit at depth 2 outbound and depth 1 under `both`, which is shortest-path
      depth stated as two numbers from one graph.
    * `rec-d` -- reachable only across a `reconciliation.related_to` edge, so the relation
      type filter has something to include and exclude.
    * `rec-e` -- reachable only across an **unsealed** relation. Never in any answer.
    * `rec-off` -- `product.other`, so the domain filter has an endpoint to exclude.
    * `rec-future` -- valid from 2096. Sealed, accepted and canonical, so only the temporal
      rule can exclude it.
    * `rec-late` -- written at `LATE_US`, so one `as_of` separates it from everything else.

    Relations (`knowledge.relation`, sealed, `product.core` unless noted). Four of them --
    `rel-b-off`, `rel-b-future`, `rel-bd` and `rel-bc-other` -- are anchored at `rec-b`
    deliberately: `rec-b` is the node that sits at exactly `depth_limit` in the negative
    test below, which is the only place a *fabricated* depth boundary could appear.
    """
    with fenced_transaction(
        m3_owned.connection,
        m3_owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=m3_owned.generation,
    ):
        for number in range(9, 40):
            m3.insert(
                m3_owned.connection,
                "omnivia_application_audit_events",
                m3.audit_row(f"audit-{number}"),
            )

    seal(m3_owned, record_id="rec-a", audit_ref="audit-9", recorded_at_us=BASE_US + 1)
    seal(m3_owned, record_id="rec-b", audit_ref="audit-10", recorded_at_us=BASE_US + 2)
    seal(m3_owned, record_id="rec-c", audit_ref="audit-11", recorded_at_us=BASE_US + 3)
    seal(m3_owned, record_id="rec-d", audit_ref="audit-12", recorded_at_us=BASE_US + 4)
    seal(m3_owned, record_id="rec-e", audit_ref="audit-13", recorded_at_us=BASE_US + 5)
    seal(
        m3_owned, record_id="rec-off", audit_ref="audit-14",
        scope="product.other", recorded_at_us=BASE_US + 6,
    )
    seal(
        m3_owned, record_id="rec-future", audit_ref="audit-15",
        recorded_at_us=BASE_US + 7, valid_from_us=NOT_YET_US,
    )
    seal(m3_owned, record_id="rec-late", audit_ref="audit-16", recorded_at_us=LATE_US)

    relation = "knowledge.relation"
    seal(
        m3_owned, record_id="rel-ab", audit_ref="audit-17", record_type=relation,
        recorded_at_us=BASE_US + 11, endpoint=(SUPPORTS, "rec-a", "rec-b"),
    )
    seal(
        m3_owned, record_id="rel-bc", audit_ref="audit-18", record_type=relation,
        recorded_at_us=BASE_US + 12, endpoint=(SUPPORTS, "rec-b", "rec-c"),
    )
    seal(
        m3_owned, record_id="rel-ca", audit_ref="audit-19", record_type=relation,
        recorded_at_us=BASE_US + 13, endpoint=(SUPPORTS, "rec-c", "rec-a"),
    )
    seal(
        m3_owned, record_id="rel-ad", audit_ref="audit-20", record_type=relation,
        recorded_at_us=BASE_US + 14, endpoint=(RELATED_TO, "rec-a", "rec-d"),
    )
    seal(
        m3_owned, record_id="rel-bd", audit_ref="audit-21", record_type=relation,
        recorded_at_us=BASE_US + 15, endpoint=(RELATED_TO, "rec-b", "rec-d"),
    )
    seal(
        m3_owned, record_id="rel-b-off", audit_ref="audit-22", record_type=relation,
        recorded_at_us=BASE_US + 16, endpoint=(SUPPORTS, "rec-b", "rec-off"),
    )
    seal(
        m3_owned, record_id="rel-b-future", audit_ref="audit-23", record_type=relation,
        recorded_at_us=BASE_US + 17, endpoint=(SUPPORTS, "rec-b", "rec-future"),
    )
    # Same two endpoints and same relation type as `rel-bc`, in another domain scope. The
    # pair is what separates "the domain filter narrows node records" from "it narrows the
    # relation records an edge rests on", which the contract's result validator applies to
    # both and which one fixture record alone cannot distinguish.
    seal(
        m3_owned, record_id="rel-bc-other", audit_ref="audit-24", record_type=relation,
        scope="product.other", recorded_at_us=BASE_US + 18,
        endpoint=(SUPPORTS, "rec-b", "rec-c"),
    )
    # Committed and unsealed: 0009's inert row. `rec-e` is a perfectly good sealed node, so
    # its absence from every answer can only be this relation's missing seal.
    seal(
        m3_owned, record_id="rel-be-unsealed", audit_ref="audit-25", record_type=relation,
        recorded_at_us=BASE_US + 19, endpoint=(SUPPORTS, "rec-b", "rec-e"),
        seal_governed=False, candidate_layer=False,
    )
    # The late write, and the one endpoint row stamped later than every sealed version.
    seal(
        m3_owned, record_id="rel-c-late", audit_ref="audit-26", record_type=relation,
        recorded_at_us=LATE_US, endpoint=(SUPPORTS, "rec-c", "rec-late"),
        endpoint_recorded_at_us=LATE_ENDPOINT_US,
    )
    seed_supersessions(m3_owned)
    yield m3_owned


#: M3's own workspace fixture -- a Phase 0 baseline migrated through 0009, owned under a
#: live lease, with the M2 evidence chain and eight audit events already seeded. Bound to a
#: second name so `graph` can depend on it without shadowing.
m3_owned = m3.owned


# --- the production path ------------------------------------------------------


def request_for(payload: Any, *, operation: str = GRAPH_TRAVERSE_OPERATION) -> RequestEnvelope:
    """A request the catalogue validator accepts, built from the frozen entry.

    Every field the catalogue decides is read off the entry, so a refusal below is never a
    malformed request wearing a denial's clothes.
    """
    entry = get_operation_metadata(operation)
    return RequestEnvelope(
        operation=operation,
        metadata=RequestMetadata(
            request_id="req-graph-1",
            correlation_id="corr-graph-1",
            trace_id="trace-graph-1",
            api_version=CONTRACT_VERSION,
            client=CLIENT,
            workspace_id=WORKSPACE_ID,
            scopes=tuple(entry.scope.required_scopes),
            purpose=KNOWLEDGE_RETRIEVAL_PURPOSE,
            required_capabilities=(
                CapabilityRequirement(
                    id=entry.required_capability.id,
                    minimum_version=entry.required_capability.minimum_version,
                    required=True,
                ),
            ),
            idempotency_key=None,
            mutation_precondition=None,
            principal_claim=None,
        ),
        input=payload,
    )


def production_path(
    service: object, *, scopes: frozenset[str] | None = None
) -> ApplicationDispatcher:
    """The production dispatcher, wired as `service.main.serve` wires it.

    `scopes` narrows the session's scope grant and nothing else: the operation allowlist,
    the purposes and the capability grant stay exactly as the production wiring derives
    them from `PRODUCTION_OPERATIONS`. That is what lets the negative below be a statement
    about scope authority rather than about the allowlist one check earlier.
    """
    registry = build_application_registry()
    session = local_owner_session(
        principal_id=LOCAL_PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        operations=PRODUCTION_OPERATIONS,
    )
    return ApplicationDispatcher(
        registry=registry,
        session=session if scopes is None else replace(session, scopes=scopes),
        binding=ServiceBinding(
            installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID
        ),
        supported_capabilities=server_capability_snapshot(registry),
        transport=LOCAL_TRANSPORT_ADAPTER,
        probe=Dispatcher.for_service_operations(
            Grant(
                principal=LOCAL_PRINCIPAL,
                workspaces=frozenset({WORKSPACE_ID}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            service,
        ),
        record=None,
        service=service,
    )


def answered(response: ResponseEnvelope) -> SuccessResponseEnvelope:
    assert isinstance(response, SuccessResponseEnvelope), response
    return response


def refused(response: ResponseEnvelope) -> ErrorResponseEnvelope:
    assert isinstance(response, ErrorResponseEnvelope), response
    return response


def traverse(holder: m2.Owned, payload: Any) -> GraphTraversalResult:
    """One traversal through the whole production path, as its decoded result."""
    wire = answered(production_path(holder).dispatch(request_for(payload))).result
    return GraphTraversalResult.from_wire(wire)


def start(
    *record_ids: str,
    direction: str | None = None,
    relation_types: list[str] | None = None,
    domain_scope: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """One `graph.traverse` wire payload, with only the selectors a test names present."""
    payload: dict[str, Any] = {"start": [reference(record_id) for record_id in record_ids]}
    if direction is not None:
        payload["direction"] = direction
    if relation_types is not None:
        payload["relation_types"] = relation_types
    if domain_scope is not None:
        payload["domain_scope"] = domain_scope
    payload.update(overrides)
    return payload


def page(
    *seeds: tuple[str, int], view: str | None = "history", **overrides: Any
) -> dict[str, Any]:
    """One traversal payload over the chain half of the fixture, `history` unless named.

    The twin of `start` above: that one names records whose single version `seal` wrote, this
    one names an exact version of a corrected record, which is the only way to ask about a
    supersession at all.
    """
    payload: dict[str, Any] = {
        "start": [step_reference(record_id, step) for record_id, step in seeds]
    }
    if view is not None:
        payload["view"] = view
    payload.update(overrides)
    return payload


def steps_of(result: GraphTraversalResult) -> tuple[tuple[str, int], ...]:
    """A page's nodes as `(version, depth)`.

    Versions rather than record ids, because a chain returns several versions of one record
    and `nodes_of` could not tell `rec-h`'s history from `rec-h` returned twice.
    """
    return tuple((node.reference.version, node.depth) for node in result.nodes)


def step_edges_of(
    result: GraphTraversalResult,
) -> tuple[tuple[str | None, str, str | None, str, str | None], ...]:
    """A page's edges as `(source version, type, target version, relation version, reason)`."""
    return tuple(
        (
            None if edge.source is None else edge.source.version,
            edge.relation_type,
            None if edge.target is None else edge.target.version,
            edge.relation_reference.version,
            edge.boundary_reason,
        )
        for edge in result.edges
    )


def nodes_of(result: GraphTraversalResult) -> tuple[tuple[str, int], ...]:
    """A page's nodes as `(record_id, depth)`, in the order the result states them."""
    return tuple((node.reference.record_id, node.depth) for node in result.nodes)


def edges_of(
    result: GraphTraversalResult,
) -> tuple[tuple[str | None, str, str | None, str, str | None], ...]:
    """A page's edges as `(source, relation_type, target, relation, boundary_reason)`.

    An omitted endpoint stays `None` rather than becoming a placeholder, because which
    endpoint is absent -- and therefore the edge's orientation at a boundary -- is exactly
    what the tests below assert.
    """
    return tuple(
        (
            None if edge.source is None else edge.source.record_id,
            edge.relation_type,
            None if edge.target is None else edge.target.record_id,
            edge.relation_reference.record_id,
            edge.boundary_reason,
        )
        for edge in result.edges
    )


def message(name: str) -> str:
    """One of the handler's frozen refusal messages, read off the module by name.

    Read rather than transcribed: the property under test is that a refusal *is* the frozen
    constant, not a formatted string that currently reads like one.
    """
    constant = getattr(graph_handlers, name)
    assert isinstance(constant, str)
    return constant


def wire_text(response: ResponseEnvelope) -> str:
    """The whole encoded response as one string, for leak assertions."""
    return json.dumps(encode_response(response), sort_keys=True)


# --- 1. the three directions --------------------------------------------------


def test_outbound_follows_source_to_target_and_terminates_on_the_cycle(
    graph: m2.Owned,
) -> None:
    """`rec-a -> rec-b -> rec-c`, with the cycle's closing edge fully materialized.

    `rel-ca` comes back as a *complete* edge rather than a boundary: `rec-c` sits at the
    depth limit and `rec-a` is already a returned node, so both endpoints are present and
    the contract forbids a `boundary_reason` on it. That is also the cycle terminating --
    `rec-a` is not walked a second time at depth 3.

    Falsifier: a walk that revisited `rec-a` would return it at a nonzero depth, which the
    result validator refuses outright for a requested seed.
    """
    result = traverse(
        graph, start("rec-a", direction=GRAPH_DIRECTION_OUTBOUND,
                     relation_types=[SUPPORTS], depth_limit=2)
    )

    assert nodes_of(result) == (
        ("rec-a", 0),
        ("rec-b", 1),
        ("rec-c", 2),
        ("rec-off", 2),
    )
    assert edges_of(result) == (
        ("rec-a", SUPPORTS, "rec-b", "rel-ab", None),
        ("rec-b", SUPPORTS, "rec-c", "rel-bc", None),
        ("rec-b", SUPPORTS, "rec-c", "rel-bc-other", None),
        ("rec-b", SUPPORTS, "rec-off", "rel-b-off", None),
        ("rec-c", SUPPORTS, None, "rel-c-late", GRAPH_BOUNDARY_REASON_DEPTH),
        ("rec-c", SUPPORTS, "rec-a", "rel-ca", None),
    )


def test_inbound_follows_target_to_source_and_never_the_other_way(graph: m2.Owned) -> None:
    """The mirror of the walk above, and the one that proves direction is a real selector.

    `rec-b` is one outbound hop from the seed and is absent here; `rec-c`, one *inbound*
    hop, is present. `rel-c-late` -- an edge leaving `rec-c` -- contributes nothing at all,
    because an inbound walk never pointed at `rec-late` and an unreached endpoint the
    direction never led to is not a depth boundary.
    """
    result = traverse(
        graph, start("rec-a", direction=GRAPH_DIRECTION_INBOUND,
                     relation_types=[SUPPORTS], depth_limit=1)
    )

    assert nodes_of(result) == (("rec-a", 0), ("rec-c", 1))
    assert edges_of(result) == (
        (None, SUPPORTS, "rec-c", "rel-bc", GRAPH_BOUNDARY_REASON_DEPTH),
        (None, SUPPORTS, "rec-c", "rel-bc-other", GRAPH_BOUNDARY_REASON_DEPTH),
        ("rec-c", SUPPORTS, "rec-a", "rel-ca", None),
    )


def test_both_reaches_what_each_single_direction_reaches_and_no_more(graph: m2.Owned) -> None:
    """One hop in either direction, which is exactly the union of the two walks above."""
    result = traverse(
        graph, start("rec-a", direction=GRAPH_DIRECTION_BOTH,
                     relation_types=[SUPPORTS], depth_limit=1)
    )

    assert nodes_of(result) == (("rec-a", 0), ("rec-b", 1), ("rec-c", 1))
    assert edges_of(result) == (
        ("rec-a", SUPPORTS, "rec-b", "rel-ab", None),
        ("rec-b", SUPPORTS, None, "rel-b-off", GRAPH_BOUNDARY_REASON_DEPTH),
        ("rec-b", SUPPORTS, "rec-c", "rel-bc", None),
        ("rec-b", SUPPORTS, "rec-c", "rel-bc-other", None),
        ("rec-c", SUPPORTS, None, "rel-c-late", GRAPH_BOUNDARY_REASON_DEPTH),
        ("rec-c", SUPPORTS, "rec-a", "rel-ca", None),
    )


def test_an_absent_direction_is_outbound_rather_than_whatever_ran_first(
    graph: m2.Owned,
) -> None:
    """`GRAPH_DIRECTION_DEFAULT`, and the fixture above proves that is not `inbound`."""
    absent = traverse(graph, start("rec-a", relation_types=[SUPPORTS], depth_limit=1))
    outbound = traverse(
        graph,
        start("rec-a", direction=GRAPH_DIRECTION_OUTBOUND,
              relation_types=[SUPPORTS], depth_limit=1),
    )
    inbound = traverse(
        graph,
        start("rec-a", direction=GRAPH_DIRECTION_INBOUND,
              relation_types=[SUPPORTS], depth_limit=1),
    )

    assert nodes_of(absent) == nodes_of(outbound)
    assert edges_of(absent) == edges_of(outbound)
    assert nodes_of(absent) != nodes_of(inbound)


# --- 2. the two selectors -----------------------------------------------------


def test_the_relation_type_filter_narrows_both_the_edges_and_what_is_reachable(
    graph: m2.Owned,
) -> None:
    """A filtered-out relation is not a filtered-out *edge*; it is a path that does not exist.

    `rec-d` hangs off `rec-a` by a single `reconciliation.related_to` edge and off nothing
    else, so it is present with no filter, absent under `[SUPPORTS]`, and the only reachable
    node under `[RELATED_TO]`. Three pages from one seed, which no single-filter assertion
    could separate.
    """
    unfiltered = traverse(graph, start("rec-a", depth_limit=1))
    supports = traverse(graph, start("rec-a", relation_types=[SUPPORTS], depth_limit=1))
    related = traverse(graph, start("rec-a", relation_types=[RELATED_TO], depth_limit=1))

    assert nodes_of(unfiltered) == (("rec-a", 0), ("rec-b", 1), ("rec-d", 1))
    assert nodes_of(supports) == (("rec-a", 0), ("rec-b", 1))
    assert nodes_of(related) == (("rec-a", 0), ("rec-d", 1))

    # Every returned edge carries a type the request asked for -- the result validator's own
    # rule, restated here because the fixture makes its violation reachable.
    assert {edge[1] for edge in edges_of(supports)} == {SUPPORTS}
    assert edges_of(related) == (("rec-a", RELATED_TO, "rec-d", "rel-ad", None),)


def test_the_domain_filter_narrows_node_records_and_relation_records_alike(
    graph: m2.Owned,
) -> None:
    """Both halves, separated by the fixture rather than argued in prose.

    `rec-off` is an out-of-scope *node*; `rel-bc-other` is an out-of-scope *relation record*
    whose two endpoints are both in scope. A filter applied to nodes alone would keep the
    second, and the page would carry a relation the request did not ask about while looking
    perfectly well formed.

    Falsifier: the same request without `domain_scope` returns both.
    """
    scoped = traverse(
        graph, start("rec-a", relation_types=[SUPPORTS], domain_scope="product.core",
                     depth_limit=2)
    )
    unscoped = traverse(graph, start("rec-a", relation_types=[SUPPORTS], depth_limit=2))

    assert nodes_of(scoped) == (("rec-a", 0), ("rec-b", 1), ("rec-c", 2))
    assert edges_of(scoped) == (
        ("rec-a", SUPPORTS, "rec-b", "rel-ab", None),
        ("rec-b", SUPPORTS, "rec-c", "rel-bc", None),
        ("rec-c", SUPPORTS, None, "rel-c-late", GRAPH_BOUNDARY_REASON_DEPTH),
        ("rec-c", SUPPORTS, "rec-a", "rel-ca", None),
    )

    assert ("rec-off", 2) in nodes_of(unscoped)
    assert "rel-bc-other" in {edge[3] for edge in edges_of(unscoped)}
    assert "rel-bc-other" not in {edge[3] for edge in edges_of(scoped)}


# --- 3. depth: shortest path, seeds, determinism ------------------------------


def test_depth_is_shortest_path_depth_rather_than_discovery_order(graph: m2.Owned) -> None:
    """One node, two depths, one graph -- which is what makes this checkable at all.

    `rec-c` is two outbound hops from `rec-a` and one inbound hop from it, across the
    cycle's closing edge. Under `both` the shorter path wins and it sits at depth 1; under
    `outbound` that path is not traversable and it sits at 2.

    Falsifier: keep the *first* depth a node is discovered at only if the walk happens to
    reach it in the right order, and the `both` page places `rec-c` at 2 whenever the
    outbound edge is examined first.
    """
    outbound = traverse(
        graph, start("rec-a", direction=GRAPH_DIRECTION_OUTBOUND,
                     relation_types=[SUPPORTS], depth_limit=3)
    )
    both = traverse(
        graph, start("rec-a", direction=GRAPH_DIRECTION_BOTH,
                     relation_types=[SUPPORTS], depth_limit=3)
    )

    assert dict(nodes_of(outbound))["rec-c"] == 2
    assert dict(nodes_of(both))["rec-c"] == 1
    # And the cycle terminates in both: the seed is returned exactly once, at depth 0.
    for result in (outbound, both):
        assert [depth for record_id, depth in nodes_of(result) if record_id == "rec-a"] == [0]


def test_every_requested_seed_is_returned_at_depth_zero(graph: m2.Owned) -> None:
    """Multi-seed closure: two seeds, both at depth 0, and the closure taken from both.

    `rec-d` is reachable from `rec-a` only across a `related_to` edge, which this request
    filters out -- so it is here as a seed and could not have arrived any other way. That is
    what makes this a statement about seeds rather than about reachability.
    """
    result = traverse(
        graph, start("rec-a", "rec-d", relation_types=[SUPPORTS], depth_limit=1)
    )

    # Depth leads the node order, so both seeds precede the one node the walk reached.
    assert nodes_of(result) == (("rec-a", 0), ("rec-d", 0), ("rec-b", 1))
    assert {record_id for record_id, depth in nodes_of(result) if depth == 0} == {
        "rec-a",
        "rec-d",
    }


def test_the_page_is_a_function_of_the_request_rather_than_of_the_run(
    graph: m2.Owned,
) -> None:
    """Determinism, and the ordering the result validator checks, asserted as sequences.

    Two identical requests over the same snapshot, plus the two orderings stated directly:
    `(depth, record_id, version)` for nodes and the validator's complete tuple for edges,
    with an absent endpoint spelled `("", "")` exactly as it spells one. The handler already
    ran `validate_graph_traversal_result`, so an out-of-order page could not have reached
    here -- this pins the order these particular values actually take.
    """
    payload = start("rec-a", direction=GRAPH_DIRECTION_BOTH, depth_limit=2)
    first = traverse(graph, payload)
    second = traverse(graph, payload)

    assert nodes_of(first) == nodes_of(second)
    assert edges_of(first) == edges_of(second)

    node_keys = [
        (node.depth, node.reference.record_id, node.reference.version) for node in first.nodes
    ]
    assert node_keys == sorted(node_keys)

    edge_keys = [
        (
            *(("", "") if edge.source is None else (edge.source.record_id, edge.source.version)),
            edge.relation_type,
            *(("", "") if edge.target is None else (edge.target.record_id, edge.target.version)),
            edge.relation_reference.record_id,
            edge.relation_reference.version,
        )
        for edge in first.edges
    ]
    assert edge_keys == sorted(edge_keys)


# --- 4. the depth boundary, and the four things that are not one --------------


def test_a_depth_boundary_anchors_at_the_returned_endpoint_in_both_orientations(
    graph: m2.Owned,
) -> None:
    """The exact edge shape, and the fact that orientation follows the graph, not the walk.

    One relation -- `rel-bc`, semantically `rec-b -> rec-c` -- seen from each end at
    `depth_limit=1`. Outbound from `rec-a` it is the *source* that was returned, so the
    target is omitted; inbound from `rec-a` it is the *target*, so the source is omitted.
    The relation's own orientation never flips: `rec-b` is the source in both, and it is
    which endpoint the walk reached that decides which one is absent.

    Falsifier: anchor the boundary at "the endpoint the walk came from" as a direction and
    the inbound case comes back with `source=rec-c`, inverting a relation the database
    states one way.
    """
    outbound = traverse(
        graph, start("rec-a", direction=GRAPH_DIRECTION_OUTBOUND,
                     relation_types=[SUPPORTS], domain_scope="product.core", depth_limit=1)
    )
    inbound = traverse(
        graph, start("rec-a", direction=GRAPH_DIRECTION_INBOUND,
                     relation_types=[SUPPORTS], domain_scope="product.core", depth_limit=1)
    )

    (outbound_edge,) = [edge for edge in outbound.edges if edge.boundary_reason is not None]
    assert outbound_edge.relation_reference.record_id == "rel-bc"
    assert outbound_edge.source is not None
    assert (outbound_edge.source.record_id, outbound_edge.source.version) == key("rec-b")
    assert outbound_edge.target is None
    assert outbound_edge.boundary_reason == GRAPH_BOUNDARY_REASON_DEPTH
    # The anchor is a returned node, and it sits at exactly the applied depth limit -- the
    # two conditions the contract requires a depth boundary to satisfy.
    assert dict(nodes_of(outbound))["rec-b"] == outbound.applied_depth_limit

    (inbound_edge,) = [edge for edge in inbound.edges if edge.boundary_reason is not None]
    assert inbound_edge.relation_reference.record_id == "rel-bc"
    assert inbound_edge.source is None
    assert inbound_edge.target is not None
    assert (inbound_edge.target.record_id, inbound_edge.target.version) == key("rec-c")
    assert inbound_edge.boundary_reason == GRAPH_BOUNDARY_REASON_DEPTH
    assert dict(nodes_of(inbound))["rec-c"] == inbound.applied_depth_limit


def test_an_excluded_endpoint_never_fabricates_a_depth_boundary(graph: m2.Owned) -> None:
    """The negative, and the reason four fixture relations are anchored where they are.

    This request puts `rec-b` at exactly `depth_limit`, which is the only place a depth
    boundary may be claimed at all. Five relations leave `rec-b`, and only one of them
    describes a record that a larger `depth_limit` would actually reveal:

    * `rel-bc` -- `rec-c` is in the view, in scope, of an included type, and simply past the
      limit. **This is the one real boundary.**
    * `rel-b-off` -- `rec-off` is excluded by `domain_scope`.
    * `rel-b-future` -- `rec-future` is excluded by the temporal rule.
    * `rel-bd` -- excluded by `relation_types`.
    * `rel-bc-other` -- its *relation record* is out of scope.

    A boundary for any of the last four would tell a caller "raise `depth_limit` and you
    will see it" about a record no depth could reveal. Exactly one boundary edge comes back,
    which is the assertion; a count alone would pass against a build that emitted the wrong
    one, so the surviving edge is identified by relation record too.
    """
    result = traverse(
        graph,
        start("rec-a", direction=GRAPH_DIRECTION_OUTBOUND, relation_types=[SUPPORTS],
              domain_scope="product.core", depth_limit=1),
    )

    assert nodes_of(result) == (("rec-a", 0), ("rec-b", 1))
    assert edges_of(result) == (
        ("rec-a", SUPPORTS, "rec-b", "rel-ab", None),
        ("rec-b", SUPPORTS, None, "rel-bc", GRAPH_BOUNDARY_REASON_DEPTH),
    )

    boundaries = [edge for edge in result.edges if edge.boundary_reason is not None]
    assert len(boundaries) == 1
    assert boundaries[0].relation_reference.record_id == "rel-bc"
    excluded = {"rel-b-off", "rel-b-future", "rel-bd", "rel-bc-other", "rel-be-unsealed"}
    assert excluded.isdisjoint({edge[3] for edge in edges_of(result)})


# --- 5. the limits: the complete answer, or none of it ------------------------


def test_a_node_overflow_refuses_and_returns_no_partial_content(graph: m2.Owned) -> None:
    """This build issues no continuation token, so a sliced page would be a silent truncation.

    Falsifier: the same request with a limit that fits comes back with four nodes, so the
    refusal is the limit's doing and not the request's.
    """
    response = refused(
        production_path(graph).dispatch(
            request_for(start("rec-a", relation_types=[SUPPORTS], depth_limit=2, node_limit=1))
        )
    )

    assert response.error.code == ERROR_CODE_SIZE_LIMIT_EXCEEDED
    assert response.error.message == message("_MESSAGE_TOO_LARGE")
    # Not a page with a note attached: there is no result document at all, so no node, no
    # edge and no freshness statement reached the caller.
    assert not isinstance(response, SuccessResponseEnvelope)
    for absent in ('"nodes"', '"edges"', "rec-b", "rec-off"):
        assert absent not in wire_text(response)

    fits = traverse(graph, start("rec-a", relation_types=[SUPPORTS], depth_limit=2))
    assert len(fits.nodes) == 4


def test_an_edge_overflow_refuses_on_its_own_budget(graph: m2.Owned) -> None:
    """The edge limit is a separate budget, so it has to refuse on its own.

    The node limit here is wide enough for the whole answer; only the edge budget is short.
    Falsifier: a build that compared both counts against the node limit would answer this.
    """
    response = refused(
        production_path(graph).dispatch(
            request_for(
                start("rec-a", relation_types=[SUPPORTS], depth_limit=2, edge_limit=1)
            )
        )
    )

    assert response.error.code == ERROR_CODE_SIZE_LIMIT_EXCEEDED
    assert response.error.message == message("_MESSAGE_TOO_LARGE")
    # The same all-or-nothing statement the node limit makes: no result document, so no
    # node, no edge and no freshness statement reached the caller.
    assert not isinstance(response, SuccessResponseEnvelope)
    for absent in ('"nodes"', '"edges"', "rec-b", "rec-off"):
        assert absent not in wire_text(response)

    fits = traverse(
        graph, start("rec-a", relation_types=[SUPPORTS], depth_limit=2, edge_limit=50)
    )
    assert len(fits.edges) == 6


def test_the_applied_limits_are_stated_and_the_page_says_it_is_exhausted(
    graph: m2.Owned,
) -> None:
    """A page that fits states the budgets it was answered under, and claims no next page."""
    result = traverse(
        graph, start("rec-a", relation_types=[SUPPORTS], depth_limit=2, node_limit=20)
    )

    assert result.applied_depth_limit == 2
    assert result.applied_node_limit == 20
    assert result.page.continuation_token is None
    # An absent `depth_limit` takes the contract's own default rather than a local one.
    default = traverse(graph, start("rec-a", relation_types=[SUPPORTS]))
    assert default.applied_depth_limit == GRAPH_DEFAULT_DEPTH_LIMIT


# --- 6. an unknown seed, and what its refusal may not carry -------------------


def test_an_unknown_seed_refuses_not_found_without_echoing_any_caller_value(
    graph: m2.Owned,
) -> None:
    """A traversal owes every seed at depth 0, so a seed the view does not hold is fatal.

    The refusal is the frozen module constant, and it carries none of the four caller-supplied
    values planted in this request: the unknown record id, the relation type, the domain
    scope, and the served workspace. The sentinels appear nowhere in this build, so their
    absence from the encoded response can only mean the refusal never contained them.
    """
    response = refused(
        production_path(graph).dispatch(
            request_for(
                {
                    "start": [
                        reference("rec-a"),
                        {"record_id": SENTINEL_ID, "version": f"ver-{SENTINEL_ID}"},
                    ],
                    "relation_types": [SENTINEL_CODE],
                    "domain_scope": SENTINEL_CODE,
                }
            )
        )
    )

    assert response.error.code == ERROR_CODE_NOT_FOUND
    assert response.error.message == message("_MESSAGE_UNKNOWN_START")
    text = wire_text(response)
    for sentinel in (SENTINEL_ID, SENTINEL_CODE, WORKSPACE_ID, "rec-a"):
        assert sentinel not in text


def test_a_seed_the_view_does_not_hold_is_refused_the_same_way(graph: m2.Owned) -> None:
    """`rec-future` exists, is sealed, accepted and canonical -- and is not yet valid.

    Falsifier for the test above: it shows the refusal is about the *resolved view* rather
    than about a syntactically odd identifier. `rec-e` is the second case: a perfectly
    ordinary current record, refused only because it is named beside one the view lacks --
    a traversal may not quietly answer the narrower question.
    """
    response = refused(
        production_path(graph).dispatch(request_for(start("rec-e", "rec-future")))
    )

    assert response.error.code == ERROR_CODE_NOT_FOUND
    assert response.error.message == message("_MESSAGE_UNKNOWN_START")
    # And `rec-e` alone is answerable, so the refusal above is `rec-future`'s doing.
    assert nodes_of(traverse(graph, start("rec-e"))) == (("rec-e", 0),)


# --- 7. what the snapshot may not contain -------------------------------------


def test_an_unsealed_relation_and_a_not_yet_valid_endpoint_are_both_absent(
    graph: m2.Owned,
) -> None:
    """Two exclusions with one shape: the row is committed, and it is not in the view.

    `rel-be-unsealed` is a committed governed relation assembly with no seal -- 0009's inert
    row -- and `rec-e`, its target, is a perfectly good sealed node reachable by no other
    path. `rec-future` is sealed, accepted and canonical, and excluded only by its validity
    window; `rel-b-future` names it and therefore contributes no edge either.

    The widest request this fixture admits is used deliberately: every direction, every
    relation type, every scope, and the maximum depth, so neither absence can be a selector's
    doing.
    """
    result = traverse(graph, start("rec-a", direction=GRAPH_DIRECTION_BOTH, depth_limit=8))

    reached = {record_id for record_id, _ in nodes_of(result)}
    assert "rec-e" not in reached
    assert "rec-future" not in reached
    assert {"rel-be-unsealed", "rel-b-future"}.isdisjoint({edge[3] for edge in edges_of(result)})
    # Both rows are really there -- the absences above are the view rule, not an empty table.
    assert graph.connection.execute(
        f"SELECT count(*) FROM {m3.ENDPOINTS} WHERE assembly_id = 'asm-rel-be-unsealed'"
    ).fetchone()[0] == 1
    assert graph.connection.execute(
        f"SELECT count(*) FROM {m3.ASSEMBLIES} WHERE governed_record_id = 'rec-future'"
    ).fetchone()[0] == 2


def test_a_candidate_layer_relation_is_not_an_edge_in_the_current_canonical_view(
    graph: m2.Owned,
) -> None:
    """Every sealed relation here has a sealed *candidate* twin carrying its own endpoint row.

    Those rows are in `omnivia_authoritative_governed_versions` -- they are sealed -- and the
    endpoint query joins them. They are dropped by the view rule alone: their relation record
    is not one `read_governed_record_values` returns under `current_canonical`. So every
    returned edge names a governed version, and the edge count is half what the endpoint
    table holds for these relations.

    Falsifier: drop the `in records` test in `read_graph_snapshot` and each edge below
    doubles, with the candidate twin's `relation_reference` beside the governed one.
    """
    result = traverse(graph, start("rec-a", direction=GRAPH_DIRECTION_BOTH, depth_limit=8))

    for edge in result.edges:
        assert edge.relation_reference.version.startswith("ver-rel-")
        assert not edge.relation_reference.version.endswith("-cand")
    assert graph.connection.execute(
        f"SELECT count(*) FROM {m3.ENDPOINTS} WHERE assembly_id LIKE '%-cand'"
    ).fetchone()[0] == 9


def test_an_as_of_before_a_write_excludes_the_relation_and_the_endpoint_it_names(
    graph: m2.Owned,
) -> None:
    """One instant separates the fixture's two writes, and the answer moves with it.

    `rec-late` and `rel-c-late` were both recorded after `CUTOFF_AS_OF`. Asked at that
    instant they are absent -- and, because `rec-late` was the only thing past `rec-c`, no
    depth boundary is claimed at `rec-c` either. Asked at the wall clock they are both back.
    """
    at_cutoff = traverse(
        graph,
        start("rec-a", direction=GRAPH_DIRECTION_OUTBOUND, relation_types=[SUPPORTS],
              domain_scope="product.core", depth_limit=2, as_of=CUTOFF_AS_OF),
    )
    now = traverse(
        graph,
        start("rec-a", direction=GRAPH_DIRECTION_OUTBOUND, relation_types=[SUPPORTS],
              domain_scope="product.core", depth_limit=2),
    )

    assert nodes_of(at_cutoff) == (("rec-a", 0), ("rec-b", 1), ("rec-c", 2))
    assert edges_of(at_cutoff) == (
        ("rec-a", SUPPORTS, "rec-b", "rel-ab", None),
        ("rec-b", SUPPORTS, "rec-c", "rel-bc", None),
        ("rec-c", SUPPORTS, "rec-a", "rel-ca", None),
    )
    assert at_cutoff.freshness.as_of == CUTOFF_AS_OF
    assert "rel-c-late" in {edge[3] for edge in edges_of(now)}


def test_a_foreign_workspaces_rows_cannot_be_written_and_are_not_read(
    graph: m2.Owned,
) -> None:
    """Both halves of the workspace boundary, because neither alone is the whole statement.

    The write half is 0009's: an endpoint row naming another workspace is refused outright
    by the mutation guard, so a foreign row is not a thing this database can come to hold.
    The read half is `read_graph_snapshot`'s own `workspace_id = ?` predicate, which is why
    resolving another workspace over this same connection yields an empty snapshot rather
    than this workspace's graph.
    """
    foreign_row = {
        "workspace_id": FOREIGN_WORKSPACE_ID,
        "assembly_id": "asm-rel-ab",
        "provenance_event_id": "ev-asm-rel-ab-asserted",
        "correlation_kind": "m1_audit",
        "correlation_id": "audit-17",
        "relation_type": SUPPORTS,
        "source_record_id": "rec-a",
        "source_version_id": "ver-rec-a",
        "target_record_id": "rec-b",
        "target_version_id": "ver-rec-b",
        "recorded_at_us": BASE_US + 30,
    }
    with pytest.raises(sqlite3.IntegrityError), fenced_transaction(
        graph.connection,
        graph.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=graph.generation,
    ):
        m3.insert(graph.connection, m3.ENDPOINTS, foreign_row)

    foreign = read_graph_snapshot(
        graph.connection,
        workspace_id=FOREIGN_WORKSPACE_ID,
        resolution_instant_us=LATE_ENDPOINT_US + 1_000,
    )
    assert foreign.records == {}
    assert foreign.relations == ()
    assert foreign.watermark_us == 0


# --- 8. the freshness statement -----------------------------------------------


def test_the_freshness_names_one_stable_projection_and_is_never_stale(
    graph: m2.Owned,
) -> None:
    """A direct authoritative read cannot lag itself, and it says which projection it is.

    The id and version are asserted as literals beside the module constants: read through
    the constants alone this would agree with whatever they were renamed to, and a moved
    projection name makes two answers incomparable.
    """
    first = traverse(graph, start("rec-a"))
    second = traverse(graph, start("rec-b", direction=GRAPH_DIRECTION_INBOUND))

    assert graph_handlers.GRAPH_PROJECTION == "graph.governed_relations"
    assert graph_handlers.GRAPH_PROJECTION_VERSION == "1"
    for result in (first, second):
        assert result.freshness.stale is False
        assert result.freshness.projection_versions == {"graph.governed_relations": "1"}
        assert set(result.freshness.projection_watermarks) == {"graph.governed_relations"}
    assert first.freshness.projection_versions == second.freshness.projection_versions
    assert (
        first.freshness.projection_watermarks == second.freshness.projection_watermarks
    )


def test_the_watermark_includes_an_ordinary_endpoint_rows_own_instant(
    graph: m2.Owned,
) -> None:
    """0009 stamps an endpoint row separately from its assembly, so both halves are needed.

    `rel-c-late`'s endpoint row is the newest authoritative fact in this workspace, and it is
    strictly newer than the newest sealed *version*. A watermark taken from the versions
    alone would state a freshness that predates a row this very answer was built from.

    Falsifier: drop the endpoint half and the watermark falls back to `LATE_US`, which the
    second assertion pins as a different number.
    """
    result = traverse(graph, start("rec-a"))
    newest_version = graph.connection.execute(
        "SELECT MAX(recorded_at_us) FROM omnivia_authoritative_governed_versions "
        "WHERE workspace_id = ?",
        (WORKSPACE_ID,),
    ).fetchone()[0]

    assert newest_version == LATE_US
    assert LATE_ENDPOINT_US > LATE_US
    assert result.freshness.projection_watermarks == {
        "graph.governed_relations": str(LATE_ENDPOINT_US)
    }

    # And it is the snapshot's own high-water mark, not a clock read: an `as_of` before the
    # late write moves it back to a fact that write cannot have contributed to.
    at_cutoff = traverse(graph, start("rec-a", as_of=CUTOFF_AS_OF))
    assert int(at_cutoff.freshness.projection_watermarks["graph.governed_relations"]) < CUTOFF_US


# --- 9. registration, the grant, and the binding clause -----------------------


def test_the_production_build_registers_the_operation_and_grants_it(graph: m2.Owned) -> None:
    """`graph.traverse` is a registered application handler and a granted session operation.

    The exact sets are pinned in `test_workspace_inspect_refusals.py`; what is asserted here
    is this operation's own membership in both, plus the two facts that make the grant
    coherent: it is a read, and the scope it needs is one the derived session actually holds.
    """
    entry = get_operation_metadata(GRAPH_TRAVERSE_OPERATION)
    session = local_owner_session(
        principal_id=LOCAL_PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        operations=PRODUCTION_OPERATIONS,
    )

    assert GRAPH_TRAVERSE_OPERATION in build_application_registry().operations
    assert GRAPH_TRAVERSE_OPERATION in session.operations
    assert entry.scope.side_effect == "none"
    assert set(entry.scope.required_scopes) <= session.scopes
    assert entry.required_capability.id == "graph.read"


def test_the_operation_is_absent_from_every_probe_registration(graph: m2.Owned) -> None:
    """The binding clause, at the registry side: absence, not presence.

    A build that registered `graph.traverse` in *both* places would pass a presence-only
    test and would violate the clause -- the probe seam answers service operations, and an
    application operation reachable through it is one that never met the twelve checks.
    """
    assert GRAPH_TRAVERSE_OPERATION not in SERVICE_OPERATIONS
    assert GRAPH_TRAVERSE_OPERATION not in build_service_registry()
    probe = Dispatcher.for_service_operations(
        Grant(
            principal=LOCAL_PRINCIPAL,
            workspaces=frozenset({WORKSPACE_ID}),
            operations=frozenset(SERVICE_OPERATIONS),
        ),
        graph,
    )
    assert GRAPH_TRAVERSE_OPERATION not in probe.grant.operations
    assert refused(probe.dispatch(request_for(start("rec-a")))).error.code == (
        "core.operation_not_implemented"
    )


def test_neither_scope_grant_reaches_the_others_operation(graph: m2.Owned) -> None:
    """The scope negative in both directions, through the real dispatcher.

    `graph.traverse` needs `graph:read` and `memory.search` needs `memory:read`, and the
    two scopes are disjoint -- so a session holding one and not the other is the smallest
    thing that separates the two operations *at the scope check*. Only the scopes are
    narrowed here: both sessions still grant every production operation and every
    capability the production wiring derives, which is asserted directly below so that
    neither refusal can be the operation allowlist stopping the request one check earlier.
    Both are asked of the production path rather than of the seam directly, because what is
    under test is that a request *reaches* the scope check -- a build that routed either
    operation past it would still refuse when the seam is called by hand.

    Falsifier: the same two requests under the full production scope set are answered, so
    the refusals are the scope grant's doing and not the requests'.
    """
    # Read off the frozen catalogue rather than transcribed, so this stays a statement about
    # the scopes the operations actually require.
    graph_scopes = frozenset(get_operation_metadata(GRAPH_TRAVERSE_OPERATION).scope.required_scopes)
    memory_scopes = frozenset(get_operation_metadata(MEMORY_SEARCH_OPERATION).scope.required_scopes)
    assert graph_scopes.isdisjoint(memory_scopes)

    graph_scope_only = production_path(graph, scopes=graph_scopes)
    memory_scope_only = production_path(graph, scopes=memory_scopes)
    denied = message_from_authorization("_MESSAGE_SCOPES_NOT_GRANTED")

    both_operations = {GRAPH_TRAVERSE_OPERATION, MEMORY_SEARCH_OPERATION}
    for dispatcher in (graph_scope_only, memory_scope_only):
        assert both_operations <= dispatcher.session.operations
        assert {"graph.read", "memory.read"} <= {ref.id for ref in dispatcher.session.capabilities}

    reached_memory = refused(
        graph_scope_only.dispatch(
            request_for({"query": "omnivia"}, operation=MEMORY_SEARCH_OPERATION)
        )
    )
    assert reached_memory.error.code == ERROR_CODE_AUTHORIZATION_DENIED
    assert reached_memory.error.message == denied

    reached_graph = refused(memory_scope_only.dispatch(request_for(start("rec-a"))))
    assert reached_graph.error.code == ERROR_CODE_AUTHORIZATION_DENIED
    assert reached_graph.error.message == denied

    full = production_path(graph)
    answered(full.dispatch(request_for(start("rec-a"))))
    answered(full.dispatch(request_for({"query": "omnivia"}, operation=MEMORY_SEARCH_OPERATION)))


def message_from_authorization(name: str) -> str:
    """One of the seam's frozen refusal messages, read off the module by name."""
    constant = getattr(authorization, name)
    assert isinstance(constant, str)
    return constant


# --- 10. Amendment 010: the derived `record.superseded` edge -------------------


def test_one_supersession_maps_to_exactly_one_derived_edge_in_the_history_view(
    graph: m2.Owned,
) -> None:
    """The exact mapping, field by field, and the same edge seen from either end.

    `rec-h` was corrected twice, so `history` holds `v1` and `v2` and the replacement `v3` is
    the current answer. The one complete derived edge is `v1 -> v2`: source is the superseded
    version, target is the replacement, the type is `record.superseded`, and the relation
    reference *is* the replacement -- which is also the edge's record, the same sealed
    `GovernedRecord` the traversal returns as the node for `v2`. Nothing is synthesized: no
    relation record was written for this edge, and the assertion below reads the edge's record
    identity off the edge itself.

    `v2 -> v3` is a real, authoritative supersession and is deliberately *not* here: `v3` is
    current, so it is not a version the history view holds, and an edge is materialized only
    when both of its ends are.

    Falsifier: both orientations are asked, and the relation keeps the database's own
    direction in each. An implementation that anchored source and target on the walk rather
    than on the fact would invert one of the two.
    """
    outbound = traverse(
        graph,
        page(("rec-h", 1), direction=GRAPH_DIRECTION_OUTBOUND,
             relation_types=[SUPERSEDED], depth_limit=1),
    )
    inbound = traverse(
        graph,
        page(("rec-h", 2), direction=GRAPH_DIRECTION_INBOUND,
             relation_types=[SUPERSEDED], depth_limit=1),
    )

    assert steps_of(outbound) == (("ver-rec-h-1", 0), ("ver-rec-h-2", 1))
    assert steps_of(inbound) == (("ver-rec-h-2", 0), ("ver-rec-h-1", 1))
    for result in (outbound, inbound):
        (edge,) = result.edges
        assert edge.relation_type == SUPERSEDED
        assert edge.source is not None and edge.target is not None
        assert (edge.source.record_id, edge.source.version) == step_key("rec-h", 1)
        assert (edge.target.record_id, edge.target.version) == step_key("rec-h", 2)
        assert (
            edge.relation_reference.record_id,
            edge.relation_reference.version,
        ) == step_key("rec-h", 2)
        identity = edge.record.provenance.identity
        assert (identity.record_id, identity.version) == step_key("rec-h", 2)
        assert edge.boundary_reason is None
        # The edge record is the replacement version this same page returned as a node, not a
        # second hydration of it: the traversal states one record, referenced twice.
        (replacement,) = [
            node.record for node in result.nodes if node.reference.version == "ver-rec-h-2"
        ]
        assert edge.record == replacement

    # And the supersession the view does not hold both ends of contributes no edge at all.
    assert {"ver-rec-h-3"}.isdisjoint({version for version, _ in steps_of(outbound)})
    assert graph.connection.execute(
        "SELECT count(*) FROM omnivia_record_supersessions WHERE workspace_id = ? "
        "AND source_version_id = 'ver-rec-h-2' AND target_version_id = 'ver-rec-h-3'",
        (WORKSPACE_ID,),
    ).fetchone()[0] == 1


def test_the_current_canonical_view_omits_a_supersession_without_fabricating_a_boundary(
    graph: m2.Owned,
) -> None:
    """The old source is absent from that view, so the edge is absent -- not deferred.

    `rec-h/v3` is the current answer and `v2 -> v3` is an authoritative supersession whose
    *target* the current view does hold. What it does not hold is the source, and a version a
    view excludes is not a version past the depth limit: a `depth_boundary` anchored at `v3`
    would tell a caller "raise `depth_limit` and you will see it" about a record no depth can
    reveal. The page comes back with the seed and nothing else, at the widest depth this
    build allows.

    Falsifier: the same supersession *is* returnable under `history` when both ends are in
    view (the test above), and asking for the superseded version by name under
    `current_canonical` is refused outright rather than answered from the history table.
    """
    current = traverse(
        graph, page(("rec-h", 3), view=None, direction=GRAPH_DIRECTION_BOTH, depth_limit=8)
    )

    assert steps_of(current) == (("ver-rec-h-3", 0),)
    assert current.edges == ()

    refusal = refused(
        production_path(graph).dispatch(request_for(page(("rec-h", 1), view=None)))
    )
    assert refusal.error.code == ERROR_CODE_NOT_FOUND
    assert refusal.error.message == message("_MESSAGE_UNKNOWN_START")


def test_a_supersession_edge_is_filtered_by_relation_type_like_any_other(
    graph: m2.Owned,
) -> None:
    """`record.superseded` is a relation type a request may name, include and exclude.

    Three pages from one seed. Unfiltered, the walk crosses both kinds of edge and reaches
    `rec-o` two hops out. Filtered to `record.superseded`, the explicit edges are gone and so
    is everything only they reached -- a filtered-out relation is not a filtered-out edge, it
    is a path that does not exist. Filtered to `reconciliation.supports`, the derived edge is
    the one that is gone.
    """
    unfiltered = traverse(
        graph, page(("rec-h", 1), direction=GRAPH_DIRECTION_BOTH, depth_limit=2)
    )
    derived = traverse(
        graph, page(("rec-h", 1), direction=GRAPH_DIRECTION_BOTH,
                    relation_types=[SUPERSEDED], depth_limit=2)
    )
    explicit = traverse(
        graph, page(("rec-h", 1), direction=GRAPH_DIRECTION_BOTH,
                    relation_types=[SUPPORTS], depth_limit=2)
    )

    assert steps_of(unfiltered) == (
        ("ver-rec-h-1", 0),
        ("ver-rec-h-2", 1),
        ("ver-rec-o-1", 1),
        ("ver-rec-o-2", 2),
    )
    assert {edge[1] for edge in step_edges_of(unfiltered)} == {SUPERSEDED, SUPPORTS}

    assert steps_of(derived) == (("ver-rec-h-1", 0), ("ver-rec-h-2", 1))
    assert step_edges_of(derived) == (
        ("ver-rec-h-1", SUPERSEDED, "ver-rec-h-2", "ver-rec-h-2", None),
    )

    assert steps_of(explicit) == (("ver-rec-h-1", 0), ("ver-rec-o-1", 1))
    assert step_edges_of(explicit) == (
        ("ver-rec-h-1", SUPPORTS, "ver-rec-o-1", "ver-rel-x-1", None),
        ("ver-rec-h-1", SUPPORTS, "ver-rec-o-1", "ver-rel-x-2", None),
    )


def test_the_domain_filter_excludes_an_out_of_scope_supersession_and_keeps_the_in_scope_one(
    graph: m2.Owned,
) -> None:
    """One request, two supersessions, one scope: the derived edge is filtered like any other.

    `rec-o`'s chain is `product.other` and `rec-h`'s is `product.core`, and both are reachable
    from the same seed. Under `domain_scope=product.core` the `rec-o` nodes and the `rec-o`
    supersession edge are gone while the `rec-h` supersession edge is untouched -- and the
    excluded end fabricates no boundary, for the same reason the current view fabricates none:
    `rec-o/v1` sits at depth 1 under a depth limit of 2, and no depth would reveal it.

    Falsifier: the identical request without the filter returns both.
    """
    scoped = traverse(
        graph, page(("rec-h", 1), direction=GRAPH_DIRECTION_BOTH,
                    domain_scope="product.core", depth_limit=2)
    )
    unscoped = traverse(
        graph, page(("rec-h", 1), direction=GRAPH_DIRECTION_BOTH, depth_limit=2)
    )

    assert steps_of(scoped) == (("ver-rec-h-1", 0), ("ver-rec-h-2", 1))
    assert step_edges_of(scoped) == (
        ("ver-rec-h-1", SUPERSEDED, "ver-rec-h-2", "ver-rec-h-2", None),
    )

    assert ("ver-rec-o-1", SUPERSEDED, "ver-rec-o-2", "ver-rec-o-2", None) in step_edges_of(
        unscoped
    )
    assert ("ver-rec-o-1", 1) in steps_of(unscoped)


def test_a_supersession_page_that_does_not_fit_its_budget_returns_none_of_it(
    graph: m2.Owned,
) -> None:
    """The all-or-nothing rule, with the supersession as the thing that overflows it.

    The seed alone is one node; the derived edge is what makes the answer two. So a
    `node_limit` of one is a budget the supersession breaks, and this build has no
    continuation token to hand the remainder back with. The mixed page's four edges make the
    same statement against the separate edge budget.

    Falsifier: each request is repeated with a budget that fits, and comes back whole.
    """
    dispatcher = production_path(graph)
    nodes_refused = refused(
        dispatcher.dispatch(
            request_for(
                page(("rec-h", 1), direction=GRAPH_DIRECTION_OUTBOUND,
                     relation_types=[SUPERSEDED], depth_limit=1, node_limit=1)
            )
        )
    )
    assert nodes_refused.error.code == ERROR_CODE_SIZE_LIMIT_EXCEEDED
    assert nodes_refused.error.message == message("_MESSAGE_TOO_LARGE")
    for absent in ('"nodes"', '"edges"', "ver-rec-h-2", SUPERSEDED):
        assert absent not in wire_text(nodes_refused)

    edges_refused = refused(
        dispatcher.dispatch(
            request_for(
                page(("rec-h", 1), direction=GRAPH_DIRECTION_BOTH, depth_limit=2, edge_limit=3)
            )
        )
    )
    assert edges_refused.error.code == ERROR_CODE_SIZE_LIMIT_EXCEEDED
    assert edges_refused.error.message == message("_MESSAGE_TOO_LARGE")

    fits_nodes = traverse(
        graph, page(("rec-h", 1), direction=GRAPH_DIRECTION_OUTBOUND,
                    relation_types=[SUPERSEDED], depth_limit=1, node_limit=2)
    )
    assert len(fits_nodes.nodes) == 2 and len(fits_nodes.edges) == 1
    fits_edges = traverse(
        graph, page(("rec-h", 1), direction=GRAPH_DIRECTION_BOTH, depth_limit=2, edge_limit=4)
    )
    assert len(fits_edges.edges) == 4


def test_an_explicit_edge_wins_a_shared_relation_reference_and_only_that_one_is_dropped(
    graph: m2.Owned,
) -> None:
    """The ratified collision rule, in the shape that actually produces one.

    `rel-x` is a `knowledge.relation` record that was corrected, so its `v2` is two things at
    once: the record asserting an explicit endpoint edge, and the *target* of the `v1 -> v2`
    supersession -- whose derived edge would name `rel-x/v2` as its relation reference too.
    The contract's result validator refuses two edges naming one relation record version, and
    the ratified rule keeps the explicit one.

    Four things are asserted, because dropping the wrong edge, dropping too many, or dropping
    none each fail a different one:

    * the explicit edge resting on `rel-x/v2` is returned, and so is the one resting on
      `rel-x/v1`;
    * no `record.superseded` edge names `rel-x/v2`, and `rel-x/v2` is not reached as a node --
      the suppressed edge was the only path to it from its seed;
    * the *other* supersessions on the same page, including `rec-h`'s, are untouched;
    * every returned edge still names a distinct relation record, which is the validator rule
      the collision would have broken. The handler ran `validate_graph_traversal_result`
      before returning, so this page has already satisfied it.

    Falsifier: the colliding supersession row is really there, and the explicit edge really
    does carry that same version as its `relation_reference` -- both read back below, so a
    build that simply never materialized `rel-x`'s supersession would still have to explain
    the row.
    """
    result = traverse(
        graph,
        page(("rec-h", 1), ("rel-x", 1), direction=GRAPH_DIRECTION_BOTH, depth_limit=2),
    )

    edges = step_edges_of(result)
    assert ("ver-rec-h-1", SUPPORTS, "ver-rec-o-1", "ver-rel-x-1", None) in edges
    assert ("ver-rec-h-1", SUPPORTS, "ver-rec-o-1", "ver-rel-x-2", None) in edges
    assert not [
        edge for edge in edges if edge[1] == SUPERSEDED and edge[3] == "ver-rel-x-2"
    ]
    assert "ver-rel-x-2" not in {version for version, _ in steps_of(result)}

    # Only the colliding one. Both of the page's other supersessions are still edges.
    assert ("ver-rec-h-1", SUPERSEDED, "ver-rec-h-2", "ver-rec-h-2", None) in edges
    assert ("ver-rec-o-1", SUPERSEDED, "ver-rec-o-2", "ver-rec-o-2", None) in edges

    relation_keys = [
        (edge.relation_reference.record_id, edge.relation_reference.version)
        for edge in result.edges
    ]
    assert len(relation_keys) == len(set(relation_keys))

    assert graph.connection.execute(
        "SELECT count(*) FROM omnivia_record_supersessions WHERE workspace_id = ? "
        "AND source_version_id = 'ver-rel-x-1' AND target_version_id = 'ver-rel-x-2'",
        (WORKSPACE_ID,),
    ).fetchone()[0] == 1
    (explicit,) = [
        edge for edge in result.edges if edge.relation_reference.version == "ver-rel-x-2"
    ]
    assert explicit.relation_type == SUPPORTS


def test_the_watermark_includes_the_supersessions_and_an_as_of_excludes_a_later_one(
    graph: m2.Owned,
) -> None:
    """A supersession is an authoritative fact an answer rests on, so it moves the watermark.

    `rec-o`'s last supersession edge row is stamped at `LATE_SUPERSESSION_US`, later than
    every sealed version and every endpoint row in this workspace at that instant -- both read
    back from the database below, so this is a measurement rather than a restatement of the
    fixture. The watermark is that instant, which a version-and-endpoint watermark could not
    produce.

    And it is the snapshot's, not a clock's: one `as_of` before it excludes the fact
    entirely. The watermark falls back to the newest sealed version, and the nodes move with
    it -- `rec-o/v2` is not yet superseded at that instant, so the history view does not hold
    it, so neither it nor the `v1 -> v2` edge is in the answer. Facts after the resolution
    instant affect nodes, edges and watermark alike: none of the three.
    """
    after = traverse(
        graph,
        page(("rec-h", 1), direction=GRAPH_DIRECTION_BOTH, depth_limit=2,
             as_of=AFTER_SUPERSESSION_AS_OF),
    )
    before = traverse(
        graph,
        page(("rec-h", 1), direction=GRAPH_DIRECTION_BOTH, depth_limit=2,
             as_of=BEFORE_SUPERSESSION_AS_OF),
    )

    newest_version = graph.connection.execute(
        "SELECT MAX(recorded_at_us) FROM omnivia_authoritative_governed_versions "
        "WHERE workspace_id = ? AND recorded_at_us <= ?",
        (WORKSPACE_ID, AFTER_SUPERSESSION_US),
    ).fetchone()[0]
    newest_endpoint = graph.connection.execute(
        "SELECT MAX(r.recorded_at_us) FROM omnivia_governed_relation_endpoints r "
        "JOIN omnivia_authoritative_governed_versions a "
        "  ON a.workspace_id = r.workspace_id AND a.assembly_id = r.assembly_id "
        "WHERE r.workspace_id = ? AND r.recorded_at_us <= ?",
        (WORKSPACE_ID, AFTER_SUPERSESSION_US),
    ).fetchone()[0]

    assert newest_version < LATE_SUPERSESSION_US
    assert newest_endpoint < LATE_SUPERSESSION_US
    assert after.freshness.projection_watermarks == {
        "graph.governed_relations": str(LATE_SUPERSESSION_US)
    }
    assert ("ver-rec-o-1", 1) in steps_of(after)

    assert before.freshness.projection_watermarks == {
        "graph.governed_relations": str(newest_version)
    }
    assert steps_of(before) == (("ver-rec-h-1", 0), ("ver-rec-h-2", 1), ("ver-rec-o-1", 1))
    assert "ver-rec-o-2" not in {edge[3] for edge in step_edges_of(before)}


def test_ordinary_and_derived_edges_share_one_deterministic_order(graph: m2.Owned) -> None:
    """One page, three relation types, one sort key -- and the same page twice.

    The derived edges are not appended after the ordinary ones or grouped by kind: the whole
    sequence is ordered by the validator's own complete tuple, which interleaves them. Asserted
    as an exact sequence rather than as a set, because "sorted" is the property a build could
    lose while still returning the right edges.
    """
    payload = page(("rec-h", 1), ("rel-x", 1), direction=GRAPH_DIRECTION_BOTH, depth_limit=2)
    first = traverse(graph, payload)
    second = traverse(graph, payload)

    assert step_edges_of(first) == step_edges_of(second)
    assert steps_of(first) == steps_of(second)
    assert step_edges_of(first) == (
        ("ver-rec-h-1", SUPPORTS, "ver-rec-o-1", "ver-rel-x-1", None),
        ("ver-rec-h-1", SUPPORTS, "ver-rec-o-1", "ver-rel-x-2", None),
        ("ver-rec-h-1", SUPERSEDED, "ver-rec-h-2", "ver-rec-h-2", None),
        ("ver-rec-o-1", SUPERSEDED, "ver-rec-o-2", "ver-rec-o-2", None),
    )

    edge_keys = [
        (
            *(("", "") if edge.source is None else (edge.source.record_id, edge.source.version)),
            edge.relation_type,
            *(("", "") if edge.target is None else (edge.target.record_id, edge.target.version)),
            edge.relation_reference.record_id,
            edge.relation_reference.version,
        )
        for edge in first.edges
    ]
    assert edge_keys == sorted(edge_keys)
    assert graph_storage.RELATION_TYPE_SUPERSEDED == SUPERSEDED


def test_a_supersession_committed_mid_read_is_wholly_absent_from_that_snapshot(
    graph: m2.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two connections, one WAL database, and a correction committed *during* the read.

    The reader is paused at the one place a leak could hide: the read transaction is open and
    the frontier has been resolved, but the endpoint rows, the supersessions and the watermark
    have not been read yet. A second connection -- the same file, the same lease, authority
    being rows rather than a handle -- then commits `rec-w`'s correction and returns.

    The service's own connection cannot be either of the two. `OpenMode.SERVICE_OWNED` sets
    `PRAGMA locking_mode = EXCLUSIVE`, and under WAL that means the owner never releases its
    locks and no second connection may touch the file at all -- which is the production
    guarantee, not something to work around. So the owner is closed first and both connections
    here are ordinary ones over the same WAL file, with the reader given the same
    deny-by-default authorizer the service connection carries, so the read path under test is
    the production one in everything but the lock mode. `journal_mode` is asserted below,
    because a database that had fallen back to a rollback journal would serialize the two and
    the interleaving would never happen.

    The answer that read produces must be wholly *before* that commit: `rec-w/v1` is not in its
    history view, no `record.superseded` edge names it, and the watermark is the one the
    workspace had before the write. A snapshot that read supersessions outside the transaction
    -- or took its watermark from a later query, or from a clock -- would answer with a
    frontier from before the commit and an edge or a watermark from after it, which is the
    mixed answer this shape exists to make impossible.

    The second read, over the same connection with no pause, is the wholly *after* half: the
    edge is there and the watermark has moved to the new fact. Without it, a build that simply
    never materialized supersessions would pass the first half.
    """
    real_frontier = graph_storage.read_governed_record_values
    graph.connection.close()
    reader = open_database(graph.path, OpenMode.EPHEMERAL)
    install_authorizer(reader, allow_mutations=False, allow_ddl=False)
    mark_authorizer_installed(reader)
    writer = open_database(graph.path, OpenMode.EPHEMERAL)
    for connection in (reader, writer):
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    paused: list[bool] = []

    def pause_after_the_frontier(
        connection: sqlite3.Connection, **selectors: Any
    ) -> Any:
        values = real_frontier(connection, **selectors)
        if not paused:
            # Inside the reader's transaction, before any graph fact has been materialized.
            paused.append(bool(reader.in_transaction))
            # Two corrections, because one would move only the frontier and the watermark: a
            # *complete* derived edge needs `v2` to be history too, and that is what `v3`
            # makes it. So the write that lands mid-read is a whole node, edge and watermark
            # change at once -- every part of the answer this snapshot is assembling.
            for step, instant in ((2, MIDREAD_US), (3, MIDREAD_LAST_US)):
                governed_version(
                    graph,
                    record_id="rec-w",
                    version_id=f"ver-rec-w-{step}",
                    audit_ref="audit-31",
                    ordinal=step + 1,
                    recorded_at_us=instant,
                    digest="sha256:" + str(step + 6) * 64,
                    supersedes=f"ver-rec-w-{step - 1}",
                    connection=writer,
                )
        return values

    monkeypatch.setattr(
        graph_storage, "read_governed_record_values", pause_after_the_frontier
    )
    try:
        during = read_graph_snapshot(
            reader,
            workspace_id=WORKSPACE_ID,
            resolution_instant_us=CUTOFF_US,
            view="history",
        )
    finally:
        monkeypatch.setattr(graph_storage, "read_governed_record_values", real_frontier)

    # The reader really was mid-transaction when the write landed, and the write really did
    # commit -- neither half of the falsifier is vacuous.
    assert paused == [True]
    assert writer.execute(
        "SELECT count(*) FROM omnivia_record_supersessions WHERE workspace_id = ? "
        "AND source_version_id IN ('ver-rec-w-1', 'ver-rec-w-2')",
        (WORKSPACE_ID,),
    ).fetchone()[0] == 2

    assert step_key("rec-w", 1) not in during.records
    assert not [
        relation for relation in during.relations if relation.source[0] == "rec-w"
    ]
    assert during.watermark_us == LATE_SUPERSESSION_US

    after = read_graph_snapshot(
        reader,
        workspace_id=WORKSPACE_ID,
        resolution_instant_us=CUTOFF_US,
        view="history",
    )
    assert step_key("rec-w", 1) in after.records
    assert [
        (relation.relation_type, relation.source, relation.target)
        for relation in after.relations
        if relation.source[0] == "rec-w"
    ] == [(SUPERSEDED, step_key("rec-w", 1), step_key("rec-w", 2))]
    assert after.watermark_us == MIDREAD_LAST_US
    reader.close()
    writer.close()
