"""The `evidence.search` FTS5 projection: its builder, and the index reads run over.

**What is durable and what is not, and why the split is not a compromise.**

Migration 0012 declares one ordinary table, `omnivia_evidence_search_documents`, and
that table is the projection. The FTS5 index over it is a `temp` virtual table this
module materialises from the activated run (`open_search_index`). The split is forced
and it is the correct one:

* SQLite refuses `CREATE TRIGGER` on a virtual table -- literally, *"cannot create
  triggers on virtual tables"* -- and a persisted `fts5` table also materialises five
  shadow tables (`_data`, `_idx`, `_content`, `_docsize`, `_config`) whose internal
  INSERTs and DELETEs a guard trigger would abort mid-merge.
* Guard coverage in this repository is derived, not curated. `fencing.guarded_tables()`
  reads the canonical schema and subtracts a named substrate, and
  `test_fencing_mutation.py::test_sb05_every_mutable_table_is_guarded_for_every_statement`
  asserts both that every guarded table has all three statement triggers *and* that
  the unguarded remainder is exactly the substrate. A persisted FTS5 table would put
  six ungovernable tables in the authoritative database and fail that test -- correctly.

So the durable rows carry the evidence and take the guards; the index carries the
inverted lists and is rebuilt per session. That costs a linear pass over one run's
documents at startup and buys a projection whose every persisted byte is fenced,
leased and workspace-scoped like every other row in the database.

**It still satisfies persistence and restart convergence**, which is the property that
matters and the one a session-scoped index could plausibly break. It does not, because
the index is a pure function of durable rows the ledger already points at: after a
crash at any point, `build_search_projection` re-derives its position from the
database rather than from process state, and `open_search_index` re-derives the index
from the activated run. Nothing a restart needs lives only in memory. The `temp`
schema is invisible to `sqlite_master`, so the index cannot move a schema fingerprint,
cannot appear in `guarded_tables()`, and cannot survive to be found stale.

**Reads never build.** `SearchIndex.search` materialises nothing, repairs nothing and
falls back to nothing. It re-checks freshness against the authoritative checkpoint on
every call and refuses -- `ProjectionUnavailable` when no run is activated,
`StaleProjection` when the activated run is behind by any amount at all. Packet §20.7
removed the middle option (*"There is no configurable time tolerance in v0.6"*), so
there is no threshold here and no operator "rebuild now" entry point either: the only
way this projection advances is `build_search_projection`, which is maintenance work,
not request work.

**Ranking observes the frozen frontier and only the frozen frontier.** `search` takes
an `AuthorizedFrontier` and binds *its ids into the ranking statement*, so `bm25()` is
evaluated over frontier members and nothing else -- packet §7.2 names scoring alongside
ranking and selection, and asking FTS5 to score the whole index and discarding the
misses afterwards would have observed the candidates the filter chain excluded. Each
score is then resolved back to its candidate by lookup in the frontier, so §7.2's
"every item in any result SHALL be a member of that frozen frontier" holds by
construction rather than by a membership check. The ordering is
`retrieval.relevance_order_key` -- the same total order Lane A sorts by, with bm25
supplying the relevance Lane A had no index to compute.

One bounded residual, stated rather than hidden: bm25's inverse document frequency is a
statistic of the *index*, which holds every artifact in the workspace, so an excluded
artifact carrying a query term can shift the relative scores of frontier members. It
cannot enter a result, be scored, or be observed by any ordering or selection decision.
Removing even that would mean an index built per request from the frontier, which is a
build on the request path -- the one thing this projection may never do.

**Reads reach the index through `session_search_index` and never materialise one.**
The index is recorded on the connection whose `temp` schema holds it, so the handler's
lookup carries no identity, no fencing generation and no DDL grant: there is nothing
for a request path to build with even if a later edit wanted to.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Final

from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError, authorised
from omnivia_core_runtime.storage.projections import EVIDENCE_SEARCH_PROJECTION_ID
from omnivia_core_runtime.storage.repository import (
    authoritative_checkpoint,
    projection_readiness,
)
from omnivia_core_runtime.storage.retrieval import (
    AuthorizedFrontier,
    EvidenceCandidate,
    normalize_query,
    relevance_order_key,
)

#: The ledger identity this module builds and reads. One projection, one id, and the
#: same one `repository.CONTRIBUTING_PROJECTIONS` gates on -- see the package constant
#: for why it is declared a level up rather than here.
PROJECTION_ID: Final = EVIDENCE_SEARCH_PROJECTION_ID

#: The ledger's `projection_kind`, which is the *contract* the projection serves
#: rather than the technology serving it. A future replacement index would keep this
#: kind and take a new `PROJECTION_ID`.
PROJECTION_KIND: Final = "evidence.search"

#: The migration that declares the durable table, and the tokenizer profile the index
#: is built with. Both are recorded on every run so a build made under one profile is
#: distinguishable from a build made under another rather than silently comparable.
SCHEMA_VERSION: Final = "0012"
PROFILE_VERSION: Final = "fts5.unicode61.1"

#: The durable projection table, and the session-scoped index over it.
DOCUMENTS_TABLE: Final = "omnivia_evidence_search_documents"
INDEX_TABLE: Final = "omnivia_evidence_search_index"

#: Documents written between checkpoints. Every checkpoint is a resume point, so this
#: is the bound on how much work an interruption can cost -- not a performance knob.
#: ponytail: fixed batch, make it a parameter if a workspace ever makes it matter.
CHECKPOINT_BATCH: Final = 256

#: Frontier ids bound into one ranking statement. SQLite bounds a statement's host
#: parameters (`SQLITE_MAX_VARIABLE_NUMBER`, 999 on older builds) and a frontier is a
#: whole workspace's worth of candidates, so the ids are passed in batches. bm25 reads
#: its statistics off the index rather than off the result set, so a batched ranking and
#: a single-statement one score identically.
MATCH_BATCH: Final = 500


class ProjectionError(StorageError):
    """The search projection cannot serve this read, and no read may proceed."""


class Fts5Unavailable(ProjectionError):
    """This SQLite build has no FTS5, so the projection cannot exist at all."""


class ProjectionUnavailable(ProjectionError):
    """No run has been activated; there is nothing to serve from."""


class StaleProjection(ProjectionError):
    """The activated run is behind the authoritative checkpoint. Refuse, never build."""


@dataclass(frozen=True, slots=True)
class ActiveBuild:
    """The run the ledger currently points at, read from the activation history.

    From `omnivia_projection_activations` rather than from the ledger columns, because
    0011's pointer trigger refuses any ledger write without a matching activation row:
    the history is authoritative and the pointer is downstream of it.
    """

    run_id: str
    epoch: int
    source_checkpoint: str
    build_digest: str


@dataclass(frozen=True, slots=True)
class BuildOutcome:
    """What one `build_search_projection` call left behind.

    `activated` is `False` when the call found the projection already level with the
    authoritative checkpoint and did nothing. That is the ordinary result of running
    the builder twice, and it is what makes the builder safe to call on every restart.
    """

    run_id: str
    epoch: int
    source_checkpoint: str
    document_count: int
    build_digest: str
    activated: bool


def require_fts5(connection: sqlite3.Connection) -> None:
    """Refuse early, by name, on a SQLite without FTS5.

    Asked of the compile options rather than by attempting a `CREATE VIRTUAL TABLE`,
    so the check needs no DDL grant and no cleanup, and so the failure is *"this build
    has no FTS5"* rather than a create error a caller has to interpret.
    """
    row = connection.execute(
        "SELECT COUNT(*) FROM pragma_compile_options "
        "WHERE compile_options = 'ENABLE_FTS5'"
    ).fetchone()
    if row is None or int(row[0]) != 1:
        raise Fts5Unavailable(
            "this SQLite build does not have FTS5; the evidence.search projection "
            "cannot be built or read"
        )


def active_build(
    connection: sqlite3.Connection, *, workspace_id: str
) -> ActiveBuild | None:
    """The activated run, or `None` when the projection has never been activated."""
    with authorised(connection, mutations=False, ddl=False) as fenced:
        row = fenced.execute(
            "SELECT run_id, target_epoch, source_checkpoint, build_digest "
            "FROM omnivia_projection_activations "
            "WHERE workspace_id = ? AND projection_id = ? "
            "ORDER BY activation_sequence DESC LIMIT 1",
            (workspace_id, PROJECTION_ID),
        ).fetchone()
    if row is None:
        return None
    return ActiveBuild(
        run_id=str(row[0]),
        epoch=int(row[1]),
        source_checkpoint=str(row[2]),
        build_digest=str(row[3]),
    )


def current_build(connection: sqlite3.Connection, *, workspace_id: str) -> ActiveBuild:
    """The activated run, proven level with the authoritative checkpoint, or a refusal.

    Equality, no tolerance, and the two failures are kept apart because they mean
    different things to a caller: nothing activated is not the same as something
    activated and behind. The comparison is delegated to `repository.projection_readiness`
    so this projection inherits the gate the rest of the read path already uses rather
    than growing a second freshness rule that could drift from it.
    """
    checkpoint = authoritative_checkpoint(connection, workspace_id=workspace_id)
    readiness = projection_readiness(
        connection,
        workspace_id=workspace_id,
        source_checkpoint=checkpoint,
        contributing=(PROJECTION_ID,),
    )
    if readiness.missing:
        raise ProjectionUnavailable(
            f"{PROJECTION_ID} has no activated run; evidence.search cannot be served"
        )
    build = active_build(connection, workspace_id=workspace_id)
    if readiness.stale or build is None or build.source_checkpoint != checkpoint:
        raise StaleProjection(
            f"{PROJECTION_ID} is at checkpoint "
            f"{build.source_checkpoint if build else 'none'!r} and the workspace is "
            f"at {checkpoint!r}; there is no tolerance for lag"
        )
    return build


@dataclass(frozen=True, slots=True)
class SearchIndex:
    """One session's FTS5 index over one activated run.

    Holds the connection because the index lives in that connection's `temp` schema
    and nowhere else -- a second connection cannot see it, which is the honest scope
    of a session-scoped index and is why this is a value a caller keeps rather than a
    global something could reach.

    **This class is the production ranker, and it holds a store.** That is the one
    §20.12 property Lane B cannot inherit: `retrieval.rank_candidates` proved it could
    not reach an unfiltered candidate through its module's *import boundary*, and an
    FTS5 ranker has an index in SQLite, so it holds a connection to one. The proof is
    therefore made over values instead: the ranking statement is constrained to the
    frozen frontier's ids and every score is resolved back through that same frontier,
    so what comes out is a subset of what went in. The frontier still arrives frozen,
    and this class narrows it and never widens it -- but a reviewer should read
    `search` for that, not take the import block's word for it.
    """

    connection: sqlite3.Connection
    workspace_id: str
    build: ActiveBuild
    document_count: int

    def search(
        self, frontier: AuthorizedFrontier, query: str, *, limit: int
    ) -> tuple[EvidenceCandidate, ...]:
        """Rank the frozen frontier by bm25 over this index. Builds nothing, ever.

        Freshness is re-proven on every call, not once at open: an index opened when
        the projection was current is stale the moment an artifact is ingested, and a
        session that kept serving from it would be exactly the silent staleness §20.7
        forbids. Two things are checked -- the activated run must still be level with
        the workspace, and it must still be *this* index's run.

        The query is matched as a single FTS5 phrase. That is a deliberate narrowing
        of Lane A's substring test to token boundaries, and it is also what makes a
        caller-supplied string safe here: FTS5's `MATCH` argument is a query language
        with its own operators, so a raw query would let a caller write `AND`, `NOT`,
        column filters and prefix wildcards into a search. Quoting it as a phrase
        leaves no operator reachable.

        **The frontier is handed to FTS5, not applied to its answer.** The statement
        below carries the frozen frontier's ids, so the only documents `bm25()` is ever
        evaluated over are candidates that already passed every filter -- packet §7.2
        names *scoring* alongside ranking and selection, and a build that scored the
        whole index and dropped the misses afterwards would have observed exactly what
        the clause forbids. It would also be invisible to every result-level assertion,
        which is F2's whole point: widen the frontier by one unauthorized artifact that
        scores last and the returned page is byte-identical.
        """
        current = current_build(self.connection, workspace_id=self.workspace_id)
        if current.run_id != self.build.run_id:
            raise StaleProjection(
                f"this index was built from run {self.build.run_id!r} and "
                f"{PROJECTION_ID} has since activated {current.run_id!r}"
            )
        if frontier.workspace_id != self.workspace_id:
            raise ProjectionError(
                f"frontier names workspace {frontier.workspace_id!r} and this index "
                f"serves {self.workspace_id!r}"
            )

        phrase = '"' + normalize_query(query).replace('"', '""') + '"'
        by_id = {candidate.evidence_id: candidate for candidate in frontier.candidates}
        identifiers = sorted(by_id)
        matched: list[tuple[EvidenceCandidate, float]] = []
        # Read under the same explicit fence every other read in this tree runs under.
        # Deny-by-default is already in force, so this widens nothing; it states that
        # the ranking path is a read, in the one place a future edit would be tempted
        # to make it repair something.
        with authorised(self.connection, mutations=False, ddl=False) as fenced:
            for start in range(0, len(identifiers), MATCH_BATCH):
                batch = identifiers[start : start + MATCH_BATCH]
                marks = ", ".join("?" * len(batch))
                # `bm25()` sits in the result list and the frontier predicate in the
                # WHERE, so SQLite scores only rows that survive the predicate. And the
                # candidate each score belongs to is looked *up* in the frontier: there
                # is no branch here that could return an object the frontier does not
                # already hold.
                matched.extend(
                    (by_id[str(evidence_id)], float(relevance))
                    for evidence_id, relevance in fenced.execute(
                        f"SELECT evidence_id, bm25({INDEX_TABLE}) FROM {INDEX_TABLE} "
                        f"WHERE {INDEX_TABLE} MATCH ? AND evidence_id IN ({marks})",
                        (phrase, *batch),
                    ).fetchall()
                )
        matched.sort(key=lambda pair: relevance_order_key(pair[0], pair[1]))
        return tuple(candidate for candidate, _ in matched[:limit])


def open_search_index(
    connection: sqlite3.Connection, *, workspace_id: str
) -> SearchIndex:
    """Materialise this session's index from the activated run, or refuse.

    Called from startup and maintenance, never from a request -- `service/main.py`
    invokes it behind `build_search_projection` before the endpoint accepts anything.
    It re-creates the `temp` table from scratch rather than repairing one, because a
    rebuild from durable rows is both cheaper to reason about and the only version that
    converges after an interruption partway through a previous materialisation.

    **The widening is two statements wide.** `temp` DDL is denied on a governed service
    connection like every other schema action -- `_schema_action_codes()` matches every
    `SQLITE_CREATE_*`, temporary and virtual-table actions included -- so the grant is
    stated here rather than assumed. It covers the drop and the create and stops: those
    two carry no data and no caller value, and the linear pass that does carry the
    workspace's text runs under a DML grant with DDL refused. Splitting them is not
    decoration -- an authorizer grant is only as narrow as the block it spans, and the
    insert loop is by far the longest-lived of the three statements.

    `mutations=True` is needed for the create as well, and that is FTS5 rather than
    carelessness: the virtual table's constructor writes its own `_config` shadow row,
    which reaches the authorizer as an ordinary INSERT and fails the create without it.
    """
    require_fts5(connection)
    build = current_build(connection, workspace_id=workspace_id)
    with authorised(connection, mutations=False, ddl=False) as fenced:
        documents = fenced.execute(
            f"SELECT evidence_id, search_text FROM {DOCUMENTS_TABLE} "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ? "
            "ORDER BY evidence_id ASC",
            (workspace_id, PROJECTION_ID, build.run_id),
        ).fetchall()

    # Cleared first. A failure below must not leave a previous session's index
    # reachable through `session_search_index` behind a run that is no longer active.
    connection.omnivia_search_index = None  # type: ignore[attr-defined]
    with authorised(connection, mutations=True, ddl=True):
        connection.execute(f"DROP TABLE IF EXISTS temp.{INDEX_TABLE}")
        connection.execute(
            f"CREATE VIRTUAL TABLE temp.{INDEX_TABLE} USING fts5("
            "evidence_id UNINDEXED, search_text, tokenize = 'unicode61')"
        )
    with authorised(connection, mutations=True, ddl=False):
        connection.executemany(
            f"INSERT INTO temp.{INDEX_TABLE} (evidence_id, search_text) "
            "VALUES (?, ?)",
            # Normalized on the way in, exactly as `normalize_query` normalizes the
            # query, so a match is a property of the text rather than of which side a
            # caller happened to type. The durable row keeps the raw identity surface,
            # which is what makes it comparable with the candidate the read layer builds.
            [
                (evidence_id, normalize_query(str(search_text)))
                for evidence_id, search_text in documents
            ],
        )
    index = SearchIndex(
        connection=connection,
        workspace_id=workspace_id,
        build=build,
        document_count=len(documents),
    )
    connection.omnivia_search_index = index  # type: ignore[attr-defined]
    return index


def session_search_index(connection: sqlite3.Connection) -> SearchIndex | None:
    """This session's index, or `None` when nothing has materialised one.

    The index is recorded on the connection because that is exactly where it lives: the
    `temp` schema is connection-local, so the index and the connection have one lifetime
    and one scope, and a handle kept anywhere else could outlive the table it names.
    `storage/connection.py` already records the authorizer grant on the connection for
    the same reason, so this introduces no mechanism -- only one more connection-local
    fact.

    This is the read path's only way to reach an index. It is a lookup and cannot
    become anything else: it holds no identity, no fencing generation and no DDL grant,
    so a request that reached for a missing index gets `None` and a refusal rather than
    a build. Materialising is `open_search_index`, and that is startup and maintenance
    work.
    """
    index = getattr(connection, "omnivia_search_index", None)
    return index if isinstance(index, SearchIndex) else None


def build_search_projection(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    now_us: int,
) -> BuildOutcome:
    """Bring the projection level with the workspace, from wherever it actually is.

    Maintenance work, and the only way this projection ever advances. There is no
    request path into it and no operator "rebuild" verb beside it: a rebuild is this
    same call, which is why interruption and restart converge instead of needing an
    escape hatch.

    Every phase below reads its own position out of the database and is separately
    idempotent, and each runs in its own fenced transaction. So a crash between any
    two of them -- or inside one, which rolls back -- leaves a state this function
    resumes from on the next call:

    * killed while appending -> the run is still `running`, its committed documents and
      checkpoints stand, and the next call resumes after the last durable document;
    * killed before validating -> validation re-derives its counts and digest from the
      durable rows, so a resumed build and an uninterrupted one produce the same digest;
    * killed between validating and activating -> the run is complete, validated and
      unactivated. 0011 has no `succeeded -> failed` transition, so the only legal move
      is to activate it; the loop below then builds forward if the workspace moved on
      meanwhile, which is why this is a loop and not a straight line;
    * killed after activating -> the ledger already points at this run, the checkpoint
      already matches, and the next call finds nothing to do and says so.

    The ledger pointer moves in exactly one place, and it is not here: the activation
    INSERT's AFTER trigger moves it. Nothing in this module issues an UPDATE against
    `omnivia_projection_ledger`'s pointer columns, and 0011's guard would refuse one.
    """
    require_fts5(connection)
    fence = (workspace_id, fencing_generation)
    _ensure_ledger_row(connection, identity, fence=fence)

    while True:
        checkpoint = authoritative_checkpoint(connection, workspace_id=workspace_id)
        plan = _plan_run(
            connection, identity, fence=fence, checkpoint=checkpoint, now_us=now_us
        )
        if plan is None:
            # `_plan_run` answers `None` only when the ledger is already level with this
            # checkpoint, and the ledger cannot be level without an activation behind it.
            build = current_build(connection, workspace_id=workspace_id)
            # Reclaim anyway. A crash between activation and reclamation leaves a
            # superseded run's documents behind, and this is the call that would
            # otherwise never come back for them.
            _reclaim(connection, identity, fence=fence, keep_run_id=build.run_id)
            return BuildOutcome(
                run_id=build.run_id,
                epoch=build.epoch,
                source_checkpoint=build.source_checkpoint,
                document_count=_document_count(connection, workspace_id, build.run_id),
                build_digest=build.build_digest,
                activated=False,
            )

        run_id, epoch, state, run_checkpoint = plan
        if state == "running":
            _append_documents(
                connection,
                identity,
                fence=fence,
                run_id=run_id,
                checkpoint=run_checkpoint,
            )
            digest, count = _validate_run(
                connection, identity, fence=fence, run_id=run_id, now_us=now_us
            )
        else:
            # A run interrupted between validation and activation. Its content is
            # complete and its digest is already attested, so there is nothing to
            # rebuild -- only the activation it never reached.
            digest, count = _completed_run_evidence(connection, workspace_id, run_id)
        _activate_run(
            connection,
            identity,
            fence=fence,
            run_id=run_id,
            epoch=epoch,
            checkpoint=run_checkpoint,
            now_us=now_us,
        )
        _reclaim(connection, identity, fence=fence, keep_run_id=run_id)
        if run_checkpoint == checkpoint:
            return BuildOutcome(
                run_id=run_id,
                epoch=epoch,
                source_checkpoint=run_checkpoint,
                document_count=count,
                build_digest=digest,
                activated=True,
            )
        # The recovered run was complete but for an older checkpoint, so activating it
        # was correct and is not the whole job. The loop runs again at the current
        # checkpoint; it terminates because each pass activates a strictly higher epoch
        # and the second pass always starts from the current one.


def _ensure_ledger_row(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    fence: tuple[str, int],
) -> None:
    """Declare the projection in the ledger once, without touching its pointer.

    0011's FK and its run-declaration guard both require the ledger row to exist before
    a run can start, and the ledger carries no `workspace_id`, so the row is created
    here rather than by the migration: a migration that inserted it would be writing
    workspace data from DDL, and would have to do it without a guard open.

    The pointer columns are left NULL. They are activation's to set.
    """
    workspace_id, generation = fence
    with authorised(connection, mutations=False, ddl=False) as fenced:
        declared = fenced.execute(
            "SELECT 1 FROM omnivia_projection_ledger WHERE projection_id = ?",
            (PROJECTION_ID,),
        ).fetchone()
    if declared is not None:
        return
    with fenced_transaction(
        connection, identity, workspace_id=workspace_id, fencing_generation=generation
    ):
        connection.execute(
            "INSERT INTO omnivia_projection_ledger "
            "(projection_id, version, rebuildable, state, updated_at, "
            "fencing_generation) VALUES (?, ?, 1, 'ready', ?, ?)",
            (PROJECTION_ID, SCHEMA_VERSION, _LEDGER_DECLARED_AT, generation),
        )


#: The ledger's `updated_at` is a Phase 0 text column with no format constraint and no
#: reader in this lane. A constant keeps the ledger declaration byte-identical across
#: rebuilds, so a workspace's schema-level content does not depend on when it was
#: first built; the times that mean something are 0011's `_us` columns.
_LEDGER_DECLARED_AT: Final = "0012-evidence-search-projection"

#: The canonical error a build records when it abandons a run whose source checkpoint
#: the workspace has already moved past.
_SUPERSEDED_SOURCE: Final = json.dumps(
    {"code": "projection.source_checkpoint_advanced"},
    separators=(",", ":"),
    sort_keys=True,
)


def _plan_run(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    fence: tuple[str, int],
    checkpoint: str,
    now_us: int,
) -> tuple[str, int, str, str] | None:
    """The run this build should be advancing, or `None` when there is nothing to do.

    "In flight" means *declared and not yet activated*, and it deliberately includes
    `succeeded` as well as `running`. A build killed between validation and activation
    leaves a complete, validated, unactivated run; treating only `running` as in flight
    made that run invisible, and the next build tried to declare a fresh one at the same
    epoch and was refused by 0011's identity guard -- a restart that could not converge.

    Four answers, and which one comes back is decided by the database rather than by
    anything the caller remembers:

    * an in-flight run already at this checkpoint -> resume it, so an interrupted build
      continues instead of starting a second run;
    * an in-flight `succeeded` run at an older checkpoint -> hand it back anyway. Its
      content is complete and 0011 has no `succeeded -> failed` transition, so the way
      forward is to activate it and let the caller's loop advance past it;
    * an in-flight `running` run at a checkpoint the workspace has moved past -> fail it
      with a canonical error and start a fresh one, because its content would attest to
      a digest over evidence that is no longer the whole of the workspace;
    * nothing in flight and the ledger already at this checkpoint -> `None`.

    Run ids are derived from `(epoch, checkpoint)` rather than generated. A generated
    id would make the resumed and the fresh build two different runs on any restart
    that raced the read above, and a deterministic one cannot: re-running the same
    build asks for the same identity and 0011's identity guard answers.
    """
    workspace_id, generation = fence
    with authorised(connection, mutations=False, ddl=False) as fenced:
        in_flight = fenced.execute(
            "SELECT r.run_id, r.target_epoch, r.source_checkpoint, r.state "
            "FROM omnivia_projection_runs r "
            "WHERE r.workspace_id = ? AND r.projection_id = ? "
            "  AND r.state IN ('running', 'succeeded') "
            "  AND NOT EXISTS (SELECT 1 FROM omnivia_projection_activations a "
            "                  WHERE a.workspace_id = r.workspace_id "
            "                    AND a.projection_id = r.projection_id "
            "                    AND a.run_id = r.run_id) "
            "ORDER BY r.target_epoch DESC LIMIT 1",
            (workspace_id, PROJECTION_ID),
        ).fetchone()
        ledger = fenced.execute(
            "SELECT active_epoch, active_source_checkpoint FROM omnivia_projection_ledger "
            "WHERE projection_id = ?",
            (PROJECTION_ID,),
        ).fetchone()

    if in_flight is not None and (
        str(in_flight[2]) == checkpoint or str(in_flight[3]) == "succeeded"
    ):
        return (
            str(in_flight[0]),
            int(in_flight[1]),
            str(in_flight[3]),
            str(in_flight[2]),
        )

    if in_flight is not None:
        with fenced_transaction(
            connection,
            identity,
            workspace_id=workspace_id,
            fencing_generation=generation,
        ):
            connection.execute(
                "UPDATE omnivia_projection_runs SET state = 'failed', "
                "finished_at_us = ?, error_json = ? "
                "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
                (
                    _next_us(connection, workspace_id, now_us),
                    _SUPERSEDED_SOURCE,
                    workspace_id,
                    PROJECTION_ID,
                    str(in_flight[0]),
                ),
            )

    active_epoch = int(ledger[0]) if ledger is not None and ledger[0] is not None else 0
    active_checkpoint = (
        str(ledger[1]) if ledger is not None and ledger[1] is not None else None
    )
    if in_flight is None and active_checkpoint == checkpoint:
        return None

    epoch = active_epoch + 1
    run_id = f"{PROJECTION_ID}-{epoch}-{checkpoint}"
    with fenced_transaction(
        connection, identity, workspace_id=workspace_id, fencing_generation=generation
    ):
        connection.execute(
            "INSERT INTO omnivia_projection_runs "
            "(workspace_id, projection_id, run_id, projection_kind, schema_version, "
            "profile_version, source_checkpoint, target_epoch, started_at_us, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')",
            (
                workspace_id,
                PROJECTION_ID,
                run_id,
                PROJECTION_KIND,
                SCHEMA_VERSION,
                PROFILE_VERSION,
                checkpoint,
                epoch,
                _next_us(connection, workspace_id, now_us),
            ),
        )
    return run_id, epoch, "running", checkpoint


def _completed_run_evidence(
    connection: sqlite3.Connection, workspace_id: str, run_id: str
) -> tuple[str, int]:
    """The digest and count a `succeeded` run already recorded, read back verbatim.

    Not recomputed. The run's validation attested to these numbers and the activation
    guard compares against them, so recomputing here would only introduce a second
    opinion that could disagree.
    """
    with authorised(connection, mutations=False, ddl=False) as fenced:
        row = fenced.execute(
            "SELECT build_digest, output_record_count FROM omnivia_projection_runs "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
            (workspace_id, PROJECTION_ID, run_id),
        ).fetchone()
    return str(row[0]), int(row[1])


def _append_documents(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    fence: tuple[str, int],
    run_id: str,
    checkpoint: str,
) -> None:
    """Append every artifact this run does not already carry, checkpointing as it goes.

    Resume position comes from the durable documents, not from the checkpoint cursor.
    Both are written in one transaction so they cannot disagree, and deriving from the
    rows means the digest a resumed build computes is a function of what is stored --
    which is the whole of why an interrupted build and an uninterrupted one agree.

    `search_text` is the artifact's identity surface, joined exactly as
    `repository._candidate` joins it. The projection is a different *index* over the
    same text, never a different text; `test_fts_projection_lifecycle.py` pins the two
    against each other rather than trusting the duplication.
    """
    workspace_id, generation = fence
    with authorised(connection, mutations=False, ddl=False) as fenced:
        existing = fenced.execute(
            f"SELECT evidence_id, search_text FROM {DOCUMENTS_TABLE} "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ? "
            "ORDER BY evidence_id ASC",
            (workspace_id, PROJECTION_ID, run_id),
        ).fetchall()

    hasher = hashlib.sha256()
    for evidence_id, search_text in existing:
        hasher.update(f"{evidence_id}\x1f{search_text}\x1e".encode())
    written = len(existing)
    after = str(existing[-1][0]) if existing else ""

    while True:
        with authorised(connection, mutations=False, ddl=False) as fenced:
            batch = fenced.execute(
                "SELECT evidence_id, source_kind, source_native_id, source_locator "
                "FROM omnivia_evidence_artifacts "
                "WHERE workspace_id = ? AND evidence_id > ? "
                "ORDER BY evidence_id ASC LIMIT ?",
                (workspace_id, after, CHECKPOINT_BATCH),
            ).fetchall()
        if not batch:
            return

        documents = [
            (
                str(evidence_id),
                " ".join(part for part in (kind, native_id, locator) if part),
            )
            for evidence_id, kind, native_id, locator in batch
        ]
        for evidence_id, search_text in documents:
            hasher.update(f"{evidence_id}\x1f{search_text}\x1e".encode())
        written += len(documents)
        after = documents[-1][0]

        with fenced_transaction(
            connection,
            identity,
            workspace_id=workspace_id,
            fencing_generation=generation,
        ):
            connection.executemany(
                f"INSERT INTO {DOCUMENTS_TABLE} "
                "(workspace_id, projection_id, run_id, evidence_id, search_text) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (workspace_id, PROJECTION_ID, run_id, evidence_id, search_text)
                    for evidence_id, search_text in documents
                ],
            )
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(checkpoint_sequence), -1) + 1 "
                    "FROM omnivia_projection_run_checkpoints "
                    "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
                    (workspace_id, PROJECTION_ID, run_id),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO omnivia_projection_run_checkpoints "
                "(workspace_id, projection_id, run_id, checkpoint_sequence, "
                "created_at_us, source_checkpoint, cursor_json, checkpoint_digest, "
                "input_record_count, output_record_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    PROJECTION_ID,
                    run_id,
                    sequence,
                    _next_us(connection, workspace_id, 0),
                    checkpoint,
                    json.dumps(
                        {"after_evidence_id": after},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    f"sha256:{hasher.hexdigest()}",
                    written,
                    written,
                ),
            )


def _validate_run(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    fence: tuple[str, int],
    run_id: str,
    now_us: int,
) -> tuple[str, int]:
    """Validate and close the run, deriving every recorded number from the durable rows.

    One transaction for the whole `running -> validating -> succeeded` sequence, on
    purpose: 0011 permits a durable `validating` state, and nothing this module does
    would benefit from being resumable *inside* validation. Keeping it atomic means the
    only in-flight state a restart can find is `running`, which is the state `_plan_run`
    knows how to resume.

    The digest is recomputed here rather than carried from `_append_documents`, so an
    interrupted-and-resumed build attests to exactly what an uninterrupted one would.
    """
    workspace_id, generation = fence
    with authorised(connection, mutations=False, ddl=False) as fenced:
        rows = fenced.execute(
            f"SELECT evidence_id, search_text FROM {DOCUMENTS_TABLE} "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ? "
            "ORDER BY evidence_id ASC",
            (workspace_id, PROJECTION_ID, run_id),
        ).fetchall()
    hasher = hashlib.sha256()
    for evidence_id, search_text in rows:
        hasher.update(f"{evidence_id}\x1f{search_text}\x1e".encode())
    digest = f"sha256:{hasher.hexdigest()}"
    count = len(rows)
    report = json.dumps(
        {"accepted": True, "documents": count},
        separators=(",", ":"),
        sort_keys=True,
    )
    validation_digest = "sha256:" + hashlib.sha256(
        f"{digest}\x1f{report}".encode()
    ).hexdigest()

    with fenced_transaction(
        connection, identity, workspace_id=workspace_id, fencing_generation=generation
    ):
        started = _next_us(connection, workspace_id, now_us)
        connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'validating', "
            "validation_started_at_us = ? "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
            (started, workspace_id, PROJECTION_ID, run_id),
        )
        connection.execute(
            "INSERT INTO omnivia_projection_validations "
            "(workspace_id, projection_id, run_id, validated_at_us, accepted, "
            "input_record_count, output_record_count, build_digest, report_json, "
            "validation_digest) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                PROJECTION_ID,
                run_id,
                started + 1,
                count,
                count,
                digest,
                report,
                validation_digest,
            ),
        )
        connection.execute(
            "UPDATE omnivia_projection_runs SET state = 'succeeded', "
            "finished_at_us = ?, input_record_count = ?, output_record_count = ?, "
            "build_digest = ? "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
            (started + 2, count, count, digest, workspace_id, PROJECTION_ID, run_id),
        )
    return digest, count


def _activate_run(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    fence: tuple[str, int],
    run_id: str,
    epoch: int,
    checkpoint: str,
    now_us: int,
) -> None:
    """Insert the activation row. The ledger pointer moves as its AFTER trigger's doing.

    This is the whole of activation from this module's side: one INSERT into
    `omnivia_projection_activations`. Superseding the previous run and advancing the
    ledger both happen inside 0011's `omnivia_apply_projection_activation`, in the same
    statement, so there is no window in which the pointer and the history disagree and
    no second write for a crash to land between.
    """
    workspace_id, generation = fence
    with authorised(connection, mutations=False, ddl=False) as fenced:
        row = fenced.execute(
            "SELECT build_digest, validation_digest FROM omnivia_projection_validations "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
            (workspace_id, PROJECTION_ID, run_id),
        ).fetchone()
    with fenced_transaction(
        connection, identity, workspace_id=workspace_id, fencing_generation=generation
    ):
        previous = connection.execute(
            "SELECT active_run_id FROM omnivia_projection_ledger WHERE projection_id = ?",
            (PROJECTION_ID,),
        ).fetchone()
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(activation_sequence), -1) + 1 "
                "FROM omnivia_projection_activations "
                "WHERE workspace_id = ? AND projection_id = ?",
                (workspace_id, PROJECTION_ID),
            ).fetchone()[0]
        )
        connection.execute(
            "INSERT INTO omnivia_projection_activations "
            "(workspace_id, projection_id, activation_sequence, run_id, target_epoch, "
            "previous_run_id, activated_at_us, source_checkpoint, build_digest, "
            "validation_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                PROJECTION_ID,
                sequence,
                run_id,
                epoch,
                previous[0] if previous is not None else None,
                _next_us(connection, workspace_id, now_us),
                checkpoint,
                str(row[0]),
                str(row[1]),
            ),
        )


def _reclaim(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    fence: tuple[str, int],
    keep_run_id: str,
) -> None:
    """Drop every run's documents except the one the ledger points at.

    Content, not evidence: the runs, checkpoints, validations and activations that
    record *what happened* are append-preserved and are never touched here. What goes
    is a failed run's partial rows and a superseded run's whole content, which is
    derived material the next build can reproduce and which would otherwise grow by a
    full copy of the workspace on every rebuild.

    0012's DELETE guard refuses to drop the active run's documents whatever this
    function passes, so the predicate below is the intent and the trigger is the proof.
    """
    workspace_id, generation = fence
    with fenced_transaction(
        connection, identity, workspace_id=workspace_id, fencing_generation=generation
    ):
        connection.execute(
            f"DELETE FROM {DOCUMENTS_TABLE} "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id <> ?",
            (workspace_id, PROJECTION_ID, keep_run_id),
        )


def _document_count(
    connection: sqlite3.Connection, workspace_id: str, run_id: str
) -> int:
    with authorised(connection, mutations=False, ddl=False) as fenced:
        row = fenced.execute(
            f"SELECT COUNT(*) FROM {DOCUMENTS_TABLE} "
            "WHERE workspace_id = ? AND projection_id = ? AND run_id = ?",
            (workspace_id, PROJECTION_ID, run_id),
        ).fetchone()
    return int(row[0])


def _next_us(connection: sqlite3.Connection, workspace_id: str, now_us: int) -> int:
    """A microsecond stamp at least one past everything this projection has recorded.

    0011 orders its evidence with `>=` comparisons across five tables, so a build whose
    clock reads earlier than a stamp already stored -- a resumed build after a restart,
    a test with a fixed clock -- would be refused by a trigger rather than accepted out
    of order. Taking the high-water mark from the database makes the sequence a fact
    about the stored evidence instead of a fact about the caller's clock.
    """
    with authorised(connection, mutations=False, ddl=False) as fenced:
        row = fenced.execute(
            "SELECT MAX(latest) FROM ("
            "  SELECT MAX(started_at_us) AS latest FROM omnivia_projection_runs "
            "  WHERE workspace_id = ? AND projection_id = ?"
            "  UNION ALL"
            "  SELECT MAX(validation_started_at_us) FROM omnivia_projection_runs "
            "  WHERE workspace_id = ? AND projection_id = ?"
            "  UNION ALL"
            "  SELECT MAX(finished_at_us) FROM omnivia_projection_runs "
            "  WHERE workspace_id = ? AND projection_id = ?"
            "  UNION ALL"
            "  SELECT MAX(created_at_us) FROM omnivia_projection_run_checkpoints "
            "  WHERE workspace_id = ? AND projection_id = ?"
            "  UNION ALL"
            "  SELECT MAX(activated_at_us) FROM omnivia_projection_activations "
            "  WHERE workspace_id = ? AND projection_id = ?"
            ")",
            (workspace_id, PROJECTION_ID) * 5,
        ).fetchone()
    latest = int(row[0]) if row is not None and row[0] is not None else 0
    return max(now_us, latest + 1)


__all__ = [
    "CHECKPOINT_BATCH",
    "DOCUMENTS_TABLE",
    "INDEX_TABLE",
    "MATCH_BATCH",
    "PROFILE_VERSION",
    "PROJECTION_ID",
    "PROJECTION_KIND",
    "SCHEMA_VERSION",
    "ActiveBuild",
    "BuildOutcome",
    "Fts5Unavailable",
    "ProjectionError",
    "ProjectionUnavailable",
    "SearchIndex",
    "StaleProjection",
    "active_build",
    "build_search_projection",
    "current_build",
    "open_search_index",
    "require_fts5",
    "session_search_index",
]
