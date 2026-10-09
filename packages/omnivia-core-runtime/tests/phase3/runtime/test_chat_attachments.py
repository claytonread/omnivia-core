"""C7A: authoritative attachment staging and durable draft recovery.

Migration 0070's `omnivia_chat_staged_attachments`, the `StageAttachment` and
`RemoveStagedAttachment` commands behind the public `chat.command` seam, `SaveDraft`'s
atomic attachment-reference resolution, and the tagged read-back through
`chat.snapshot` and `chat.draft`. Reuses the C5A/GB harnesses for the dispatcher and the
`seeded` conversation, and seeds evidence the way production holds it: a real published
blob, a blob row, an evidence artifact and its provenance event, written under the
fence.

Not here, on purpose: attachment-bearing `SubmitMessage`/`EnqueueMessage`
(`test_chat_attachment_submission.py`). Context references are still refused; one test
here proves nothing partial is written.
"""

# ruff: noqa: F811

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_chat_offline_queue as gb05
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service.chat_snapshot import resolve_draft_readback
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage import chat, chat_attachments
from omnivia_core_runtime.storage.connection import StorageError, authorised
from omnivia_core_runtime.storage.migrations import applied_migrations, load_migrations
from omnivia_core_runtime.workspace.blob_publication import blob_path, publish_blob
from omnivia_core_runtime.workspace.layout import WorkspaceLayout
from test_chat_drafts import _draft, _save, _save_command
from test_chat_offline_queue import owned, seeded  # noqa: F401  (pytest fixtures)
from test_chat_submit_message_resolver import (
    ACTOR_ID,
    BASE_US,
    CHAT_TABLES,
    CONVERSATION_ID,
    PRINCIPAL,
    ROOT_MESSAGE_ID,
    WORKSPACE_ID,
    _counts,
    _dispatcher,
    _request,
    _submit_command,
)

from omnivia_core.chat_contract.v1 import PROTOCOL_VERSION
from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
)
from omnivia_core.contracts.v1.generated import ChatDraftInput

STAGED = "omnivia_chat_staged_attachments"
EVIDENCE = "omnivia_evidence_artifacts"
BLOBS = "omnivia_blob_objects"
PROVENANCE = "omnivia_evidence_provenance_events"
INTEGRITY = "omnivia_blob_integrity_events"
STAGE = "StageAttachment"
REMOVE = "RemoveStagedAttachment"
CONTENT = b"an already-uploaded file"
DRAFT_ID = "cmd-c5a-save.draft"


# --- a service with a blob root, and evidence seeded the way production holds it ------


def _service(holder: m1.Owned) -> Any:
    """The holder, plus the workspace layout `build_chat_application_dispatcher` reads."""
    layout = WorkspaceLayout(root=holder.path.parent)
    layout.blobs_path.mkdir(exist_ok=True)
    return SimpleNamespace(
        connection=holder.connection,
        identity=holder.identity,
        generation=holder.generation,
        path=holder.path,
        layout=layout,
    )


@pytest.fixture
def svc(seeded: m1.Owned) -> Any:
    return _service(seeded)


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _seed_file(
    service: Any,
    evidence_id: str,
    content: bytes = CONTENT,
    *,
    media_type: str = "text/plain",
    checksum: str | None = None,
    declared_length: int | None = None,
    publish: bool = True,
) -> str:
    """One evidence artifact over one blob; returns the artifact's content checksum."""
    digest = _digest(content)
    checksum = checksum or digest
    if publish:
        publish_blob(service.layout.blobs_path, digest, content)
    with fenced_transaction(
        service.connection,
        service.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=service.generation,
    ):
        have = service.connection.execute(
            f"SELECT 1 FROM {BLOBS} WHERE workspace_id = ? AND content_digest = ?",
            (WORKSPACE_ID, digest),
        ).fetchone()
        if have is None:
            service.connection.execute(
                f"INSERT INTO {BLOBS} (workspace_id, content_digest, content_length_bytes, "
                "created_at_us, verified_at_us) VALUES (?, ?, ?, ?, ?)",
                (
                    WORKSPACE_ID,
                    digest,
                    len(content) if declared_length is None else declared_length,
                    BASE_US,
                    BASE_US,
                ),
            )
        service.connection.execute(
            f"INSERT INTO {EVIDENCE} (evidence_id, workspace_id, source_kind, "
            "source_native_id, source_locator, source_retrieved_at_us, event_at_us, "
            "observed_at_us, ingested_at_us, recorded_at_us, content_checksum, "
            "blob_content_digest, media_type, original_metadata_json, "
            "original_metadata_digest, sensitivity, parser_status, ingestion_status, "
            "staged_source_ref, import_run_id) "
            "VALUES (?, ?, 'filesystem.archive', ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, "
            "'internal', 'parsed', 'ingested', NULL, NULL)",
            (
                evidence_id,
                WORKSPACE_ID,
                evidence_id,
                BASE_US,
                BASE_US - 10,
                BASE_US - 5,
                BASE_US,
                BASE_US + 1,
                checksum,
                digest,
                media_type,
                digest,
            ),
        )
        _provenance(service.connection, evidence_id, 1, None)
    return checksum


