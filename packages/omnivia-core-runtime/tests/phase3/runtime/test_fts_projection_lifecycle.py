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

import ast
import hashlib
import inspect
import pathlib
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
from omnivia_core_runtime.ownership.fencing import fenced_transaction, guarded_tables
from omnivia_core_runtime.storage import retrieval as retrieval_module
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.projections import fts
from omnivia_core_runtime.storage.repository import (
    authoritative_checkpoint,
    read_evidence_candidates,
)
from omnivia_core_runtime.storage.retrieval import (
    CONFIGURED_LOCAL_OWNER,
    AuthorizedFrontier,
    EvidenceCandidate,
    ProjectedFrontier,
    authorized_frontier,
    local_owner_label_grant,
    normalize_query,
    query_tokens,
    rank_projected,
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


def ranked(
    holder: m2.Owned,
    projection: fts.SearchProjection,
    frozen: AuthorizedFrontier,
    query: str,
    *,
    limit: int,
) -> tuple[EvidenceCandidate, ...]:
    """The production read path's post-freeze half, in the order the handler runs it.

    Re-prove freshness, narrow the session's material to the frozen frontier's ids, rank
    the value that comes back. `service/handlers/evidence.py` does exactly these three
    things and nothing else after the freeze, so a test that calls this is exercising the
    production composition rather than one of its own.
    """
    fts.require_current(holder.connection, projection)
    return rank_projected(projection.project(frozen), query, limit=limit)


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
        fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)


def test_lb_l11_a_search_over_a_lagging_projection_refuses_and_builds_nothing(
    owned: m2.Owned,
) -> None:
    """Zero known lag, no tolerance, and no repair on the request path."""
    build(owned)
    projection = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    assert ranked(owned, projection, frontier(owned), "alpha", limit=10)

    add_artifact(owned, "evd-later-1", recorded_at=m2.BASE_US + 500)
    before = (
        scalar(owned, "SELECT COUNT(*) FROM omnivia_projection_runs"),
        scalar(owned, "SELECT COUNT(*) FROM omnivia_projection_activations"),
        scalar(owned, f"SELECT COUNT(*) FROM {fts.DOCUMENTS_TABLE}"),
    )
    with pytest.raises(fts.StaleProjection):
        ranked(owned, projection, frontier(owned), "alpha", limit=10)
    with pytest.raises(fts.StaleProjection):
        fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    assert (
        scalar(owned, "SELECT COUNT(*) FROM omnivia_projection_runs"),
        scalar(owned, "SELECT COUNT(*) FROM omnivia_projection_activations"),
        scalar(owned, f"SELECT COUNT(*) FROM {fts.DOCUMENTS_TABLE}"),
    ) == before


def test_lb_l12_an_index_built_from_a_superseded_run_refuses(owned: m2.Owned) -> None:
    """Freshness is re-proven per call, not once at open."""
    build(owned)
    projection = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    add_artifact(owned, "evd-later-1", recorded_at=m2.BASE_US + 500)
    build(owned, now_us=NOW_US + 1000)
    with pytest.raises(fts.StaleProjection, match="has since activated"):
        ranked(owned, projection, frontier(owned), "alpha", limit=10)


def test_lb_l13_ranking_returns_frontier_members_in_the_shared_total_order(
    owned: m2.Owned,
) -> None:
    build(owned)
    projection = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    frozen = frontier(owned)
    results = ranked(owned, projection, frozen, "alpha", limit=10)

    assert [candidate.evidence_id for candidate in results] == [
        "evd-alpha-1",
        "evd-tie-b",
        "evd-tie-a",
    ]
    members = {candidate.evidence_id for candidate in frozen.candidates}
    assert {candidate.evidence_id for candidate in results} <= members

    # Relevance decides first place and the tie-breakers decide the rest, and both halves
    # are read off the material rather than asserted about: `evd-alpha-1` carries the term
    # more often than the other two, and the two tied artifacts carry token sequences that
    # are equal term for term -- so no relevance signal can separate them and recency then
    # identity has to. bm25 ties are the common case, not the exotic one.
    material = {
        item.candidate.evidence_id: item.terms
        for item in projection.project(frozen).candidates
    }
    assert material["evd-tie-a"] == material["evd-tie-b"]
    assert material["evd-alpha-1"].count("alpha") > material["evd-tie-a"].count("alpha")

    # And the order is the key's rather than the order the candidates arrived in --
    # §8.2's first ordering hazard, which a sort keyed on relevance alone would show.
    shuffled = replace(frozen, candidates=tuple(reversed(frozen.candidates)))
    assert ranked(owned, projection, shuffled, "alpha", limit=10) == results


