"""Acceptance for `storage/chat.py`'s 0030 recovery and queue projections.

The 0030 migration test holds the guarded triggers to what SQL can enforce.
These hold the repository to what SQL cannot: that the successor writes go
through the same fenced authority as every 0029 write, that the composed reads
project the state they claim to, that a workspace with 0030's tables and none of
its rows still reads exactly the pre-successor answer, and that a queue reorder
is one compare-and-set with no partial reorder to observe.

The 0029 repository acceptance module supplies the fixture, the writer and the
conversation seed, so a drift in either fails there rather than being restated --
and restated differently -- here.
"""

from __future__ import annotations

import sqlite3

import pytest
import test_application_audit_idempotency_migration as m1
import test_chat_repository as repo29
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.chat import StaleVersion
from omnivia_core_runtime.storage.connection import StorageError

owned = repo29.owned
writer = repo29.writer
seed_conversation = repo29.seed_conversation
digest = repo29.digest

WORKSPACE_ID = repo29.WORKSPACE_ID
OTHER_WORKSPACE_ID = repo29.OTHER_WORKSPACE_ID
BASE_US = repo29.BASE_US
CONVERSATION_ID = repo29.CONVERSATION_ID
BRANCH_ID = repo29.BRANCH_ID
ACTOR_ID = repo29.ACTOR_ID
ROOT_MESSAGE_ID = repo29.ROOT_MESSAGE_ID
TRIGGER_MESSAGE_ID = repo29.TRIGGER_MESSAGE_ID
JOB_ID = repo29.JOB_ID
ATTEMPT_ID = repo29.ATTEMPT_ID
SECOND_ATTEMPT_ID = "generation-attempt-repo-2"
QUEUE_ID = repo29.QUEUE_ID
SECOND_QUEUE_ID = "queue-repo-2"
THIRD_QUEUE_ID = "queue-repo-3"


def seed_running_job(holder: m1.Owned) -> None:
    """A conversation with a trigger message, a Generation Job and Attempt 1 running."""
    seed_conversation(holder)
    with writer(holder) as w:
        w.update_conversation(
            conversation_id=CONVERSATION_ID,
            expected_graph_revision=2,
            graph_revision=2,
            latest_conversation_sequence=2,
            state="active",
            updated_at_us=BASE_US + 40,
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
            content_hash=digest("c"),
            completion_status="complete",
            visibility="standard",
            created_at_us=BASE_US + 41,
            committed_at_us=BASE_US + 41,
            generation_job_id=None,
        )
        w.append_generation_job(
            generation_job_id=JOB_ID,
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            trigger_message_id=TRIGGER_MESSAGE_ID,
            graph_revision_observed=2,
            idempotency_key="generation-repo-key-1",
            schema_version=1,
            created_at_us=BASE_US + 42,
            updated_at_us=BASE_US + 42,
        )
        w.append_generation_attempt(
            generation_attempt_id=ATTEMPT_ID,
            conversation_id=CONVERSATION_ID,
            generation_job_id=JOB_ID,
            attempt_number=1,
            state="running",
            schema_version=1,
            started_at_us=BASE_US + 43,
        )
        w.update_generation_job(
            generation_job_id=JOB_ID,
            expected_state="queued",
            expected_lease_epoch=0,
            state="running",
            current_attempt_id=ATTEMPT_ID,
            lease_owner="worker-repo-1",
            lease_epoch=1,
            lease_expires_at_us=BASE_US + 9000,
            heartbeat_at_us=BASE_US + 44,
            last_event_sequence=0,
            updated_at_us=BASE_US + 44,
            started_at_us=BASE_US + 44,
        )


def fail_attempt(holder: m1.Owned, attempt_id: str, *, ended_at_us: int) -> None:
    with writer(holder) as w:
        w.append_generation_attempt_outcome(
            generation_attempt_id=attempt_id,
            conversation_id=CONVERSATION_ID,
            generation_job_id=JOB_ID,
            outcome="failed",
            error_class="provider.timeout",
            error_detail="the provider did not answer before the deadline",
            schema_version=1,
            ended_at_us=ended_at_us,
            recorded_at_us=ended_at_us + 1,
        )


