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

This file is unit-level, over values rather than over a database, because every property
here is a property of a pure function over a frozen value. That is not an accident of test
convenience: it is the seam. `governed.py` holds the connection and resolves the versions;
this ranker is handed a value and can reach nothing else, and the isolation tests below are
what make "can reach nothing else" checkable rather than asserted.
"""

from __future__ import annotations

import ast
import inspect
import itertools
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_runtime.storage import retrieval as retrieval_module
from omnivia_core_runtime.storage.retrieval import (
    GOVERNED_FRONTIER_FILTERS,
    GovernedCandidate,
    GovernedFrontier,
    governed_order_key,
    governed_search_text,
    rank_governed,
)

from omnivia_core.contracts.v1 import (
    GOVERNANCE_LAYER_GOVERNED,
    GOVERNANCE_STATE_ACCEPTED,
    KNOWLEDGE_SEARCH_CANONICAL_AUTHORITY_LEVEL,
    KNOWLEDGE_SEARCH_ORDER_RECENCY,
    KNOWLEDGE_SEARCH_ORDER_RELEVANCE,
    GovernedRecord,
    RecordIdentity,
    RecordProvenance,
    RecordTemporalMetadata,
)

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
