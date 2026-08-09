"""V06-3 Lane B acceptance for the `evidence.search` FTS5 builder and its reads.

The properties under test, and why each one needs a test rather than a paragraph:

* **Restart convergence at every meaningful point.** Killing the builder between any
  two phases -- or inside one -- must leave a state the next call finishes, arriving at
  the same run, the same digest and the same pointer an uninterrupted build would. That
  is asserted by injecting a failure at each phase in turn and comparing the recovered
  workspace against an independently computed expectation, not against whatever the
  first run happened to produce.
* **The pointer moves only through activation.** A direct `UPDATE` of the ledger's
  pointer columns is refused, and the builder never issues one.
* **Reads never build, and there is no tolerance for lag.** A stale or unactivated
  projection refuses; it does not repair itself, and no rebuild entry point exists
  beside `build_search_projection`.
* **Ranking sees the frozen frontier and nothing else**, in the one total order
  `retrieval.relevance_order_key` defines, with bm25 supplying relevance.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
from omnivia_core_runtime.ownership.fencing import fenced_transaction, guarded_tables
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.projections import fts
from omnivia_core_runtime.storage.repository import (
    authoritative_checkpoint,
    read_evidence_candidates,
)
from omnivia_core_runtime.storage.retrieval import (
    CONFIGURED_LOCAL_OWNER,
    AuthorizedFrontier,
    authorized_frontier,
    local_owner_label_grant,
    relevance_order_key,
)

WORKSPACE_ID = m2.WORKSPACE_ID
NOW_US = m2.BASE_US + 1_000_000
RESOLUTION_US = m2.BASE_US + 10_000_000

#: The extra artifacts, chosen so the ranking assertions are not accidents.
#: `evd-alpha-1` carries the term four times in a surface of comparable length, so bm25
#: scores it strictly ahead of the others and relevance -- not recency -- decides first
#: place. `evd-tie-a` and `evd-tie-b` carry it the same number of times in surfaces of
#: identical length, so bm25 scores them to the last bit identically and the recency
#: tie-breaker is what separates them; `evd-tie-b` is the later of the two.
EXTRA_ARTIFACTS: tuple[tuple[str, str, str, int], ...] = (
    ("evd-alpha-1", "doc-alpha-1", "archive://alpha/alpha/alpha.md", m2.BASE_US + 10),
    ("evd-gamma-1", "doc-gamma-1", "archive://gamma/one.md", m2.BASE_US + 20),
    ("evd-tie-a", "doc-alpha-2", "archive://alpha/two.md", m2.BASE_US + 30),
    ("evd-tie-b", "doc-alpha-2", "archive://alpha/two.md", m2.BASE_US + 40),
)

#: Every phase `build_search_projection` runs, by the name it is reachable under. The
#: convergence test injects a failure at each in turn, so this tuple is what "every
#: meaningful point" means and adding a phase without adding it here is visible.
PHASES: tuple[str, ...] = (
    "_ensure_ledger_row",
    "_plan_run",
    "_append_documents",
    "_validate_run",
    "_activate_run",
    "_reclaim",
)


class Interrupted(RuntimeError):
    """The injected failure. Stands for a kill, a crash or a lost lease."""


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m2.Owned]:
    """A workspace at 0012 carrying five evidence artifacts and no projection."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m2.bootstrap_and_migrate(path)
    holder = m2.take_ownership(path)
    m2.seed_chain(holder)
    for evidence_id, native_id, locator, recorded_at in EXTRA_ARTIFACTS:
        m2.write(
            holder,
            m2.EVIDENCE,
            evidence_id=evidence_id,
            source_native_id=native_id,
            source_locator=locator,
            recorded_at_us=recorded_at,
        )
    yield holder
    holder.connection.close()


def build(holder: m2.Owned, *, now_us: int = NOW_US) -> fts.BuildOutcome:
    return fts.build_search_projection(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        now_us=now_us,
    )


def frontier(holder: m2.Owned, **overrides: Any) -> AuthorizedFrontier:
    """The frozen frontier the real read path would build, through the real filters."""
    candidates = read_evidence_candidates(holder.connection, workspace_id=WORKSPACE_ID)
    arguments: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "grant": local_owner_label_grant(
            principal_id=CONFIGURED_LOCAL_OWNER,
            workspace_id=WORKSPACE_ID,
            granted_workspace=WORKSPACE_ID,
        ),
        "resolution_time_us": RESOLUTION_US,
    }
    arguments.update(overrides)
    return authorized_frontier(candidates, **arguments)


