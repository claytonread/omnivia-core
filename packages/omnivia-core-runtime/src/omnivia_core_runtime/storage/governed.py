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
edges count. The earliest edge out of a version is the one that ends it, so a chain folds
to the one version nothing visible has replaced yet.

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

    An empty workspace, an unknown workspace and a workspace whose every version has been
    superseded all resolve to `()`. None of them is an error here.
    """
    resolved = resolve_governed_record_view(view)
    if resolved not in GOVERNED_RECORD_VIEWS:
        raise ValueError(
            f"view {view!r} is not a recognized GovernedRecordView; must be one of "
            f"{sorted(GOVERNED_RECORD_VIEWS)!r} or absent"
        )

    with authorised(connection, mutations=False, ddl=False) as fenced:
        rows = fenced.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM {_VIEW} "
            "WHERE workspace_id = ? AND recorded_at_us <= ? "
            "ORDER BY governed_record_id ASC, recorded_at_us ASC, append_ordinal ASC, "
            "assembly_id ASC",
            (workspace_id, resolution_instant_us),
        ).fetchall()
        # The seal join is the whole point of reading the edge table directly rather than
        # trusting it: `omnivia_record_supersessions` carries no seal of its own, so an
        # unsealed correction sitting in the table would otherwise retire a version that
        # is still the workspace's live answer -- the one direction of error that loses
        # canonical knowledge rather than merely showing too little.
        #
        # The *earliest* sealed edge out of a version is the one that ended it. 0009
        # refuses a branching source, so today there is at most one; MIN states the rule
        # the fold depends on rather than relying on that refusal holding forever.
        replaced_at = {
            source: int(recorded_at_us)
            for source, recorded_at_us in fenced.execute(
                "SELECT r.source_version_id, MIN(r.recorded_at_us) "
                f"FROM {_SUPERSESSIONS} r "
                f"JOIN {_SEALS} s ON s.workspace_id = r.workspace_id "
                "                AND s.assembly_id = r.assembly_id "
                "WHERE r.workspace_id = ? "
                "GROUP BY r.source_version_id ORDER BY r.source_version_id ASC",
                (workspace_id,),
            ).fetchall()
        }

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

    Equality counts as replaced: a version superseded at exactly the resolution instant is
    history at it, which is the same boundary the contract's `history` view applies to
    `superseded_at`.
    """
    ended_at = replaced_at.get(version.governed_record_version_id)
    return ended_at is not None and ended_at <= instant_us


def _precedence(version: GovernedVersion) -> tuple[int, int, int, str]:
    """The total order the fold picks a record's one frontier version by.

    Authority first, then recorded time, then the append ordinal, then the assembly id.
    The last two exist because the first two do not separate two versions written in one
    transaction, and `assembly_id` is unique per workspace, so the key is total: the same
    rows resolve to the same winner on every run and on every index layout.
    """
    return (
        _AUTHORITY_RANK.get(version.authority_level, -1),
        version.recorded_at_us,
        version.append_ordinal,
        version.assembly_id,
    )


__all__ = [
    "LAYER_CANDIDATE",
    "LAYER_GOVERNED",
    "GovernedVersion",
    "resolve_governed_versions",
]
