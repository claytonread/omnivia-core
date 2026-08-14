"""V06-8 acceptance for `IngestionCoordinator.synchronise_spi` against the
actual `FilesystemSourceConnector`.

Reuses the durable-workspace and blob-store setup from
`test_v06_8_ingestion_coordinator` (the `owned`/`blobs` fixtures and the
`coordinator()` helper) rather than duplicating it.
"""

# Imported fixtures intentionally share their pytest parameter names.
# ruff: noqa: F811

from __future__ import annotations

import dataclasses
import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import omnivia_core_runtime.service.ingestion_coordinator as coordinator_module
import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.ingestion_coordinator import (
    SPI_MAX_RUN_ATTEMPTS,
    SPI_RUN_OPERATION,
    SPI_TELEMETRY_SIGNALS,
    IngestionCoordinator,
    IngestionRefused,
)
from omnivia_core_runtime.storage import connectors as connector_state
from test_v06_8_ingestion_coordinator import blobs, owned  # noqa: F401

from omnivia_core.connector import ConnectorCursor
from omnivia_core.connector.filesystem import FilesystemSourceConnector
from omnivia_core.connector.spi import (
    ERROR_CONNECTOR_CURSOR_FOREIGN,
    ERROR_CONNECTOR_CURSOR_NOT_MONOTONIC,
    ERROR_CONNECTOR_STATE_INVALID,
    Batch,
    ConnectorRefused,
    CursorBinding,
    CursorRecord,
    CursorState,
)
from omnivia_core.contracts.v1 import to_canonical_json

WORKSPACE_ID = m1.WORKSPACE_ID
CONNECTOR_ID = "local.filesystem"

# A wall time safely after any real file mtime this suite creates: the
# durable `observed_at_us <= ingested_at_us` check compares a real file's
# mtime (set by the OS, "now") against the coordinator clock's wall time, so
# the fixed-past default `FakeClock` from the reused ingestion-coordinator
# suite -- built for a synthetic `SourceChange` stream, never a real clock --
# cannot be reused here.
_FUTURE_WALL = datetime(2999, 1, 1, tzinfo=UTC)


def coordinator(
    holder: m1.Owned, blobs_root: Path, *, connection=None, generation: int | None = None, **limits
) -> IngestionCoordinator:
    return IngestionCoordinator(
        connection=connection or holder.connection,
        identity=holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation if generation is None else generation,
        blobs_root=blobs_root,
        clock=FakeClock(wall=_FUTURE_WALL),
        **limits,
    )


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    return root


def write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


def checkpoint_documents(connection, job_id: str) -> list[dict]:
    return [
        json.loads(str(row[0]))
        for row in connection.execute(
            "SELECT checkpoint_json FROM omnivia_job_checkpoints "
            "WHERE checkpoint_kind = 'connector.cursor' AND job_id = ? "
            "ORDER BY checkpoint_sequence",
            (job_id,),
        ).fetchall()
    ]


class _NotBuiltIn:
    """Not a `FilesystemSourceConnector`, so `synchronise_spi` must refuse it
    before calling any of its four operations."""

    def describe(self):  # pragma: no cover - never reached
        raise AssertionError("describe must never be called")


class _FilesystemSubclass(FilesystemSourceConnector):
    """A subclass is not the exact built-in type and is therefore third-party."""


def test_initial_ingest_is_durable_content_evidence_and_checkpoint(
    owned: m1.Owned, blobs: Path, source_root: Path
) -> None:
    write(source_root, "a.txt", "alpha body")
    connector = FilesystemSourceConnector(root=source_root)
    outcome = coordinator(owned, blobs).synchronise_spi(connector)

    assert (outcome.state, outcome.ingested, outcome.unchanged) == ("succeeded", 1, 0)
    row = owned.connection.execute(
        "SELECT source_native_id, content_checksum FROM omnivia_evidence_artifacts"
    ).fetchone()
    assert row is not None
    native_id, checksum = str(row[0]), str(row[1])
    published = blobs / "sha256" / checksum.removeprefix("sha256:")
    assert published.read_bytes() == b"alpha body"
    assert (
        owned.connection.execute(
            "SELECT source_native_id FROM omnivia_evidence_provenance_events "
            "WHERE action = 'source.ingested'"
        ).fetchone()[0]
        == native_id
    )
    assert checkpoint_documents(owned.connection, outcome.run_id)


