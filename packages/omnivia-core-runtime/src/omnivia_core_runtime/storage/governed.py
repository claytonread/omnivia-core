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

import json
import sqlite3
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    GOVERNANCE_LAYER_CANDIDATE,
    GOVERNANCE_LAYER_GOVERNED,
    GOVERNANCE_STATE_ACCEPTED,
    GOVERNANCE_STATE_CANDIDATE,
    GOVERNED_RECORD_VIEW_CANDIDATES,
    GOVERNED_RECORD_VIEW_CURRENT_CANONICAL,
    GOVERNED_RECORD_VIEW_HISTORY,
    GOVERNED_RECORD_VIEWS,
    KNOWLEDGE_SEARCH_CANONICAL_AUTHORITY_LEVEL,
    EvidenceReference,
    GovernedRecord,
    ProvenanceEntry,
    RecordIdentity,
    RecordProvenance,
    RecordTemporalMetadata,
    SourceReference,
    SourceSpan,
    SupersessionReference,
    resolve_governed_record_view,
    validate_governed_record,
    validate_record_temporal_metadata,
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
_LINKS: Final = "omnivia_governed_version_evidence_links"
_ARTIFACTS: Final = "omnivia_evidence_artifacts"
_SPANS: Final = "omnivia_normalized_source_spans"


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


@dataclass(frozen=True, slots=True)
class _GovernedProvenanceEvent:
    """One governed provenance event, as 0009 recorded it.

    Only the columns a governed record's own provenance is answered from: who acted, under
    what action and policy, when, why, and what it said about its evidence. `recorded_at_us`
    and the correlation columns are deliberately absent -- those are the write path's
    bookkeeping, not the record's account of itself.

    The field order *is* the select list, exactly as `GovernedVersion`'s is.
    """

    workspace_id: str
    assembly_id: str
    governed_record_version_id: str
    provenance_event_id: str
    provenance_sequence: int
    action: str
    actor_id: str | None
    actor_kind: str | None
    policy_id: str | None
    occurred_at_us: int
    reason_code: str | None
    reason_comment: str | None
    evidence_disposition: str


@dataclass(frozen=True, slots=True)
class _GovernedEvidenceLink:
    """One evidence link, joined to the M2 artifact it cites and, if it names one, the exact
    normalized span inside it.

    The artifact join is inner: 0009's own foreign key makes a link without its artifact
    unrepresentable, so a row that failed to join would mean the database had already lost
    the thing being cited, and publishing a citation with no source behind it is the one
    direction that misleads. The span join is *left*: `normalized_span_id` is nullable, and a
    link that cites a whole artifact rather than a span is an ordinary, complete citation.

    `governed_record_version_id` and `provenance_sequence` come from the event, which is the
    only place the link table records them -- it keys on the assembly and the event, not on
    the version.
    """

    workspace_id: str
    assembly_id: str
    governed_record_version_id: str
    provenance_event_id: str
    provenance_sequence: int
    link_ordinal: int
    evidence_id: str
    source_kind: str
    source_native_id: str
    source_locator: str | None
    source_retrieved_at_us: int | None
    ingested_at_us: int
    event_at_us: int | None
    observed_at_us: int | None
    normalized_record_id: str | None
    normalized_span_id: str | None
    span_pointer: str | None
    span_start_offset: int | None
    span_end_offset: int | None


@dataclass(frozen=True, slots=True)
class _GovernedHydrationSnapshot:
    """One workspace's resolved frontier and everything a projection of it needs, read from
    one database state.

    Four tuples of frozen values and nothing else: no connection, no cursor and no callback,
    so a later mapping step cannot reach back into the database and read a fifth fact from a
    state the other four never saw.
    """

    versions: tuple[GovernedVersion, ...]
    supersessions: tuple[GovernedSupersession, ...]
    events: tuple[_GovernedProvenanceEvent, ...]
    links: tuple[_GovernedEvidenceLink, ...]


