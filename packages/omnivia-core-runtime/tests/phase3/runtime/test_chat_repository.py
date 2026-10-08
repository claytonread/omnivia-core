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
from types import MappingProxyType

import pytest
import test_application_audit_idempotency_migration as m1
import test_chat_direction_migration as dm
import test_chat_foundation_migration as foundation
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


# --- 10. read_conversation_snapshot_inputs: the authoritative snapshot read -----


SECOND_BRANCH_ID = "branch-repo-second"
RESULT_MESSAGE_ID = "msg-repo-result"
SECOND_RESULT_MESSAGE_ID = "msg-repo-result-2"


def seed_branch_head_advance(holder: m1.Owned) -> None:
    """Advance BRANCH_ID's head to TRIGGER_MESSAGE_ID, one message past the root."""
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
        )
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
            occurred_at_us=BASE_US + 42,
            schema_version=1,
        )
        w.update_branch_head(
            branch_id=BRANCH_ID,
            expected_head_version=1,
            head_version=2,
            current_head_message_id=TRIGGER_MESSAGE_ID,
            state="open",
        )


def seed_two_results_from_the_same_job(holder: m1.Owned) -> None:
    """Past TRIGGER_MESSAGE_ID: one generation job, and two assistant messages that
    both name it -- the shape :func:`read_conversation_snapshot_inputs` must dedupe
    rather than report twice.
    """
    seed_branch_head_advance(holder)
    with writer(holder) as w:
        w.append_generation_job(
            generation_job_id=JOB_ID,
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            trigger_message_id=TRIGGER_MESSAGE_ID,
            graph_revision_observed=2,
            idempotency_key="generation-repo-snapshot-key",
            schema_version=1,
            created_at_us=BASE_US + 43,
            updated_at_us=BASE_US + 43,
        )
        w.update_conversation(
            conversation_id=CONVERSATION_ID,
            expected_graph_revision=2,
            graph_revision=2,
            latest_conversation_sequence=4,
            state="active",
            updated_at_us=BASE_US + 44,
            title="Repository chat",
            title_source="user",
        )
        w.append_message(
            message_id=RESULT_MESSAGE_ID,
            conversation_id=CONVERSATION_ID,
            role="assistant",
            author_type="provider",
            parent_message_id=TRIGGER_MESSAGE_ID,
            conversation_sequence=3,
            schema_version=1,
            content_hash=digest("d"),
            completion_status="complete",
            visibility="standard",
            generation_job_id=JOB_ID,
            created_at_us=BASE_US + 45,
            committed_at_us=BASE_US + 45,
        )
        w.append_message(
            message_id=SECOND_RESULT_MESSAGE_ID,
            conversation_id=CONVERSATION_ID,
            role="assistant",
            author_type="provider",
            parent_message_id=RESULT_MESSAGE_ID,
            conversation_sequence=4,
            schema_version=1,
            content_hash=digest("e"),
            completion_status="complete",
            visibility="standard",
            generation_job_id=JOB_ID,
            created_at_us=BASE_US + 46,
            committed_at_us=BASE_US + 46,
        )
        w.append_branch_head_event(
            event_id="head-event-repo-3",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            head_version=3,
            previous_head_message_id=TRIGGER_MESSAGE_ID,
            new_head_message_id=RESULT_MESSAGE_ID,
            cause="assistant_message_materialised",
            command_id="command-repo-3",
            graph_revision=2,
            conversation_sequence=3,
            actor_id=ACTOR_ID,
            occurred_at_us=BASE_US + 47,
            schema_version=1,
        )
        w.update_branch_head(
            branch_id=BRANCH_ID,
            expected_head_version=2,
            head_version=3,
            current_head_message_id=RESULT_MESSAGE_ID,
            state="open",
        )
        w.append_branch_head_event(
            event_id="head-event-repo-4",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            head_version=4,
            previous_head_message_id=RESULT_MESSAGE_ID,
            new_head_message_id=SECOND_RESULT_MESSAGE_ID,
            cause="assistant_message_materialised",
            command_id="command-repo-4",
            graph_revision=2,
            conversation_sequence=4,
            actor_id=ACTOR_ID,
            occurred_at_us=BASE_US + 48,
            schema_version=1,
        )
        w.update_branch_head(
            branch_id=BRANCH_ID,
            expected_head_version=3,
            head_version=4,
            current_head_message_id=SECOND_RESULT_MESSAGE_ID,
            state="open",
        )
        w.insert_view_state(
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
            active_branch_id=BRANCH_ID,
            last_seen_graph_revision=2,
            schema_version=1,
            updated_at_us=BASE_US + 49,
        )


def test_snapshot_inputs_walk_the_branch_path_oldest_to_newest_with_deduped_job_ids(
    owned: m1.Owned,
) -> None:
    seed_conversation(owned)
    seed_two_results_from_the_same_job(owned)

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
    assert [m.message_id for m in inputs.path] == [
        ROOT_MESSAGE_ID,
        TRIGGER_MESSAGE_ID,
        RESULT_MESSAGE_ID,
        SECOND_RESULT_MESSAGE_ID,
    ]
    assert [p.part_id for p in inputs.parts_by_message_id[ROOT_MESSAGE_ID]] == [ROOT_PART_ID]
    assert inputs.parts_by_message_id[TRIGGER_MESSAGE_ID] == ()
    # Two messages name the same job; it is reported once.
    assert inputs.generation_job_ids == (JOB_ID,)
    assert inputs.generation_job_ids_truncated is False
    assert isinstance(inputs.parts_by_message_id, MappingProxyType)


