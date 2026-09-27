"""The engineering search preview projection: what `engineering.search` reads instead of a body.

SPEC-CORE-ENGMEM-001 §11.1 and AC-033: a search over long observations reads only
authorised metadata and bounded preview projections, and never hydrates a full
record body -- not before ranking, filtering, pagination or a `current_safe`
applicability check. Migration 0053 defines the bounded preview of an
engineering-domain assembly (`omnivia_engineering_preview_source`) and stores it
beside the assembly (`omnivia_engineering_preview_projection`). The writers
project each assembly they insert, in the same settlement (`record_preview`), and
the table's INSERT guard admits only the derivation of the row's own assembly; a
search only ever *reads* the projection.

**Authorization comes first, and it reads no preview.** `read_authorized_previews`
freezes the evidence-label-authorised frontier from identities and evidence links
(`memory.read_authorized_memory_frontier`) and only then asks the projection for
the rows of the versions that frontier admitted. A denied version is never named
in a projection read, so its preview is never selected, scored, counted or
digested.

**A missing or stale projection is a refusal, never a fallback.** A version the
frontier admitted with no projection row is `PreviewProjectionUnavailable`; one
whose rows are of another projection version, or were derived from another
content digest, is `PreviewProjectionStale`. Falling back to the stored body would
answer the request from outside the projection this build claims to serve, which
is exactly the read this module exists to remove. `rebuild_missing_previews` is
the maintenance path that restores a lost row; a read never builds anything.

**The ranker is pure, and it ranks what a caller could see.** `rank_previews` is
the governed ranker's own rule -- how many times the normalised query occurs in the
candidate's normalised text, then recency, then identity -- over the bounded
projection text (title, preview, observation kind and topic key) rather than over
the whole content. It holds no connection and sees nothing but the candidates it
is handed, so an unauthorised version cannot influence an authorised one's rank.
A term that occurs only beyond a preview is not matched: finding it is an exact
read or expansion of the version, which is where a full body is hydrated.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, NamedTuple

from omnivia_core.contracts.v1 import (
    GOVERNANCE_STATE_CANDIDATE,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.memory import (
    AuthorizedMemoryFrontier,
    AuthorizedVersion,
    read_authorized_memory_frontier,
    read_snapshot,
)
from omnivia_core_runtime.storage.retrieval import EvidenceLabelGrant, normalize_query

#: The domain whose assemblies carry a projection row (§22.1). It is the domain
#: `engineering.search` serves, so the frontier is narrowed by it before any label
#: is folded.
OBSERVATION_DOMAIN: Final = "engineering.codebase"

#: The projection version this build reads. It is the literal migration 0053's
#: `omnivia_engineering_preview_source` emits, and a change to what a preview is
#: ships as a new migration that adds rows of the next version beside these, so a
#: workspace between the two is `PreviewProjectionStale` rather than silently
#: served from the old rules.
PROJECTION_VERSION: Final = 1

#: The bounds of one preview, stated once: the same numbers 0053's CHECKs enforce.
#: 480 code points can never exceed 1920 UTF-8 bytes, so the byte bound is never the
#: binding one, and it is checked anyway.
TITLE_MAX_CODEPOINTS: Final = 200
PREVIEW_MAX_CODEPOINTS: Final = 480
PREVIEW_MAX_BYTES: Final = 2048

#: Projection rows read per statement, so a statement stays under SQLite's oldest
#: default bound on bound parameters however many versions a frontier admits.
_ROW_BATCH: Final = 900


class PreviewProjectionUnavailable(StorageError):
    """An admitted version has no projection row: nothing may be served for it."""


class PreviewProjectionStale(StorageError):
    """An admitted version's projection is of another version or another content."""


@dataclass(frozen=True, slots=True)
class PreviewCandidate:
    """One admitted engineering observation: identity facts and its bounded preview.

    Nothing here was read from a body column. `governance_state` is the exact
    version's own: `candidate` for the candidate layer, its stored disposition for
    a governed version. `content_digest` is the stored digest of the exact content,
    so a caller can bind a snapshot to the content without reading it.
    """

    assembly_id: str
    record_id: str
    version: str
    recorded_at_us: int
    governance_state: str
    evidence_disposition: str
    evidence_available: bool
    content_digest: str
    title: str
    preview: str
    truncated: bool
    observation_kind: str | None
    assertion_basis: str | None
    topic_key: str | None
    repository_id: str | None
    snapshot_id: str | None


