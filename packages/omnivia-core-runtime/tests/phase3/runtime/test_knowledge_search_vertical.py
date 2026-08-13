"""V06-3 Lane C: the frozen governed frontier and the purity of its ranker.

The governed half of what `test_retrieval_filter_chain.py` proves for evidence: §7.2's
"nothing ranks what a filter has not already passed", §20.12's four prohibitions on what a
ranker may hold, and §8.2's determinism requirements -- over `GovernedFrontier` and
`rank_governed` rather than over `AuthorizedFrontier` and the evidence rankers.

**Why the obvious test is worthless, restated because it governs this whole file.** A test
that ranks an authorized frontier and asserts the page contains only authorized records
passes identically whether the ranker observed the frontier alone or read the whole
workspace and dropped the rest at the end -- and it passes when an excluded record silently
moved the relative order of the records that *were* returned, which is the failure Lane B
shipped once already. So every positive test below states its falsifier, the mutations that
make those falsifiers real are exercised as tests rather than argued in prose, and the
frontier tests assert the *negative and its complement*: that a record outside the frontier
changes nothing, and that the same record inside it changes the page -- because a test that
only asserts "nothing changed" also passes when the ranker ignores its input entirely.

**Part A is unit-level**, over values rather than over a database, because every property
in it is a property of a pure function over a frozen value. That is not an accident of test
convenience: it is the seam. `governed.py` holds the connection and resolves the versions;
this ranker is handed a value and can reach nothing else, and the isolation tests there are
what make "can reach nothing else" checkable rather than asserted.

**Part B is the vertical**, and nothing on it is stubbed: a real workspace database migrated
through 0009, rows seeded through the accepted fenced writer, a real production session, a
real `ServiceBinding`, real `RequestEnvelope`s, all twelve checks of
`authorize_application_request`, the registered handlers, and conformant
`KnowledgeSearchResult`/`MemorySearchResult` documents on the way out. Two devices make its
claims observable rather than inferred, because a page alone cannot show either:

* the frontier the handler actually froze is captured by wrapping `rank_governed` where the
  handler resolves it, so "an excluded record never reached ranking" is read off the
  ranker's own input instead of guessed from what came back;
* every SQL statement the connection executes is traced, and the count at the moment of the
  freeze is compared with the count at the end, so "no post-freeze SQL, no lazy lookup"
  is a measurement.

The whole of Part B runs with **no active compatible FTS projection installed**, and B1
proves that from the schema rather than assuming it: no FTS table, no 0011 activation
ledger for a projection to be registered in, an empty projection ledger, and no
session-attached projection -- while both governed handlers answer over that same
connection. `evidence.search` is not exercised there, because the 0009-stopped schema
lacks the later lifecycle tables its handler reads; its absence of a projection is read
off the database instead.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import json
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_governed_truth_and_relations_migration as m3
import test_v06_5_s2_memory_family as s2
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service.application import (
    CONTEXT_PACK_BUILD_OPERATION,
    EVIDENCE_SEARCH_OPERATION,
    GRAPH_TRAVERSE_OPERATION,
    KNOWLEDGE_RETRIEVAL_PURPOSE,
    KNOWLEDGE_SEARCH_OPERATION,
    LOCAL_TRANSPORT_ADAPTER,
    MEMORY_SEARCH_OPERATION,
    OPERATION_PURPOSES,
    WORKSPACE_INSPECT_OPERATION,
    ApplicationDispatcher,
    build_application_registry,
    local_owner_session,
)
from omnivia_core_runtime.service.authorization import Grant, ServiceBinding
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers import knowledge as knowledge_handlers
from omnivia_core_runtime.service.main import LOCAL_PRINCIPAL
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    OperationContext,
    OperationError,
    server_capability_snapshot,
)
from omnivia_core_runtime.storage import retrieval as retrieval_module
from omnivia_core_runtime.storage.projections.fts import session_search_projection
from omnivia_core_runtime.storage.retrieval import (
    GOVERNED_FRONTIER_FILTERS,
    GovernedCandidate,
    GovernedFrontier,
    governed_order_key,
    governed_search_text,
    rank_governed,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    GOVERNANCE_LAYER_GOVERNED,
    GOVERNANCE_STATE_ACCEPTED,
    GOVERNED_RECORD_VIEW_CANDIDATES,
    GOVERNED_RECORD_VIEW_CURRENT_CANONICAL,
    GOVERNED_RECORD_VIEW_HISTORY,
    KNOWLEDGE_SEARCH_CANONICAL_AUTHORITY_LEVEL,
    KNOWLEDGE_SEARCH_ORDER_RECENCY,
    KNOWLEDGE_SEARCH_ORDER_RELEVANCE,
    CapabilityRef,
    CapabilityRequirement,
    ClientIdentity,
    ContractSemanticError,
    ErrorResponseEnvelope,
    GovernedRecord,
    KnowledgeSearchInput,
    KnowledgeSearchResult,
    MemorySearchInput,
    MemorySearchResult,
    PageMetadata,
    RecordIdentity,
    RecordProvenance,
    RecordTemporalMetadata,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
    SuccessResponseEnvelope,
    encode_response,
    get_operation_metadata,
)
from omnivia_core.contracts.v1.semantics_knowledge import validate_memory_search_result

WORKSPACE_ID = "ws-knowledge-0001"
BASE_US = 1_700_000_000_000_000

#: Both orderings, so every ordering-independent property below is asserted under both
#: rather than under whichever one happened to be written first.
BOTH_ORDERS = (None, KNOWLEDGE_SEARCH_ORDER_RELEVANCE, KNOWLEDGE_SEARCH_ORDER_RECENCY)


def record(
    record_id: str,
    *,
    version: str = "v1",
    content: dict[str, Any] | None = None,
    workspace_id: str = WORKSPACE_ID,
) -> GovernedRecord:
    """One hydrated governed record, built from the real contract DTOs.

    Real DTOs rather than stubs because the ranker reads its tie-break off
    `provenance.identity` and its matched surface off `content`: a stub would let the test
    agree with an implementation that had stopped reading either.
    """
    return GovernedRecord(
        workspace_id=workspace_id,
        record_type="note",
        domain_scope="workspace",
        authority_level=KNOWLEDGE_SEARCH_CANONICAL_AUTHORITY_LEVEL,
        provenance=RecordProvenance(
            identity=RecordIdentity(
                record_id=record_id,
                version=version,
                layer=GOVERNANCE_LAYER_GOVERNED,
                governance_state=GOVERNANCE_STATE_ACCEPTED,
                currentness="current",
            ),
            temporal=RecordTemporalMetadata(
                ingested_at="2023-11-14T22:13:20.000Z",
                recorded_at="2023-11-14T22:13:20.000Z",
            ),
            history=(),
            evidence_disposition="asserted_without_evidence",
            sources=(),
        ),
        content={"body": ""} if content is None else content,
    )


def candidate(
    record_id: str,
    *,
    version: str = "v1",
    body: str = "alpha",
    content: dict[str, Any] | None = None,
    recorded_at_us: int = BASE_US,
) -> GovernedCandidate:
    """One frontier member. `body` is the shorthand for the common one-field content."""
    return GovernedCandidate(
        recorded_at_us=recorded_at_us,
        record=record(
            record_id,
            version=version,
            content={"body": body} if content is None else content,
        ),
    )


def frontier(*candidates: GovernedCandidate) -> GovernedFrontier:
    return GovernedFrontier(
        workspace_id=WORKSPACE_ID,
        candidates=candidates,
        filters_applied=GOVERNED_FRONTIER_FILTERS,
    )


def ids(records: tuple[GovernedRecord, ...]) -> tuple[tuple[str, str], ...]:
    """A page as its `(record_id, version)` pairs -- the pair the tie-break is total on."""
    return tuple(
        (item.provenance.identity.record_id, item.provenance.identity.version)
        for item in records
    )


# --- the frontier states what narrowed it -------------------------------------


def test_the_frontier_names_the_filters_the_caller_must_have_applied() -> None:
    """The six filters the handler runs before the freeze, in the order it runs them.

    Falsifier: drop one from the tuple and the frontier claims a narrowing that never
    happened, which is the one thing about this value a reviewer cannot check from the
    ranker -- the ranker is forbidden the store it would need to re-derive them.
    """
    assert GOVERNED_FRONTIER_FILTERS == (
        "workspace",
        "view",
        "governance",
        "temporal",
        "record_type",
        "domain_scope",
    )
    assert frontier().filters_applied == GOVERNED_FRONTIER_FILTERS


def test_the_frontier_and_its_candidates_are_immutable_values() -> None:
    """Frozen in the dataclass sense, so nothing downstream can widen a page in place."""
    built = frontier(candidate("rec-a"))
    member = built.candidates[0]

    with pytest.raises(FrozenInstanceError):
        built.candidates = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        member.recorded_at_us = 0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        member.record.workspace_id = "ws-other"  # type: ignore[misc]


def test_the_candidates_ranking_identity_is_the_records_own() -> None:
    """`record_id`/`version` are read off the record, not duplicated beside it.

    Falsifier: carry them as fields and the tie-break can be computed from an identity
    that disagrees with the record the caller is handed. Here that state is unrepresentable
    -- there is no second copy to fall out of step.
    """
    member = candidate("rec-a", version="v7")

    assert (member.record_id, member.version) == ("rec-a", "v7")
    assert member.record_id == member.record.provenance.identity.record_id
    assert member.version == member.record.provenance.identity.version


# --- 1: the matched surface is normalized, and stable under insertion order ----


def test_matching_normalizes_both_sides_by_nfkc_and_case_folding() -> None:
    """A match is a property of the text, not of the form either side was typed in.

    Two distinct falsifiers, one per normalization, so neither can pass on the other's
    behalf: `office` matches `oﬃce` only under **NFKC** (U+FB03 decomposes to `ffi`, and
    case folding alone leaves it alone), and `LEDGER` matches `ledger` only under **case
    folding** (NFKC alone leaves case alone).
    """
    ligature = frontier(candidate("rec-a", body="the oﬃce ledger"))

    assert ids(rank_governed(ligature, "office", order=None, limit=10)) == (("rec-a", "v1"),)
    assert ids(rank_governed(ligature, "LEDGER", order=None, limit=10)) == (("rec-a", "v1"),)
    assert ids(rank_governed(ligature, "OFFICE LEDGER", order=None, limit=10)) == (
        ("rec-a", "v1"),
    )
    # And the query side is normalized too, not just the content side.
    assert ids(rank_governed(frontier(candidate("rec-a", body="office")), "OﬃCE", order=None, limit=10)) == (
        ("rec-a", "v1"),
    )


def test_the_matched_surface_is_independent_of_mapping_insertion_order() -> None:
    """Canonical serialization, so matchability is what a record says, not how it was built.

    Falsifier: serialize with `str(content)` or `json.dumps` without `sort_keys`, and the
    two mappings below -- equal as mappings, built in opposite orders, nested included --
    produce different text, different occurrence counts and therefore a different page.
    """
    forward: dict[str, Any] = {"alpha": "one", "beta": "two"}
    forward["nested"] = {"x": "alpha alpha", "y": "beta"}
    backward: dict[str, Any] = {}
    backward["nested"] = {"y": "beta", "x": "alpha alpha"}
    backward["beta"] = "two"
    backward["alpha"] = "one"

    assert list(forward) != list(backward)
    assert forward == backward
    assert governed_search_text(record("rec-a", content=forward)) == governed_search_text(
        record("rec-a", content=backward)
    )

    # And the property that actually matters: the same content ranks the same either way.
    for order in BOTH_ORDERS:
        assert rank_governed(
            frontier(
                candidate("rec-a", content=forward),
                candidate("rec-b", content=backward),
            ),
            "alpha",
            order=order,
            limit=10,
        ) == rank_governed(
            frontier(
                candidate("rec-a", content=backward),
                candidate("rec-b", content=forward),
            ),
            "alpha",
            order=order,
            limit=10,
        )


# --- 2: ordering, under both orders, total and deterministic ------------------


def test_relevance_and_recency_are_different_orders_over_one_frontier() -> None:
    """One fixture, two orders, opposite pages -- so neither test can pass by accident.

    Occurrence count and recency are arranged to contradict each other exactly: the record
    that mentions the query most is the oldest. Falsifier for relevance: ignore the count
    and the page comes back in recency order. Falsifier for recency: leave the count in the
    key and the page comes back in relevance order. Neither mutation can satisfy both
    assertions.
    """
    built = frontier(
        candidate("rec-hot", body="alpha alpha alpha", recorded_at_us=BASE_US),
        candidate("rec-mid", body="alpha alpha", recorded_at_us=BASE_US + 1),
        candidate("rec-cold", body="alpha", recorded_at_us=BASE_US + 2),
    )

    ordered_by_count = (("rec-hot", "v1"), ("rec-mid", "v1"), ("rec-cold", "v1"))
    ordered_by_instant = (("rec-cold", "v1"), ("rec-mid", "v1"), ("rec-hot", "v1"))

    assert ids(rank_governed(built, "alpha", order=KNOWLEDGE_SEARCH_ORDER_RELEVANCE, limit=10)) == ordered_by_count
    assert ids(rank_governed(built, "alpha", order=KNOWLEDGE_SEARCH_ORDER_RECENCY, limit=10)) == ordered_by_instant


def test_an_absent_order_is_relevance_rather_than_whatever_ran_first() -> None:
    """`None` resolves to relevance, and the fixture above proves that is not recency."""
    built = frontier(
        candidate("rec-hot", body="alpha alpha alpha", recorded_at_us=BASE_US),
        candidate("rec-cold", body="alpha", recorded_at_us=BASE_US + 2),
    )

    assert rank_governed(built, "alpha", order=None, limit=10) == rank_governed(
        built, "alpha", order=KNOWLEDGE_SEARCH_ORDER_RELEVANCE, limit=10
    )
    assert rank_governed(built, "alpha", order=None, limit=10) != rank_governed(
        built, "alpha", order=KNOWLEDGE_SEARCH_ORDER_RECENCY, limit=10
    )


@pytest.mark.parametrize("order", BOTH_ORDERS)
def test_an_equal_score_at_an_equal_instant_breaks_on_record_id_then_version(
    order: str | None,
) -> None:
    """§8.2's first ordering hazard, in the shape governed truth actually produces it.

    Every record below scores identically and was recorded at the same microsecond, which
    is not exotic: a correction sealed in one transaction writes every version it touches
    at one instant. Two falsifiers, and the fixture separates them -- break on
    `recorded_at_us` alone and the page is insertion order; break on `record_id` alone and
    `rec-a`'s two versions stay in insertion order while `rec-b` looks correctly placed.
    """
    same = (
        candidate("rec-b", version="v1"),
        candidate("rec-a", version="v2"),
        candidate("rec-a", version="v1"),
    )

    assert ids(rank_governed(frontier(*same), "alpha", order=order, limit=10)) == (
        ("rec-a", "v1"),
        ("rec-a", "v2"),
        ("rec-b", "v1"),
    )


@pytest.mark.parametrize("order", BOTH_ORDERS)
def test_every_input_permutation_yields_the_identical_page(order: str | None) -> None:
    """The order is total, so the page is a function of the set and not of its arrival.

    Falsifier: any key that leaves two members comparing equal. Ties on score, on instant
    and on `record_id` are all present below at once, so a key missing any one of its four
    components lets at least one permutation disagree.
    """
    members = (
        candidate("rec-b", version="v1", body="alpha", recorded_at_us=BASE_US),
        candidate("rec-a", version="v2", body="alpha", recorded_at_us=BASE_US),
        candidate("rec-a", version="v1", body="alpha alpha", recorded_at_us=BASE_US),
        candidate("rec-c", version="v1", body="alpha", recorded_at_us=BASE_US + 5),
    )
    pages = {
        ids(rank_governed(frontier(*permutation), "alpha", order=order, limit=10))
        for permutation in itertools.permutations(members)
    }

    assert len(pages) == 1


def test_the_order_key_states_its_directions_rather_than_relying_on_a_sort() -> None:
    """Relevance descending, recency descending, identity ascending -- read off the key.

    Falsifier: forget a negation and the key still sorts, silently backwards, which is a
    defect no assertion over a single page reliably catches.
    """
    member = candidate("rec-a", version="v1", recorded_at_us=BASE_US)

    assert governed_order_key(member, 3) == (-3, -BASE_US, "rec-a", "v1")
    assert governed_order_key(member, 3) < governed_order_key(member, 1)
    assert governed_order_key(
        candidate("rec-a", recorded_at_us=BASE_US + 1), 0
    ) < governed_order_key(candidate("rec-a", recorded_at_us=BASE_US), 0)


# --- 3: the frontier bounds the page, and bounds the ordering too -------------


@pytest.mark.parametrize("order", BOTH_ORDERS)
def test_a_record_outside_the_frontier_changes_neither_membership_nor_order(
    order: str | None,
) -> None:
    """§7.2's acceptance property, asserted with its complement so it cannot pass vacuously.

    `smuggled` is built to dominate under *both* orderings at once -- it mentions the query
    fifty times and is the most recently recorded -- so if the ranker could observe it, it
    would sort first and the assertion would fail rather than merely being unaffected.

    The second half is what makes the first half mean something. A test that only asserted
    "the page did not change" would also pass against a ranker that ignored its input, so
    this widens the frontier by exactly that record and shows the page *does* change. The
    difference between the two halves is the frontier and nothing else: same query, same
    order, same limit, same records.
    """
    authorized = (
        candidate("rec-a", body="alpha alpha", recorded_at_us=BASE_US),
        candidate("rec-b", body="alpha", recorded_at_us=BASE_US + 1),
    )
    smuggled = candidate(
        "rec-unauthorized",
        body=" ".join(["alpha"] * 50),
        recorded_at_us=BASE_US + 999,
    )

    narrow = ids(rank_governed(frontier(*authorized), "alpha", order=order, limit=10))
    widened = ids(
        rank_governed(frontier(*authorized, smuggled), "alpha", order=order, limit=10)
    )

    assert ("rec-unauthorized", "v1") not in narrow
    assert set(narrow) == {("rec-a", "v1"), ("rec-b", "v1")}
    # The relative order of the authorized members is identical with and without it. This
    # is the assertion Lane B's first design would have failed: it kept the *page* inside
    # the frontier while letting an excluded artifact's statistics move the members within
    # it, which no membership assertion catches.
    assert narrow == tuple(item for item in widened if item != ("rec-unauthorized", "v1"))
    # The complement: inside the frontier, that same record leads the page under both
    # orderings. So the assertions above are facts about the frontier, not tautologies.
    assert widened[0] == ("rec-unauthorized", "v1")
    assert widened != narrow


@pytest.mark.parametrize("order", BOTH_ORDERS)
def test_no_query_widens_the_frontier(order: str | None) -> None:
    """Selection narrows. There is no query, under either order, that adds a member.

    Falsifier: any fallback to a source other than `frontier.candidates` when the query
    matches nothing -- F4's shape, arrived at by accident rather than by design.
    """
    built = frontier(candidate("rec-a", body="alpha"), candidate("rec-b", body="beta"))
    held = set(ids(tuple(member.record for member in built.candidates)))

    for query in ("alpha", "beta", "rec-b", "ﬃ", "}", '{"body":', "nothing here"):
        assert set(ids(rank_governed(built, query, order=order, limit=100))) <= held


# --- 4: the limit, the empty frontier and the unmatched query -----------------


@pytest.mark.parametrize("order", BOTH_ORDERS)
def test_the_limit_bounds_the_page_and_not_the_frontier(order: str | None) -> None:
    """A page is a view of the frontier; the frontier is not narrowed by the page size.

    Falsifier: apply the limit before ordering and the page is the first N in arrival
    order rather than the first N in the total order -- here, `rec-4` would be absent.
    """
    built = frontier(
        *(
            candidate(f"rec-{index}", body="alpha " * (index + 1), recorded_at_us=BASE_US + index)
            for index in range(5)
        )
    )

    assert len(built.candidates) == 5
    assert len(rank_governed(built, "alpha", order=order, limit=2)) == 2
    assert ids(rank_governed(built, "alpha", order=order, limit=2)) == ids(
        rank_governed(built, "alpha", order=order, limit=100)
    )[:2]
    assert ids(rank_governed(built, "alpha", order=order, limit=2))[0] == ("rec-4", "v1")
    assert len(built.candidates) == 5


@pytest.mark.parametrize("order", BOTH_ORDERS)
def test_a_non_positive_limit_returns_nothing_rather_than_slicing_backwards(
    order: str | None,
) -> None:
    """Falsifier: a bare `[:limit]` with `limit=-1` returns everything but the last item."""
    built = frontier(candidate("rec-a"), candidate("rec-b"))

    assert rank_governed(built, "alpha", order=order, limit=0) == ()
    assert rank_governed(built, "alpha", order=order, limit=-1) == ()


@pytest.mark.parametrize("order", BOTH_ORDERS)
def test_an_empty_frontier_an_unmatched_query_and_an_empty_query_are_all_empty(
    order: str | None,
) -> None:
    """Three ways to select nothing, none of them an error and none of them everything.

    The empty query is the one worth stating: a bare substring test admits it -- `""` is
    in every string -- so without the guard an empty query would return the whole frontier
    scored by content length. It fails closed instead.
    """
    assert rank_governed(frontier(), "alpha", order=order, limit=10) == ()
    built = frontier(candidate("rec-a", body="alpha"), candidate("rec-b", body="beta"))
    assert rank_governed(built, "gamma", order=order, limit=10) == ()
    assert rank_governed(built, "", order=order, limit=10) == ()


# --- 5: an unrecognized order is refused, not resolved -------------------------


@pytest.mark.parametrize(
    "order",
    ["Relevance", "RECENCY", "relevance ", "", "score", "created_at", "random"],
)
def test_an_unrecognized_order_is_refused_rather_than_defaulted(order: str) -> None:
    """Fail closed, the same direction `resolve_governed_versions` takes for a view.

    Falsifier: resolve an unknown value into relevance and the caller is served an
    ordering it did not ask for under the name of one it did -- silently, since a page
    ordered by *something* looks exactly like a page ordered correctly.
    """
    built = frontier(candidate("rec-a"))

    with pytest.raises(ValueError, match="not a recognized MemorySearchOrder"):
        rank_governed(built, "alpha", order=order, limit=10)


def test_the_refusal_does_not_carry_the_supplied_order_back_out() -> None:
    """The message is server-authored end to end: requirement and selectors, no echo.

    Falsifier: format the supplied order into the message and the refusal becomes a
    carrier for caller-supplied text -- into logs, into whatever renders the error, and
    back to a caller who now learns what this build rejects by reading its own value
    quoted back. The distinctive sentinel below appears nowhere in this build, so its
    absence from the message can only mean the message never contained it.
    """
    built = frontier(candidate("rec-a"))
    sentinel = "zz-caller-sentinel-9f3c1d<script>"

    with pytest.raises(ValueError, match="not a recognized MemorySearchOrder") as exc:
        rank_governed(built, "alpha", order=sentinel, limit=10)

    assert sentinel not in str(exc.value)


def test_the_refusal_precedes_the_query_and_does_not_depend_on_there_being_a_match() -> None:
    """An empty frontier and an empty query still refuse an unknown order.

    Falsifier: validate the order after the early returns and an unknown order is accepted
    for exactly the requests that return nothing -- which is where it would go unnoticed
    longest and then start serving pages the day the frontier stopped being empty.
    """
    for built, query, limit in (
        (frontier(), "alpha", 10),
        (frontier(candidate("rec-a")), "", 10),
        (frontier(candidate("rec-a")), "alpha", 0),
    ):
        with pytest.raises(ValueError, match="not a recognized MemorySearchOrder"):
            rank_governed(built, query, order="score", limit=limit)


# --- 6: §20.12, all four prohibitions, none substituting for another ----------


#: F4's `module_global` form has to be built at module scope to be that form at all. A
#: ranker defined inside the test function and reading a local of it is a *closure* -- the
#: same mutation as the `closure` branch wearing a different name, which is exactly the
#: substitution this file forbids. So the store is bound into this module's globals for the
#: duration of that branch, and the ranker below resolves it there at call time, capturing
#: nothing. The placeholder keeps the name resolvable and gives `monkeypatch` a value to
#: restore.
_MODULE_STORE: Any = None


def _defective_global(f: GovernedFrontier, q: str) -> tuple[GovernedRecord, ...]:
    """The module_global mutation: no store parameter, no cell -- a global lookup."""
    return (*(member.record for member in f.candidates), _MODULE_STORE.extra())


def test_isolation_1_the_ranker_takes_no_store_shaped_parameter() -> None:
    """Proof one of three: the signature. Necessary and, the owner ruled, not sufficient.

    Four parameters, all of them values, none of them callable and none of them carrying a
    default a handle could be bound into. A callback is the prohibition this catches that
    an import check cannot: `fetch: Callable[..., GovernedRecord]` imports nothing.
    """
    signature = inspect.signature(rank_governed)

    assert list(signature.parameters) == ["frontier", "query", "order", "limit"]
    assert signature.parameters["frontier"].annotation == "GovernedFrontier"
    assert signature.parameters["query"].annotation == "str"
    assert signature.parameters["order"].annotation == "str | None"
    assert signature.parameters["limit"].annotation == "int"
    for parameter in signature.parameters.values():
        assert parameter.default is inspect.Parameter.empty


def test_isolation_2_the_module_imports_nothing_that_could_reach_a_row() -> None:
    """Proof two: the import boundary, which is what a signature cannot show.

    Read from the module's own `import` statements rather than its runtime namespace, so a
    lazy import inside a function does not evade it.

    `omnivia_core_runtime.storage.governed` is the one this slice adds and the one that
    matters most here: it is where the governed frontier is actually resolved, it holds a
    `sqlite3.Connection`, and it is exactly the module a later edit would reach for to
    "just re-check governance" inside the ranker. Importing it would put a store back
    within reach without any signature changing.
    """
    source = Path(inspect.getsourcefile(retrieval_module) or "").read_text()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    for forbidden in (
        "sqlite3",
        "omnivia_core_runtime.storage.governed",
        "omnivia_core_runtime.storage.connection",
        "omnivia_core_runtime.storage.repository",
        "omnivia_core_runtime.storage.projections",
        "omnivia_core_runtime.storage.projections.fts",
    ):
        assert forbidden not in imported

    assert not any(
        name.startswith("omnivia_core_runtime.") for name in imported
    ), sorted(imported)


def test_isolation_3_no_module_global_and_no_closure_holds_a_store() -> None:
    """Proof three: the two hiding places a signature and an import list both miss.

    A store bound at module scope, and a store captured in a closure. The second is checked
    directly -- `rank_governed.__closure__` is `None`, so the function has captured nothing
    at all and there is no cell a handle could be sitting in.
    """
    for name, value in vars(retrieval_module).items():
        if name.startswith("__"):
            continue
        assert not hasattr(value, "execute"), name
        assert not hasattr(value, "cursor"), name

    for function in (rank_governed, governed_search_text, governed_order_key):
        assert function.__closure__ is None, function.__name__
        assert function.__defaults__ is None, function.__name__
        assert function.__kwdefaults__ is None, function.__name__


@pytest.mark.parametrize("form", ["parameter", "module_global", "closure", "callback"])
def test_f4_a_governed_ranker_given_a_live_store_in_any_form_is_killed(
    form: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4, demonstrated as the mutation it is, in all four forms §20.12 names.

    Each branch builds the defective ranker the prohibition describes, has it read one
    extra record, and shows the page carrying a record that is **not** a member of the
    frozen frontier. That is the kill: `rank_governed` cannot be written any of these four
    ways, because its module imports nothing that could supply the store, its signature
    takes no callback, and it captures no cell. This test is what makes "cannot" something
    a reviewer checks rather than something the packet asserts.
    """
    frozen = frontier(candidate("rec-a", body="alpha"))
    smuggled = record("rec-unauthorized", content={"body": "alpha alpha alpha"})

    class Store:
        """Stands in for the resolver's connection: it can produce an unauthorized row."""

        def extra(self) -> GovernedRecord:
            return smuggled

    store = Store()

    if form == "parameter":

        def defective(
            f: GovernedFrontier, q: str, *, store: Store
        ) -> tuple[GovernedRecord, ...]:
            return (*(member.record for member in f.candidates), store.extra())

        leaked = defective(frozen, "alpha", store=store)
    elif form == "module_global":
        # Bind into the ranker's own `__globals__`, and let monkeypatch put the placeholder
        # back afterwards, so no live store outlives this branch.
        monkeypatch.setitem(_defective_global.__globals__, "_MODULE_STORE", store)

        # Structural, so this branch cannot silently regress into a second closure test:
        # the ranker holds no cells at all, and the store it reaches is a module global.
        assert _defective_global.__closure__ is None
        assert _defective_global.__globals__["_MODULE_STORE"] is store

        leaked = _defective_global(frozen, "alpha")
    elif form == "closure":

        def make(bound: Store) -> Any:
            def ranker(f: GovernedFrontier, q: str) -> tuple[GovernedRecord, ...]:
                return (*(member.record for member in f.candidates), bound.extra())

            return ranker

        leaked = make(store)(frozen, "alpha")
    else:

        def defective_callback(
            f: GovernedFrontier, q: str, *, fetch: Any
        ) -> tuple[GovernedRecord, ...]:
            return (*(member.record for member in f.candidates), fetch())

        leaked = defective_callback(frozen, "alpha", fetch=store.extra)

    # The defect, made visible: a record in the page the frontier never held.
    assert ("rec-unauthorized", "v1") in ids(leaked)
    held = set(ids(tuple(member.record for member in frozen.candidates)))
    assert ("rec-unauthorized", "v1") not in held

    # And the real ranker, given the same frozen frontier, cannot produce it.
    for order in BOTH_ORDERS:
        ranked = rank_governed(frozen, "alpha", order=order, limit=100)
        assert set(ids(ranked)) <= held