def test_snapshot_inputs_publish_every_durable_branch_job_not_only_referenced_ones(
    owned: m1.Owned,
) -> None:
    """A job is durable from the moment `SubmitMessage` opens it, and no assistant
    Message references it until generation produces one. The projection is the
    branch's whole job set, deduplicated against the ids the path already names,
    and never another branch's, conversation's or workspace's.
    """
    seed_conversation(owned)
    seed_two_results_from_the_same_job(owned)
    with writer(owned) as w:
        # Freshly opened, still `queued`, no result Message: exactly the job a
        # hosted controller has to prove immediately after a submit.
        w.append_generation_job(
            generation_job_id="generation-job-repo-live",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            trigger_message_id=SECOND_RESULT_MESSAGE_ID,
            graph_revision_observed=2,
            idempotency_key="generation-repo-live-key",
            schema_version=1,
            created_at_us=BASE_US + 60,
            updated_at_us=BASE_US + 60,
        )
        w.append_branch(
            branch_id=SECOND_BRANCH_ID,
            conversation_id=CONVERSATION_ID,
            origin_kind="explicit_fork",
            initial_head_message_id=ROOT_MESSAGE_ID,
            created_by_actor_id=ACTOR_ID,
            created_at_us=BASE_US + 61,
            created_conversation_sequence=1,
            schema_version=1,
        )
        w.append_generation_job(
            generation_job_id="generation-job-repo-other-branch",
            conversation_id=CONVERSATION_ID,
            branch_id=SECOND_BRANCH_ID,
            trigger_message_id=ROOT_MESSAGE_ID,
            graph_revision_observed=2,
            idempotency_key="generation-repo-other-branch-key",
            schema_version=1,
            created_at_us=BASE_US + 62,
            updated_at_us=BASE_US + 62,
        )

    inputs = chat.read_conversation_snapshot_inputs(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        actor_id=ACTOR_ID,
    )
    assert inputs is not None
    # The referenced job stays first and is not duplicated; the unreferenced one
    # is published beside it; the other branch's is not published at all.
    assert inputs.generation_job_ids == (JOB_ID, "generation-job-repo-live")
    assert inputs.generation_job_ids_truncated is False

    # The same read, scoped away: another workspace or another conversation holds
    # none of these jobs.
    assert (
        chat.read_branch_generation_job_ids(
            owned.connection,
            workspace_id=OTHER_WORKSPACE_ID,
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
        )
        == ()
    )
    assert (
        chat.read_branch_generation_job_ids(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            conversation_id="conv-repo-absent",
            branch_id=BRANCH_ID,
        )
        == ()
    )
    assert chat.read_branch_generation_job_ids(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        branch_id=SECOND_BRANCH_ID,
    ) == ("generation-job-repo-other-branch",)


def test_snapshot_inputs_flag_a_branch_job_set_past_the_governed_maximum(
    owned: m1.Owned,
) -> None:
    """4096 is `BranchPathResult.generationJobIds`' governed bound. One job past it
    is reported as such rather than silently shortened -- the caller fails the whole
    snapshot closed, because a truncated list would read as "that job does not
    exist" to a controller proving a candidate against it.
    """
    seed_conversation(owned)
    with writer(owned) as w:
        for index in range(4097):
            w.append_generation_job(
                generation_job_id=f"generation-job-repo-bulk-{index:05d}",
                conversation_id=CONVERSATION_ID,
                branch_id=BRANCH_ID,
                trigger_message_id=ROOT_MESSAGE_ID,
                graph_revision_observed=2,
                idempotency_key=f"generation-repo-bulk-key-{index:05d}",
                schema_version=1,
                created_at_us=BASE_US + 100 + index,
                updated_at_us=BASE_US + 100 + index,
            )

    inputs = chat.read_conversation_snapshot_inputs(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        actor_id=ACTOR_ID,
        branch_id=BRANCH_ID,
    )
    assert inputs is not None
    assert len(inputs.generation_job_ids) == 4097
    assert inputs.generation_job_ids_truncated is True


def test_snapshot_inputs_bound_the_path_at_its_oldest_end(owned: m1.Owned) -> None:
    seed_conversation(owned)
    seed_two_results_from_the_same_job(owned)

    inputs = chat.read_conversation_snapshot_inputs(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        actor_id=ACTOR_ID,
        branch_id=BRANCH_ID,
        max_path_messages=2,
    )
    assert inputs is not None
    # The newest two messages, not the oldest two.
    assert [m.message_id for m in inputs.path] == [RESULT_MESSAGE_ID, SECOND_RESULT_MESSAGE_ID]


def test_snapshot_inputs_reject_out_of_bounds_max_path_messages(owned: m1.Owned) -> None:
    seed_conversation(owned)
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
            conversation_id="conv-repo-absent",
            actor_id=ACTOR_ID,
        )
        is None
    )


def test_snapshot_inputs_answer_without_a_branch_or_view_state(owned: m1.Owned) -> None:
    """A conversation with no default branch and no actor view state is still a
    conversation: the snapshot reads what exists rather than refusing.
    """
    with writer(owned) as w:
        w.append_conversation(
            conversation_id="conv-repo-no-branch",
            state="active",
            graph_revision=1,
            latest_conversation_sequence=0,
            schema_version=1,
            created_by_actor_id=ACTOR_ID,
            created_at_us=BASE_US,
            updated_at_us=BASE_US,
        )

    inputs = chat.read_conversation_snapshot_inputs(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id="conv-repo-no-branch",
        actor_id="actor-repo-with-no-view-state",
    )
    assert inputs is not None
    assert inputs.branch is None
    assert inputs.view_state is None
    assert inputs.path == ()
    assert inputs.parts_by_message_id == {}
    assert inputs.generation_job_ids == ()


