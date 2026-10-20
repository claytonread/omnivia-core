"""LEX-01: `evidence.erase` end to end, through the production `ApplicationDispatcher`.

Evidence is captured by the real `evidence.capture`, searched by the real `evidence.search`
and packed by the real `context_pack.build`, all behind the one router `service.main.serve`
composes, over a real migrated, leased and guarded workspace with a real blob root and a real
FTS5 projection. Erasure is the shipped `evidence.erase` operation and nothing else: no
test writes a receipt, deletes a row or removes a file to simulate it.

What each test is for, and the falsifier it carries:

* the erased evidence is **physically** gone -- every row in every table that held it, its
  blob file, its projection documents in every run, and its bytes in the database file and
  the write-ahead log -- and a tombstone would fail every one of those assertions;
* neither `evidence.search` nor a Context Pack can return it or be **moved by it**: the
  surviving evidence ranks as it does in a workspace that never held the erased evidence,
  which differs from the ranking while it was held;
* the projection lifecycle stays safe: a new run supersedes the one that held the erased
  document, a failure after the commit is a retryable refusal that a same-key replay repairs,
  and a restart converges without any request;
* repeated requests are deterministic, and nothing here lets a capture replay put erased
  bytes back;
* the guards that make evidence append-only still refuse every delete no receipt licenses.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_evidence_capture_vertical as cap
import test_evidence_search_vertical as v
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service.application import (
    CONTEXT_PACK_BUILD_OPERATION,
    EVIDENCE_ERASE_OPERATION,
    EVIDENCE_SEARCH_OPERATION,
    JOB_FAMILY_PURPOSES,
    KNOWLEDGE_RETRIEVAL_PURPOSE,
    LOCAL_TRANSPORT_ADAPTER,
    WORKSPACE_INSPECT_OPERATION,
    ApplicationDispatcher,
    build_application_registry,
    build_job_application_dispatcher,
    local_owner_session,
)
from omnivia_core_runtime.service.authorization import Grant, ServiceBinding
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers import evidence as evidence_handlers
from omnivia_core_runtime.service.operations import (
    SERVICE_OPERATIONS,
    server_capability_snapshot,
)
from omnivia_core_runtime.storage import evidence_erasure
from omnivia_core_runtime.storage.migrations import load_migrations, phase0_baseline_sql
from omnivia_core_runtime.storage.projections import fts

from omnivia_core.contracts.v1 import (
    EvidenceEraseResult,
    EvidenceSearchResult,
    PrincipalClaim,
    RequestEnvelope,
    ResponseEnvelope,
    get_operation_metadata,
)

WORKSPACE_ID = cap.WORKSPACE_ID
OTHER_WORKSPACE_ID = "ws-capture-0002"
ERASE_ENTRY = get_operation_metadata(EVIDENCE_ERASE_OPERATION)
PACK_ENTRY = get_operation_metadata(CONTEXT_PACK_BUILD_OPERATION)

ARTIFACTS = cap.ARTIFACTS
BLOBS = cap.BLOBS
INTEGRITY = cap.INTEGRITY
STAGED = cap.STAGED
PROVENANCE = cap.PROVENANCE
AUDIT = cap.AUDIT
ERASURES = "omnivia_evidence_erasures"
DOCUMENTS = "omnivia_evidence_search_documents"

#: Words that occur in no identity surface this build writes, so a hit on one is a hit on
#: the submitted bytes, and a miss after an erase is the bytes being gone.
KEEPER_WORD = "quixotic"
TARGET_WORD = "zymurgic"
#: A source version claim, which lands in the staged source and nowhere else a row of the
#: evidence survives, so it is what the file scan below looks for.
VERSION_MARKER = "vmarkerzx9q"


# --- one router that serves capture, erase, search and the pack -----------------


def build_reads(owned: cap.Served) -> ApplicationDispatcher:
    """The read-only family: search and the pack, and no operation that writes."""
    registry = build_application_registry()
    return ApplicationDispatcher(
        registry=registry,
        session=local_owner_session(
            principal_id=cap.LOCAL_PRINCIPAL,
            installation_id=cap.INSTALLATION_ID,
            workspace_id=WORKSPACE_ID,
            operations=frozenset(
                {
                    WORKSPACE_INSPECT_OPERATION,
                    EVIDENCE_SEARCH_OPERATION,
                    CONTEXT_PACK_BUILD_OPERATION,
                }
            ),
        ),
        binding=ServiceBinding(
            installation_id=cap.INSTALLATION_ID, workspace_id=WORKSPACE_ID
        ),
        supported_capabilities=server_capability_snapshot(registry),
        transport=LOCAL_TRANSPORT_ADAPTER,
        probe=Dispatcher.for_service_operations(
            Grant(
                principal=cap.LOCAL_PRINCIPAL,
                workspaces=frozenset({WORKSPACE_ID}),
                operations=frozenset(SERVICE_OPERATIONS),
            ),
            owned,
        ),
        record=None,
        service=owned,
    )


def build_router(
    owned: cap.Served, clock: cap.FakeClock, *, tag: str = "ers"
) -> ApplicationDispatcher:
    """Capture, erase, search and the pack behind one router, as `serve` composes them.

    The reads family is the fallback of the ingestion family, which is production's own
    chaining, so one `dispatch` answers every operation and none is served by a registry
    this test built for it.
    """
    reads = build_reads(owned)
    return build_job_application_dispatcher(
        service=owned,
        principal_id=cap.LOCAL_PRINCIPAL,
        installation_id=cap.INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        fallback=reads,
        clock=clock,
        allocate_identifier=cap._allocator(tag),
    )


@pytest.fixture
def owned(tmp_path: Path):
    served = cap.serve(tmp_path / "workspace")
    yield served
    served.connection.close()


@pytest.fixture
def clock() -> cap.FakeClock:
    """A clock that moves, because the projection checkpoint is the newest `recorded_at_us`.

    Two captures stamped the same microsecond leave the checkpoint where it was, so the
    second would not be in the projection a fixed clock built for the first.
    """
    return cap.FakeClock()


@pytest.fixture
def router(owned: cap.Served, clock: cap.FakeClock) -> ApplicationDispatcher:
    return build_router(owned, clock)


def tick(clock: cap.FakeClock) -> None:
    clock.advance_wall(60)
    clock.advance_monotonic(1)


def erase_request(
    evidence_id: str,
    *,
    request_id: str = "req-erase-1",
    key: str | None = "idem-erase-1",
    workspace_id: str = WORKSPACE_ID,
    purpose: str | None = None,
    payload: Any = None,
) -> RequestEnvelope:
    metadata = cap._metadata(
        ERASE_ENTRY, request_id=request_id, key=key, workspace_id=workspace_id
    )
    if purpose is not None:
        metadata = replace(metadata, purpose=purpose)
    return RequestEnvelope(
        operation=EVIDENCE_ERASE_OPERATION,
        metadata=metadata,
        input={"evidence_id": evidence_id} if payload is None else payload,
    )


def pack_request(query: str, *, request_id: str = "req-pack-1") -> RequestEnvelope:
    metadata = replace(
        cap._metadata(PACK_ENTRY, request_id=request_id, key=None),
        purpose=KNOWLEDGE_RETRIEVAL_PURPOSE,
    )
    return RequestEnvelope(
        operation=CONTEXT_PACK_BUILD_OPERATION,
        metadata=metadata,
        input={"query": query, "mode": "deterministic_view", "token_budget": 4096},
    )


def capture(
    router: ApplicationDispatcher,
    clock: cap.FakeClock,
    native_id: str,
    text: str,
    *,
    n: int,
    **extra: Any,
) -> str:
    """One real capture; returns the evidence id the service allocated."""
    tick(clock)
    return cap.captured(
        router.dispatch(
            cap.capture_request(
                request_id=f"req-cap-{n}",
                key=f"idem-cap-{n}",
                source_native_id=native_id,
                text=text,
                **extra,
            )
        )
    ).evidence_id


def erase(router: ApplicationDispatcher, evidence_id: str, **kwargs: Any) -> Any:
    return router.dispatch(erase_request(evidence_id, **kwargs))


def tick_then_erase(
    router: ApplicationDispatcher,
    clock: cap.FakeClock,
    evidence_id: str,
    **kwargs: Any,
) -> Any:
    tick(clock)
    return erase(router, evidence_id, **kwargs)


def erased(response: ResponseEnvelope) -> EvidenceEraseResult:
    return EvidenceEraseResult.from_wire(cap.answered(response).result)


def code_of(response: ResponseEnvelope) -> str:
    return str(cap.refusal(response).error.code)


def pack_text(router: ApplicationDispatcher, query: str) -> str:
    """What a Context Pack says about the corpus, as canonical text.

    The members that carry material or account for it -- the sections it selected, what they
    cite, the evidence and records behind them and every candidate it omitted. The pack also
    repeats the caller's own query in two places, so a query naming the evidence would find
    itself in a whole-answer search whatever the pack held.
    """
    result = cap.answered(router.dispatch(pack_request(query))).result
    members = ("sections", "citations", "evidence", "records", "omissions", "history")
    return json.dumps(
        {name: result.get(name) for name in members}, sort_keys=True, default=str
    )


def pack_without_identity(router: ApplicationDispatcher, query: str) -> Any:
    """A pack with the build-instant-bound identity removed, which is not comparable."""
    result = dict(cap.answered(router.dispatch(pack_request(query))).result)
    for volatile in ("pack_id", "generated_at", "artifact_digest"):
        result.pop(volatile, None)
    return json.loads(json.dumps(result, sort_keys=True, default=str))


def tables_naming(owned: cap.Served, token: str) -> list[str]:
    """Every table with a text cell containing `token`, found by reading all of them."""
    connection = owned.connection
    found: list[str] = []
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for name in names:
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({name})")]
        if not columns:
            continue
        predicate = " OR ".join(f"CAST({c} AS TEXT) LIKE ?" for c in columns)
        try:
            hit = connection.execute(
                f"SELECT 1 FROM {name} WHERE {predicate} LIMIT 1",
                tuple(f"%{token}%" for _ in columns),
            ).fetchone()
        except sqlite3.Error:
            continue
        if hit is not None:
            found.append(name)
    return found


def file_bytes(owned: cap.Served) -> bytes:
    """The database file and its write-ahead log, as bytes."""
    base = owned.layout.database_path
    parts = [base, base.with_name(base.name + "-wal")]
    return b"".join(p.read_bytes() for p in parts if p.exists())


def attach_children(owned: cap.Served, evidence_id: str) -> dict[str, int]:
    """Give one captured artifact a label, a reference, a normalized record and its span.

    Capture itself writes none of the four, so without them an erase that mishandled those
    tables would pass on every row it was never asked to delete. They go in through the
    fenced writer the capture's own rows did, bound to the artifact's real source identity
    and blob address, so the schema accepts them as it would from the importer that writes
    them.
    """
    artifact = cap.rows(
        owned,
        "SELECT source_native_id, blob_content_digest, recorded_at_us FROM "
        f"{ARTIFACTS} WHERE evidence_id = ?",
        evidence_id,
    )[0]
    native_id, digest, at = artifact
    provenance = cap.rows(
        owned,
        f"SELECT provenance_event_id, source_kind FROM {PROVENANCE} WHERE evidence_id = ?",
        evidence_id,
    )[0]
    suffix = evidence_id.replace("evd-", "")
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        shared = {"evidence_id": evidence_id, "workspace_id": WORKSPACE_ID}
        m2.insert(
            owned.connection,
            m2.LABELS,
            m2.row_for(
                m2.LABELS,
                **shared,
                label_event_id=f"lbl-{suffix}",
                recorded_at_us=at + 1,
            ),
        )
        m2.insert(
            owned.connection,
            m2.REFERENCES,
            m2.row_for(
                m2.REFERENCES,
                **shared,
                event_reference_id=f"ref-{suffix}",
                provenance_event_id=provenance[0],
                source_kind=provenance[1],
                source_native_id=native_id,
            ),
        )
        m2.insert(
            owned.connection,
            m2.RECORDS,
            m2.row_for(
                m2.RECORDS,
                **shared,
                normalized_record_id=f"nrc-{suffix}",
                evidence_blob_digest=digest,
                recorded_at_us=at + 2,
            ),
        )
        m2.insert(
            owned.connection,
            m2.SPANS,
            m2.row_for(
                m2.SPANS,
                **shared,
                normalized_span_id=f"nsp-{suffix}",
                normalized_record_id=f"nrc-{suffix}",
                recorded_at_us=at + 3,
            ),
        )
    return {
        table: cap.count(owned, table)
        for table in (m2.LABELS, m2.REFERENCES, m2.RECORDS, m2.SPANS)
    }


# --- the vertical --------------------------------------------------------------


def test_lex_01_erased_evidence_is_physically_gone_and_never_found_again(
    owned: cap.Served, router: ApplicationDispatcher, clock: cap.FakeClock
) -> None:
    """Capture, find, erase, and then neither search nor pack can see it, and no row can.

    Falsifier: tombstoning the artifact instead of deleting it fails the row, blob,
    document and session-material assertions, though it would pass the search ones.
    """
    keeper = capture(
        router, clock, "note-keeper", f"The {KEEPER_WORD} ledger entry.", n=1
    )
    target = capture(
        router,
        clock,
        "note-target",
        f"The {TARGET_WORD} ledger entry.",
        n=2,
        source_version=VERSION_MARKER,
    )
    target_blob = cap.blob_file(
        owned,
        cap.rows(
            owned,
            f"SELECT blob_content_digest FROM {ARTIFACTS} WHERE evidence_id = ?",
            target,
        )[0][0],
    )
    assert target_blob.is_file()
    # Before: both are found, by their content and by their identity, and the pack cites.
    assert cap.found(router, TARGET_WORD) == (target,)
    assert cap.found(router, "note-target") == (target,)
    assert target in pack_text(router, "note-target")

    tick(clock)
    response = erase(router, target)
    result = erased(response)
    assert (
        result.evidence_id,
        result.erase_disposition,
        result.content_disposition,
    ) == (
        target,
        "erased",
        "erased",
    )
    assert cap.answered(response).metadata.audit_reference

    # Physically: every row, the file, every projection document, and the session index.
    for table in (ARTIFACTS, PROVENANCE, "omnivia_evidence_permission_labels"):
        assert (
            cap.rows(owned, f"SELECT 1 FROM {table} WHERE evidence_id = ?", target)
            == []
        )
    assert (
        cap.rows(
            owned, f"SELECT 1 FROM {STAGED} WHERE source_version = ?", VERSION_MARKER
        )
        == []
    )
    assert not target_blob.exists()
    assert cap.count(owned, BLOBS) == 1  # only the keeper's
    assert cap.count(owned, INTEGRITY) == 1
    assert (
        cap.rows(owned, f"SELECT 1 FROM {DOCUMENTS} WHERE evidence_id = ?", target)
        == []
    )
    session = fts.session_search_projection(owned.connection)
    assert session is not None and target not in session.material
    assert keeper in session.material
    # One receipt, naming the evidence and nothing of its content.
    receipts = cap.rows(
        owned, f"SELECT evidence_id, blob_content_digest IS NOT NULL FROM {ERASURES}"
    )
    assert receipts == [(target, 1)]
    # No cell of any table still carries the identity the evidence was captured under,
    # except the one record this operation cannot reach: the capture's own idempotency
    # outcome, which is append-only, answers a replay of that capture, and quotes the source
    # id. A replay is refused below rather than served from it, and the residue is stated
    # here so that closing it has to change this assertion.
    assert tables_naming(owned, "note-target") == ["omnivia_idempotency_outcomes"]
    # The opaque evidence id survives in three places and nowhere else: the receipt that
    # proves the erasure, that same stored outcome, and the append-preserved cursor of the
    # projection run that indexed it. None of them holds a source id, a locator, a version,
    # a checksum or any content.
    assert set(tables_naming(owned, target)) == {
        ERASURES,
        "omnivia_idempotency_outcomes",
        "omnivia_projection_run_checkpoints",
    }
    assert tables_naming(owned, VERSION_MARKER) == []

    # After: neither the content nor the identity finds it, the keeper is untouched, and
    # the pack neither cites nor names it.
    assert cap.found(router, TARGET_WORD) == ()
    assert cap.found(router, "note-target") == ()
    assert cap.found(router, KEEPER_WORD) == (keeper,)
    erased_pack = pack_text(router, "note-target")
    assert target not in erased_pack and "note-target" not in erased_pack
    assert keeper in pack_text(router, "note-keeper")


def test_lex_01_erasure_removes_every_row_the_evidence_owns_and_only_those(
    owned: cap.Served, router: ApplicationDispatcher, clock: cap.FakeClock
) -> None:
    """Labels, references, normalized records and spans go with the artifact, in an order
    the foreign keys accept, and a neighbour's identical rows stay.

    Falsifier: an erase that skipped any of the four tables fails the foreign key on the
    artifact delete and rolls everything back, so the evidence is still found.
    """
    keeper = capture(router, clock, "note-keeper", f"{KEEPER_WORD} one", n=1)
    target = capture(router, clock, "note-target", f"{TARGET_WORD} two", n=2)
    attach_children(owned, keeper)
    assert all(count == 1 for count in attach_children(owned, target).values()) is False
    owned_tables = (m2.LABELS, m2.REFERENCES, m2.RECORDS, m2.SPANS)
    assert [cap.count(owned, table) for table in owned_tables] == [2, 2, 2, 2]

    tick(clock)
    erased(erase(router, target))

    for table in owned_tables:
        assert (
            cap.rows(owned, f"SELECT 1 FROM {table} WHERE evidence_id = ?", target)
            == []
        )
        assert cap.rows(owned, f"SELECT 1 FROM {table} WHERE evidence_id = ?", keeper)
    assert cap.found(router, TARGET_WORD) == ()
    assert cap.found(router, KEEPER_WORD) == (keeper,)


# --- the file, not only the schema ------------------------------------------------


def test_lex_01_the_database_file_and_log_hold_no_trace_of_what_was_erased(
    owned: cap.Served, router: ApplicationDispatcher, clock: cap.FakeClock
) -> None:
    """A deleted row's bytes survive in free pages and the log unless the erase zeroes them.

    Falsifier: without `secure_delete` and the checkpoint, the version marker -- which the
    capture wrote into the staged source row and nowhere that outlives the erase -- is
    still in the file after the rows are gone.
    """
    capture(router, clock, "note-keeper", f"The {KEEPER_WORD} entry.", n=1)
    target = capture(
        router,
        clock,
        "note-target",
        f"The {TARGET_WORD} entry.",
        n=2,
        source_version=VERSION_MARKER,
    )
    assert VERSION_MARKER.encode() in file_bytes(owned)
    initial_secure_delete = owned.connection.execute("PRAGMA secure_delete").fetchone()[0]

    erased(tick_then_erase(router, clock, target))

    assert VERSION_MARKER.encode() not in file_bytes(owned)
    assert (
        owned.connection.execute("PRAGMA secure_delete").fetchone()[0]
        == initial_secure_delete
    )


# --- statistics: the erased evidence is not in any number the ranking is computed from -----


def sources(router: ApplicationDispatcher, query: str) -> tuple[str, ...]:
    """The source ids `evidence.search` answers `query` with, in rank order."""
    response = cap.answered(router.dispatch(cap.search_request(query)))
    return tuple(
        item.source.source_id
        for item in EvidenceSearchResult.from_wire(response.result).evidence
    )


def cited_sources(router: ApplicationDispatcher, query: str) -> list[str]:
    """Which captured sources a pack's selected members name, in the order they appear."""
    return re.findall(r"note-[a-z0-9-]+", pack_text(router, query))


