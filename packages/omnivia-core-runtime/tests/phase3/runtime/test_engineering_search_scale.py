"""Engineering search and context narrowing at large candidate counts
(SPEC-CORE-ENGMEM-001 §11.1, §20.2; migration 0053's projection).

`engineering.search` narrows the record-id space by query in SQLite, authorises the
narrowed ids a bounded page at a time, reads only the admitted projection rows and
ranks them in Python. This suite proves that path at corpora far larger than the
behaviour tests seed, without seeding them through the writers: `bulk_load` clones one
real, writer-produced version (its record, assembly, seal, provenance event, evidence
link and projection row) per seed under lifted guards, then restores the guards and
checks the schema fingerprint. The search itself runs through the production
application surface.

Four things are held, none of them with a wall-clock assertion:

* the answer: paged search returns exactly what an independent oracle (the spec's
  NFKC-and-case-fold rule, then hits, recency, record id, version) returns, across
  Unicode spellings, newlines between fields, label-denied records on every page, and
  damaged projection rows on the last one;
* the shape of the work: no statement names more ids than one page, each candidate is
  normalised once however many rules ask, and the authorised read for a page costs the
  same at any corpus size (SQLite's own VM-instruction count, which is the same on every
  machine, stands in for time);
* the whole read stays linear in the corpus within a budget in instructions per row;
* the 100 000-observation figure of §20.2 is reproducible here, on request, as a
  benchmark (`OMNIVIA_ENGINEERING_SEARCH_BENCH=100000`), alongside the qualification lane.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import statistics
import time
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_engineering_preview_projection as epp
import test_engineering_source_coverage as esc
from omnivia_core_runtime.service.authorization import AuthenticatedSession
from omnivia_core_runtime.storage import engineering_preview, memory
from omnivia_core_runtime.storage.connection import authorised
from omnivia_core_runtime.storage.memory import AUTHORIZED_FRONTIER_PAGE_SIZE
from omnivia_core_runtime.storage.retrieval import (
    CONFIGURED_LOCAL_OWNER,
    EvidenceLabelGrant,
)

Workspace = esc.Workspace
WORKSPACE_ID = esc.WORKSPACE_ID

ASSEMBLIES = "omnivia_governed_version_assemblies"
RECORDS = "omnivia_governed_records"
SEALS = "omnivia_governed_version_seals"
EVENTS = "omnivia_governed_provenance_events"
LINKS = "omnivia_governed_version_evidence_links"
PROJECTION = "omnivia_engineering_preview_projection"
CLONED = (RECORDS, ASSEMBLIES, SEALS, EVENTS, LINKS, PROJECTION)
BULK_PREFIX = "bulk"


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    opened = Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


def another(root: Path, name: str) -> Workspace:
    """A second, independent workspace beside the fixture's."""
    (root / name).mkdir()
    return Workspace(root / name)


# --- the bulk fixture ----------------------------------------------------------------


@dataclass(frozen=True)
class Seed:
    """One engineering observation's projected text, and where it sits in the order."""

    title: str
    preview: str = "A note."
    kind: str | None = None
    topic: str | None = None
    denied: bool = False  # evidence under the owner-held label: another reader is denied
    age: int = 0  # recorded `age` microseconds before the template, so recency varies
    body_pad: int = 0  # extra stored body bytes, to make assembly rows realistically wide


@dataclass(frozen=True)
class Loaded:
    record_id: str
    version: str
    recorded_at_us: int
    seed: Seed


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    """The table's insertable columns: generated ones are computed, never written."""
    return [row[1] for row in connection.execute(f"PRAGMA table_xinfo({table})") if row[6] == 0]


def _insert_guards(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'trigger' AND name LIKE '%insert' "
            f"AND tbl_name IN ({', '.join('?' for _ in CLONED)}) ORDER BY name",
            CLONED,
        )
    ]


