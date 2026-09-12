"""Acceptance for migration 0030's Chat Gate B successor state.

Migration 0029 remains the immutable Chat foundation. This file proves the
additive 0030 overlay that later Gate B command/executor work needs: retry-aware
job status, attempt outcomes, durable stream text chunks and CAS queue ordering.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import test_application_audit_idempotency_migration as m1
import test_chat_repository as repo
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.chat import StaleVersion
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

WORKSPACE_ID = m1.WORKSPACE_ID
OTHER_WORKSPACE_ID = "ws-chat-0030-nope"
BASE_US = 2_600_000_000_000_000

CONVERSATION_ID = repo.CONVERSATION_ID
BRANCH_ID = repo.BRANCH_ID
ACTOR_ID = repo.ACTOR_ID
ROOT_MESSAGE_ID = repo.ROOT_MESSAGE_ID
TRIGGER_MESSAGE_ID = "msg-0030-trigger"
JOB_ID = "generation-job-0030-1"
ATTEMPT_ID = "generation-attempt-0030-1"
RETRY_ATTEMPT_ID = "generation-attempt-0030-2"
QUEUE_ID = "queue-0030-1"

TABLES = {
    "omnivia_chat_generation_job_status_projection",
    "omnivia_chat_generation_attempt_outcomes",
    "omnivia_chat_generation_text_chunks",
    "omnivia_chat_queue_order_projection",
}

INDEXES = {
    "omnivia_idx_chat_job_status_projection_state",
    "omnivia_idx_chat_generation_text_chunks_attempt",
    "omnivia_idx_chat_queue_order_projection_conversation",
}

TRIGGERS = {
    "omnivia_guard_chat_generation_job_status_projection_insert",
    "omnivia_guard_chat_generation_job_status_projection_update",
    "omnivia_guard_chat_generation_job_status_projection_delete",
    "omnivia_guard_chat_generation_attempt_outcomes_insert",
    "omnivia_guard_chat_generation_attempt_outcomes_update",
    "omnivia_guard_chat_generation_attempt_outcomes_delete",
    "omnivia_guard_chat_generation_text_chunks_insert",
    "omnivia_guard_chat_generation_text_chunks_update",
    "omnivia_guard_chat_generation_text_chunks_delete",
    "omnivia_guard_chat_queue_order_projection_insert",
    "omnivia_guard_chat_queue_order_projection_update",
    "omnivia_guard_chat_queue_order_projection_delete",
}


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def writer(holder: m1.Owned):
    return chat.chat_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def seed_generation(holder: m1.Owned) -> None:
    repo.seed_conversation(holder)
    with writer(holder) as w:
        w.update_conversation(
            conversation_id=CONVERSATION_ID,
            expected_graph_revision=2,
            graph_revision=2,
            latest_conversation_sequence=2,
            state="active",
            updated_at_us=BASE_US + 1,
            title="Repository chat",
            title_source="user",
        )
        w.append_message(
            message_id=TRIGGER_MESSAGE_ID,
            conversation_id=CONVERSATION_ID,
            role="user",
            author_type="human",
            author_id=ACTOR_ID,
            parent_message_id=ROOT_MESSAGE_ID,
            conversation_sequence=2,
            schema_version=1,
            content_hash=repo.digest("c"),
            completion_status="complete",
            visibility="standard",
            created_at_us=BASE_US + 2,
            committed_at_us=BASE_US + 2,
        )
        w.append_generation_job(
            generation_job_id=JOB_ID,
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            trigger_message_id=TRIGGER_MESSAGE_ID,
            graph_revision_observed=2,
            idempotency_key="generation-0030-key-1",
            schema_version=1,
            created_at_us=BASE_US + 3,
            updated_at_us=BASE_US + 3,
        )
        w.append_generation_attempt(
            generation_attempt_id=ATTEMPT_ID,
            conversation_id=CONVERSATION_ID,
            generation_job_id=JOB_ID,
            attempt_number=1,
            state="running",
            schema_version=1,
            started_at_us=BASE_US + 4,
        )


def seed_queue(holder: m1.Owned) -> None:
    repo.seed_conversation(holder)
    with writer(holder) as w:
        w.append_queued_submission(
            queued_submission_id=QUEUE_ID,
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
            queue_sequence=1,
            branch_id=BRANCH_ID,
            editable_parts=[{"partType": "text"}],
            references=[],
            idempotency_key="queue-0030-key-1",
            created_at_us=BASE_US + 10,
            updated_at_us=BASE_US + 10,
        )


def test_0030_schema_inventory_contains_expected_objects(owned: m1.Owned) -> None:
    objects = {
        (row[0], row[1])
        for row in owned.connection.execute(
            "SELECT name, type FROM sqlite_master WHERE name LIKE 'omnivia_%'"
        )
    }

    assert {(table, "table") for table in TABLES}.issubset(objects)
    assert {(index, "index") for index in INDEXES}.issubset(objects)
    assert {(trigger, "trigger") for trigger in TRIGGERS}.issubset(objects)
    assert owned.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert owned.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_successor_job_status_supports_retryable_then_running(owned: m1.Owned) -> None:
    seed_generation(owned)
    with writer(owned) as w:
        w.insert_generation_job_status_projection(
            generation_job_id=JOB_ID,
            conversation_id=CONVERSATION_ID,
            state="running",
            current_attempt_id=ATTEMPT_ID,
            updated_at_us=BASE_US + 20,
        )
        w.update_generation_job_status_projection(
            generation_job_id=JOB_ID,
            expected_version=1,
            state="retryable",
            current_attempt_id=ATTEMPT_ID,
            sanitized_error_code="rate_limited",
            sanitized_error_detail="provider returned a retryable throttling response",
            updated_at_us=BASE_US + 21,
        )
        w.append_generation_attempt(
            generation_attempt_id=RETRY_ATTEMPT_ID,
            conversation_id=CONVERSATION_ID,
            generation_job_id=JOB_ID,
            attempt_number=2,
            retry_of_attempt_id=ATTEMPT_ID,
            state="running",
            schema_version=1,
            started_at_us=BASE_US + 22,
        )
        w.update_generation_job_status_projection(
            generation_job_id=JOB_ID,
            expected_version=2,
            state="running",
            current_attempt_id=RETRY_ATTEMPT_ID,
            updated_at_us=BASE_US + 23,
        )

    status = chat.read_generation_job_status_projection(
        owned.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert status is not None
    assert status.state == "running"
    assert status.current_attempt_id == RETRY_ATTEMPT_ID
    assert status.sanitized_error_code is None
    assert status.version == 3

    assert (
        chat.read_generation_job_status_projection(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, generation_job_id=JOB_ID
        )
        is None
    )


def test_successor_job_status_rejects_stale_projection_version(owned: m1.Owned) -> None:
    seed_generation(owned)
    with writer(owned) as w:
        w.insert_generation_job_status_projection(
            generation_job_id=JOB_ID,
            conversation_id=CONVERSATION_ID,
            state="running",
            current_attempt_id=ATTEMPT_ID,
            updated_at_us=BASE_US + 30,
        )

    with writer(owned) as w, pytest.raises(StaleVersion):
        w.update_generation_job_status_projection(
            generation_job_id=JOB_ID,
            expected_version=9,
            state="retryable",
            current_attempt_id=ATTEMPT_ID,
            sanitized_error_code="rate_limited",
            updated_at_us=BASE_US + 31,
        )

    status = chat.read_generation_job_status_projection(
        owned.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert status is not None
    assert status.state == "running"
    assert status.version == 1


def test_attempt_outcomes_are_single_append_only_terminal_facts(owned: m1.Owned) -> None:
    seed_generation(owned)
    with writer(owned) as w:
        w.append_generation_attempt_outcome(
            conversation_id=CONVERSATION_ID,
            generation_job_id=JOB_ID,
            generation_attempt_id=ATTEMPT_ID,
            terminal_state="failed",
            retryable=True,
            provider_event_id="provider-event-0030-outcome-1",
            sanitized_error_code="rate_limited",
            sanitized_error_detail="safe retry after backoff",
            occurred_at_us=BASE_US + 40,
        )

    outcome = chat.read_generation_attempt_outcome(
        owned.connection, workspace_id=WORKSPACE_ID, generation_attempt_id=ATTEMPT_ID
    )
    assert outcome is not None
    assert outcome.retryable is True
    assert outcome.terminal_state == "failed"
    assert outcome.sanitized_error_code == "rate_limited"

    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        w.append_generation_attempt_outcome(
            conversation_id=CONVERSATION_ID,
            generation_job_id=JOB_ID,
            generation_attempt_id=ATTEMPT_ID,
            terminal_state="failed",
            retryable=True,
            sanitized_error_code="rate_limited",
            occurred_at_us=BASE_US + 41,
        )

    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        w.connection.execute(
            "UPDATE omnivia_chat_generation_attempt_outcomes SET retryable = 0 "
            "WHERE workspace_id = ? AND generation_attempt_id = ?",
            (WORKSPACE_ID, ATTEMPT_ID),
        )

    outcomes = chat.read_generation_attempt_outcomes(
        owned.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert [row.generation_attempt_id for row in outcomes] == [ATTEMPT_ID]


def test_text_chunks_are_contiguous_append_only_and_dedupe_provider_events(
    owned: m1.Owned,
) -> None:
    seed_generation(owned)
    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        w.append_generation_text_chunk(
            conversation_id=CONVERSATION_ID,
            generation_job_id=JOB_ID,
            generation_attempt_id=ATTEMPT_ID,
            chunk_ordinal=1,
            provider_event_id="provider-event-0030-chunk-skip",
            text_content="skipped",
            content_hash=repo.digest("d"),
            occurred_at_us=BASE_US + 50,
        )

    with writer(owned) as w:
        w.append_generation_text_chunk(
            conversation_id=CONVERSATION_ID,
            generation_job_id=JOB_ID,
            generation_attempt_id=ATTEMPT_ID,
            chunk_ordinal=0,
            provider_event_id="provider-event-0030-chunk-1",
            text_content="hel",
            content_hash=repo.digest("e"),
            occurred_at_us=BASE_US + 51,
        )
        w.append_generation_text_chunk(
            conversation_id=CONVERSATION_ID,
            generation_job_id=JOB_ID,
            generation_attempt_id=ATTEMPT_ID,
            chunk_ordinal=1,
            provider_event_id="provider-event-0030-chunk-2",
            text_content="lo",
            content_hash=repo.digest("f"),
            occurred_at_us=BASE_US + 52,
        )

    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        w.append_generation_text_chunk(
            conversation_id=CONVERSATION_ID,
            generation_job_id=JOB_ID,
            generation_attempt_id=ATTEMPT_ID,
            chunk_ordinal=2,
            provider_event_id="provider-event-0030-chunk-2",
            text_content="duplicate provider event",
            content_hash=repo.digest("a"),
            occurred_at_us=BASE_US + 53,
        )

    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        w.connection.execute(
            "UPDATE omnivia_chat_generation_text_chunks SET text_content = 'changed' "
            "WHERE workspace_id = ? AND generation_job_id = ? AND chunk_ordinal = 0",
            (WORKSPACE_ID, JOB_ID),
        )

    chunks = chat.read_generation_text_chunks(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=JOB_ID,
        generation_attempt_id=ATTEMPT_ID,
    )
    assert [(chunk.chunk_ordinal, chunk.text_content) for chunk in chunks] == [(0, "hel"), (1, "lo")]
    assert chat.read_generation_text_chunks(
        owned.connection, workspace_id=OTHER_WORKSPACE_ID, generation_job_id=JOB_ID
    ) == ()


def test_queue_order_projection_is_versioned_and_identity_immutable(
    owned: m1.Owned,
) -> None:
    seed_queue(owned)
    with writer(owned) as w:
        w.insert_queue_order_projection(
            queued_submission_id=QUEUE_ID,
            conversation_id=CONVERSATION_ID,
            queue_position=1,
            updated_by_actor_id=ACTOR_ID,
            updated_at_us=BASE_US + 60,
        )
        w.update_queue_order_projection(
            queued_submission_id=QUEUE_ID,
            expected_version=1,
            queue_position=2,
            updated_by_actor_id=ACTOR_ID,
            updated_at_us=BASE_US + 61,
        )

    projection = chat.read_queue_order_projection(
        owned.connection, workspace_id=WORKSPACE_ID, queued_submission_id=QUEUE_ID
    )
    assert projection is not None
    assert projection.queue_position == 2
    assert projection.version == 2

    with writer(owned) as w, pytest.raises(StaleVersion):
        w.update_queue_order_projection(
            queued_submission_id=QUEUE_ID,
            expected_version=1,
            queue_position=3,
            updated_by_actor_id=ACTOR_ID,
            updated_at_us=BASE_US + 62,
        )

    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="identity"):
        w.connection.execute(
            "UPDATE omnivia_chat_queue_order_projection "
            "SET queued_submission_id = ?, version = ? "
            "WHERE workspace_id = ? AND queued_submission_id = ?",
            ("queue-0030-other", 3, WORKSPACE_ID, QUEUE_ID),
        )

    ordered = chat.read_queue_order_for_conversation(
        owned.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
    )
    assert [row.queued_submission_id for row in ordered] == [QUEUE_ID]
    assert (
        chat.read_queue_order_projection(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, queued_submission_id=QUEUE_ID
        )
        is None
    )


def test_0030_direct_unguarded_insert_still_fails(owned: m1.Owned) -> None:
    seed_generation(owned)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded INSERT"):
        owned.connection.execute(
            "INSERT INTO omnivia_chat_generation_text_chunks "
            "(workspace_id, conversation_id, generation_job_id, generation_attempt_id, "
            "chunk_ordinal, provider_event_id, text_content, content_hash, "
            "schema_version, occurred_at_us) "
            "VALUES (?, ?, ?, ?, 0, NULL, 'hello', ?, 1, ?)",
            (WORKSPACE_ID, CONVERSATION_ID, JOB_ID, ATTEMPT_ID, repo.digest("b"), BASE_US + 70),
        )