DENSE_TEXT = "alpha alpha " + " ".join(f"p{index}" for index in range(14))
FILLER_TEXT = " ".join(f"f{index}" for index in range(55))


def test_lex_01_surviving_evidence_ranks_as_if_the_erased_evidence_never_existed(
    owned: cap.Served,
    router: ApplicationDispatcher,
    clock: cap.FakeClock,
    tmp_path: Path,
) -> None:
    """BM25 depends on the average length of what is searched, so erased evidence that
    still counted would move the order of what remains.

    A control workspace that only ever held the two matching documents answers `alpha`
    brief-first. Three long unmatched fillers reverse that to dense-first, which is the
    measurement that makes the rest meaningful. Erasing the fillers one by one must
    walk the order back and leave it equal to the control's, for the search and for the
    Context Pack's selection, because the erased documents are in no statistic.

    Falsifier: an erase that left the fillers' tokens in the session index or the documents
    table, or counted them in the frontier, would keep the order dense-first.
    """
    control_owned = cap.serve(tmp_path / "control")
    control_clock = cap.FakeClock()
    control = build_router(control_owned, control_clock, tag="ctl")
    try:
        capture(control, control_clock, "note-dense", DENSE_TEXT, n=1)
        capture(control, control_clock, "note-brief", "alpha", n=2)
        baseline = sources(control, "alpha")
        assert baseline == ("note-brief", "note-dense")

        capture(router, clock, "note-dense", DENSE_TEXT, n=1)
        capture(router, clock, "note-brief", "alpha", n=2)
        fillers = [
            capture(router, clock, f"note-filler-{k}", FILLER_TEXT, n=3 + k)
            for k in range(3)
        ]
        # The sensitivity: while they are held the fillers do change the order.
        assert sources(router, "alpha") == ("note-dense", "note-brief")

        for k, filler in enumerate(fillers):
            tick(clock)
            erased(erase(router, filler, request_id=f"req-e{k}", key=f"idem-e{k}"))
            # Still found by nothing, and the order follows the shrinking frontier.
            assert sources(router, f"note-filler-{k}") == ()
        assert sources(router, "alpha") == baseline
        for query in ("alpha", "note", "p3", "dense brief"):
            assert sources(router, query) == sources(control, query)
        # The pack selects over the same frontier, in the same order as the control's.
        assert cited_sources(router, "note") == cited_sources(control, "note")
        assert "filler" not in pack_text(router, "note")
        # And the session material is the survivors' alone.
        session = fts.session_search_projection(owned.connection)
        assert session is not None and session.document_count == 2
    finally:
        control_owned.connection.close()