# --- 7: the page is the frontier's own objects, not a reconstruction ----------


@pytest.mark.parametrize("order", BOTH_ORDERS)
def test_the_page_returns_the_frontiers_own_record_objects(order: str | None) -> None:
    """Identity, not equality: every returned record *is* a frozen member of the frontier.

    This is the structural half of §7.2. Because the page is built by mapping over frozen
    members, "every item in any result is a member of the frozen frontier" holds by
    construction -- there is no membership check to forget and no second read to get wrong.

    Falsifier: reconstruct a DTO from carried fields, or resolve an id back through
    anything, and these `is` assertions fail while every equality assertion in this file
    still passes. That is the whole point of asserting identity here: a reconstruction is
    equal to what it reconstructs, and a lazy lookup is equal to what it looked up.
    """
    built = frontier(
        candidate("rec-a", body="alpha alpha", recorded_at_us=BASE_US),
        candidate("rec-b", body="alpha", recorded_at_us=BASE_US + 1),
    )
    held = {id(member.record) for member in built.candidates}

    page = rank_governed(built, "alpha", order=order, limit=10)

    assert len(page) == 2
    for item in page:
        assert id(item) in held
    assert any(item is built.candidates[0].record for item in page)
    assert any(item is built.candidates[1].record for item in page)