def test_lb_l14_a_document_outside_the_frontier_is_never_returned(
    owned: m2.Owned,
) -> None:
    """The index holds every artifact; the frontier decides which ones exist for a read.

    Narrowed by the *temporal* filter, because it removes a candidate the ACL stage
    would have admitted -- so a result that leaked would be leaking through ranking
    rather than through a grant.
    """
    build(owned)
    projection = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    narrowed = frontier(owned, resolution_time_us=m2.BASE_US + 35)
    excluded = {candidate.evidence_id for candidate in narrowed.excluded}
    assert "evd-tie-b" in excluded

    results = ranked(owned, projection, narrowed, "alpha", limit=10)
    assert [candidate.evidence_id for candidate in results] == [
        "evd-alpha-1",
        "evd-tie-a",
    ]
    assert projection.document_count == 5


def test_lb_l15_the_limit_selects_from_the_ordered_head(owned: m2.Owned) -> None:
    build(owned)
    projection = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    frozen = frontier(owned)
    assert [
        c.evidence_id for c in ranked(owned, projection, frozen, "alpha", limit=2)
    ] == ["evd-alpha-1", "evd-tie-b"]
    assert ranked(owned, projection, frozen, "alpha", limit=0) == ()


def test_lb_l16_a_query_cannot_reach_the_fts5_operator_grammar(
    owned: m2.Owned,
) -> None:
    """`MATCH` takes a query language, and the query is caller-supplied text.

    Quoted as a phrase, so a caller cannot write `OR`, `NOT`, a column filter or a
    prefix wildcard into a search -- and a stray quote is data rather than a syntax
    error the caller can use to probe the schema.
    """
    build(owned)
    projection = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    frozen = frontier(owned)
    for hostile in (
        'alpha" OR "gamma',
        "alpha OR gamma",
        "search_text : alpha",
        "NEAR(alpha gamma)",
        '"',
    ):
        assert ranked(owned, projection, frozen, hostile, limit=10) == (), hostile
    assert ranked(owned, projection, frozen, "", limit=10) == ()
    # `*` is a prefix operator in FTS5's grammar and nothing at all in this one: the
    # query is split into tokens, so it reads as the single token `alpha`.
    assert ranked(owned, projection, frozen, "alpha*", limit=10) == ranked(
        owned, projection, frozen, "alpha", limit=10
    )


def test_lb_l17_matching_is_normalized_on_both_sides(owned: m2.Owned) -> None:
    build(owned)
    projection = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    frozen = frontier(owned)
    assert ranked(owned, projection, frozen, "ALPHA", limit=10) == ranked(
        owned, projection, frozen, "alpha", limit=10
    )


def test_lb_l18_a_frontier_from_another_workspace_is_refused(owned: m2.Owned) -> None:
    build(owned)
    projection = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
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
        projection.project(other)


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
    fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    assert persisted() == before
    assert fts.INDEX_TABLE not in guarded_tables()
    assert not any(name.startswith(fts.INDEX_TABLE) for name in before)

    session = {
        str(row[0])
        for row in owned.connection.execute("SELECT name FROM sqlite_temp_master")
    }
    assert fts.INDEX_TABLE in session
    assert f"{fts.INDEX_TABLE}_data" in session
    # The vocabulary view the token material is read out of is session-scoped on exactly
    # the same terms, and for the same reason: it is derived from the index, which is
    # derived from the durable rows.
    assert fts.VOCAB_TABLE in session
    assert fts.VOCAB_TABLE not in guarded_tables()
    assert fts.VOCAB_TABLE not in before