# --- shared bytes ------------------------------------------------------------------------


def test_lex_01_bytes_a_survivor_shares_are_kept_until_the_last_holder_is_erased(
    owned: cap.Served, router: ApplicationDispatcher, clock: cap.FakeClock
) -> None:
    """Two captures of the same text share one content address, so erasing one keeps the
    file for the other and says so; erasing the second removes it.

    Falsifier: an erase that unlinked the file whenever its own artifact went would leave
    the survivor findable by identity and unfindable by content, and fail the first block.
    """
    text = f"The shared {TARGET_WORD} paragraph."
    first = capture(router, clock, "note-first", text, n=1)
    second = capture(router, clock, "note-second", text, n=2)
    digest = cap.rows(
        owned,
        f"SELECT blob_content_digest FROM {ARTIFACTS} WHERE evidence_id = ?",
        first,
    )[0][0]
    shared = cap.blob_file(owned, digest)
    assert cap.found(router, TARGET_WORD) == (second, first)

    tick(clock)
    kept = erased(erase(router, first, request_id="req-e1", key="idem-e1"))
    assert (kept.erase_disposition, kept.content_disposition) == (
        "erased",
        "retained_shared",
    )
    assert shared.is_file()
    assert cap.rows(owned, f"SELECT 1 FROM {BLOBS} WHERE content_digest = ?", digest)
    assert cap.found(router, TARGET_WORD) == (second,)
    assert cap.rows(owned, f"SELECT blob_content_digest FROM {ERASURES}") == [(None,)]

    tick(clock)
    gone = erased(erase(router, second, request_id="req-e2", key="idem-e2"))
    assert gone.content_disposition == "erased"
    assert not shared.exists()
    assert cap.rows(owned, f"SELECT 1 FROM {BLOBS}") == []
    assert cap.found(router, TARGET_WORD) == ()

    # The same text captured again afterwards is new evidence with its own file.
    again = capture(router, clock, "note-third", text, n=3)
    assert again not in (first, second)
    assert shared.is_file()
    assert cap.found(router, TARGET_WORD) == (again,)


