"""V06-8 acceptance for the connector SDK's coordination behaviour.

Every test here is about a property that only shows up when something goes
wrong: a crash between applying a batch and committing its cursor, a replay of
work already done, a cancellation mid-batch, a source that lies about its own
checksum, a connector holding a fencing generation it no longer owns. The happy
path is covered because the failure paths need somewhere to start from, not
because it is the interesting part.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.ingestion_coordinator import (
    CONTRACT_VIOLATION,
    IngestionCoordinator,
    IngestionRefused,
)
from omnivia_core_runtime.service.source_capture import MAX_SOURCE_BYTES
from omnivia_core_runtime.storage import connectors as connector_state

from omnivia_core.connector import (
    MAX_DEAD_LETTER_ATTEMPTS,
    ChangeKind,
    ConnectorCursor,
    ConnectorError,
    ConnectorFailure,
    HealthState,
    SourceBatch,
    SourceChange,
    SourceHealth,
)

WORKSPACE_ID = m1.WORKSPACE_ID
CONNECTOR_ID = "conn-alpha"
SOURCE_KIND = "document"


def digest_of(payload: bytes) -> str:
    return f"sha256:{sha256(payload).hexdigest()}"


def upsert(
    native_id: str,
    payload: bytes,
    *,
    locator: str | None = None,
    labels: tuple[str, ...] = (),
) -> SourceChange:
    return SourceChange(
        change_kind=ChangeKind.UPSERTED,
        native_id=native_id,
        locator=locator or f"/source/{native_id}",
        checksum=digest_of(payload),
        media_type="text/plain",
        content_length_bytes=len(payload),
        permission_labels=labels,
    )


def batch(*changes: SourceChange, token: str, has_more: bool = False) -> SourceBatch:
    return SourceBatch(
        changes=tuple(changes),
        cursor=ConnectorCursor(state_version=1, token=token),
        has_more=has_more,
    )


@dataclass
class FakeConnector:
    """A connector whose every fault is something a test asked for."""

    pages: list[SourceBatch]
    contents: dict[str, bytes]
    connector_id: str = CONNECTOR_ID
    source_kind: str = SOURCE_KIND
    state_version: int = 1
    reported_health: SourceHealth = field(
        default_factory=lambda: SourceHealth(state=HealthState.HEALTHY)
    )
    fetch_error: Exception | None = None
    content_errors: dict[str, list[Exception]] = field(default_factory=dict)
    fetched: list[ConnectorCursor | None] = field(default_factory=list)
    content_calls: list[str] = field(default_factory=list)

    def health(self) -> SourceHealth:
        return self.reported_health

    def fetch(
        self, cursor: ConnectorCursor | None, *, cancelled: Any
    ) -> SourceBatch:
        del cancelled
        self.fetched.append(cursor)
        if self.fetch_error is not None:
            raise self.fetch_error
        index = 0 if cursor is None else int(cursor.token)
        return self.pages[index]

    def content(self, change: SourceChange) -> bytes:
        self.content_calls.append(change.native_id)
        queued = self.content_errors.get(change.native_id)
        if queued:
            raise queued.pop(0)
        return self.contents[change.native_id]


class RefuseCheckpoint:
    """Forwarding proxy that dies exactly where a crash would hurt most.

    Between the last row of a batch and the cursor that says the batch is done.
    A proxy rather than a patch because the property under test is transactional
    rollback, which only means something against genuine SQLite semantics.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.refused = 0

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if "INSERT INTO omnivia_job_checkpoints" in " ".join(sql.split()):
            self.refused += 1
            raise RuntimeError("power loss before the checkpoint")
        return self._connection.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    m1.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


@pytest.fixture
def blobs(tmp_path: Path) -> Path:
    root = tmp_path / "blobs"
    root.mkdir()
    return root


def coordinator(
    holder: m1.Owned,
    blobs_root: Path,
    *,
    connection: Any = None,
    generation: int | None = None,
    **limits: Any,
) -> IngestionCoordinator:
    return IngestionCoordinator(
        connection=connection or holder.connection,
        identity=holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation if generation is None else generation,
        blobs_root=blobs_root,
        clock=FakeClock(),
        **limits,
    )


def artifacts(connection: sqlite3.Connection) -> list[tuple[str, str, str, str | None]]:
    return [
        (str(row[0]), str(row[1]), str(row[2]), None if row[3] is None else str(row[3]))
        for row in connection.execute(
            "SELECT evidence_id, source_native_id, content_checksum, source_locator "
            "FROM omnivia_evidence_artifacts ORDER BY source_native_id, evidence_id"
        ).fetchall()
    ]


def checkpoints(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT checkpoint_json FROM omnivia_job_checkpoints "
            "WHERE checkpoint_kind = 'connector.cursor' "
            "ORDER BY job_id, checkpoint_sequence"
        ).fetchall()
    ]


def rename(
    native_id: str, payload: bytes, *, was: str, now: str
) -> SourceChange:
    return SourceChange(
        change_kind=ChangeKind.RENAMED,
        native_id=native_id,
        locator=now,
        previous_locator=was,
        checksum=digest_of(payload),
        media_type="text/plain",
        content_length_bytes=len(payload),
    )


def provenance(connection: sqlite3.Connection) -> list[tuple[str, int | None]]:
    """Every provenance event in stream order, as (action, tombstone verdict)."""
    return [
        (str(row[0]), None if row[1] is None else int(row[1]))
        for row in connection.execute(
            "SELECT action, tombstoned_observation "
            "FROM omnivia_evidence_provenance_events ORDER BY provenance_sequence"
        ).fetchall()
    ]


def label_stream(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT permission_label, label_action "
            "FROM omnivia_evidence_permission_labels ORDER BY label_sequence"
        ).fetchall()
    ]


def moves(connection: sqlite3.Connection) -> list[str]:
    """Where each recorded move said the item went, in the order they happened."""
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT r.source_locator FROM omnivia_evidence_event_references r "
            "JOIN omnivia_evidence_provenance_events p "
            "  ON p.provenance_event_id = r.provenance_event_id "
            "WHERE p.action = 'source.renamed' ORDER BY p.provenance_sequence"
        ).fetchall()
    ]


