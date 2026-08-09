"""Sealed governed versions, resolved into one frozen frontier under a read-only fence.

0009 stores governed truth as append-only assemblies and refuses, deliberately, to carry
a currentness flag, a canonical pointer or any other column something would later have to
overwrite: *"'Which version is current' is a question a later, separately accepted
resolver answers by reading these facts, not a column this file lets anybody set."* This
module is that resolver, and nothing more than that.

**Read-only, structurally.** Every statement runs inside `authorised(connection,
mutations=False, ddl=False)`, so SQLite's own authorizer refuses a write here before it
reaches a row, exactly as `repository.py` establishes. There is no writer, no seal, no
migration and no cache in this module.

**One snapshot, not two.** The frontier is folded from two queries -- the sealed versions
and the sealed supersession edges -- and they only answer the same question if they see the
same database. Read in two separate autocommit statements they do not: an accepted writer
may seal a correction in between, and the fold then gets the *old* versions plus the *new*
edge retiring one of them, resolving a live record to nothing. That is the one failure
direction this module exists to prevent, arrived at from the other side. `resolve_governed_versions`
therefore opens one explicit read transaction and runs both queries inside it and inside one
authorizer fence, so either both corrections are visible or neither is. It commits only a
transaction it opened itself: a caller resolving inside its own transaction gets its own
snapshot back, unended.

**Only sealed rows exist.** Every read goes through
`omnivia_authoritative_governed_versions`, which joins the seal. A committed but unsealed
assembly is inert by construction rather than by a predicate this module remembers to
write -- 0009's own words: *"it is not in the view, it is not ancestry anything else may
name."*

**The three views are the contract's three views.** An absent selector resolves through
the contract's own `resolve_governed_record_view` to `current_canonical`, and an
unrecognized one is refused rather than silently widened into it. `candidates` is the
sealed candidate layer, `history` is what *was* canonical and had already been replaced at
the resolution instant, and `current_canonical` is the live, settled, citable version.

**Time is a parameter, never a clock.** `resolution_instant_us` is supplied by the caller,
so the same workspace resolves to the same frontier for the same instant forever. It is
one instant doing two jobs, and both are needed for the answer to be the one the workspace
actually held:

- *as of*: a version, and a supersession edge, whose `recorded_at_us` is after the
  resolution instant had not been written yet, so neither is visible. Without this a
  correction recorded later would answer a question asked earlier -- and, worse, a chain
  would fold past every version anything ever replaced straight to its newest tip, since
  the edges retiring the older ones would not be visible either;
- *valid at*: the half-open interval `[valid_from_us, valid_to_us)` must contain the
  instant. Closed at the top, so two abutting versions of one record cover the timeline
  without ever both being current.

Supersession is folded from the edges rather than read off a version, and only *sealed*
edges count -- and an edge counts only from the instant its sealed target assembly is
visible too, because a replacement that has not been recorded yet cannot have replaced
anything. The earliest edge out of a version is the one that ends it, so a chain folds to
the one version nothing visible has replaced yet.

**Ties are broken, not left to SQLite.** 0009 permits two sealed canonical versions of one
record -- it enforces the supersession *edge*, not the absence of a second frontier
version -- and two versions written in one transaction carry the same `recorded_at_us`.
Natural row order is not stable across index changes (packet §8.2), so every query states
a total `ORDER BY` and the fold picks its winner by an explicit key rather than by
whichever row arrived last.

**The frontier is a value.** A tuple of frozen slotted dataclasses: nothing a later
ranking, selection or projection step can mutate, and no handle it could read more rows
through.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from typing import Final

from omnivia_core.contracts.v1 import (
    GOVERNANCE_STATE_ACCEPTED,
    GOVERNED_RECORD_VIEW_CANDIDATES,
    GOVERNED_RECORD_VIEW_CURRENT_CANONICAL,
    GOVERNED_RECORD_VIEW_HISTORY,
    GOVERNED_RECORD_VIEWS,
    KNOWLEDGE_SEARCH_CANONICAL_AUTHORITY_LEVEL,
    resolve_governed_record_view,
)
from omnivia_core_runtime.storage.connection import authorised

#: 0009's two sealable layers, spelled as its `layer` column spells them. `context_model`
#: is the third value the column admits and is refused a seal outright, so it can never
#: reach this module and is not named here.
LAYER_GOVERNED: Final = "governed"
LAYER_CANDIDATE: Final = "candidate"

#: 0009's fixed authority semantics, as a rank rather than a string comparison. The three
#: numbers are the generated `authority_rank` column's own `CASE`, restated here because
#: the fold below orders by them and reading the generated column would make this module
#: depend on a value it cannot verify was generated.
_AUTHORITY_RANK: Final[dict[str, int]] = {"proposed": 0, "reviewed": 100, "canonical": 200}

_VIEW: Final = "omnivia_authoritative_governed_versions"
_SUPERSESSIONS: Final = "omnivia_record_supersessions"
_SEALS: Final = "omnivia_governed_version_seals"
_EVENTS: Final = "omnivia_governed_provenance_events"


@dataclass(frozen=True, slots=True)
class GovernedVersion:
    """One sealed governed version, exactly as `omnivia_authoritative_governed_versions`
    projects it.

    The field order *is* the select list (see `_COLUMNS`), so a column renamed or dropped
    in the view fails this module's query rather than silently shifting values into the
    wrong attributes.
    """

    workspace_id: str
    assembly_id: str
    seal_id: str
    governed_record_id: str
    governed_record_version_id: str
    record_type: str
    domain_scope: str
    layer: str
    governance_disposition: str | None
    authority_level: str
    decision_source_kind: str | None
    decision_source_id: str | None
    content_schema_version: str
    content_json: str
    content_digest: str
    evidence_disposition: str
    valid_from_us: int
    valid_to_us: int | None
    recorded_at_us: int
    append_ordinal: int
    correlation_kind: str
    correlation_id: str
    audit_ref: str | None
    sealed_at_us: int


_COLUMNS: Final[tuple[str, ...]] = tuple(field.name for field in fields(GovernedVersion))


@dataclass(frozen=True, slots=True)
class GovernedSupersession:
    """One sealed supersession, at the instant it actually took effect.

    `effective_at_us` is the later of the edge's own `recorded_at_us` and that of the
    sealed target assembly it names -- the instant by which *both* halves of the
    replacement had been written -- not either row's timestamp on its own. `reason_code`
    is whatever the edge's provenance event recorded, and is `None` when the event
    recorded none.
    """

    workspace_id: str
    governed_record_id: str
    source_version_id: str
    target_version_id: str
    assembly_id: str
    effective_at_us: int
    reason_code: str | None


def read_governed_supersessions(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
) -> tuple[GovernedSupersession, ...]:
    """One workspace's sealed supersessions that had taken effect at `resolution_instant_us`.

    The seal join is the whole point of reading the edge table directly rather than
    trusting it: `omnivia_record_supersessions` carries no seal of its own, so an unsealed
    correction sitting in the table would otherwise retire a version that is still the
    workspace's live answer -- the one direction of error that loses canonical knowledge
    rather than merely showing too little.

    The target join is the same argument one step further out. A supersession is a
    *replacement*, so it has happened only once the thing doing the replacing is itself
    visible -- and 0009's seal trigger ties the edge to its target assembly without ever
    tying their two `recorded_at_us` together, so an edge recorded before the target
    assembly it names is a shape the accepted writer permits. Reading the edge alone would
    then retire the source at the edge's instant while the replacement was still invisible,
    and the record would resolve to *nothing* for the gap between them. Joining the
    authoritative view on the exact target assembly -- workspace, `target_version_id` *and*
    `assembly_id`, which 0009's seal trigger requires to be the assembly carrying the edge
    -- buys both facts at once: that exact target assembly is sealed, and it was recorded at
    `t.recorded_at_us`. Matching on the version id alone would identify the target by a
    column the view does not key on, so the join would say "some sealed assembly for this
    version" where the effective instant needs *this* one.

    The effective instant is therefore the later of the two, because transaction time asks
    what the workspace *held*, and it held the source right up until its replacement was
    there to take over. An unjoined edge (target not sealed) drops out entirely, leaving the
    source current: too little supersession rather than lost knowledge, the same direction
    the seal join takes. The provenance join is a *left* join for the opposite reason:
    `reason_code` is annotation, and a missing or unlabelled event must not delete a
    supersession that did happen.

    Facts effective after `resolution_instant_us` are not returned at all -- a correction
    recorded later must not answer a question asked earlier.

    One query, so one statement's implicit snapshot is all this needs; the fence is opened
    here and the query itself lives in `_read_supersession_facts`, which
    `resolve_governed_versions` calls directly under the fence and transaction it already
    owns. Any transaction the caller had open is left exactly as it was found.
    """
    with authorised(connection, mutations=False, ddl=False) as fenced:
        return _read_supersession_facts(
            fenced,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
        )


def _read_supersession_facts(
    fenced: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
) -> tuple[GovernedSupersession, ...]:
    """`read_governed_supersessions`' query, on a connection already fenced by the caller.

    Split out so the resolver can read the edges inside the *same* authorizer fence and the
    same read transaction as the versions, rather than through a public entry point that
    would open a second fence around a second snapshot. It opens neither, on purpose: a
    reader that fenced itself here could not be composed into a caller's fence without
    nesting one, and a reader that began its own transaction could not be composed at all.

    The rows are materialised into frozen dataclasses before returning, so nothing the fold
    holds afterwards is a cursor the connection could still be read through.
    """
    rows = fenced.execute(
        "SELECT r.workspace_id, r.governed_record_id, r.source_version_id, "
        "       r.target_version_id, r.assembly_id, "
        "       MAX(r.recorded_at_us, t.recorded_at_us), e.reason_code "
        f"FROM {_SUPERSESSIONS} r "
        f"JOIN {_SEALS} s ON s.workspace_id = r.workspace_id "
        "                AND s.assembly_id = r.assembly_id "
        f"JOIN {_VIEW} t ON t.workspace_id = r.workspace_id "
        "               AND t.assembly_id = r.assembly_id "
        "               AND t.governed_record_version_id = r.target_version_id "
        f"LEFT JOIN {_EVENTS} e ON e.workspace_id = r.workspace_id "
        "                      AND e.provenance_event_id = r.provenance_event_id "
        "WHERE r.workspace_id = ? "
        "  AND MAX(r.recorded_at_us, t.recorded_at_us) <= ? "
        "ORDER BY r.source_version_id ASC, r.target_version_id ASC, "
        "         r.assembly_id ASC",
        (workspace_id, resolution_instant_us),
    ).fetchall()
    return tuple(GovernedSupersession(*row) for row in rows)


def resolve_governed_versions(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
    view: str | None = None,
) -> tuple[GovernedVersion, ...]:
    """The sealed versions one workspace's `view` holds at `resolution_instant_us`.

    An absent `view` is `current_canonical`; `candidates` and `history` are reachable only
    by naming them exactly, and an unrecognized selector raises rather than defaulting into
    the canonical view -- the same fail-closed direction the contract's own input
    validation takes, for the same reason: an unrecognized value could mean a wider view
    than the caller intended.

    Every view answers *as of* `resolution_instant_us`: nothing recorded after it is
    visible under any of the three, and `current_canonical` additionally requires the
    instant to fall inside the version's validity window.

    Both queries run inside one read transaction, so the versions and the edges retiring
    them are read from one database state. An empty workspace, an unknown workspace and a
    workspace whose every version has been superseded all resolve to `()`. None of them is
    an error here.
    """
    resolved = resolve_governed_record_view(view)
    if resolved not in GOVERNED_RECORD_VIEWS:
        raise ValueError(
            f"view {view!r} is not a recognized GovernedRecordView; must be one of "
            f"{sorted(GOVERNED_RECORD_VIEWS)!r} or absent"
        )

    # Deferred, because this reads: the snapshot opens at the first SELECT and is held
    # until this function ends it, which is exactly the span the two queries have to share.
    # BEGIN, COMMIT and ROLLBACK sit outside the fence -- transaction control is not one of
    # the statement classes `authorised` widens, and the fence is about what may touch a
    # row, not about who owns the transaction the rows are read in.
    #
    # `owns_transaction` is the whole ownership rule: a caller resolving inside its own
    # transaction already has a snapshot and wants its transaction back afterwards, so this
    # function neither begins, commits nor rolls back one it did not open. Only the fence is
    # unconditional, because both reads must share it either way.
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        with authorised(connection, mutations=False, ddl=False) as fenced:
            rows = fenced.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM {_VIEW} "
                "WHERE workspace_id = ? AND recorded_at_us <= ? "
                "ORDER BY governed_record_id ASC, recorded_at_us ASC, append_ordinal ASC, "
                "assembly_id ASC",
                (workspace_id, resolution_instant_us),
            ).fetchall()
            # The supersession rule is not restated here: the seal join, the exact-target
            # join and the effective instant are one rule about what counts as a
            # replacement, and a second copy of that rule is a second place for it to
            # drift. `read_governed_supersessions`' query is reached through its internal
            # reader rather than through the public function, because the public one would
            # open a second fence around a second statement -- and under an already-open
            # transaction that second statement would still be a second *query*, which is
            # only safe here by accident of this transaction, not by anything it states.
            facts = _read_supersession_facts(
                fenced,
                workspace_id=workspace_id,
                resolution_instant_us=resolution_instant_us,
            )
    except BaseException:
        if owns_transaction:
            connection.execute("ROLLBACK")
        raise
    if owns_transaction:
        connection.execute("COMMIT")

    # That reader already drops anything effective after the instant, so a version is
    # replaced exactly when some returned fact names it -- and the *earliest* such fact is
    # the one that ended it. 0009 refuses a branching source, so today there is at most one;
    # the `min` states the rule the fold depends on rather than relying on that refusal
    # holding forever.
    replaced_at: dict[str, int] = {}
    for fact in facts:
        ended_at = replaced_at.get(fact.source_version_id)
        if ended_at is None or fact.effective_at_us < ended_at:
            replaced_at[fact.source_version_id] = fact.effective_at_us

    versions = tuple(GovernedVersion(*row) for row in rows)

    if resolved == GOVERNED_RECORD_VIEW_CANDIDATES:
        return tuple(version for version in versions if version.layer == LAYER_CANDIDATE)

    canonical = tuple(version for version in versions if _is_canonical(version))

    if resolved == GOVERNED_RECORD_VIEW_HISTORY:
        return tuple(
            version
            for version in canonical
            if _replaced_by(version, replaced_at, resolution_instant_us)
        )

    assert resolved == GOVERNED_RECORD_VIEW_CURRENT_CANONICAL
    frontier: dict[str, GovernedVersion] = {}
    for version in canonical:
        if _replaced_by(version, replaced_at, resolution_instant_us):
            continue
        if not _valid_at(version, resolution_instant_us):
            continue
        incumbent = frontier.get(version.governed_record_id)
        if incumbent is None or _precedence(version) > _precedence(incumbent):
            frontier[version.governed_record_id] = version
    return tuple(frontier[record_id] for record_id in sorted(frontier))


def _is_canonical(version: GovernedVersion) -> bool:
    """Whether `version` is settled, citable knowledge on all three axes at once.

    0009's own CHECK already ties `canonical` to a governed, accepted assembly, so two of
    these three are unreachable through the accepted write path. They are stated anyway,
    because this module is what decides what a caller may treat as canonical and a
    predicate that depends on a constraint in another file is a predicate that loosens
    when that file does.
    """
    return (
        version.layer == LAYER_GOVERNED
        and version.governance_disposition == GOVERNANCE_STATE_ACCEPTED
        and version.authority_level == KNOWLEDGE_SEARCH_CANONICAL_AUTHORITY_LEVEL
    )


def _valid_at(version: GovernedVersion, instant_us: int) -> bool:
    """Whether `[valid_from_us, valid_to_us)` contains `instant_us`; an absent upper bound
    is unbounded. Half-open at the top: a window closing exactly at the instant is closed."""
    return version.valid_from_us <= instant_us and (
        version.valid_to_us is None or instant_us < version.valid_to_us
    )


def _replaced_by(
    version: GovernedVersion, replaced_at: dict[str, int], instant_us: int
) -> bool:
    """Whether `version` had already been superseded at `instant_us`.

    `replaced_at` holds the *effective* replacement instant -- the later of the edge and
    its sealed target assembly -- so a target recorded after its own edge cannot retire
    this version before the replacement was visible.

    Equality counts as replaced: a version superseded at exactly the resolution instant is
    history at it, which is the same boundary the contract's `history` view applies to
    `superseded_at`.
    """
    ended_at = replaced_at.get(version.governed_record_version_id)
    return ended_at is not None and ended_at <= instant_us


def _precedence(version: GovernedVersion) -> tuple[int, int, str, str, int, str]:
    """The total order the fold picks a record's one frontier version by.

    Authority first, then recorded time -- and then the correlation *stream* before that
    stream's ordinal. 0009 scopes `append_ordinal` to the correlation parent and says
    outright what follows from it: *"append ordinals from different correlation parents
    are not comparable at all"*, because *"they were never counting the same thing."*
    Comparing them across streams would let one stream's local counter decide which
    stream wins, so a version's rank would move with a number written under an unrelated
    audit. `(correlation_kind, correlation_id)` therefore selects one stream first, and
    the ordinal only ever separates versions inside the stream it selected -- which is
    exactly the comparison 0009's unique index makes total.

    The last three exist because the first two do not separate two versions written in
    one transaction; `assembly_id` closes the key and is unique per workspace, so the
    same rows resolve to the same winner on every run and on every index layout.
    """
    return (
        _AUTHORITY_RANK.get(version.authority_level, -1),
        version.recorded_at_us,
        version.correlation_kind,
        version.correlation_id,
        version.append_ordinal,
        version.assembly_id,
    )


__all__ = [
    "LAYER_CANDIDATE",
    "LAYER_GOVERNED",
    "GovernedSupersession",
    "GovernedVersion",
    "read_governed_supersessions",
    "resolve_governed_versions",
]