# --- the projection lifecycle ------------------------------------------------------------


def checkpoint_and_run(owned: cap.Served) -> tuple[str, int, str]:
    build = fts.current_build(owned.connection, workspace_id=WORKSPACE_ID)
    return build.run_id, build.epoch, build.source_checkpoint


def test_lex_01_every_erasure_starts_a_new_projection_run_and_reclaims_the_old_one(
    owned: cap.Served, router: ApplicationDispatcher, clock: cap.FakeClock
) -> None:
    """The newest evidence moves the checkpoint back; an older item leaves it where it
    was. Either way the run that indexed the erased document is superseded by one that did
    not, its documents are reclaimed, and the builder then finds nothing left to do.

    Falsifier: a builder that compared checkpoints alone would leave the second case on the
    old run, with the erased document still in the active run's rows.
    """
    older = capture(router, clock, "note-older", f"{KEEPER_WORD} one", n=1)
    middle = capture(router, clock, "note-middle", f"{KEEPER_WORD} two", n=2)
    newest = capture(router, clock, "note-newest", f"{KEEPER_WORD} three", n=3)
    run, epoch, checkpoint = checkpoint_and_run(owned)

    # The newest: the checkpoint falls back to the middle item's.
    tick(clock)
    erased(erase(router, newest, request_id="req-e1", key="idem-e1"))
    run_2, epoch_2, checkpoint_2 = checkpoint_and_run(owned)
    assert run_2 != run and epoch_2 > epoch and int(checkpoint_2) < int(checkpoint)
    assert {
        row[0] for row in cap.rows(owned, f"SELECT DISTINCT run_id FROM {DOCUMENTS}")
    } == {run_2}

    # An older item: the checkpoint does not move, and the run still changes.
    tick(clock)
    erased(erase(router, older, request_id="req-e2", key="idem-e2"))
    run_3, epoch_3, checkpoint_3 = checkpoint_and_run(owned)
    assert checkpoint_3 == checkpoint_2
    assert run_3 != run_2 and epoch_3 > epoch_2
    assert cap.rows(owned, f"SELECT evidence_id FROM {DOCUMENTS}") == [(middle,)]

    # Level, and the same call a restart makes does nothing.
    guard = evidence_handlers.read_guard(owned.connection)
    outcome = fts.build_search_projection(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=guard.fencing_generation,
        now_us=10**18,
    )
    assert (outcome.run_id, outcome.activated, outcome.document_count) == (
        run_3,
        False,
        1,
    )
    assert cap.found(router, KEEPER_WORD) == (middle,)


