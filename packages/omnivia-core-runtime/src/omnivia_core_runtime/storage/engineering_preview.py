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

**A query narrows the frontier before it is authorised, and only by identity.**
`narrow_record_ids` asks SQLite which records have a version whose projection text
could contain the query and returns record ids, never a preview. It is a candidate
superset, not a result: the exact rule is still `rank_previews`, and what a caller
sees, counts or is bound to is read only from the frontier those ids are then
authorised through. A denied record's id may enter the narrowing and leaves it at
the label fold, so it cannot reach a rank, a total, the frontier digest or a
continuation. The narrowing is exact for ASCII-only text (where NFKC and case
folding reduce to `lower`) and conservative for the rest: a row with any other
character is always kept for the Python check. It is also skipped, in favour of the
full authorised read, whenever any version in the domain lacks a current projection
row, so absent and stale projections fail closed exactly as before.

**The narrowed ids are authorised a page at a time.** `read_authorized_previews` cuts the
narrowed (or, unnarrowed, the whole domain's) record ids into pages of
`AUTHORIZED_FRONTIER_PAGE_SIZE`, the page `engineering.context.build` already uses, and
authorises and projects each page on its own, so no statement carries more ids than one
page. Every rule a frontier applies is per record, so the pages together admit exactly the
versions one frontier would, in the same order; a single page keeps its own digest and
several are bound by one digest over the page digests, in page order. A candidate's
normalised text is computed once and kept on the candidate for the request.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Final, NamedTuple

from omnivia_core.contracts.v1 import (
    GOVERNANCE_STATE_CANDIDATE,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.memory import (
    AUTHORIZED_FRONTIER_PAGE_SIZE,
    AuthorizedMemoryFrontier,
    AuthorizedVersion,
    read_authorized_memory_frontier,
    read_memory_record_id_page,
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

#: `preview_search_text` as SQL: title, preview, kind and topic key, one per line. Title
#: and preview are never empty (0053's CHECKs) and kind and topic key are NULL or
#: non-empty, so the same parts are joined as in Python.
_SEARCH_TEXT_SQL: Final = (
    "(title || char(10) || preview || coalesce(char(10) || observation_kind, '') "
    "|| coalesce(char(10) || topic_key, ''))"
)


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
    #: `preview_search_text`, filled on first use so a request normalises each
    #: candidate once however many rules (match, cap, rank, selection) ask for it.
    _search_text: str | None = field(default=None, init=False, repr=False, compare=False)


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
    record_ids: Sequence[str] | None = None,
    query: str | None = None,
) -> tuple[tuple[PreviewCandidate, ...], str]:
    """Return bounded previews and their authorization-frontier digest.

    One read snapshot holds both reads, so the projection rows are those of the
    frontier's own state. The frontier is read first and carries no preview; the
    projection is then read for exactly the admitted assemblies. Its digest includes
    the effective label grant and label-event stream, which lets a continuation bind
    the ACL epoch even when an attach/withdraw cycle leaves the same rows visible.
    ``record_ids`` is the durable-processor seam: when supplied, authorization and
    projection reads are confined to that indexed stable-record page. ``query`` narrows
    the frontier to the records `narrow_record_ids` keeps, before authorization; the
    previews returned are then a superset of the query's matches among the admitted
    versions, to be ranked by `rank_previews`.
    """
    with read_snapshot(connection):
        pages: Iterable[Sequence[str]]
        if record_ids is not None:
            # A caller's own page (the durable processors') is one frontier, as ever.
            pages = (record_ids,)
        else:
            if query is not None:
                record_ids = narrow_record_ids(
                    connection,
                    workspace_id=workspace_id,
                    resolution_instant_us=resolution_instant_us,
                    query=query,
                )
            pages = _record_id_pages(
                connection,
                workspace_id=workspace_id,
                resolution_instant_us=resolution_instant_us,
                record_ids=record_ids,
            )
        candidates: list[PreviewCandidate] = []
        digests: list[str] = []
        for page in pages:
            frontier = read_authorized_memory_frontier(
                connection,
                workspace_id=workspace_id,
                resolution_instant_us=resolution_instant_us,
                view=view,
                label_grant=label_grant,
                domain_scope=OBSERVATION_DOMAIN,
                record_ids=page,
            )
            candidates.extend(
                read_previews_for_frontier(
                    connection, workspace_id=workspace_id, frontier=frontier
                )
            )
            digests.append(frontier.digest)
        return tuple(candidates), _combined_digest(digests)


def _record_id_pages(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
    record_ids: Sequence[str] | None,
) -> Iterator[Sequence[str]]:
    """The domain's record ids in stable, bounded pages, narrowed or not.

    A narrowed list is cut in order, an unnarrowed one is walked by cursor over the
    metadata, the way `engineering.context.build` pages its frontier. Each page is read
    and authorised on its own, so no statement carries more than one page of ids and a
    page's rows are released before the next. Always yields at least one page (an empty
    one for an empty domain), so an empty read still has a frontier digest.
    """
    if record_ids is not None:
        for start in range(0, max(len(record_ids), 1), AUTHORIZED_FRONTIER_PAGE_SIZE):
            yield record_ids[start : start + AUTHORIZED_FRONTIER_PAGE_SIZE]
        return
    after: str | None = None
    first = True
    while True:
        page = read_memory_record_id_page(
            connection,
            workspace_id=workspace_id,
            resolution_instant_us=resolution_instant_us,
            domain_scope=OBSERVATION_DOMAIN,
            after_record_id=after,
        )
        if page or first:
            yield page
        first = False
        if len(page) < AUTHORIZED_FRONTIER_PAGE_SIZE:
            return
        after = page[-1]


def _combined_digest(digests: list[str]) -> str:
    """One frontier's own digest, or one digest binding every page's, in page order."""
    if len(digests) == 1:
        return digests[0]
    joined = "\n".join(digests).encode("utf-8")
    return f"sha256:{hashlib.sha256(joined).hexdigest()}"


def narrow_record_ids(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    resolution_instant_us: int,
    query: str,
) -> tuple[str, ...] | None:
    """The record ids whose versions' preview text could contain ``query``, or None.

    Identity only: no preview is returned, and nothing here is authorised, so the
    result is only ever the key set an authorised frontier is then read for. None
    means "do not narrow, read the whole authorised frontier": the query is empty, or
    some version recorded by the instant has no current projection row (absent or
    stale), which only the authorised read may judge. The caller owns the read
    snapshot.
    ``ponytail:`` the text match is an unindexed SQLite scan of the projection (C
    speed, no Python rows); a persisted trigram index is the upgrade if it ever
    dominates, and rows with non-ASCII text are always returned for the Python check.
    """
    if not connection.in_transaction:
        raise ValueError("preview narrowing requires the caller's active read snapshot")
    needle = normalize_query(query)
    if not needle:
        return None
    unhealthy = connection.execute(
        "SELECT 1 FROM omnivia_authoritative_governed_version_metadata m "
        "WHERE m.workspace_id = ? AND m.domain_scope = ? AND m.recorded_at_us <= ? "
        "AND NOT EXISTS (SELECT 1 FROM omnivia_engineering_preview_projection p "
        "WHERE p.workspace_id = m.workspace_id AND p.assembly_id = m.assembly_id "
        "AND p.projection_version = ? AND p.content_digest = m.content_digest) LIMIT 1",
        (workspace_id, OBSERVATION_DOMAIN, resolution_instant_us, PROJECTION_VERSION),
    ).fetchone()
    if unhealthy is not None:
        return None
    # Drive from the projection, not the metadata: the match is decided on the narrow
    # projection rows and only the matches (and the non-ASCII rows kept for Python) look
    # up their assembly's identity, instead of every assembly of the domain being read
    # (a version's body sits between its identity columns, so reading one assembly row
    # walks its body pages). The inner SELECT's LIMIT keeps SQLite from flattening it, so
    # the joined text is built once per row and not once per use.
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT m.governed_record_id "
            "FROM (SELECT assembly_id FROM ("
            f"SELECT assembly_id, {_SEARCH_TEXT_SQL} AS search_text "
            "FROM omnivia_engineering_preview_projection "
            "WHERE workspace_id = ? AND projection_version = ? LIMIT -1) "
            "WHERE instr(lower(search_text), ?) > 0 "
            "OR length(CAST(search_text AS BLOB)) != length(search_text)) p "
            "CROSS JOIN omnivia_authoritative_governed_version_metadata m "
            "ON m.workspace_id = ? AND m.assembly_id = p.assembly_id "
            "WHERE m.domain_scope = ? AND m.recorded_at_us <= ? "
            "ORDER BY m.governed_record_id",
            (
                workspace_id,
                PROJECTION_VERSION,
                needle,
                workspace_id,
                OBSERVATION_DOMAIN,
                resolution_instant_us,
            ),
        )
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
    metadata a caller filters on, not text it searches. Computed once per candidate:
    a candidate is immutable, so the cached text is always the text it would derive.
    """
    text = candidate._search_text
    if text is None:
        text = normalize_query(
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
        object.__setattr__(candidate, "_search_text", text)
    return text


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
    "narrow_record_ids",
    "preview_search_text",
    "rank_previews",
    "read_authorized_previews",
    "read_previews_for_frontier",
    "rebuild_missing_previews",
    "record_preview",
]