def prepare(workspace: Workspace) -> dict[str, dict[str, Any]]:
    """The writer-produced template version, and the open evidence the open seeds use."""
    template = workspace.observe(esc._observation(None, title="template", evidence=True))
    m2.write(workspace.holder, m2.EVIDENCE, evidence_id="evd-open", source_native_id="doc-open")
    connection = workspace.holder.connection
    rows: dict[str, dict[str, Any]] = {}
    for table, column in (
        (RECORDS, "governed_record_id"),
        (ASSEMBLIES, "governed_record_id"),
        (PROJECTION, "assembly_id"),
    ):
        names = _columns(connection, table)
        key = template["record_id"]
        if column == "assembly_id":
            key = connection.execute(
                f"SELECT assembly_id FROM {ASSEMBLIES} WHERE governed_record_id = ?", (key,)
            ).fetchone()[0]
        rows[table] = dict(
            zip(
                names,
                connection.execute(
                    f"SELECT {', '.join(names)} FROM {table} WHERE {column} = ?", (key,)
                ).fetchone(),
                strict=True,
            )
        )
    assembly_id = rows[ASSEMBLIES]["assembly_id"]
    for table in (SEALS, EVENTS, LINKS):
        names = _columns(connection, table)
        rows[table] = dict(
            zip(
                names,
                connection.execute(
                    f"SELECT {', '.join(names)} FROM {table} WHERE assembly_id = ?",
                    (assembly_id,),
                ).fetchone(),
                strict=True,
            )
        )
    return rows


def bulk_load(workspace: Workspace, seeds: Sequence[Seed]) -> list[Loaded]:
    """Insert one sealed, projected, evidence-linked candidate version per seed.

    The rows are copies of the writer-produced template with fresh identities and the
    seed's text, inserted with the insert guards lifted and put back verbatim, so the
    database ends in the canonical schema. Deterministic: identities are numbered from
    the number of bulk versions already present.
    """
    connection = workspace.holder.connection
    template: dict[str, dict[str, Any]] = workspace.bulk_template  # type: ignore[attr-defined]
    start = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {RECORDS} WHERE governed_record_id LIKE 'rec-{BULK_PREFIX}%'"
        ).fetchone()[0]
    )
    base = int(template[ASSEMBLIES]["recorded_at_us"])
    ordinal = int(template[ASSEMBLIES]["append_ordinal"])
    loaded: list[Loaded] = []
    batches: dict[str, list[list[Any]]] = {table: [] for table in CLONED}
    for offset, seed in enumerate(seeds):
        n = start + offset
        record, version, assembly = (f"{p}-{BULK_PREFIX}-{n:08d}" for p in ("rec", "ver", "asm"))
        digest = "sha256:" + hashlib.sha256(f"{BULK_PREFIX}:{n}".encode()).hexdigest()
        recorded = base - seed.age
        title = seed.title[:200]
        preview = seed.preview[:480]
        body = json.dumps(
            {"title": title, "summary": preview, "pad": "x" * seed.body_pad},
            separators=(",", ":"),
        )
        overrides: dict[str, dict[str, Any]] = {
            RECORDS: {"governed_record_id": record, "recorded_at_us": recorded},
            ASSEMBLIES: {
                "assembly_id": assembly,
                "governed_record_id": record,
                "governed_record_version_id": version,
                "content_digest": digest,
                "content_json": body,
                "append_ordinal": ordinal + n + 1,
                "recorded_at_us": recorded,
                "valid_from_us": recorded,
            },
            SEALS: {
                "seal_id": f"seal-{BULK_PREFIX}-{n:08d}",
                "assembly_id": assembly,
                "governed_record_version_id": version,
                "sealed_at_us": recorded,
            },
            EVENTS: {
                "provenance_event_id": f"pev-{BULK_PREFIX}-{n:08d}",
                "assembly_id": assembly,
                "governed_record_version_id": version,
                "occurred_at_us": recorded,
                "recorded_at_us": recorded,
            },
            LINKS: {
                "assembly_id": assembly,
                "provenance_event_id": f"pev-{BULK_PREFIX}-{n:08d}",
                "evidence_id": "evd-0001" if seed.denied else "evd-open",
                "recorded_at_us": recorded,
            },
            PROJECTION: {
                "assembly_id": assembly,
                "content_digest": digest,
                "title": title,
                "preview": preview,
                "truncated": int(len(seed.preview) > 480 or len(seed.title) > 200),
                "observation_kind": seed.kind,
                "topic_key": seed.topic,
            },
        }
        for table in CLONED:
            row = {**template[table], **overrides[table]}
            batches[table].append([row[name] for name in template[table]])
        loaded.append(Loaded(record, version, recorded, seed))
    guards = epp._guards_lifted(connection, *_insert_guards(connection))
    with guards, authorised(connection, mutations=True):
        connection.execute("BEGIN IMMEDIATE")
        try:
            for table in CLONED:
                names = list(template[table])
                connection.executemany(
                    f"INSERT INTO {table} ({', '.join(names)}) "
                    f"VALUES ({', '.join('?' for _ in names)})",
                    batches[table],
                )
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    return loaded