def _read_hydration_support(
    fenced: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
) -> tuple[
    tuple[GovernedSupersession, ...],
    tuple[_GovernedProvenanceEvent, ...],
    tuple[_GovernedEvidenceLink, ...],
]:
    """Every support fact behind a resolved frontier, on a connection the caller has already
    fenced and already opened a read transaction on.

    One function rather than three, because these three queries are one boundary: the caller
    resolves versions and then, without giving the database a chance to move underneath it,
    reads everything those versions are described by. Split apart they would be three places
    a later edit could put a statement outside the caller's transaction.

    The supersession facts are `_read_supersession_facts`', unchanged -- what the seal join,
    the exact-target join and the effective instant mean is settled there and is not restated
    here.

    The two remaining queries read the *whole* workspace rather than the selected assemblies:
    they are keyed on `workspace_id` alone, so the row count is the workspace's, and the
    caller filters. That is on purpose. Filtering in SQL would mean interpolating an
    assembly-id list into these statements, and the selection is already a value the caller
    holds; a Python filter over materialised rows cannot get the predicate subtly different
    from the one the resolver applied, and cannot be got wrong by an empty list.

    Both queries state a total `ORDER BY`, for the same reason every other query in this
    module does: natural row order is not stable across index changes, and a projection built
    from these tuples would then reorder a record's own provenance between runs.
    """
    facts = _read_supersession_facts(
        fenced,
        workspace_id=workspace_id,
        resolution_instant_us=resolution_instant_us,
    )
    event_rows = fenced.execute(
        "SELECT workspace_id, assembly_id, governed_record_version_id, "
        "       provenance_event_id, provenance_sequence, action, actor_id, actor_kind, "
        "       policy_id, occurred_at_us, reason_code, reason_comment, "
        "       evidence_disposition "
        f"FROM {_EVENTS} "
        "WHERE workspace_id = ? "
        "ORDER BY assembly_id ASC, provenance_sequence ASC, provenance_event_id ASC",
        (workspace_id,),
    ).fetchall()
    # Every join carries `workspace_id`. `omnivia_evidence_artifacts` keys on `evidence_id`
    # alone and the span table on `normalized_span_id` alone, so joining on those columns by
    # themselves would still return exactly one row each -- and that row could belong to
    # another workspace, which is how a citation crosses a tenancy boundary while looking
    # perfectly well-formed. The predicate is the guard, not the key shape.
    link_rows = fenced.execute(
        "SELECT l.workspace_id, l.assembly_id, e.governed_record_version_id, "
        "       l.provenance_event_id, e.provenance_sequence, l.link_ordinal, "
        "       l.evidence_id, a.source_kind, a.source_native_id, a.source_locator, "
        "       a.source_retrieved_at_us, a.ingested_at_us, a.event_at_us, "
        "       a.observed_at_us, l.normalized_record_id, l.normalized_span_id, "
        "       s.span_pointer, s.span_start_offset, s.span_end_offset "
        f"FROM {_LINKS} l "
        f"JOIN {_EVENTS} e ON e.workspace_id = l.workspace_id "
        "               AND e.assembly_id = l.assembly_id "
        "               AND e.provenance_event_id = l.provenance_event_id "
        f"JOIN {_ARTIFACTS} a ON a.workspace_id = l.workspace_id "
        "                   AND a.evidence_id = l.evidence_id "
        f"LEFT JOIN {_SPANS} s ON s.workspace_id = l.workspace_id "
        "                    AND s.evidence_id = l.evidence_id "
        "                    AND s.normalized_record_id = l.normalized_record_id "
        "                    AND s.normalized_span_id = l.normalized_span_id "
        "WHERE l.workspace_id = ? "
        "ORDER BY l.assembly_id ASC, e.provenance_sequence ASC, "
        "         l.provenance_event_id ASC, l.link_ordinal ASC, l.evidence_id ASC",
        (workspace_id,),
    ).fetchall()
    return (
        facts,
        tuple(_GovernedProvenanceEvent(*row) for row in event_rows),
        tuple(_GovernedEvidenceLink(*row) for row in link_rows),
    )