def _provenance(
    connection: sqlite3.Connection, evidence_id: str, sequence: int, tombstoned: int | None
) -> None:
    connection.execute(
        f"INSERT INTO {PROVENANCE} (provenance_event_id, evidence_id, workspace_id, "
        "provenance_sequence, actor_id, actor_kind, action, occurred_at_us, reason_code, "
        "reason_comment, parser_status, ingestion_status, tombstoned_observation, "
        "source_kind, source_native_id, audit_ref) "
        "VALUES (?, ?, ?, ?, 'importer', 'service', ?, ?, NULL, NULL, NULL, NULL, ?, "
        "'filesystem.archive', ?, NULL)",
        (
            f"prv-{evidence_id}-{sequence}",
            evidence_id,
            WORKSPACE_ID,
            sequence,
            "ingested" if tombstoned is None else "tombstoned",
            BASE_US + sequence,
            tombstoned,
            evidence_id,
        ),
    )


def _fenced(service: Any) -> Any:
    return fenced_transaction(
        service.connection,
        service.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=service.generation,
    )


def _tombstone(service: Any, evidence_id: str, *, sequence: int = 2, value: int = 1) -> None:
    with _fenced(service):
        _provenance(service.connection, evidence_id, sequence, value)


# --- commands ---------------------------------------------------------------------------


def _attachment_command(
    reference_id: str,
    *,
    command_id: str,
    actor_id: str = PRINCIPAL,
    draft_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "commandId": command_id,
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "actorId": actor_id,
        "attachmentReferenceId": reference_id,
    }
    if draft_id is not None:
        command["draftId"] = draft_id
    command.update(extra)
    return command


def _run(service: Any, name: str, command: Mapping[str, Any]) -> Any:
    command_id = str(command["commandId"])
    return _dispatcher(service).dispatch(
        _request(
            command,
            command_name=name,
            idempotency_key=f"idem-{command_id}",
            request_id=f"req-{command_id}",
        )
    )


def _stage(
    service: Any, reference_id: str, *, command_id: str = "cmd-stage-1", **fields: Any
) -> Any:
    return _run(
        service, STAGE, _attachment_command(reference_id, command_id=command_id, **fields)
    )


def _remove(
    service: Any, reference_id: str, *, command_id: str = "cmd-remove-1", **fields: Any
) -> Any:
    return _run(
        service, REMOVE, _attachment_command(reference_id, command_id=command_id, **fields)
    )


def _row(service: Any, reference_id: str) -> chat_attachments.StagedAttachment | None:
    return chat_attachments.read_staged_attachment(
        service.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        actor_id=PRINCIPAL,
        attachment_reference_id=reference_id,
    )


def _refused(response: Any, code: str) -> None:
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == code, response.error


def _written(before: Mapping[str, int], after: Mapping[str, int]) -> dict[str, int]:
    return {
        table: after[table] - before[table]
        for table in CHAT_TABLES
        if after[table] != before[table]
    }


def _ref(reference_id: str, revision: str | None = None) -> dict[str, str]:
    reference = {"referenceId": reference_id}
    if revision is not None:
        reference["sourceRevision"] = revision
    return reference


def _save_with(service: Any, references: list[Any], *, command_id: str = "cmd-c5a-save", **fields: Any) -> Any:
    return _save(
        service,
        _save_command(command_id=command_id, attachment_references=references, **fields),
        idempotency_key=f"idem-{command_id}",
        request_id=f"req-{command_id}",
    )


# --- 1. migration 0070 ----------------------------------------------------------------


def test_0070_is_the_next_forward_only_migration_and_its_objects_exist(svc: Any) -> None:
    catalogue = {migration.version: migration for migration in load_migrations()}
    assert catalogue[70].name == "0070_chat_staged_attachments.sql"
    assert catalogue[69].name == "0069_chat_generation_directions.sql"
    assert applied_migrations(svc.connection)[70] == catalogue[70].checksum
    names = {
        row[0]
        for row in svc.connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%staged_attachments%'"
        )
    }
    assert names == {
        STAGED,
        "omnivia_guard_chat_staged_attachments_insert",
        "omnivia_guard_chat_staged_attachments_update",
        "omnivia_guard_chat_staged_attachments_delete",
    }
    columns = {row[1] for row in svc.connection.execute(f"PRAGMA table_info({STAGED})")}
    # Identity, scope, revision and lifecycle only: no path, URL, bytes or handle.
    assert columns == {
        "workspace_id",
        "conversation_id",
        "actor_id",
        "attachment_reference_id",
        "draft_id",
        "source_revision",
        "blob_content_digest",
        "state",
        "version",
        "staged_at_us",
        "updated_at_us",
        "removed_at_us",
    }