def test_lex_01_a_failure_after_the_commit_is_retryable_and_the_same_key_repairs_it(
    owned: cap.Served,
    router: ApplicationDispatcher,
    clock: cap.FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The erase commits and the rebuild then fails: the rows are gone, the answer is a
    retryable refusal, nothing erased can be found in the meantime, and a replay of the
    same key finishes the job and returns the original result.

    Falsifier: reporting success before the rebuild, or never finishing it, fails here.
    """
    keeper = capture(router, clock, "note-keeper", f"{KEEPER_WORD} entry", n=1)
    older = capture(router, clock, "note-older", f"{TARGET_WORD} entry", n=2)
    capture(router, clock, "note-newest", f"{KEEPER_WORD} entry two", n=3)
    real = evidence_handlers.build_search_projection
    calls: list[int] = []

    def failing(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        raise fts.ProjectionError("injected")

    monkeypatch.setattr(evidence_handlers, "build_search_projection", failing)
    tick(clock)
    refused = cap.refusal(erase(router, older))
    assert refused.error.code in ("projection_unavailable", "stale_projection")
    assert refused.error.retry_class == "retryable_after_delay"
    assert calls

    # Committed: the artifact is gone; the projection has not caught up.
    assert (
        cap.rows(owned, f"SELECT 1 FROM {ARTIFACTS} WHERE evidence_id = ?", older) == []
    )
    assert len(cap.rows(owned, f"SELECT 1 FROM {ERASURES}")) == 1
    assert cap.rows(owned, f"SELECT 1 FROM {DOCUMENTS} WHERE evidence_id = ?", older)
    # And the erased evidence is already unreachable: the frontier is the artifacts table.
    assert cap.found(router, TARGET_WORD) == ()
    assert older not in pack_text(router, "note-older")

    # The same key, with the builder working again, finishes it and answers as the first
    # attempt would have, writing no second receipt.
    monkeypatch.setattr(evidence_handlers, "build_search_projection", real)
    tick(clock)
    result = erased(erase(router, older))
    assert (result.erase_disposition, result.content_disposition) == (
        "erased",
        "erased",
    )
    assert len(cap.rows(owned, f"SELECT 1 FROM {ERASURES}")) == 1
    assert (
        cap.rows(owned, f"SELECT 1 FROM {DOCUMENTS} WHERE evidence_id = ?", older) == []
    )
    session = fts.session_search_projection(owned.connection)
    assert session is not None and older not in session.material
    assert keeper in session.material


def test_lex_01_a_restart_after_a_half_finished_erasure_converges_without_a_request(
    owned: cap.Served,
    router: ApplicationDispatcher,
    clock: cap.FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup runs the builder and nothing else, so it alone must retire the document an
    erase left behind in the active run -- even though the checkpoint never moved.

    Falsifier: a builder that trusts the checkpoint reports `activated=False` and leaves the
    erased document in the active run.
    """
    capture(router, clock, "note-keeper", f"{KEEPER_WORD} entry", n=1)
    older = capture(router, clock, "note-older", f"{TARGET_WORD} entry", n=2)
    capture(router, clock, "note-newest", f"{KEEPER_WORD} entry two", n=3)
    run_before = checkpoint_and_run(owned)[0]

    monkeypatch.setattr(
        evidence_handlers,
        "build_search_projection",
        lambda *a, **k: (_ for _ in ()).throw(fts.ProjectionError("injected")),
    )
    tick(clock)
    cap.refusal(erase(router, older))
    assert cap.rows(owned, f"SELECT 1 FROM {DOCUMENTS} WHERE evidence_id = ?", older)

    guard = evidence_handlers.read_guard(owned.connection)
    outcome = fts.build_search_projection(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=guard.fencing_generation,
        now_us=10**18,
    )
    assert outcome.activated and outcome.run_id != run_before
    assert (
        cap.rows(owned, f"SELECT 1 FROM {DOCUMENTS} WHERE evidence_id = ?", older) == []
    )
    projection = fts.open_search_projection(owned.connection, workspace_id=WORKSPACE_ID)
    assert older not in projection.material and projection.document_count == 2


# --- repeated requests -------------------------------------------------------------------


def test_lex_01_repeated_erase_requests_are_deterministic(
    owned: cap.Served, router: ApplicationDispatcher, clock: cap.FakeClock
) -> None:
    """One key is one erasure; a new key for the same evidence is `already_erased` with the
    same content disposition; reusing a key for other evidence is a conflict; evidence that
    never existed is `not_found`; and none of the repeats writes a second receipt.
    """
    target = capture(router, clock, "note-target", f"{TARGET_WORD} entry", n=1)
    other = capture(router, clock, "note-other", f"{KEEPER_WORD} entry", n=2)

    tick(clock)
    first = erase(router, target, request_id="req-1", key="idem-1")
    assert erased(first).erase_disposition == "erased"
    receipts = cap.rows(owned, f"SELECT * FROM {ERASURES}")
    assert len(receipts) == 1

    # The same key and body: the stored answer, the same audit reference, no new receipt.
    tick(clock)
    replay = erase(router, target, request_id="req-2", key="idem-1")
    assert cap.answered(replay).result == cap.answered(first).result
    assert (
        cap.answered(replay).metadata.audit_reference
        == cap.answered(first).metadata.audit_reference
    )
    assert cap.rows(owned, f"SELECT * FROM {ERASURES}") == receipts

    # A new key for the same evidence: a receipt-backed no-op with the same disposition.
    tick(clock)
    again = erased(erase(router, target, request_id="req-3", key="idem-3"))
    assert (again.erase_disposition, again.content_disposition) == (
        "already_erased",
        "erased",
    )
    assert cap.rows(owned, f"SELECT * FROM {ERASURES}") == receipts

    # The same key naming other evidence: refused, and the other evidence is untouched.
    tick(clock)
    conflict = erase(router, other, request_id="req-4", key="idem-1")
    assert code_of(conflict) == "idempotency_conflict"
    assert cap.rows(owned, f"SELECT 1 FROM {ARTIFACTS} WHERE evidence_id = ?", other)

    # Evidence that never existed: not found, and nothing at all is written for it.
    audits = cap.count(owned, AUDIT)
    tick(clock)
    missing = erase(router, "evd-never-existed", request_id="req-5", key="idem-5")
    assert code_of(missing) == "not_found"
    assert cap.count(owned, AUDIT) == audits
    assert cap.rows(owned, f"SELECT * FROM {ERASURES}") == receipts


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"evidence_id": ""},
        {"evidence_id": "has space"},
        {"evidence_id": "evd-1", "workspace_id": "ws-other"},
        {"evidence_id": 7},
        ["evd-1"],
    ],
    ids=["empty", "blank", "bad-id", "extra-field", "not-a-string", "not-an-object"],
)
def test_lex_01_a_malformed_erase_request_is_refused_before_any_write(
    owned: cap.Served,
    router: ApplicationDispatcher,
    clock: cap.FakeClock,
    payload: Any,
) -> None:
    capture(router, clock, "note-keeper", f"{KEEPER_WORD} entry", n=1)
    audits = cap.count(owned, AUDIT)
    tick(clock)
    response = erase(router, "ignored", payload=payload)
    assert code_of(response) == "invalid_request"
    assert cap.count(owned, AUDIT) == audits and cap.count(owned, ERASURES) == 0