def _read_governed_hydration_snapshot(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
    view: str | None = None,
) -> _GovernedHydrationSnapshot:
    """One workspace's `view` at `resolution_instant_us`, with the support facts describing it.

    The coherence argument `resolve_governed_versions` makes about its two queries is the
    same one this makes about five, one step out: a projection assembled from a frontier read
    at one instant and provenance read at another can show a version alongside the events of
    the correction that replaced it -- an account of the record that was never true. So the
    transaction opens here and every read happens inside it, the resolver included, which is
    why it is called on this connection rather than a fresh one: it declines to end a
    transaction it did not open, so the snapshot it reads under is this one.

    Ownership is the same rule for the same reason. A caller already in a transaction is
    reading this as one fact among several and gets its transaction back untouched; only a
    transaction begun here is committed or rolled back here.

    Events and links are filtered to the assemblies the resolved versions actually name, and
    the filter is the point rather than an optimisation: the three views select different
    assemblies, so unfiltered support rows would let a `candidates` snapshot carry the
    provenance of the governed versions it deliberately does not show, and a `history`
    snapshot carry the current one's. The versions would be right and the account of them
    would not be.

    The supersession facts are scoped too, but by *version id* and on either end: a fact is
    kept when the view selected its source or its target. An edge is a statement about two
    versions and a view need not hold both of them -- the incoming edge explaining why a
    current version is current names a source `current_canonical` does not contain, and the
    outgoing edge explaining why the newest historical version is history names a target
    `history` does not contain. (A view can hold both ends: `history` holds both ends of an
    earlier edge, one whose source it retired and whose target it also retired in turn.)
    Filtering these by selected assembly, as events and links are, would delete
    exactly the edges that explain why the frontier looks the way it does; not filtering them
    at all would hand a `candidates` snapshot the governed layer's whole correction chain,
    which is the same wrong-account-of-the-right-versions failure the other filter exists to
    prevent.
    """
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        versions = resolve_governed_versions(
            connection,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
            view=view,
        )
        with authorised(connection, mutations=False, ddl=False) as fenced:
            facts, events, links = _read_hydration_support(
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

    selected = {version.assembly_id for version in versions}
    selected_versions = {version.governed_record_version_id for version in versions}
    return _GovernedHydrationSnapshot(
        versions=versions,
        supersessions=tuple(
            fact
            for fact in facts
            if fact.source_version_id in selected_versions
            or fact.target_version_id in selected_versions
        ),
        events=tuple(event for event in events if event.assembly_id in selected),
        links=tuple(link for link in links if link.assembly_id in selected),
    )


def _timestamp(microseconds: int) -> str:
    """One microsecond instant as the contract's `Timestamp`.

    Millisecond precision with a `Z` suffix, sub-millisecond digits truncated rather than
    rounded -- the same rendering `repository.py` already emits, restated here rather than
    imported because that module is the evidence read path and this one is the governed
    read path; a shared import would tie them together for nothing more than eight lines.
    """
    moment = datetime.fromtimestamp(microseconds / 1_000_000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _temporal_metadata(version: GovernedVersion) -> RecordTemporalMetadata:
    """`version`'s own instants, as the contract's `RecordTemporalMetadata`.

    Amendment 008's mapping, and only it. `recorded_at` is the version's `recorded_at_us`:
    the authoritative instant this governed version's assembly was persisted. `ingested_at`
    is required by the contract, but 0009 stores no distinct governed-record ingestion
    instant, so it reuses `recorded_at_us` as the explicit V06-3 read-model compatibility
    projection Amendment 008 approves -- not a claim that no ingestion occurred, and not a
    claim that ingestion and persistence were the same real event. Reaching instead for an
    evidence artifact's `ingested_at_us` or a provenance event's `occurred_at_us` is
    forbidden: those are a different record's clock answering this record's question, and
    would make `ingested_at` move when a citation was added.

    `valid_from`/`valid_until` are the version's assertion window verbatim; an absent
    `valid_to_us` is an open-ended window and stays *absent* rather than becoming a far-future
    instant the database never recorded.

    `event_at`, `observed_at` and `superseded_at` are optional and remain absent here because
    the exact facts they require are not inputs to this helper: the first two are facts about
    the world behind the record, and `superseded_at` is a fact about a supersession edge. A
    later assembly step holding those facts fills them in; guessing them from what is
    reachable here would publish an account of the record that no row asserts.

    Pure: one frozen value out, no clock, no connection and nothing read that is not a field
    of `version`. The result is validated before it is returned, so a version whose stored
    window is inverted fails here rather than downstream of whatever publishes it.
    """
    temporal = RecordTemporalMetadata(
        ingested_at=_timestamp(version.recorded_at_us),
        recorded_at=_timestamp(version.recorded_at_us),
        valid_from=_timestamp(version.valid_from_us),
        valid_until=(
            None if version.valid_to_us is None else _timestamp(version.valid_to_us)
        ),
    )
    validate_record_temporal_metadata(temporal)
    return temporal


#: 0009's two sealable `layer` values as the contract's own `GovernanceLayer` codes. The
#: third value the column admits, `context_model`, is refused a seal outright and so is not
#: mapped: an unmapped layer reaching the hydrator is a stored fact this module refuses to
#: publish rather than one it guesses a wire code for.
_GOVERNANCE_LAYER: Final[dict[str, str]] = {
    LAYER_GOVERNED: GOVERNANCE_LAYER_GOVERNED,
    LAYER_CANDIDATE: GOVERNANCE_LAYER_CANDIDATE,
}

_CURRENTNESS_CURRENT: Final = "current"
_CURRENTNESS_SUPERSEDED: Final = "superseded"


def _content(version: GovernedVersion) -> dict[str, Any]:
    """`version`'s stored canonical content, decoded.

    0009 bounds `content_json` by byte length and forbids NULs but deliberately never calls
    SQLite's `json(...)`, so "this column holds a JSON object" is an invariant the write
    path asserts and this read path cannot assume. A row that fails to decode, or decodes to
    a JSON value that is not an object, is a stored fact this module refuses rather than
    repairs: publishing `{}` in its place -- the degradation `repository.py` chooses for an
    artifact's *opaque decoration* -- would here be publishing a governed claim as though it
    said nothing.
    """
    decoded: object
    try:
        decoded = json.loads(version.content_json)
    except ValueError as error:
        raise ValueError(
            f"governed version {version.governed_record_version_id!r} stores content_json "
            f"that is not decodable JSON"
        ) from error
    if isinstance(decoded, dict):
        return decoded
    raise ValueError(
        f"governed version {version.governed_record_version_id!r} stores content_json "
        f"that is not a JSON object"
    )


def _source_reference(link: _GovernedEvidenceLink) -> SourceReference:
    """The M2 artifact behind one evidence link, as the contract's `SourceReference`.

    The artifact's own record of where it came from, verbatim -- the same four columns
    `repository.py` renders an `EvidenceArtifact.source` from, so one artifact cited by a
    governed record and returned by an evidence search describes itself identically.
    """
    return SourceReference(
        kind=link.source_kind,
        source_id=link.source_native_id,
        locator=link.source_locator,
        retrieved_at=(
            None
            if link.source_retrieved_at_us is None
            else _timestamp(link.source_retrieved_at_us)
        ),
    )


def _evidence_reference(link: _GovernedEvidenceLink) -> EvidenceReference:
    """One evidence link as the contract's `EvidenceReference`.

    The span is present exactly when the link named one and the join found it. `excerpt` is
    absent always: no column stores one, and quoting content back out of the artifact would
    be this module inventing the excerpt rather than reading it.
    """
    return EvidenceReference(
        source=_source_reference(link),
        span=(
            None
            if link.span_pointer is None
            else SourceSpan(
                pointer=link.span_pointer,
                start_offset=link.span_start_offset,
                end_offset=link.span_end_offset,
            )
        ),
    )


def _provenance_entry(
    event: _GovernedProvenanceEvent,
    version: GovernedVersion,
    evidence: tuple[EvidenceReference, ...],
) -> ProvenanceEntry:
    """One stored provenance event as one `ProvenanceEntry`.

    `actor_id`/`actor_kind` are required by the contract and nullable in 0009, because 0009
    admits a policy evaluation as well as a person -- and then requires exactly one of the
    two shapes: an event carries `actor_id`, `actor_kind` and `actor_role`, *or* it carries
    `policy_id` and `policy_version` and none of those three. An actor event publishes both
    of its own columns; half of that pair is an internally incomplete row and is refused
    rather than completed from somewhere else.

    A policy event's durable actor identity is its exact `policy_id`, and its kind is the
    *version's* exact stored `decision_source_kind` -- the only row that records what kind
    of decision source this assembly was governed by. 0009's own trigger ties the two
    together, requiring a `policy_evaluator` assembly's events to carry
    `policy_id = decision_source_id`, so both halves of that tie are checked here and a
    policy event whose version records a different source, a different kind, or no decision
    source at all is refused. Nothing is synthesised: an event that names neither an actor
    nor a policy, and one whose version cannot say what kind of policy source it was, are
    both shapes this module declines to publish rather than guess a kind for.

    `evidence` is *absent* rather than empty when the event cited nothing. Those are
    different claims -- "this act named no evidence" against "this act named an empty list"
    -- and only the first is one the rows support.
    """
    if event.actor_id is not None or event.actor_kind is not None:
        if event.actor_id is None or event.actor_kind is None:
            raise ValueError(
                f"provenance event {event.provenance_event_id!r} carries half an actor "
                f"identity (actor_id={event.actor_id!r}, actor_kind={event.actor_kind!r})"
            )
        actor_id, actor_kind = event.actor_id, event.actor_kind
    elif event.policy_id is not None:
        kind = version.decision_source_kind
        if (
            kind is None
            or kind != "policy_evaluator"
            or version.decision_source_id != event.policy_id
        ):
            raise ValueError(
                f"provenance event {event.provenance_event_id!r} names policy "
                f"{event.policy_id!r}, which is not the exact policy decision source "
                f"governed version {version.governed_record_version_id!r} records "
                f"(decision_source_kind={kind!r}, "
                f"decision_source_id={version.decision_source_id!r})"
            )
        actor_id, actor_kind = event.policy_id, kind
    else:
        raise ValueError(
            f"provenance event {event.provenance_event_id!r} names neither an actor nor a "
            f"policy"
        )
    return ProvenanceEntry(
        actor_id=actor_id,
        actor_kind=actor_kind,
        action=event.action,
        occurred_at=_timestamp(event.occurred_at_us),
        reason_code=event.reason_code,
        reason_comment=event.reason_comment,
        evidence=evidence or None,
    )


def _supersession_reference(
    fact: GovernedSupersession, version_id: str
) -> SupersessionReference:
    """The *other* end of `fact` from `version_id`, with the reason the edge recorded.

    `SupersessionReference` is direction-neutral -- which of `supersedes`/`superseded_by`
    carries it is the whole direction -- so this names the version at the far end and lets
    the caller place it. Asking for the far end by the near one, rather than by field name,
    is what makes the two directions reciprocal by construction: one edge, read from either
    side, yields the two halves of the same replacement.
    """
    return SupersessionReference(
        record_id=fact.governed_record_id,
        version=(
            fact.target_version_id
            if version_id == fact.source_version_id
            else fact.source_version_id
        ),
        reason=fact.reason_code,
    )


def _governed_record(
    version: GovernedVersion,
    *,
    events: tuple[_GovernedProvenanceEvent, ...],
    links: tuple[_GovernedEvidenceLink, ...],
    supersedes: GovernedSupersession | None,
    superseded_by: GovernedSupersession | None,
) -> GovernedRecord:
    """One selected version and its own support rows, as one validated `GovernedRecord`.

    Every field is the stored fact. `layer` is 0009's layer through
    :data:`_GOVERNANCE_LAYER`; `governance_state` is a governed version's own
    `governance_disposition`, and a candidate's is
    :data:`~omnivia_core.contracts.v1.GOVERNANCE_STATE_CANDIDATE` -- 0009 forbids the
    candidate half of the table a disposition at all, and `candidate` is the state the
    contract's `candidates` view requires of every record in it, exactly. `proposed` is the
    neighbouring state `memory.create` stamps on a *creation*, and publishing it here would
    make every candidate this read projects fail the contract's own candidates-view
    validation, which admits `l1`/`candidate`/`current` and nothing else.

    `currentness` is not a column and is not asked of the resolver's view either: a version
    is `superseded` exactly when the snapshot holds a sealed edge out of it, which is the
    same fact the frontier fold used to leave it out of `current_canonical`. So the three
    views agree by construction rather than by three restatements of one rule, and
    `superseded_at` is that edge's *effective* instant -- the later of the edge and its
    sealed target -- never either row's own timestamp.

    `reviewer` is `decision_source_id` and nothing else: 0009 stores the exact reviewer
    principal or policy id there, requires it on every governed version, and forbids it on
    every candidate. An `accepted`/`reviewed`/`canonical` record with no reviewer is refused
    below rather than given one.

    `assertion` and `extraction` stay absent under Amendment 008's fourth rule. Both require
    an instant 0009 stores nowhere -- `CandidateAssertion.asserted_at` and
    `CandidateExtractionMetadata.extracted_at` -- so their exact durable origin lineage
    cannot be reconstructed from the assembly, and the nearby provenance event's
    `occurred_at_us` is a different act's clock, not this claim's assertion time.

    The result is validated whole: shape, temporal ordering, evidence/source agreement,
    currentness/supersession coherence and the governance/authority/layer triple. A stored
    combination the contract calls impossible fails here, with what it was, rather than
    reaching a caller as a well-formed lie.
    """
    layer = _GOVERNANCE_LAYER.get(version.layer)
    if layer is None:
        raise ValueError(
            f"governed version {version.governed_record_version_id!r} carries layer "
            f"{version.layer!r}, which has no authoritative GovernanceLayer"
        )
    state = (
        version.governance_disposition
        if version.layer == LAYER_GOVERNED
        else GOVERNANCE_STATE_CANDIDATE
    )
    if state is None:
        raise ValueError(
            f"governed version {version.governed_record_version_id!r} records no "
            f"governance_disposition"
        )

    version_id = version.governed_record_version_id
    identity = RecordIdentity(
        record_id=version.governed_record_id,
        version=version_id,
        layer=layer,
        governance_state=state,
        currentness=(
            _CURRENTNESS_CURRENT if superseded_by is None else _CURRENTNESS_SUPERSEDED
        ),
        supersedes=(
            None if supersedes is None else _supersession_reference(supersedes, version_id)
        ),
        superseded_by=(
            None
            if superseded_by is None
            else _supersession_reference(superseded_by, version_id)
        ),
    )
    temporal = _temporal_metadata(version)
    if superseded_by is not None:
        temporal = replace(
            temporal, superseded_at=_timestamp(superseded_by.effective_at_us)
        )

    # `sources` is the set of distinct things this version's own citations rest on -- in a
    # fixed order, and without the duplicate `(kind, source_id)` pair the contract refuses
    # when two events cite one artifact. `dict` preserves insertion order, which is what
    # makes that deterministic rather than merely deduplicated.
    #
    # Only an *identical* repeat collapses. Two declarations of one `(kind, source_id)` that
    # disagree on `locator` or `retrieved_at` are two different accounts of where the same
    # source was read from, and keeping whichever arrived first would publish one of them as
    # the record's only answer while silently dropping the other -- a citation the reader
    # cannot tell from an uncontested one. It is refused instead, in the same direction
    # every other stored-fact contradiction here is.
    sources: dict[tuple[str, str], SourceReference] = {}
    for link in links:
        source = _source_reference(link)
        declared = sources.setdefault((source.kind, source.source_id), source)
        if declared != source:
            raise ValueError(
                f"governed version {version.governed_record_version_id!r} declares source "
                f"({source.kind!r}, {source.source_id!r}) twice with different facts: "
                f"{declared!r} then {source!r}"
            )

    record = GovernedRecord(
        workspace_id=version.workspace_id,
        record_type=version.record_type,
        domain_scope=version.domain_scope,
        authority_level=version.authority_level,
        reviewer=version.decision_source_id,
        provenance=RecordProvenance(
            identity=identity,
            temporal=temporal,
            history=tuple(
                _provenance_entry(
                    event,
                    version,
                    tuple(
                        _evidence_reference(link)
                        for link in links
                        if link.provenance_event_id == event.provenance_event_id
                    ),
                )
                for event in events
            ),
            evidence_disposition=version.evidence_disposition,
            sources=tuple(sources.values()),
        ),
        content=_content(version),
    )
    validate_governed_record(record)
    return record


def _hydrate_governed_records(
    snapshot: _GovernedHydrationSnapshot,
) -> tuple[GovernedRecord, ...]:
    """Every version a snapshot selected, as `GovernedRecord`s, in the snapshot's own order.

    Pure: one frozen value in, one tuple of frozen values out. It holds no connection and
    the snapshot holds none either, so there is no post-freeze read to make -- a support row
    the snapshot did not capture is a row this mapping cannot consult, by construction
    rather than by discipline.

    Support rows are grouped by `(assembly_id, governed_record_version_id)` and edges by
    version id, so a record is described only by rows naming it. The snapshot already scopes
    both to the view's selection; matching on the pair as well is what stops two selected
    versions of *different* records sharing an assembly's provenance if a later reader ever
    widens that scoping.

    An edge appears at both of its ends: out of its source as `superseded_by`, into its
    target as `supersedes`. Where a version has more than one edge on a side, the earliest
    *effective* one is taken -- 0009 refuses a branching source, so today there is at most
    one; the rule is stated rather than assumed, and it is the same `min` the frontier fold
    already picks a version's ending instant by.
    """
    events: dict[tuple[str, str], list[_GovernedProvenanceEvent]] = {}
    for event in snapshot.events:
        events.setdefault(
            (event.assembly_id, event.governed_record_version_id), []
        ).append(event)
    links: dict[tuple[str, str], list[_GovernedEvidenceLink]] = {}
    for link in snapshot.links:
        links.setdefault((link.assembly_id, link.governed_record_version_id), []).append(link)

    outgoing: dict[str, GovernedSupersession] = {}
    incoming: dict[str, GovernedSupersession] = {}
    for fact in snapshot.supersessions:
        for edges, version_id in (
            (outgoing, fact.source_version_id),
            (incoming, fact.target_version_id),
        ):
            held = edges.get(version_id)
            if held is None or fact.effective_at_us < held.effective_at_us:
                edges[version_id] = fact

    return tuple(
        _governed_record(
            version,
            events=tuple(
                events.get((version.assembly_id, version.governed_record_version_id), ())
            ),
            links=tuple(
                links.get((version.assembly_id, version.governed_record_version_id), ())
            ),
            supersedes=incoming.get(version.governed_record_version_id),
            superseded_by=outgoing.get(version.governed_record_version_id),
        )
        for version in snapshot.versions
    )


@dataclass(frozen=True, slots=True)
class GovernedRecordValue:
    """One selected version, as the contract sees it *and* as 0009 recorded it.

    `record` is the hydrated DTO and `recorded_at_us` is the exact stored instant that DTO
    cannot carry back: `RecordTemporalMetadata.recorded_at` is the contract's `Timestamp`,
    which is millisecond-precision, so parsing it back loses the microseconds a recency
    ordering has to be total on -- and 0009 writes every version of one correction at one
    microsecond, so the digits lost are exactly the ones that separate them. The two facts
    are paired here, at the read, rather than re-derived later from the wire rendering.

    A frozen value holding two frozen values, so this adds no capability: it is the same
    read `read_governed_records` performs, keeping one integer per record that it drops.
    """

    recorded_at_us: int
    record: GovernedRecord


def read_governed_record_values(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
    view: str | None = None,
) -> tuple[GovernedRecordValue, ...]:
    """`read_governed_records`, with each record's exact stored `recorded_at_us` beside it.

    One snapshot and one hydration -- the versions and the records are the *same* selection
    in the same order, zipped strictly, so a record can never be paired with another
    version's instant.
    """
    snapshot = _read_governed_hydration_snapshot(
        connection,
        workspace_id=workspace_id,
        resolution_instant_us=resolution_instant_us,
        view=view,
    )
    return tuple(
        GovernedRecordValue(recorded_at_us=version.recorded_at_us, record=record)
        for version, record in zip(
            snapshot.versions, _hydrate_governed_records(snapshot), strict=True
        )
    )


def read_governed_records(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
    view: str | None = None,
) -> tuple[GovernedRecord, ...]:
    """One workspace's `view` at `resolution_instant_us`, as contract `GovernedRecord`s.

    The whole authoritative read is one coherent snapshot and the whole projection of it is
    pure, which is the only reason this can be one function: it reads, the transaction ends,
    and the mapping that follows cannot reach the database again because it is handed
    values rather than a handle. Nothing a caller receives is a connection, a cursor or a
    lazily-resolved reference -- a tuple of frozen contract values, and no storage
    capability escapes with them.

    Everything about *which* versions these are is `resolve_governed_versions`': the three
    views, the resolution instant doing both of its jobs, and the supersession fold. This
    adds the projection and nothing else.

    The records are `read_governed_record_values`' own, with the companion instant dropped,
    so a caller that wants only the DTOs and one that wants the pair cannot disagree about
    which versions the view holds.
    """
    return tuple(
        value.record
        for value in read_governed_record_values(
            connection,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
            view=view,
        )
    )


__all__ = [
    "LAYER_CANDIDATE",
    "LAYER_GOVERNED",
    "GovernedRecordValue",
    "GovernedSupersession",
    "GovernedVersion",
    "read_governed_record_values",
    "read_governed_records",
    "read_governed_supersessions",
    "resolve_governed_versions",
]