def load(workspace: Workspace, seeds: Sequence[Seed]) -> list[Loaded]:
    """`bulk_load`, preparing the writer-produced template on the workspace's first use."""
    if not hasattr(workspace, "bulk_template"):
        workspace.bulk_template = prepare(workspace)  # type: ignore[attr-defined]
    return bulk_load(workspace, seeds)


# --- the oracle ------------------------------------------------------------------------


def _spec_text(seed: Seed) -> str:
    """The searchable surface exactly as the specification states it: title, preview,
    kind and topic key, one per line, then NFKC and case folding."""
    parts = [seed.title[:200], seed.preview[:480], seed.kind, seed.topic]
    return unicodedata.normalize("NFKC", "\n".join(part for part in parts if part)).casefold()


def oracle(loaded: Sequence[Loaded], query: str, *, visible: bool | None = None) -> list[str]:
    """The record ids a search must return, in order, from the seeds alone.

    ``visible`` limits the corpus to what a restricted reader may see (not denied).
    """
    needle = unicodedata.normalize("NFKC", query).casefold()
    if not needle:
        return []
    scored = []
    for item in loaded:
        if visible and item.seed.denied:
            continue
        hits = _spec_text(item.seed).count(needle)
        if hits:
            scored.append((-hits, -item.recorded_at_us, item.record_id, item.version))
    return [record for _hits, _recorded, record, _version in sorted(scored)]


def walk(
    workspace: Workspace,
    query: str,
    *,
    session: AuthenticatedSession | None = None,
    limit: int = 100,
) -> list[str]:
    """Every page of one search, in order."""
    payload: dict[str, Any] = {"query": query, "view": "candidates", "limit": limit}
    found: list[str] = []
    while True:
        page = workspace.ok("engineering.search", payload, session=session)
        found += [preview["record_id"] for preview in page["previews"]]
        if not page["page"]:
            return found
        payload["page"] = page["page"]


# --- instruments -----------------------------------------------------------------------


@contextmanager
def vm_steps(connection: sqlite3.Connection) -> Iterator[list[int]]:
    """SQLite's own count of virtual-machine instructions run in the block, in thousands.

    The same on every machine for one SQLite build, so a budget in it is a deterministic
    threshold where a millisecond budget would be a flaky one."""
    counter = [0]

    def tick() -> int:
        counter[0] += 1
        return 0

    connection.set_progress_handler(tick, 1000)
    try:
        yield counter
    finally:
        connection.set_progress_handler(None, 0)