def test_snapshot_inputs_prefer_explicit_branch_then_view_state_then_default(
    owned: m1.Owned,
) -> None:
    seed_conversation(owned)
    with writer(owned) as w:
        w.append_branch(
            branch_id=SECOND_BRANCH_ID,
            conversation_id=CONVERSATION_ID,
            origin_kind="explicit_fork",
            initial_head_message_id=ROOT_MESSAGE_ID,
            created_by_actor_id=ACTOR_ID,
            created_at_us=BASE_US + 5,
            created_conversation_sequence=1,
            schema_version=1,
            created_from_branch_id=BRANCH_ID,
            fork_parent_message_id=ROOT_MESSAGE_ID,
            fork_source_message_id=ROOT_MESSAGE_ID,
        )
        w.update_conversation(
            conversation_id=CONVERSATION_ID,
            expected_graph_revision=2,
            graph_revision=2,
            latest_conversation_sequence=1,
            state="active",
            updated_at_us=BASE_US + 6,
            title="Repository chat",
            title_source="user",
            default_branch_id=BRANCH_ID,
        )
        w.insert_view_state(
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
            active_branch_id=SECOND_BRANCH_ID,
            last_seen_graph_revision=2,
            schema_version=1,
            updated_at_us=BASE_US + 7,
        )

    # An explicit branch_id wins even though this actor's view state names another.
    explicit = chat.read_conversation_snapshot_inputs(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        actor_id=ACTOR_ID,
        branch_id=BRANCH_ID,
    )
    assert explicit is not None
    assert explicit.branch is not None
    assert explicit.branch.branch_id == BRANCH_ID

    # No explicit branch: this actor's view state's active branch wins.
    via_view_state = chat.read_conversation_snapshot_inputs(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        actor_id=ACTOR_ID,
    )
    assert via_view_state is not None
    assert via_view_state.branch is not None
    assert via_view_state.branch.branch_id == SECOND_BRANCH_ID

    # No explicit branch and no view state for this actor: the conversation default wins.
    via_default = chat.read_conversation_snapshot_inputs(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        actor_id="actor-repo-with-no-view-state",
    )
    assert via_default is not None
    assert via_default.branch is not None
    assert via_default.branch.branch_id == BRANCH_ID


def test_snapshot_inputs_treat_a_branch_from_another_conversation_as_absent(
    owned: m1.Owned,
) -> None:
    seed_conversation(owned)
    with writer(owned) as w:
        w.append_conversation(
            conversation_id="conv-repo-other",
            state="active",
            graph_revision=1,
            latest_conversation_sequence=0,
            schema_version=1,
            created_by_actor_id=ACTOR_ID,
            created_at_us=BASE_US,
            updated_at_us=BASE_US,
        )
        w.update_conversation(
            conversation_id="conv-repo-other",
            expected_graph_revision=1,
            graph_revision=2,
            latest_conversation_sequence=1,
            state="active",
            updated_at_us=BASE_US + 1,
        )
        w.append_message(
            message_id="msg-repo-other-root",
            conversation_id="conv-repo-other",
            role="user",
            author_type="human",
            author_id=ACTOR_ID,
            conversation_sequence=1,
            schema_version=1,
            content_hash=digest("f"),
            completion_status="complete",
            visibility="standard",
            created_at_us=BASE_US + 1,
            committed_at_us=BASE_US + 1,
        )
        w.append_branch(
            branch_id="branch-repo-other",
            conversation_id="conv-repo-other",
            origin_kind="original",
            initial_head_message_id="msg-repo-other-root",
            created_by_actor_id=ACTOR_ID,
            created_at_us=BASE_US + 2,
            created_conversation_sequence=1,
            schema_version=1,
        )

    inputs = chat.read_conversation_snapshot_inputs(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        actor_id=ACTOR_ID,
        branch_id="branch-repo-other",
    )
    assert inputs is not None
    assert inputs.branch is None
    assert inputs.path == ()


# --- 11. one shared read transaction owns every component read ------------------


def test_snapshot_inputs_standalone_call_emits_one_begin_and_one_commit(
    owned: m1.Owned,
) -> None:
    seed_conversation(owned)
    statements: list[str] = []
    owned.connection.set_trace_callback(statements.append)
    try:
        inputs = chat.read_conversation_snapshot_inputs(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
        )
    finally:
        owned.connection.set_trace_callback(None)
    assert inputs is not None
    assert [s for s in statements if s in ("BEGIN", "COMMIT", "ROLLBACK")] == [
        "BEGIN",
        "COMMIT",
    ]


def test_snapshot_inputs_leave_a_callers_transaction_open(owned: m1.Owned) -> None:
    """The read ends only a transaction it opened itself."""
    seed_conversation(owned)
    owned.connection.execute("BEGIN")
    try:
        inputs = chat.read_conversation_snapshot_inputs(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
        )
        assert inputs is not None
        assert owned.connection.in_transaction is True
    finally:
        owned.connection.execute("ROLLBACK")