def test_blob_is_published_before_the_fenced_database_transaction(
    owned: m1.Owned,
    blobs: Path,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(source_root, "a.txt", "alpha body")
    original = coordinator_module.publish_blob

    def checked_publish(root: Path, digest: str, content: bytes) -> Path:
        assert not owned.connection.in_transaction
        return original(root, digest, content)

    monkeypatch.setattr(coordinator_module, "publish_blob", checked_publish)
    outcome = coordinator(owned, blobs).synchronise_spi(
        FilesystemSourceConnector(root=source_root)
    )

    assert outcome.state == "succeeded"


def test_run_recorded_as_import_start_ingestion_import_max_attempts_three(
    owned: m1.Owned, blobs: Path, source_root: Path
) -> None:
    write(source_root, "a.txt", "alpha body")
    outcome = coordinator(owned, blobs).synchronise_spi(
        FilesystemSourceConnector(root=source_root)
    )

    job_type = owned.connection.execute(
        "SELECT job_type FROM omnivia_durable_jobs WHERE job_id = ?",
        (outcome.run_id,),
    ).fetchone()[0]
    job_kind, originating_operation, max_attempts = owned.connection.execute(
        "SELECT job_kind, originating_operation, max_attempts "
        "FROM omnivia_job_application_metadata WHERE job_id = ?",
        (outcome.run_id,),
    ).fetchone()
    assert str(job_type) == "ingestion.import"
    assert str(job_kind) == "ingestion.import"
    assert str(originating_operation) == SPI_RUN_OPERATION == "import.start"
    assert int(max_attempts) == SPI_MAX_RUN_ATTEMPTS == 3
    assert SPI_TELEMETRY_SIGNALS == frozenset(
        {"connector.health", "connector.retry", "connector.dead_letter"}
    )


def test_checkpoint_json_carries_exact_binding_and_four_state_fields(
    owned: m1.Owned, blobs: Path, source_root: Path
) -> None:
    write(source_root, "a.txt", "alpha body")
    outcome = coordinator(owned, blobs).synchronise_spi(
        FilesystemSourceConnector(root=source_root)
    )

    [document] = checkpoint_documents(owned.connection, outcome.run_id)
    assert document["binding"] == {
        "workspace_id": WORKSPACE_ID,
        "connector_id": CONNECTOR_ID,
    }
    state = document["state"]
    assert set(state) == {
        "state_version",
        "payload",
        "witness_seq",
        "predecessor_digest",
    }
    assert state["state_version"] == 1
    assert state["witness_seq"] == 0
    assert isinstance(state["payload"], str)


def test_second_run_resumes_idempotently_and_records_deletion_tombstone(
    owned: m1.Owned, blobs: Path, source_root: Path
) -> None:
    path = write(source_root, "a.txt", "alpha body")
    first = coordinator(owned, blobs).synchronise_spi(
        FilesystemSourceConnector(root=source_root)
    )
    assert (first.ingested, first.deleted) == (1, 0)

    unchanged = coordinator(owned, blobs).synchronise_spi(
        FilesystemSourceConnector(root=source_root)
    )
    assert (unchanged.ingested, unchanged.unchanged, unchanged.deleted) == (0, 1, 0)

    path.unlink()
    deleted = coordinator(owned, blobs).synchronise_spi(
        FilesystemSourceConnector(root=source_root)
    )
    assert deleted.deleted == 1
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_evidence_artifacts"
        ).fetchone()[0]
        == 1
    )
    tombstones = owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_evidence_provenance_events "
        "WHERE action = 'source.deleted' AND tombstoned_observation = 1"
    ).fetchone()[0]
    assert tombstones == 1