def spy_frontier(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The id count of every frontier read the search path makes."""
    sizes: list[int] = []
    frontier = engineering_preview.read_authorized_memory_frontier

    def spy(*args: Any, **kwargs: Any) -> Any:
        sizes.append(len(kwargs["record_ids"]))
        return frontier(*args, **kwargs)

    monkeypatch.setattr(engineering_preview, "read_authorized_memory_frontier", spy)
    return sizes


def narrowed(workspace: Workspace, query: str) -> tuple[str, ...] | None:
    connection = workspace.holder.connection
    with memory.read_snapshot(connection):
        return engineering_preview.narrow_record_ids(
            connection,
            workspace_id=WORKSPACE_ID,
            resolution_instant_us=2**62,
            query=query,
        )


NEEDLE = "needle"
OWNER_GRANT = EvidenceLabelGrant(
    principal_id=CONFIGURED_LOCAL_OWNER,
    workspace_id=WORKSPACE_ID,
    all_labels=True,
    labels=frozenset(),
)


def _matching(count: int, rng: random.Random, *, denied: bool = False) -> list[Seed]:
    """Seeds that contain the needle, with varied hit counts and recency (so ties and
    every sort key are exercised)."""
    seeds = []
    for index in range(count):
        hits = rng.choice((1, 1, 1, 2, 3))
        seeds.append(
            Seed(
                title=f"Finding {index} {' '.join([NEEDLE] * hits)}",
                preview=f"Note {index}",
                denied=denied,
                age=rng.randrange(0, 6),
            )
        )
    return seeds


def _filler(count: int, *, unicode_text: bool = False) -> list[Seed]:
    word = "Ünrelated é" if unicode_text else "Unrelated"
    return [Seed(title=f"{word} filler {index}", preview="Nothing to find.") for index in range(count)]


# --- the answer, over many pages ---------------------------------------------------------


def test_paged_search_returns_exactly_the_oracles_ranking(workspace: Workspace) -> None:
    """More than two pages of candidates, with hits, recency and identity ties: every
    page of the production search, concatenated, is the oracle's order, and no
    statement or frontier read names more ids than one page."""
    rng = random.Random(20261005)
    seeds = _matching(1300, rng) + _filler(500) + _filler(40, unicode_text=True)
    rng.shuffle(seeds)
    loaded = load(workspace, seeds)
    expected = oracle(loaded, NEEDLE)
    assert len(expected) == 1300
    ids = narrowed(workspace, NEEDLE)
    assert ids is not None and len(ids) > 2 * AUTHORIZED_FRONTIER_PAGE_SIZE
    assert len(ids) == 1300 + 40  # every match, plus the non-ASCII rows kept for Python
    assert list(ids) == sorted(ids)

    connection = workspace.holder.connection
    with epp.Trace(connection) as trace:
        found = walk(workspace, NEEDLE)
    assert found == expected
    for statement in trace.statements:
        assert statement.count("'rec-") <= AUTHORIZED_FRONTIER_PAGE_SIZE


def test_the_frontier_is_read_a_page_of_ids_at_a_time(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = random.Random(1)
    loaded = load(workspace, _matching(1200, rng) + _filler(100))
    sizes = spy_frontier(monkeypatch)
    first = workspace.ok("engineering.search", {"query": NEEDLE, "view": "candidates"})
    assert [p["record_id"] for p in first["previews"]] == oracle(loaded, NEEDLE)[:20]
    assert sizes == [512, 512, 176]
    # With no narrowing (no query) the whole domain is read, still a page at a time.
    sizes.clear()
    connection = workspace.holder.connection
    previews, digest = engineering_preview.read_authorized_previews(
        connection,
        workspace_id=WORKSPACE_ID,
        resolution_instant_us=2**62,
        view="candidates",
        label_grant=OWNER_GRANT,
    )
    assert sizes == [512, 512, 277]
    assert len(previews) == 1300 + 1  # the corpus and the template version
    assert digest.startswith("sha256:")


def test_a_page_boundary_never_splits_or_repeats_a_record(workspace: Workspace) -> None:
    """Corpus sizes straddling the page size, so the last page is empty, one short,
    exactly full and one over."""
    for count in (
        AUTHORIZED_FRONTIER_PAGE_SIZE - 1,
        AUTHORIZED_FRONTIER_PAGE_SIZE,
        AUTHORIZED_FRONTIER_PAGE_SIZE + 1,
    ):
        rng = random.Random(count)
        local = another(workspace.holder.path.parent, f"w{count}")
        try:
            loaded = load(local, _matching(count, rng))
            found = walk(local, NEEDLE)
            assert found == oracle(loaded, NEEDLE)
            assert len(set(found)) == count
        finally:
            local.holder.connection.close()


# --- Unicode, newline and case equivalence (property) ------------------------------------

#: Spellings that are one word under NFKC and case folding (and ones that only look alike).
FOLDS = (
    "needle", "NEEDLE", "Needle", "ＮＥＥＤＬＥ", "ｎｅｅｄｌｅ", "ne\u200bedle",
    "straße", "STRASSE", "Strasse", "STRAẞE", "ſtrasse",
    "İstanbul", "istanbul", "ISTANBUL", "i̇stanbul",
    "ﬁnder", "finder", "FINDER", "ǆ", "ǅ", "Ǆ",
    "café", "café", "CAFÉ", "①", "1", "ｶﾞ", "ガ",
)  # fmt: skip
SEPARATORS = (" ", "\n", " \n ", "-", "/", "")
QUERIES = (
    "needle", "NEEDLE", "ＮＥＥＤＬＥ", "strasse", "STRAßE", "straße", "ſtrasse",
    "istanbul", "İSTANBUL", "i̇stanbul", "ﬁnder", "FINDER", "ǆ", "café", "CAFÉ",
    "1", "ガ", "ｶﾞ", "needle strasse", "needle\nstrasse", "x\ny",
)  # fmt: skip


def _random_seed(rng: random.Random, index: int) -> Seed:
    def text(max_parts: int) -> str:
        parts = []
        for _ in range(rng.randrange(1, max_parts + 1)):
            parts += [rng.choice(FOLDS), rng.choice(SEPARATORS)]
        return "".join(parts).strip() or "x"

    return Seed(
        title=text(3),
        preview=text(6),
        kind=rng.choice((None, "decision", "FINDER", "ＮＥＥＤＬＥ")),
        topic=rng.choice((None, "straße/needle", "x\ny")),
        age=rng.randrange(0, 4),
    )


def test_unicode_case_and_newline_equivalence_holds_through_the_narrowing(
    workspace: Workspace,
) -> None:
    """For random text over fullwidth, ß/SS, İ, ligature, combining-mark and newline
    spellings, the narrowing is a superset of the oracle's matches (it never drops one),
    and the production search is exactly the oracle -- query by query."""
    rng = random.Random(0xC0FFEE)
    seeds = [_random_seed(rng, index) for index in range(700)]
    # Queries that cross the field boundary, built from the seeds themselves.
    boundary = []
    for seed in seeds[:12]:
        title = unicodedata.normalize("NFKC", seed.title).casefold()
        preview = unicodedata.normalize("NFKC", seed.preview).casefold()
        boundary.append(f"{title[-3:]}\n{preview[:3]}")
    loaded = load(workspace, seeds)
    checked = 0
    for query in (*QUERIES, *boundary):
        expected = oracle(loaded, query)
        keep = narrowed(workspace, query)
        if expected:
            checked += 1
        if not unicodedata.normalize("NFKC", query).casefold():
            assert keep is None
            continue
        assert keep is not None
        assert set(expected) <= set(keep), query
        assert walk(workspace, query) == expected, query
    assert checked >= 15  # the queries are not vacuous


@pytest.mark.parametrize(
    ("stored", "query"),
    [
        ("Straße Guide", "STRASSE"),
        ("STRASSE guide", "straße"),
        ("İstanbul Guide", "i̇stanbul"),
        ("ＦＵＬＬＷＩＤＴＨ text", "fullwidth"),
        ("fullwidth text", "ＦＵＬＬＷＩＤＴＨ"),
        ("ﬁnal ﬂight", "final flight"),
        ("line one", "one"),
    ],
)
def test_named_unicode_equivalences_match_in_both_directions(
    workspace: Workspace, stored: str, query: str
) -> None:
    loaded = load(workspace, [Seed(title=stored), Seed(title="Unmatched title")])
    assert oracle(loaded, query) == [loaded[0].record_id]
    assert walk(workspace, query) == [loaded[0].record_id]


def test_a_query_spanning_two_fields_matches_across_the_newline(workspace: Workspace) -> None:
    loaded = load(
        workspace,
        [Seed(title="alpha title", preview="beta preview"), Seed(title="alpha", preview="gamma")],
    )
    assert walk(workspace, "title\nbeta") == [loaded[0].record_id]
    assert walk(workspace, "title beta") == []  # a space is not the line break between fields
    assert walk(workspace, "alpha\ngamma") == [loaded[1].record_id]


# --- authorisation precedes ranking, on every page ---------------------------------------


def test_denied_records_on_every_page_change_nothing_a_reader_sees(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Denied matches interleave with visible ones across three pages. The reader's
    results are the visible oracle's, the ranker is handed no denied record, and a
    corpus whose denied records do not match the query at all yields the same results:
    a hidden match changes no rank, count or page."""
    rng = random.Random(7)
    visible = _matching(700, rng)
    denied = _matching(700, rng, denied=True)
    seeds = [item for pair in zip(visible, denied, strict=True) for item in pair]
    loaded = load(workspace, seeds)
    reader = esc._reader()
    expected = oracle(loaded, NEEDLE, visible=True)
    assert len(expected) == 700
    assert len(narrowed(workspace, NEEDLE) or ()) == 1400  # the narrowing is not authorised

    handed: list[set[str]] = []
    rank = engineering_preview.rank_previews

    def spy(candidates: Any, *args: Any, **kwargs: Any) -> Any:
        handed.append({candidate.record_id for candidate in candidates})
        return rank(candidates, *args, **kwargs)

    monkeypatch.setattr(epp.handlers, "rank_previews", spy)
    assert walk(workspace, NEEDLE, session=reader) == expected
    denied_ids = {item.record_id for item in loaded if item.seed.denied}
    assert handed and all(not (ids & denied_ids) for ids in handed)
    assert set(walk(workspace, NEEDLE)) == {item.record_id for item in loaded}  # the owner

    quiet = another(tmp_path, "quiet")
    try:
        silenced = [
            Seed(title=s.title.replace(NEEDLE, "other"), preview=s.preview, denied=True, age=s.age)
            if s.denied
            else s
            for s in seeds
        ]
        load(quiet, silenced)
        assert walk(quiet, NEEDLE, session=reader) == expected
        first = workspace.ok("engineering.search", {"query": NEEDLE, "view": "candidates"}, session=reader)
        again = quiet.ok("engineering.search", {"query": NEEDLE, "view": "candidates"}, session=reader)
        assert first["previews"] == again["previews"]
        assert first["coverage"] == again["coverage"]
        assert bool(first["page"]) == bool(again["page"])
    finally:
        quiet.holder.connection.close()


def test_a_denied_records_projection_cannot_refuse_a_reader_but_refuses_the_owner(
    workspace: Workspace,
) -> None:
    """Stale-projection refusal is judged on what the caller is admitted to: a damaged
    row of a label-denied version on the last page is the owner's refusal, not the
    reader's, exactly as an unpaged read answered."""
    rng = random.Random(3)
    loaded = load(workspace, _matching(600, rng) + _matching(600, rng, denied=True))
    last_denied = max(item.record_id for item in loaded if item.seed.denied)
    connection = workspace.holder.connection
    assembly = connection.execute(
        f"SELECT assembly_id FROM {ASSEMBLIES} WHERE governed_record_id = ?", (last_denied,)
    ).fetchone()[0]
    with epp._guards_lifted(connection, epp.UPDATE_GUARD):
        epp._damage(
            connection,
            f"UPDATE {PROJECTION} SET content_digest = 'sha256:' || printf('%064d', 7) "
            f"WHERE assembly_id = '{assembly}'",
        )
    request = {"query": NEEDLE, "view": "candidates"}
    assert workspace.refused("engineering.search", request)[0] == "stale_projection"
    ok = workspace.ok("engineering.search", request, session=esc._reader())
    assert ok["previews"]


def test_a_damaged_row_on_the_last_page_refuses_with_the_unpaged_codes(
    workspace: Workspace,
) -> None:
    """The damaged row is off the query, in the last page of the domain read the
    narrowing steps aside for: it refuses with the code an unpaged read gave -- stale
    for another version or content, unavailable for a lost row."""
    rng = random.Random(5)
    loaded = load(workspace, _matching(900, rng) + _filler(300))
    off_query = max(
        item.record_id for item in loaded if NEEDLE not in item.seed.title
    )
    connection = workspace.holder.connection
    assembly = connection.execute(
        f"SELECT assembly_id FROM {ASSEMBLIES} WHERE governed_record_id = ?", (off_query,)
    ).fetchone()[0]
    request = {"query": NEEDLE, "view": "candidates"}
    assert isinstance(workspace.call("engineering.search", request), esc.SuccessResponseEnvelope)
    with epp._guards_lifted(connection, epp.UPDATE_GUARD):
        epp._damage(
            connection, f"UPDATE {PROJECTION} SET projection_version = 2 WHERE assembly_id = '{assembly}'"
        )
    assert workspace.refused("engineering.search", request)[0] == "stale_projection"
    with epp._guards_lifted(connection, epp.UPDATE_GUARD):
        epp._damage(
            connection,
            f"UPDATE {PROJECTION} SET projection_version = 1, content_digest = "
            f"'sha256:' || printf('%064d', 7) WHERE assembly_id = '{assembly}'",
        )
    assert workspace.refused("engineering.search", request)[0] == "stale_projection"
    with epp._guards_lifted(connection, epp.DELETE_GUARD):
        epp._damage(connection, f"DELETE FROM {PROJECTION} WHERE assembly_id = '{assembly}'")
    assert workspace.refused("engineering.search", request)[0] == "projection_unavailable"


# --- context build over the same candidates ----------------------------------------------


def test_context_build_selects_the_oracles_top_candidates_across_pages(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`engineering.context.build` pages the same narrowed ids and ranks per page and
    then overall: over two pages of candidates it selects the oracle's top ranks, in the
    oracle's order, and never authorises more than a page of ids at once."""
    workspace.record(esc._source(1, "esnap-a", esc.FILES_A))
    rng = random.Random(9)
    loaded = load(workspace, _matching(700, rng) + _filler(200))
    sizes: list[int] = []
    frontier = epp.handlers.read_authorized_memory_frontier

    def spy(*args: Any, **kwargs: Any) -> Any:
        sizes.append(len(kwargs["record_ids"]))
        return frontier(*args, **kwargs)

    monkeypatch.setattr(epp.handlers, "read_authorized_memory_frontier", spy)
    built = workspace.ok(
        "engineering.context.build",
        {
            "query": NEEDLE,
            "targets": [
                {
                    "repository_id": esc.REPOSITORY,
                    "snapshot_id": "esnap-a",
                    "snapshot_kind": "git_commit",
                }
            ],
            "profile": "investigate",
        },
    )
    assert sizes and max(sizes) <= AUTHORIZED_FRONTIER_PAGE_SIZE
    assert sum(sizes) >= 700  # the matching records, authorised page by page
    by_record = {item.record_id: item.seed for item in loaded}
    expected = [
        f"{by_record[record].title}. {by_record[record].preview}" for record in oracle(loaded, NEEDLE)
    ]
    selected = [section["content"] for section in built["pack"]["sections"]]
    assert selected
    assert selected == expected[: len(selected)]


# --- each candidate is normalised once -----------------------------------------------------


def test_a_candidate_is_normalised_once_however_many_rules_ask() -> None:
    candidates = [epp._candidate(f"rec-{n:03d}", f"Alpha {n} ＮＥＥＤＬＥ") for n in range(200)]
    calls: list[str] = []
    real = engineering_preview.normalize_query

    def counting(text: str) -> str:
        calls.append(text)
        return real(text)

    original = engineering_preview.normalize_query
    engineering_preview.normalize_query = counting  # type: ignore[assignment]
    try:
        first = engineering_preview.rank_previews(candidates, "needle")
        second = engineering_preview.rank_previews(candidates, "NEEDLE")
        texts = [engineering_preview.preview_search_text(candidate) for candidate in candidates]
    finally:
        engineering_preview.normalize_query = original  # type: ignore[assignment]
    assert first == second and len(first) == 200
    assert all("needle" in text for text in texts)
    candidate_texts = [text for text in calls if text.startswith("Alpha")]
    assert len(candidate_texts) == len(candidates)  # once each, across three uses
    assert len(calls) == len(candidates) + 2  # and the query once per ranking


def test_a_search_normalises_each_candidate_once(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = random.Random(11)
    load(workspace, _matching(300, rng) + _filler(60))
    calls: list[str] = []
    real = engineering_preview.normalize_query
    monkeypatch.setattr(
        engineering_preview, "normalize_query", lambda text: (calls.append(text), real(text))[1]
    )
    walk(workspace, NEEDLE)
    pages = -(-300 // 100)  # each continuation re-ranks the pinned set
    per_candidate = [text for text in calls if text.startswith("Finding ")]
    assert len(per_candidate) <= 300 * pages


# --- the shape of the work, in SQLite's own units ------------------------------------------


def _frontier_steps(workspace: Workspace) -> int:
    """VM steps (thousands) of authorising one full page of narrowed ids."""
    connection = workspace.holder.connection
    ids = narrowed(workspace, NEEDLE)
    assert ids is not None
    page = ids[:AUTHORIZED_FRONTIER_PAGE_SIZE]
    with memory.read_snapshot(connection), vm_steps(connection) as steps:
        frontier = memory.read_authorized_memory_frontier(
            connection,
            workspace_id=WORKSPACE_ID,
            resolution_instant_us=2**62,
            view="candidates",
            label_grant=OWNER_GRANT,
            domain_scope=engineering_preview.OBSERVATION_DOMAIN,
            record_ids=page,
        )
    assert len(frontier.versions) == len(page)
    return steps[0]


def test_authorising_a_page_costs_the_same_at_any_corpus_size(tmp_path: Path) -> None:
    """The page of ids is looked up, not found by scanning the workspace: the
    instructions an authorised page costs do not grow with the corpus around it. (A
    full scan of the assemblies per page, which SQLite chooses without statistics unless
    the statement orders by the record id, is four times this at four times the corpus.)"""
    steps = {}
    for size in (1_000, 4_000):
        local = another(tmp_path, f"w{size}")
        try:
            rng = random.Random(size)
            load(local, _matching(300, rng) + _filler(size - 300))
            steps[size] = _frontier_steps(local)
        finally:
            local.holder.connection.close()
    assert steps[4_000] <= steps[1_000] * 1.5 + 2, steps


#: The whole narrowed read may cost this many times one pass over the projection rows
#: (counted in the same process, so the unit cancels SQLite builds) and, at four times the
#: corpus, this many times the work. Measured: about 8 and 3.3. The read this replaced,
#: which scanned every assembly of the domain and built the searched text three times
#: per row, cost 14 and 3.8.
SCAN_MULTIPLE_BUDGET = 11.0
LINEAR_GROWTH_BUDGET = 4.8


def test_the_narrowed_read_is_linear_in_the_corpus_within_a_budget(tmp_path: Path) -> None:
    steps: dict[int, int] = {}
    scans: dict[int, int] = {}
    for size in (2_000, 8_000):
        local = another(tmp_path, f"w{size}")
        try:
            rng = random.Random(size)
            load(local, _matching(300, rng) + _filler(size - 300))
            connection = local.holder.connection
            with vm_steps(connection) as counter:
                found = local.ok("engineering.search", {"query": NEEDLE, "view": "candidates"})
            assert len(found["previews"]) == 20
            steps[size] = counter[0]
            with vm_steps(connection) as scan:
                connection.execute(
                    f"SELECT count(*), sum(length(title) + length(preview)) FROM {PROJECTION}"
                ).fetchall()
            scans[size] = scan[0]
        finally:
            local.holder.connection.close()
    assert steps[8_000] <= steps[2_000] * LINEAR_GROWTH_BUDGET, steps
    assert steps[8_000] <= scans[8_000] * SCAN_MULTIPLE_BUDGET, (steps, scans)


# --- the reproducible benchmark (not part of the suite's run) ---------------------------------


@pytest.mark.skipif(
    not os.environ.get("OMNIVIA_ENGINEERING_SEARCH_BENCH"),
    reason="set OMNIVIA_ENGINEERING_SEARCH_BENCH=<observations> to measure search at scale",
)
def test_search_benchmark(workspace: Workspace) -> None:
    """The qualification corpus's shape at any size: 160 query buckets, every fourth
    version with long text and a wide stored body, every tenth under the open label.
    Prints warm p50/p95 for the owner and for a restricted reader."""
    count = int(os.environ["OMNIVIA_ENGINEERING_SEARCH_BENCH"])
    stems = [f"stem{index:02d}" for index in range(20)]
    components = [f"component-{index:02d}" for index in range(8)]
    seeds = []
    for index in range(count):
        stem, component = stems[index % 20], components[(index % 160) // 20]
        long_text = index % 4 == 1
        seeds.append(
            Seed(
                title=f"{stem} {component} finding {index}",
                preview=(
                    "".join(
                        f"v{hashlib.sha256(f'{index}{n}'.encode()).hexdigest()[:6]} "
                        for n in range(70)
                    )
                    if long_text
                    else f"Summary of {stem} {component} {index}"
                ),
                denied=index % 10 != 0,
                body_pad=4000 if long_text else 500,
            )
        )
    started = time.perf_counter()
    load(workspace, seeds)
    print(f"\nloaded {count} versions in {time.perf_counter() - started:.1f}s")
    reader = esc._reader()
    for label, session in (("owner", None), ("reader", reader)):
        samples = []
        for run in range(25):
            bucket = run * 10 if session is not None else run
            query = f"{stems[bucket % 20]} {components[(bucket % 160) // 20]}"
            began = time.perf_counter()
            result = workspace.ok(
                "engineering.search", {"query": query, "view": "candidates"}, session=session
            )
            samples.append((time.perf_counter() - began) * 1000)
            assert result["previews"]
        samples.sort()
        print(
            f"search {label}: p50 {statistics.median(samples):.0f} ms, "
            f"p95 {samples[int(0.95 * len(samples))]:.0f} ms, n={len(samples)}"
        )
