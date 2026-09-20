"""W2-R acceptance for `storage/chat.py`'s Chat repository over migration 0029.

The migration tests hold 0029's guarded triggers to what SQL can enforce. These
hold the repository's writer, standalone wrappers and readers to what SQL cannot:
that a repository write actually goes through the fenced authority the triggers
require, that a direct unguarded write still fails, that reads are workspace-
scoped, that append-only rows are never silently overwritten, that a compare-and-
set update rejects a stale expected token without changing the row, that branch
head events compose with the branch projection the way 0029's own guard requires,
and that a stored JSON payload is canonical and fails closed when corrupted from
outside this database's own guards.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.chat import StaleVersion
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

from omnivia_core.chat_contract.v1 import CONTRACT_VERSION

WORKSPACE_ID = m1.WORKSPACE_ID
OTHER_WORKSPACE_ID = "ws-chat-repo-nope"
BASE_US = 2_500_000_000_000_000

CONVERSATION_ID = "conv-repo-01"
BRANCH_ID = "branch-repo-main"
ACTOR_ID = "actor-repo-human"
ROOT_MESSAGE_ID = "msg-repo-root"
ROOT_PART_ID = "part-repo-root-0"
TRIGGER_MESSAGE_ID = "msg-repo-trigger"
JOB_ID = "generation-job-repo-1"
ATTEMPT_ID = "generation-attempt-repo-1"
QUEUE_ID = "queue-repo-1"
OUTBOX_EVENT_ID = "domain-event-repo-1"


def digest(char: str) -> str:
    return "sha256:" + char * 64


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


def tamper(holder: m1.Owned, *statements: str) -> sqlite3.Connection:
    """Close the owned connection and reopen the file with no guard in the way.

    The guards refuse this from inside the runtime; a reader still has to behave
    when the file was corrupted from outside it, and this is the only way to make
    one -- matching `test_rt102_agent_runtime_repository.tamper`.
    """
    holder.connection.close()
    connection = sqlite3.connect(str(holder.path))
    for statement in statements:
        connection.execute(statement)
    connection.commit()
    return connection


def seed_conversation(holder: m1.Owned) -> None:
    """One conversation, one root message and part, one branch at that root."""
    with writer(holder) as w:
        w.append_conversation(
            conversation_id=CONVERSATION_ID,
            state="active",
            graph_revision=1,
            latest_conversation_sequence=0,
            schema_version=1,
            created_by_actor_id=ACTOR_ID,
            created_at_us=BASE_US,
            updated_at_us=BASE_US,
            title="Repository chat",
            title_source="user",
        )
        w.update_conversation(
            conversation_id=CONVERSATION_ID,
            expected_graph_revision=1,
            graph_revision=2,
            latest_conversation_sequence=1,
            state="active",
            updated_at_us=BASE_US + 1,
            title="Repository chat",
            title_source="user",
        )
        w.append_message(
            message_id=ROOT_MESSAGE_ID,
            conversation_id=CONVERSATION_ID,
            role="user",
            author_type="human",
            author_id=ACTOR_ID,
            conversation_sequence=1,
            schema_version=1,
            content_hash=digest("a"),
            completion_status="complete",
            visibility="standard",
            created_at_us=BASE_US + 2,
            committed_at_us=BASE_US + 2,
        )
        w.append_message_part(
            part_id=ROOT_PART_ID,
            message_id=ROOT_MESSAGE_ID,
            conversation_id=CONVERSATION_ID,
            part_index=0,
            part_type="text",
            schema_version=1,
            visibility="standard",
            payload={"text": "hello"},
            content_hash=digest("b"),
            created_at_us=BASE_US + 3,
            provenance="human",
        )
        w.append_branch(
            branch_id=BRANCH_ID,
            conversation_id=CONVERSATION_ID,
            origin_kind="original",
            initial_head_message_id=ROOT_MESSAGE_ID,
            created_by_actor_id=ACTOR_ID,
            created_at_us=BASE_US + 4,
            created_conversation_sequence=1,
            schema_version=1,
        )
        w.append_branch_head_event(
            event_id="head-event-repo-1",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            head_version=1,
            previous_head_message_id=None,
            new_head_message_id=ROOT_MESSAGE_ID,
            cause="branch_created",
            command_id="command-repo-1",
            graph_revision=2,
            conversation_sequence=1,
            actor_id=ACTOR_ID,
            occurred_at_us=BASE_US + 4,
            schema_version=1,
        )


# --- 1. repository writes pass the guarded triggers ------------------------------


def test_repository_writes_pass_the_guarded_0029_triggers(owned: m1.Owned) -> None:
    seed_conversation(owned)

    conversation = chat.read_conversation(
        owned.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
    )
    assert conversation is not None
    assert conversation.graph_revision == 2
    assert conversation.latest_conversation_sequence == 1

    messages = chat.read_messages_by_conversation_sequence(
        owned.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
    )
    assert [m.message_id for m in messages] == [ROOT_MESSAGE_ID]

    parts = chat.read_message_parts(
        owned.connection, workspace_id=WORKSPACE_ID, message_id=ROOT_MESSAGE_ID
    )
    assert [p.part_id for p in parts] == [ROOT_PART_ID]
    assert parts[0].payload == {"text": "hello"}

    events = chat.read_branch_head_events(
        owned.connection, workspace_id=WORKSPACE_ID, branch_id=BRANCH_ID
    )
    assert [e.head_version for e in events] == [1]

    assert owned.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert owned.connection.execute("PRAGMA foreign_key_check").fetchall() == []


# --- 2. direct unguarded writes still fail ---------------------------------------


def test_direct_unguarded_insert_still_fails(owned: m1.Owned) -> None:
    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded INSERT"):
        owned.connection.execute(
            "INSERT INTO omnivia_chat_conversations "
            "(workspace_id, conversation_id, title, title_source, state, "
            "default_branch_id, graph_revision, latest_conversation_sequence, "
            "schema_version, created_by_actor_id, created_at_us, updated_at_us, "
            "archived_at_us, tombstoned_at_us) "
            "VALUES (?, ?, NULL, NULL, 'active', NULL, 1, 0, 1, ?, ?, ?, NULL, NULL)",
            (WORKSPACE_ID, "conv-unguarded", ACTOR_ID, BASE_US, BASE_US),
        )


# --- 3. reads are workspace-scoped -----------------------------------------------


def test_reads_are_workspace_scoped(owned: m1.Owned) -> None:
    seed_conversation(owned)

    assert (
        chat.read_conversation(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, conversation_id=CONVERSATION_ID
        )
        is None
    )
    assert (
        chat.read_messages_by_conversation_sequence(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, conversation_id=CONVERSATION_ID
        )
        == ()
    )
    assert (
        chat.read_message_parts(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, message_id=ROOT_MESSAGE_ID
        )
        == ()
    )
    assert (
        chat.read_branch_head_events(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, branch_id=BRANCH_ID
        )
        == ()
    )


# --- 4. append-only facts are never silently overwritten -------------------------


def test_committed_messages_are_immutable_even_under_the_repositorys_own_fence(
    owned: m1.Owned,
) -> None:
    seed_conversation(owned)
    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        w.connection.execute(
            "UPDATE omnivia_chat_messages SET visibility = 'internal' "
            "WHERE workspace_id = ? AND message_id = ?",
            (WORKSPACE_ID, ROOT_MESSAGE_ID),
        )


def test_message_parts_have_no_update_function_and_the_table_refuses_one_anyway(
    owned: m1.Owned,
) -> None:
    assert not hasattr(chat, "update_message_part")
    seed_conversation(owned)
    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        w.connection.execute(
            "UPDATE omnivia_chat_message_parts SET provenance = 'system' "
            "WHERE workspace_id = ? AND part_id = ?",
            (WORKSPACE_ID, ROOT_PART_ID),
        )


# --- 5 & 7. compare-and-set updates reject stale expected tokens -----------------


def test_view_state_cas_rejects_a_stale_expected_version(owned: m1.Owned) -> None:
    seed_conversation(owned)
    with writer(owned) as w:
        w.insert_view_state(
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
            active_branch_id=BRANCH_ID,
            focused_message_id=ROOT_MESSAGE_ID,
            last_seen_graph_revision=2,
            schema_version=1,
            updated_at_us=BASE_US + 10,
        )

    with writer(owned) as w:
        w.update_view_state(
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
            expected_version=1,
            active_branch_id=BRANCH_ID,
            last_seen_graph_revision=2,
            updated_at_us=BASE_US + 11,
        )

    with writer(owned) as w, pytest.raises(StaleVersion):
        w.update_view_state(
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
            expected_version=1,
            active_branch_id=BRANCH_ID,
            last_seen_graph_revision=2,
            updated_at_us=BASE_US + 12,
        )

    state = chat.read_actor_view_state(
        owned.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID, actor_id=ACTOR_ID
    )
    assert state is not None
    assert state.version == 2
    assert state.updated_at_us == BASE_US + 11


def test_draft_cas_rejects_a_stale_expected_version(owned: m1.Owned) -> None:
    seed_conversation(owned)
    with writer(owned) as w:
        w.insert_draft(
            draft_id="draft-repo-1",
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
            mode="normal",
            text_content="hello",
            references=[],
            schema_version=1,
            updated_at_us=BASE_US + 20,
        )

    with writer(owned) as w, pytest.raises(StaleVersion):
        w.update_draft(
            draft_id="draft-repo-1",
            expected_version=99,
            text_content="edited",
            references=[],
            updated_at_us=BASE_US + 21,
        )

    draft = chat.read_active_draft(
        owned.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID, actor_id=ACTOR_ID
    )
    assert draft is not None
    assert draft.version == 1
    assert draft.text_content == "hello"


def test_queued_submission_cas_rejects_stale_version_and_the_trigger_rejects_the_rest(
    owned: m1.Owned,
) -> None:
    seed_conversation(owned)
    with writer(owned) as w:
        w.append_queued_submission(
            queued_submission_id=QUEUE_ID,
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
            queue_sequence=1,
            branch_id=BRANCH_ID,
            editable_parts=[{"partType": "text"}],
            references=[],
            idempotency_key="queue-repo-key-1",
            created_at_us=BASE_US + 30,
            updated_at_us=BASE_US + 30,
        )

    with writer(owned) as w, pytest.raises(StaleVersion):
        w.update_queued_submission(
            queued_submission_id=QUEUE_ID,
            expected_version=7,
            state="claimed",
            updated_at_us=BASE_US + 31,
            claimed_by="worker-repo-1",
            claim_epoch=1,
            claim_expires_at_us=BASE_US + 500,
        )

    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="transition"):
        w.update_queued_submission(
            queued_submission_id=QUEUE_ID,
            expected_version=1,
            state="submitted",
            updated_at_us=BASE_US + 32,
            submitted_message_id=ROOT_MESSAGE_ID,
            submitted_generation_job_id=JOB_ID,
        )

    submission = chat.read_queued_submission(
        owned.connection, workspace_id=WORKSPACE_ID, queued_submission_id=QUEUE_ID
    )
    assert submission is not None
    assert submission.version == 1
    assert submission.state == "queued"

    with writer(owned) as w:
        w.update_queued_submission(
            queued_submission_id=QUEUE_ID,
            expected_version=1,
            state="claimed",
            updated_at_us=BASE_US + 33,
            claimed_by="worker-repo-1",
            claim_epoch=1,
            claim_expires_at_us=BASE_US + 500,
        )
    submission = chat.read_queued_submission(
        owned.connection, workspace_id=WORKSPACE_ID, queued_submission_id=QUEUE_ID
    )
    assert submission is not None
    assert submission.state == "claimed"
    assert submission.version == 2


def test_generation_job_cas_rejects_a_stale_lease_and_the_trigger_rejects_bad_transitions(
    owned: m1.Owned,
) -> None:
    seed_conversation(owned)
    with writer(owned) as w:
        # `graph_revision` stays put: `omnivia_chat_branch_head_events` already
        # holds a row referencing revision 2, and that FK is not deferrable, so
        # advancing the conversation's single row past a value a durable row still
        # cites would orphan it. Only `latest_conversation_sequence` moves here.
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

    with writer(owned) as w, pytest.raises(StaleVersion):
        w.update_generation_job(
            generation_job_id=JOB_ID,
            expected_state="running",
            expected_lease_epoch=0,
            state="running",
            lease_epoch=1,
            current_attempt_id=ATTEMPT_ID,
            lease_owner="worker-repo-1",
            lease_expires_at_us=BASE_US + 500,
            heartbeat_at_us=BASE_US + 44,
            updated_at_us=BASE_US + 44,
            started_at_us=BASE_US + 44,
        )

    with writer(owned) as w:
        w.update_generation_job(
            generation_job_id=JOB_ID,
            expected_state="queued",
            expected_lease_epoch=0,
            state="running",
            lease_epoch=1,
            current_attempt_id=ATTEMPT_ID,
            lease_owner="worker-repo-1",
            lease_expires_at_us=BASE_US + 500,
            heartbeat_at_us=BASE_US + 44,
            updated_at_us=BASE_US + 44,
            started_at_us=BASE_US + 44,
        )

    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="transition"):
        w.update_generation_job(
            generation_job_id=JOB_ID,
            expected_state="running",
            expected_lease_epoch=1,
            state="queued",
            lease_epoch=1,
            updated_at_us=BASE_US + 45,
        )

    job = chat.read_generation_job(owned.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID)
    assert job is not None
    assert job.state == "running"
    assert job.lease_epoch == 1
    assert job.current_attempt_id == ATTEMPT_ID


def test_outbox_delivery_cas_rejects_a_stale_token_and_a_delivered_entry_cannot_reopen(
    owned: m1.Owned,
) -> None:
    seed_conversation(owned)
    with writer(owned) as w:
        w.append_outbox_entry(
            outbox_cursor=1,
            domain_event_id=OUTBOX_EVENT_ID,
            event_kind="chat.generation.succeeded",
            payload={"event": "succeeded"},
            created_at_us=BASE_US + 50,
            conversation_id=CONVERSATION_ID,
        )

    with writer(owned) as w, pytest.raises(StaleVersion):
        w.update_outbox_delivery(
            outbox_cursor=1,
            expected_delivery_state="delivering",
            expected_delivery_attempts=0,
            delivery_state="delivered",
            delivery_attempts=1,
            delivered_at_us=BASE_US + 51,
        )

    with writer(owned) as w:
        w.update_outbox_delivery(
            outbox_cursor=1,
            expected_delivery_state="pending",
            expected_delivery_attempts=0,
            delivery_state="delivered",
            delivery_attempts=1,
            delivered_at_us=BASE_US + 51,
        )

    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="cannot reopen"):
        w.update_outbox_delivery(
            outbox_cursor=1,
            expected_delivery_state="delivered",
            expected_delivery_attempts=1,
            delivery_state="pending",
            delivery_attempts=1,
        )

    entry = chat.read_outbox_event(owned.connection, workspace_id=WORKSPACE_ID, domain_event_id=OUTBOX_EVENT_ID)
    assert entry is not None
    assert entry.delivery_state == "delivered"
    assert entry.delivery_attempts == 1


# --- 6. branch head event sequence / expected-head rules -------------------------


def test_branch_head_events_are_contiguous_and_backed_by_the_prior_head(
    owned: m1.Owned,
) -> None:
    seed_conversation(owned)

    with writer(owned) as w:
        # `graph_revision` stays at 2: the first head event already cites it, and
        # that FK is not deferrable (see the generation-job CAS test above).
        w.update_conversation(
            conversation_id=CONVERSATION_ID,
            expected_graph_revision=2,
            graph_revision=2,
            latest_conversation_sequence=2,
            state="active",
            updated_at_us=BASE_US + 60,
            title="Repository chat",
            title_source="user",
        )
        w.append_message(
            message_id="msg-repo-second",
            conversation_id=CONVERSATION_ID,
            role="user",
            author_type="human",
            author_id=ACTOR_ID,
            parent_message_id=ROOT_MESSAGE_ID,
            conversation_sequence=2,
            schema_version=1,
            content_hash=digest("d"),
            completion_status="complete",
            visibility="standard",
            created_at_us=BASE_US + 61,
            committed_at_us=BASE_US + 61,
        )

    # A head event that skips a version is refused as non-contiguous.
    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        w.append_branch_head_event(
            event_id="head-event-repo-skip",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            head_version=3,
            previous_head_message_id=ROOT_MESSAGE_ID,
            new_head_message_id="msg-repo-second",
            cause="user_message_appended",
            command_id="command-repo-skip",
            graph_revision=2,
            conversation_sequence=2,
            actor_id=ACTOR_ID,
            occurred_at_us=BASE_US + 62,
            schema_version=1,
        )

    # A head event naming the wrong prior head is refused even at the right version.
    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="prior head"):
        w.append_branch_head_event(
            event_id="head-event-repo-wrong-prior",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            head_version=2,
            previous_head_message_id="msg-repo-second",
            new_head_message_id="msg-repo-second",
            cause="user_message_appended",
            command_id="command-repo-wrong-prior",
            graph_revision=2,
            conversation_sequence=2,
            actor_id=ACTOR_ID,
            occurred_at_us=BASE_US + 63,
            schema_version=1,
        )

    with writer(owned) as w:
        w.append_branch_head_event(
            event_id="head-event-repo-2",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            head_version=2,
            previous_head_message_id=ROOT_MESSAGE_ID,
            new_head_message_id="msg-repo-second",
            cause="user_message_appended",
            command_id="command-repo-2",
            graph_revision=2,
            conversation_sequence=2,
            actor_id=ACTOR_ID,
            occurred_at_us=BASE_US + 64,
            schema_version=1,
        )
        w.update_branch_head(
            branch_id=BRANCH_ID,
            expected_head_version=1,
            head_version=2,
            current_head_message_id="msg-repo-second",
            state="open",
        )

    events = chat.read_branch_head_events(owned.connection, workspace_id=WORKSPACE_ID, branch_id=BRANCH_ID)
    assert [e.head_version for e in events] == [1, 2]
    assert events[-1].new_head_message_id == "msg-repo-second"


def test_branch_head_cas_rejects_a_stale_expected_head_version(owned: m1.Owned) -> None:
    seed_conversation(owned)
    with writer(owned) as w, pytest.raises(StaleVersion):
        w.update_branch_head(
            branch_id=BRANCH_ID,
            expected_head_version=99,
            head_version=2,
            current_head_message_id=ROOT_MESSAGE_ID,
            state="open",
        )


# --- 8. deterministic, workspace-scoped outbox cursor reads ----------------------


def test_outbox_cursor_reads_are_deterministic_and_workspace_scoped(owned: m1.Owned) -> None:
    seed_conversation(owned)
    with writer(owned) as w:
        w.append_outbox_entry(
            outbox_cursor=1,
            domain_event_id="domain-event-repo-a",
            event_kind="chat.generation.queued",
            payload={"n": 1},
            created_at_us=BASE_US + 70,
            conversation_id=CONVERSATION_ID,
        )
        w.append_outbox_entry(
            outbox_cursor=2,
            domain_event_id="domain-event-repo-b",
            event_kind="chat.generation.queued",
            payload={"n": 2},
            created_at_us=BASE_US + 71,
            conversation_id=CONVERSATION_ID,
        )

    since_zero = chat.read_outbox_events_since(owned.connection, workspace_id=WORKSPACE_ID, after_cursor=0)
    assert [e.outbox_cursor for e in since_zero] == [1, 2]

    since_one = chat.read_outbox_events_since(owned.connection, workspace_id=WORKSPACE_ID, after_cursor=1)
    assert [e.outbox_cursor for e in since_one] == [2]

    by_domain_id = chat.read_outbox_event(
        owned.connection, workspace_id=WORKSPACE_ID, domain_event_id="domain-event-repo-a"
    )
    assert by_domain_id is not None
    assert by_domain_id.outbox_cursor == 1

    assert (
        chat.read_outbox_events_since(owned.connection, workspace_id=OTHER_WORKSPACE_ID, after_cursor=0) == ()
    )
    assert (
        chat.read_outbox_event(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, domain_event_id="domain-event-repo-a"
        )
        is None
    )


# --- 9. JSON payloads are canonical, and corruption fails closed on read ---------


def test_message_part_payload_is_stored_as_exact_canonical_json(owned: m1.Owned) -> None:
    seed_conversation(owned)
    stored = owned.connection.execute(
        "SELECT payload_json FROM omnivia_chat_message_parts WHERE workspace_id = ? AND part_id = ?",
        (WORKSPACE_ID, ROOT_PART_ID),
    ).fetchone()[0]
    assert stored == '{"text":"hello"}'


def test_a_non_canonical_stored_payload_fails_closed_on_read(owned: m1.Owned) -> None:
    seed_conversation(owned)
    connection = tamper(
        owned,
        "DROP TRIGGER omnivia_guard_chat_message_parts_update",
        "UPDATE omnivia_chat_message_parts SET payload_json = "
        "'{\"text\": \"hello\"}' "
        f"WHERE workspace_id = '{WORKSPACE_ID}' AND part_id = '{ROOT_PART_ID}'",
    )
    try:
        with pytest.raises(StorageError, match="not canonical JSON"):
            chat.read_message_parts(connection, workspace_id=WORKSPACE_ID, message_id=ROOT_MESSAGE_ID)
    finally:
        connection.close()


def test_an_invalid_stored_payload_fails_closed_on_read(owned: m1.Owned) -> None:
    seed_conversation(owned)
    connection = tamper(
        owned,
        "DROP TRIGGER omnivia_guard_chat_message_parts_update",
        "UPDATE omnivia_chat_message_parts SET payload_json = 'not-json-at-all' "
        f"WHERE workspace_id = '{WORKSPACE_ID}' AND part_id = '{ROOT_PART_ID}'",
    )
    try:
        with pytest.raises(StorageError, match="not valid JSON"):
            chat.read_message_parts(connection, workspace_id=WORKSPACE_ID, message_id=ROOT_MESSAGE_ID)
    finally:
        connection.close()


def test_generation_events_round_trip_their_canonical_payload(owned: m1.Owned) -> None:
    seed_conversation(owned)
    with writer(owned) as w:
        w.append_generation_job(
            generation_job_id=JOB_ID,
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            trigger_message_id=ROOT_MESSAGE_ID,
            graph_revision_observed=2,
            idempotency_key="generation-repo-events-key",
            schema_version=1,
            created_at_us=BASE_US + 80,
            updated_at_us=BASE_US + 80,
        )
        w.append_generation_event(
            event_id="generation-event-repo-1",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            generation_job_id=JOB_ID,
            event_type="chat.generation.queued",
            generation_event_sequence=1,
            trigger_message_id=ROOT_MESSAGE_ID,
            cursor="generation-cursor-repo-1",
            payload={"event": "chat.generation.queued"},
            occurred_at_us=BASE_US + 81,
            schema_version=1,
        )

    events = chat.read_generation_events(owned.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID)
    assert [e.generation_event_sequence for e in events] == [1]
    assert events[0].payload == {"event": "chat.generation.queued"}


# --- 10. no wire drift ------------------------------------------------------------


def test_this_lane_moved_no_public_contract() -> None:
    """W2-R is persistence over 0029; the frozen Chat wire surface is untouched."""
    assert CONTRACT_VERSION == "1.0.0-rc.1"