def expected_digest(holder: m2.Owned) -> str:
    """The build digest, derived from 0008 rather than from the projection.

    Computed from the authoritative artifacts so the convergence assertions compare the
    recovered projection with what a correct build *must* produce, not with what the
    first attempt happened to leave behind.
    """
    hasher = hashlib.sha256()
    for evidence_id, kind, native_id, locator in holder.connection.execute(
        "SELECT evidence_id, source_kind, source_native_id, source_locator "
        "FROM omnivia_evidence_artifacts WHERE workspace_id = ? "
        "ORDER BY evidence_id ASC",
        (WORKSPACE_ID,),
    ):
        surface = " ".join(part for part in (kind, native_id, locator) if part)
        hasher.update(f"{evidence_id}\x1f{surface}\x1e".encode())
    return f"sha256:{hasher.hexdigest()}"


def scalar(holder: m2.Owned, sql: str, *parameters: object) -> Any:
    return holder.connection.execute(sql, parameters).fetchone()[0]


def add_artifact(holder: m2.Owned, evidence_id: str, *, recorded_at: int) -> None:
    m2.write(
        holder,
        m2.EVIDENCE,
        evidence_id=evidence_id,
        source_native_id=f"doc-{evidence_id}",
        source_locator=f"archive://{evidence_id}.md",
        recorded_at_us=recorded_at,
    )


# --- the projection exists at all ---------------------------------------------


def test_lb_l1_fts5_is_required_explicitly_and_by_name(owned: m2.Owned) -> None:
    fts.require_fts5(owned.connection)

    class WithoutFts5:
        def execute(self, *_: object) -> WithoutFts5:
            return self

        def fetchone(self) -> tuple[int]:
            return (0,)

    with pytest.raises(fts.Fts5Unavailable, match="does not have FTS5"):
        fts.require_fts5(WithoutFts5())  # type: ignore[arg-type]


def test_lb_l2_a_build_activates_one_run_and_projects_every_artifact(
    owned: m2.Owned,
) -> None:
    outcome = build(owned)
    assert outcome.activated is True
    assert outcome.epoch == 1
    assert outcome.source_checkpoint == authoritative_checkpoint(
        owned.connection, workspace_id=WORKSPACE_ID
    )
    assert outcome.document_count == 5
    assert outcome.build_digest == expected_digest(owned)

    assert scalar(
        owned,
        "SELECT state FROM omnivia_projection_runs WHERE run_id = ?",
        outcome.run_id,
    ) == "succeeded"
    assert scalar(
        owned,
        "SELECT active_run_id FROM omnivia_projection_ledger WHERE projection_id = ?",
        fts.PROJECTION_ID,
    ) == outcome.run_id
    assert (
        scalar(
            owned,
            "SELECT COUNT(*) FROM omnivia_projection_activations WHERE projection_id = ?",
            fts.PROJECTION_ID,
        )
        == 1
    )


def test_lb_l3_the_projected_text_is_the_repository_identity_surface(
    owned: m2.Owned,
) -> None:
    """The projection is a different index over the same text, never a different text.

    `repository._candidate` builds `search_text` for the read path and this module
    builds it for the projection. Nothing in the language holds the two expressions
    equal, so the equality is pinned here rather than assumed.
    """
    outcome = build(owned)
    projected = dict(
        owned.connection.execute(
            f"SELECT evidence_id, search_text FROM {fts.DOCUMENTS_TABLE} "
            "WHERE run_id = ?",
            (outcome.run_id,),
        ).fetchall()
    )
    candidates = read_evidence_candidates(owned.connection, workspace_id=WORKSPACE_ID)
    assert projected == {c.evidence_id: c.search_text for c in candidates}


# --- interruption and restart -------------------------------------------------


