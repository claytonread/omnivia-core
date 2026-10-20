"""Lexical-retrieval evidence for LEX-03, LEX-05 and LEX-06, through `evidence.search`.

Every search here is a real `evidence.search` request through the production
`ApplicationDispatcher` over a real migrated, leased and guarded workspace database
whose projection was built by `fts.build_search_projection`. Nothing ranks in the test:
the expectations are orders written down in advance from the fixtures' term counts and
lengths, and each fixture is built so the order is different if the statistic under test
is wrong.

* **LEX-03** backs a workspace up and restores it into a fresh location with the
  production backup path, deletes the source, reopens the restored file the way the
  service starts (build, which must find the restored run current, then materialise) and
  requires the same ranked pages, the same artifact checksums and the same projection
  identity.
* **LEX-05** adds and removes authorised evidence -- by arrival, tombstone and label --
  and requires the order to follow the statistics of the *authorised frontier* at each
  step, never the corpus. There is no erase operation (LEX-01), so "removed" means
  leaves the frontier. BM25's idf is one constant per query, so it cannot reorder a
  single-phrase result; document frequency shows in the match set (the rare-term
  matrix) and the average length shows in the order.
* **LEX-06** opens two workspaces at once in one process, with the same evidence ids,
  native ids, checksums, checkpoint and projection run id, and requires each to answer
  from its own evidence alone however the searches interleave.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_evidence_search_vertical as v
from omnivia_core_runtime.ownership.fencing import fenced_transaction, guard_present
from omnivia_core_runtime.service.application import (
    LOCAL_TRANSPORT_ADAPTER,
    ApplicationDispatcher,
    build_application_registry,
    local_owner_session,
)
from omnivia_core_runtime.service.authorization import Grant, ServiceBinding
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    server_capability_snapshot,
)
from omnivia_core_runtime.storage.backup import (
    backup_database,
    new_attempt_id,
    restore_backup,
    verify_backup,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.projections import fts

from omnivia_core.contracts.v1 import (
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_STALE_PROJECTION,
    ErrorResponseEnvelope,
    SuccessResponseEnvelope,
)

OWNER = v.LOCAL_PRINCIPAL
#: Holds no label, so a restricted artifact is outside this principal's frontier.
STRANGER = "not-the-owner"
WS_A = "ws-lexical-0001"
WS_B = "ws-lexical-0002"
BASE = v.BASE_US
NOW_US = BASE + 1_000_000


@dataclass
class Workspace:
    holder: m2.Owned
    workspace_id: str
    seeded: bool = False

    @property
    def connection(self) -> Any:
        return self.holder.connection


def open_workspace(path: Path, workspace_id: str) -> Workspace:
    """A workspace taken to the head schema by the real migrator, then leased and guarded."""
    path.parent.mkdir(parents=True, exist_ok=True)
    materialise_phase0_baseline(path)
    m2.bootstrap_and_migrate(path, workspace_id=workspace_id)
    return Workspace(m2.take_ownership(path, workspace_id=workspace_id), workspace_id)


@pytest.fixture
def workspaces(tmp_path: Path) -> Iterator[tuple[Workspace, Workspace]]:
    """Two workspaces open in this process at once."""
    opened = (
        open_workspace(tmp_path / "a" / "workspace.sqlite", WS_A),
        open_workspace(tmp_path / "b" / "workspace.sqlite", WS_B),
    )
    yield opened
    for workspace in opened:
        with contextlib.suppress(Exception):
            workspace.connection.close()


def locator(term_count: int, padding: int = 0, *, term: str = "alpha") -> str:
    """A locator carrying `term` `term_count` times and `padding` other tokens."""
    parts = [term] * term_count + [f"p{index}" for index in range(padding)]
    return "archive://" + "/".join(parts)


def artifact(
    workspace: Workspace,
    evidence_id: str,
    *,
    at: int,
    where: str,
    checksum: str = v.DIGEST_A,
) -> dict[str, Any]:
    """An artifact row; the search surface is its kind, native id and locator."""
    return v.artifact_row(
        evidence_id,
        workspace_id=workspace.workspace_id,
        recorded_at_us=at,
        native_id=f"doc-{evidence_id}",
        locator=where,
        checksum=checksum,
    )


def write(
    workspace: Workspace,
    *rows: dict[str, Any],
    extra: tuple[tuple[str, dict[str, Any]], ...] = (),
) -> None:
    """New evidence (with its first provenance entry) and/or events, in one fenced write."""
    assert guard_present(workspace.connection)
    holder = workspace.holder
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=workspace.workspace_id,
        fencing_generation=holder.generation,
    ):
        if not workspace.seeded:
            v.insert(
                holder.connection,
                v.BLOBS,
                {
                    "workspace_id": workspace.workspace_id,
                    "content_digest": v.DIGEST_A,
                    "content_length_bytes": 1024,
                    "created_at_us": BASE - 100,
                    "verified_at_us": BASE - 99,
                },
            )
            workspace.seeded = True
        for row in rows:
            v.insert(holder.connection, v.EVIDENCE, row)
            first = v.provenance_row(
                str(row["evidence_id"]), native_id=str(row["source_native_id"])
            )
            v.insert(
                holder.connection,
                v.PROVENANCE,
                {**first, "workspace_id": workspace.workspace_id},
            )
        for table, values in extra:
            v.insert(
                holder.connection,
                table,
                {**values, "workspace_id": workspace.workspace_id},
            )


def tombstone(
    workspace: Workspace, evidence_id: str, sequence: int, *, on: bool
) -> None:
    row = v.provenance_row(
        evidence_id,
        sequence=sequence,
        tombstoned=int(on),
        native_id=f"doc-{evidence_id}",
    )
    write(workspace, extra=((v.PROVENANCE, row),))


def label(
    workspace: Workspace, evidence_id: str, sequence: int, *, action: str
) -> None:
    row = v.label_row(evidence_id, "restricted", sequence=sequence, action=action)
    write(workspace, extra=((v.LABELS, row),))


def project(workspace: Workspace, *, now_us: int = NOW_US) -> fts.BuildOutcome:
    """The service's own startup maintenance: build, then materialise the session index."""
    outcome = fts.build_search_projection(
        workspace.connection,
        workspace.holder.identity,
        workspace_id=workspace.workspace_id,
        fencing_generation=workspace.holder.generation,
        now_us=now_us,
    )
    fts.open_search_projection(
        workspace.connection, workspace_id=workspace.workspace_id
    )
    return outcome