# --- authority ---------------------------------------------------------------------------


def untouched(
    owned: cap.Served, router: ApplicationDispatcher, evidence_id: str
) -> None:
    """Nothing about this evidence moved: its rows, its file, its search and its receipt."""
    assert cap.rows(
        owned, f"SELECT 1 FROM {ARTIFACTS} WHERE evidence_id = ?", evidence_id
    )
    assert cap.rows(
        owned, f"SELECT 1 FROM {PROVENANCE} WHERE evidence_id = ?", evidence_id
    )
    assert cap.count(owned, ERASURES) == 0
    assert cap.found(router, TARGET_WORD) == (evidence_id,)
    digest = cap.rows(
        owned,
        f"SELECT blob_content_digest FROM {ARTIFACTS} WHERE evidence_id = ?",
        evidence_id,
    )[0][0]
    assert cap.blob_file(owned, digest).is_file()


def test_lex_01_unauthorized_and_cross_workspace_erasure_is_refused_and_changes_nothing(
    owned: cap.Served, router: ApplicationDispatcher, clock: cap.FakeClock
) -> None:
    target = capture(router, clock, "note-target", f"{TARGET_WORD} entry", n=1)
    tick(clock)

    # A family that holds no erase handler does not run one.
    unserved = build_reads(owned).dispatch(erase_request(target, request_id="req-r"))
    assert cap.refusal(unserved)
    untouched(owned, router, target)

    # The same router with the operation withdrawn from the session's grant.
    withdrawn = replace(
        router,
        session=replace(
            router.session,
            operations=router.session.operations - {EVIDENCE_ERASE_OPERATION},
        ),
    )
    assert code_of(withdrawn.dispatch(erase_request(target, request_id="req-n"))) == (
        "authorization_denied"
    )
    untouched(owned, router, target)

    # Another workspace: refused by the binding, and by the session grant.
    elsewhere = erase(
        router,
        target,
        request_id="req-w",
        key="idem-w",
        workspace_id=OTHER_WORKSPACE_ID,
    )
    assert code_of(elsewhere) == "workspace_not_granted"
    untouched(owned, router, target)

    # A capture grant is not an erase grant: the purpose it carries does not cover this.
    wrong_purpose = erase(
        router,
        target,
        request_id="req-p",
        key="idem-p",
        purpose=JOB_FAMILY_PURPOSES["evidence.capture"],
    )
    assert code_of(wrong_purpose) == "authorization_denied"
    untouched(owned, router, target)

    # A claim can narrow the grant and never widen it: claiming no roles removes the role
    # the mutation requires.
    request = erase_request(target, request_id="req-c", key="idem-c")
    narrowed = replace(
        request,
        metadata=replace(
            request.metadata, principal_claim=PrincipalClaim(claimed_roles=())
        ),
    )
    assert code_of(router.dispatch(narrowed)) == "authorization_denied"
    untouched(owned, router, target)

    # And a replay is authorised afresh: the erase that succeeds is the one under a grant.
    assert (
        erased(erase(router, target, request_id="req-ok", key="idem-ok")).evidence_id
        == target
    )