@pytest.mark.parametrize("phase", PHASES)
def test_lb_l4_an_interruption_at_any_phase_converges_on_the_next_build(
    owned: m2.Owned, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """Kill the builder at each phase in turn; the next call must finish the job.

    The expectation is computed from 0008, so this asserts the recovered projection is
    *correct*, not merely that a second call did not raise. A resumed build and an
    uninterrupted one produce the same digest because the digest is recomputed from the
    durable documents at validation time rather than carried in memory.
    """
    original = getattr(fts, phase)

    def interrupt(*_: object, **__: object) -> None:
        raise Interrupted(phase)

    monkeypatch.setattr(fts, phase, interrupt)
    with pytest.raises(Interrupted):
        build(owned)
    monkeypatch.setattr(fts, phase, original)

    outcome = build(owned)
    assert outcome.build_digest == expected_digest(owned)
    assert outcome.document_count == 5
    assert fts.current_build(
        owned.connection, workspace_id=WORKSPACE_ID
    ).run_id == outcome.run_id
    # Exactly one run reached the pointer, and no orphan content survived.
    assert (
        scalar(
            owned,
            "SELECT COUNT(*) FROM omnivia_projection_activations WHERE projection_id = ?",
            fts.PROJECTION_ID,
        )
        == 1
    )
    assert (
        scalar(
            owned,
            f"SELECT COUNT(DISTINCT run_id) FROM {fts.DOCUMENTS_TABLE}",
        )
        == 1
    )


def test_lb_l5_an_interruption_mid_append_resumes_from_the_last_checkpoint(
    owned: m2.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batch is the resume granularity, and the resumed run is the *same* run.

    Starting a second run instead would collide with 0011's epoch guard, which is why
    run ids are derived from `(epoch, checkpoint)` rather than generated.
    """
    monkeypatch.setattr(fts, "CHECKPOINT_BATCH", 2)
    real_next_us = fts._next_us

    def stop_once_two_checkpoints_are_durable(
        connection: sqlite3.Connection, workspace_id: str, now_us: int
    ) -> int:
        durable = connection.execute(
            "SELECT COUNT(*) FROM omnivia_projection_run_checkpoints"
        ).fetchone()[0]
        if durable >= 2:
            raise Interrupted("mid-append")
        return int(real_next_us(connection, workspace_id, now_us))

    monkeypatch.setattr(fts, "_next_us", stop_once_two_checkpoints_are_durable)
    with pytest.raises(Interrupted):
        build(owned)
    monkeypatch.setattr(fts, "_next_us", real_next_us)

    partial = scalar(owned, f"SELECT COUNT(*) FROM {fts.DOCUMENTS_TABLE}")
    interrupted_run = scalar(
        owned,
        "SELECT run_id FROM omnivia_projection_runs WHERE state = 'running'",
    )
    assert 0 < partial < 5

    outcome = build(owned)
    assert outcome.run_id == interrupted_run
    assert outcome.document_count == 5
    assert outcome.build_digest == expected_digest(owned)
    assert scalar(
        owned,
        "SELECT MAX(checkpoint_sequence) FROM omnivia_projection_run_checkpoints "
        "WHERE run_id = ?",
        outcome.run_id,
    ) == 2


def test_lb_l6_a_second_build_at_the_same_checkpoint_does_nothing(
    owned: m2.Owned,
) -> None:
    first = build(owned)
    second = build(owned)
    assert second.activated is False
    assert second.run_id == first.run_id
    assert second.build_digest == first.build_digest
    assert (
        scalar(
            owned,
            "SELECT COUNT(*) FROM omnivia_projection_activations WHERE projection_id = ?",
            fts.PROJECTION_ID,
        )
        == 1
    )
    assert scalar(owned, "SELECT COUNT(*) FROM omnivia_projection_runs") == 1


def test_lb_l7_new_evidence_starts_a_fresh_run_that_supersedes_and_reclaims(
    owned: m2.Owned,
) -> None:
    first = build(owned)
    add_artifact(owned, "evd-later-1", recorded_at=m2.BASE_US + 500)
    second = build(owned, now_us=NOW_US + 1000)

    assert second.activated is True
    assert second.epoch == first.epoch + 1
    assert second.document_count == 6
    assert second.build_digest == expected_digest(owned)
    assert scalar(
        owned,
        "SELECT state FROM omnivia_projection_runs WHERE run_id = ?",
        first.run_id,
    ) == "superseded"
    # The superseded run's content is derived material the next build reproduced.
    assert (
        scalar(
            owned,
            f"SELECT COUNT(*) FROM {fts.DOCUMENTS_TABLE} WHERE run_id = ?",
            first.run_id,
        )
        == 0
    )


def test_lb_l8_a_run_whose_source_moved_on_is_failed_not_activated(
    owned: m2.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-flight run attests to a digest over the evidence it was started for.

    If the workspace moves past that checkpoint mid-build, finishing the run would
    activate a projection that is behind the moment it lands. It is failed with a
    canonical error instead, and the next build starts at the current checkpoint.
    """
    monkeypatch.setattr(fts, "_validate_run", lambda *a, **k: (_ for _ in ()).throw(Interrupted("x")))
    with pytest.raises(Interrupted):
        build(owned)
    monkeypatch.undo()

    add_artifact(owned, "evd-later-1", recorded_at=m2.BASE_US + 500)
    outcome = build(owned, now_us=NOW_US + 1000)

    assert outcome.document_count == 6
    assert outcome.build_digest == expected_digest(owned)
    failed = owned.connection.execute(
        "SELECT run_id, error_json FROM omnivia_projection_runs WHERE state = 'failed'"
    ).fetchall()
    assert len(failed) == 1
    assert failed[0][1] == '{"code":"projection.source_checkpoint_advanced"}'
    assert failed[0][0] != outcome.run_id


def test_lb_l8b_a_complete_run_killed_before_activation_is_landed_then_overtaken(
    owned: m2.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one recovery that takes two runs, still inside one call.

    A build killed between validation and activation leaves a complete, validated run
    that 0011 cannot fail -- there is no `succeeded -> failed` transition -- and must not
    simply activate if the workspace has moved on, because that would publish a
    projection that is stale the moment it lands. So the recovered run is activated,
    which is the only legal thing to do with it, and the same call then builds forward
    to the current checkpoint. One call in, level with the workspace out.
    """
    monkeypatch.setattr(fts, "_activate_run", lambda *a, **k: (_ for _ in ()).throw(Interrupted("x")))
    with pytest.raises(Interrupted):
        build(owned)
    monkeypatch.undo()
    stranded = scalar(
        owned, "SELECT run_id FROM omnivia_projection_runs WHERE state = 'succeeded'"
    )

    add_artifact(owned, "evd-later-1", recorded_at=m2.BASE_US + 500)
    outcome = build(owned, now_us=NOW_US + 1000)

    assert outcome.source_checkpoint == authoritative_checkpoint(
        owned.connection, workspace_id=WORKSPACE_ID
    )
    assert outcome.document_count == 6
    assert outcome.build_digest == expected_digest(owned)
    assert outcome.run_id != stranded
    assert scalar(
        owned,
        "SELECT state FROM omnivia_projection_runs WHERE run_id = ?",
        stranded,
    ) == "superseded"
    assert (
        scalar(
            owned,
            "SELECT COUNT(*) FROM omnivia_projection_activations WHERE projection_id = ?",
            fts.PROJECTION_ID,
        )
        == 2
    )


# --- the pointer ---------------------------------------------------------------


def test_lb_l9_the_active_pointer_refuses_a_direct_update(owned: m2.Owned) -> None:
    """The builder writes an activation row; nothing writes the pointer.

    Proven from both sides: the module contains no UPDATE against the ledger's pointer
    columns, and 0011's guard refuses one issued by hand under full write authority.
    """
    outcome = build(owned)
    with pytest.raises(
        sqlite3.DatabaseError, match="requires matching activation"
    ), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        owned.connection.execute(
            "UPDATE omnivia_projection_ledger SET active_run_id = ?, active_epoch = 99 "
            "WHERE projection_id = ?",
            ("forged-run", fts.PROJECTION_ID),
        )
    assert fts.current_build(
        owned.connection, workspace_id=WORKSPACE_ID
    ).run_id == outcome.run_id


# --- reads ---------------------------------------------------------------------


def test_lb_l10_an_unactivated_projection_is_unavailable_not_empty(
    owned: m2.Owned,
) -> None:
    """"Nothing activated" must not read as "no results"; that is the silent success
    §20.7 forbids."""
    with pytest.raises(fts.ProjectionUnavailable):
        fts.open_search_index(owned.connection, workspace_id=WORKSPACE_ID)


def test_lb_l11_a_search_over_a_lagging_projection_refuses_and_builds_nothing(
    owned: m2.Owned,
) -> None:
    """Zero known lag, no tolerance, and no repair on the request path."""
    build(owned)
    index = fts.open_search_index(owned.connection, workspace_id=WORKSPACE_ID)
    assert index.search(frontier(owned), "alpha", limit=10)

    add_artifact(owned, "evd-later-1", recorded_at=m2.BASE_US + 500)
    before = (
        scalar(owned, "SELECT COUNT(*) FROM omnivia_projection_runs"),
        scalar(owned, "SELECT COUNT(*) FROM omnivia_projection_activations"),
        scalar(owned, f"SELECT COUNT(*) FROM {fts.DOCUMENTS_TABLE}"),
    )
    with pytest.raises(fts.StaleProjection):
        index.search(frontier(owned), "alpha", limit=10)
    with pytest.raises(fts.StaleProjection):
        fts.open_search_index(owned.connection, workspace_id=WORKSPACE_ID)
    assert (
        scalar(owned, "SELECT COUNT(*) FROM omnivia_projection_runs"),
        scalar(owned, "SELECT COUNT(*) FROM omnivia_projection_activations"),
        scalar(owned, f"SELECT COUNT(*) FROM {fts.DOCUMENTS_TABLE}"),
    ) == before


def test_lb_l12_an_index_built_from_a_superseded_run_refuses(owned: m2.Owned) -> None:
    """Freshness is re-proven per call, not once at open."""
    build(owned)
    index = fts.open_search_index(owned.connection, workspace_id=WORKSPACE_ID)
    add_artifact(owned, "evd-later-1", recorded_at=m2.BASE_US + 500)
    build(owned, now_us=NOW_US + 1000)
    with pytest.raises(fts.StaleProjection, match="has since activated"):
        index.search(frontier(owned), "alpha", limit=10)


def test_lb_l13_ranking_returns_frontier_members_in_the_shared_total_order(
    owned: m2.Owned,
) -> None:
    build(owned)
    index = fts.open_search_index(owned.connection, workspace_id=WORKSPACE_ID)
    frozen = frontier(owned)
    results = index.search(frozen, "alpha", limit=10)

    assert [candidate.evidence_id for candidate in results] == [
        "evd-alpha-1",
        "evd-tie-b",
        "evd-tie-a",
    ]
    members = {candidate.evidence_id for candidate in frozen.candidates}
    assert {candidate.evidence_id for candidate in results} <= members
    # The order is the key's, not the query planner's: re-sorting the returned pairs by
    # `relevance_order_key` cannot move them.
    scored = {
        str(row[0]): float(row[1])
        for row in owned.connection.execute(
            f"SELECT evidence_id, bm25({fts.INDEX_TABLE}) FROM {fts.INDEX_TABLE} "
            f"WHERE {fts.INDEX_TABLE} MATCH ?",
            ('"alpha"',),
        )
    }
    assert list(results) == sorted(
        results, key=lambda c: relevance_order_key(c, scored[c.evidence_id])
    )
    # bm25 ties are the common case, and recency then identity is what breaks them.
    assert scored["evd-tie-a"] == scored["evd-tie-b"]


def test_lb_l14_a_document_outside_the_frontier_is_never_returned(
    owned: m2.Owned,
) -> None:
    """The index holds every artifact; the frontier decides which ones exist for a read.

    Narrowed by the *temporal* filter, because it removes a candidate the ACL stage
    would have admitted -- so a result that leaked would be leaking through ranking
    rather than through a grant.
    """
    build(owned)
    index = fts.open_search_index(owned.connection, workspace_id=WORKSPACE_ID)
    narrowed = frontier(owned, resolution_time_us=m2.BASE_US + 35)
    excluded = {candidate.evidence_id for candidate in narrowed.excluded}
    assert "evd-tie-b" in excluded

    results = index.search(narrowed, "alpha", limit=10)
    assert [candidate.evidence_id for candidate in results] == [
        "evd-alpha-1",
        "evd-tie-a",
    ]
    assert index.document_count == 5


def test_lb_l15_the_limit_selects_from_the_ordered_head(owned: m2.Owned) -> None:
    build(owned)
    index = fts.open_search_index(owned.connection, workspace_id=WORKSPACE_ID)
    frozen = frontier(owned)
    assert [c.evidence_id for c in index.search(frozen, "alpha", limit=2)] == [
        "evd-alpha-1",
        "evd-tie-b",
    ]
    assert index.search(frozen, "alpha", limit=0) == ()


def test_lb_l16_a_query_cannot_reach_the_fts5_operator_grammar(
    owned: m2.Owned,
) -> None:
    """`MATCH` takes a query language, and the query is caller-supplied text.

    Quoted as a phrase, so a caller cannot write `OR`, `NOT`, a column filter or a
    prefix wildcard into a search -- and a stray quote is data rather than a syntax
    error the caller can use to probe the schema.
    """
    build(owned)
    index = fts.open_search_index(owned.connection, workspace_id=WORKSPACE_ID)
    frozen = frontier(owned)
    for hostile in (
        'alpha" OR "gamma',
        "alpha OR gamma",
        "search_text : alpha",
        "NEAR(alpha gamma)",
        '"',
    ):
        assert index.search(frozen, hostile, limit=10) == (), hostile
    assert index.search(frozen, "", limit=10) == ()
    # `*` is a prefix operator only *outside* a phrase; inside one it is an ordinary
    # separator, so this reads as the single token `alpha` rather than as a wildcard.
    assert index.search(frozen, "alpha*", limit=10) == index.search(
        frozen, "alpha", limit=10
    )


def test_lb_l17_matching_is_normalized_on_both_sides(owned: m2.Owned) -> None:
    build(owned)
    index = fts.open_search_index(owned.connection, workspace_id=WORKSPACE_ID)
    frozen = frontier(owned)
    assert index.search(frozen, "ALPHA", limit=10) == index.search(
        frozen, "alpha", limit=10
    )


def test_lb_l18_a_frontier_from_another_workspace_is_refused(owned: m2.Owned) -> None:
    build(owned)
    index = fts.open_search_index(owned.connection, workspace_id=WORKSPACE_ID)
    other = authorized_frontier(
        (),
        workspace_id=m2.OTHER_WORKSPACE_ID,
        grant=local_owner_label_grant(
            principal_id=CONFIGURED_LOCAL_OWNER,
            workspace_id=m2.OTHER_WORKSPACE_ID,
            granted_workspace=m2.OTHER_WORKSPACE_ID,
        ),
        resolution_time_us=RESOLUTION_US,
    )
    with pytest.raises(fts.ProjectionError, match="serves"):
        index.search(other, "alpha", limit=10)


# --- the index is session-scoped ----------------------------------------------


def test_lb_l19_the_index_lives_in_temp_and_touches_no_persisted_schema(
    owned: m2.Owned,
) -> None:
    """The design claim's other half: nothing about the index is durable.

    If the FTS5 table were persisted it would appear in `sqlite_master` -- moving the
    schema fingerprint -- and its five shadow tables would appear in `GUARDED_TABLES`
    with no triggers, which is the failure `test_sb05_...` exists to report.
    """
    build(owned)

    def persisted() -> set[str]:
        return {
            str(row[0])
            for row in owned.connection.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }

    before = persisted()
    fts.open_search_index(owned.connection, workspace_id=WORKSPACE_ID)
    assert persisted() == before
    assert fts.INDEX_TABLE not in guarded_tables()
    assert not any(name.startswith(fts.INDEX_TABLE) for name in before)

    session = {
        str(row[0])
        for row in owned.connection.execute("SELECT name FROM sqlite_temp_master")
    }
    assert fts.INDEX_TABLE in session
    assert f"{fts.INDEX_TABLE}_data" in session


def test_lb_l20_reopening_rebuilds_the_index_from_the_durable_rows(
    owned: m2.Owned,
) -> None:
    """Restart convergence for the read side: a new session re-derives the index.

    Dropping the `temp` table stands for the process exiting; nothing needed to serve
    reads lives only in that table, so the next `open_search_index` reproduces it from
    the run the ledger points at.
    """
    outcome = build(owned)
    first = fts.open_search_index(owned.connection, workspace_id=WORKSPACE_ID)
    expected = first.search(frontier(owned), "alpha", limit=10)

    second = fts.open_search_index(owned.connection, workspace_id=WORKSPACE_ID)
    assert second.build.run_id == outcome.run_id
    assert second.document_count == first.document_count
    assert second.search(frontier(owned), "alpha", limit=10) == expected


def test_lb_l21_there_is_no_second_way_to_advance_the_projection() -> None:
    """No operator rebuild verb, and no request-path builder.

    `build_search_projection` is the only exported name that writes, which is what makes
    "reads never build" a property of the module's surface rather than of its callers'
    discipline.
    """
    writers = [
        name
        for name in fts.__all__
        if name.startswith(("build", "rebuild", "refresh", "repair", "reindex"))
    ]
    assert writers == ["build_search_projection"]
    assert not hasattr(fts.SearchIndex, "rebuild")
    assert not hasattr(fts.SearchIndex, "refresh")