def _raw_stage(
    service: Any, reference_id: str, *, revision: str, digest: str, **fields: Any
) -> None:
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "actor_id": PRINCIPAL,
        "attachment_reference_id": reference_id,
        "draft_id": None,
        "source_revision": revision,
        "blob_content_digest": digest,
        "state": "active",
        "version": 1,
        "staged_at_us": BASE_US + 50,
        "updated_at_us": BASE_US + 50,
        "removed_at_us": None,
    }
    values.update(fields)
    service.connection.execute(
        f"INSERT INTO {STAGED} ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )


def test_0070_refuses_an_unguarded_or_inconsistent_insert(svc: Any) -> None:
    revision = _seed_file(svc, "file-a")
    digest = _digest(CONTENT)
    with pytest.raises(sqlite3.DatabaseError):  # outside the fence
        _raw_stage(svc, "file-a", revision=revision, digest=digest)
    for fields in (
        {"revision": "sha256:" + "0" * 64},  # not the artifact's revision
        {"revision": revision, "digest": "sha256:" + "1" * 64},  # not its blob
        {"revision": revision, "state": "removed", "removed_at_us": BASE_US + 50},
        {"revision": revision, "version": 2},
        {"revision": revision, "conversation_id": "conv-missing"},
        {"revision": revision, "draft_id": "draft-missing"},
    ):
        with pytest.raises(sqlite3.DatabaseError), _fenced(svc):
            _raw_stage(
                svc,
                "file-a",
                **{"revision": revision, "digest": digest} | fields,  # type: ignore[arg-type]
            )
    assert m1.count(svc.connection, STAGED) == 0

    _tombstone(svc, "file-a")
    with pytest.raises(sqlite3.IntegrityError, match="tombstoned"), _fenced(svc):
        _raw_stage(svc, "file-a", revision=revision, digest=digest)

    # A draft binding must be the same conversation and actor's.
    _seed_file(svc, "file-b", b"second")
    with chat.chat_writer(
        svc.connection, svc.identity, workspace_id=WORKSPACE_ID, fencing_generation=svc.generation
    ) as writer:
        writer.insert_draft(
            draft_id="draft-foreign",
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
            mode="normal",
            text_content="theirs",
            references=(),
            schema_version=1,
            updated_at_us=BASE_US + 9,
        )
    with pytest.raises(sqlite3.IntegrityError, match="same conversation and actor"), _fenced(svc):
        _raw_stage(
            svc, "file-b", revision=_digest(b"second"), digest=_digest(b"second"),
            draft_id="draft-foreign",
        )


def test_0070_holds_the_lifecycle_the_service_relies_on(svc: Any) -> None:
    _seed_file(svc, "file-a")
    assert isinstance(_stage(svc, "file-a"), SuccessResponseEnvelope)

    def attempt(sql: str, params: tuple[Any, ...] = ()) -> None:
        with _fenced(svc):
            svc.connection.execute(sql, params)

    update = f"UPDATE {STAGED} SET "
    for statement in (
        update + "source_revision = 'sha256:x', version = 2",
        update + "staged_at_us = 1, version = 2",
        update + "version = 3, state = 'removed', removed_at_us = updated_at_us",
        update + "version = 2",  # no state change
        update + "version = 2, state = 'removed', removed_at_us = NULL",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            attempt(statement)
    with pytest.raises(sqlite3.DatabaseError):
        attempt(f"DELETE FROM {STAGED}")
    with pytest.raises(sqlite3.DatabaseError):  # unguarded
        svc.connection.execute(update + "version = 2, state = 'removed'")
    row = _row(svc, "file-a")
    assert row is not None and (row.state, row.version) == ("active", 1)

    # Sixty-five active rows are one too many, whoever writes them.
    for index in range(1, 64):
        content = f"file-{index}".encode()
        _seed_file(svc, f"bulk-{index}", content)
        with _fenced(svc):
            _raw_stage(svc, f"bulk-{index}", revision=_digest(content), digest=_digest(content))
    assert chat_attachments.count_active_staged_attachments(
        svc.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID,
        actor_id=PRINCIPAL,
    ) == 64
    _seed_file(svc, "one-too-many", b"one-too-many")
    with pytest.raises(sqlite3.IntegrityError, match="at most 64"), _fenced(svc):
        _raw_stage(
            svc, "one-too-many", revision=_digest(b"one-too-many"), digest=_digest(b"one-too-many")
        )


# --- 2. StageAttachment: success, replay, result ----------------------------------------


def test_stage_writes_one_authority_row_and_replays_exactly(svc: Any) -> None:
    revision = _seed_file(svc, "file-a")
    before = _counts(svc)
    response = _stage(svc, "file-a")
    assert isinstance(response, SuccessResponseEnvelope), response
    assert response.result["command_result"]["resultRef"] == f"attachment:file-a@{revision}"
    assert response.result["command_result"]["status"] == "completed"
    assert _written(before, _counts(svc)) == {STAGED: 1}

    row = _row(svc, "file-a")
    assert row == chat_attachments.StagedAttachment(
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        actor_id=PRINCIPAL,
        attachment_reference_id="file-a",
        draft_id=None,
        source_revision=revision,
        blob_content_digest=_digest(CONTENT),
        state="active",
        version=1,
        staged_at_us=row.staged_at_us if row else 0,
        updated_at_us=row.staged_at_us if row else 0,
        removed_at_us=None,
    )

    replay_before = _counts(svc)
    replay = _stage(svc, "file-a")
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == response.result
    assert _written(replay_before, _counts(svc)) == {}
    assert _row(svc, "file-a") == row


def test_a_second_stage_of_an_active_reference_is_a_conflict(svc: Any) -> None:
    _seed_file(svc, "file-a")
    _stage(svc, "file-a")
    before = (_counts(svc), _row(svc, "file-a"))
    _refused(_stage(svc, "file-a", command_id="cmd-stage-2"), "conflict")
    assert (_counts(svc), _row(svc, "file-a")) == before


def test_stage_with_a_draft_binds_it_only_to_that_actors_own_draft(svc: Any) -> None:
    _seed_file(svc, "file-a")
    _seed_file(svc, "file-b", b"second")
    _save(svc, _save_command())
    with chat.chat_writer(
        svc.connection, svc.identity, workspace_id=WORKSPACE_ID, fencing_generation=svc.generation
    ) as writer:
        writer.insert_draft(
            draft_id="draft-foreign",
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
            mode="normal",
            text_content="theirs",
            references=(),
            schema_version=1,
            updated_at_us=BASE_US + 9,
        )
    before = _counts(svc)
    _refused(_stage(svc, "file-a", draft_id="draft-foreign"), "conflict")
    _refused(_stage(svc, "file-a", command_id="cmd-stage-2", draft_id="draft-missing"), "conflict")
    assert _counts(svc) == before and _row(svc, "file-a") is None

    assert isinstance(_stage(svc, "file-b", command_id="cmd-stage-3", draft_id=DRAFT_ID), SuccessResponseEnvelope)
    bound = _row(svc, "file-b")
    assert bound is not None and bound.draft_id == DRAFT_ID


# --- 3. StageAttachment refusals write nothing ------------------------------------------


@pytest.mark.parametrize(
    ("label", "seed", "tamper", "code"),
    [
        ("unknown", None, None, "conflict"),
        ("media", {"media_type": "application/zip"}, None, "conflict"),
        ("too-large", {"declared_length": 25 * 1024 * 1024 + 1}, None, "conflict"),
        ("unstatable-revision", {"checksum": "x:" + "a" * 130}, None, "conflict"),
        ("physical-blob-missing", {}, "unlink", "conflict"),
        ("physical-blob-truncated", {}, "truncate", "conflict"),
        ("physical-blob-substituted", {}, "substitute", "conflict"),
        ("integrity-failed", {}, "integrity", "conflict"),
        ("tombstoned", {}, "tombstone", "conflict"),
    ],
)
def test_stage_refuses_an_unusable_file_and_writes_nothing(
    svc: Any, label: str, seed: dict[str, Any] | None, tamper: str | None, code: str
) -> None:
    if seed is not None:
        _seed_file(svc, "file-a", **seed)
    if tamper == "unlink":
        blob_path(svc.layout.blobs_path, _digest(CONTENT)).unlink()
    elif tamper == "truncate":
        blob_path(svc.layout.blobs_path, _digest(CONTENT)).write_bytes(b"short")
    elif tamper == "substitute":  # same length, different bytes: only a re-hash sees it
        blob_path(svc.layout.blobs_path, _digest(CONTENT)).write_bytes(b"X" * len(CONTENT))
    elif tamper == "integrity":
        with _fenced(svc):
            svc.connection.execute(
                f"INSERT INTO {INTEGRITY} (integrity_event_id, workspace_id, content_digest, "
                "integrity_sequence, outcome, observed_digest, observed_length_bytes, "
                "expected_length_bytes, inventory_id, checked_at_us) "
                "VALUES ('int-1', ?, ?, 1, 'missing', NULL, NULL, ?, NULL, ?)",
                (WORKSPACE_ID, _digest(CONTENT), len(CONTENT), BASE_US + 5),
            )
    elif tamper == "tombstone":
        _tombstone(svc, "file-a")
    before = _counts(svc)
    _refused(_stage(svc, "file-a"), code)
    assert _counts(svc) == before and _row(svc, "file-a") is None, label


def test_stage_refuses_a_wrong_scope_or_malformed_request_before_writing(svc: Any) -> None:
    _seed_file(svc, "file-a")
    before = _counts(svc)
    base = _attachment_command("file-a", command_id="cmd-stage-x")
    malformed = (
        base | {"actorId": ACTOR_ID},  # not the authenticated principal
        base | {"workspaceId": "ws-other"},
        base | {"path": "/etc/passwd"},  # no field for a path, URL or bytes
        base | {"bytes": "aGk="},
        base | {"attachmentReferenceId": "not a valid id!"},
        {key: value for key, value in base.items() if key != "attachmentReferenceId"},
    )
    for index, command in enumerate(malformed):
        response = _run(svc, STAGE, command | {"commandId": f"cmd-stage-x{index}"})
        _refused(response, "invalid_request")
    assert _counts(svc) == before

    # An absent conversation is a conflict inside the transaction, not a decode refusal.
    absent = _attachment_command("file-a", command_id="cmd-stage-y") | {"conversationId": "conv-nope"}
    _refused(_run(svc, STAGE, absent), "conflict")
    assert _counts(svc) == before


def test_the_sixty_fifth_active_staging_is_refused(svc: Any) -> None:
    for index in range(65):
        _seed_file(svc, f"bulk-{index}", f"bulk-{index}".encode())
    for index in range(64):
        response = _stage(svc, f"bulk-{index}", command_id=f"cmd-stage-{index}")
        assert isinstance(response, SuccessResponseEnvelope), (index, response)
    before = _counts(svc)
    _refused(_stage(svc, "bulk-64", command_id="cmd-stage-64"), "conflict")
    assert _counts(svc) == before and _row(svc, "bulk-64") is None


def test_stage_is_dependency_unavailable_when_the_service_has_no_blob_root(
    seeded: m1.Owned,
) -> None:
    before = _counts(seeded)
    _refused(_stage(seeded, "file-a"), "dependency_unavailable")
    assert _counts(seeded) == before


# --- 4. RemoveStagedAttachment ----------------------------------------------------------


def test_remove_moves_the_row_to_removed_and_replays_exactly(svc: Any) -> None:
    _seed_file(svc, "file-a")
    _stage(svc, "file-a")
    staged = _row(svc, "file-a")
    assert staged is not None

    before = _counts(svc)
    response = _remove(svc, "file-a")
    assert isinstance(response, SuccessResponseEnvelope), response
    assert _written(before, _counts(svc)) == {}  # one row changed, none added
    row = _row(svc, "file-a")
    assert row is not None
    assert (row.state, row.version) == ("removed", 2)
    assert row.removed_at_us == row.updated_at_us
    assert (row.source_revision, row.staged_at_us) == (staged.source_revision, staged.staged_at_us)

    replay_before = _counts(svc)
    assert _remove(svc, "file-a").result == response.result
    assert _written(replay_before, _counts(svc)) == {}
    assert _row(svc, "file-a") == row


def test_remove_refuses_what_is_not_an_active_staging_of_this_actor(svc: Any) -> None:
    _seed_file(svc, "file-a")
    _seed_file(svc, "file-b", b"second")
    _save(svc, _save_command())
    _stage(svc, "file-a")
    _stage(svc, "file-b", command_id="cmd-stage-b", draft_id=DRAFT_ID)
    before = (_counts(svc), _row(svc, "file-a"), _row(svc, "file-b"))

    _refused(_remove(svc, "file-missing"), "conflict")
    _refused(_remove(svc, "file-a", command_id="cmd-remove-2", draft_id=DRAFT_ID), "conflict")
    _refused(_remove(svc, "file-b", command_id="cmd-remove-3", draft_id="draft-other"), "conflict")
    _refused(_remove(svc, "file-b", command_id="cmd-remove-4", actor_id=ACTOR_ID), "invalid_request")
    assert (_counts(svc), _row(svc, "file-a"), _row(svc, "file-b")) == before

    assert isinstance(_remove(svc, "file-b", command_id="cmd-remove-5", draft_id=DRAFT_ID), SuccessResponseEnvelope)
    _refused(_remove(svc, "file-b", command_id="cmd-remove-6"), "conflict")  # already removed


def test_a_removed_reference_stages_again_as_the_same_row(svc: Any) -> None:
    _seed_file(svc, "file-a")
    _stage(svc, "file-a")
    _remove(svc, "file-a")
    response = _stage(svc, "file-a", command_id="cmd-stage-2")
    assert isinstance(response, SuccessResponseEnvelope), response
    row = _row(svc, "file-a")
    assert row is not None
    assert (row.state, row.version, row.removed_at_us) == ("active", 3, None)
    assert m1.count(svc.connection, STAGED) == 1

    # A file tombstoned while removed cannot come back.
    _remove(svc, "file-a", command_id="cmd-remove-2")
    _tombstone(svc, "file-a")
    _refused(_stage(svc, "file-a", command_id="cmd-stage-3"), "conflict")
    row = _row(svc, "file-a")
    assert row is not None and row.state == "removed"


def test_remove_drops_the_reference_from_the_actors_drafts_in_the_same_transaction(
    svc: Any,
) -> None:
    revision = _seed_file(svc, "file-a")
    _seed_file(svc, "file-b", b"second")
    _stage(svc, "file-a")
    _stage(svc, "file-b", command_id="cmd-stage-b")
    _save_with(svc, [_ref("file-a", revision), _ref("file-b")])
    saved = _draft(svc, DRAFT_ID)
    assert saved is not None and len(saved.references) == 2

    assert isinstance(_remove(svc, "file-a"), SuccessResponseEnvelope)
    after = _draft(svc, DRAFT_ID)
    assert after is not None
    assert after.version == saved.version + 1
    assert chat_attachments.draft_attachment_references(after.references) == (_ref("file-b"),)

    # And the removed reference can no longer be saved.
    _refused(_save_with(svc, [_ref("file-a")], command_id="cmd-c5a-save-2"), "conflict")


# --- 5. SaveDraft resolves references atomically ----------------------------------------


def test_save_stores_validated_attachment_references_tagged_and_reads_them_back(
    svc: Any,
) -> None:
    revision = _seed_file(svc, "file-a")
    _seed_file(svc, "file-b", b"second")
    _stage(svc, "file-a")
    _stage(svc, "file-b", command_id="cmd-stage-b")

    before = _counts(svc)
    # One pinned, one unpinned: the draft keeps each exactly as it was validated.
    response = _save_with(svc, [_ref("file-a", revision), _ref("file-b")])
    assert isinstance(response, SuccessResponseEnvelope), response
    assert _written(before, _counts(svc)) == {"omnivia_chat_drafts": 1}

    draft = _draft(svc, DRAFT_ID)
    assert draft is not None
    assert draft.references == (
        {"kind": "attachment", "referenceId": "file-a", "sourceRevision": revision},
        {"kind": "attachment", "referenceId": "file-b"},
    )

    gb05._give_principal_a_view_state(svc)
    snapshot = gb05._snapshot(svc, request_id="req-c7a-snapshot")
    (projected,) = snapshot["composerDrafts"]
    assert projected["attachmentReferences"] == [
        {"referenceId": "file-a", "sourceRevision": revision},
        {"referenceId": "file-b"},
    ]
    assert projected["contextReferences"] == []
    assert gb05._snapshot_schema_errors(snapshot) == []

    readback = _readback(svc, DRAFT_ID)
    assert readback["found"] is True
    assert readback["draft"] == projected

    # A later save keeps the staged set, and an exact replay writes nothing.
    replay_before = _counts(svc)
    assert _save_with(svc, [_ref("file-a", revision), _ref("file-b")]).result == response.result
    assert _written(replay_before, _counts(svc)) == {}


def _readback(service: Any, draft_id: str) -> Mapping[str, Any]:
    request_id = f"req-readback-{draft_id}"
    query = {
        "requestId": request_id,
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "actorId": PRINCIPAL,
        "draftId": draft_id,
    }
    context = OperationContext(
        request=s0.envelope_for(
            get_operation_metadata("chat.draft"),
            operation_input={"draft_id": draft_id, "draft_query": query},
            request_id=request_id,
            workspace_id=WORKSPACE_ID,
        ),
        principal=PRINCIPAL,
        workspace_id=WORKSPACE_ID,
        granted_operations=frozenset({"chat.draft"}),
    )
    return resolve_draft_readback(
        service.connection, ChatDraftInput(draft_id=draft_id, draft_query=query), context
    )


@pytest.mark.parametrize(
    "case",
    [
        "unstaged",
        "removed",
        "wrong-revision",
        "tombstoned",
        "blob-missing",
        "bound-to-another-draft",
        "other-conversation",
        "other-actor",
    ],
)
def test_save_refuses_a_reference_that_is_not_active_staged_and_available(
    svc: Any, case: str
) -> None:
    revision = _seed_file(svc, "file-a")
    _seed_file(svc, "file-b", b"second")
    _stage(svc, "file-b", command_id="cmd-stage-b")
    _save(svc, _save_command())  # the draft the failing save must leave alone
    stored = _draft(svc, DRAFT_ID)
    reference = _ref("file-a", revision)
    if case == "removed":
        _stage(svc, "file-a")
        _remove(svc, "file-a")
    elif case == "wrong-revision":
        _stage(svc, "file-a")
        reference = _ref("file-a", "sha256:" + "f" * 64)
    elif case == "tombstoned":
        _stage(svc, "file-a")
        _tombstone(svc, "file-a")
    elif case == "blob-missing":
        _stage(svc, "file-a")
        blob_path(svc.layout.blobs_path, _digest(CONTENT)).unlink()
    elif case == "bound-to-another-draft":
        with chat.chat_writer(
            svc.connection, svc.identity, workspace_id=WORKSPACE_ID,
            fencing_generation=svc.generation,
        ) as writer:
            writer.insert_draft(
                draft_id="draft-edit", conversation_id=CONVERSATION_ID, actor_id=PRINCIPAL,
                mode="edit_message", text_content="", references=(), schema_version=1,
                updated_at_us=BASE_US + 9, source_message_id=ROOT_MESSAGE_ID,
            )
        _stage(svc, "file-a", draft_id="draft-edit")
    elif case == "other-conversation":
        # Staged by this actor, but in a conversation the saved draft is not in.
        with _fenced(svc):
            svc.connection.execute(
                "INSERT INTO omnivia_chat_conversations (workspace_id, conversation_id, "
                "state, graph_revision, latest_conversation_sequence, schema_version, "
                "created_by_actor_id, created_at_us, updated_at_us) "
                "VALUES (?, 'conv-other', 'active', 1, 0, 1, ?, ?, ?)",
                (WORKSPACE_ID, PRINCIPAL, BASE_US, BASE_US),
            )
            _raw_stage(
                svc, "file-a", revision=revision, digest=_digest(CONTENT),
                conversation_id="conv-other",
            )
    elif case == "other-actor":
        with _fenced(svc):
            _raw_stage(
                svc, "file-a", revision=revision, digest=_digest(CONTENT), actor_id=ACTOR_ID
            )
    before = _counts(svc)
    _refused(_save_with(svc, [_ref("file-b"), reference], command_id="cmd-c5a-save-2"), "conflict")
    assert _counts(svc) == before
    assert _draft(svc, DRAFT_ID) == stored


def test_a_changed_file_is_a_new_reference_not_a_revision_of_the_old_one(svc: Any) -> None:
    revision = _seed_file(svc, "file-a")
    _stage(svc, "file-a")
    _save_with(svc, [_ref("file-a", revision)])
    stored = _draft(svc, DRAFT_ID)

    # The same artifact id whose recorded checksum no longer matches the pinned revision
    # (0008 forbids this by trigger, so it is forced here): the draft cannot name it.
    with authorised(svc.connection, mutations=True, ddl=True):
        svc.connection.execute(
            "DROP TRIGGER omnivia_guard_evidence_artifacts_update"
        )
        svc.connection.execute(
            f"UPDATE {EVIDENCE} SET content_checksum = ? WHERE evidence_id = 'file-a'",
            ("sha256:" + "9" * 64,),
        )
    before = _counts(svc)
    _refused(
        _save_with(svc, [_ref("file-a", revision)], command_id="cmd-c5a-save-2"), "conflict"
    )
    assert _counts(svc) == before and _draft(svc, DRAFT_ID) == stored


def test_save_refuses_duplicates_unknown_fields_and_every_context_reference(svc: Any) -> None:
    revision = _seed_file(svc, "file-a")
    _stage(svc, "file-a")
    before = _counts(svc)
    _refused(
        _save_with(svc, [_ref("file-a"), _ref("file-a", revision)], command_id="cmd-dup"),
        "invalid_request",
    )
    _refused(
        _save_with(svc, [{"referenceId": "file-a", "path": "/tmp/x"}], command_id="cmd-path"),
        "invalid_request",
    )
    _refused(
        _save(
            svc,
            _save_command(
                command_id="cmd-ctx",
                attachment_references=[_ref("file-a")],
                context_references=[{"referenceId": "ctx-1"}],
            ),
            idempotency_key="idem-cmd-ctx",
            request_id="req-cmd-ctx",
        ),
        "dependency_unavailable",
    )
    assert _counts(svc) == before


# --- 6. restart ---------------------------------------------------------------------------


def test_staged_attachments_and_the_draft_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "c7a-restart.sqlite"
    s0.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    try:
        gb05._seed_restart_workspace(holder)
        gb05._give_principal_a_view_state(holder)
        service = _service(holder)
        revision = _seed_file(service, "file-a")
        _seed_file(service, "file-b", b"second")
        _stage(service, "file-a")
        _stage(service, "file-b", command_id="cmd-stage-b")
        _remove(service, "file-b")
        response = _save_with(service, [_ref("file-a", revision)])
        assert isinstance(response, SuccessResponseEnvelope), response
    finally:
        holder.connection.close()

    reopened = m1.take_ownership(path)
    try:
        service = _service(reopened)
        assert (_row(service, "file-a") or pytest.fail("lost")).state == "active"
        assert (_row(service, "file-b") or pytest.fail("lost")).state == "removed"
        draft = _readback(service, DRAFT_ID)
        assert draft["draft"]["attachmentReferences"] == [_ref("file-a", revision)]
        assert draft["draft"]["contextReferences"] == []
        snapshot = gb05._snapshot(service, request_id="req-c7a-restart")
        assert snapshot["composerDrafts"][0]["attachmentReferences"] == [_ref("file-a", revision)]

        # The same requests answer from the stored outcome, writing nothing.
        before = _counts(service)
        assert _save_with(service, [_ref("file-a", revision)]).result == response.result
        assert isinstance(_stage(service, "file-a"), SuccessResponseEnvelope)
        assert _written(before, _counts(service)) == {}
    finally:
        reopened.connection.close()


# --- 7. the tagged store cannot be misread --------------------------------------------------


def test_a_stored_entry_that_is_not_a_tagged_attachment_is_never_projected(svc: Any) -> None:
    _save(svc, _save_command())
    gb05._give_principal_a_view_state(svc)
    for entries in ([{"referenceId": "file-a"}], [{"kind": "context", "referenceId": "ctx-1"}]):
        draft = _draft(svc, DRAFT_ID)
        assert draft is not None
        with chat.chat_writer(
            svc.connection, svc.identity, workspace_id=WORKSPACE_ID,
            fencing_generation=svc.generation,
        ) as writer:
            writer.update_draft(
                draft_id=DRAFT_ID, expected_version=draft.version, text_content="x",
                references=entries, updated_at_us=BASE_US + 20 + draft.version,
            )
        with pytest.raises(StorageError):
            chat_attachments.draft_attachment_references(entries)
        with pytest.raises(OperationError) as refusal:
            gb05._snapshot(svc, request_id=f"req-c7a-tagged-{draft.version}")
        assert refusal.value.code == "internal_non_recoverable"


# --- 8. context references stay fail-closed -------------------------------------------------
#
# Attachment-bearing submission itself is `test_chat_attachment_submission.py`.


def test_context_references_and_an_unproved_blob_root_still_refuse_submission(
    svc: Any, seeded: m1.Owned
) -> None:
    revision = _seed_file(svc, "file-a")
    _stage(svc, "file-a")
    staged = _row(svc, "file-a")
    before = _counts(svc)
    context = {"referenceId": "ctx-1"}

    submit = _submit_command(
        command_id="cmd-c7a-submit",
        message_id="msg-c7a-submitted",
        actorId=PRINCIPAL,
        attachment_references=(_ref("file-a", revision),),
        context_references=(context,),
    )
    response = _dispatcher(svc).dispatch(
        _request(submit, idempotency_key="idem-c7a-submit", request_id="req-c7a-submit")
    )
    _refused(response, "dependency_unavailable")

    enqueue = gb05._enqueue_command(
        command_id="cmd-c7a-enqueue",
        attachmentReferences=[_ref("file-a", revision)],
        contextReferences=[context],
    )
    _refused(_run(svc, "EnqueueMessage", enqueue), "dependency_unavailable")

    # An attachment cannot be proved without a blob root, so a service that has none
    # refuses it rather than admitting an unchecked file.
    for name, command in (
        ("SubmitMessage", submit | {"contextReferences": []}),
        ("EnqueueMessage", enqueue | {"contextReferences": []}),
    ):
        response = _dispatcher(seeded).dispatch(
            _request(command, command_name=name, idempotency_key="idem-c7a-nobase",
                     request_id="req-c7a-nobase")
        )
        _refused(response, "dependency_unavailable")

    assert _counts(svc) == before  # no Message, part, branch event, job, queue row or outbox
    assert _row(svc, "file-a") == staged