class _Row(NamedTuple):
    """One projection row without its keys, in the table's own column order."""

    content_digest: str
    title: str
    preview: str
    truncated: int
    observation_kind: str | None
    assertion_basis: str | None
    topic_key: str | None
    repository_id: str | None
    snapshot_id: str | None


def read_authorized_previews(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
    view: str | None,
    label_grant: EvidenceLabelGrant,
) -> tuple[PreviewCandidate, ...]:
    """The engineering observations one grant admits under `view`, as bounded previews.

    One read snapshot holds both reads, so the projection rows are those of the
    frontier's own state. The frontier is read first and carries no preview; the
    projection is then read for exactly the admitted assemblies.
    """
    with read_snapshot(connection):
        frontier = read_authorized_memory_frontier(
            connection,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
            view=view,
            label_grant=label_grant,
            domain_scope=OBSERVATION_DOMAIN,
        )
        return read_previews_for_frontier(
            connection, workspace_id=workspace_id, frontier=frontier
        )


def read_previews_for_frontier(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    frontier: AuthorizedMemoryFrontier,
) -> tuple[PreviewCandidate, ...]:
    """Read projections for exactly one already-authorized frozen frontier.

    The caller owns the read snapshot.  This function performs no authorization
    lookup and opens no transaction; its only projection keys are the admitted
    assembly ids in ``frontier``.  The bounded projection is the complete search
    surface, so terms that occur only beyond its first 480 code points do not
    match without a later exact expansion.
    """
    if not connection.in_transaction:
        raise ValueError("preview reads require the caller's active read snapshot")
    held = _read_rows(
        connection, workspace_id, [version.assembly_id for version in frontier.versions]
    )
    candidates: list[PreviewCandidate] = []
    absent = stale = False
    for version in frontier.versions:
        rows = held.get(version.assembly_id)
        if not rows:
            absent = True
            continue
        row = rows.get(PROJECTION_VERSION)
        if row is None or row.content_digest != version.content_digest:
            stale = True
            continue
        candidates.append(_candidate(version, row))
    # Sentinel-then-raise, as the evidence search does: nothing is raised while a
    # storage error is being handled, so no error's text is chained to the refusal.
    if absent:
        raise PreviewProjectionUnavailable(
            "an admitted engineering version has no preview projection row"
        )
    if stale:
        raise PreviewProjectionStale(
            "an admitted engineering version's preview projection is not current"
        )
    return tuple(candidates)


def _read_rows(
    connection: sqlite3.Connection, workspace_id: str, assembly_ids: list[str]
) -> dict[str, dict[int, _Row]]:
    """Every projection row of the given assemblies, by assembly and projection version."""
    held: dict[str, dict[int, _Row]] = {}
    for start in range(0, len(assembly_ids), _ROW_BATCH):
        batch = assembly_ids[start : start + _ROW_BATCH]
        placeholders = ", ".join("?" for _ in batch)
        for row in connection.execute(
            "SELECT assembly_id, projection_version, content_digest, title, preview, "
            "truncated, observation_kind, assertion_basis, topic_key, repository_id, "
            "snapshot_id FROM omnivia_engineering_preview_projection "
            f"WHERE workspace_id = ? AND assembly_id IN ({placeholders})",
            (workspace_id, *batch),
        ):
            held.setdefault(str(row[0]), {})[int(row[1])] = _Row(*tuple(row)[2:])
    return held


def _candidate(version: AuthorizedVersion, row: _Row) -> PreviewCandidate:
    return PreviewCandidate(
        assembly_id=version.assembly_id,
        record_id=version.record_id,
        version=version.version_id,
        recorded_at_us=version.recorded_at_us,
        governance_state=(
            GOVERNANCE_STATE_CANDIDATE
            if version.layer == "candidate"
            else str(version.governance_disposition)
        ),
        evidence_disposition=version.evidence_disposition,
        evidence_available=version.has_evidence,
        content_digest=version.content_digest,
        title=row.title,
        preview=row.preview,
        truncated=bool(row.truncated),
        observation_kind=row.observation_kind,
        assertion_basis=row.assertion_basis,
        topic_key=row.topic_key,
        repository_id=row.repository_id,
        snapshot_id=row.snapshot_id,
    )