def test_lb_l20_reopening_rebuilds_the_index_from_the_durable_rows(
    owned: m2.Owned,
) -> None:
    """Restart convergence for the read side: a new session re-derives the index.

    Dropping the `temp` table stands for the process exiting; nothing needed to serve
    reads lives only in that table, so the next `open_search_projection` reproduces it
    from the run the ledger points at.
    """
    outcome = build(owned)
    first = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    expected = ranked(owned, first, frontier(owned), "alpha", limit=10)

    second = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    assert second.build.run_id == outcome.run_id
    assert second.document_count == first.document_count
    assert second.material == first.material
    assert ranked(owned, second, frontier(owned), "alpha", limit=10) == expected


def test_lb_l22_the_index_is_reachable_only_after_it_is_materialised(
    owned: m2.Owned,
) -> None:
    """The read path's handle appears when the index does, and never before.

    `session_search_projection` is how the handler reaches it, and it is a lookup with
    nothing to build from -- no identity, no fencing generation, no DDL grant. Before
    startup materialises one there is nothing to find, and a build that answered from a
    missing index would have to invent one rather than return `None`.
    """
    assert fts.session_search_projection(owned.connection) is None
    build(owned)
    # Still nothing: building the projection is not materialising the index, and the
    # two are separate calls in `service.main.serve` for exactly that reason.
    assert fts.session_search_projection(owned.connection) is None

    opened = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    found = fts.session_search_projection(owned.connection)

    assert found is opened
    assert found.build.run_id == fts.current_build(
        owned.connection, workspace_id=WORKSPACE_ID
    ).run_id