def test_invalid_permission_label_leaves_no_durable_mutation(
    owned: m1.Owned,
    blobs: Path,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(source_root, "a.txt", "alpha body")
    original = FilesystemSourceConnector.poll

    def injecting_poll(self, ctx, cursor):
        for one in original(self, ctx, cursor):
            yield Batch(
                observations=tuple(
                    dataclasses.replace(o, permission_labels=("workspace.guest",))
                    if not o.deleted
                    else o
                    for o in one.observations
                ),
                successor_cursor=one.successor_cursor,
                item_failures=one.item_failures,
            )

    monkeypatch.setattr(FilesystemSourceConnector, "poll", injecting_poll)
    outcome = coordinator(owned, blobs).synchronise_spi(
        FilesystemSourceConnector(root=source_root)
    )

    assert outcome.state == "failed"
    assert outcome.failure is not None
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_evidence_artifacts"
    ).fetchone()[0] == 0
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_blob_objects"
    ).fetchone()[0] == 0
    assert connector_state.read_spi_resume_cursor(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    ) is None


def test_tampered_checksum_leaves_no_durable_mutation(
    owned: m1.Owned,
    blobs: Path,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(source_root, "a.txt", "alpha body")
    original = FilesystemSourceConnector.poll

    def tampering_poll(self, ctx, cursor):
        for one in original(self, ctx, cursor):
            yield Batch(
                observations=tuple(
                    dataclasses.replace(o, content=b"not the bytes that were read")
                    if o.content is not None
                    else o
                    for o in one.observations
                ),
                successor_cursor=one.successor_cursor,
                item_failures=one.item_failures,
            )

    monkeypatch.setattr(FilesystemSourceConnector, "poll", tampering_poll)
    outcome = coordinator(owned, blobs).synchronise_spi(
        FilesystemSourceConnector(root=source_root)
    )

    assert outcome.state == "failed"
    assert outcome.failure is not None
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_evidence_artifacts"
    ).fetchone()[0] == 0
    assert connector_state.read_spi_resume_cursor(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    ) is None


def test_witness_regression_is_refused_and_leaves_no_durable_mutation(
    owned: m1.Owned,
    blobs: Path,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(source_root, "a.txt", "alpha body")
    coordinator(owned, blobs).synchronise_spi(FilesystemSourceConnector(root=source_root))
    coordinator(owned, blobs).synchronise_spi(FilesystemSourceConnector(root=source_root))
    before = connector_state.read_spi_resume_cursor(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    )
    assert before is not None and before.state.witness_seq == 1

    original = FilesystemSourceConnector.poll

    def regressing_poll(self, ctx, cursor):
        for one in original(self, ctx, cursor):
            yield Batch(
                observations=one.observations,
                successor_cursor=dataclasses.replace(
                    one.successor_cursor, witness_seq=0
                ),
                item_failures=one.item_failures,
            )

    monkeypatch.setattr(FilesystemSourceConnector, "poll", regressing_poll)
    outcome = coordinator(owned, blobs).synchronise_spi(
        FilesystemSourceConnector(root=source_root)
    )

    assert outcome.state == "failed"
    assert outcome.failure is not None
    assert outcome.failure.code == ERROR_CONNECTOR_CURSOR_NOT_MONOTONIC
    after = connector_state.read_spi_resume_cursor(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    )
    assert after == before
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_evidence_artifacts"
        ).fetchone()[0]
        == 1
    )