def test_lex_01_evidence_that_is_not_a_direct_capture_is_refused_and_left_whole(
    owned: cap.Served, router: ApplicationDispatcher
) -> None:
    """Evidence from an import, a document or an engineering capture belongs to lanes this
    operation cannot unwind, so it is refused as a conflict and nothing about it moves."""
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        v.insert(
            owned.connection,
            v.BLOBS,
            {
                "workspace_id": WORKSPACE_ID,
                "content_digest": v.DIGEST_A,
                "content_length_bytes": 10,
                "created_at_us": v.BASE_US - 100,
                "verified_at_us": v.BASE_US - 99,
            },
        )
        row = v.artifact_row("evd-foreign-1", workspace_id=WORKSPACE_ID)
        v.insert(owned.connection, v.EVIDENCE, row)
        v.insert(
            owned.connection,
            v.PROVENANCE,
            {**v.provenance_row("evd-foreign-1"), "workspace_id": WORKSPACE_ID},
        )
    audits = cap.count(owned, AUDIT)
    response = erase(router, "evd-foreign-1")
    assert code_of(response) == "conflict"
    assert cap.rows(
        owned, f"SELECT 1 FROM {ARTIFACTS} WHERE evidence_id = 'evd-foreign-1'"
    )
    assert cap.rows(
        owned, f"SELECT 1 FROM {PROVENANCE} WHERE evidence_id = 'evd-foreign-1'"
    )
    assert cap.count(owned, ERASURES) == 0 and cap.count(owned, AUDIT) == audits


