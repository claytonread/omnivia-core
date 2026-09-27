"""Engineering search previews served without body hydration
(SPEC-CORE-ENGMEM-001 §11.1, AC-033; migration 0053).

`engineering.search` over long observations reads authorised identities, evidence
links, stored digests and bounded projection rows, and nothing else. The proof here
is four independent instruments over the real production surface: the SQL each read
runs (no statement names a body column or the body-reading source view), spies on
every hydration function, the same reads over a workspace whose stored bodies have
been corrupted after the fact, and the projection's own bounds. Authorisation is
shown to precede any projection read, an absent or stale projection is shown to
refuse rather than fall back, and the projection is shown to be written by every
writer of a version and, by migration 0053, backfilled for the versions that predate
it.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_engineering_source_coverage as esc
from omnivia_core_runtime.ownership.fencing import assert_guards_intact
from omnivia_core_runtime.service import ovc1
from omnivia_core_runtime.service.handlers import engineering as handlers
from omnivia_core_runtime.storage import engineering_preview, governed, memory
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    authorised,
    fingerprint_schema,
    foreign_key_check,
    install_authorizer,
    integrity_check,
    open_database,
    split_sql_statements,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    canonical_schema_fingerprint,
    load_migrations,
    read_workspace_state,
)
from omnivia_core_runtime.storage.retrieval import (
    CONFIGURED_LOCAL_OWNER,
    EvidenceLabelGrant,
    governed_order_key,
)

from omnivia_core.contracts.v1 import to_canonical_json

Workspace = esc.Workspace
WORKSPACE_ID = esc.WORKSPACE_ID
MIGRATION_VERSION = 53
MIGRATION_NAME = "0053_engineering_preview_projection.sql"
PROJECTION = "omnivia_engineering_preview_projection"
SOURCE_VIEW = "omnivia_engineering_preview_source"
METADATA_VIEW = "omnivia_authoritative_governed_version_metadata"
AUTHORITATIVE_VIEW = "omnivia_authoritative_governed_versions"
DELETE_GUARD = f"omnivia_guard_{PROJECTION}_delete"
UPDATE_GUARD = f"omnivia_guard_{PROJECTION}_update"

#: The columns that hold a version's body, and the names that only a body read touches.
BODY_COLUMNS = ("content_json", "claim_json", "rationale_json")
BODY_MARKERS = (*BODY_COLUMNS, SOURCE_VIEW)


@pytest.fixture
def workspace(tmp_path: Path) -> Any:
    opened = Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


# --- instruments ---------------------------------------------------------------------


class Trace:
    """Every SQL statement a block runs on a connection, as SQLite executes it, with
    each bound value expanded in place."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.statements: list[str] = []

    def __enter__(self) -> Self:
        self.connection.set_trace_callback(self.statements.append)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.connection.set_trace_callback(None)

    def body_reads(self) -> list[str]:
        return [
            statement
            for statement in self.statements
            if any(marker in statement for marker in BODY_MARKERS)
        ]

    def projection_reads(self) -> list[int]:
        """The positions of the statements that read the projection table."""
        return [
            index
            for index, statement in enumerate(self.statements)
            if PROJECTION in statement and statement.lstrip().upper().startswith("SELECT")
        ]


class Reads:
    """Every column SQLite's own authorizer reports read while a block runs.

    The authorizer sees each column a statement names, including the ones a view
    selects without the outer query ever asking for them, so it is a stricter
    instrument than the statement text. The runtime's own authorizer is put back on
    exit, whatever the block did to it."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.columns: set[tuple[str, str]] = set()

    def __enter__(self) -> Self:
        def record(action: int, table: str | None, column: str | None, *_rest: object) -> int:
            if action == sqlite3.SQLITE_READ and table is not None and column is not None:
                self.columns.add((table, column))
            return sqlite3.SQLITE_OK

        self.connection.set_authorizer(record)
        return self

    def __exit__(self, *_exc: object) -> None:
        install_authorizer(self.connection, allow_mutations=False)

    def body_reads(self) -> set[tuple[str, str]]:
        return {read for read in self.columns if read[1] in BODY_COLUMNS}


def _forbid_hydration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every function that hydrates a governed body raise if it is reached."""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a governed body was hydrated on the search path")

    for module, name in (
        (governed, "hydrate_authorized_governed_record_values"),
        (memory, "hydrate_authorized_governed_record_values"),
        (governed, "read_governed_record_values"),
        (governed, "read_governed_records"),
        (governed, "_hydrate_governed_records"),
        (governed, "_content"),
        (handlers, "read_authorized_memory_snapshot"),
    ):
        monkeypatch.setattr(module, name, refuse)


@contextmanager
def _guards_lifted(connection: sqlite3.Connection, *names: str) -> Iterator[None]:
    """Damage from outside the runtime: lift guards, then restore them verbatim."""
    restore = [
        connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?", (name,)
        ).fetchone()[0]
        for name in names
    ]
    with authorised(connection, ddl=True):
        for name in names:
            connection.execute(f"DROP TRIGGER {name}")
    try:
        yield
    finally:
        with authorised(connection, ddl=True):
            for sql in restore:
                connection.execute(sql)
        assert fingerprint_schema(connection).matches(canonical_schema_fingerprint())


def _damage(connection: sqlite3.Connection, *statements: str) -> None:
    with authorised(connection, mutations=True):
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                connection.execute(statement)
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")


# --- fixtures -------------------------------------------------------------------------

#: Long, astral-plane text: every code point is four UTF-8 bytes, so a cut by bytes
#: and a cut by code points cannot be mistaken for one another.
EMOJI = "\U0001f600"


def _long(
    title: str = "Long provider decision",
    manifest: dict[str, Any] | None = None,
    *,
    evidence: bool | None = None,
    source: dict[str, Any] = esc.EVIDENCE_SOURCE,
    **content: Any,
) -> dict[str, Any]:
    """An observation as long as one practically is: a near-2000-code-point summary and
    what (four bytes a code point) and a further 40 KB of body no preview may carry.

    A manifest makes it a `current_safe` candidate, and needs evidence; evidence alone
    is what puts a version behind an evidence label.
    """
    claim = esc._observation(
        manifest,
        title=title,
        evidence=manifest is not None if evidence is None else evidence,
        source=source,
    )
    claim["content"]["summary"] = "provider " + EMOJI * 1990
    claim["content"]["what"] = "what " + EMOJI * 1990
    claim["content"]["notes"] = "n" * 40_000
    claim["content"].update(content)
    return claim


def _search(workspace: Workspace, **payload: Any) -> dict[str, Any]:
    return workspace.ok("engineering.search", {"query": "provider", **payload})


def _ids(result: dict[str, Any]) -> list[str]:
    return [preview["record_id"] for preview in result["previews"]]


def _assemblies(workspace: Workspace, record_id: str) -> list[str]:
    return [
        row[0]
        for row in workspace.holder.connection.execute(
            "SELECT assembly_id FROM omnivia_governed_version_assemblies "
            "WHERE governed_record_id = ? ORDER BY append_ordinal",
            (record_id,),
        )
    ]


def _row_count(workspace: Workspace) -> int:
    return int(
        workspace.holder.connection.execute(f"SELECT COUNT(*) FROM {PROJECTION}").fetchone()[0]
    )


# --- AC-033: no body is read ---------------------------------------------------------