def dispatcher(
    workspace: Workspace, *, principal: str = OWNER
) -> ApplicationDispatcher:
    """The application path composed as `service.main.serve` composes it."""
    registry = build_application_registry()
    served = v.ServedWorkspace(workspace.connection)
    return ApplicationDispatcher(
        registry=registry,
        session=local_owner_session(
            principal_id=principal,
            installation_id=v.INSTALLATION_ID,
            workspace_id=workspace.workspace_id,
            operations=v.PRODUCTION_OPERATIONS,
        ),
        binding=ServiceBinding(
            installation_id=v.INSTALLATION_ID, workspace_id=workspace.workspace_id
        ),
        supported_capabilities=server_capability_snapshot(registry),
        transport=LOCAL_TRANSPORT_ADAPTER,
        probe=Dispatcher.for_service_operations(
            Grant(
                principal=principal,
                workspaces=frozenset({workspace.workspace_id}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            served,
        ),
        record=None,
        service=served,
    )


def ask(
    path: ApplicationDispatcher,
    workspace_id: str,
    query: str,
    **payload: Any,
) -> Any:
    return path.dispatch(
        v.request_for(workspace_id=workspace_id, input={"query": query, **payload})
    )


def page(response: Any) -> dict[str, Any]:
    assert isinstance(response, SuccessResponseEnvelope), response
    return dict(response.result)


def ids(response: Any) -> tuple[str, ...]:
    return tuple(item["evidence_id"] for item in page(response)["evidence"])


def code(response: Any) -> str:
    assert isinstance(response, ErrorResponseEnvelope), response
    return str(response.error.code)


# --- LEX-03 -------------------------------------------------------------------

#: `evd-tie-a` and `evd-tie-b` are scored identically, so recency orders them. The rest
#: differ in term count and length, so relevance orders them.
RESTORE_FIXTURE: tuple[tuple[str, int, str], ...] = (
    ("evd-solo", 1, locator(1, 1)),
    ("evd-dense", 4, locator(4, 1)),
    ("evd-tie-a", 2, locator(2, 3)),
    ("evd-tie-b", 3, locator(2, 3)),
    ("evd-long", 5, locator(2, 14)),
    ("evd-other", 6, locator(0, 2, term="gamma")),
)
RESTORE_QUERIES = ("alpha", "gamma", "archive", "alpha alpha", "absent", "doc")


def test_lex_03_a_restored_workspace_returns_the_same_ranked_results(
    tmp_path: Path,
) -> None:
    """Back up, restore into a fresh location, reopen, and search again.

    The source is deleted before the restored file is opened, so nothing the restored
    workspace answers can come from it. Reopening is exactly the service start: a build
    that must find the restored run current -- if the restore lost the projection it
    would build a new run and change the identity asserted below -- then the session
    index is materialised from the restored durable rows.
    """
    live = open_workspace(tmp_path / "live" / "workspace.sqlite", WS_A)
    write(
        live,
        *(
            artifact(live, name, at=BASE + offset, where=where)
            for name, offset, where in RESTORE_FIXTURE
        ),
    )
    built = project(live)
    assert built.activated is True and built.document_count == len(RESTORE_FIXTURE)

    before = {
        query: page(ask(dispatcher(live), WS_A, query)) for query in RESTORE_QUERIES
    }
    first_page = ask(dispatcher(live), WS_A, "archive", limit=3)
    before_paged = ids(first_page)
    assert page(first_page)["page"]["continuation_token"] is not None
    # The fixture discriminates: relevance, the recency tie-break and "no match" all
    # appear, so a restore that reordered or dropped anything is visible.
    assert [item["evidence_id"] for item in before["alpha"]["evidence"]] == [
        "evd-dense",
        "evd-tie-b",
        "evd-tie-a",
        "evd-solo",
        "evd-long",
    ]
    assert before["absent"]["evidence"] == []
    checksums = dict(
        live.connection.execute(
            "SELECT evidence_id, content_checksum FROM omnivia_evidence_artifacts "
            "WHERE workspace_id = ?",
            (WS_A,),
        ).fetchall()
    )
    assert len(checksums) == len(RESTORE_FIXTURE)
    identity = (
        built.run_id,
        built.build_digest,
        built.source_checkpoint,
        built.document_count,
    )

    # A quiesced service releases the workspace before it is backed up.
    source = live.holder.path
    live.connection.close()
    attempt = new_attempt_id()
    backup = backup_database(source, tmp_path / "backups" / f"{attempt}.sqlite")
    verified = verify_backup(source, backup, attempt)
    assert verified.verified is True
    restored_path = restore_backup(backup, tmp_path / "restored" / "workspace.sqlite")
    assert restored_path != source
    for leftover in source.parent.glob("workspace.sqlite*"):
        leftover.unlink()

    restored = Workspace(
        m2.take_ownership(restored_path, workspace_id=WS_A), WS_A, seeded=True
    )
    try:
        # The restore carried the active run, so the service-start build is a no-op.
        reopened = project(restored, now_us=NOW_US + 1000)
        assert reopened.activated is False
        assert (
            reopened.run_id,
            reopened.build_digest,
            reopened.source_checkpoint,
            reopened.document_count,
        ) == identity
        assert (
            dict(
                restored.connection.execute(
                    "SELECT evidence_id, content_checksum "
                    "FROM omnivia_evidence_artifacts WHERE workspace_id = ?",
                    (WS_A,),
                ).fetchall()
            )
            == checksums
        )

        path = dispatcher(restored)
        after = {query: page(ask(path, WS_A, query)) for query in RESTORE_QUERIES}
        assert after == before
        assert ids(ask(path, WS_A, "archive", limit=3)) == before_paged
    finally:
        restored.connection.close()


# --- LEX-05 -------------------------------------------------------------------

#: Two evidence items that answer "alpha". `evd-dense` carries it twice in a long
#: locator and `evd-brief` once in a short one. Alone their average length is low, so
#: the long one's length penalty is heavy and the brief one leads.
DENSE = locator(2, 14)
BRIEF = locator(1, 0)
FILLER = "archive://" + "/".join(f"f{index}" for index in range(55))
POISON = v.POISON_LOCATOR


def test_lex_05_statistics_follow_the_authorised_frontier_as_evidence_comes_and_goes(
    tmp_path: Path,
) -> None:
    """A matrix of arrivals, tombstones and label changes against one query.

    The same two matching items change order only when the *authorised* frontier's
    average length does. Long unmatched filler lifts it (the dense item leads), a
    tombstone takes the filler out again (the brief item leads), a restricted item
    that is not in this principal's frontier lifts nothing however long it is, and
    withdrawing the restriction lets it in, where it lifts the average and leads.
    """
    workspace = open_workspace(tmp_path / "workspace.sqlite", WS_A)
    # The strangers' view: restricted evidence is outside the frontier.
    search = lambda query, **payload: ids(  # noqa: E731
        ask(dispatcher(workspace, principal=STRANGER), WS_A, query, **payload)
    )
    write(
        workspace,
        artifact(workspace, "evd-dense", at=BASE + 1, where=DENSE),
        artifact(workspace, "evd-brief", at=BASE + 2, where=BRIEF),
    )
    project(workspace)
    assert search("alpha") == ("evd-brief", "evd-dense")

    # New evidence is not served from the old run: the read refuses until the
    # projection has been brought level, which is a fresh run.
    first_run = fts.current_build(workspace.connection, workspace_id=WS_A).run_id
    write(
        workspace,
        *(
            artifact(workspace, f"evd-filler-{n}", at=BASE + 10 + n, where=FILLER)
            for n in range(3)
        ),
    )
    stale = ask(dispatcher(workspace, principal=STRANGER), WS_A, "alpha")
    assert code(stale) == ERROR_CODE_STALE_PROJECTION
    project(workspace, now_us=NOW_US + 10)
    assert (
        fts.current_build(workspace.connection, workspace_id=WS_A).run_id != first_run
    )
    # Added: the lengthened average changes the order.
    assert search("alpha") == ("evd-dense", "evd-brief")

    # Removed: the average falls back, and so does the order.
    for n in range(3):
        tombstone(workspace, f"evd-filler-{n}", 2, on=True)
    assert search("alpha") == ("evd-brief", "evd-dense")

    # Re-added: an un-tombstone is an arrival to the frontier.
    for n in range(3):
        tombstone(workspace, f"evd-filler-{n}", 3, on=False)
    assert search("alpha") == ("evd-dense", "evd-brief")
    for n in range(3):
        tombstone(workspace, f"evd-filler-{n}", 4, on=True)
    assert search("alpha") == ("evd-brief", "evd-dense")

    # The hidden-corpus rule: a very long restricted item, 340 occurrences of the
    # term, joins the workspace and moves nothing this principal sees.
    write(
        workspace,
        artifact(workspace, "evd-poison", at=BASE + 100, where=POISON),
        extra=((v.LABELS, v.label_row("evd-poison", "restricted")),),
    )
    project(workspace, now_us=NOW_US + 20)
    assert search("alpha") == ("evd-brief", "evd-dense")
    # While the owner, who holds the label, has it in the frontier and sees it lead.
    assert ids(ask(dispatcher(workspace), WS_A, "alpha")) == (
        "evd-poison",
        "evd-dense",
        "evd-brief",
    )

    # Releasing the restriction adds it to the stranger's frontier, and the order
    # follows the new statistics.
    label(workspace, "evd-poison", 2, action="withdrawn")
    assert search("alpha") == ("evd-poison", "evd-dense", "evd-brief")
    label(workspace, "evd-poison", 3, action="attached")
    assert search("alpha") == ("evd-brief", "evd-dense")


COMMON = ("evd-c3", "evd-c2", "evd-c6", "evd-c1", "evd-c5", "evd-c4")


def test_lex_05_the_rare_term_matrix_follows_document_frequency(tmp_path: Path) -> None:
    """A rare term and a common one, as the holders of each come and go.

    Document frequency is what decides which items answer: "zephyr" is held by two of
    eight authorised items, "alpha" by six. Removing a holder from the frontier removes
    it from the answer, and a restricted holder is never counted.
    """
    workspace = open_workspace(tmp_path / "workspace.sqlite", WS_A)
    search = lambda query: ids(  # noqa: E731
        ask(dispatcher(workspace, principal=STRANGER), WS_A, query)
    )
    write(
        workspace,
        artifact(workspace, "evd-c1", at=BASE + 1, where=locator(1, 0)),
        artifact(workspace, "evd-c2", at=BASE + 2, where=locator(2, 0)),
        artifact(workspace, "evd-c3", at=BASE + 3, where=locator(3, 2)),
        artifact(workspace, "evd-c4", at=BASE + 4, where=locator(1, 6)),
        artifact(workspace, "evd-c5", at=BASE + 5, where=locator(1, 1)),
        artifact(workspace, "evd-c6", at=BASE + 6, where=locator(2, 9)),
        artifact(workspace, "evd-r1", at=BASE + 7, where=locator(1, 0, term="zephyr")),
        artifact(workspace, "evd-r2", at=BASE + 8, where=locator(2, 4, term="zephyr")),
        artifact(workspace, "evd-r3", at=BASE + 9, where=locator(5, 0, term="zephyr")),
        extra=((v.LABELS, v.label_row("evd-r3", "restricted")),),
    )
    project(workspace)
    assert search("zephyr") == ("evd-r2", "evd-r1")
    assert search("alpha") == COMMON

    tombstone(workspace, "evd-r1", 2, on=True)
    assert search("zephyr") == ("evd-r2",)
    assert search("alpha") == COMMON

    label(workspace, "evd-r3", 2, action="withdrawn")
    assert search("zephyr") == ("evd-r3", "evd-r2")

    for name in ("evd-r2", "evd-r3"):
        tombstone(workspace, name, 2, on=True)
    assert search("zephyr") == ()

    write(
        workspace,
        artifact(workspace, "evd-r4", at=BASE + 20, where=locator(1, 0, term="zephyr")),
    )
    assert code(ask(dispatcher(workspace, principal=STRANGER), WS_A, "zephyr")) == (
        ERROR_CODE_STALE_PROJECTION
    )
    project(workspace, now_us=NOW_US + 30)
    assert search("zephyr") == ("evd-r4",)


# --- LEX-06 -------------------------------------------------------------------


#: The same ids, native ids, checksums and recording instants in both workspaces, so the
#: two projections even share a run id; only the locators -- the searchable text --
#: differ, which is the only thing that can tell the answers apart.
def seed_pair(a: Workspace, b: Workspace) -> None:
    write(
        a,
        artifact(a, "evd-1", at=BASE + 1, where=locator(1, 0)),
        artifact(a, "evd-2", at=BASE + 2, where=locator(3, 0)),
        artifact(a, "evd-3", at=BASE + 3, where=locator(2, 0, term="bravo")),
        artifact(a, "evd-a-only", at=BASE + 4, where=locator(2, 1)),
    )
    write(
        b,
        artifact(b, "evd-1", at=BASE + 1, where=locator(4, 0)),
        artifact(b, "evd-2", at=BASE + 2, where=locator(1, 0)),
        artifact(b, "evd-3", at=BASE + 3, where=locator(1, 0, term="bravo")),
        artifact(b, "evd-b-only", at=BASE + 4, where=locator(1, 0, term="bravo")),
    )


def test_lex_06_two_live_workspaces_never_share_results_counts_ranks_or_state(
    workspaces: tuple[Workspace, Workspace],
) -> None:
    a, b = workspaces
    seed_pair(a, b)
    project(a)
    project(b)
    path_a, path_b = dispatcher(a), dispatcher(b)

    expected = {
        (WS_A, "alpha"): ("evd-2", "evd-a-only", "evd-1"),
        (WS_B, "alpha"): ("evd-1", "evd-2"),
        (WS_A, "bravo"): ("evd-3",),
        (WS_B, "bravo"): ("evd-3", "evd-b-only"),
    }
    paths = {WS_A: path_a, WS_B: path_b}
    # Interleaved and repeated, so state left by one search cannot decide another.
    for _ in range(2):
        for key in (
            (WS_A, "alpha"),
            (WS_B, "alpha"),
            (WS_B, "bravo"),
            (WS_A, "bravo"),
        ):
            workspace_id, query = key
            assert ids(ask(paths[workspace_id], workspace_id, query)) == expected[key]
    assert "evd-a-only" not in ids(ask(path_b, WS_B, "alpha"))
    assert "evd-b-only" not in ids(ask(path_a, WS_A, "bravo"))

    # Each answer is the workspace's own artifact, not merely an id that exists in both.
    for workspace_id, locators in (
        (WS_A, {"evd-1": locator(1, 0), "evd-2": locator(3, 0)}),
        (WS_B, {"evd-1": locator(4, 0), "evd-2": locator(1, 0)}),
    ):
        for item in page(ask(paths[workspace_id], workspace_id, "alpha"))["evidence"]:
            assert item["workspace_id"] == workspace_id
            if item["evidence_id"] in locators:
                assert locators[item["evidence_id"]] in json.dumps(item)

    # A request addressed to the other workspace is refused by the binding.
    assert code(ask(path_a, WS_B, "alpha"))

    # Projection state: the same run id, the same checkpoint and the same artifact ids in
    # both -- so anything keyed on them would collide -- and still two separate sessions.
    build_a = fts.current_build(a.connection, workspace_id=WS_A)
    build_b = fts.current_build(b.connection, workspace_id=WS_B)
    assert build_a.run_id == build_b.run_id
    session_a = fts.session_search_projection(a.connection)
    session_b = fts.session_search_projection(b.connection)
    assert session_a is not None and session_b is not None
    assert (session_a.workspace_id, session_a.document_count) == (WS_A, 4)
    assert (session_b.workspace_id, session_b.document_count) == (WS_B, 4)
    assert set(session_a.material) == {"evd-1", "evd-2", "evd-3", "evd-a-only"}
    assert set(session_b.material) == {"evd-1", "evd-2", "evd-3", "evd-b-only"}

    # New evidence in B starts a new run there and leaves A level and unchanged.
    write(b, artifact(b, "evd-b-new", at=BASE + 50, where=locator(9, 0)))
    assert code(ask(path_b, WS_B, "alpha")) == ERROR_CODE_STALE_PROJECTION
    assert ids(ask(path_a, WS_A, "alpha")) == expected[(WS_A, "alpha")]
    project(b, now_us=NOW_US + 10)
    assert ids(ask(path_b, WS_B, "alpha")) == ("evd-b-new", "evd-1", "evd-2")
    assert ids(ask(path_a, WS_A, "alpha")) == expected[(WS_A, "alpha")]
    assert fts.current_build(a.connection, workspace_id=WS_A).run_id == build_a.run_id
    assert fts.current_build(b.connection, workspace_id=WS_B).run_id != build_b.run_id


def test_lex_06_a_continuation_minted_in_one_workspace_is_refused_in_the_other(
    workspaces: tuple[Workspace, Workspace],
) -> None:
    """Continuation tokens live in one process-wide store.

    Both workspaces answer "bravo" with the same evidence ids and checksums and so the
    same ranking snapshot; the workspace in the token's binding is the only thing that
    stops one workspace's token continuing the other's search.
    """
    a, b = workspaces
    for workspace in (a, b):
        write(
            workspace,
            artifact(
                workspace, "evd-1", at=BASE + 1, where=locator(1, 0, term="bravo")
            ),
            artifact(
                workspace, "evd-2", at=BASE + 2, where=locator(1, 0, term="bravo")
            ),
        )
        project(workspace)
    first = page(ask(dispatcher(a), WS_A, "bravo", limit=1))
    token = first["page"]["continuation_token"]
    assert token is not None
    continued = {"page": {"continuation_token": token}}
    own = ask(dispatcher(a), WS_A, "bravo", limit=1, **continued)
    assert ids(own) == ("evd-1",)
    foreign = ask(dispatcher(b), WS_B, "bravo", limit=1, **continued)
    assert code(foreign) == ERROR_CODE_INVALID_REQUEST