# ==============================================================================
# Part B: `knowledge.search` and `memory.search` end to end, over authoritative
# 0009 storage, with no compatible FTS projection installed.
# ==============================================================================

#: The M3 suite's own workspace fixture: a Phase 0 baseline migrated through 0009, owned
#: under a live lease, with the M2 evidence chain and eight audit events already seeded.
#: Reused rather than restated, so nothing below can seed a shape 0009's accepted writer
#: would refuse -- every governed row in Part B goes through `fenced_transaction` and M3's
#: own row builders.
owned = m3.owned

STORAGE_WORKSPACE_ID = m3.WORKSPACE_ID
STORAGE_BASE_US = m3.BASE_US
FOREIGN_WORKSPACE_ID = "ws-knowledge-foreign-0001"
INSTALLATION_ID = "inst-knowledge-0001"
CLIENT = ClientIdentity(id="omnivia-core-cli", version="0.1.0")

#: A distinctive value that appears nowhere in this build, so its absence from a refusal can
#: only mean the refusal never carried it. Every malformed payload below puts it in the field
#: the contract validator will quote back.
SENTINEL = "zz-caller-sentinel-9f3c1d"

#: The one query Part B searches with. Two words, present once in every seeded record's
#: content, so relevance differences below come from deliberate repetition rather than from
#: an accident of the fixture text.
QUERY = "omnivia ledger"