def durable_text(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Every durable column this slice writes connector-authored text into."""
    parts: list[str] = []
    for sql in (
        "SELECT message FROM omnivia_connector_dead_letters",
        "SELECT detail FROM omnivia_connector_health_events",
        "SELECT error_json FROM omnivia_job_attempts",
        "SELECT message FROM omnivia_job_events",
    ):
        parts.extend(
            "" if row[0] is None else str(row[0])
            for row in connection.execute(sql).fetchall()
        )
    return tuple(parts)


# --- capture, identity preservation and replay --------------------------------


def test_initial_run_preserves_native_id_checksum_acl_and_locator(
    owned: m1.Owned, blobs: Path
) -> None:
    """The four facts a connector is the only source of all survive the round trip."""
    payload = b"alpha body"
    connector = FakeConnector(
        pages=[
            batch(
                upsert("doc-1", payload, locator="/drive/a.txt", labels=("team.a",)),
                token="1",
            )
        ],
        contents={"doc-1": payload},
    )
    outcome = coordinator(owned, blobs).synchronise(connector)

    assert (outcome.state, outcome.ingested, outcome.unchanged) == ("succeeded", 1, 0)
    rows = artifacts(owned.connection)
    assert len(rows) == 1
    _, native_id, checksum, locator = rows[0]
    assert (native_id, checksum, locator) == ("doc-1", digest_of(payload), "/drive/a.txt")

    labels = owned.connection.execute(
        "SELECT permission_label, label_action FROM omnivia_evidence_permission_labels"
    ).fetchall()
    assert [(str(a), str(b)) for a, b in labels] == [("team.a", "attached")]

    published = blobs / "sha256" / digest_of(payload).removeprefix("sha256:")
    assert published.read_bytes() == payload


def test_replaying_the_same_batch_recognises_its_own_work(
    owned: m1.Owned, blobs: Path
) -> None:
    """A second pass over identical facts ingests nothing and duplicates nothing."""
    payload = b"alpha body"
    page = batch(upsert("doc-1", payload), token="1")
    # The second entry is the same page again, so resuming from the committed
    # cursor re-delivers work already done -- exactly what a source does after a
    # run committed a batch and died before anyone learned it had.
    pages = [page, page]
    first = coordinator(owned, blobs).synchronise(
        FakeConnector(pages=pages, contents={"doc-1": payload})
    )
    second = coordinator(owned, blobs).synchronise(
        FakeConnector(pages=pages, contents={"doc-1": payload})
    )

    assert first.ingested == 1
    assert (second.ingested, second.unchanged) == (0, 1)
    assert len(artifacts(owned.connection)) == 1


def test_a_second_run_resumes_from_the_durable_cursor(
    owned: m1.Owned, blobs: Path
) -> None:
    """Continuous synchronisation starts where the last committed cursor points."""
    first_payload, second_payload = b"one", b"two"
    pages = [
        batch(upsert("doc-1", first_payload), token="1"),
        batch(upsert("doc-2", second_payload), token="2"),
    ]
    contents = {"doc-1": first_payload, "doc-2": second_payload}

    coordinator(owned, blobs).synchronise(FakeConnector(pages=pages, contents=contents))
    resumed = FakeConnector(pages=pages, contents=contents)
    outcome = coordinator(owned, blobs).synchronise(resumed)

    assert resumed.fetched == [ConnectorCursor(state_version=1, token="1")]
    assert outcome.ingested == 1
    assert [row[1] for row in artifacts(owned.connection)] == ["doc-1", "doc-2"]


# --- crash safety --------------------------------------------------------------


def test_a_crash_before_the_checkpoint_commits_nothing_and_replays_cleanly(
    owned: m1.Owned, blobs: Path
) -> None:
    """The ordering guarantee, exercised at the one instant it matters."""
    payload = b"alpha body"
    pages = [batch(upsert("doc-1", payload), token="1")]
    contents = {"doc-1": payload}
    proxy = RefuseCheckpoint(owned.connection)

    with pytest.raises(RuntimeError, match="power loss"):
        coordinator(owned, blobs, connection=proxy).synchronise(
            FakeConnector(pages=pages, contents=contents)
        )

    assert proxy.refused == 1
    # Neither half of the batch survived: no rows, and no cursor claiming there
    # were any.
    assert artifacts(owned.connection) == []
    assert checkpoints(owned.connection) == []
    assert (
        connector_state.read_resume_cursor(
            owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
        )
        is None
    )

    recovered = coordinator(owned, blobs).synchronise(
        FakeConnector(pages=pages, contents=contents)
    )
    assert recovered.ingested == 1
    assert len(artifacts(owned.connection)) == 1


def test_a_stale_fencing_generation_refuses_the_run(
    owned: m1.Owned, blobs: Path
) -> None:
    """Authority is proved inside the transaction, so a stale holder writes nothing."""
    payload = b"alpha body"
    stale = coordinator(owned, blobs, generation=owned.generation + 1)

    with pytest.raises(StaleGeneration):
        stale.synchronise(
            FakeConnector(
                pages=[batch(upsert("doc-1", payload), token="1")],
                contents={"doc-1": payload},
            )
        )

    assert artifacts(owned.connection) == []
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_connector_sync_runs"
        ).fetchone()[0]
        == 0
    )


# --- cancellation --------------------------------------------------------------


def test_cancellation_lands_on_a_boundary_and_keeps_the_last_cursor(
    owned: m1.Owned, blobs: Path
) -> None:
    """A cancelled run commits whole batches only, and says so."""
    first_payload, second_payload = b"one", b"two"
    pages = [
        batch(upsert("doc-1", first_payload), token="1", has_more=True),
        batch(upsert("doc-2", second_payload), token="2"),
    ]
    connector = FakeConnector(
        pages=pages, contents={"doc-1": first_payload, "doc-2": second_payload}
    )

    def cancelled() -> bool:
        # Cancelled the instant the first batch becomes durable, which is
        # expressed as the fact itself rather than as a count of observations:
        # a test that counted them would be measuring where the observations
        # are rather than that the run stops at a boundary.
        return (
            connector_state.read_resume_cursor(
                owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
            )
            is not None
        )

    outcome = coordinator(owned, blobs).synchronise(connector, cancelled=cancelled)

    assert outcome.state == "cancelled"
    assert [row[1] for row in artifacts(owned.connection)] == ["doc-1"]
    assert connector_state.read_resume_cursor(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    ) == ConnectorCursor(state_version=1, token="1")
    assert (
        owned.connection.execute(
            "SELECT state FROM omnivia_job_attempts WHERE job_id = ?",
            (outcome.run_id,),
        ).fetchone()[0]
        == "cancelled"
    )


def test_cancellation_during_the_last_content_fetch_abandons_the_whole_batch(
    owned: m1.Owned, blobs: Path
) -> None:
    """The window the between-items check cannot close from the inside.

    A batch's last `content()` call has no item after it, so a run that only
    looked between items would fetch the bytes, observe nothing, and commit a
    batch the caller had already cancelled. The observation therefore happens
    once more after preparation and before the transaction opens, and finding it
    true abandons the batch entirely: no rows, no cursor, nothing for the next
    run to reconcile. The bytes reaching the blob store is harmless and expected
    -- an unreferenced blob is what the ordering guarantee trades for never
    naming bytes that are not there.
    """
    payload = b"alpha body"
    connector = FakeConnector(
        pages=[batch(upsert("doc-1", payload), token="1")],
        contents={"doc-1": payload},
    )
    stopping = False
    fetch_content = connector.content

    def cancel_while_reading(change: SourceChange) -> bytes:
        nonlocal stopping
        content = fetch_content(change)
        stopping = True
        return content

    connector.content = cancel_while_reading  # type: ignore[method-assign]
    outcome = coordinator(owned, blobs).synchronise(
        connector, cancelled=lambda: stopping
    )

    assert outcome.state == "cancelled"
    assert connector.content_calls == ["doc-1"]
    assert (outcome.ingested, outcome.cursor) == (0, None)
    assert artifacts(owned.connection) == []
    assert checkpoints(owned.connection) == []
    assert (
        connector_state.read_resume_cursor(
            owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
        )
        is None
    )


# --- retry classification and dead letters -------------------------------------


def test_a_retryable_item_failure_is_retried_and_then_ingested(
    owned: m1.Owned, blobs: Path
) -> None:
    payload = b"alpha body"
    connector = FakeConnector(
        pages=[batch(upsert("doc-1", payload), token="1")],
        contents={"doc-1": payload},
        content_errors={
            "doc-1": [
                ConnectorError(
                    ConnectorFailure(
                        code="source_throttled",
                        message="slow down",
                        retry_class="retryable",
                    )
                )
            ]
        },
    )
    outcome = coordinator(owned, blobs).synchronise(connector)

    assert connector.content_calls == ["doc-1", "doc-1"]
    assert (outcome.ingested, outcome.dead_lettered) == (1, 0)


def test_a_non_retryable_item_failure_is_dead_lettered_without_a_retry(
    owned: m1.Owned, blobs: Path
) -> None:
    payload = b"alpha body"
    connector = FakeConnector(
        pages=[batch(upsert("doc-1", payload), token="1")],
        contents={"doc-1": payload},
        content_errors={
            "doc-1": [
                ConnectorError(
                    ConnectorFailure(
                        code="source_forbidden",
                        message="no access to this item",
                        retry_class="non_retryable",
                    )
                )
            ]
        },
    )
    outcome = coordinator(owned, blobs).synchronise(connector)

    assert connector.content_calls == ["doc-1"]
    assert (outcome.state, outcome.ingested, outcome.dead_lettered) == (
        "succeeded",
        0,
        1,
    )
    letters = connector_state.read_dead_letters(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    )
    assert len(letters) == 1
    assert letters[0].native_id == "doc-1"
    assert letters[0].failure.code == "source_forbidden"
    assert letters[0].attempts == 1
    # The run still committed its cursor: one poisoned item does not stall the
    # source behind it forever.
    assert connector_state.read_resume_cursor(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    ) == ConnectorCursor(state_version=1, token="1")


def test_an_exhausted_retryable_failure_is_dead_lettered_with_its_attempt_count(
    owned: m1.Owned, blobs: Path
) -> None:
    payload = b"alpha body"
    failure = ConnectorError(
        ConnectorFailure(
            code="source_unreachable", message="timeout", retry_class="retryable"
        )
    )
    connector = FakeConnector(
        pages=[batch(upsert("doc-1", payload), token="1")],
        contents={"doc-1": payload},
        content_errors={"doc-1": [failure, failure, failure]},
    )
    outcome = coordinator(owned, blobs, max_item_attempts=3).synchronise(connector)

    assert connector.content_calls == ["doc-1"] * 3
    assert outcome.dead_lettered == 1
    letters = connector_state.read_dead_letters(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    )
    assert (letters[0].attempts, letters[0].failure.retry_class) == (3, "retryable")


def test_an_item_that_dead_letters_on_two_pages_of_one_run_is_recorded_once(
    owned: m1.Owned, blobs: Path
) -> None:
    """A paginated listing may hand back the same item on a later page.

    That is the source's prerogative, not a contract violation: only a single
    batch is forbidden from repeating an item. If the item fails both times, the
    invariant that a run dead-letters an item at most once still has to hold --
    and it has to hold without taking the batch down with it. The second sighting
    therefore records nothing, the second batch commits its cursor like any
    other, and the run reaches a terminal outcome instead of escaping as an
    `IntegrityError` from a rolled-back transaction.
    """
    payload = b"alpha body"
    change = upsert("doc-1", payload)
    forbidden = ConnectorError(
        ConnectorFailure(
            code="source_forbidden",
            message="no access to this item",
            retry_class="non_retryable",
        )
    )
    connector = FakeConnector(
        pages=[
            batch(change, token="1", has_more=True),
            batch(change, upsert("doc-2", b"beta body"), token="2"),
        ],
        contents={"doc-1": payload, "doc-2": b"beta body"},
        content_errors={"doc-1": [forbidden, forbidden]},
    )
    outcome = coordinator(owned, blobs).synchronise(connector)

    assert (outcome.state, outcome.failure) == ("succeeded", None)
    assert connector.content_calls == ["doc-1", "doc-1", "doc-2"]
    # One durable row, so one dead letter counted: the count is of rows this run
    # created, not of times it saw the item fail.
    assert outcome.dead_lettered == 1
    letters = connector_state.read_dead_letters(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    )
    assert [(letter.native_id, letter.attempts) for letter in letters] == [("doc-1", 1)]
    # Both batches committed: the second was not rolled back by the duplicate,
    # so the item behind it landed and the cursor moved on twice.
    assert [
        json.loads(document)["token"] for document in checkpoints(owned.connection)
    ] == ["1", "2"]
    assert outcome.cursor == ConnectorCursor(state_version=1, token="2")
    assert connector_state.read_resume_cursor(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    ) == ConnectorCursor(state_version=1, token="2")
    assert (outcome.ingested, [row[1] for row in artifacts(owned.connection)]) == (
        1,
        ["doc-2"],
    )


def test_an_unclassified_connector_fault_is_treated_as_non_retryable(
    owned: m1.Owned, blobs: Path
) -> None:
    payload = b"alpha body"
    connector = FakeConnector(
        pages=[batch(upsert("doc-1", payload), token="1")],
        contents={"doc-1": payload},
        content_errors={"doc-1": [ZeroDivisionError("connector bug")]},
    )
    coordinator(owned, blobs).synchronise(connector)

    assert connector.content_calls == ["doc-1"]
    letters = connector_state.read_dead_letters(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    )
    assert letters[0].failure.code == "connector_fault"
    assert letters[0].failure.retry_class == "non_retryable"


def test_bytes_that_do_not_match_the_declared_checksum_are_never_ingested(
    owned: m1.Owned, blobs: Path
) -> None:
    """A source that disagrees with itself is refused, not believed.

    The substitute is the same length as the declaration on purpose, so the only
    thing wrong with it is its identity.
    """
    declared = upsert("doc-1", b"the declared body")
    connector = FakeConnector(
        pages=[batch(declared, token="1")], contents={"doc-1": b"the DECLARED body"}
    )
    outcome = coordinator(owned, blobs).synchronise(connector)

    assert (outcome.ingested, outcome.dead_lettered) == (0, 1)
    assert artifacts(owned.connection) == []
    letters = connector_state.read_dead_letters(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    )
    assert letters[0].failure.code == "content_checksum_mismatch"
    assert letters[0].failure.retry_class == "non_retryable"


def test_bytes_that_are_not_the_declared_length_are_dead_lettered(
    owned: m1.Owned, blobs: Path
) -> None:
    """The other declaration. Checked first, because it is the cheap one and
    because a length disagreement is already enough to refuse the item."""
    declared = upsert("doc-1", b"the declared body")
    connector = FakeConnector(
        pages=[batch(declared, token="1")], contents={"doc-1": b"short"}
    )
    outcome = coordinator(owned, blobs).synchronise(connector)

    assert (outcome.ingested, outcome.dead_lettered) == (0, 1)
    assert artifacts(owned.connection) == []
    letters = connector_state.read_dead_letters(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    )
    assert letters[0].failure.code == "content_length_mismatch"
    assert letters[0].failure.retry_class == "non_retryable"


def test_an_item_larger_than_the_capture_limit_is_refused_before_it_is_fetched(
    owned: m1.Owned, blobs: Path
) -> None:
    """The declaration is enough to refuse: fetching it is the harm."""
    payload = b"alpha body"
    oversized = dataclasses.replace(
        upsert("doc-1", payload), content_length_bytes=MAX_SOURCE_BYTES + 1
    )
    connector = FakeConnector(
        pages=[batch(oversized, token="1")], contents={"doc-1": payload}
    )
    outcome = coordinator(owned, blobs).synchronise(connector)

    assert connector.content_calls == []
    assert (outcome.ingested, outcome.dead_lettered) == (0, 1)
    letters = connector_state.read_dead_letters(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    )
    assert letters[0].failure.code == "content_too_large"


def test_a_fetch_failure_ends_the_run_and_is_recorded_on_the_attempt(
    owned: m1.Owned, blobs: Path
) -> None:
    connector = FakeConnector(
        pages=[],
        contents={},
        fetch_error=ConnectorError(
            ConnectorFailure(
                code="source_unreachable",
                message="the source refused the connection",
                retry_class="retryable_after_delay",
            )
        ),
    )
    outcome = coordinator(owned, blobs).synchronise(connector)

    assert outcome.state == "failed"
    assert outcome.failure is not None
    assert outcome.failure.retry_class == "retryable_after_delay"
    state, error_json = owned.connection.execute(
        "SELECT state, error_json FROM omnivia_job_attempts WHERE job_id = ?",
        (outcome.run_id,),
    ).fetchone()
    assert state == "failed"
    assert "source_unreachable" in str(error_json)


# --- rename, deletion and health ----------------------------------------------


def test_a_rename_records_the_move_against_the_existing_artifact(
    owned: m1.Owned, blobs: Path
) -> None:
    """A move is a new fact about a known item, not a new item."""
    payload = b"alpha body"
    coordinator(owned, blobs).synchronise(
        FakeConnector(
            pages=[batch(upsert("doc-1", payload, locator="/old.txt"), token="1")],
            contents={"doc-1": payload},
        )
    )
    renamed = SourceChange(
        change_kind=ChangeKind.RENAMED,
        native_id="doc-1",
        locator="/new.txt",
        previous_locator="/old.txt",
        checksum=digest_of(payload),
        media_type="text/plain",
        content_length_bytes=len(payload),
    )
    outcome = coordinator(owned, blobs).synchronise(
        FakeConnector(
            pages=[
                batch(upsert("doc-1", payload, locator="/old.txt"), token="1"),
                batch(renamed, token="2"),
            ],
            contents={"doc-1": payload},
        )
    )

    assert outcome.renamed == 1
    assert len(artifacts(owned.connection)) == 1
    reference = owned.connection.execute(
        "SELECT r.source_locator FROM omnivia_evidence_event_references r "
        "JOIN omnivia_evidence_provenance_events p "
        "  ON p.provenance_event_id = r.provenance_event_id "
        "WHERE p.action = 'source.renamed'"
    ).fetchone()
    assert reference is not None
    assert str(reference[0]) == "/new.txt"
    # The artifact keeps the locator it was captured at; the move is the event.
    assert artifacts(owned.connection)[0][3] == "/old.txt"


def test_a_deletion_is_a_tombstone_observation_and_repeats_harmlessly(
    owned: m1.Owned, blobs: Path
) -> None:
    payload = b"alpha body"
    coordinator(owned, blobs).synchronise(
        FakeConnector(
            pages=[batch(upsert("doc-1", payload), token="1")],
            contents={"doc-1": payload},
        )
    )
    deletion = SourceChange(change_kind=ChangeKind.DELETED, native_id="doc-1")
    pages = [
        batch(upsert("doc-1", payload), token="1"),
        batch(deletion, token="2"),
        batch(deletion, token="3"),
    ]
    second = coordinator(owned, blobs).synchronise(
        FakeConnector(pages=pages, contents={"doc-1": payload})
    )
    third = coordinator(owned, blobs).synchronise(
        FakeConnector(pages=pages, contents={"doc-1": payload})
    )

    assert second.deleted == 1
    assert third.deleted == 0
    assert len(artifacts(owned.connection)) == 1
    tombstones = owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_evidence_provenance_events "
        "WHERE action = 'source.deleted' AND tombstoned_observation = 1"
    ).fetchone()[0]
    assert tombstones == 1


def test_an_unavailable_source_fails_the_run_and_leaves_a_health_record(
    owned: m1.Owned, blobs: Path
) -> None:
    connector = FakeConnector(
        pages=[],
        contents={},
        reported_health=SourceHealth(
            state=HealthState.UNAVAILABLE, detail="credentials expired"
        ),
    )
    outcome = coordinator(owned, blobs).synchronise(connector)

    assert outcome.state == "failed"
    assert outcome.failure is not None
    assert outcome.failure.code == "source_unavailable"
    assert connector.fetched == []
    health = connector_state.read_latest_health(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    )
    assert health == SourceHealth(
        state=HealthState.UNAVAILABLE, detail="credentials expired"
    )


def test_a_health_probe_that_raises_is_itself_the_observation(
    owned: m1.Owned, blobs: Path
) -> None:
    connector = FakeConnector(pages=[], contents={})
    connector.health = lambda: (_ for _ in ()).throw(OSError("socket closed"))  # type: ignore[method-assign]
    outcome = coordinator(owned, blobs).synchronise(connector)

    assert outcome.state == "failed"
    health = connector_state.read_latest_health(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    )
    assert health is not None
    assert health.state is HealthState.UNAVAILABLE
    assert "socket closed" in health.detail


# --- connector state versioning -------------------------------------------------


def test_a_connector_that_moved_backwards_in_state_version_is_refused(
    owned: m1.Owned, blobs: Path
) -> None:
    """A downgrade would read a newer cursor under older rules. Fail closed."""
    payload = b"alpha body"
    pages = [batch(upsert("doc-1", payload), token="1")]
    connector = FakeConnector(
        pages=pages, contents={"doc-1": payload}, state_version=2
    )
    coordinator(owned, blobs).synchronise(connector)

    with pytest.raises(IngestionRefused, match="behind the durable"):
        coordinator(owned, blobs).synchronise(
            FakeConnector(pages=pages, contents={"doc-1": payload}, state_version=1)
        )


def test_a_newer_state_version_discards_the_cursor_rather_than_reinterpreting_it(
    owned: m1.Owned, blobs: Path
) -> None:
    payload = b"alpha body"
    pages = [
        batch(upsert("doc-1", payload), token="1"),
        batch(upsert("doc-2", b"two"), token="2"),
    ]
    contents = {"doc-1": payload, "doc-2": b"two"}
    coordinator(owned, blobs).synchronise(
        FakeConnector(pages=pages, contents=contents, state_version=1)
    )

    upgraded = FakeConnector(pages=pages, contents=contents, state_version=2)
    coordinator(owned, blobs).synchronise(upgraded)

    # The version moved, so the old token means nothing: the run starts over.
    assert upgraded.fetched == [None]


def test_a_batch_cursor_from_another_state_version_is_never_committed(
    owned: m1.Owned, blobs: Path
) -> None:
    """The one bad cursor that would outlive the run that accepted it.

    A committed cursor is read back by every later run. One carrying a state
    version its own connector does not write is refused on resume forever, so
    accepting it would poison the connector rather than merely fail a run.
    """
    payload = b"alpha body"
    stray = SourceBatch(
        changes=(upsert("doc-1", payload),),
        cursor=ConnectorCursor(state_version=9, token="1"),
    )
    connector = FakeConnector(pages=[stray], contents={"doc-1": payload})
    outcome = coordinator(owned, blobs).synchronise(connector)

    assert outcome.state == "failed"
    assert outcome.failure is not None
    assert outcome.failure.code == CONTRACT_VIOLATION
    assert artifacts(owned.connection) == []
    assert checkpoints(owned.connection) == []
    assert (
        connector_state.read_resume_cursor(
            owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
        )
        is None
    )
    # And the next run is a normal one, not a permanently refused one.
    recovered = coordinator(owned, blobs).synchronise(
        FakeConnector(
            pages=[batch(upsert("doc-1", payload), token="1")],
            contents={"doc-1": payload},
        )
    )
    assert recovered.ingested == 1


def test_more_work_behind_an_already_returned_cursor_ends_the_run(
    owned: m1.Owned, blobs: Path
) -> None:
    """`has_more` with a cursor that does not advance describes a loop.

    The loop would be inside `synchronise`, so it is refused rather than
    entered. One batch is still committed -- it was legitimate work -- and the
    run then fails with the violation that stopped it.
    """
    payload = b"alpha body"
    looping = batch(upsert("doc-1", payload), token="1", has_more=True)
    connector = FakeConnector(
        pages=[looping, looping], contents={"doc-1": payload}
    )
    outcome = coordinator(owned, blobs).synchronise(connector)

    assert outcome.state == "failed"
    assert outcome.failure is not None
    assert outcome.failure.code == CONTRACT_VIOLATION
    assert "already returned" in outcome.failure.message
    assert outcome.ingested == 1
    assert len(connector.fetched) == 2


def test_a_run_stops_at_its_own_limit_and_reports_that_it_did(
    owned: m1.Owned, blobs: Path
) -> None:
    """A bounded run, and a bound that is stated rather than silent."""
    payloads = {f"doc-{index}": f"body {index}".encode() for index in range(1, 4)}
    pages = [
        batch(
            upsert(native_id, payload),
            token=str(index),
            has_more=index < len(payloads),
        )
        for index, (native_id, payload) in enumerate(payloads.items(), start=1)
    ]
    outcome = coordinator(owned, blobs, max_batches_per_run=2).synchronise(
        FakeConnector(pages=pages, contents=payloads)
    )

    assert (outcome.state, outcome.truncated, outcome.ingested) == ("succeeded", True, 2)
    assert outcome.cursor == ConnectorCursor(state_version=1, token="2")
    # Nothing was dropped: the next run picks the remainder up from the cursor.
    resumed = coordinator(owned, blobs, max_batches_per_run=2).synchronise(
        FakeConnector(pages=pages, contents=payloads)
    )
    assert (resumed.truncated, resumed.ingested) == (False, 1)
    assert len(artifacts(owned.connection)) == 3


# --- resource bounds, decided before the bytes move ------------------------------


def ten_bytes(native_id: str) -> bytes:
    """Ten bytes, distinct per item, so a byte budget is easy to state exactly."""
    return native_id.encode("ascii").ljust(10, b".")[:10]


def two_pages() -> tuple[list[SourceBatch], dict[str, bytes]]:
    """Two single-item pages of ten declared bytes each."""
    payloads = {name: ten_bytes(name) for name in ("doc-1", "doc-2")}
    pages = [
        batch(upsert(name, payload), token=str(index), has_more=index == 1)
        for index, (name, payload) in enumerate(payloads.items(), start=1)
    ]
    return pages, payloads


def test_a_batch_that_does_not_fit_the_remaining_byte_budget_is_never_fetched(
    owned: m1.Owned, blobs: Path
) -> None:
    """The bound is decided from what the batch declares, before any of it moves.

    A batch is applied whole or not at all, so a budget checked *after* the bytes
    are in hand is not a budget: it bounds nothing and has already paid for the
    overrun. Checked against the declaration instead, the batch that does not fit
    costs one `fetch` of metadata and nothing else -- no `content()` call, no
    rows, and a cursor still naming the last batch that did fit.
    """
    pages, payloads = two_pages()
    connector = FakeConnector(pages=pages, contents=payloads)
    outcome = coordinator(
        owned, blobs, max_content_bytes_per_run=15
    ).synchronise(connector)

    assert (outcome.state, outcome.truncated, outcome.ingested) == (
        "succeeded",
        True,
        1,
    )
    assert connector.content_calls == ["doc-1"]
    assert outcome.cursor == ConnectorCursor(state_version=1, token="1")
    assert connector_state.read_resume_cursor(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    ) == ConnectorCursor(state_version=1, token="1")

    # Nothing was dropped and nothing needs reconciling: the deferred batch is
    # simply the next run's first one.
    resumed = FakeConnector(pages=pages, contents=payloads)
    second = coordinator(owned, blobs, max_content_bytes_per_run=25).synchronise(
        resumed
    )

    assert (second.truncated, second.ingested) == (False, 1)
    assert resumed.content_calls == ["doc-2"]
    assert [row[1] for row in artifacts(owned.connection)] == ["doc-1", "doc-2"]


def test_a_batch_no_run_could_afford_fails_rather_than_truncating_forever(
    owned: m1.Owned, blobs: Path
) -> None:
    """The one case truncation cannot resolve, so it must not be truncated.

    A batch declaring more than a whole run's ceiling cannot be split and cannot
    be deferred to a run with more budget, because there is no such run. Reported
    as truncation it would park the source behind it permanently while every run
    said `succeeded`. It is the connector's ceiling to respect, so it is failed
    as a contract violation -- deterministically, the same way every time.
    """
    pages, payloads = two_pages()
    outcomes = [
        coordinator(owned, blobs, max_content_bytes_per_run=5).synchronise(
            FakeConnector(pages=pages, contents=payloads)
        )
        for _ in range(2)
    ]

    for outcome in outcomes:
        assert (outcome.state, outcome.truncated) == ("failed", False)
        assert outcome.failure is not None
        assert outcome.failure.code == CONTRACT_VIOLATION
        assert outcome.failure.retry_class == "non_retryable"
    assert artifacts(owned.connection) == []
    assert checkpoints(owned.connection) == []
    assert (
        connector_state.read_resume_cursor(
            owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
        )
        is None
    )


def test_a_batch_that_exactly_exhausts_the_byte_budget_is_admitted(
    owned: m1.Owned, blobs: Path
) -> None:
    """The boundary itself, because an off-by-one here is silent data loss."""
    pages, payloads = two_pages()
    connector = FakeConnector(pages=pages, contents=payloads)
    outcome = coordinator(
        owned, blobs, max_content_bytes_per_run=20
    ).synchronise(connector)

    assert (outcome.state, outcome.truncated, outcome.ingested) == (
        "succeeded",
        False,
        2,
    )
    assert connector.content_calls == ["doc-1", "doc-2"]


def test_a_dead_lettered_item_still_spends_the_budget_its_batch_was_admitted_on(
    owned: m1.Owned, blobs: Path
) -> None:
    """Counted cost is declared cost, or the ceiling stops meaning anything.

    Counting fetched bytes instead would let a batch full of dead letters be
    admitted against its declaration and then charged nothing, so a source whose
    items all fail could hand over unlimited batches and every one would fit.
    """
    pages, payloads = two_pages()
    connector = FakeConnector(
        pages=pages,
        contents=payloads,
        content_errors={
            "doc-1": [
                ConnectorError(
                    ConnectorFailure(
                        code="source_forbidden",
                        message="no access to this item",
                        retry_class="non_retryable",
                    )
                )
            ]
        },
    )
    outcome = coordinator(
        owned, blobs, max_content_bytes_per_run=15
    ).synchronise(connector)

    # Ten declared bytes were spent on an item that stored none, and the second
    # batch is deferred exactly as if they had been stored.
    assert (outcome.dead_lettered, outcome.ingested, outcome.truncated) == (1, 0, True)
    assert connector.content_calls == ["doc-1"]
    assert outcome.cursor == ConnectorCursor(state_version=1, token="1")


def test_a_batch_that_does_not_fit_the_remaining_change_budget_is_never_fetched(
    owned: m1.Owned, blobs: Path
) -> None:
    """The change ceiling has the same shape, and gets the same treatment."""
    payloads = {name: ten_bytes(name) for name in ("doc-1", "doc-2", "doc-3", "doc-4")}
    items = list(payloads.items())
    pages = [
        batch(
            *[upsert(name, payload) for name, payload in items[:2]],
            token="1",
            has_more=True,
        ),
        batch(*[upsert(name, payload) for name, payload in items[2:]], token="2"),
    ]
    connector = FakeConnector(pages=pages, contents=payloads)
    outcome = coordinator(owned, blobs, max_changes_per_run=3).synchronise(connector)

    assert (outcome.state, outcome.truncated, outcome.ingested) == (
        "succeeded",
        True,
        2,
    )
    assert connector.content_calls == ["doc-1", "doc-2"]
    assert outcome.cursor == ConnectorCursor(state_version=1, token="1")

    resumed = FakeConnector(pages=pages, contents=payloads)
    assert coordinator(owned, blobs, max_changes_per_run=3).synchronise(
        resumed
    ).ingested == 2
    assert resumed.content_calls == ["doc-3", "doc-4"]


def test_a_batch_with_more_changes_than_a_whole_run_allows_fails(
    owned: m1.Owned, blobs: Path
) -> None:
    """Same reasoning as the byte ceiling: undeferrable work is not truncated."""
    payloads = {name: ten_bytes(name) for name in ("doc-1", "doc-2")}
    connector = FakeConnector(
        pages=[
            batch(
                *[upsert(name, payload) for name, payload in payloads.items()],
                token="1",
            )
        ],
        contents=payloads,
    )
    outcome = coordinator(owned, blobs, max_changes_per_run=1).synchronise(connector)

    assert (outcome.state, outcome.truncated) == ("failed", False)
    assert outcome.failure is not None
    assert outcome.failure.code == CONTRACT_VIOLATION
    assert connector.content_calls == []
    assert checkpoints(owned.connection) == []


@pytest.mark.parametrize(
    "limit",
    [
        "max_item_attempts",
        "max_batches_per_run",
        "max_changes_per_run",
        "max_content_bytes_per_run",
    ],
)
def test_a_limit_that_is_not_a_positive_integer_is_refused_at_construction(
    owned: m1.Owned, blobs: Path, limit: str
) -> None:
    """A misconfigured ceiling is a mistake, not a run outcome.

    Zero and negative admit nothing and would truncate or fail every run
    forever; `True` is an `int` that reads as a flag and quietly means one; a
    string compares in ways nobody intended. Refusing at construction is
    necessarily before any durable write, which is the property being asserted
    at the end.
    """
    for invalid in (0, -1, True, "3", 1.5):
        with pytest.raises(IngestionRefused, match=limit):
            coordinator(owned, blobs, **{limit: invalid})

    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_connector_sync_runs"
        ).fetchone()[0]
        == 0
    )


def test_more_item_attempts_than_a_dead_letter_can_record_is_refused(
    owned: m1.Owned, blobs: Path
) -> None:
    """The ceiling on retries is the durable `attempts` column's, not this
    module's.

    Configured above it, the first item to exhaust its retries would build a
    `DeadLetter` the contract refuses -- mid-run, after bytes had already moved,
    as an exception rather than a run outcome. So it is refused at construction,
    which is before any durable write.
    """
    with pytest.raises(IngestionRefused, match="max_item_attempts"):
        coordinator(owned, blobs, max_item_attempts=MAX_DEAD_LETTER_ATTEMPTS + 1)

    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_connector_sync_runs"
        ).fetchone()[0]
        == 0
    )
    # The bound itself is admissible: it is the most a dead letter can record,
    # not one more than it.
    coordinator(owned, blobs, max_item_attempts=MAX_DEAD_LETTER_ATTEMPTS)


# --- the connector as untrusted input --------------------------------------------


def test_a_connector_identity_outside_the_durable_domain_is_refused(
    owned: m1.Owned, blobs: Path
) -> None:
    """Refused before any durable write, and without quoting what was wrong.

    Echoing the offending value into the exception is how a hostile identity
    reaches a log or an audit column, so the refusal names the field only.
    """
    payload = b"alpha body"
    hostile = "conn'; DROP TABLE omnivia_evidence_artifacts; --"
    connector = FakeConnector(
        pages=[batch(upsert("doc-1", payload), token="1")],
        contents={"doc-1": payload},
        connector_id=hostile,
    )
    with pytest.raises(IngestionRefused) as refused:
        coordinator(owned, blobs).synchronise(connector)

    assert "connector_id" in str(refused.value)
    assert hostile not in str(refused.value)
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_connector_sync_runs"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"connector_id": ""}, "connector_id"),
        ({"connector_id": "x" * 129}, "connector_id"),
        ({"source_kind": "Document"}, "source_kind"),
        ({"source_kind": "doc kind"}, "source_kind"),
        ({"state_version": 0}, "state_version"),
        ({"state_version": True}, "state_version"),
        ({"state_version": "1"}, "state_version"),
    ],
)
def test_a_connector_boundary_value_outside_its_domain_is_refused(
    owned: m1.Owned, blobs: Path, overrides: dict[str, Any], expected: str
) -> None:
    connector = FakeConnector(pages=[], contents={}, **overrides)
    with pytest.raises(IngestionRefused, match=expected):
        coordinator(owned, blobs).synchronise(connector)


def test_a_fetch_that_does_not_return_a_source_batch_fails_the_run(
    owned: m1.Owned, blobs: Path
) -> None:
    connector = FakeConnector(pages=[], contents={})
    connector.fetch = lambda cursor, *, cancelled: {"changes": []}  # type: ignore[assignment,method-assign]
    outcome = coordinator(owned, blobs).synchronise(connector)

    assert outcome.state == "failed"
    assert outcome.failure is not None
    assert outcome.failure.code == CONTRACT_VIOLATION
    assert outcome.failure.retry_class == "non_retryable"


def test_content_that_is_not_bytes_is_dead_lettered_rather_than_hashed(
    owned: m1.Owned, blobs: Path
) -> None:
    payload = b"alpha body"
    connector = FakeConnector(
        pages=[batch(upsert("doc-1", payload), token="1")],
        contents={"doc-1": "alpha body"},  # type: ignore[dict-item]
    )
    outcome = coordinator(owned, blobs).synchronise(connector)

    assert (outcome.ingested, outcome.dead_lettered) == (0, 1)
    letters = connector_state.read_dead_letters(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    )
    assert letters[0].failure.code == CONTRACT_VIOLATION


def test_a_health_report_that_is_not_a_source_health_is_unavailable(
    owned: m1.Owned, blobs: Path
) -> None:
    connector = FakeConnector(pages=[], contents={})
    connector.health = lambda: "fine, thanks"  # type: ignore[assignment,method-assign]
    outcome = coordinator(owned, blobs).synchronise(connector)

    assert outcome.state == "failed"
    health = connector_state.read_latest_health(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    )
    assert health is not None
    assert health.state is HealthState.UNAVAILABLE


SECRETS = (
    "sk-live-4d1f9c2b7a8e3061f5c4b9d2e7a10836",
    "hunter2-correct-horse",
    "s3cr3t-basic-blob",
)


def test_no_connector_authored_secret_reaches_a_durable_column(
    owned: m1.Owned, blobs: Path
) -> None:
    """The normal way a credential leaks is an exception that quotes the request.

    A connector does not have to be malicious for this to happen once, and once
    is enough: these columns are append-only, so a secret that lands in one is
    there for the life of the workspace.
    """
    payloads = {name: name.encode() for name in ("doc-1", "doc-2", "doc-3")}
    connector = FakeConnector(
        pages=[
            batch(
                *[upsert(name, payload) for name, payload in payloads.items()],
                token="1",
            )
        ],
        contents=payloads,
        reported_health=SourceHealth(
            state=HealthState.DEGRADED,
            detail=f"retrying https://svc:{SECRETS[2]}@example.test/feed",
        ),
        content_errors={
            "doc-1": [
                ConnectorError(
                    ConnectorFailure(
                        code="source_forbidden",
                        message=f"GET /items rejected: Authorization: Bearer {SECRETS[0]}",
                        retry_class="non_retryable",
                    )
                )
            ],
            "doc-2": [RuntimeError(f"connect failed (password={SECRETS[1]})")],
            "doc-3": [
                ConnectorError(
                    ConnectorFailure(
                        code="source_unreachable",
                        message="lost\x07the\nconnection\ttwice",
                        retry_class="non_retryable",
                    )
                )
            ],
        },
    )
    coordinator(owned, blobs).synchronise(connector)

    stored = durable_text(owned.connection)
    written = " ".join(stored)
    for secret in SECRETS:
        assert secret not in written
    assert "[redacted]" in written
    # Hostile text is flattened rather than merely truncated: a durable column
    # that accepts a control character still has no use for one.
    assert "lost the connection twice" in written
    assert all(value.isprintable() for value in stored)
    # The classification survives redaction; only the wording is rewritten.
    letters = connector_state.read_dead_letters(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    )
    assert {letter.failure.code for letter in letters} == {
        "source_forbidden",
        "connector_fault",
        "source_unreachable",
    }


def test_a_secret_in_a_fetch_failure_never_reaches_the_attempt_record(
    owned: m1.Owned, blobs: Path
) -> None:
    connector = FakeConnector(
        pages=[],
        contents={},
        fetch_error=ConnectorError(
            ConnectorFailure(
                code="source_unreachable",
                message=f"refresh failed: api_key={SECRETS[0]}",
                retry_class="retryable_after_delay",
            )
        ),
    )
    outcome = coordinator(owned, blobs).synchronise(connector)

    assert outcome.failure is not None
    assert SECRETS[0] not in outcome.failure.message
    assert SECRETS[0] not in " ".join(durable_text(owned.connection))
    assert outcome.failure.retry_class == "retryable_after_delay"


# --- ACL changes ------------------------------------------------------------------


def test_an_acl_change_on_an_unchanged_item_is_recorded_and_replays_as_a_no_op(
    owned: m1.Owned, blobs: Path
) -> None:
    """The bytes are identical, so the artifact is; who may see it is not.

    The permission-label stream is the only authority for that, so bringing it
    current is an append -- withdrawals then attachments, both in label order --
    and reporting the same ACL again appends nothing.
    """
    payload = b"alpha body"
    pages = [
        batch(upsert("doc-1", payload, labels=("team.a", "team.b")), token="1"),
        batch(upsert("doc-1", payload, labels=("team.b", "team.c")), token="2"),
        batch(upsert("doc-1", payload, labels=("team.b", "team.c")), token="3"),
    ]
    contents = {"doc-1": payload}

    first = coordinator(owned, blobs).synchronise(
        FakeConnector(pages=pages, contents=contents)
    )
    second = coordinator(owned, blobs).synchronise(
        FakeConnector(pages=pages, contents=contents)
    )
    third = coordinator(owned, blobs).synchronise(
        FakeConnector(pages=pages, contents=contents)
    )

    assert (first.ingested, first.relabelled) == (1, 0)
    assert (second.ingested, second.relabelled, second.unchanged) == (0, 1, 0)
    assert (third.relabelled, third.unchanged) == (0, 1)
    assert label_stream(owned.connection) == [
        ("team.a", "attached"),
        ("team.b", "attached"),
        ("team.a", "withdrawn"),
        ("team.c", "attached"),
    ]
    assert len(artifacts(owned.connection)) == 1


def test_withdrawing_every_label_is_representable_and_still_idempotent(
    owned: m1.Owned, blobs: Path
) -> None:
    payload = b"alpha body"
    pages = [
        batch(upsert("doc-1", payload, labels=("team.a",)), token="1"),
        batch(upsert("doc-1", payload), token="2"),
        batch(upsert("doc-1", payload), token="3"),
    ]
    contents = {"doc-1": payload}
    for _ in range(3):
        coordinator(owned, blobs).synchronise(
            FakeConnector(pages=pages, contents=contents)
        )

    assert label_stream(owned.connection) == [
        ("team.a", "attached"),
        ("team.a", "withdrawn"),
    ]


# --- tombstone, reappearance and the rename cycle ------------------------------------


def test_delete_reappear_delete_keeps_every_observation_and_the_current_one(
    owned: m1.Owned, blobs: Path
) -> None:
    """A tombstone is what the stream last said, not something the item became.

    Reading the head rather than "has this ever been tombstoned" is what makes
    the reappearance representable at all, and what stops the second deletion
    from being mistaken for a replay of the first.
    """
    payload = b"alpha body"
    deletion = SourceChange(change_kind=ChangeKind.DELETED, native_id="doc-1")
    pages = [
        batch(upsert("doc-1", payload), token="1"),
        batch(deletion, token="2"),
        batch(upsert("doc-1", payload), token="3"),
        batch(deletion, token="4"),
        batch(deletion, token="5"),
    ]
    contents = {"doc-1": payload}
    outcomes = [
        coordinator(owned, blobs).synchronise(
            FakeConnector(pages=pages, contents=contents)
        )
        for _ in range(5)
    ]

    assert [outcome.ingested for outcome in outcomes] == [1, 0, 0, 0, 0]
    assert [outcome.deleted for outcome in outcomes] == [0, 1, 0, 1, 0]
    assert [outcome.restored for outcome in outcomes] == [0, 0, 1, 0, 0]
    assert [outcome.unchanged for outcome in outcomes] == [0, 0, 0, 0, 1]
    assert len(artifacts(owned.connection)) == 1
    assert provenance(owned.connection) == [
        ("source.ingested", 0),
        ("source.deleted", 1),
        ("source.ingested", 0),
        ("source.deleted", 1),
    ]


def test_a_rename_cycle_records_every_move_and_replays_as_a_no_op(
    owned: m1.Owned, blobs: Path
) -> None:
    """The same locator transition can happen twice, years apart.

    Deriving the event's identity from the pair of locators alone made the third
    move a duplicate of the first; deriving it from the stream position too, and
    deciding from where the item *is* rather than from what has been written,
    makes the move representable and the replay free.
    """
    payload = b"alpha body"
    pages = [
        batch(upsert("doc-1", payload, locator="/a.txt"), token="1"),
        batch(rename("doc-1", payload, was="/a.txt", now="/b.txt"), token="2"),
        batch(rename("doc-1", payload, was="/b.txt", now="/a.txt"), token="3"),
        batch(rename("doc-1", payload, was="/a.txt", now="/b.txt"), token="4"),
        batch(rename("doc-1", payload, was="/a.txt", now="/b.txt"), token="5"),
    ]
    contents = {"doc-1": payload}
    outcomes = [
        coordinator(owned, blobs).synchronise(
            FakeConnector(pages=pages, contents=contents)
        )
        for _ in range(5)
    ]

    assert [outcome.renamed for outcome in outcomes] == [0, 1, 1, 1, 0]
    assert outcomes[4].unchanged == 1
    assert moves(owned.connection) == ["/b.txt", "/a.txt", "/b.txt"]
    assert len(artifacts(owned.connection)) == 1
    # The artifact still says where it was captured; the moves are the stream.
    assert artifacts(owned.connection)[0][3] == "/a.txt"


def test_a_move_from_a_locator_the_item_has_left_is_a_no_op(
    owned: m1.Owned, blobs: Path
) -> None:
    """The whole rename rule, stated at its actual width and no wider.

    A move whose `previous_locator` is not the item's current locator has
    nothing to apply and is ignored; otherwise it is applied. That is
    applicability, not staleness detection, and the difference matters because
    an opaque cursor cannot support the wider claim: a source version carries no
    order either, so nothing here can tell an old A-to-B replayed after the item
    legitimately cycled back to A from a fresh A-to-B recurrence. It applies
    both -- correctly, since they are the same observation. The rename-cycle
    test is that case; this one is the other half, where the move simply no
    longer starts where the item is. Last delivered writer wins, and every
    earlier word is still in the stream.
    """
    payload = b"alpha body"
    pages = [
        batch(upsert("doc-1", payload, locator="/a.txt"), token="1"),
        batch(rename("doc-1", payload, was="/a.txt", now="/b.txt"), token="2"),
        batch(rename("doc-1", payload, was="/b.txt", now="/c.txt"), token="3"),
        # A move from /a.txt, which the item left two observations ago.
        batch(rename("doc-1", payload, was="/a.txt", now="/b.txt"), token="4"),
    ]
    contents = {"doc-1": payload}
    outcomes = [
        coordinator(owned, blobs).synchronise(
            FakeConnector(pages=pages, contents=contents)
        )
        for _ in range(4)
    ]

    assert [outcome.renamed for outcome in outcomes] == [0, 1, 1, 0]
    assert moves(owned.connection) == ["/b.txt", "/c.txt"]


def test_the_resume_cursor_is_the_last_committed_one_and_never_an_earlier_one(
    owned: m1.Owned, blobs: Path
) -> None:
    """Monotonicity, which is a property of the read rather than of the token.

    Tokens are opaque and unordered, so "latest" cannot mean the largest one. It
    means the single row the total order over (run sequence, checkpoint
    sequence) selects, and that order only ever grows.
    """
    payloads = {f"doc-{index}": f"body {index}".encode() for index in range(1, 5)}
    items = list(payloads.items())
    pages = [
        batch(upsert(name, payload), token=str(index), has_more=index < len(items))
        for index, (name, payload) in enumerate(items, start=1)
    ]
    # A poll that finds nothing still commits its cursor, which is what the third
    # run does and why the checkpoint count outruns the number of items.
    pages.append(batch(token=str(len(items))))
    seen: list[ConnectorCursor | None] = []
    for _ in range(3):
        coordinator(owned, blobs, max_batches_per_run=2).synchronise(
            FakeConnector(pages=pages, contents=payloads)
        )
        seen.append(
            connector_state.read_resume_cursor(
                owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
            )
        )

    assert [None if cursor is None else cursor.token for cursor in seen] == [
        "2",
        "4",
        "4",
    ]
    # Every cursor ever committed is still on disk; the read picks one of them.
    assert len(checkpoints(owned.connection)) == 5


def test_an_item_that_moved_and_changed_content_keeps_both_facts(
    owned: m1.Owned, blobs: Path
) -> None:
    """The case that made capture unconditional, and the one that decides which
    artifact later observations land on.

    The new bytes are a new artifact, born at the destination, so where it moved
    *from* exists nowhere but the move itself. Recording it is the only way that
    fact survives -- and the artifact a later deletion tombstones has to be the
    new one, which is what the run-sequence ordering guarantees.

    The two payloads are chosen so that the newer artifact's derived id sorts
    *below* the older one's. Both are captured at the same instant under the test
    clock, so a reader that fell back to the id to break that tie would tombstone
    the wrong artifact, and this test would say so.
    """
    before, after = b"first body", b"second body 4"
    moved = rename("doc-1", after, was="/a.txt", now="/b.txt")
    deletion = SourceChange(change_kind=ChangeKind.DELETED, native_id="doc-1")
    pages = [
        batch(upsert("doc-1", before, locator="/a.txt"), token="1"),
        batch(moved, token="2"),
        batch(moved, token="3"),
        batch(deletion, token="4"),
    ]
    contents = {"doc-1": before}
    outcomes = []
    for index in range(4):
        if index == 1:
            contents["doc-1"] = after
        outcomes.append(
            coordinator(owned, blobs).synchronise(
                FakeConnector(pages=pages, contents=dict(contents))
            )
        )

    assert [outcome.renamed for outcome in outcomes] == [0, 1, 0, 0]
    assert [outcome.deleted for outcome in outcomes] == [0, 0, 0, 1]
    # Two artifacts, one move recorded once, and the tombstone on the current one.
    rows = artifacts(owned.connection)
    assert {row[2] for row in rows} == {digest_of(before), digest_of(after)}
    assert {row[3] for row in rows} == {"/a.txt", "/b.txt"}
    assert moves(owned.connection) == ["/b.txt"]
    tombstoned = owned.connection.execute(
        "SELECT evidence_id FROM omnivia_evidence_provenance_events "
        "WHERE tombstoned_observation = 1"
    ).fetchall()
    assert len(tombstoned) == 1
    newest = owned.connection.execute(
        "SELECT evidence_id FROM omnivia_evidence_artifacts "
        "WHERE content_checksum = ?",
        (digest_of(after),),
    ).fetchone()
    assert str(tombstoned[0][0]) == str(newest[0])