def test_search_hydrates_no_body_over_long_observations(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both modes, every governed view and every page of a search over long
    observations run no statement that names a body column, and reach no hydration
    function. The bodies here are about 56 KB each; the previews are bounded."""
    workspace.record(esc._source(1, "esnap-a", esc.FILES_A))
    first = workspace.observe(_long("Long provider one", esc._manifest()))
    second = workspace.observe(_long("Long provider two", esc._manifest()))
    accepted = esc._accept(workspace, workspace.observe(_long("Long provider three", esc._manifest())))
    connection = workspace.holder.connection
    _forbid_hydration(monkeypatch)

    def run(**payload: Any) -> dict[str, Any]:
        with Trace(connection) as trace:
            result = _search(workspace, **payload)
        assert trace.body_reads() == [], trace.body_reads()
        # One read of the admitted versions' rows, or none when nothing is admitted.
        assert len(trace.projection_reads()) == (1 if result["previews"] else 0)
        return result

    safe = {
        "applicability_mode": "current_safe",
        "repository_target": {"repository_id": esc.REPOSITORY, "snapshot_id": "esnap-a"},
    }
    diagnostic_candidates = run(view="candidates")
    assert set(_ids(diagnostic_candidates)) == {first["record_id"], second["record_id"]}
    assert _ids(run(view="accepted")) == [accepted["record_id"]]
    assert _ids(run(view="history")) == []
    safe_candidates = run(view="candidates", **safe)
    assert set(_ids(safe_candidates)) == {first["record_id"], second["record_id"]}
    assert {p["applicability"] for p in safe_candidates["previews"]} == {"matched"}
    assert safe_candidates["coverage"] == {"projection": "current", "applicability": "current"}
    assert _ids(run(view="accepted", **safe)) == [accepted["record_id"]]

    # Pagination: the page is cut from the ranked set, so a second page reads no body
    # either, and the two pages are the whole set.
    page_one = run(view="candidates", limit=1)
    token = page_one["page"]["continuation_token"]
    page_two = run(view="candidates", limit=1, page={"continuation_token": token})
    assert page_two["page"] == {}
    assert _ids(page_one) + _ids(page_two) == _ids(diagnostic_candidates)

    # Every preview is bounded and says it was cut; none carries the 40 KB body.
    for result in (diagnostic_candidates, safe_candidates, page_one):
        for preview in result["previews"]:
            assert len(preview["title"]) <= 200
            assert 0 < len(preview["preview"]) <= 480
            assert len(preview["preview"].encode("utf-8")) <= 2048
            assert preview["truncated"] is True
            assert preview["preview"] == ("provider " + EMOJI * 1990)[:480]
            assert "nnnnnnnnnn" not in to_canonical_json(preview)


def test_the_instruments_would_catch_a_body_read(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A control for the tests above: the pack builder does hydrate each admitted
    version's body, so the statement trace names a body column for it and the
    hydration spy is reached. A search that read a body would fail those tests the
    same way."""
    workspace.observe(_long("Long provider decision"))
    build = {"query": "provider", "targets": [], "profile": "investigate"}
    with Trace(workspace.holder.connection) as trace:
        workspace.ok("engineering.context.build", build)
    assert trace.body_reads()
    _forbid_hydration(monkeypatch)
    with pytest.raises(AssertionError, match="hydrated"):
        workspace.call("engineering.context.build", build)


def test_sqlite_reports_no_body_column_read_during_a_search(workspace: Workspace) -> None:
    """SQLite's own authorizer -- which reports every column a statement names, through
    views too -- sees no body column read at any point of a governed search, in either
    mode. A control shows it does report one when a body is read, and that it reports a
    view's body column even when the query never asks for it: which is why the frontier
    reads a view that has none."""
    workspace.record(esc._source(1, "esnap-a", esc.FILES_A))
    workspace.observe(_long("Long provider decision", esc._manifest()))
    esc._accept(workspace, workspace.observe(_long("Long provider accepted", esc._manifest())))
    connection = workspace.holder.connection
    safe = {
        "applicability_mode": "current_safe",
        "repository_target": {"repository_id": esc.REPOSITORY, "snapshot_id": "esnap-a"},
    }
    for payload in (
        {"view": "candidates"},
        {"view": "accepted"},
        {"view": "history"},
        {"view": "candidates", **safe},
        {"view": "accepted", **safe},
    ):
        with Reads(connection) as reads:
            result = _search(workspace, **payload)
        assert reads.body_reads() == set(), (payload, reads.body_reads())
        # The instrument was live: the frontier's identity columns were reported.
        assert ("omnivia_governed_version_assemblies", "content_digest") in reads.columns
        assert result["previews"] or payload["view"] == "history"

    body = ("omnivia_governed_version_assemblies", "content_json")
    with Reads(connection) as reads:
        connection.execute(f"SELECT content_json FROM {AUTHORITATIVE_VIEW}").fetchall()
    assert body in reads.body_reads()
    with Reads(connection) as reads:
        connection.execute(f"SELECT assembly_id FROM {AUTHORITATIVE_VIEW}").fetchall()
    assert reads.body_reads() == {body}
    with Reads(connection) as reads:
        connection.execute(f"SELECT assembly_id, content_digest FROM {METADATA_VIEW}").fetchall()
    assert reads.body_reads() == set()


def test_the_metadata_view_is_the_authoritative_view_without_its_body(
    workspace: Workspace,
) -> None:
    """The view the frontier reads is 0009's authoritative view minus `content_json`:
    the same columns in the same order, and the same rows -- across candidate, proposed,
    accepted, rejected and superseding versions -- so reading it changes no view's
    membership."""
    created = workspace.observe(_long("Long provider decision"))
    accepted = esc._accept(workspace, workspace.observe(_long("Long provider accepted")))
    rejected = workspace.observe(esc._observation(None, title="Provider rejected", evidence=False))
    proposed = workspace.ok(
        "knowledge.propose",
        {"record_id": rejected["record_id"], "rationale": {"reason_code": "review"}},
        mutation_precondition=esc.MutationPrecondition(record_version=rejected["version"]),
    )
    workspace.ok(
        "candidate.reject",
        {"record_id": rejected["record_id"], "rationale": {"reason_code": "review"}},
        mutation_precondition=esc.MutationPrecondition(
            record_version=proposed["updated_record"]["provenance"]["identity"]["version"]
        ),
    )
    fact = {**esc._observation(None, title="Provider fact", evidence=False), "record_type": "memory.fact"}
    fact["content"] = {"fact": "provider facts live here"}
    original = esc._accept(workspace, workspace.observe(fact))
    esc._supersede(workspace, original, {**fact, "content": {"fact": "provider facts moved"}})
    assert created["record_id"] != accepted["record_id"]
    connection = workspace.holder.connection

    def columns(view: str) -> list[str]:
        return [row[1] for row in connection.execute(f"PRAGMA table_info({view})")]

    wide = columns(AUTHORITATIVE_VIEW)
    assert columns(METADATA_VIEW) == [name for name in wide if name != "content_json"]
    assert not any(name.endswith("_json") for name in columns(METADATA_VIEW))
    shared = ", ".join(columns(METADATA_VIEW))
    order = "ORDER BY assembly_id"
    rows = connection.execute(f"SELECT {shared} FROM {AUTHORITATIVE_VIEW} {order}").fetchall()
    assert len(rows) >= 9
    assert rows == connection.execute(f"SELECT {shared} FROM {METADATA_VIEW} {order}").fetchall()


def test_search_never_depends_on_a_stored_body(workspace: Workspace) -> None:
    """The stored bodies are corrupted after the projection was written -- the
    content no longer decodes and the claim lineage is garbage -- and search answers
    exactly as before, while a read that does hydrate a body notices at once. Nothing
    on the search path can have read what it could not have decoded."""
    workspace.record(esc._source(1, "esnap-a", esc.FILES_A))
    long = workspace.observe(_long("Long provider decision", esc._manifest()))
    safe = {
        "applicability_mode": "current_safe",
        "repository_target": {"repository_id": esc.REPOSITORY, "snapshot_id": "esnap-a"},
    }
    before = _search(workspace, view="candidates")
    before_safe = _search(workspace, view="candidates", **safe)
    assert _ids(before) == _ids(before_safe) == [long["record_id"]]
    connection = workspace.holder.connection

    garbage = "x" * 100
    with _guards_lifted(
        connection,
        "omnivia_guard_governed_version_assemblies_update",
        "omnivia_guard_application_claim_lineage_update",
    ):
        _damage(
            connection,
            "UPDATE omnivia_governed_version_assemblies SET content_json = '{not json'",
            "UPDATE omnivia_application_claim_lineage "
            f"SET claim_json = '{garbage}', claim_byte_length = {len(garbage)}",
        )

    assert _search(workspace, view="candidates") == before
    assert _search(workspace, view="candidates", **safe) == before_safe
    # The corruption is real: a read that hydrates the body cannot decode it.
    with pytest.raises(ValueError, match="claim lineage"):
        workspace.call("engineering.expand", {"anchor": long})


def test_a_preview_carries_exactly_its_projection_and_nothing_more(
    workspace: Workspace,
) -> None:
    """The whole preview a fully described observation renders, as the wire holds it:
    the bounded title and preview text, the server-owned governance and evidence
    facts, and each optional field the projection holds -- and no other content."""
    claim = _long(
        "Provider decision",
        kind="decision",
        assertion_basis="observed",
        topic_ref={"proposed_key": "auth/provider"},
        applicability={"repository_id": "erepo-app", "snapshot_id": "esnap-a"},
    )
    record = workspace.observe(claim)
    (preview,) = _search(workspace, view="candidates")["previews"]
    assert preview == {
        "record_id": record["record_id"],
        "version": record["version"],
        "title": "Provider decision",
        "preview": ("provider " + EMOJI * 1990)[:480],
        "truncated": True,
        "governance_state": "candidate",
        "applicability": "not_evaluated",
        "evidence_available": False,
        "observation_kind": "decision",
        "assertion_basis": "observed",
        "topic_key": "auth/provider",
        "repository_id": "erepo-app",
        "snapshot_id": "esnap-a",
    }
    # A short observation is not marked truncated, and a version with evidence says so.
    short = esc._observation(None, title="Provider fact", evidence=True)
    workspace.observe(short)
    by_title = {p["title"]: p for p in _search(workspace, view="candidates")["previews"]}
    assert by_title["Provider fact"]["truncated"] is False
    assert by_title["Provider fact"]["preview"] == "Interactive sign-in uses provider A."
    assert by_title["Provider fact"]["evidence_available"] is True
    assert by_title["Provider decision"]["evidence_available"] is False


def test_a_repository_target_scopes_previews_by_the_projected_repository(
    workspace: Workspace,
) -> None:
    """A diagnostic read scoped to a repository serves the versions that claim it,
    from the projection's repository field: another repository's versions and versions
    that claim none are out of scope. No assessment is stored, so applicability stays
    `not_evaluated`, and no coverage is claimed."""
    claim = {"repository_id": "erepo-1", "snapshot_id": "esnap-1"}
    one = workspace.observe(_long("Provider one", applicability=claim))
    workspace.observe(_long("Provider two", applicability={**claim, "repository_id": "erepo-2"}))
    workspace.observe(_long("Provider unscoped"))
    found = _search(workspace, view="candidates", repository_target=claim)
    assert _ids(found) == [one["record_id"]]
    (preview,) = found["previews"]
    assert (preview["repository_id"], preview["snapshot_id"]) == ("erepo-1", "esnap-1")
    assert preview["applicability"] == "not_evaluated"
    assert found["coverage"] == {"projection": "current", "applicability": "unavailable"}


def test_the_match_surface_is_the_bounded_preview(workspace: Workspace) -> None:
    """Ranking counts the query in the projection's text, so a term that occurs only
    beyond the preview is not matched: finding it is an exact read, which hydrates a
    body. A term in the title, in the preview, in the kind or in the topic key is."""
    deep = _long("Deep tail")
    deep["content"]["summary"] = "s" * 1500 + " zebrafish"
    kind = _long("Kind match", kind="zebrafish_kind")
    topic = _long("Topic match", topic_ref={"proposed_key": "zoo/zebrafish"})
    title = _long("Zebrafish title")
    early = _long("Early match")
    early["content"]["summary"] = "an early zebrafish, then " + "s" * 1900
    ids = {
        name: workspace.observe(claim)["record_id"]
        for name, claim in (
            ("deep", deep),
            ("kind", kind),
            ("topic", topic),
            ("title", title),
            ("early", early),
        )
    }
    found = _ids(_search(workspace, query="zebrafish", view="candidates"))
    assert set(found) == {ids["kind"], ids["topic"], ids["title"], ids["early"]}
    assert ids["deep"] not in found
    # Normalisation is the query's and the text's alike: NFKC and case folding.
    assert set(_ids(_search(workspace, query="ZEBRAFISH", view="candidates"))) == set(found)


# --- authorisation precedes any projection read ------------------------------------


def test_a_denied_version_is_never_named_in_a_projection_read(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seeded evidence carries a label only the owner holds. For another reader
    the label fold runs first, from identities and links, and the projection is then
    read for the admitted assemblies only: the denied assembly is never selected,
    scored or counted, and the reader learns nothing of it. The owner sees both."""
    m2.write(workspace.holder, m2.EVIDENCE, evidence_id="evd-open", source_native_id="doc-open")
    open_source = {**esc.EVIDENCE_SOURCE, "source_id": "doc-open"}
    hidden = workspace.observe(_long("XYZZY provider provider provider provider", evidence=True))
    visible = workspace.observe(_long("Open provider one", evidence=True, source=open_source))
    (hidden_assembly,) = _assemblies(workspace, hidden["record_id"])
    (visible_assembly,) = _assemblies(workspace, visible["record_id"])
    connection = workspace.holder.connection
    _forbid_hydration(monkeypatch)
    reader = esc._reader()

    with Trace(connection) as trace:
        seen = workspace.ok(
            "engineering.search",
            {"query": "provider", "view": "candidates"},
            session=reader,
        )
    assert _ids(seen) == [visible["record_id"]]
    assert seen["page"] == {}
    assert "XYZZY" not in to_canonical_json(seen)
    assert trace.body_reads() == []
    (position,) = trace.projection_reads()
    projection_read = trace.statements[position]
    assert visible_assembly in projection_read
    assert hidden_assembly not in projection_read
    # The label fold ran before the projection was read, and read no preview.
    folds = [
        index
        for index, statement in enumerate(trace.statements)
        if "omnivia_evidence_permission_labels" in statement
    ]
    assert folds and max(folds) < position
    assert not any(
        hidden_assembly in statement and PROJECTION in statement
        for statement in trace.statements
    )

    owner = workspace.ok("engineering.search", {"query": "provider", "view": "candidates"})
    assert set(_ids(owner)) == {hidden["record_id"], visible["record_id"]}
    assert _ids(owner)[0] == hidden["record_id"]


def test_the_scorer_sees_only_admitted_previews(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rank_previews` is handed the admitted, filtered candidates and nothing else:
    not a denied version, and not a hypothesis under `accepted`."""
    m2.write(workspace.holder, m2.EVIDENCE, evidence_id="evd-open", source_native_id="doc-open")
    open_source = {**esc.EVIDENCE_SOURCE, "source_id": "doc-open"}
    hidden = workspace.observe(_long("XYZZY provider hidden", evidence=True))
    plain = esc._accept(
        workspace,
        workspace.observe(_long("Open provider plain", evidence=True, source=open_source)),
    )
    hypothesis = esc._accept(
        workspace,
        workspace.observe(
            _long(
                "Open provider guess",
                evidence=True,
                source=open_source,
                assertion_basis="hypothesis",
            )
        ),
    )
    hidden_accepted = esc._accept(
        workspace, workspace.observe(_long("XYZZY provider accepted", evidence=True))
    )
    ranked: list[str] = []
    rank = handlers.rank_previews

    def spy(candidates: Any, *args: Any, **kwargs: Any) -> Any:
        ranked.extend(candidate.record_id for candidate in candidates)
        return rank(candidates, *args, **kwargs)

    monkeypatch.setattr(handlers, "rank_previews", spy)
    reader = esc._reader()
    accepted = workspace.ok(
        "engineering.search", {"query": "provider", "view": "accepted"}, session=reader
    )
    # §8.2: the accepted hypothesis never reaches the scorer under `accepted`, and a
    # denied record never reaches it under any view.
    assert _ids(accepted) == [plain["record_id"]]
    assert ranked == [plain["record_id"]]
    ranked.clear()
    candidates = workspace.ok(
        "engineering.search", {"query": "provider", "view": "candidates"}, session=reader
    )
    assert _ids(candidates) == []
    assert ranked == []
    denied = {hidden["record_id"], hidden_accepted["record_id"]}
    assert denied.isdisjoint(ranked) and hypothesis["record_id"] not in _ids(accepted)
    # The owner holds the label, and a hypothesis is still excluded from `accepted`.
    ranked.clear()
    owner = workspace.ok("engineering.search", {"query": "provider", "view": "accepted"})
    assert set(_ids(owner)) == {plain["record_id"], hidden_accepted["record_id"]}
    assert hypothesis["record_id"] not in ranked


# --- pagination and the cursor ---------------------------------------------------------


def test_the_cursor_is_bound_to_the_ranked_versions_and_their_content(
    workspace: Workspace,
) -> None:
    """A continuation resumes the same ranked set, and is refused -- as an explicit
    restart -- once its scope or the ranked order has changed."""
    records = [
        workspace.observe(esc._observation(None, title=f"Provider note {index}", evidence=False))
        for index in range(5)
    ]
    full = _ids(_search(workspace, view="candidates"))
    assert set(full) == {record["record_id"] for record in records}

    # Pages of two are the full ranking, once each, in order.
    collected: list[str] = []
    payload: dict[str, Any] = {"view": "candidates", "limit": 2}
    while True:
        page = _search(workspace, **payload)
        collected += _ids(page)
        if not page["page"]:
            break
        payload["page"] = page["page"]
    assert collected == full

    token = _search(workspace, view="candidates", limit=2)["page"]["continuation_token"]

    def resume(**overrides: Any) -> Any:
        request: dict[str, Any] = {
            "query": "provider",
            "view": "candidates",
            "limit": 2,
            "page": {"continuation_token": token},
        }
        request.update(overrides)
        return workspace.call("engineering.search", request)

    # Unchanged, the pinned snapshot resumes; a different query, view or limit is
    # another scope and the token is refused.
    assert isinstance(resume(), esc.SuccessResponseEnvelope)
    for scope in ({"query": "note"}, {"view": "accepted"}, {"limit": 3}):
        assert isinstance(resume(**scope), esc.ErrorResponseEnvelope), scope
        assert resume(**scope).error.code == "invalid_request"

    # A later write is outside the pinned snapshot, so it neither breaks nor joins the
    # pages already promised; a fresh read sees it.
    workspace.observe(esc._observation(None, title="Provider note six", evidence=False))
    assert isinstance(resume(), esc.SuccessResponseEnvelope)
    assert len(_ids(_search(workspace, view="candidates"))) == 6

    # A change to the ranked order -- a preference for the version that ranked last --
    # is a different snapshot, and the pinned page restarts.
    (last,) = [record for record in records if record["record_id"] == full[-1]]
    workspace.ok("context.priority.set", {"target": last, "priority": "preferred"})
    refused = resume()
    assert isinstance(refused, esc.ErrorResponseEnvelope)
    assert refused.error.code == "invalid_request"


def test_the_legacy_snapshot_stays_off_the_0053_view_and_the_frontier_stays_on_it(
    workspace: Workspace,
) -> None:
    """The legacy memory snapshot must run on schemas that predate 0053, so it never
    names the metadata view; the search path's frontier always does."""
    workspace.observe(esc._observation(None, title="Provider note", evidence=False))
    connection = workspace.holder.connection
    kwargs: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "resolution_instant_us": 2**62,
        "view": "candidates",
        "label_grant": EvidenceLabelGrant(
            principal_id=CONFIGURED_LOCAL_OWNER,
            workspace_id=WORKSPACE_ID,
            all_labels=True,
            labels=frozenset(),
        ),
    }
    with Trace(connection) as legacy:
        snapshot = memory.read_authorized_memory_snapshot(connection, **kwargs)
    assert snapshot.values
    assert not any(METADATA_VIEW in statement for statement in legacy.statements)
    with Trace(connection) as search:
        frontier = memory.read_authorized_memory_frontier(connection, **kwargs)
    assert frontier.versions
    assert any(METADATA_VIEW in statement for statement in search.statements)
    assert search.body_reads() == []


def test_the_snapshot_binds_the_stored_content_digest(workspace: Workspace) -> None:
    """The digest a continuation is bound to is built from each ranked version's
    stored content digest: a version whose recorded content digest differs is another
    snapshot, so a pinned page restarts. It names the content without reading it."""
    for index in range(3):
        workspace.observe(esc._observation(None, title=f"Provider note {index}", evidence=False))
    connection = workspace.holder.connection
    token = _search(workspace, view="candidates", limit=1)["page"]["continuation_token"]
    request = {
        "query": "provider",
        "view": "candidates",
        "limit": 1,
        "page": {"continuation_token": token},
    }
    assert isinstance(workspace.call("engineering.search", request), esc.SuccessResponseEnvelope)

    other = "sha256:" + "e" * 64
    with _guards_lifted(
        connection, "omnivia_guard_governed_version_assemblies_update", UPDATE_GUARD
    ):
        _damage(
            connection,
            f"UPDATE omnivia_governed_version_assemblies SET content_digest = '{other}'",
            f"UPDATE {PROJECTION} SET content_digest = '{other}'",
        )
    refused = workspace.call("engineering.search", request)
    assert isinstance(refused, esc.ErrorResponseEnvelope)
    assert refused.error.code == "invalid_request"


# --- bounds ------------------------------------------------------------------------------


def test_a_response_never_exceeds_its_byte_cap_and_pages_continue(
    workspace: Workspace,
) -> None:
    """Forty maximal previews are about 120 KB; a page of 100 is cut at 64 KiB and
    continues, so no response passes the cap and no version is lost or repeated."""
    long_title = "provider " + EMOJI * 191
    assert len(long_title) == 200
    claims = []
    for index in range(40):
        claim = _long(long_title)
        claim["content"]["summary"] = f"provider {index:02d} " + EMOJI * 1900
        claims.append(claim)
    expected = {workspace.observe(claim)["record_id"] for claim in claims}

    seen: list[str] = []
    payload: dict[str, Any] = {"view": "candidates", "limit": 100}
    pages = 0
    while True:
        response = workspace.call("engineering.search", {"query": "provider", **payload})
        assert isinstance(response, esc.SuccessResponseEnvelope)
        result = response.to_wire()["result"]
        # The whole frame the client receives, not just the result, is inside the cap.
        assert len(ovc1.canonical_json_bytes(response.to_wire())) <= handlers.RESPONSE_MAX_BYTES
        assert len(to_canonical_json(result).encode("utf-8")) <= handlers.RESPONSE_MAX_BYTES
        for preview in result["previews"]:
            assert len(preview["title"]) <= 200
            assert len(preview["preview"]) <= 480
            assert len(preview["preview"].encode("utf-8")) <= 2048
        seen += _ids(result)
        pages += 1
        if not result["page"]:
            break
        assert result["previews"], "a page always holds one preview"
        payload["page"] = result["page"]
    assert pages > 1
    assert len(seen) == len(set(seen)) == 40
    assert set(seen) == expected


def test_the_response_cap_cuts_any_page_and_keeps_at_least_one_preview() -> None:
    """The cap is applied to every page of every view family, from the previews a page
    holds: a page that fits is untouched, one that does not keeps its leading previews
    that do, and a single preview is never cut away."""
    preview = {"record_id": "rec-1", "version": "v1", "title": "t", "preview": "p" * 2000}
    size = len(to_canonical_json(preview).encode("utf-8")) + 1
    fits = (handlers.RESPONSE_MAX_BYTES - handlers._RESPONSE_RESERVE) // size
    page = [dict(preview, record_id=f"rec-{index}") for index in range(fits + 25)]
    kept = handlers._within_response_cap(page)
    assert kept == page[:fits] and 0 < fits < len(page)
    assert handlers._within_response_cap(page[:fits]) == page[:fits]
    huge = dict(preview, preview="p" * (handlers.RESPONSE_MAX_BYTES * 2))
    assert handlers._within_response_cap([huge, preview]) == [huge]
    assert handlers._within_response_cap([]) == []


def test_the_page_limit_is_clamped_to_its_hard_maximum(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview list is bounded: a limit above the hard maximum pages at the
    maximum, and a request without one pages at the default."""
    for index in range(3):
        workspace.observe(esc._observation(None, title=f"Provider note {index}", evidence=False))
    assert handlers.SEARCH_MAX_LIMIT == 100 and handlers.SEARCH_DEFAULT_LIMIT == 20
    monkeypatch.setattr(handlers, "SEARCH_MAX_LIMIT", 2)
    clamped = _search(workspace, view="candidates", limit=50)
    assert len(clamped["previews"]) == 2 and clamped["page"]
    monkeypatch.setattr(handlers, "SEARCH_MAX_LIMIT", 100)
    monkeypatch.setattr(handlers, "SEARCH_DEFAULT_LIMIT", 1)
    assert len(_search(workspace, view="candidates")["previews"]) == 1


# --- the ranking rule --------------------------------------------------------------------


def _candidate(
    record_id: str,
    title: str,
    *,
    version: str = "v1",
    recorded_at_us: int = 1,
    preview: str | None = None,
    kind: str | None = None,
    topic_key: str | None = None,
) -> engineering_preview.PreviewCandidate:
    return engineering_preview.PreviewCandidate(
        assembly_id=f"asm-{record_id}-{version}",
        record_id=record_id,
        version=version,
        recorded_at_us=recorded_at_us,
        governance_state="candidate",
        evidence_disposition="unavailable",
        evidence_available=False,
        content_digest="sha256:" + "0" * 64,
        title=title,
        preview=title if preview is None else preview,
        truncated=False,
        observation_kind=kind,
        assertion_basis=None,
        topic_key=topic_key,
        repository_id=None,
        snapshot_id=None,
    )


def test_ranking_is_the_governed_rule_over_preview_text() -> None:
    """Occurrences of the normalised query, then recency, then identity: the governed
    order key itself, over the text of the bounded preview."""
    candidates = [
        _candidate("rec-b", "alpha", recorded_at_us=5),
        _candidate("rec-a", "alpha", recorded_at_us=5),
        _candidate("rec-c", "alpha alpha", recorded_at_us=1),
        _candidate("rec-d", "beta", recorded_at_us=9),
        _candidate("rec-e", "x", preview="ALPHA alpha alpha", recorded_at_us=2),
        _candidate("rec-f", "x", version="v2", recorded_at_us=5, kind="alpha"),
        _candidate("rec-f", "x", version="v1", recorded_at_us=5, kind="alpha"),
        _candidate("rec-g", "x", topic_key="alpha/beta", recorded_at_us=5),
    ]
    ordered = engineering_preview.rank_previews(candidates, "AlPhA")
    hits = {
        (c.record_id, c.version): engineering_preview.preview_search_text(c).count("alpha")
        for c in candidates
    }
    expected = sorted(
        (c for c in candidates if hits[(c.record_id, c.version)]),
        key=lambda c: governed_order_key(c, hits[(c.record_id, c.version)]),  # type: ignore[arg-type]
    )
    assert list(ordered) == expected
    assert [(c.record_id, c.version) for c in ordered] == [
        ("rec-c", "v1"),  # four occurrences (title and preview), though the oldest
        ("rec-e", "v1"),  # three
        ("rec-a", "v1"),  # two, newest: ties break by record id ...
        ("rec-b", "v1"),
        ("rec-f", "v1"),  # one, newest: ties break by record id, then version
        ("rec-f", "v2"),
        ("rec-g", "v1"),
    ]
    assert "rec-d" not in {c.record_id for c in ordered}
    # An empty query matches nothing, and no candidate is ranked twice or invented.
    assert engineering_preview.rank_previews(candidates, "") == ()
    assert engineering_preview.rank_previews([], "alpha") == ()


# --- absent and stale projection state fail closed -------------------------------------


def test_an_absent_projection_row_is_refused_never_answered_from_the_body(
    workspace: Workspace,
) -> None:
    """A version the grant admits with no projection row is `projection_unavailable`,
    retryable, and no body is read to answer around it. The maintenance path restores
    the row, and the search then serves it."""
    kept = workspace.observe(esc._observation(None, title="Provider kept", evidence=False))
    lost = workspace.observe(esc._observation(None, title="Provider lost", evidence=False))
    connection = workspace.holder.connection
    (lost_assembly,) = _assemblies(workspace, lost["record_id"])
    assert set(_ids(_search(workspace, view="candidates"))) == {
        kept["record_id"],
        lost["record_id"],
    }

    with _guards_lifted(connection, DELETE_GUARD):
        _damage(connection, f"DELETE FROM {PROJECTION} WHERE assembly_id = '{lost_assembly}'")
    with Trace(connection) as trace:
        code, message, retry = workspace.refused(
            "engineering.search", {"query": "provider", "view": "candidates"}
        )
    assert (code, retry) == ("projection_unavailable", "retryable_after_delay")
    assert lost_assembly not in message and lost["record_id"] not in message
    assert trace.body_reads() == []

    with esc._fenced(workspace):
        assert engineering_preview.rebuild_missing_previews(connection) == 1
    assert set(_ids(_search(workspace, view="candidates"))) == {
        kept["record_id"],
        lost["record_id"],
    }
    # Nothing is missing any more, so a rebuild adds nothing.
    with esc._fenced(workspace):
        assert engineering_preview.rebuild_missing_previews(connection) == 0


def test_a_damaged_off_query_projection_still_refuses_under_current_safe(
    workspace: Workspace,
) -> None:
    """The query pre-filter that keeps the `current_safe` cap bounded is a
    ranking-time convenience over admitted candidates, never an authorization or
    a projection-integrity shortcut: an admitted version with no projection row
    still refuses the whole read, even though its preview text never contains
    the query and it would otherwise never reach the evaluator or the cap."""
    workspace.record(esc._source(1, "esnap-a", esc.FILES_A))
    off_query = esc._observation(esc._manifest(), title="Auth decision")
    off_query["content"]["summary"] = "Nothing about the search word here."
    off_query["content"]["what"] = "Still nothing to see here."
    off_query_record = workspace.observe(off_query)
    matched = workspace.observe(
        esc._observation(esc._manifest(), title="Sign-in provider decision two")
    )
    (off_query_assembly,) = _assemblies(workspace, off_query_record["record_id"])
    connection = workspace.holder.connection
    safe = {
        "applicability_mode": "current_safe",
        "repository_target": {"repository_id": esc.REPOSITORY, "snapshot_id": "esnap-a"},
    }
    assert set(_ids(_search(workspace, view="candidates", **safe))) == {matched["record_id"]}

    with _guards_lifted(connection, DELETE_GUARD):
        _damage(
            connection, f"DELETE FROM {PROJECTION} WHERE assembly_id = '{off_query_assembly}'"
        )
    code, message, retry = workspace.refused(
        "engineering.search", {"query": "provider", "view": "candidates", **safe}
    )
    assert (code, retry) == ("projection_unavailable", "retryable_after_delay")
    assert off_query_assembly not in message and off_query_record["record_id"] not in message


def test_a_stale_projection_row_is_refused(workspace: Workspace) -> None:
    """A row derived from other content, or of another projection version, is
    `stale_projection`: the read refuses rather than serve rules or text the version
    no longer has, and falls back to nothing."""
    record = workspace.observe(esc._observation(None, title="Provider one", evidence=False))
    workspace.observe(esc._observation(None, title="Provider two", evidence=False))
    connection = workspace.holder.connection
    (assembly,) = _assemblies(workspace, record["record_id"])
    request = {"query": "provider", "view": "candidates"}

    other_digest = "sha256:" + "f" * 64
    with _guards_lifted(connection, UPDATE_GUARD):
        _damage(
            connection,
            f"UPDATE {PROJECTION} SET content_digest = '{other_digest}' "
            f"WHERE assembly_id = '{assembly}'",
        )
    with Trace(connection) as trace:
        code, _message, retry = workspace.refused("engineering.search", request)
    assert (code, retry) == ("stale_projection", "retryable_after_delay")
    assert trace.body_reads() == []

    # Another projection version: the version this build reads is absent, the row
    # that is present is not current.
    with _guards_lifted(connection, UPDATE_GUARD):
        _damage(
            connection,
            f"UPDATE {PROJECTION} SET projection_version = 2 WHERE assembly_id = '{assembly}'",
        )
    assert workspace.refused("engineering.search", request)[0] == "stale_projection"

    # Restoring the row (the current version, the version's own digest) serves again.
    with _guards_lifted(connection, UPDATE_GUARD):
        _damage(
            connection,
            f"UPDATE {PROJECTION} SET projection_version = 1, content_digest = "
            "(SELECT content_digest FROM omnivia_governed_version_assemblies "
            f" WHERE assembly_id = '{assembly}') WHERE assembly_id = '{assembly}'",
        )
    assert len(_ids(_search(workspace, view="candidates"))) == 2


def test_the_projection_is_append_only_even_for_the_service_writer(
    workspace: Workspace,
) -> None:
    record = workspace.observe(esc._observation(None, title="Provider one", evidence=False))
    (assembly,) = _assemblies(workspace, record["record_id"])
    connection = workspace.holder.connection
    for statement, message in (
        (f"UPDATE {PROJECTION} SET title = 'x' WHERE assembly_id = '{assembly}'", "UPDATE"),
        (f"DELETE FROM {PROJECTION} WHERE assembly_id = '{assembly}'", "DELETE"),
    ):
        with (
            pytest.raises(sqlite3.DatabaseError, match=f"append-only; {message}"),
            esc._fenced(workspace),
        ):
            connection.execute(statement)
    # An insert outside a fenced writer is refused as unguarded.
    with pytest.raises(sqlite3.DatabaseError):
        connection.execute(
            f"INSERT INTO {PROJECTION} SELECT * FROM {PROJECTION} WHERE assembly_id = ?",
            (assembly,),
        )


def test_the_insert_guard_admits_only_the_derivation_of_the_assembly(
    workspace: Workspace,
) -> None:
    """A fenced writer can still not leave a row that describes other text, other
    content or another version: the guard re-derives the row from its own assembly and
    refuses any difference, so a forged or drifted row cannot exist."""
    record = workspace.observe(esc._observation(None, title="Provider one", evidence=False))
    other = workspace.observe(esc._observation(None, title="Provider two", evidence=False))
    (assembly,) = _assemblies(workspace, record["record_id"])
    (other_assembly,) = _assemblies(workspace, other["record_id"])
    connection = workspace.holder.connection
    derived = (
        "SELECT workspace_id, assembly_id, projection_version, content_digest, title, "
        "preview, truncated, observation_kind, assertion_basis, topic_key, repository_id, "
        f"snapshot_id FROM {SOURCE_VIEW} WHERE assembly_id = ?"
    )
    columns = (
        "workspace_id, assembly_id, projection_version, content_digest, title, preview, "
        "truncated, observation_kind, assertion_basis, topic_key, repository_id, snapshot_id"
    )

    def forge(**changes: Any) -> None:
        row = dict(
            zip(columns.split(", "), connection.execute(derived, (assembly,)).fetchone(), strict=True)
        )
        row.update(changes)
        connection.execute(
            f"INSERT INTO {PROJECTION} ({columns}) VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )

    with _guards_lifted(connection, DELETE_GUARD):
        _damage(connection, f"DELETE FROM {PROJECTION} WHERE assembly_id = '{assembly}'")
    refusal = "derivation of its own assembly"
    for changes in (
        {"title": "Another title"},
        {"preview": "another preview"},
        {"truncated": 1},
        {"content_digest": "sha256:" + "d" * 64},
        {"observation_kind": "forged_kind"},
        {"topic_key": "forged/topic"},
        {"repository_id": "erepo-forged"},
        {"projection_version": 2},
        {"assembly_id": "asm-nowhere"},
        {"assembly_id": other_assembly},
    ):
        with pytest.raises(sqlite3.DatabaseError, match=refusal), esc._fenced(workspace):
            forge(**changes)
    with esc._fenced(workspace):
        forge()
    assert _ids(_search(workspace, view="candidates")) == [other["record_id"], record["record_id"]]


# --- the projection is maintained for every writer ------------------------------------


def _projection_rows(
    workspace: Workspace, assemblies: list[str]
) -> dict[str, tuple[Any, ...]]:
    marks = ", ".join("?" for _ in assemblies)
    return {
        row[0]: tuple(row[1:])
        for row in workspace.holder.connection.execute(
            "SELECT assembly_id, projection_version, content_digest, title, preview, "
            "truncated, observation_kind, assertion_basis, topic_key, repository_id, "
            f"snapshot_id FROM {PROJECTION} WHERE assembly_id IN ({marks})",
            assemblies,
        )
    }


def test_the_projection_follows_every_writer_of_a_version(workspace: Workspace) -> None:
    """`memory.create` and each governance transition that copies content into a new
    exact version write an assembly, and each gets its own projection row in the same
    transaction: the proposal, the version `knowledge.propose` mints and the one
    `candidate.approve` mints. Each row is derived from its own assembly's digest, and
    the reads of every view serve the version that is current in it."""
    created = workspace.observe(_long("Long provider decision"))
    proposed = workspace.ok(
        "knowledge.propose",
        {"record_id": created["record_id"], "rationale": {"reason_code": "review"}},
        mutation_precondition=esc.MutationPrecondition(record_version=created["version"]),
    )
    proposed_version = proposed["updated_record"]["provenance"]["identity"]["version"]
    assert _search(workspace, view="accepted")["previews"] == []
    assert _ids(_search(workspace, view="candidates")) == [created["record_id"]]
    (candidate,) = _search(workspace, view="candidates")["previews"]
    assert (candidate["version"], candidate["governance_state"]) == (
        proposed_version,
        "candidate",
    )
    approved = workspace.ok(
        "candidate.approve",
        {"record_id": created["record_id"], "rationale": {"reason_code": "review"}},
        mutation_precondition=esc.MutationPrecondition(record_version=proposed_version),
    )
    approved_version = approved["updated_record"]["provenance"]["identity"]["version"]

    assemblies = _assemblies(workspace, created["record_id"])
    assert len(assemblies) == 3
    rows = _projection_rows(workspace, assemblies)
    assert set(rows) == set(assemblies)
    digests = {
        row[0]: row[1]
        for row in workspace.holder.connection.execute(
            "SELECT assembly_id, content_digest FROM omnivia_governed_version_assemblies "
            "WHERE governed_record_id = ?",
            (created["record_id"],),
        )
    }
    for assembly, row in rows.items():
        assert row[0] == engineering_preview.PROJECTION_VERSION
        assert row[1] == digests[assembly]
    # Content is copied byte for byte, so the three rows describe one preview.
    assert len({row[1:] for row in rows.values()}) == 1

    (accepted,) = _search(workspace, view="accepted")["previews"]
    assert (accepted["version"], accepted["governance_state"]) == (approved_version, "accepted")
    assert accepted["preview"] == candidate["preview"]
    assert _search(workspace, view="candidates")["previews"] == []
    assert _search(workspace, view="history")["previews"] == []


def test_a_rejected_version_and_a_superseding_version_are_projected_too(
    workspace: Workspace,
) -> None:
    """The remaining writers of a version -- `candidate.reject` and `record.supersede`
    -- project the assembly each inserts, so the projection has no version to miss."""
    rejected = workspace.observe(esc._observation(None, title="Provider rejected", evidence=False))
    proposed = workspace.ok(
        "knowledge.propose",
        {"record_id": rejected["record_id"], "rationale": {"reason_code": "review"}},
        mutation_precondition=esc.MutationPrecondition(record_version=rejected["version"]),
    )
    proposed_version = proposed["updated_record"]["provenance"]["identity"]["version"]
    workspace.ok(
        "candidate.reject",
        {"record_id": rejected["record_id"], "rationale": {"reason_code": "review"}},
        mutation_precondition=esc.MutationPrecondition(record_version=proposed_version),
    )
    fact = {
        **esc._observation(None, title="Provider fact", evidence=False),
        "record_type": "memory.fact",
    }
    fact["content"] = {"fact": "provider facts live here"}
    original = esc._accept(workspace, workspace.observe(fact))
    replacement = {**fact, "content": {"fact": "provider facts moved"}}
    esc._supersede(workspace, original, replacement)

    for record_id in (rejected["record_id"], original["record_id"]):
        assemblies = _assemblies(workspace, record_id)
        assert len(assemblies) >= 3
        assert set(_projection_rows(workspace, assemblies)) == set(assemblies)
    total = workspace.holder.connection.execute(
        "SELECT COUNT(*) FROM omnivia_governed_version_assemblies "
        "WHERE domain_scope = 'engineering.codebase'"
    ).fetchone()[0]
    assert _row_count(workspace) == total


def test_only_engineering_domain_versions_have_a_projection_row(workspace: Workspace) -> None:
    other = esc._observation(None, title="Provider note", evidence=False)
    other = {**other, "record_type": "memory.fact", "domain_scope": "workspace.notes"}
    other["content"] = {"fact": "provider notes live in the wiki"}
    fact = workspace.observe(other)
    kept = workspace.observe(esc._observation(None, title="Provider kept", evidence=False))
    (fact_assembly,) = _assemblies(workspace, fact["record_id"])
    (kept_assembly,) = _assemblies(workspace, kept["record_id"])
    assert set(_projection_rows(workspace, [fact_assembly, kept_assembly])) == {kept_assembly}
    assert _ids(_search(workspace, view="candidates")) == [kept["record_id"]]


# --- the projection's rules, run over the migration's own SQL -----------------------


def _reference(record_id: str, content: dict[str, Any]) -> tuple[Any, ...]:
    """What a preview is, stated in Python: the rules the migration's view applies."""

    def text(key: str) -> str | None:
        value = content.get(key)
        return value if isinstance(value, str) and value and "\x00" not in value else None

    def bounded(value: Any, low: int, high: int) -> str | None:
        return (
            value
            if isinstance(value, str) and low <= len(value) <= high and "\x00" not in value
            else None
        )

    def identifier(value: Any) -> str | None:
        return (
            value
            if isinstance(value, str)
            and 1 <= len(value) <= 128
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value)
            else None
        )

    full_title = text("title") or record_id
    full_body = text("summary") or text("what") or text("learned")
    title = full_title[:200]
    preview = title if full_body is None else full_body[:480]
    truncated = len(full_title) > 200 or (full_body is not None and len(full_body) > 480)
    topic = content.get("topic_ref")
    applicability = content.get("applicability")
    return (
        title,
        preview,
        int(truncated),
        bounded(content.get("kind"), 1, 64),
        bounded(content.get("assertion_basis"), 1, 32),
        bounded(topic.get("proposed_key"), 1, 256) if isinstance(topic, dict) else None,
        identifier(applicability.get("repository_id")) if isinstance(applicability, dict) else None,
        identifier(applicability.get("snapshot_id")) if isinstance(applicability, dict) else None,
    )


def _source_view() -> sqlite3.Connection:
    """The migration's own `CREATE VIEW`, over a table with just the columns it reads."""
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE omnivia_governed_version_assemblies (workspace_id TEXT, assembly_id TEXT, "
        "governed_record_id TEXT, domain_scope TEXT, content_digest TEXT, content_json TEXT)"
    )
    (migration,) = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    (view,) = [
        s
        for s in split_sql_statements(migration.sql)
        if s.startswith("CREATE VIEW") and SOURCE_VIEW in s.split("AS")[0]
    ]
    connection.execute(view)
    return connection


def _project(connection: sqlite3.Connection, content_json: str, domain: str = "engineering.codebase") -> Any:
    connection.execute("DELETE FROM omnivia_governed_version_assemblies")
    connection.execute(
        "INSERT INTO omnivia_governed_version_assemblies VALUES "
        "('ws', 'asm-1', 'rec-1', ?, 'sha256:' || hex(zeroblob(32)), ?)",
        (domain, content_json),
    )
    return connection.execute(
        f"SELECT projection_version, title, preview, truncated, observation_kind, "
        f"assertion_basis, topic_key, repository_id, snapshot_id FROM {SOURCE_VIEW}"
    ).fetchall()


_EDGE_CONTENTS: list[dict[str, Any]] = [
    {"title": "T", "summary": "S", "what": "W", "kind": "finding"},
    {"title": "T", "what": "W only"},
    {"title": "T", "learned": "learned only"},
    {"title": "T", "summary": "", "what": "", "learned": ""},
    {"title": "T", "summary": "", "what": "the next non-empty"},
    {"title": "", "summary": "no title"},
    {"summary": "no title at all"},
    {"title": 5, "summary": 7, "what": ["x"]},
    {"title": "t" * 200, "summary": "x"},
    {"title": "t" * 201, "summary": "x"},
    {"title": "T", "summary": "s" * 480},
    {"title": "T", "summary": "s" * 481},
    {"title": "T", "summary": EMOJI * 480},
    {"title": "T", "summary": EMOJI * 481},
    {"title": EMOJI * 200, "summary": "x"},
    {"title": EMOJI * 201, "summary": "x"},
    {"title": "é" * 300, "summary": "é" * 600},
    {"title": "a\u0000b", "summary": "s\u0000t", "what": "clean what"},
    {"title": "T", "summary": "x", "kind": "k" * 64, "assertion_basis": "b" * 32},
    {"title": "T", "summary": "x", "kind": "k" * 65, "assertion_basis": "b" * 33},
    {"title": "T", "summary": "x", "kind": "", "assertion_basis": 7},
    {"title": "T", "summary": "x", "topic_ref": {"proposed_key": "k" * 256}},
    {"title": "T", "summary": "x", "topic_ref": {"proposed_key": "k" * 257}},
    {"title": "T", "summary": "x", "topic_ref": "not an object"},
    {"title": "T", "summary": "x", "topic_ref": {"proposed_key": ""}},
    {
        "title": "T",
        "summary": "x",
        "applicability": {"repository_id": "erepo-app", "snapshot_id": "esnap-1"},
    },
    {
        "title": "T",
        "summary": "x",
        "applicability": {"repository_id": "bad id", "snapshot_id": "-leading"},
    },
    {
        "title": "T",
        "summary": "x",
        "applicability": {"repository_id": "r" * 129, "snapshot_id": "s" * 128},
    },
    {"title": "T", "summary": "x", "applicability": ["erepo"]},
]


@pytest.mark.parametrize("content", _EDGE_CONTENTS, ids=range(len(_EDGE_CONTENTS)))
def test_the_migrations_view_applies_the_documented_preview_rules(
    content: dict[str, Any],
) -> None:
    """The exact SQL migration 0053 ships, over edge-case content, agrees with the
    rules as stated in Python: code points not bytes, the first non-empty of
    summary, what and learned, the title falling back to the record id, and every
    metadata field left out rather than cut when it is outside its profile."""
    import json

    connection = _source_view()
    (row,) = _project(connection, json.dumps(content, ensure_ascii=False))
    assert row[0] == engineering_preview.PROJECTION_VERSION
    assert tuple(row[1:]) == _reference("rec-1", content)
    title, preview = row[1], row[2]
    assert 1 <= len(title) <= 200
    assert 1 <= len(preview) <= 480 and len(preview.encode("utf-8")) <= 2048


def test_random_content_agrees_with_the_reference_and_never_violates_a_table_check() -> None:
    """A seeded fuzz of the same SQL: mixed astral, combining, NUL, escaped and oversized
    text, and non-string and non-object fields, in both JSON escapings. Every content
    projects exactly as the reference says, and every derived row satisfies the table's
    own CHECKs -- so a writer's projection can never fail the write it rides on."""
    import json
    import random

    rng = random.Random(20260927)
    alphabet = ["a", "Z", "é", "e\u0301", EMOJI, "中", "\u0000", "\u2028", "\\", '"', " ", "\n", "ﬃ"]
    lengths = (0, 1, 2, 5, 199, 200, 201, 479, 480, 481, 700, 2000)

    def text() -> str:
        return "".join(rng.choice(alphabet) for _ in range(rng.choice(lengths)))

    def value() -> Any:
        roll = rng.random()
        if roll < 0.7:
            return text()
        if roll < 0.8:
            return rng.choice([None, True, 5, 1.5, [], {}, ["x"], {"k": "v"}])
        return ""

    def identifier() -> str:
        return rng.choice(["erepo-1", "bad id", "a" * 128, "a" * 129, "", "-x", "é", "ok.1:2_3"])

    connection = _source_view()
    (migration,) = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    (table,) = [s for s in split_sql_statements(migration.sql) if s.startswith("CREATE TABLE")]
    connection.execute(table)
    columns = (
        "workspace_id, assembly_id, projection_version, content_digest, title, preview, "
        "truncated, observation_kind, assertion_basis, topic_key, repository_id, snapshot_id"
    )
    for _ in range(400):
        content: dict[str, Any] = {
            key: value()
            for key in ("title", "summary", "what", "learned", "kind", "assertion_basis")
            if rng.random() < 0.7
        }
        if rng.random() < 0.5:
            content["topic_ref"] = rng.choice(
                [{"proposed_key": value()}, "x", None, {"other": 1}, []]
            )
        if rng.random() < 0.5:
            content["applicability"] = rng.choice(
                [
                    {"repository_id": identifier(), "snapshot_id": identifier()},
                    {"repository_id": value()},
                    "s",
                    [],
                ]
            )
        encoded = json.dumps(content, ensure_ascii=rng.random() < 0.5)
        (row,) = _project(connection, encoded)
        assert tuple(row[1:]) == _reference("rec-1", json.loads(encoded)), encoded
        connection.execute("DELETE FROM omnivia_engineering_preview_projection")
        connection.execute(
            f"INSERT INTO omnivia_engineering_preview_projection ({columns}) "
            f"SELECT {columns} FROM {SOURCE_VIEW}"
        )


def test_content_that_is_not_an_object_or_not_engineering_has_no_row() -> None:
    connection = _source_view()
    for content_json in ("[1, 2]", '"text"', "7", "null", "{not json", ""):
        assert _project(connection, content_json) == []
    assert _project(connection, '{"title": "T", "summary": "x"}', domain="workspace.notes") == []
    assert len(_project(connection, '{"title": "T", "summary": "x"}')) == 1


def test_the_python_constants_are_the_views_literals() -> None:
    connection = _source_view()
    (row,) = _project(connection, '{"title": "T", "summary": "x"}')
    assert row[0] == engineering_preview.PROJECTION_VERSION
    assert engineering_preview.PREVIEW_MAX_CODEPOINTS == 480
    assert engineering_preview.PREVIEW_MAX_BYTES == 2048
    assert engineering_preview.TITLE_MAX_CODEPOINTS == 200
    assert handlers.PREVIEW_MAX_CODEPOINTS == engineering_preview.PREVIEW_MAX_CODEPOINTS
    domain = engineering_preview.OBSERVATION_DOMAIN
    assert domain == handlers.OBSERVATION_DOMAIN == memory._ENGINEERING_DOMAIN
    (migration,) = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert "substr(b.full_title, 1, 200)" in migration.sql
    assert "substr(b.full_body, 1, 480)" in migration.sql
    assert "<= 2048" in migration.sql
    assert f"domain_scope = '{domain}'" in migration.sql


# --- migration 0053 ----------------------------------------------------------------------


@contextmanager
def older_release(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The writers of a release that predates the preview projection: they insert a
    version and project nothing, as every workspace written before 0053 was."""
    with monkeypatch.context() as patched:
        patched.setattr(engineering_preview, "record_preview", lambda *_a, **_k: None)
        yield


def test_0053_is_comment_free_inside_its_statements() -> None:
    """The migrator's statement splitter drops comments and `executescript` keeps
    them, so a comment inside a statement would store two different schemas."""
    (migration,) = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert migration.name == MIGRATION_NAME
    statements = split_sql_statements(migration.sql)
    assert [s.split()[1] for s in statements] == [
        "TABLE",
        "VIEW",
        "VIEW",
        "INTO",
        "TRIGGER",
        "TRIGGER",
        "TRIGGER",
    ]
    for statement in statements:
        assert "--" not in statement
    # The backfill runs before the guards that would otherwise refuse it exist.
    assert migration.sql.index("INSERT INTO") < migration.sql.index("CREATE TRIGGER")


def test_a_write_and_the_guard_seek_their_own_assembly(workspace: Workspace) -> None:
    """The per-version projection a writer runs and the derivation the INSERT guard
    re-runs each seek their assembly by its identity index, in a workspace built by the
    real migrator, so neither grows with the workspace. Only the maintenance rebuild
    scans."""
    connection = workspace.holder.connection
    seek = re.compile(
        r"SEARCH a USING INDEX omnivia_idx_governed_version_assemblies_identity "
        r"\(workspace_id=\? AND assembly_id=\?\)"
    )

    def plan(statement: str, parameters: Any) -> list[str]:
        with authorised(connection, mutations=True):
            return [
                str(row[3])
                for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}", parameters)
            ]

    writer = plan(
        engineering_preview._PROJECT + "WHERE s.workspace_id = ? AND s.assembly_id = ?",
        ("ws", "asm"),
    )
    assert any(seek.fullmatch(line) for line in writer), writer
    assert not any(line.startswith("SCAN a") for line in writer), writer

    (sql,) = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?",
        (f"omnivia_guard_{PROJECTION}_insert",),
    ).fetchone()
    body = sql[sql.index("\nBEGIN\n") + len("\nBEGIN\n") : sql.rindex("END")]
    derivations = 0
    for statement in split_sql_statements(body):
        if SOURCE_VIEW not in statement:
            continue
        values = {name: None for name in re.findall(r"\bNEW\.(\w+)", statement)}
        compiled = re.sub(r"RAISE\(ABORT, '(?:[^']|'')*'\)", "1", statement)
        compiled = re.sub(r"\bNEW\.(\w+)", r":\1", compiled)
        guard = plan(compiled, values)
        assert any(seek.fullmatch(line) for line in guard), guard
        assert not any(line.startswith("SCAN a") for line in guard), guard
        derivations += 1
    assert derivations == 1


def test_0053_fresh_and_upgraded_workspaces_reach_one_canonical_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace upgraded from 0052 backfills a projection row for every
    engineering-domain version it already held, and each is the row a fresh
    workspace's trigger writes for the same content."""
    (migration,) = [m for m in load_migrations() if m.version == MIGRATION_VERSION]

    def verified(connection: sqlite3.Connection) -> None:
        assert applied_migrations(connection)[MIGRATION_VERSION] == migration.checksum
        assert fingerprint_schema(connection).matches(canonical_schema_fingerprint())
        assert_guards_intact(connection)
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []

    fresh = tmp_path / "fresh.sqlite"
    m2.materialise_phase0_baseline(fresh)
    m2.bootstrap_and_migrate(fresh)
    connection = open_database(fresh, OpenMode.READ_ONLY)
    try:
        verified(connection)
        assert connection.execute(f"SELECT COUNT(*) FROM {PROJECTION}").fetchone() == (0,)
    finally:
        connection.close()

    def scenario(workspace: Workspace) -> tuple[dict[str, str], dict[str, str]]:
        long = workspace.observe(_long("Long provider decision", kind="decision"))
        note = {
            **esc._observation(None, title="Provider note", evidence=False),
            "record_type": "memory.fact",
            "domain_scope": "workspace.notes",
        }
        note["content"] = {"fact": "not an engineering version"}
        workspace.observe(note)
        return long, esc._accept(workspace, long)

    (tmp_path / "upgraded").mkdir()
    with older_release(monkeypatch), m2.migration_catalogue_through(MIGRATION_VERSION - 1):
        upgraded = Workspace(tmp_path / "upgraded")
        scenario(upgraded)
        assert MIGRATION_VERSION not in applied_migrations(upgraded.holder.connection)
        assert (
            upgraded.holder.connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name = ?", (PROJECTION,)
            ).fetchone()
            == (0,)
        )
        upgraded.holder.connection.close()
    with m2.migration_catalogue_through(MIGRATION_VERSION):
        maintenance = open_database(upgraded.holder.path, OpenMode.EXCLUSIVE_MAINTENANCE)
        try:
            state = read_workspace_state(maintenance)
            assert state is not None
            applied = apply_pending_migrations(
                maintenance,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m2.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
            assert [m.version for m in applied] == [MIGRATION_VERSION]
            verified(maintenance)
            backfilled = sorted(
                tuple(row)
                for row in maintenance.execute(
                    "SELECT projection_version, content_digest, title, preview, truncated, "
                    f"observation_kind FROM {PROJECTION}"
                )
            )
            engineering = maintenance.execute(
                "SELECT COUNT(*) FROM omnivia_governed_version_assemblies "
                "WHERE domain_scope = 'engineering.codebase'"
            ).fetchone()[0]
            assert engineering == 3 == len(backfilled)
        finally:
            maintenance.close()

    # The restarted service serves the backfilled versions, and writes new ones by
    # trigger: the same rows either way.
    upgraded.restart()
    try:
        assert len(_ids(_search(upgraded, view="accepted"))) == 1
        (old_preview,) = _search(upgraded, view="accepted")["previews"]
        upgraded.observe(_long("Long provider later", kind="decision"))
        assert len(_ids(_search(upgraded, view="candidates"))) == 1
    finally:
        upgraded.holder.connection.close()

    (tmp_path / "fresh-run").mkdir()
    fresh_workspace = Workspace(tmp_path / "fresh-run")
    try:
        scenario(fresh_workspace)
        written = sorted(
            tuple(row)
            for row in fresh_workspace.holder.connection.execute(
                "SELECT projection_version, content_digest, title, preview, truncated, "
                f"observation_kind FROM {PROJECTION}"
            )
        )
        (fresh_preview,) = _search(fresh_workspace, view="accepted")["previews"]
    finally:
        fresh_workspace.holder.connection.close()
    assert backfilled == written
    assert {k: v for k, v in old_preview.items() if k not in ("record_id", "version")} == {
        k: v for k, v in fresh_preview.items() if k not in ("record_id", "version")
    }


def test_the_migration_backfill_never_fails_a_workspace_with_unreadable_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content that is not valid JSON has no projection row, and neither it nor a
    null byte in a title can fail the migration or the write it rides on."""
    with older_release(monkeypatch), m2.migration_catalogue_through(MIGRATION_VERSION - 1):
        workspace = Workspace(tmp_path)
        fine = workspace.observe(esc._observation(None, title="Provider fine", evidence=False))
        broken = workspace.observe(esc._observation(None, title="Provider broken", evidence=False))
        connection = workspace.holder.connection
        (broken_assembly,) = _assemblies(workspace, broken["record_id"])
        with _guards_lifted(connection, "omnivia_guard_governed_version_assemblies_update"):
            _damage(
                connection,
                "UPDATE omnivia_governed_version_assemblies SET content_json = '{not json' "
                f"WHERE assembly_id = '{broken_assembly}'",
            )
        connection.close()
    with m2.migration_catalogue_through(MIGRATION_VERSION):
        maintenance = open_database(workspace.holder.path, OpenMode.EXCLUSIVE_MAINTENANCE)
        try:
            state = read_workspace_state(maintenance)
            assert state is not None
            apply_pending_migrations(
                maintenance,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m2.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
            assembled = [
                row[0]
                for row in maintenance.execute(
                    f"SELECT governed_record_id FROM {PROJECTION} p "
                    "JOIN omnivia_governed_version_assemblies a "
                    "ON a.workspace_id = p.workspace_id AND a.assembly_id = p.assembly_id"
                )
            ]
        finally:
            maintenance.close()
    assert assembled == [fine["record_id"]]

    workspace.restart()
    try:
        # A version whose content cannot be read has no row: a search that admits it
        # refuses, and does not read the body it cannot decode.
        with Trace(workspace.holder.connection) as trace:
            code = workspace.refused(
                "engineering.search", {"query": "provider", "view": "candidates"}
            )[0]
        assert code == "projection_unavailable"
        assert trace.body_reads() == []
        # A title with a null byte writes and reads like any other.
        odd = esc._observation(None, title="a\u0000b provider", evidence=False)
        odd_record = workspace.observe(odd)
        (odd_assembly,) = _assemblies(workspace, odd_record["record_id"])
        assert _projection_rows(workspace, [odd_assembly])[odd_assembly][2] == odd_record[
            "record_id"
        ]
    finally:
        workspace.holder.connection.close()
