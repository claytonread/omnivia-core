"""The `evidence.search` FTS5 projection: its builder, and the material reads narrow.

**What is durable and what is not, and why the split is not a compromise.**

Migration 0012 declares one ordinary table, `omnivia_evidence_search_documents`, and
that table is the projection. The FTS5 index over it is a `temp` virtual table this
module materialises from the activated run (`open_search_projection`). The split is
forced and it is the correct one:

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
database rather than from process state, and `open_search_projection` re-derives the
index from the activated run. Nothing a restart needs lives only in memory. The `temp`
schema is invisible to `sqlite_master`, so the index cannot move a schema fingerprint,
cannot appear in `guarded_tables()`, and cannot survive to be found stale.

**Reads never build.** Nothing on the request path materialises, repairs, re-creates or
re-tokenizes anything. Freshness is re-proven per request by `require_current` and
refuses -- `ProjectionUnavailable` when no run is activated, `StaleProjection` when the
activated run is behind by any amount at all. Packet §20.7 removed the middle option
(*"There is no configurable time tolerance in v0.6"*), so there is no threshold here and
no operator "rebuild now" entry point either: the only way this projection advances is
`build_search_projection`, which is maintenance work, not request work.

**This module is not the ranker, and that is the correction this design exists to make.**
The first version of Lane B put ranking in a `SearchIndex.search` that held the
connection its index lived on and scored with `bm25()`. Two things were wrong with it.
It gave up §20.12's proof, which is an *import boundary* -- a ranker in this module
imports `sqlite3`, `storage.connection` and `storage.repository`, so "it cannot reach an
unfiltered candidate" stopped being a structural fact and became a promise about the
body of one function. And `bm25()` takes its inverse document frequency and its average
document length from the *whole index*, so an artifact the filter chain had excluded
still moved the relative order of the members that were returned -- a §7.2 violation
that no assertion over a result page can see, because every id in the page is authorized
either way.

So the split. `open_search_projection` materialises the FTS5 index at startup **and
reads the per-document token material back out of it** through `fts5vocab`: the index is
the tokenizer, and what the session holds afterwards is one immutable token sequence per
document. `SearchProjection.project` then addresses that material by exactly the frozen
frontier's ids -- it never iterates the corpus, never touches an id the frontier does not
hold, and computes no score at all -- and returns a `ProjectedFrontier` value. Every
statistic BM25 needs is recomputed from that value by `retrieval.rank_projected`, which
holds no connection and imports no storage module. An excluded artifact is absent from
the numbers, not merely absent from the page.

A candidate the frontier holds and the material does not is a refusal, never a fallback.
The authoritative `search_text` is right there in `omnivia_evidence_artifacts` and
reading it would answer the request; it would also be answering from a source this
projection does not describe, which is the silent staleness §20.7 forbids wearing a
helpful face.

**Reads reach the material through `session_search_projection` and never materialise
any.** It is recorded on the connection whose `temp` schema the index lives in, so the
handler's lookup carries no identity, no fencing generation and no DDL grant: there is
nothing for a request path to build with even if a later edit wanted to.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

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
    ProjectedCandidate,
    ProjectedFrontier,
    normalize_query,
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
PROFILE_VERSION: Final = "fts5.unicode61.nodiacritics.1"

#: The tokenizer, stated once. `remove_diacritics 0` is the load-bearing half: with
#: removal on, `unicode61` folds combining marks away by its own internal table, and the
#: query tokenizer in `retrieval.py` -- which has no index to ask -- could not reproduce
#: that. With it off, both sides are "runs of Unicode alphanumerics over NFKC-normalized,
#: case-folded text", which is a rule two implementations can agree on and a test can pin.
TOKENIZER: Final = "unicode61 remove_diacritics 0"

#: The durable projection table, the session-scoped index over it, and the vocabulary
#: view this module reads the per-document token material back out of.
DOCUMENTS_TABLE: Final = "omnivia_evidence_search_documents"
INDEX_TABLE: Final = "omnivia_evidence_search_index"
VOCAB_TABLE: Final = "omnivia_evidence_search_vocabulary"

#: Documents written between checkpoints. Every checkpoint is a resume point, so this
#: is the bound on how much work an interruption can cost -- not a performance knob.
#: ponytail: fixed batch, make it a parameter if a workspace ever makes it matter.
CHECKPOINT_BATCH: Final = 256


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


#: The refusal a request gets when the frontier holds a candidate the materialised
#: projection does not. A constant because it names the rule and nothing about this
#: request: which id was missing is a fact about the caller's authorized material.
_MESSAGE_MISSING_MATERIAL: Final = (
    "the materialised evidence.search projection has no document for an authorized "
    "candidate; this read cannot be served from it"
)


@dataclass(frozen=True, slots=True)
class SearchProjection:
    """One session's materialised `evidence.search` projection, as token material.

    **The adapter, not the ranker.** What it holds is what the service's own startup
    materialised: one immutable token sequence per document, produced by the FTS5 index
    over the activated run's durable rows and read back out of it through `fts5vocab`.
    Holding the whole workspace's material is exactly what a projection is for, and it
    is why `project` is written the way it is -- the narrowing has to happen here, and it
    has to happen by address rather than by scan.

    There is no connection on this value. The `temp` index the material came from lives
    on one connection and nowhere else, but nothing after materialisation needs it: the
    material is values, the narrowing is a lookup, and the freshness re-check is
    `require_current`, which takes the connection as a parameter at the one call site
    that has one. So a reviewer asking "what can this reach?" can answer from the fields.
    """

    workspace_id: str
    build: ActiveBuild
    #: `evidence_id` to that document's tokens, in order. A `Mapping` rather than a
    #: `dict` because addressing it is the whole of `project`'s access to it, and a test
    #: substitutes a store that fails on iteration to prove that. What materialisation
    #: puts here is read-only in fact and not only in annotation -- see
    #: `_materialise_terms`.
    material: Mapping[str, tuple[str, ...]]
    document_count: int

    def project(self, frontier: AuthorizedFrontier) -> ProjectedFrontier:
        """Narrow this session's material to exactly the frozen frontier's ids.

        The loop is over `frontier.candidates` and the material is reached by
        `self.material[...]`. Both halves matter, and a test enforces them with a store
        that raises on iteration and on any id the frontier does not hold: this method
        never walks the corpus, never asks for an excluded id, and -- because the value
        it returns carries the tokens themselves rather than a way to fetch them -- there
        is no lazy lookup left to happen later, during ranking, out of sight.

        It computes nothing. No score, no frequency, no average, no ordering. Every one
        of those is `retrieval.rank_projected`'s, over the value returned here, so that
        the statistics are properties of the authorized set and not of the workspace.

        A candidate with no material is a refusal. It means the frontier and the
        activated run disagree about what exists -- material reclaimed underneath a
        session, a document the build never wrote -- and the honest answer is the
        retryable one the caller already handles. Reading the artifact's authoritative
        `search_text` instead would answer from outside the projection while every
        freshness check in the path reported current.
        """
        if frontier.workspace_id != self.workspace_id:
            raise ProjectionError(
                f"frontier names workspace {frontier.workspace_id!r} and this "
                f"projection serves {self.workspace_id!r}"
            )
        projected: list[ProjectedCandidate] = []
        for candidate in frontier.candidates:
            terms = self._terms(candidate.evidence_id)
            if terms is None:
                raise ProjectionUnavailable(_MESSAGE_MISSING_MATERIAL)
            projected.append(
                ProjectedCandidate(candidate=candidate, terms=terms)
            )
        return ProjectedFrontier(
            workspace_id=frontier.workspace_id,
            # Carried verbatim, never recomputed: this is the same set of candidates the
            # freeze digested, so a second digest here could only agree by accident.
            checksum=frontier.checksum,
            candidates=tuple(projected),
        )

    def _terms(self, evidence_id: str) -> tuple[str, ...] | None:
        """One document's tokens, or `None`. The only way this class reads its material.

        Separated so the refusal in `project` is raised with no active exception behind
        it -- this tree's convention, and load-bearing wherever a message could otherwise
        reach a caller through `__context__`.
        """
        try:
            return self.material[evidence_id]
        except KeyError:
            return None


def require_current(
    connection: sqlite3.Connection, projection: SearchProjection
) -> None:
    """Re-prove per request that this material is still the activated run's, or refuse.

    Per request rather than once at materialisation, because a projection that was
    current when the session opened is stale the moment an artifact is ingested, and a
    session that kept serving from it would be exactly the silent staleness §20.7
    forbids. Two things are proven: `current_build` refuses unless the activated run is
    level with the workspace, and the run it names must still be *this* material's run.

    A free function taking the connection rather than a method holding one, so that the
    value the ranking path carries stays a value.
    """
    current = current_build(connection, workspace_id=projection.workspace_id)
    if current.run_id != projection.build.run_id:
        raise StaleProjection(
            f"this material was built from run {projection.build.run_id!r} and "
            f"{PROJECTION_ID} has since activated {current.run_id!r}"
        )


def open_search_projection(
    connection: sqlite3.Connection, *, workspace_id: str
) -> SearchProjection:
    """Materialise this session's index and its token material, or refuse.

    Called from startup and maintenance, never from a request -- `service/main.py`
    invokes it behind `build_search_projection` before the endpoint accepts anything.
    It re-creates the `temp` tables from scratch rather than repairing them, because a
    rebuild from durable rows is both cheaper to reason about and the only version that
    converges after an interruption partway through a previous materialisation.

    **The FTS5 index is the tokenizer.** The material this returns is not a second
    analysis of the same text: the documents go into the index, and `_materialise_terms`
    reads the token instances back out of it. So what a request is ranked against is what
    FTS5 made of the workspace, and the only thing `retrieval.py` has to reproduce
    independently is the split of one query string -- which is a rule, not a corpus, and
    is pinned by a test against a real index.

    **The widening is three statements wide.** `temp` DDL is denied on a governed service
    connection like every other schema action -- `_schema_action_codes()` matches every
    `SQLITE_CREATE_*`, temporary and virtual-table actions included -- so the grant is
    stated here rather than assumed. It covers the drops and the creates and stops: those
    carry no data and no caller value, and the linear pass that does carry the
    workspace's text runs under a DML grant with DDL refused. Splitting them is not
    decoration -- an authorizer grant is only as narrow as the block it spans, and the
    insert loop is by far the longest-lived of the statements.

    `mutations=True` is needed for the creates as well, and that is FTS5 rather than
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

    # Cleared first. A failure below must not leave a previous session's material
    # reachable through `session_search_projection` behind a run that is no longer
    # active.
    connection.omnivia_search_projection = None  # type: ignore[attr-defined]
    with authorised(connection, mutations=True, ddl=True):
        # The vocabulary view goes first in both directions: it names the index, so it
        # cannot outlive a drop of one or precede a create of one.
        connection.execute(f"DROP TABLE IF EXISTS temp.{VOCAB_TABLE}")
        connection.execute(f"DROP TABLE IF EXISTS temp.{INDEX_TABLE}")
        connection.execute(
            f"CREATE VIRTUAL TABLE temp.{INDEX_TABLE} USING fts5("
            f"evidence_id UNINDEXED, search_text, tokenize = '{TOKENIZER}')"
        )
        connection.execute(
            f"CREATE VIRTUAL TABLE temp.{VOCAB_TABLE} "
            f"USING fts5vocab({INDEX_TABLE}, instance)"
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
    material = _materialise_terms(connection, documents)
    projection = SearchProjection(
        workspace_id=workspace_id,
        build=build,
        material=material,
        document_count=len(material),
    )
    connection.omnivia_search_projection = projection  # type: ignore[attr-defined]
    return projection


def _materialise_terms(
    connection: sqlite3.Connection, documents: Sequence[Any]
) -> Mapping[str, tuple[str, ...]]:
    """Every document's tokens, in order, read back out of the index that produced them.

    `fts5vocab(..., instance)` is one row per token occurrence -- term, document, column
    and position -- which is the per-document material and nothing more. It exposes no
    corpus statistic: no document frequency, no average length, no score. That is why it
    is the right thing to read here and `bm25()` is not.

    Seeded from `documents` rather than from the vocabulary alone, because a document
    whose identity surface is empty contributes no token instances and would otherwise be
    absent from the material entirely -- and 0012 permits an empty `search_text` by name.
    An authorized candidate that fell out here would be refused at every request, which
    is a correct-looking refusal for an incorrect reason.

    Returned behind a `MappingProxyType`, so `SearchProjection.material` being a
    `Mapping` is a fact about the object rather than a claim about the annotation. The
    session holds one of these for the life of the connection and every request ranks
    against it; a write through a surviving reference to the underlying dict would move
    what every later request is scored over, invisibly and behind a value the rest of
    this design proves immutable field by field.
    """
    tokens: dict[str, list[str]] = {
        str(evidence_id): [] for evidence_id, _ in documents
    }
    with authorised(connection, mutations=False, ddl=False) as fenced:
        identifiers = {
            int(rowid): str(evidence_id)
            for rowid, evidence_id in fenced.execute(
                f"SELECT rowid, evidence_id FROM temp.{INDEX_TABLE}"
            )
        }
        for document, term in fenced.execute(
            f"SELECT doc, term FROM temp.{VOCAB_TABLE} ORDER BY doc ASC, offset ASC"
        ):
            tokens[identifiers[int(document)]].append(str(term))
    return MappingProxyType(
        {evidence_id: tuple(sequence) for evidence_id, sequence in tokens.items()}
    )


def session_search_projection(
    connection: sqlite3.Connection,
) -> SearchProjection | None:
    """This session's material, or `None` when nothing has materialised any.

    Recorded on the connection because that is where the index it came from lives: the
    `temp` schema is connection-local, so the material and the connection have one
    lifetime and one scope, and a handle kept anywhere else could outlive the tables it
    was derived from. `storage/connection.py` already records the authorizer grant on the
    connection for the same reason, so this introduces no mechanism -- only one more
    connection-local fact.

    This is the read path's only way to reach the projection. It is a lookup and cannot
    become anything else: it holds no identity, no fencing generation and no DDL grant,
    so a request that reached for missing material gets `None` and a refusal rather than
    a build. Materialising is `open_search_projection`, and that is startup and
    maintenance work.
    """
    projection = getattr(connection, "omnivia_search_projection", None)
    return projection if isinstance(projection, SearchProjection) else None


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
    "PROFILE_VERSION",
    "PROJECTION_ID",
    "PROJECTION_KIND",
    "SCHEMA_VERSION",
    "TOKENIZER",
    "VOCAB_TABLE",
    "ActiveBuild",
    "BuildOutcome",
    "Fts5Unavailable",
    "ProjectionError",
    "ProjectionUnavailable",
    "SearchProjection",
    "StaleProjection",
    "active_build",
    "build_search_projection",
    "current_build",
    "open_search_projection",
    "require_current",
    "require_fts5",
    "session_search_projection",
]