def seed_queued_submissions(holder: m1.Owned, *identifiers: str) -> None:
    with writer(holder) as w:
        for index, identifier in enumerate(identifiers, start=1):
            w.append_queued_submission(
                queued_submission_id=identifier,
                conversation_id=CONVERSATION_ID,
                actor_id=ACTOR_ID,
                queue_sequence=index,
                branch_id=BRANCH_ID,
                editable_parts=[{"type": "text", "text": identifier}],
                references=[],
                idempotency_key=f"queue-repo-key-{index}",
                created_at_us=BASE_US + 50 + index,
                updated_at_us=BASE_US + 50 + index,
            )


# --- 1. the successor writes go through the same fence ----------------------------


def test_successor_writes_pass_the_guarded_triggers_and_direct_writes_do_not(
    owned: m1.Owned,
) -> None:
    seed_running_job(owned)
    with writer(owned) as w:
        w.append_generation_chunks(
            generation_attempt_id=ATTEMPT_ID,
            conversation_id=CONVERSATION_ID,
            generation_job_id=JOB_ID,
            first_ordinal=1,
            chunks=[("hello ", "provider-chunk-1"), ("world", None)],
            schema_version=1,
            created_at_us=BASE_US + 45,
        )
    chunks = chat.read_generation_chunks(
        owned.connection, workspace_id=WORKSPACE_ID, generation_attempt_id=ATTEMPT_ID
    )
    assert [c.chunk_ordinal for c in chunks] == [1, 2]
    assert "".join(c.text_content for c in chunks) == "hello world"

    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded INSERT"):
        owned.connection.execute(
            "INSERT INTO omnivia_chat_generation_chunks "
            "(workspace_id, conversation_id, generation_job_id, generation_attempt_id, "
            "chunk_ordinal, provider_event_id, text_content, schema_version, created_at_us) "
            "VALUES (?, ?, ?, ?, 3, NULL, 'unguarded', 1, ?)",
            (WORKSPACE_ID, CONVERSATION_ID, JOB_ID, ATTEMPT_ID, BASE_US + 46),
        )


def test_chunk_batches_are_bounded_before_the_write(owned: m1.Owned) -> None:
    seed_running_job(owned)
    with writer(owned) as w, pytest.raises(StorageError, match="at least one chunk"):
        w.append_generation_chunks(
            generation_attempt_id=ATTEMPT_ID,
            conversation_id=CONVERSATION_ID,
            generation_job_id=JOB_ID,
            first_ordinal=1,
            chunks=[],
            schema_version=1,
            created_at_us=BASE_US + 45,
        )
    oversize = [("x", None)] * (chat.MAX_CHUNK_BATCH + 1)
    with writer(owned) as w, pytest.raises(StorageError, match="at most 256 chunks"):
        w.append_generation_chunks(
            generation_attempt_id=ATTEMPT_ID,
            conversation_id=CONVERSATION_ID,
            generation_job_id=JOB_ID,
            first_ordinal=1,
            chunks=oversize,
            schema_version=1,
            created_at_us=BASE_US + 45,
        )


def test_chunk_reads_are_bounded_workspace_scoped_and_resumable(owned: m1.Owned) -> None:
    seed_running_job(owned)
    with writer(owned) as w:
        w.append_generation_chunks(
            generation_attempt_id=ATTEMPT_ID,
            conversation_id=CONVERSATION_ID,
            generation_job_id=JOB_ID,
            first_ordinal=1,
            chunks=[(f"chunk-{index}", None) for index in range(1, 6)],
            schema_version=1,
            created_at_us=BASE_US + 45,
        )
    first = chat.read_generation_chunks(
        owned.connection, workspace_id=WORKSPACE_ID, generation_attempt_id=ATTEMPT_ID, limit=2
    )
    assert [c.chunk_ordinal for c in first] == [1, 2]
    rest = chat.read_generation_chunks(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        generation_attempt_id=ATTEMPT_ID,
        after_ordinal=first[-1].chunk_ordinal,
    )
    assert [c.chunk_ordinal for c in rest] == [3, 4, 5]
    assert (
        chat.read_generation_chunks(
            owned.connection,
            workspace_id=OTHER_WORKSPACE_ID,
            generation_attempt_id=ATTEMPT_ID,
        )
        == ()
    )