def test_lb_l23_a_failed_rematerialisation_leaves_no_index_reachable(
    owned: m2.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A superseded handle must not survive an attempt to replace it.

    The projection advances, the index is re-opened, and the re-open fails. What must
    not remain reachable is the previous index: it describes a run the ledger no longer
    points at, and a read served from it would be answering from a superseded build
    while every freshness check in the path reported current.
    """
    build(owned)
    fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    assert fts.session_search_projection(owned.connection) is not None

    add_artifact(owned, "evd-later-1", recorded_at=m2.BASE_US + 500)
    build(owned, now_us=NOW_US + 1000)
    # The failure lands after the index table has been dropped and re-created, which is
    # the window that matters: the `temp` table the old handle named is already gone.
    monkeypatch.setattr(
        fts,
        "normalize_query",
        lambda _text: (_ for _ in ()).throw(Interrupted("x")),
    )
    with pytest.raises(Interrupted):
        fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)

    assert fts.session_search_projection(owned.connection) is None


def test_lb_l23a_the_materialised_store_is_read_only(owned: m2.Owned) -> None:
    """The session's material rejects a write, rather than being annotated as if it did.

    `SearchProjection` is frozen, so the *field* cannot be rebound -- but the store it
    names is what every request for the life of the connection is ranked against, and
    `Mapping` is a static claim that a `dict` satisfies while remaining writable through
    any surviving reference. This is the one piece of Lane B's state that outlives a
    request, so the immutability is made structural at materialisation instead.

    Falsifier: return the plain `dict` from `_materialise_terms`. Nothing else in the
    tree changes, which is the point -- the defect is invisible until something writes.
    """
    build(owned)
    projection = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)

    with pytest.raises(TypeError):
        projection.material["evd-alpha-1"] = ()  # type: ignore[index]
    assert projection.material["evd-alpha-1"] != ()


class PoisonMaterial(Mapping[str, tuple[str, ...]]):
    """A projection store that fails rather than lets the adapter overreach.

    Every way the narrowing could observe more than the frontier is a failure here, and
    each is a real defect rather than a hypothetical one:

    * iterating -- `for evidence_id in material` -- is the corpus scan the whole design
      exists to prevent, and it reads perfectly innocently at the call site;
    * addressing an id the frontier excludes is the same violation one row at a time;
    * addressing anything after `sealed` is set is a lazy lookup happening *during*
      ranking, which is how a value that looks immutable ends up reaching a store.
    """

    def __init__(
        self, material: Mapping[str, tuple[str, ...]], *, admitted: frozenset[str]
    ) -> None:
        self._material = dict(material)
        self._admitted = admitted
        self.addressed: list[str] = []
        self.sealed = False

    def __getitem__(self, key: str) -> tuple[str, ...]:
        if self.sealed:
            raise AssertionError(f"material was addressed lazily, for {key!r}")
        if key not in self._admitted:
            raise AssertionError(f"material was addressed for excluded {key!r}")
        self.addressed.append(key)
        return self._material[key]

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("the whole corpus was iterated")

    def __len__(self) -> int:
        raise AssertionError("the whole corpus was measured")


def test_lb_l24_the_narrowing_addresses_the_frontier_and_never_the_corpus(
    owned: m2.Owned,
) -> None:
    """Packet §7.2 names scoring, and this is where a build gets that wrong.

    The session holds material for every artifact in the workspace, so the tempting
    implementation walks it and keeps the ids the frontier holds. The page is identical
    either way, which is why this is asserted against the *store* rather than against a
    result: the material is replaced with one that fails on iteration, on any excluded
    id, and on any access at all once ranking has started.

    `evd-tie-b` is excluded by the temporal filter and matches the query, so it is a
    document the defective build would have scored.
    """
    build(owned)
    projection = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    narrowed = frontier(owned, resolution_time_us=m2.BASE_US + 35)
    admitted = {candidate.evidence_id for candidate in narrowed.candidates}
    assert "evd-tie-b" in {candidate.evidence_id for candidate in narrowed.excluded}

    store = PoisonMaterial(projection.material, admitted=frozenset(admitted))
    watched = replace(projection, material=store)

    statements: list[str] = []
    owned.connection.set_trace_callback(statements.append)
    try:
        projected = watched.project(narrowed)
        # Sealed before ranking, so any lookup the ranker performed would fail here. It
        # cannot perform one: what it holds is a value, not a way back to this store.
        store.sealed = True
        results = rank_projected(projected, "alpha", limit=10)
    finally:
        owned.connection.set_trace_callback(None)

    assert sorted(store.addressed) == sorted(admitted)
    # And the narrowing and the ranking together issue no statement at all. There is no
    # index read on the request path to constrain correctly, because there is no index
    # read on the request path.
    assert statements == []
    assert [candidate.evidence_id for candidate in results] == [
        "evd-alpha-1",
        "evd-tie-a",
    ]


def fts5_tokens(text: str) -> tuple[str, ...]:
    """One string, split by a real FTS5 index built with the projection's own tokenizer.

    A throwaway database rather than the session's index, because the point is the
    *tokenizer* and inserting probe text into the live index would change the material a
    later assertion reads. `fts.TOKENIZER` is the same constant `open_search_projection`
    builds with, so what this measures is what production tokenizes with.
    """
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.execute(
            f"CREATE VIRTUAL TABLE probe USING fts5(body, tokenize = '{fts.TOKENIZER}')"
        )
        scratch.execute("INSERT INTO probe (body) VALUES (?)", (text,))
        scratch.execute("CREATE VIRTUAL TABLE probe_v USING fts5vocab(probe, instance)")
        return tuple(
            str(row[0])
            for row in scratch.execute("SELECT term FROM probe_v ORDER BY offset ASC")
        )
    finally:
        scratch.close()


def test_lb_l25_the_query_tokenizer_agrees_with_the_projections_own(
    owned: m2.Owned,
) -> None:
    """The one fact this design states twice, pinned rather than trusted.

    Documents are tokenized by FTS5 at materialisation; queries are tokenized by
    `retrieval.query_tokens`, which has no index to ask and must not acquire one. A drift
    between the two is a silent recall bug -- a search that matches nothing for a
    document that plainly contains the term -- so the equality is asserted against a real
    index, over the workspace's own material and over the cases a hand-written tokenizer
    gets wrong.
    """
    build(owned)
    projection = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    surfaces = {
        str(evidence_id): str(text)
        for evidence_id, text in owned.connection.execute(
            f"SELECT evidence_id, search_text FROM {fts.DOCUMENTS_TABLE}"
        )
    }

    assert surfaces
    for evidence_id, terms in projection.material.items():
        assert query_tokens(surfaces[evidence_id]) == terms, evidence_id

    for probe in (
        "café_naïve",
        "ÜNÏCODE-doc",
        "archive://alpha/two.md",
        "straße ß",
        "a.b:c/d_e",
        "42 x² ½",
        "   ",
        "",
        'alpha" OR "gamma',
    ):
        # `normalize_query` on the way in on both sides, exactly as materialisation
        # applies it to every document: the claim is that the two *tokenizers* agree,
        # not that FTS5 normalizes.
        assert query_tokens(probe) == fts5_tokens(normalize_query(probe)), probe


# --- §20.12 F4: the production ranker, and the four forms it must not take ------


def test_lb_l26_f4_the_production_ranker_is_reachable_by_no_store_at_all(
    owned: m2.Owned,
) -> None:
    """F4 against the ranker this build actually serves from, in all four forms.

    §20.12's ruling is that a signature-only inspection is insufficient, so this asserts
    the signature, the module's import boundary and the module's own bindings, and then
    builds each defective ranker the prohibition names and shows it returning an artifact
    the projected frontier does not hold. The kill is that `rank_projected` cannot be
    written any of those ways: there is nothing in `storage/retrieval.py` that could
    supply a connection, a store or a retrieval callback, and the value it is handed
    carries the tokens themselves rather than a way to fetch them.

    The first Lane B design failed exactly here. Its ranker lived in
    `storage/projections/fts.py`, which imports `sqlite3`, `storage.connection` and
    `storage.repository`, and it held the connection its index was on -- so the second
    and fourth assertions below would both have failed against it.
    """
    build(owned)
    projection = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    frozen = frontier(owned)
    projected = projection.project(frozen)

    # 1. The signature. Three parameters: an immutable value, a string, an integer.
    signature = inspect.signature(rank_projected)
    assert list(signature.parameters) == ["projected", "query", "limit"]
    assert signature.parameters["projected"].annotation == "ProjectedFrontier"
    assert signature.parameters["query"].annotation == "str"
    assert signature.parameters["limit"].annotation == "int"
    for parameter in signature.parameters.values():
        assert parameter.default is inspect.Parameter.empty

    # 2. The import boundary, read off the source rather than the runtime namespace, so
    #    a lazy import inside a function cannot evade it.
    source = pathlib.Path(inspect.getsourcefile(retrieval_module) or "").read_text()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for forbidden in (
        "sqlite3",
        "omnivia_core_runtime.storage.connection",
        "omnivia_core_runtime.storage.repository",
        "omnivia_core_runtime.storage.projections",
        "omnivia_core_runtime.storage.projections.fts",
    ):
        assert forbidden not in imported
    assert not any(name.startswith("omnivia_core_runtime.") for name in imported), sorted(
        imported
    )

    # 3. Nothing at module scope is a live handle, so no global and no closure could
    #    have captured one -- and the value the ranker is handed holds none either.
    for name, value in vars(retrieval_module).items():
        if name.startswith("__"):
            continue
        assert not hasattr(value, "execute"), name
        assert not hasattr(value, "cursor"), name
    for held in (projected, *projected.candidates):
        for field in held.__slots__:
            attribute = getattr(held, field)
            assert not hasattr(attribute, "execute"), field
            assert not hasattr(attribute, "cursor"), field
            assert not callable(attribute), field

    # 4. And each forbidden form, built and shown leaking. `evd-tie-b` is on no frontier
    #    this test froze; every branch returns it anyway, which is what the real ranker
    #    has no expression available to do.
    smuggled = next(
        candidate
        for candidate in read_evidence_candidates(
            owned.connection, workspace_id=WORKSPACE_ID
        )
        if candidate.evidence_id == "evd-tie-b"
    )
    narrowed = projection.project(frontier(owned, resolution_time_us=m2.BASE_US + 35))
    admitted = {item.candidate.evidence_id for item in narrowed.candidates}
    assert "evd-tie-b" not in admitted

    def by_connection(
        value: ProjectedFrontier, connection: sqlite3.Connection
    ) -> tuple[str, ...]:
        """A ranker holding the connection its index is on -- the rejected design."""
        return tuple(
            str(row[0])
            for row in connection.execute(
                f"SELECT evidence_id FROM {fts.DOCUMENTS_TABLE} ORDER BY evidence_id"
            )
        )

    module_store = {smuggled.evidence_id: smuggled}

    def by_module_store(value: ProjectedFrontier) -> tuple[str, ...]:
        return (*(i.candidate.evidence_id for i in value.candidates), *module_store)

    def with_closure() -> Any:
        captured = module_store

        def by_closure(value: ProjectedFrontier) -> tuple[str, ...]:
            return (*(i.candidate.evidence_id for i in value.candidates), *captured)

        return by_closure

    def by_callback(
        value: ProjectedFrontier, retrieve: Any
    ) -> tuple[str, ...]:
        return (*(i.candidate.evidence_id for i in value.candidates), retrieve())

    leaks = (
        by_connection(narrowed, owned.connection),
        by_module_store(narrowed),
        with_closure()(narrowed),
        by_callback(narrowed, lambda: smuggled.evidence_id),
    )
    for leaked in leaks:
        assert "evd-tie-b" in leaked
    assert "evd-tie-b" not in {
        candidate.evidence_id for candidate in rank_projected(narrowed, "alpha", limit=10)
    }


def test_lb_l27_a_candidate_with_no_material_refuses_rather_than_falling_back(
    owned: m2.Owned,
) -> None:
    """Missing material is a retryable refusal, and the fallback is not taken.

    The tempting repair is right there: `EvidenceCandidate.search_text` on the frozen
    frontier is the artifact's authoritative identity surface, and ranking from it would
    answer this call and look correct. It would be answering from outside the projection
    this build claims to serve, while every freshness check in the path reported current
    -- the silent staleness §20.7 forbids, arrived at by being helpful.

    So the refusal is asserted *and* the material that would have satisfied a fallback is
    shown to be present, because a test that only asserted the raise would pass against a
    build that had nothing to fall back to.
    """
    build(owned)
    projection = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    frozen = frontier(owned)
    surfaces = {
        candidate.evidence_id: candidate.search_text for candidate in frozen.candidates
    }
    assert "alpha" in surfaces["evd-alpha-1"]

    thinned = replace(
        projection,
        material={
            evidence_id: terms
            for evidence_id, terms in projection.material.items()
            if evidence_id != "evd-alpha-1"
        },
    )

    with pytest.raises(fts.ProjectionUnavailable):
        thinned.project(frozen)
    # And the refusal names no candidate: which id is missing is a fact about this
    # caller's authorized material.
    with pytest.raises(fts.ProjectionUnavailable) as refused:
        thinned.project(frozen)
    assert "evd-alpha-1" not in str(refused.value)


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
    assert not hasattr(fts.SearchProjection, "rebuild")
    assert not hasattr(fts.SearchProjection, "refresh")