#: The production grant after Lane C's additive edit, stated as `service.main.serve` states
#: it. `test_workspace_inspect_refusals.py` is what pins this against `main.py`'s own
#: syntax; here it is the wiring these end-to-end tests run under.
PRODUCTION_OPERATIONS = frozenset(
    {
        WORKSPACE_INSPECT_OPERATION,
        EVIDENCE_SEARCH_OPERATION,
        KNOWLEDGE_SEARCH_OPERATION,
        MEMORY_SEARCH_OPERATION,
    }
)

#: The content member each seeded `record_type` carries, as 0009's frozen schema catalogue
#: names it. Read from the catalogue's own vocabulary rather than invented, so a seeded
#: document is the shape that record type actually declares.
_PRIMARY_MEMBER = {"knowledge.claim": "statement", "knowledge.decision": "decision"}

#: Far enough in the future that a wall-clock resolution instant is before it, which is what
#: makes the "not yet valid" record not yet valid at the instant the handler resolves at.
NOT_YET_US = 4_000_000_000_000_000


def seal_record(
    holder: m2.Owned,
    *,
    record_id: str,
    audit_ref: str,
    text: str,
    record_type: str = "knowledge.claim",
    scope: str = "product.core",
    recorded_at_us: int,
    valid_from_us: int = -1,
    valid_to_us: int | None = None,
    seal_governed: bool = True,
) -> None:
    """One record's whole accepted lineage: a sealed human candidate, then the sealed,
    accepted, canonical governed version promoted from it.

    Both layers carry the *same* content on purpose. That is what a promotion looks like,
    and it is what makes the governance filter falsifiable: the candidate matches every
    query its governed twin matches, so a `current_canonical` page that contained it would
    be a governance failure rather than a query accident.

    `recorded_at_us`, the validity window and the governed seal are parameters because three
    of Part B's properties are invisible at 0009's defaults: microsecond recency, the
    half-open validity interval, and the inertness of an unsealed assembly.
    """
    content = json.dumps({_PRIMARY_MEMBER[record_type]: text})
    candidate_assembly, candidate_version = f"asm-{record_id}-cand", f"ver-{record_id}-cand"
    governed_assembly, governed_version = f"asm-{record_id}", f"ver-{record_id}"
    candidate_event, governed_event = f"event-{candidate_assembly}", f"event-{governed_assembly}"

    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=STORAGE_WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        m3.insert(
            holder.connection,
            m3.RECORDS,
            m3.record_row(record_id, record_type=record_type, scope=scope),
        )

        candidate_row = m3.assembly_row(
            candidate_assembly,
            candidate_version,
            record_id,
            record_type=record_type,
            scope=scope,
            audit_ref=audit_ref,
            correlation_id=audit_ref,
            ordinal=1,
        )
        candidate_row["content_json"] = content
        m3.insert(holder.connection, m3.ASSEMBLIES, candidate_row)
        m3.insert(
            holder.connection,
            m3.EVENTS,
            m3.event_row(
                candidate_event,
                candidate_assembly,
                candidate_version,
                "candidate.human_proposed",
                audit_ref=audit_ref,
                correlation_id=audit_ref,
            ),
        )
        m3.insert(holder.connection, m3.LINKS, m3.link_row(candidate_event, candidate_assembly))
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

        governed_row = m3.assembly_row(
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
        governed_row["content_json"] = content
        governed_row["valid_from_us"] = valid_from_us
        governed_row["valid_to_us"] = valid_to_us
        governed_row["recorded_at_us"] = recorded_at_us
        m3.insert(holder.connection, m3.ASSEMBLIES, governed_row)
        m3.insert(
            holder.connection,
            m3.EVENTS,
            m3.event_row(
                governed_event,
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
        m3.insert(holder.connection, m3.LINKS, m3.link_row(governed_event, governed_assembly))
        if seal_governed:
            m3.insert(
                holder.connection,
                m3.SEALS,
                m3.seal_row(
                    governed_assembly,
                    governed_version,
                    seal_id=f"seal-{governed_assembly}",
                    correlation_id=audit_ref,
                ),
            )


@pytest.fixture
def stocked(owned: m2.Owned) -> Iterator[m2.Owned]:
    """The workspace every Part B property is read out of.

    Seven records, and each one exists to make exactly one thing falsifiable:

    * `rec-alpha` and `rec-zulu` -- identical but for the microsecond they were recorded at
      and their ids, which sort the *other* way. Recency has to separate them, and it can
      only do so from the stored integer: both render to the same millisecond on the wire.
    * `rec-loud` -- says the query twice, so relevance and recency disagree about the page.
    * `rec-decision` -- a different `record_type`.
    * `rec-offscope` -- a different `domain_scope`.
    * `rec-stale` -- a validity window that closed in 2023, and `rec-early` -- one that
      opens in 2096. Both are sealed, accepted and canonical, so only the temporal filter
      can exclude them.
    * `rec-unsealed` -- a governed assembly with no seal. Inert by construction.

    Plus M3's own correction chain on `record-1`: a sealed candidate, a superseded accepted
    version and the current corrected one, which is what gives all three views something
    distinct to hold. Its content says `A durable claim` and never matches `QUERY`, so the
    view tests and the ranking tests cannot contaminate each other.
    """
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=STORAGE_WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        for number in range(9, 17):
            m3.insert(
                owned.connection,
                "omnivia_application_audit_events",
                m3.audit_row(f"audit-{number}"),
            )

    # Two microseconds apart, inside one millisecond, and named so that identity ordering
    # is the reverse of recency ordering.
    seal_record(
        owned, record_id="rec-alpha", audit_ref="audit-9",
        text=f"{QUERY} alpha", recorded_at_us=STORAGE_BASE_US + 2,
    )
    seal_record(
        owned, record_id="rec-zulu", audit_ref="audit-10",
        text=f"{QUERY} zulu", recorded_at_us=STORAGE_BASE_US + 3,
    )
    seal_record(
        owned, record_id="rec-loud", audit_ref="audit-11",
        text=f"{QUERY} {QUERY} loud", recorded_at_us=STORAGE_BASE_US + 1,
    )
    seal_record(
        owned, record_id="rec-decision", audit_ref="audit-12",
        text=f"{QUERY} decided", record_type="knowledge.decision",
        recorded_at_us=STORAGE_BASE_US + 4,
    )
    seal_record(
        owned, record_id="rec-offscope", audit_ref="audit-13",
        text=f"{QUERY} offscope", scope="product.other",
        recorded_at_us=STORAGE_BASE_US + 5,
    )
    seal_record(
        owned, record_id="rec-stale", audit_ref="audit-14",
        text=f"{QUERY} stale", recorded_at_us=STORAGE_BASE_US + 6,
        valid_to_us=STORAGE_BASE_US + 7,
    )
    seal_record(
        owned, record_id="rec-early", audit_ref="audit-15",
        text=f"{QUERY} early", recorded_at_us=STORAGE_BASE_US + 8,
        valid_from_us=NOT_YET_US,
    )
    seal_record(
        owned, record_id="rec-unsealed", audit_ref="audit-16",
        text=f"{QUERY} unsealed", recorded_at_us=STORAGE_BASE_US + 9,
        seal_governed=False,
    )
    m3.seed_corrected_version(owned)
    yield owned


#: Every record whose *current canonical* version matches `QUERY` once the fixture is
#: seeded. The four excluded records are excluded by exactly one filter each, and this set
#: is what every filter test below is measured against.
CANONICAL_MATCHES = frozenset(
    {"rec-alpha", "rec-zulu", "rec-loud", "rec-decision", "rec-offscope"}
)

#: Every record the *frontier* holds for an unfiltered request, which is one more: the
#: correction chain's current version. It passes every pre-freeze filter and is dropped by
#: the query, at ranking -- which is the distinction between a filter and a selection, and
#: the reason the two sets are named separately rather than one being reused for both.
CANONICAL_FRONTIER = CANONICAL_MATCHES | {"record-1"}


def request_for(operation: str, payload: Any, **overrides: Any) -> RequestEnvelope:
    """A request the catalogue validator accepts, built from the frozen entry."""
    entry = get_operation_metadata(operation)
    fields: dict[str, Any] = {
        "request_id": "req-knowledge-1",
        "correlation_id": "corr-knowledge-1",
        "trace_id": "trace-knowledge-1",
        "api_version": CONTRACT_VERSION,
        "client": CLIENT,
        "workspace_id": STORAGE_WORKSPACE_ID,
        "scopes": tuple(entry.scope.required_scopes),
        "purpose": KNOWLEDGE_RETRIEVAL_PURPOSE,
        "required_capabilities": (
            CapabilityRequirement(
                id=entry.required_capability.id,
                minimum_version=entry.required_capability.minimum_version,
                required=True,
            ),
        ),
        "idempotency_key": None,
        "mutation_precondition": None,
        "principal_claim": None,
    }
    fields.update(overrides)
    return RequestEnvelope(
        operation=operation, metadata=RequestMetadata(**fields), input=payload
    )


def production_path(service: object) -> ApplicationDispatcher:
    """The production dispatcher, wired as `service.main.serve` wires it."""
    registry = build_application_registry()
    return ApplicationDispatcher(
        registry=registry,
        session=local_owner_session(
            principal_id=LOCAL_PRINCIPAL,
            installation_id=INSTALLATION_ID,
            workspace_id=STORAGE_WORKSPACE_ID,
            operations=PRODUCTION_OPERATIONS,
        ),
        binding=ServiceBinding(
            installation_id=INSTALLATION_ID, workspace_id=STORAGE_WORKSPACE_ID
        ),
        supported_capabilities=server_capability_snapshot(registry),
        transport=LOCAL_TRANSPORT_ADAPTER,
        probe=Dispatcher.for_service_operations(
            Grant(
                principal=LOCAL_PRINCIPAL,
                workspaces=frozenset({STORAGE_WORKSPACE_ID}),
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


def search(
    holder: m2.Owned, payload: Any, *, operation: str = KNOWLEDGE_SEARCH_OPERATION
) -> KnowledgeSearchResult | MemorySearchResult:
    """One search through the whole production path, as its decoded result."""
    wire = answered(production_path(holder).dispatch(request_for(operation, payload))).result
    if operation == KNOWLEDGE_SEARCH_OPERATION:
        return KnowledgeSearchResult.from_wire(wire)
    return MemorySearchResult.from_wire(wire)


def returned(result: KnowledgeSearchResult | MemorySearchResult) -> tuple[str, ...]:
    """A page as its record ids, in page order."""
    return tuple(item.provenance.identity.record_id for item in result.records)


def versions(result: KnowledgeSearchResult | MemorySearchResult) -> tuple[str, ...]:
    return tuple(item.provenance.identity.version for item in result.records)


class Watcher:
    """What the handler froze, and what SQL ran on either side of the freeze.

    `rank_governed` is replaced *where the handler resolves it* -- the name in the handler
    module -- so the recorded frontier is the exact value the production ranker was called
    with, not a reconstruction. The real ranker still runs, so every page below is the page
    the shipped code produces.
    """

    def __init__(self) -> None:
        self.frontiers: list[GovernedFrontier] = []
        self.statements: list[str] = []
        self.sql_at_freeze: list[int] = []

    @property
    def frontier(self) -> GovernedFrontier:
        assert len(self.frontiers) == 1, self.frontiers
        return self.frontiers[0]

    def members(self) -> frozenset[str]:
        return frozenset(member.record_id for member in self.frontier.candidates)


@pytest.fixture
def watcher(
    stocked: m2.Owned, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Watcher]:
    """The frozen frontier and the connection's statement trace, for one test."""
    seen = Watcher()
    real = knowledge_handlers.rank_governed

    def spy(
        frontier: GovernedFrontier, query: str, *, order: str | None, limit: int
    ) -> tuple[GovernedRecord, ...]:
        seen.frontiers.append(frontier)
        seen.sql_at_freeze.append(len(seen.statements))
        return real(frontier, query, order=order, limit=limit)

    monkeypatch.setattr(knowledge_handlers, "rank_governed", spy)
    stocked.connection.set_trace_callback(seen.statements.append)
    yield seen
    stocked.connection.set_trace_callback(None)


# --- B1: the operations answer, and they answer without a projection ----------


def test_lc_b1_both_operations_answer_authoritatively_with_no_fts_projection(
    stocked: m2.Owned,
) -> None:
    """Amendment 007's fifth item, with the absence of the projection proven, not assumed.

    This workspace is migrated through 0009 and stops there. It has no *active* projection,
    no session-attached one, and no projection machinery at all: the schema carries neither
    an FTS table nor the 0011 activation ledger a projection would have to be registered
    in, and its projection ledger is empty. The two governed searches answer anyway, over
    that same connection, in the same test.

    Falsifier: give either handler a projection dependency -- a readiness gate, a
    `session_search_projection` lookup, a fallback that builds one -- and it refuses or
    raises on a workspace where none of that exists, instead of returning these five
    records.
    """
    assert session_search_projection(stocked.connection) is None
    tables = {
        str(row[0])
        for row in stocked.connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
    }
    assert not {name for name in tables if "fts" in name}, tables
    assert "omnivia_projection_activations" not in tables
    assert (
        stocked.connection.execute(
            "SELECT count(*) FROM omnivia_projection_ledger"
        ).fetchone()[0]
        == 0
    )

    for operation in (KNOWLEDGE_SEARCH_OPERATION, MEMORY_SEARCH_OPERATION):
        result = search(stocked, {"query": QUERY}, operation=operation)
        assert set(returned(result)) == CANONICAL_MATCHES


def test_lc_b1b_the_handler_module_imports_no_projection_at_all(
    stocked: m2.Owned, watcher: Watcher
) -> None:
    """The import boundary, and the statements actually executed, both say the same thing.

    Falsifier: a lazy `from ...projections.fts import ...` inside the handler would evade
    the import list but not the trace -- and a projection read would name the projection's
    own tables in SQL this test can see.
    """
    source = Path(inspect.getsourcefile(knowledge_handlers) or "").read_text()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any("projection" in name for name in imported), sorted(imported)

    search(stocked, {"query": QUERY})

    executed = "\n".join(watcher.statements).lower()
    assert executed  # the read happened at all
    for forbidden in ("fts", "projection", "match", "bm25"):
        assert forbidden not in executed


# --- B2, B3: the three views, and what each of them may hold ------------------


def test_lc_b2_an_absent_view_returns_only_the_current_canonical_version(
    stocked: m2.Owned,
) -> None:
    """The chain's current version, and neither of the two versions behind it.

    Falsifier: resolve an absent view to anything wider and this page gains
    `version-accepted` (superseded) or `version-1` (a candidate), both of which the
    contract's own result validator would then also refuse -- which is the second,
    independent line under this assertion.
    """
    for operation in (KNOWLEDGE_SEARCH_OPERATION, MEMORY_SEARCH_OPERATION):
        result = search(stocked, {"query": "durable"}, operation=operation)
        assert versions(result) == ("version-corrected",)
        assert result.records[0].provenance.identity.currentness == "current"


@pytest.mark.parametrize(
    ("view", "expected"),
    [
        (GOVERNED_RECORD_VIEW_CURRENT_CANONICAL, ("version-corrected",)),
        (GOVERNED_RECORD_VIEW_CANDIDATES, ("version-1",)),
        (GOVERNED_RECORD_VIEW_HISTORY, ("version-accepted",)),
    ],
)
@pytest.mark.parametrize(
    "operation", [KNOWLEDGE_SEARCH_OPERATION, MEMORY_SEARCH_OPERATION]
)
def test_lc_b3_each_named_view_returns_exactly_its_own_versions(
    stocked: m2.Owned, view: str, expected: tuple[str, ...], operation: str
) -> None:
    """Three views over one correction chain, and the three pages are disjoint.

    The chain is one record with three versions, so a view that leaked into another would
    show up here as an extra version rather than as a subtle ordering difference. Both
    operations are asked, because `memory.search` declares the same selector and the whole
    point of the shared contract rule is that the two cannot answer it differently.
    """
    result = search(stocked, {"query": "durable", "view": view}, operation=operation)

    assert versions(result) == expected


def test_lc_b4_the_view_string_alone_cannot_widen_trust(stocked: m2.Owned) -> None:
    """The boundary, stated honestly: this build's local owner *is* granted both views.

    So a request naming `history` succeeds -- and that is not the same claim as "the string
    widened trust". What decides is `LOCAL_OWNER_VIEW_GRANT`, a server-owned constant built
    from the contract's own view names and from nothing about the request; the handler
    passes that constant to the result validator, and the validator refuses an explicitly
    named view that is not in it. So the evidence here is direct rather than end-to-end:

    * the exact same result, revalidated against a grant that does *not* hold the view, is
      refused by the contract with the message that names this rule;
    * the constant is exactly the two views, so the grant is a decision and not a default;
    * the handlers pass that constant, read off their own source -- a mutation that passed
      `frozenset({request.view})` instead would satisfy every other assertion in this file
      while making the request self-authorizing, and is what this check kills.
    """
    payload = {"query": "durable", "view": GOVERNED_RECORD_VIEW_HISTORY}
    result = search(stocked, payload)
    assert versions(result) == ("version-accepted",)

    assert knowledge_handlers.LOCAL_OWNER_VIEW_GRANT == frozenset(
        {GOVERNED_RECORD_VIEW_CANDIDATES, GOVERNED_RECORD_VIEW_HISTORY}
    )

    # The same page, the same instant, a grant that does not hold `history`.
    with pytest.raises(ContractSemanticError, match="never\\s+widens trust"):
        knowledge_handlers.validate_knowledge_search_result(
            result,
            KnowledgeSearchInput.from_wire(payload),
            STORAGE_WORKSPACE_ID,
            "2026-01-01T00:00:00.000Z",
            frozenset(),
        )
    with pytest.raises(ContractSemanticError, match="never\\s+widens trust"):
        validate_memory_search_result(
            MemorySearchResult(records=result.records, page=PageMetadata()),
            MemorySearchInput.from_wire(payload),
            STORAGE_WORKSPACE_ID,
            "2026-01-01T00:00:00.000Z",
            frozenset({GOVERNED_RECORD_VIEW_CANDIDATES}),
        )

    # And the grant the handlers actually hand the validator is that constant.
    module = ast.parse(Path(inspect.getsourcefile(knowledge_handlers) or "").read_text())
    calls = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id
        in {"validate_knowledge_search_result", "validate_memory_search_result"}
    ]
    assert len(calls) == 2
    for call in calls:
        authorized = call.args[4]
        assert isinstance(authorized, ast.Name)
        assert authorized.id == "LOCAL_OWNER_VIEW_GRANT"


# --- B5: every filter runs before the freeze ----------------------------------


def test_lc_b5_no_excluded_record_ever_enters_the_frozen_frontier(
    stocked: m2.Owned, watcher: Watcher
) -> None:
    """Amendment 007's second item, read off the ranker's own input.

    The unfiltered canonical frontier holds five records, and the four the fixture seeds
    beside them are absent from it -- one per filter that must run before the freeze:

    * `rec-unsealed`'s governed assembly carries no seal, so it is not in the authoritative
      view at all (governance, at its strictest);
    * every record's *candidate* version matches the query identically and is absent, which
      is the governance/layer filter proper;
    * `rec-stale`'s window closed in 2023 and `rec-early`'s opens in 2096 (temporal).

    Falsifier: move any of these filters after the freeze -- filter the *page* rather than
    the frontier -- and the excluded record appears in `watcher.members()` while the page
    still looks right. That is exactly the defect a result assertion cannot see, and it is
    the one this reads the frontier to catch.
    """
    result = search(stocked, {"query": QUERY})

    assert watcher.members() == CANONICAL_FRONTIER
    assert set(returned(result)) == CANONICAL_MATCHES
    for excluded in ("rec-unsealed", "rec-stale", "rec-early"):
        assert excluded not in watcher.members()
    # The candidate layer, whose content is identical to its governed twin's.
    assert all(
        member.record.provenance.identity.layer == GOVERNANCE_LAYER_GOVERNED
        for member in watcher.frontier.candidates
    )
    assert watcher.frontier.filters_applied == GOVERNED_FRONTIER_FILTERS
    assert watcher.frontier.workspace_id == STORAGE_WORKSPACE_ID


@pytest.mark.parametrize(
    ("payload", "kept", "dropped"),
    [
        ({"record_type": "knowledge.decision"}, "rec-decision", "rec-alpha"),
        ({"domain_scope": "product.other"}, "rec-offscope", "rec-alpha"),
        ({"domain_scope": "product.core"}, "rec-alpha", "rec-offscope"),
    ],
)
def test_lc_b5b_the_request_selectors_narrow_the_frontier_not_the_page(
    stocked: m2.Owned,
    watcher: Watcher,
    payload: dict[str, str],
    kept: str,
    dropped: str,
) -> None:
    """`record_type` and `domain_scope`, each asserted with its complement.

    Each case names one record the filter keeps and one it drops, and both are asserted
    against the frozen frontier rather than against the page -- so a selector applied to the
    result instead would fail here while returning the same records.
    """
    result = search(stocked, {"query": QUERY, **payload})

    assert kept in watcher.members()
    assert dropped not in watcher.members()
    assert kept in returned(result)
    assert dropped not in returned(result)


def test_lc_b5c_an_unauthorized_workspace_reaches_no_record_at_all(
    stocked: m2.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace filter, asked at the handler with a different authorized workspace.

    The production binding refuses a mismatched workspace before a handler is reached, so
    the interesting question -- what does the handler do with the workspace it *is* given --
    has to be asked directly. `context.workspace_id` is the only workspace input either
    handler has: the payload carries no such field by contract.

    Falsifier: read a workspace from the payload, or leave the resolver's predicate off, and
    the foreign workspace returns this workspace's records.
    """
    seen: list[GovernedFrontier] = []
    real = knowledge_handlers.rank_governed

    def spy(
        frontier: GovernedFrontier, query: str, *, order: str | None, limit: int
    ) -> tuple[GovernedRecord, ...]:
        seen.append(frontier)
        return real(frontier, query, order=order, limit=limit)

    monkeypatch.setattr(knowledge_handlers, "rank_governed", spy)

    foreign = knowledge_handlers.knowledge_search(
        OperationContext(
            request=request_for(KNOWLEDGE_SEARCH_OPERATION, {"query": QUERY}),
            principal=LOCAL_PRINCIPAL,
            workspace_id=FOREIGN_WORKSPACE_ID,
            granted_operations=PRODUCTION_OPERATIONS,
            service=stocked,
        )
    )

    assert isinstance(foreign, knowledge_handlers.AuditedOperationResult)
    assert foreign.result["records"] == []
    assert seen[-1].candidates == ()
    assert seen[-1].workspace_id == FOREIGN_WORKSPACE_ID
    # The complement, so the emptiness above is the workspace and not the fixture.
    assert set(returned(search(stocked, {"query": QUERY}))) == CANONICAL_MATCHES
    assert "workspace_id" not in KnowledgeSearchInput.from_wire({"query": QUERY}).to_wire()


def test_lc_b5d_memory_search_has_no_domain_selector(stocked: m2.Owned) -> None:
    """`MemorySearchInput` declares no `domain_scope`, so `memory.search` filters none.

    Falsifier: give the memory path a domain filter anyway -- defaulted, inherited from
    `knowledge.search`, or read off an undeclared payload key -- and `rec-offscope` drops
    out of a page that must contain it.
    """
    assert "domain_scope" not in {
        field.name for field in MemorySearchInput.__dataclass_fields__.values()
    }
    assert "domain_scope" in {
        field.name for field in KnowledgeSearchInput.__dataclass_fields__.values()
    }

    memory = search(stocked, {"query": QUERY}, operation=MEMORY_SEARCH_OPERATION)
    knowledge = search(stocked, {"query": QUERY, "domain_scope": "product.core"})

    assert "rec-offscope" in returned(memory)
    assert "rec-offscope" not in returned(knowledge)


# --- B6: recency is the stored microsecond, not the wire rendering ------------


def test_lc_b6_exact_stored_microseconds_order_two_records_inside_one_millisecond(
    stocked: m2.Owned, watcher: Watcher
) -> None:
    """The reason `GovernedRecordValue` exists at all.

    `rec-alpha` and `rec-zulu` were recorded one microsecond apart, inside one millisecond,
    and their ids sort the opposite way to their instants. The contract's `Timestamp` is
    millisecond-precision, so both render identically on the wire -- asserted below, because
    it is the premise.

    Falsifier: derive recency by parsing `provenance.temporal.recorded_at` back out of the
    DTO. The two tie, the shared key falls through to `record_id` ascending, and the page
    comes back `alpha, zulu` -- the exact reverse of what the stored integers say.
    """
    result = search(
        stocked,
        {"query": QUERY, "order": KNOWLEDGE_SEARCH_ORDER_RECENCY, "record_type": "knowledge.claim"},
    )
    page = returned(result)

    rendered = {
        item.provenance.identity.record_id: item.provenance.temporal.recorded_at
        for item in result.records
    }
    assert rendered["rec-alpha"] == rendered["rec-zulu"]

    stored = {
        member.record_id: member.recorded_at_us for member in watcher.frontier.candidates
    }
    assert stored["rec-zulu"] == stored["rec-alpha"] + 1
    assert page.index("rec-zulu") < page.index("rec-alpha")


def test_lc_b7_relevance_and_recency_are_wired_and_deterministic(
    stocked: m2.Owned,
) -> None:
    """One fixture, two orders, contradictory pages -- and each page is stable.

    `rec-loud` says the query twice and is the *oldest* claim, so relevance must lead with
    it and recency must not. Falsifier for the wiring: drop `order` on the way to the ranker
    and both pages come back identical, which no assertion about a single page detects.
    """
    claims = {"query": QUERY, "record_type": "knowledge.claim"}
    relevance = returned(
        search(stocked, {**claims, "order": KNOWLEDGE_SEARCH_ORDER_RELEVANCE})
    )
    recency = returned(search(stocked, {**claims, "order": KNOWLEDGE_SEARCH_ORDER_RECENCY}))
    absent = returned(search(stocked, claims))

    assert relevance[0] == "rec-loud"
    assert recency[0] != "rec-loud"
    assert relevance != recency
    assert absent == relevance
    for _ in range(3):
        assert returned(search(stocked, {**claims, "order": KNOWLEDGE_SEARCH_ORDER_RECENCY})) == recency


def test_lc_b8_the_limit_bounds_the_page_and_the_default_is_the_schema_ceiling(
    stocked: m2.Owned, watcher: Watcher
) -> None:
    """A limit is a page size, not a frontier size, and the default is stated once.

    Falsifier: apply the limit to the read and the frontier shrinks with it, which changes
    *which* records the ordering chose from rather than how many it returned.
    """
    result = search(stocked, {"query": QUERY, "limit": 2})

    assert len(result.records) == 2
    assert len(watcher.frontier.candidates) == len(CANONICAL_FRONTIER)
    assert returned(result) == returned(search(stocked, {"query": QUERY}))[:2]
    assert knowledge_handlers.MAX_RESULT_LIMIT == 500


@pytest.mark.parametrize(
    "operation", [KNOWLEDGE_SEARCH_OPERATION, MEMORY_SEARCH_OPERATION]
)
def test_lc_b8b_search_continuations_are_snapshot_stable_and_request_bound(
    stocked: m2.Owned, operation: str
) -> None:
    dispatcher = production_path(stocked)
    payload = {"query": QUERY, "record_type": "knowledge.claim", "limit": 2}
    first_wire = answered(
        dispatcher.dispatch(request_for(operation, payload))
    ).result
    result_type = (
        KnowledgeSearchResult
        if operation == KNOWLEDGE_SEARCH_OPERATION
        else MemorySearchResult
    )
    first = result_type.from_wire(first_wire)
    token = first.page.continuation_token
    assert token is not None

    second_payload = {**payload, "page": {"continuation_token": token}}
    second = result_type.from_wire(
        answered(dispatcher.dispatch(request_for(operation, second_payload))).result
    )
    assert returned(first)
    assert set(returned(first)).isdisjoint(returned(second))
    unpaged_payload = dict(payload)
    unpaged_payload.pop("limit")
    assert returned(first) + returned(second) == returned(
        search(stocked, unpaged_payload, operation=operation)
    )[:4]

    rebound = refused(
        dispatcher.dispatch(
            request_for(
                operation,
                {**second_payload, "query": "different"},
            )
        )
    )
    assert rebound.error.code == ERROR_CODE_INVALID_REQUEST


@pytest.mark.parametrize("adapter", ("in-process", "local-ipc", "http"))
@pytest.mark.parametrize(
    "operation", [KNOWLEDGE_SEARCH_OPERATION, MEMORY_SEARCH_OPERATION]
)
def test_v06_5_c1_governed_search_primary_and_page_2_reach_every_real_adapter(
    stocked: m2.Owned, operation: str, adapter: str
) -> None:
    """Both governed-search pages cross the requested production transport."""
    dispatcher = production_path(stocked)
    result_type = (
        KnowledgeSearchResult
        if operation == KNOWLEDGE_SEARCH_OPERATION
        else MemorySearchResult
    )
    payload = {"query": QUERY, "record_type": "knowledge.claim", "limit": 2}
    first = result_type.from_wire(
        answered(
            s2._transport_call(
                adapter,
                dispatcher,
                request_for(
                    operation,
                    payload,
                    request_id=f"req-governed-c1-{operation}-{adapter}-1",
                ),
                case_id=f"{operation}/primary-success",
            )
        ).result
    )
    token = first.page.continuation_token
    assert token is not None

    second = result_type.from_wire(
        answered(
            s2._transport_call(
                adapter,
                dispatcher,
                request_for(
                    operation,
                    {**payload, "page": {"continuation_token": token}},
                    request_id=f"req-governed-c1-{operation}-{adapter}-2",
                ),
                case_id=f"{operation}/page-2",
            )
        ).result
    )
    assert returned(first)
    assert set(returned(first)).isdisjoint(returned(second))


# --- B9: emptiness, refusals and the absence of a second read -----------------


def test_lc_b9_an_empty_canonical_workspace_is_a_successful_empty_page(
    owned: m2.Owned,
) -> None:
    """No governed rows at all: `records=()`, `PageMetadata()`, and a success envelope.

    Falsifier: answer `not_found`, or an error of any kind, and a workspace that simply
    knows nothing yet becomes indistinguishable from one this build failed to read.
    """
    for operation in (KNOWLEDGE_SEARCH_OPERATION, MEMORY_SEARCH_OPERATION):
        response = answered(
            production_path(owned).dispatch(request_for(operation, {"query": QUERY}))
        )
        result = (
            KnowledgeSearchResult.from_wire(response.result)
            if operation == KNOWLEDGE_SEARCH_OPERATION
            else MemorySearchResult.from_wire(response.result)
        )
        assert result.records == ()
        assert result.page == PageMetadata()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"query": ""},
        {"query": "ledger", "view": SENTINEL},
        {"query": "ledger", "order": SENTINEL},
        {"query": SENTINEL, "limit": 0},
        {"query": 17},
    ],
)
@pytest.mark.parametrize(
    "operation", [KNOWLEDGE_SEARCH_OPERATION, MEMORY_SEARCH_OPERATION]
)
def test_lc_b10_malformed_input_refuses_with_a_frozen_message_and_no_echo(
    stocked: m2.Owned, payload: dict[str, Any], operation: str
) -> None:
    """Fail closed, and say nothing back.

    Every payload here is refused by the contract's own decode or semantic validation, and
    both of those quote what they rejected. The refusal a caller receives is the handler's
    frozen constant instead, and the whole encoded response is searched for the sentinel --
    asserting on the message alone would miss a value that reached another field.

    Falsifier: pass the contract's message through, or resolve an unrecognized `view`/`order`
    into its default. The first puts caller text on the wire; the second answers a request
    the caller did not make while reporting success.
    """
    response = refused(production_path(stocked).dispatch(request_for(operation, payload)))
    expected = (
        knowledge_handlers._MESSAGE_INVALID_KNOWLEDGE_INPUT
        if operation == KNOWLEDGE_SEARCH_OPERATION
        else knowledge_handlers._MESSAGE_INVALID_MEMORY_INPUT
    )

    assert response.error.code == ERROR_CODE_INVALID_REQUEST
    assert response.error.message == expected
    assert SENTINEL not in json.dumps(encode_response(response), sort_keys=True)


@pytest.mark.parametrize(
    ("handler", "operation"),
    [
        (knowledge_handlers.knowledge_search, KNOWLEDGE_SEARCH_OPERATION),
        (knowledge_handlers.memory_search, MEMORY_SEARCH_OPERATION),
    ],
)
def test_lc_b10b_the_decode_refusal_chains_no_exception_at_all(
    stocked: m2.Owned, handler: Any, operation: str
) -> None:
    """`__context__ is None`, which is the property `from None` does *not* give.

    The payload is refused by the *view* validator, which quotes the value it rejected, so
    there really is a chainable exception carrying caller text at the moment of the refusal.

    Falsifier: raise inside the `except` block. A rendered traceback stays quiet and this
    assertion fails, because the contract error -- which quotes the payload -- is still one
    attribute access away from anything that logs or serializes the refusal.
    """
    with pytest.raises(OperationError) as refusal:
        handler(
            OperationContext(
                request=request_for(operation, {"query": SENTINEL, "view": SENTINEL}),
                principal=LOCAL_PRINCIPAL,
                workspace_id=STORAGE_WORKSPACE_ID,
                granted_operations=PRODUCTION_OPERATIONS,
                service=stocked,
            )
        )

    assert refusal.value.__context__ is None
    assert refusal.value.__cause__ is None
    assert SENTINEL not in refusal.value.message


@pytest.mark.parametrize(
    "operation", [KNOWLEDGE_SEARCH_OPERATION, MEMORY_SEARCH_OPERATION]
)
def test_lc_b11_absent_authoritative_storage_refuses_with_a_frozen_message(
    operation: str,
) -> None:
    """A service instance serving no workspace cannot answer a governed read.

    Falsifier: open a database from the handler. Storage authority stays with the service,
    and a handler that can open one is a second writer waiting to happen.
    """
    response = refused(
        production_path(None).dispatch(request_for(operation, {"query": QUERY}))
    )

    assert response.error.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE
    assert response.error.message == knowledge_handlers._MESSAGE_NO_STORAGE


def test_lc_b12_no_sql_runs_after_the_frontier_is_frozen(
    stocked: m2.Owned, watcher: Watcher
) -> None:
    """Amendment 007's fourth item, measured rather than argued.

    Every statement the connection executes is traced. The count is taken at the instant the
    ranker is handed the frozen frontier and again after the response has been built, and
    the two are equal -- so ranking, result construction, semantic validation and wire
    encoding together issued no query.

    Falsifier: any post-freeze read -- an id re-resolution, a lazy DTO fetch, a "just
    re-check governance" query, a second freshness probe -- raises the second count. The
    first assertion is what stops this passing vacuously: the read before the freeze really
    did execute statements.
    """
    result = search(stocked, {"query": QUERY})

    assert result.records
    assert watcher.sql_at_freeze[0] > 0
    assert len(watcher.statements) == watcher.sql_at_freeze[0]


#: The final shipped read surface of this build: the four operations Lane C's local
#: dispatcher fixtures grant above, plus the two V06-3 lanes composed onto them. Named
#: separately from `PRODUCTION_OPERATIONS` on purpose -- that constant is the narrow grant
#: Part B's end-to-end requests run under, and widening it would change what those pages
#: prove. This set is only ever the build's own shipped surface.
SHIPPED_OPERATIONS = frozenset(
    {
        WORKSPACE_INSPECT_OPERATION,
        EVIDENCE_SEARCH_OPERATION,
        KNOWLEDGE_SEARCH_OPERATION,
        MEMORY_SEARCH_OPERATION,
        GRAPH_TRAVERSE_OPERATION,
        CONTEXT_PACK_BUILD_OPERATION,
    }
)


def test_lc_b13_the_shipped_operations_are_exactly_the_six_read_operations() -> None:
    """The registry, the purposes and the capability snapshot, all at six operations.

    `test_workspace_inspect_refusals.py` holds the production *grant* evidence; this is the
    build's own side of it, stated here because Part B's end-to-end pages are only
    meaningful if these two operations are the ones this build actually ships -- and
    because `graph.traverse` and `context_pack.build` join them on the same seam, under the
    same purpose, with no side effect.

    Falsifier: register a handler without granting its purpose and every request for it is
    refused by check 11; grant a purpose with no handler and the seam authorizes something
    this build cannot serve.
    """
    registry = build_application_registry()

    assert registry.operations == SHIPPED_OPERATIONS
    assert OPERATION_PURPOSES[KNOWLEDGE_SEARCH_OPERATION] == KNOWLEDGE_RETRIEVAL_PURPOSE
    assert OPERATION_PURPOSES[MEMORY_SEARCH_OPERATION] == KNOWLEDGE_RETRIEVAL_PURPOSE
    assert OPERATION_PURPOSES[GRAPH_TRAVERSE_OPERATION] == KNOWLEDGE_RETRIEVAL_PURPOSE
    assert OPERATION_PURPOSES[CONTEXT_PACK_BUILD_OPERATION] == KNOWLEDGE_RETRIEVAL_PURPOSE
    assert set(OPERATION_PURPOSES) == SHIPPED_OPERATIONS
    for name in SHIPPED_OPERATIONS:
        entry = get_operation_metadata(name)
        assert entry.scope.side_effect == "none"
    assert server_capability_snapshot(registry) == tuple(
        CapabilityRef(id=capability, version="1.0")
        for capability in (
            "context_pack.build",
            "evidence.read",
            "graph.read",
            "knowledge.read",
            "memory.read",
            "workspace.read",
        )
    )