def preview_search_text(candidate: PreviewCandidate) -> str:
    """The normalised surface a query matches: the text of the bounded preview.

    Title, preview text, observation kind and topic key, one per line, with the same
    NFKC and case folding the query gets. Identifiers and the assertion basis are
    metadata a caller filters on, not text it searches.
    """
    return normalize_query(
        "\n".join(
            part
            for part in (
                candidate.title,
                candidate.preview,
                candidate.observation_kind,
                candidate.topic_key,
            )
            if part
        )
    )


def rank_previews(
    candidates: Sequence[PreviewCandidate], query: str
) -> tuple[PreviewCandidate, ...]:
    """The matching candidates, totally ordered: the governed ranker's rule over previews.

    Relevance is how many times the normalised query occurs in the candidate's
    normalised preview text, descending; ties break on `recorded_at_us` descending,
    then record id, then version, ascending. That key is total because 0009 makes
    `(record id, version)` unique per workspace, so one frontier resolves to one
    order on every run. A candidate that does not contain the query is absent, not
    ranked last. An empty normalised query matches nothing.
    """
    needle = normalize_query(query)
    if not needle:
        return ()
    matched = [
        (candidate, hits)
        for candidate in candidates
        if (hits := preview_search_text(candidate).count(needle))
    ]
    matched.sort(
        key=lambda pair: (-pair[1], -pair[0].recorded_at_us, pair[0].record_id, pair[0].version)
    )
    return tuple(candidate for candidate, _ in matched)


_PROJECT: Final = (
    "INSERT INTO omnivia_engineering_preview_projection "
    "(workspace_id, assembly_id, projection_version, content_digest, title, preview, "
    "truncated, observation_kind, assertion_basis, topic_key, repository_id, snapshot_id) "
    "SELECT s.workspace_id, s.assembly_id, s.projection_version, s.content_digest, "
    "s.title, s.preview, s.truncated, s.observation_kind, s.assertion_basis, "
    "s.topic_key, s.repository_id, s.snapshot_id "
    "FROM omnivia_engineering_preview_source s "
)


def record_preview(
    connection: sqlite3.Connection, *, workspace_id: str, assembly_id: str
) -> None:
    """Project one just-inserted engineering-domain assembly, in its own settlement.

    Called by each writer of a version, on the fenced connection, right after it
    inserts the assembly: `memory.create` for a proposal and the governance
    transition that copies content into a new exact version. The row is the
    migration's own derivation of the assembly, and the table's INSERT guard admits
    nothing else. An assembly that cannot be projected (its content is not a JSON
    object) is refused here rather than left as a version no search can serve.
    """
    cursor = connection.execute(
        _PROJECT + "WHERE s.workspace_id = ? AND s.assembly_id = ?",
        (workspace_id, assembly_id),
    )
    if cursor.rowcount != 1:
        raise StorageError("an engineering version has no bounded preview to project")


def rebuild_missing_previews(connection: sqlite3.Connection) -> int:
    """Insert the current-version projection row of every assembly that lacks one.

    The maintenance path for a row that is missing while its assembly is not: a
    workspace whose projection was lost, or restored from a state that never had it.
    It runs on the fenced writer connection, derives each row the way a writer does,
    and returns how many rows it added. It is the only code here that reads a body
    outside a writer's own settlement, and no read path ever calls it.
    """
    return connection.execute(
        _PROJECT
        + "WHERE NOT EXISTS (SELECT 1 FROM omnivia_engineering_preview_projection p "
        "WHERE p.workspace_id = s.workspace_id AND p.assembly_id = s.assembly_id "
        "AND p.projection_version = s.projection_version)"
    ).rowcount


__all__ = [
    "OBSERVATION_DOMAIN",
    "PREVIEW_MAX_BYTES",
    "PREVIEW_MAX_CODEPOINTS",
    "PROJECTION_VERSION",
    "TITLE_MAX_CODEPOINTS",
    "PreviewCandidate",
    "PreviewProjectionStale",
    "PreviewProjectionUnavailable",
    "preview_search_text",
    "rank_previews",
    "read_authorized_previews",
    "read_previews_for_frontier",
    "rebuild_missing_previews",
    "record_preview",
]