def test_snapshot_inputs_roll_back_only_a_transaction_it_opened(
    owned: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_conversation(owned)

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("forced snapshot read failure")

    monkeypatch.setattr(chat, "read_message_parts", broken)

    # Standalone: the transaction this call opened is rolled back before it re-raises.
    with pytest.raises(RuntimeError, match="forced snapshot read failure"):
        chat.read_conversation_snapshot_inputs(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
            branch_id=BRANCH_ID,
        )
    assert owned.connection.in_transaction is False

    # Caller-owned: the same failure leaves the caller's transaction open, because
    # this function never opened one of its own to roll back.
    owned.connection.execute("BEGIN")
    try:
        with pytest.raises(RuntimeError, match="forced snapshot read failure"):
            chat.read_conversation_snapshot_inputs(
                owned.connection,
                workspace_id=WORKSPACE_ID,
                conversation_id=CONVERSATION_ID,
                actor_id=ACTOR_ID,
                branch_id=BRANCH_ID,
            )
        assert owned.connection.in_transaction is True
    finally:
        owned.connection.execute("ROLLBACK")


# --- 12. no wire drift ------------------------------------------------------------


def test_this_lane_moved_no_public_contract() -> None:
    """W2-R is persistence over 0029; the frozen Chat wire surface is untouched."""
    assert CONTRACT_VERSION == "1.0.0-rc.1"


# --- 13. C6 Directions and continuations (migration 0069) --------------------------

DIRECTION_TEXT = "Focus on evidence."
OTHER_ACTOR_ID = "actor-other"


def direction_content(text: str = DIRECTION_TEXT) -> dict[str, object]:
    return {"editableParts": [{"payload": {"text": text}, "type": "text"}]}


def append_direction(w: chat.ChatWriter, sequence: int = 1, **overrides: object) -> None:
    """A Direction on the migration fixture's running Job; `direction-<sequence>`."""
    fields: dict[str, object] = {
        "direction_id": f"direction-{sequence}",
        "conversation_id": foundation.CONVERSATION_ID,
        "branch_id": foundation.BRANCH_ID,
        "generation_job_id": dm.JOB_ID,
        "target_attempt_id": dm.ATTEMPT_1,
        "direction_sequence": sequence,
        "actor_id": foundation.ACTOR_ID,
        "content": direction_content(),
        "created_at_us": BASE_US + 300 + sequence,
    }
    fields.update(overrides)
    w.append_direction(**fields)  # type: ignore[arg-type]


def seed_direction(holder: m1.Owned, sequence: int = 1, **overrides: object) -> None:
    dm.seed_job(holder)
    with writer(holder) as w:
        append_direction(w, sequence, **overrides)


def read_direction(holder: m1.Owned, direction_id: str = "direction-1") -> chat.Direction:
    found = chat.read_direction(
        holder.connection, workspace_id=WORKSPACE_ID, direction_id=direction_id
    )
    assert found is not None
    return found


def apply_direction(holder: m1.Owned) -> None:
    """direction-1 applied by attempt 2 with its bound manifest, through the repository."""
    dm.add_manifest(holder)
    with writer(holder) as w:
        w.update_direction(
            direction_id="direction-1",
            expected_version=1,
            state="applied",
            terminal_at_us=BASE_US + 400,
            effective_boundary="next_iteration",
            applied_attempt_id=dm.ATTEMPT_2,
            applied_provider_invocation_id=dm.INVOCATIONS[dm.ATTEMPT_2],
            applied_request_manifest_id=dm.MANIFEST_IDS[dm.ATTEMPT_2],
            applied_iteration=1,
        )


def continuation_fields(**overrides: object) -> dict[str, object]:
    fields = dm.continuation_row(**overrides)
    del fields["workspace_id"]
    return fields


def test_a_pending_direction_round_trips_exact_canonical_content_and_digest(
    owned: m1.Owned,
) -> None:
    seed_direction(owned)

    stored = owned.connection.execute(
        "SELECT content_json, content_digest FROM omnivia_chat_directions "
        "WHERE workspace_id = ? AND direction_id = 'direction-1'",
        (WORKSPACE_ID,),
    ).fetchone()
    assert stored == (dm.CONTENT_JSON, dm.CONTENT_DIGEST)

    expected = chat.Direction(
        workspace_id=WORKSPACE_ID,
        conversation_id=foundation.CONVERSATION_ID,
        branch_id=foundation.BRANCH_ID,
        generation_job_id=dm.JOB_ID,
        target_attempt_id=dm.ATTEMPT_1,
        direction_id="direction-1",
        direction_sequence=1,
        actor_id=foundation.ACTOR_ID,
        state="pending",
        version=1,
        content=direction_content(),
        content_digest=dm.CONTENT_DIGEST,
        requested_boundary="earliest_safe",
        effective_boundary=None,
        supersedes_direction_id=None,
        superseded_by_direction_id=None,
        applied_attempt_id=None,
        applied_provider_invocation_id=None,
        applied_request_manifest_id=None,
        applied_iteration=None,
        terminal_reason=None,
        schema_version=1,
        created_at_us=BASE_US + 301,
        updated_at_us=BASE_US + 301,
        terminal_at_us=None,
    )
    assert read_direction(owned) == expected
    assert chat.read_pending_direction(
        owned.connection, workspace_id=WORKSPACE_ID, generation_job_id=dm.JOB_ID
    ) == expected
    assert chat.read_direction(
        owned.connection, workspace_id=WORKSPACE_ID, direction_id="direction-absent"
    ) is None
    assert owned.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert owned.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_append_direction_accepts_no_state_version_boundary_digest_or_evidence(
    owned: m1.Owned,
) -> None:
    dm.seed_job(owned)
    for field in ("state", "version", "requested_boundary", "content_digest", "applied_iteration"):
        with pytest.raises(TypeError), writer(owned) as w:
            append_direction(w, **{field: "x"})


SIDE_JOB_ID = "generation-job-side"
SIDE_ATTEMPT_ID = "generation-attempt-side-1"
SUPERSEDED_REASON = {"code": "direction.superseded", "by": "direction-2"}


def seed_side_branch_job(holder: m1.Owned) -> None:
    """A running Job on a second branch of the same conversation, one Attempt."""
    with foundation.guarded(holder):
        foundation.insert(holder, foundation.BRANCHES, foundation.branch_row(branch_id="branch-side"))
        foundation.insert(
            holder,
            foundation.JOBS,
            foundation.generation_job_row(
                generation_job_id=SIDE_JOB_ID,
                branch_id="branch-side",
                idempotency_key="generation-key-side",
                created_at_us=BASE_US + 102,
                updated_at_us=BASE_US + 102,
            ),
        )
        foundation.insert(
            holder,
            foundation.PROVIDER_INVOCATIONS,
            foundation.provider_invocation_row(
                invocation_id="provider-invocation-side-1",
                job_id=SIDE_JOB_ID,
                generation_attempt_id=SIDE_ATTEMPT_ID,
            ),
        )
        foundation.insert(
            holder,
            foundation.ATTEMPTS,
            foundation.generation_attempt_row(
                generation_attempt_id=SIDE_ATTEMPT_ID,
                generation_job_id=SIDE_JOB_ID,
                attempt_number=1,
                retry_of_attempt_id=None,
                provider_invocation_id="provider-invocation-side-1",
                started_at_us=BASE_US + 201,
            ),
        )
        foundation.insert(holder, dm.STATUS, dm.status_row(SIDE_JOB_ID, SIDE_ATTEMPT_ID))


def test_direction_reads_are_workspace_conversation_branch_and_actor_scoped_and_ordered(
    owned: m1.Owned,
) -> None:
    dm.seed_job(owned)
    seed_side_branch_job(owned)
    with writer(owned) as w:
        append_direction(w, 1, direction_id="direction-other", generation_job_id=dm.OTHER_JOB_ID,
                         target_attempt_id=dm.OTHER_ATTEMPT_1)
        append_direction(w, 1, direction_id="direction-side", generation_job_id=SIDE_JOB_ID,
                         target_attempt_id=SIDE_ATTEMPT_ID, branch_id="branch-side")
        append_direction(w, 1)
        # Supersession is one caller-held transaction: old row first, then the successor.
        w.update_direction(
            direction_id="direction-1",
            expected_version=1,
            state="superseded",
            terminal_at_us=BASE_US + 400,
            terminal_reason=SUPERSEDED_REASON,
            superseded_by_direction_id="direction-2",
        )
        append_direction(
            w, 2, supersedes_direction_id="direction-1", actor_id=OTHER_ACTOR_ID,
            created_at_us=BASE_US + 400,
        )

    def ids(rows: tuple[chat.Direction, ...]) -> list[str]:
        return [row.direction_id for row in rows]

    scope = {"workspace_id": WORKSPACE_ID, "conversation_id": foundation.CONVERSATION_ID}
    by_job = chat.read_directions_for_generation_job(
        owned.connection, workspace_id=WORKSPACE_ID, generation_job_id=dm.JOB_ID
    )
    assert ids(by_job) == ["direction-1", "direction-2"]
    assert (by_job[0].state, by_job[0].version) == ("superseded", 2)
    assert by_job[0].terminal_reason == SUPERSEDED_REASON
    assert by_job[1].supersedes_direction_id == "direction-1"
    pending = chat.read_pending_direction(
        owned.connection, workspace_id=WORKSPACE_ID, generation_job_id=dm.JOB_ID
    )
    assert pending is not None and pending.direction_id == "direction-2"

    def actor_rows(branch_id: str, actor_id: str, **overrides: str):
        return chat.read_actor_directions_for_branch(
            owned.connection, **{**scope, **overrides}, branch_id=branch_id, actor_id=actor_id
        )

    # Job order, then sequence: generation-job-1 sorts before generation-job-2.
    assert ids(actor_rows(foundation.BRANCH_ID, foundation.ACTOR_ID)) == [
        "direction-1",
        "direction-other",
    ]
    assert ids(actor_rows(foundation.BRANCH_ID, OTHER_ACTOR_ID)) == ["direction-2"]
    assert ids(actor_rows("branch-side", foundation.ACTOR_ID)) == ["direction-side"]
    assert actor_rows("branch-side", OTHER_ACTOR_ID) == ()
    assert actor_rows(foundation.BRANCH_ID, foundation.ACTOR_ID, conversation_id="conv-nope") == ()
    assert actor_rows(
        foundation.BRANCH_ID, foundation.ACTOR_ID, workspace_id=OTHER_WORKSPACE_ID
    ) == ()
    assert chat.read_direction(
        owned.connection, workspace_id=OTHER_WORKSPACE_ID, direction_id="direction-1"
    ) is None
    assert chat.read_pending_direction(
        owned.connection, workspace_id=OTHER_WORKSPACE_ID, generation_job_id=dm.JOB_ID
    ) is None
    assert chat.read_directions_for_generation_job(
        owned.connection, workspace_id=OTHER_WORKSPACE_ID, generation_job_id=dm.JOB_ID
    ) == ()
    assert owned.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_direction_cas_rejects_a_stale_version_and_a_second_terminal_transition(
    owned: m1.Owned,
) -> None:
    seed_direction(owned)
    before = read_direction(owned)
    reason = {"code": "direction.rejected"}
    for stale in (0, 2):
        with pytest.raises(StaleVersion), writer(owned) as w:
            w.update_direction(
                direction_id="direction-1", expected_version=stale, state="rejected",
                terminal_at_us=BASE_US + 400, terminal_reason=reason,
            )
    assert read_direction(owned) == before

    # 0069 refuses a non-terminal target state even when the token matches.
    with pytest.raises(sqlite3.IntegrityError, match="transition is invalid"), writer(owned) as w:
        w.update_direction(
            direction_id="direction-1", expected_version=1, state="pending",
            terminal_at_us=BASE_US + 400,
        )
    assert read_direction(owned) == before

    with writer(owned) as w:
        w.update_direction(
            direction_id="direction-1", expected_version=1, state="rejected",
            terminal_at_us=BASE_US + 400, terminal_reason=reason,
        )
    terminal = read_direction(owned)
    assert (terminal.state, terminal.version) == ("rejected", 2)
    assert terminal.updated_at_us == terminal.terminal_at_us == BASE_US + 400
    # A terminal row is never matched again, at its old or its new version.
    for version in (1, 2):
        with pytest.raises(StaleVersion), writer(owned) as w:
            w.update_direction(
                direction_id="direction-1", expected_version=version, state="cancelled",
                terminal_at_us=BASE_US + 500, terminal_reason=reason,
            )
    assert read_direction(owned) == terminal


@pytest.mark.parametrize("state", ["rejected", "cancelled"])
def test_a_rejected_or_cancelled_direction_stores_its_reason_as_canonical_json(
    owned: m1.Owned, state: str
) -> None:
    seed_direction(owned)
    with writer(owned) as w:
        w.update_direction(
            direction_id="direction-1", expected_version=1, state=state,
            terminal_at_us=BASE_US + 400, terminal_reason={"detail": "é", "code": "direction.closed"},
        )
    stored = owned.connection.execute(
        "SELECT terminal_reason_json FROM omnivia_chat_directions WHERE direction_id = 'direction-1'"
    ).fetchone()[0]
    assert stored == '{"code":"direction.closed","detail":"é"}'
    direction = read_direction(owned)
    assert direction.state == state
    assert direction.terminal_reason == {"code": "direction.closed", "detail": "é"}
    assert direction.effective_boundary is None and direction.applied_attempt_id is None
    # The terminal rule itself still demands a reason.
    with pytest.raises(sqlite3.IntegrityError), writer(owned) as w:
        append_direction(w, 2)
        w.update_direction(
            direction_id="direction-2", expected_version=1, state=state,
            terminal_at_us=BASE_US + 500,
        )
    assert chat.read_direction(
        owned.connection, workspace_id=WORKSPACE_ID, direction_id="direction-2"
    ) is None


def test_an_applied_direction_carries_its_complete_application_evidence(owned: m1.Owned) -> None:
    seed_direction(owned)
    apply_direction(owned)
    applied = read_direction(owned)
    assert (applied.state, applied.version) == ("applied", 2)
    assert applied.effective_boundary == "next_iteration"
    assert applied.requested_boundary == "earliest_safe"
    assert applied.applied_attempt_id == dm.ATTEMPT_2
    assert applied.applied_provider_invocation_id == dm.INVOCATIONS[dm.ATTEMPT_2]
    assert applied.applied_request_manifest_id == dm.MANIFEST_IDS[dm.ATTEMPT_2]
    assert applied.applied_iteration == 1
    assert applied.terminal_reason is None
    assert applied.updated_at_us == applied.terminal_at_us == BASE_US + 400
    assert applied.content_digest == dm.CONTENT_DIGEST
    # Incomplete evidence is refused by 0069 and leaves the row pending.
    with pytest.raises(sqlite3.IntegrityError), writer(owned) as w:
        append_direction(w, 2)
        w.update_direction(
            direction_id="direction-2", expected_version=1, state="applied",
            terminal_at_us=BASE_US + 450, effective_boundary="next_iteration",
        )
    assert chat.read_direction(
        owned.connection, workspace_id=WORKSPACE_ID, direction_id="direction-2"
    ) is None


def test_supersession_is_atomic_and_rolls_back_when_the_successor_insert_fails(
    owned: m1.Owned,
) -> None:
    seed_direction(owned)
    before = read_direction(owned)
    with pytest.raises(sqlite3.IntegrityError, match="contiguous"), writer(owned) as w:
        w.update_direction(
            direction_id="direction-1", expected_version=1, state="superseded",
            terminal_at_us=BASE_US + 400, terminal_reason=SUPERSEDED_REASON,
            superseded_by_direction_id="direction-3",
        )
        append_direction(w, 3, supersedes_direction_id="direction-1")
    assert read_direction(owned) == before
    assert chat.read_directions_for_generation_job(
        owned.connection, workspace_id=WORKSPACE_ID, generation_job_id=dm.JOB_ID
    ) == (before,)

    # Two standalone transactions cannot work: the successor insert needs the old row
    # already superseded, and the superseded row needs its successor by commit.
    with pytest.raises(sqlite3.IntegrityError):
        chat.update_direction(
            owned.connection, owned.identity, workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation, direction_id="direction-1",
            expected_version=1, state="superseded", terminal_at_us=BASE_US + 400,
            terminal_reason=SUPERSEDED_REASON, superseded_by_direction_id="direction-2",
        )
    assert read_direction(owned) == before
    with pytest.raises(sqlite3.IntegrityError, match="already superseded"):
        chat.append_direction(
            owned.connection, owned.identity, workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation, direction_id="direction-2",
            conversation_id=foundation.CONVERSATION_ID, branch_id=foundation.BRANCH_ID,
            generation_job_id=dm.JOB_ID, target_attempt_id=dm.ATTEMPT_1, direction_sequence=2,
            actor_id=foundation.ACTOR_ID, content=direction_content("again"),
            created_at_us=BASE_US + 402, supersedes_direction_id="direction-1",
        )
    assert read_direction(owned) == before


def test_a_continuation_reads_back_through_both_readers_and_refuses_duplicates(
    owned: m1.Owned,
) -> None:
    seed_direction(owned)
    apply_direction(owned)
    fields = continuation_fields()
    with writer(owned) as w:
        w.append_generation_continuation(**fields)  # type: ignore[arg-type]

    expected = chat.GenerationContinuation(workspace_id=WORKSPACE_ID, **fields)  # type: ignore[arg-type]
    assert chat.read_generation_continuation(
        owned.connection, workspace_id=WORKSPACE_ID, continuation_id="continuation-1"
    ) == expected
    assert chat.read_generation_continuation_for_direction(
        owned.connection, workspace_id=WORKSPACE_ID, direction_id="direction-1"
    ) == expected
    for reader, key in (
        (chat.read_generation_continuation, {"continuation_id": "continuation-1"}),
        (chat.read_generation_continuation_for_direction, {"direction_id": "direction-1"}),
    ):
        assert reader(owned.connection, workspace_id=OTHER_WORKSPACE_ID, **key) is None
    assert chat.read_generation_continuation_for_direction(
        owned.connection, workspace_id=WORKSPACE_ID, direction_id="direction-absent"
    ) is None

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"), writer(owned) as w:
        w.append_generation_continuation(**continuation_fields(continuation_id="continuation-2"))  # type: ignore[arg-type]
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE|PRIMARY"), writer(owned) as w:
        w.append_generation_continuation(**fields)  # type: ignore[arg-type]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), writer(owned) as w:
        w.connection.execute(
            "UPDATE omnivia_chat_generation_continuations SET created_at_us = created_at_us + 1"
        )
    assert owned.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert owned.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_an_invalid_continuation_is_refused(owned: m1.Owned) -> None:
    seed_direction(owned)
    with pytest.raises(sqlite3.IntegrityError, match="applied direction"), writer(owned) as w:
        w.append_generation_continuation(**continuation_fields())  # type: ignore[arg-type]
    apply_direction(owned)
    for override in (
        {"direction_version": 3},
        {"provider_finish_id": "provider-finish-wrong"},
        {"prior_output_digest": dm.OTHER_DIGEST},
    ):
        with pytest.raises(sqlite3.IntegrityError), writer(owned) as w:
            w.append_generation_continuation(**continuation_fields(**override))  # type: ignore[arg-type]
    assert chat.read_generation_continuation_for_direction(
        owned.connection, workspace_id=WORKSPACE_ID, direction_id="direction-1"
    ) is None


def test_standalone_direction_wrappers_and_the_transaction_local_writer(owned: m1.Owned) -> None:
    dm.seed_job(owned)
    fence = {
        "workspace_id": WORKSPACE_ID,
        "fencing_generation": owned.generation,
    }
    chat.append_direction(
        owned.connection, owned.identity, **fence,
        direction_id="direction-1", conversation_id=foundation.CONVERSATION_ID,
        branch_id=foundation.BRANCH_ID, generation_job_id=dm.JOB_ID,
        target_attempt_id=dm.ATTEMPT_1, direction_sequence=1, actor_id=foundation.ACTOR_ID,
        content=direction_content(), created_at_us=BASE_US + 301,
    )
    assert owned.connection.in_transaction is False
    chat.update_direction(
        owned.connection, owned.identity, **fence, direction_id="direction-1",
        expected_version=1, state="cancelled", terminal_at_us=BASE_US + 400,
        terminal_reason={"code": "direction.cancelled"},
    )
    assert read_direction(owned).state == "cancelled"

    # The transaction-local writer opens nothing of its own: it joins a caller-held fence,
    # and outside one 0069 refuses the write.
    local = chat.transaction_local_writer(owned.connection, workspace_id=WORKSPACE_ID)
    with foundation.guarded(owned):
        append_direction(local, 2)
    assert read_direction(owned, "direction-2").state == "pending"
    owned.connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded INSERT"):
            append_direction(local, 3)
    finally:
        owned.connection.execute("ROLLBACK")
    assert chat.read_direction(
        owned.connection, workspace_id=WORKSPACE_ID, direction_id="direction-3"
    ) is None


def test_the_standalone_continuation_wrapper_writes_one_fenced_row(owned: m1.Owned) -> None:
    seed_direction(owned)
    apply_direction(owned)
    chat.append_generation_continuation(
        owned.connection, owned.identity, workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation, **continuation_fields(),  # type: ignore[arg-type]
    )
    assert owned.connection.in_transaction is False
    assert chat.read_generation_continuation(
        owned.connection, workspace_id=WORKSPACE_ID, continuation_id="continuation-1"
    ) is not None


TAMPER_TRIGGER = "DROP TRIGGER omnivia_guard_chat_directions_update"
TAMPER_WHERE = f"WHERE workspace_id = '{WORKSPACE_ID}' AND direction_id = 'direction-1'"


def read_every_direction_way(connection: sqlite3.Connection) -> list[object]:
    scope = {"workspace_id": WORKSPACE_ID}
    return [
        lambda: chat.read_direction(connection, direction_id="direction-1", **scope),
        lambda: chat.read_pending_direction(connection, generation_job_id=dm.JOB_ID, **scope),
        lambda: chat.read_directions_for_generation_job(
            connection, generation_job_id=dm.JOB_ID, **scope
        ),
        lambda: chat.read_actor_directions_for_branch(
            connection, conversation_id=foundation.CONVERSATION_ID,
            branch_id=foundation.BRANCH_ID, actor_id=foundation.ACTOR_ID, **scope,
        ),
    ]


@pytest.mark.parametrize(
    ("update", "message"),
    [
        (f"content_digest = '{dm.OTHER_DIGEST}'", "digest does not match"),
        ("content_json = '{\"editableParts\": []}'", "not canonical JSON"),
        ("content_json = 'not-json-at-all'", "not valid JSON"),
        ("content_json = '[]'", "not a JSON object"),
    ],
)
def test_a_tampered_direction_fails_closed_on_every_reader(
    owned: m1.Owned, update: str, message: str
) -> None:
    seed_direction(owned)
    connection = tamper(owned, TAMPER_TRIGGER, f"UPDATE omnivia_chat_directions SET {update} {TAMPER_WHERE}")
    try:
        for read in read_every_direction_way(connection):
            with pytest.raises(StorageError, match=message):
                read()  # type: ignore[operator]
    finally:
        connection.close()


def test_a_noncanonical_stored_terminal_reason_fails_closed(owned: m1.Owned) -> None:
    seed_direction(owned)
    with writer(owned) as w:
        w.update_direction(
            direction_id="direction-1", expected_version=1, state="rejected",
            terminal_at_us=BASE_US + 400, terminal_reason={"code": "x"},
        )
    connection = tamper(
        owned,
        TAMPER_TRIGGER,
        f"UPDATE omnivia_chat_directions SET terminal_reason_json = '{{\"code\": \"x\"}}' {TAMPER_WHERE}",
    )
    try:
        with pytest.raises(StorageError, match="terminal reason is not canonical JSON"):
            chat.read_direction(connection, workspace_id=WORKSPACE_ID, direction_id="direction-1")
    finally:
        connection.close()


# --- 14. Provider-invocation identity (0028), reached for a continuation Attempt -----

INVOCATION_ID = "provider-inv-repo-1"
INVOCATION_FIELDS: dict[str, object] = {
    "invocation_id": INVOCATION_ID,
    "conversation_id": CONVERSATION_ID,
    "job_id": JOB_ID,
    "generation_attempt_id": ATTEMPT_ID,
    "configured_connection_id": "connection-repo-1",
    "configured_model_id": "vendor/model-repo:1@2",
    "created_at_us": BASE_US + 500,
}


def invocation_fields(**overrides: object) -> dict[str, object]:
    return {**INVOCATION_FIELDS, **overrides}


def invocation_rows(holder: m1.Owned) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in holder.connection.execute("SELECT * FROM omnivia_provider_invocations")
    ]