def test_bad_successor_lineage_is_refused_not_repaired(
    owned: m1.Owned,
    blobs: Path,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(source_root, "a.txt", "alpha body")
    original = FilesystemSourceConnector.poll

    def tampering_poll(self, ctx, cursor):
        for one in original(self, ctx, cursor):
            yield Batch(
                observations=one.observations,
                successor_cursor=dataclasses.replace(
                    one.successor_cursor, predecessor_digest=b"\x00" * 32
                ),
                item_failures=one.item_failures,
            )

    monkeypatch.setattr(FilesystemSourceConnector, "poll", tampering_poll)
    outcome = coordinator(owned, blobs).synchronise_spi(
        FilesystemSourceConnector(root=source_root)
    )

    assert outcome.state == "failed"
    assert outcome.failure is not None
    assert outcome.failure.code == "connector_state_invalid"
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_evidence_artifacts"
    ).fetchone()[0] == 0


def test_non_builtin_connector_is_refused_before_any_durable_row(
    owned: m1.Owned, blobs: Path, source_root: Path
) -> None:
    with pytest.raises(IngestionRefused, match="built-in"):
        coordinator(owned, blobs).synchronise_spi(_NotBuiltIn())  # type: ignore[arg-type]
    with pytest.raises(IngestionRefused, match="built-in"):
        coordinator(owned, blobs).synchronise_spi(
            _FilesystemSubclass(root=source_root)
        )

    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_connector_sync_runs"
        ).fetchone()[0]
        == 0
    )
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_durable_jobs"
        ).fetchone()[0]
        == 0
    )


def test_cancellation_lands_on_a_batch_boundary(
    owned: m1.Owned, blobs: Path, source_root: Path
) -> None:
    write(source_root, "a.txt", "alpha body")
    outcome = coordinator(owned, blobs).synchronise_spi(
        FilesystemSourceConnector(root=source_root), cancelled=lambda: True
    )

    assert outcome.state == "cancelled"
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_evidence_artifacts"
    ).fetchone()[0] == 0
    assert connector_state.read_spi_resume_cursor(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    ) is None


def test_equal_observed_time_items_ingest_in_stable_order(
    owned: m1.Owned,
    blobs: Path,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = write(source_root, "a.txt", "one body")
    b = write(source_root, "b.txt", "two body")
    same_time = 1_700_000_000
    os.utime(a, (same_time, same_time))
    os.utime(b, (same_time, same_time))
    applied: list[str] = []
    original = IngestionCoordinator._apply_spi_observation

    def recording_apply(self, run, bound, observation, at_us):
        applied.append(observation.source_native_id)
        return original(self, run, bound, observation, at_us)

    monkeypatch.setattr(
        IngestionCoordinator, "_apply_spi_observation", recording_apply
    )

    outcome = coordinator(owned, blobs).synchronise_spi(
        FilesystemSourceConnector(root=source_root)
    )

    assert outcome.ingested == 2
    assert len(applied) == 2
    assert applied == sorted(applied)


def test_unsupported_item_produces_one_run_scoped_dead_letter_with_attempts_three(
    owned: m1.Owned, blobs: Path, source_root: Path
) -> None:
    write(source_root, "notes.bin", "not a supported type")
    outcome = coordinator(owned, blobs).synchronise_spi(
        FilesystemSourceConnector(root=source_root)
    )

    assert outcome.dead_lettered == 1
    letters = connector_state.read_dead_letters(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        connector_id=CONNECTOR_ID,
        run_id=outcome.run_id,
    )
    assert len(letters) == 1
    assert letters[0].attempts == 3
    assert letters[0].failure.code == "filesystem_item_unsupported_type"
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_connector_dead_letters"
        ).fetchone()[0]
        == 1
    )


def test_absent_cursor_causes_a_full_resync(
    owned: m1.Owned, blobs: Path, source_root: Path
) -> None:
    write(source_root, "a.txt", "alpha body")
    assert (
        connector_state.read_spi_resume_cursor(
            owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
        )
        is None
    )

    outcome = coordinator(owned, blobs).synchronise_spi(
        FilesystemSourceConnector(root=source_root)
    )

    assert outcome.ingested == 1
    after = connector_state.read_spi_resume_cursor(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    )
    assert after is not None
    assert after.state.witness_seq == 0