def test_lex_01_a_refusal_after_the_receipt_is_written_rolls_the_whole_erasure_back(
    owned: cap.Served,
    router: ApplicationDispatcher,
    clock: cap.FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evidence another record depends on is refused, not partly erased: the dependents
    check runs before the first write, and a failure anywhere inside the transaction takes
    the receipt, the audit event and the claim with it.

    Governed citations, vector occurrences and the lanes that stage their own evidence are
    named in `DEPENDENTS`; this drives the same loop with a dependent that a capture does
    produce, so the refusal path runs end to end through the real dispatcher.
    """
    target = capture(router, clock, "note-target", f"{TARGET_WORD} entry", n=1)
    monkeypatch.setattr(
        evidence_erasure,
        "DEPENDENTS",
        (("omnivia_evidence_provenance_events", "evidence_id"),),
    )
    audits = cap.count(owned, AUDIT)
    tick(clock)
    response = erase(router, target)
    assert code_of(response) == "conflict"
    untouched(owned, router, target)
    assert cap.count(owned, AUDIT) == audits


def test_lex_01_a_capture_replayed_after_its_evidence_was_erased_is_refused(
    owned: cap.Served, router: ApplicationDispatcher, clock: cap.FakeClock
) -> None:
    """The stored success of the original capture names evidence that no longer exists.
    Serving it would also republish the bytes, putting erased content back in the blob root
    with no record to own it.

    Falsifier: a replay that publishes before it checks leaves the file on disk.
    """
    text = f"The {TARGET_WORD} entry."
    tick(clock)
    request = cap.capture_request(
        request_id="req-cap-1",
        key="idem-cap-1",
        source_native_id="note-target",
        text=text,
    )
    target = cap.captured(router.dispatch(request)).evidence_id
    digest = cap.rows(
        owned,
        f"SELECT blob_content_digest FROM {ARTIFACTS} WHERE evidence_id = ?",
        target,
    )[0][0]
    tick(clock)
    erased(erase(router, target))
    assert not cap.blob_file(owned, digest).exists()

    tick(clock)
    replay = router.dispatch(request)
    assert code_of(replay) == "conflict"
    assert not cap.blob_file(owned, digest).exists()
    assert cap.rows(owned, f"SELECT 1 FROM {ARTIFACTS}") == []
    assert cap.found(router, TARGET_WORD) == ()

    # A new key for the same source is new evidence, which is the way back.
    tick(clock)
    fresh = capture(router, clock, "note-target", text, n=2)
    assert fresh != target and cap.found(router, TARGET_WORD) == (fresh,)


# --- the schema ---------------------------------------------------------------------------

GUARDED_BY_RECEIPT = (
    "omnivia_evidence_artifacts",
    "omnivia_evidence_permission_labels",
    "omnivia_evidence_provenance_events",
    "omnivia_evidence_event_references",
    "omnivia_normalized_source_records",
    "omnivia_normalized_source_spans",
    "omnivia_staged_sources",
    "omnivia_blob_objects",
    "omnivia_blob_integrity_events",
)


def test_lex_01_every_delete_the_schema_allows_needs_a_receipt_that_names_the_row(
    owned: cap.Served, router: ApplicationDispatcher, clock: cap.FakeClock
) -> None:
    """The receipt is the only licence, and it is specific.

    Without one every one of the nine guarded tables still aborts a fenced owner's DELETE
    with the append-only message. With a receipt naming one piece of evidence, another
    piece's rows still abort, an unfenced writer still aborts, and the receipt itself can be
    neither rewritten nor removed nor written for evidence that is not there.
    """
    first = capture(router, clock, "note-first", f"{KEEPER_WORD} one", n=1)
    second = capture(router, clock, "note-second", f"{TARGET_WORD} two", n=2)
    attach_children(owned, first)
    attach_children(owned, second)
    audit_ref = cap.rows(owned, f"SELECT audit_ref FROM {AUDIT} LIMIT 1")[0][0]

    def fenced() -> Any:
        return fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        )

    for table in GUARDED_BY_RECEIPT:
        with pytest.raises(sqlite3.Error, match="append-only"), fenced():
            owned.connection.execute(f"DELETE FROM {table}")

    # A receipt for evidence that does not exist is refused.
    with pytest.raises(sqlite3.Error, match="evidence that exists"), fenced():
        owned.connection.execute(
            f"INSERT INTO {ERASURES} (audit_ref, workspace_id, evidence_id, erased_at_us) "
            "VALUES (?, ?, 'evd-nothing', 1)",
            (audit_ref, WORKSPACE_ID),
        )
    # A receipt may not name another artifact's bytes or staging.
    with pytest.raises(sqlite3.Error, match="its own artifact carries"), fenced():
        owned.connection.execute(
            f"INSERT INTO {ERASURES} (audit_ref, workspace_id, evidence_id, "
            "blob_content_digest, erased_at_us) VALUES (?, ?, ?, ?, 1)",
            (audit_ref, WORKSPACE_ID, first, "sha256:" + "0" * 64),
        )

    # A receipt for the first: the second's rows still abort, so does an unfenced delete.
    with fenced():
        owned.connection.execute(
            f"INSERT INTO {ERASURES} (audit_ref, workspace_id, evidence_id, erased_at_us) "
            "VALUES (?, ?, ?, 1)",
            (audit_ref, WORKSPACE_ID, first),
        )
    for table in ("omnivia_evidence_artifacts", "omnivia_evidence_provenance_events"):
        with pytest.raises(sqlite3.Error, match="append-only"), fenced():
            owned.connection.execute(
                f"DELETE FROM {table} WHERE evidence_id = ?", (second,)
            )
    with pytest.raises(sqlite3.Error):
        owned.connection.execute(
            "DELETE FROM omnivia_evidence_provenance_events WHERE evidence_id = ?",
            (first,),
        )
    # The receipt is append-only itself.
    for statement in (
        f"UPDATE {ERASURES} SET erased_at_us = 2",
        f"DELETE FROM {ERASURES}",
    ):
        with pytest.raises(sqlite3.Error, match="append-only"), fenced():
            owned.connection.execute(statement)


def test_lex_01_every_table_that_references_an_artifact_is_owned_or_a_named_dependent(
    owned: cap.Served,
) -> None:
    """A table added later that holds a foreign key to evidence cannot slip past the erase.

    The erase deletes `OWNED_CHILDREN` and refuses on `DEPENDENTS`; this holds the union of
    their tables to the schema's own list of foreign keys into an artifact, so a new
    reference forces a decision about which of the two it is.
    """
    connection = owned.connection
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    referrers = {
        table
        for table in tables
        for foreign_key in connection.execute(f"PRAGMA foreign_key_list({table})")
        if foreign_key[2] == "omnivia_evidence_artifacts"
    }
    named = set(evidence_erasure.OWNED_CHILDREN) | {
        table for table, _ in evidence_erasure.DEPENDENTS
    }
    assert referrers <= named, sorted(referrers - named)
    # And every owned child is reached: deleting them in order leaves no orphan the
    # artifact's own deletion would trip over, which the vertical test exercises live.
    assert set(evidence_erasure.OWNED_CHILDREN) <= set(GUARDED_BY_RECEIPT)


# --- the migration --------------------------------------------------------------------------


def _apply(connection: sqlite3.Connection, *, through: int) -> None:
    connection.executescript(phase0_baseline_sql())
    for migration in load_migrations():
        if migration.version <= through:
            connection.executescript(migration.sql)


def test_lex_01_migration_0075_is_the_successor_of_0074_and_replaces_exactly_nine_guards() -> (
    None
):
    versions = [migration.version for migration in load_migrations()]
    assert versions[-1] == 75 and versions[-2] == 74
    connection = sqlite3.connect(":memory:")
    try:
        _apply(connection, through=74)
        before = {
            name: sql
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        connection.executescript(
            next(m.sql for m in load_migrations() if m.version == 75)
        )
        after = {
            name: sql
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    finally:
        connection.close()
    changed = {name for name in before if after[name] != before[name]}
    assert changed == {
        f"omnivia_guard_{t[len('omnivia_') :]}_delete" for t in GUARDED_BY_RECEIPT
    }
    added = set(after) - set(before)
    assert added == {
        "omnivia_guard_evidence_erasures_insert",
        "omnivia_guard_evidence_erasures_update",
        "omnivia_guard_evidence_erasures_delete",
    }


def test_lex_01_migration_0075_refuses_a_database_whose_delete_guards_are_not_0008s() -> (
    None
):
    """The migration replaces a guard it has first proved is the one it expects."""
    connection = sqlite3.connect(":memory:")
    try:
        _apply(connection, through=74)
        connection.execute("DROP TRIGGER omnivia_guard_blob_objects_delete")
        connection.execute(
            "CREATE TRIGGER omnivia_guard_blob_objects_delete BEFORE DELETE ON "
            "omnivia_blob_objects BEGIN SELECT 1; END"
        )
        with pytest.raises(sqlite3.Error, match="migration 0075 requires"):
            connection.executescript(
                next(m.sql for m in load_migrations() if m.version == 75)
            )
    finally:
        connection.close()