def test_a_provider_invocation_identity_is_written_once_under_the_fence_and_reads_back(
    owned: m1.Owned,
) -> None:
    with writer(owned) as w:
        written = w.append_provider_invocation(**invocation_fields())  # type: ignore[arg-type]

    expected = chat.ProviderInvocation(
        workspace_id=WORKSPACE_ID,
        invocation_id=INVOCATION_ID,
        conversation_id=CONVERSATION_ID,
        job_id=JOB_ID,
        generation_attempt_id=ATTEMPT_ID,
        operation="language.stream",
        configured_connection_id="connection-repo-1",
        configured_model_id="vendor/model-repo:1@2",
        created_at_us=BASE_US + 500,
    )
    assert written == expected
    assert (
        chat.read_provider_invocation(
            owned.connection, workspace_id=WORKSPACE_ID, invocation_id=INVOCATION_ID
        )
        == expected
    )
    assert (
        chat.read_provider_invocation(
            owned.connection, workspace_id=OTHER_WORKSPACE_ID, invocation_id=INVOCATION_ID
        )
        is None
    )
    assert len(invocation_rows(owned)) == 1
    assert owned.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert owned.connection.execute("PRAGMA foreign_key_check").fetchall() == []

    # The typed writer is the only way in: the same row written without the fence is refused.
    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded INSERT"):
        owned.connection.execute(
            "INSERT INTO omnivia_provider_invocations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                WORKSPACE_ID,
                "provider-inv-unguarded",
                CONVERSATION_ID,
                JOB_ID,
                ATTEMPT_ID,
                "language.stream",
                "connection-repo-1",
                "model-repo-1",
                BASE_US,
            ),
        )