def test_legacy_two_field_cursor_is_not_reinterpreted_as_spi_state(
    owned: m1.Owned, blobs: Path, source_root: Path
) -> None:
    service = coordinator(owned, blobs)
    connector = FilesystemSourceConnector(root=source_root)
    descriptor = connector.describe()
    run = service._start_spi_run(service._bind_spi(connector, descriptor))
    write(source_root, "a.txt", "alpha body")
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        connector_state.write_cursor_checkpoint(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            run_id=run.run_id,
            attempt_number=1,
            cursor=ConnectorCursor(state_version=1, token="legacy-token"),
            created_at_us=run.started_at_us,
        )

    assert (
        connector_state.read_spi_resume_cursor(
            owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
        )
        is None
    )

    outcome = coordinator(owned, blobs).synchronise_spi(
        FilesystemSourceConnector(root=source_root)
    )

    assert outcome.ingested == 1
    after = connector_state.read_spi_resume_cursor(
        owned.connection, workspace_id=WORKSPACE_ID, connector_id=CONNECTOR_ID
    )
    assert after is not None
    assert after.state.witness_seq == 0


def test_foreign_spi_cursor_is_refused_instead_of_becoming_a_resync(
    owned: m1.Owned, blobs: Path, source_root: Path
) -> None:
    service = coordinator(owned, blobs)
    connector = FilesystemSourceConnector(root=source_root)
    run = service._start_spi_run(service._bind_spi(connector, connector.describe()))
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        connector_state.write_spi_cursor_checkpoint(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            run_id=run.run_id,
            attempt_number=1,
            record=CursorRecord(
                binding=CursorBinding(
                    workspace_id="ws-m1-audit-9999", connector_id=CONNECTOR_ID
                ),
                state=CursorState(state_version=1, payload=b"", witness_seq=0),
            ),
            created_at_us=run.started_at_us,
        )

    with pytest.raises(ConnectorRefused) as raised:
        service.synchronise_spi(connector)
    assert raised.value.error == ERROR_CONNECTOR_CURSOR_FOREIGN


def test_malformed_spi_checkpoint_fails_closed(
    owned: m1.Owned, blobs: Path, source_root: Path
) -> None:
    service = coordinator(owned, blobs)
    connector = FilesystemSourceConnector(root=source_root)
    run = service._start_spi_run(service._bind_spi(connector, connector.describe()))
    document = to_canonical_json(
        {
            "binding": {
                "workspace_id": WORKSPACE_ID,
                "connector_id": CONNECTOR_ID,
            },
            "state": {
                "state_version": True,
                "payload": "",
                "witness_seq": 0,
                "predecessor_digest": None,
            },
        }
    )
    digest = "sha256:" + sha256(document.encode("utf-8")).hexdigest()
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        owned.connection.execute(
            "INSERT INTO omnivia_job_checkpoints "
            "(workspace_id, job_id, checkpoint_sequence, attempt_number, created_at_us, "
            "checkpoint_kind, checkpoint_json, checkpoint_digest) "
            "VALUES (?, ?, 0, 1, ?, 'connector.cursor', ?, ?)",
            (WORKSPACE_ID, run.run_id, run.started_at_us, document, digest),
        )

    with pytest.raises(ConnectorRefused) as raised:
        connector_state.read_spi_resume_cursor(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            connector_id=CONNECTOR_ID,
        )
    assert raised.value.error == ERROR_CONNECTOR_STATE_INVALID


def test_filesystem_connector_never_needs_the_credential_resolver(
    owned: m1.Owned, blobs: Path, source_root: Path
) -> None:
    """`_refuse_credential` raises the instant it is called, so a `succeeded`
    outcome over a source with content to fetch is proof the filesystem
    connector never called `ctx.resolve_credential`."""
    write(source_root, "a.txt", "alpha body")
    outcome = coordinator(owned, blobs).synchronise_spi(
        FilesystemSourceConnector(root=source_root)
    )
    assert outcome.state == "succeeded"
    assert outcome.failure is None