def test_chunk_reads_reject_out_of_bounds_arguments(owned: m1.Owned) -> None:
    seed_running_job(owned)
    with pytest.raises(StorageError, match="after_ordinal must be >= 0"):
        chat.read_generation_chunks(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            generation_attempt_id=ATTEMPT_ID,
            after_ordinal=-1,
        )
    with pytest.raises(StorageError, match="limit must be between 1 and 256"):
        chat.read_generation_chunks(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            generation_attempt_id=ATTEMPT_ID,
            limit=0,
        )
    with pytest.raises(StorageError, match="limit must be between 1 and 256"):
        chat.read_generation_chunks(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            generation_attempt_id=ATTEMPT_ID,
            limit=-1,
        )
    with pytest.raises(StorageError, match="limit must be between 1 and 256"):
        chat.read_generation_chunks(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            generation_attempt_id=ATTEMPT_ID,
            limit=chat.MAX_CHUNK_BATCH + 1,
        )


# --- 2. the composed Attempt and Job projection -----------------------------------


def test_a_running_job_with_no_outcome_projects_running(owned: m1.Owned) -> None:
    seed_running_job(owned)
    projection = chat.read_effective_generation_job(
        owned.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert projection is not None
    assert projection.state == "running"
    assert projection.is_retryable is False
    assert projection.is_terminal is False
    assert projection.latest_attempt is not None
    assert projection.latest_attempt.state == "running"
    assert projection.latest_attempt.ended_at_us is None


def test_a_failed_latest_attempt_projects_the_open_job_as_retryable(owned: m1.Owned) -> None:
    seed_running_job(owned)
    fail_attempt(owned, ATTEMPT_ID, ended_at_us=BASE_US + 60)

    projection = chat.read_effective_generation_job(
        owned.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert projection is not None
    assert projection.job.state == "running"
    assert projection.state == "retryable"
    assert projection.is_retryable is True
    assert projection.is_terminal is False
    latest = projection.latest_attempt
    assert latest is not None
    assert latest.state == "failed"
    assert latest.is_terminal is True
    assert latest.ended_at_us == BASE_US + 60
    assert latest.outcome is not None
    assert latest.outcome.error_class == "provider.timeout"


def test_appending_the_next_attempt_projects_the_job_as_running_again(
    owned: m1.Owned,
) -> None:
    seed_running_job(owned)
    fail_attempt(owned, ATTEMPT_ID, ended_at_us=BASE_US + 60)

    with writer(owned) as w:
        w.append_generation_attempt(
            generation_attempt_id=SECOND_ATTEMPT_ID,
            conversation_id=CONVERSATION_ID,
            generation_job_id=JOB_ID,
            attempt_number=2,
            retry_of_attempt_id=ATTEMPT_ID,
            state="running",
            schema_version=1,
            started_at_us=BASE_US + 61,
        )

    projection = chat.read_effective_generation_job(
        owned.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert projection is not None
    assert projection.state == "running"
    assert projection.is_retryable is False
    assert [a.attempt.generation_attempt_id for a in projection.attempts] == [
        ATTEMPT_ID,
        SECOND_ATTEMPT_ID,
    ]
    # The retry is a new Attempt identity under the same Job, not a reopened one.
    assert projection.attempts[0].state == "failed"
    assert projection.attempts[1].state == "running"
    assert projection.attempts[1].attempt.retry_of_attempt_id == ATTEMPT_ID


def test_a_terminal_job_stays_terminal_and_never_projects_retryable(
    owned: m1.Owned,
) -> None:
    seed_running_job(owned)
    fail_attempt(owned, ATTEMPT_ID, ended_at_us=BASE_US + 60)
    with writer(owned) as w:
        w.update_generation_job(
            generation_job_id=JOB_ID,
            expected_state="running",
            expected_lease_epoch=1,
            state="failed",
            current_attempt_id=ATTEMPT_ID,
            lease_owner="worker-repo-1",
            lease_epoch=1,
            sanitized_error_code="provider.timeout",
            last_event_sequence=0,
            updated_at_us=BASE_US + 70,
            started_at_us=BASE_US + 44,
            finished_at_us=BASE_US + 70,
        )

    projection = chat.read_effective_generation_job(
        owned.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert projection is not None
    assert projection.state == "failed"
    assert projection.is_terminal is True
    assert projection.is_retryable is False

    # And 0029 still refuses to reopen it, whatever the projection says.
    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="transition is invalid"):
        w.update_generation_job(
            generation_job_id=JOB_ID,
            expected_state="failed",
            expected_lease_epoch=1,
            state="running",
            current_attempt_id=ATTEMPT_ID,
            lease_owner="worker-repo-1",
            lease_epoch=1,
            lease_expires_at_us=BASE_US + 9000,
            heartbeat_at_us=BASE_US + 71,
            last_event_sequence=0,
            updated_at_us=BASE_US + 71,
            started_at_us=BASE_US + 44,
        )


def test_an_attempt_0029_wrote_terminal_reads_from_its_own_row(owned: m1.Owned) -> None:
    """The pre-successor fallback: no outcome row, and the projection still answers."""
    seed_conversation(owned)
    with writer(owned) as w:
        w.update_conversation(
            conversation_id=CONVERSATION_ID,
            expected_graph_revision=2,
            graph_revision=2,
            latest_conversation_sequence=2,
            state="active",
            updated_at_us=BASE_US + 40,
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
            content_hash=digest("c"),
            completion_status="complete",
            visibility="standard",
            created_at_us=BASE_US + 41,
            committed_at_us=BASE_US + 41,
        )
        w.append_generation_job(
            generation_job_id=JOB_ID,
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            trigger_message_id=TRIGGER_MESSAGE_ID,
            graph_revision_observed=2,
            idempotency_key="generation-repo-key-1",
            schema_version=1,
            created_at_us=BASE_US + 42,
            updated_at_us=BASE_US + 42,
        )
        w.append_generation_attempt(
            generation_attempt_id=ATTEMPT_ID,
            conversation_id=CONVERSATION_ID,
            generation_job_id=JOB_ID,
            attempt_number=1,
            state="failed",
            schema_version=1,
            started_at_us=BASE_US + 43,
            ended_at_us=BASE_US + 50,
        )

    assert (
        chat.read_generation_attempt_outcome(
            owned.connection, workspace_id=WORKSPACE_ID, generation_attempt_id=ATTEMPT_ID
        )
        is None
    )
    projection = chat.read_effective_generation_job(
        owned.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert projection is not None
    assert projection.state == "queued"
    latest = projection.latest_attempt
    assert latest is not None
    assert latest.state == "failed"
    assert latest.ended_at_us == BASE_US + 50


def test_the_job_projection_is_workspace_scoped(owned: m1.Owned) -> None:
    seed_running_job(owned)
    assert (
        chat.read_effective_generation_job(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, generation_job_id=JOB_ID
        )
        is None
    )
    assert (
        chat.read_effective_generation_job(
            owned.connection, workspace_id=WORKSPACE_ID, generation_job_id="generation-job-absent"
        )
        is None
    )


# --- 3. the queue-order projection ------------------------------------------------


def test_with_no_projection_row_the_queue_reads_in_creation_order(owned: m1.Owned) -> None:
    seed_conversation(owned)
    seed_queued_submissions(owned, QUEUE_ID, SECOND_QUEUE_ID, THIRD_QUEUE_ID)

    assert (
        chat.read_queue_order(
            owned.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
        )
        is None
    )
    order = chat.read_effective_queue_order(
        owned.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
    )
    assert [s.queued_submission_id for s in order] == [
        QUEUE_ID,
        SECOND_QUEUE_ID,
        THIRD_QUEUE_ID,
    ]
    assert [s.queue_sequence for s in order] == [1, 2, 3]


def test_a_reorder_is_atomic_compare_and_set_over_the_whole_order(owned: m1.Owned) -> None:
    seed_conversation(owned)
    seed_queued_submissions(owned, QUEUE_ID, SECOND_QUEUE_ID, THIRD_QUEUE_ID)

    with writer(owned) as w:
        w.insert_queue_order(
            conversation_id=CONVERSATION_ID,
            order=[QUEUE_ID, SECOND_QUEUE_ID, THIRD_QUEUE_ID],
            created_at_us=BASE_US + 100,
        )
    with writer(owned) as w:
        w.update_queue_order(
            conversation_id=CONVERSATION_ID,
            expected_version=1,
            order=[THIRD_QUEUE_ID, QUEUE_ID, SECOND_QUEUE_ID],
            updated_at_us=BASE_US + 101,
        )

    projection = chat.read_queue_order(
        owned.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
    )
    assert projection is not None
    assert projection.order == (THIRD_QUEUE_ID, QUEUE_ID, SECOND_QUEUE_ID)
    assert projection.version == 2

    effective = chat.read_effective_queue_order(
        owned.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
    )
    assert [s.queued_submission_id for s in effective] == [
        THIRD_QUEUE_ID,
        QUEUE_ID,
        SECOND_QUEUE_ID,
    ]
    # Creation identity did not move: 0029's queue_sequence is what it always was.
    assert {s.queued_submission_id: s.queue_sequence for s in effective} == {
        QUEUE_ID: 1,
        SECOND_QUEUE_ID: 2,
        THIRD_QUEUE_ID: 3,
    }


def test_a_stale_reorder_changes_nothing_at_all(owned: m1.Owned) -> None:
    seed_conversation(owned)
    seed_queued_submissions(owned, QUEUE_ID, SECOND_QUEUE_ID, THIRD_QUEUE_ID)
    with writer(owned) as w:
        w.insert_queue_order(
            conversation_id=CONVERSATION_ID,
            order=[QUEUE_ID, SECOND_QUEUE_ID, THIRD_QUEUE_ID],
            created_at_us=BASE_US + 100,
        )
    with writer(owned) as w:
        w.update_queue_order(
            conversation_id=CONVERSATION_ID,
            expected_version=1,
            order=[THIRD_QUEUE_ID, QUEUE_ID, SECOND_QUEUE_ID],
            updated_at_us=BASE_US + 101,
        )

    with writer(owned) as w, pytest.raises(StaleVersion):
        w.update_queue_order(
            conversation_id=CONVERSATION_ID,
            expected_version=1,
            order=[SECOND_QUEUE_ID, THIRD_QUEUE_ID, QUEUE_ID],
            updated_at_us=BASE_US + 102,
        )

    projection = chat.read_queue_order(
        owned.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
    )
    assert projection is not None
    assert projection.order == (THIRD_QUEUE_ID, QUEUE_ID, SECOND_QUEUE_ID)
    assert projection.version == 2
    assert projection.updated_at_us == BASE_US + 101


def test_a_reorder_cannot_move_a_claimed_submission(owned: m1.Owned) -> None:
    seed_conversation(owned)
    seed_queued_submissions(owned, QUEUE_ID, SECOND_QUEUE_ID)
    with writer(owned) as w:
        w.update_queued_submission(
            queued_submission_id=SECOND_QUEUE_ID,
            expected_version=1,
            state="claimed",
            claimed_by="worker-repo-1",
            claim_epoch=1,
            claim_expires_at_us=BASE_US + 9000,
            updated_at_us=BASE_US + 60,
        )

    with writer(owned) as w, pytest.raises(
        sqlite3.IntegrityError, match="only queued submissions"
    ):
        w.insert_queue_order(
            conversation_id=CONVERSATION_ID,
            order=[SECOND_QUEUE_ID, QUEUE_ID],
            created_at_us=BASE_US + 100,
        )

    # The claimed submission is simply not in the effective order any more.
    assert [
        s.queued_submission_id
        for s in chat.read_effective_queue_order(
            owned.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
        )
    ] == [QUEUE_ID]


def test_a_submission_claimed_after_a_reorder_drops_out_of_the_effective_order(
    owned: m1.Owned,
) -> None:
    seed_conversation(owned)
    seed_queued_submissions(owned, QUEUE_ID, SECOND_QUEUE_ID, THIRD_QUEUE_ID)
    with writer(owned) as w:
        w.insert_queue_order(
            conversation_id=CONVERSATION_ID,
            order=[THIRD_QUEUE_ID, SECOND_QUEUE_ID, QUEUE_ID],
            created_at_us=BASE_US + 100,
        )
        w.update_queued_submission(
            queued_submission_id=SECOND_QUEUE_ID,
            expected_version=1,
            state="claimed",
            claimed_by="worker-repo-1",
            claim_epoch=1,
            claim_expires_at_us=BASE_US + 9000,
            updated_at_us=BASE_US + 101,
        )

    assert [
        s.queued_submission_id
        for s in chat.read_effective_queue_order(
            owned.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
        )
    ] == [THIRD_QUEUE_ID, QUEUE_ID]


def test_a_submission_the_projection_does_not_name_still_reads_in_creation_order(
    owned: m1.Owned,
) -> None:
    seed_conversation(owned)
    seed_queued_submissions(owned, QUEUE_ID, SECOND_QUEUE_ID, THIRD_QUEUE_ID)
    with writer(owned) as w:
        w.insert_queue_order(
            conversation_id=CONVERSATION_ID,
            order=[THIRD_QUEUE_ID],
            created_at_us=BASE_US + 100,
        )

    assert [
        s.queued_submission_id
        for s in chat.read_effective_queue_order(
            owned.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
        )
    ] == [THIRD_QUEUE_ID, QUEUE_ID, SECOND_QUEUE_ID]


def test_a_malformed_queue_order_is_refused_before_the_write(owned: m1.Owned) -> None:
    seed_conversation(owned)
    seed_queued_submissions(owned, QUEUE_ID, SECOND_QUEUE_ID)
    with writer(owned) as w:
        with pytest.raises(StorageError, match="between 1 and 1000"):
            w.insert_queue_order(
                conversation_id=CONVERSATION_ID, order=[], created_at_us=BASE_US + 100
            )
        with pytest.raises(StorageError, match="more than once"):
            w.insert_queue_order(
                conversation_id=CONVERSATION_ID,
                order=[QUEUE_ID, QUEUE_ID],
                created_at_us=BASE_US + 100,
            )


def test_a_string_bytes_or_mapping_queue_order_is_refused_rather_than_flattened(
    owned: m1.Owned,
) -> None:
    """A caller-supplied order that is itself a string, bytes value or mapping is
    a caller mistake, not a one-member or per-character sequence: `list(order)`
    would otherwise silently flatten it into the wrong members."""
    seed_conversation(owned)
    seed_queued_submissions(owned, QUEUE_ID)
    with writer(owned) as w:
        for bad_order in (QUEUE_ID, QUEUE_ID.encode(), bytearray(QUEUE_ID.encode()), {QUEUE_ID: 1}):
            with pytest.raises(StorageError, match="requires a sequence"):
                w.insert_queue_order(
                    conversation_id=CONVERSATION_ID,
                    order=bad_order,  # type: ignore[arg-type]
                    created_at_us=BASE_US + 100,
                )
    with writer(owned) as w:
        w.insert_queue_order(
            conversation_id=CONVERSATION_ID, order=[QUEUE_ID], created_at_us=BASE_US + 100
        )
    with writer(owned) as w, pytest.raises(StorageError, match="requires a sequence"):
        w.update_queue_order(
            conversation_id=CONVERSATION_ID,
            expected_version=1,
            order=QUEUE_ID,  # type: ignore[arg-type]
            updated_at_us=BASE_US + 101,
        )


def test_the_queue_reads_are_workspace_scoped_and_bounded(owned: m1.Owned) -> None:
    seed_conversation(owned)
    seed_queued_submissions(owned, QUEUE_ID, SECOND_QUEUE_ID, THIRD_QUEUE_ID)
    assert (
        chat.read_queue_order(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, conversation_id=CONVERSATION_ID
        )
        is None
    )
    assert (
        chat.read_effective_queue_order(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, conversation_id=CONVERSATION_ID
        )
        == ()
    )
    bounded = chat.read_effective_queue_order(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        limit=2,
    )
    assert len(bounded) == 2


def test_the_limit_slices_the_ordered_result_not_the_rows_it_is_ordered_from(
    owned: m1.Owned,
) -> None:
    """A projection that moves the last submission to the front must not lose it
    to a short read: the bound applies after the order, not before it."""
    seed_conversation(owned)
    seed_queued_submissions(owned, QUEUE_ID, SECOND_QUEUE_ID, THIRD_QUEUE_ID)
    with writer(owned) as w:
        w.insert_queue_order(
            conversation_id=CONVERSATION_ID,
            order=[THIRD_QUEUE_ID, SECOND_QUEUE_ID, QUEUE_ID],
            created_at_us=BASE_US + 100,
        )

    assert [
        s.queued_submission_id
        for s in chat.read_effective_queue_order(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            conversation_id=CONVERSATION_ID,
            limit=1,
        )
    ] == [THIRD_QUEUE_ID]


def test_effective_queue_order_reads_reject_out_of_bounds_limit(owned: m1.Owned) -> None:
    seed_conversation(owned)
    seed_queued_submissions(owned, QUEUE_ID)
    for bad_limit in (0, -1, chat.MAX_QUEUE_ORDER_MEMBERS + 1):
        with pytest.raises(StorageError, match="limit must be between 1 and 1000"):
            chat.read_effective_queue_order(
                owned.connection,
                workspace_id=WORKSPACE_ID,
                conversation_id=CONVERSATION_ID,
                limit=bad_limit,
            )


def test_a_tampered_non_string_queue_order_member_fails_closed_on_read(
    owned: m1.Owned,
) -> None:
    seed_conversation(owned)
    seed_queued_submissions(owned, QUEUE_ID)
    with writer(owned) as w:
        w.insert_queue_order(
            conversation_id=CONVERSATION_ID, order=[QUEUE_ID], created_at_us=BASE_US + 100
        )
    connection = repo29.tamper(
        owned,
        "DROP TRIGGER omnivia_guard_chat_queued_submission_order_update",
        "UPDATE omnivia_chat_queued_submission_order SET order_json = '[1]' "
        f"WHERE workspace_id = '{WORKSPACE_ID}' AND conversation_id = '{CONVERSATION_ID}'",
    )
    try:
        with pytest.raises(StorageError, match="non-string submission identifier"):
            chat.read_queue_order(
                connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
            )
    finally:
        connection.close()


# --- 4. the authoritative snapshot inputs -----------------------------------------


def test_snapshot_inputs_carry_the_branch_path_from_the_head_backwards(
    owned: m1.Owned,
) -> None:
    seed_running_job(owned)
    with writer(owned) as w:
        w.append_branch_head_event(
            event_id="head-event-repo-2",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            head_version=2,
            previous_head_message_id=ROOT_MESSAGE_ID,
            new_head_message_id=TRIGGER_MESSAGE_ID,
            cause="user_message_appended",
            command_id="command-repo-2",
            graph_revision=2,
            conversation_sequence=2,
            actor_id=ACTOR_ID,
            occurred_at_us=BASE_US + 45,
            schema_version=1,
        )
        w.update_branch_head(
            branch_id=BRANCH_ID,
            expected_head_version=1,
            head_version=2,
            current_head_message_id=TRIGGER_MESSAGE_ID,
            state="open",
        )
        w.insert_view_state(
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
            active_branch_id=BRANCH_ID,
            last_seen_graph_revision=2,
            schema_version=1,
            updated_at_us=BASE_US + 46,
        )

    inputs = chat.read_conversation_snapshot_inputs(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        actor_id=ACTOR_ID,
    )
    assert inputs is not None
    assert inputs.conversation.conversation_id == CONVERSATION_ID
    assert inputs.branch is not None
    assert inputs.branch.branch_id == BRANCH_ID
    assert inputs.view_state is not None
    assert inputs.view_state.active_branch_id == BRANCH_ID
    # Oldest first, ending at the branch head.
    assert [m.message_id for m in inputs.path] == [ROOT_MESSAGE_ID, TRIGGER_MESSAGE_ID]
    assert [p.part_id for p in inputs.parts_by_message_id[ROOT_MESSAGE_ID]] == [
        repo29.ROOT_PART_ID
    ]
    assert inputs.parts_by_message_id[TRIGGER_MESSAGE_ID] == ()
    assert inputs.generation_job_ids == ()


def test_snapshot_inputs_bound_the_path_at_its_oldest_end(owned: m1.Owned) -> None:
    seed_running_job(owned)
    with writer(owned) as w:
        w.append_branch_head_event(
            event_id="head-event-repo-2",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            head_version=2,
            previous_head_message_id=ROOT_MESSAGE_ID,
            new_head_message_id=TRIGGER_MESSAGE_ID,
            cause="user_message_appended",
            command_id="command-repo-2",
            graph_revision=2,
            conversation_sequence=2,
            actor_id=ACTOR_ID,
            occurred_at_us=BASE_US + 45,
            schema_version=1,
        )
        w.update_branch_head(
            branch_id=BRANCH_ID,
            expected_head_version=1,
            head_version=2,
            current_head_message_id=TRIGGER_MESSAGE_ID,
            state="open",
        )

    inputs = chat.read_conversation_snapshot_inputs(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        actor_id=ACTOR_ID,
        branch_id=BRANCH_ID,
        max_path_messages=1,
    )
    assert inputs is not None
    assert [m.message_id for m in inputs.path] == [TRIGGER_MESSAGE_ID]


def test_snapshot_inputs_reject_out_of_bounds_max_path_messages(owned: m1.Owned) -> None:
    seed_running_job(owned)
    for bad_bound in (0, -1, 201):
        with pytest.raises(StorageError, match="max_path_messages must be between 1 and 200"):
            chat.read_conversation_snapshot_inputs(
                owned.connection,
                workspace_id=WORKSPACE_ID,
                conversation_id=CONVERSATION_ID,
                actor_id=ACTOR_ID,
                max_path_messages=bad_bound,
            )


def test_snapshot_inputs_are_workspace_scoped_and_answer_none_for_an_absent_conversation(
    owned: m1.Owned,
) -> None:
    seed_conversation(owned)
    assert (
        chat.read_conversation_snapshot_inputs(
            owned.connection,
            workspace_id=OTHER_WORKSPACE_ID,
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
        )
        is None
    )
    assert (
        chat.read_conversation_snapshot_inputs(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            conversation_id="conv-absent",
            actor_id=ACTOR_ID,
        )
        is None
    )


def test_snapshot_inputs_answer_without_a_branch_or_view_state(owned: m1.Owned) -> None:
    """A conversation with no default branch and no actor view state is still a
    conversation: the snapshot reads what exists rather than refusing."""
    seed_conversation(owned)
    inputs = chat.read_conversation_snapshot_inputs(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        actor_id="actor-with-no-view-state",
    )
    assert inputs is not None
    assert inputs.branch is None
    assert inputs.view_state is None
    assert inputs.path == ()
    assert inputs.parts_by_message_id == {}