def test_an_identical_provider_invocation_replay_returns_the_durable_row_and_writes_nothing(
    owned: m1.Owned,
) -> None:
    with writer(owned) as w:
        first = w.append_provider_invocation(**invocation_fields())  # type: ignore[arg-type]
    before = owned.connection.total_changes

    with writer(owned) as w:
        replayed = w.append_provider_invocation(
            **invocation_fields(created_at_us=BASE_US + 9_000)  # type: ignore[arg-type]
        )
    again = chat.append_provider_invocation_once(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        **invocation_fields(created_at_us=BASE_US + 9_001),  # type: ignore[arg-type]
    )

    # The durable row, not the replay's clock: the identity was written at its own instant.
    assert replayed == first == again
    assert replayed.created_at_us == BASE_US + 500
    assert owned.connection.total_changes == before
    assert len(invocation_rows(owned)) == 1


@pytest.mark.parametrize(
    "different",
    [
        {"conversation_id": "conv-repo-other"},
        {"job_id": "generation-job-repo-other"},
        {"generation_attempt_id": "generation-attempt-repo-other"},
        {"configured_connection_id": "connection-repo-other"},
        {"configured_model_id": "vendor/model-repo-other"},
    ],
)
def test_a_provider_invocation_replay_naming_another_identity_is_a_conflict(
    owned: m1.Owned, different: dict[str, object]
) -> None:
    with writer(owned) as w:
        w.append_provider_invocation(**invocation_fields())  # type: ignore[arg-type]
    stored = invocation_rows(owned)

    with pytest.raises(chat.ProviderInvocationConflict, match=INVOCATION_ID), writer(owned) as w:
        w.append_provider_invocation(**invocation_fields(**different))  # type: ignore[arg-type]
    with pytest.raises(chat.ProviderInvocationConflict):
        chat.append_provider_invocation_once(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            **invocation_fields(**different),  # type: ignore[arg-type]
        )

    # Append-only: the first identity stands, byte for byte.
    assert invocation_rows(owned) == stored
    assert owned.connection.in_transaction is False


@pytest.mark.parametrize(
    "bad",
    [
        {"invocation_id": ""},
        {"invocation_id": "a" * 129},
        {"invocation_id": ".leading-dot"},
        {"invocation_id": "has space"},
        {"invocation_id": "nul\x00byte"},
        {"invocation_id": 7},
        {"conversation_id": "slash/not-allowed-here"},
        {"job_id": "x" * 129},
        {"generation_attempt_id": None},
        {"configured_connection_id": "https://endpoint.invalid"},
        {"configured_model_id": ""},
        {"configured_model_id": "m" * 257},
        {"configured_model_id": "vendor://model"},
        {"configured_model_id": "-leading-dash"},
        {"created_at_us": 0},
        {"created_at_us": True},
        {"created_at_us": "1"},
    ],
)
def test_an_out_of_bounds_provider_invocation_is_refused_before_anything_is_written(
    owned: m1.Owned, bad: dict[str, object]
) -> None:
    with pytest.raises(StorageError, match="provider invocation") as refused, writer(owned) as w:
        w.append_provider_invocation(**invocation_fields(**bad))  # type: ignore[arg-type]

    # A refusal that names the field, never the value that failed it.
    assert not isinstance(refused.value, sqlite3.Error)
    assert "https://endpoint.invalid" not in str(refused.value)
    assert invocation_rows(owned) == []


def test_a_provider_invocation_follows_the_transaction_it_is_written_in(owned: m1.Owned) -> None:
    class Abandoned(Exception):
        pass

    with pytest.raises(Abandoned), writer(owned) as w:
        w.append_provider_invocation(**invocation_fields())  # type: ignore[arg-type]
        raise Abandoned
    assert invocation_rows(owned) == []

    # And the same write retried lands once, which is what a crashed caller relies on.
    with writer(owned) as w:
        w.append_provider_invocation(**invocation_fields())  # type: ignore[arg-type]
    assert len(invocation_rows(owned)) == 1


def test_a_provider_invocation_is_not_written_without_current_authority(owned: m1.Owned) -> None:
    with pytest.raises(StorageError, match="authority"):
        chat.append_provider_invocation_once(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation + 1,
            **invocation_fields(),  # type: ignore[arg-type]
        )
    assert invocation_rows(owned) == []
    assert owned.connection.in_transaction is False
