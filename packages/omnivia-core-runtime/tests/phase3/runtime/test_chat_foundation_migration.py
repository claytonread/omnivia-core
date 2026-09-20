"""Acceptance for migration 0029's durable Chat foundation.

What 0029 is: a unique consecutive successor to 0028, pinned by content
checksum, whose objects are the durable Chat truth tables for conversations,
committed message graphs, branches, drafts, queued submissions, generation
jobs/events and the transactional Chat outbox.

What it is not: no repository, no service, no provider adapter, no renderer and
no Runtime session. Provider invocation truth stays in 0028 and is linked here
only by stable identifiers.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    foreign_key_check,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    load_migrations,
    materialise_phase0_baseline,
)

MIGRATION_VERSION = 29
PREDECESSOR_VERSION = 28
MIGRATION_NAME = "0029_chat_foundation.sql"
WORKSPACE_ID = m1.WORKSPACE_ID
BASE_US = 2_500_000_000_000_000

CONVERSATIONS = "omnivia_chat_conversations"
MESSAGES = "omnivia_chat_messages"
PARTS = "omnivia_chat_message_parts"
DERIVATIONS = "omnivia_chat_message_derivations"
BRANCHES = "omnivia_chat_message_branches"
HEAD_EVENTS = "omnivia_chat_branch_head_events"
VIEW_STATES = "omnivia_chat_conversation_view_states"
DRAFTS = "omnivia_chat_drafts"
QUEUED = "omnivia_chat_queued_submissions"
JOBS = "omnivia_chat_generation_jobs"
ATTEMPTS = "omnivia_chat_generation_attempts"
EVENTS = "omnivia_chat_generation_events"
OUTBOX = "omnivia_chat_transactional_outbox"
PROVIDER_INVOCATIONS = "omnivia_provider_invocations"

TABLES = (
    CONVERSATIONS,
    MESSAGES,
    PARTS,
    DERIVATIONS,
    BRANCHES,
    HEAD_EVENTS,
    VIEW_STATES,
    DRAFTS,
    QUEUED,
    JOBS,
    ATTEMPTS,
    EVENTS,
    OUTBOX,
)

INDEXES = {
    "omnivia_idx_chat_conversations_list",
    "omnivia_idx_chat_messages_conversation_sequence",
    "omnivia_idx_chat_messages_parent",
    "omnivia_idx_chat_message_parts_order",
    "omnivia_idx_chat_message_derivations_source",
    "omnivia_idx_chat_message_derivations_derived",
    "omnivia_idx_chat_message_branches_conversation",
    "omnivia_idx_chat_branch_head_events_order",
    "omnivia_idx_chat_view_states_actor",
    "omnivia_idx_chat_drafts_actor_updated",
    "omnivia_idx_chat_queued_submissions_order",
    "omnivia_idx_chat_generation_jobs_claim",
    "omnivia_idx_chat_generation_events_order",
    "omnivia_idx_chat_outbox_delivery",
}

TRIGGERS = {
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
    for table in TABLES
    for statement in ("insert", "update", "delete")
}

FORBIDDEN_STORAGE_NAMES = (
    "credential",
    "secret",
    "sdk",
    "endpoint",
    "url",
    "header",
    "body",
    "prompt",
    "renderer",
    "session",
)

CONVERSATION_ID = "conv-chat-01"
BRANCH_ID = "branch-main"
ACTOR_ID = "actor-human"
DEVICE_ID = "device-main"
ROOT_MESSAGE_ID = "msg-user-1"
EDITED_MESSAGE_ID = "msg-user-2"
TRIGGER_MESSAGE_ID = "msg-user-3"
ASSISTANT_MESSAGE_ID = "msg-assistant-4"
GENERATION_JOB_ID = "generation-job-1"
GENERATION_ATTEMPT_ID = "generation-attempt-1"
PROVIDER_INVOCATION_ID = "provider-invocation-1"
QUEUE_ID = "queue-submission-1"
OUTBOX_EVENT_ID = "domain-event-1"


def digest(char: str) -> str:
    return "sha256:" + char * 64


def migration_under_test() -> Any:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    return path


@pytest.fixture
def owned(migrated: Path) -> Iterator[m1.Owned]:
    holder = m1.take_ownership(migrated)
    yield holder
    holder.connection.close()


def guarded(holder: m1.Owned) -> Any:
    return fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def insert(holder: m1.Owned, table: str, row: dict[str, object]) -> None:
    holder.connection.execute(
        f"INSERT INTO {table} ({', '.join(row)}) "
        f"VALUES ({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )


def conversation_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "title": "Durable chat",
        "title_source": "user",
        "state": "active",
        "default_branch_id": None,
        "graph_revision": 2,
        "latest_conversation_sequence": 4,
        "schema_version": 1,
        "created_by_actor_id": ACTOR_ID,
        "created_at_us": BASE_US,
        "updated_at_us": BASE_US + 10,
        "archived_at_us": None,
        "tombstoned_at_us": None,
    }
    values.update(overrides)
    return values


def message_row(
    message_id: str,
    conversation_sequence: int,
    *,
    role: str = "user",
    parent_message_id: str | None = None,
    generation_job_id: str | None = None,
    conversation_id: str = CONVERSATION_ID,
    **overrides: object,
) -> dict[str, object]:
    author_type = {
        "user": "human",
        "assistant": "provider",
        "system": "system",
        "tool": "service",
    }[role]
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "parent_message_id": parent_message_id,
        "role": role,
        "author_type": author_type,
        "author_id": ACTOR_ID if role == "user" else "provider-service",
        "conversation_sequence": conversation_sequence,
        "schema_version": 1,
        "content_hash": digest("a"),
        "completion_status": "complete",
        "visibility": "standard",
        "created_on_branch_id": None,
        "generation_job_id": generation_job_id,
        "created_at_us": BASE_US + conversation_sequence,
        "committed_at_us": BASE_US + conversation_sequence,
        "tombstoned_at_us": None,
    }
    values.update(overrides)
    return values


def part_row(
    part_id: str,
    message_id: str,
    part_index: int,
    *,
    conversation_id: str = CONVERSATION_ID,
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "part_id": part_id,
        "part_index": part_index,
        "part_type": "text",
        "schema_version": 1,
        "visibility": "standard",
        "payload_json": '{"text":"hello"}',
        "provenance": "human",
        "content_hash": digest("b"),
        "created_at_us": BASE_US + 20 + part_index,
    }
    values.update(overrides)
    return values


def derivation_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "source_message_id": ROOT_MESSAGE_ID,
        "derived_message_id": EDITED_MESSAGE_ID,
        "derivation_kind": "amendment",
        "created_by_actor_id": ACTOR_ID,
        "created_at_us": BASE_US + 30,
        "metadata_json": '{"reason":"user-edit"}',
    }
    values.update(overrides)
    return values


def branch_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "branch_id": BRANCH_ID,
        "origin_kind": "original",
        "created_from_branch_id": None,
        "fork_parent_message_id": None,
        "fork_source_message_id": None,
        "initial_head_message_id": EDITED_MESSAGE_ID,
        "current_head_message_id": EDITED_MESSAGE_ID,
        "created_by_actor_id": ACTOR_ID,
        "created_at_us": BASE_US + 40,
        "created_conversation_sequence": 2,
        "head_version": 1,
        "schema_version": 1,
        "state": "open",
        "archived_at_us": None,
        "tombstoned_at_us": None,
    }
    values.update(overrides)
    return values


def head_event_row(
    head_version: int,
    *,
    previous_head_message_id: str | None,
    new_head_message_id: str,
    conversation_sequence: int,
    event_id: str | None = None,
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "branch_id": BRANCH_ID,
        "event_id": event_id or f"head-event-{head_version}",
        "head_version": head_version,
        "previous_head_message_id": previous_head_message_id,
        "new_head_message_id": new_head_message_id,
        "cause": "branch_created" if head_version == 1 else "user_message_appended",
        "command_id": f"command-{head_version}",
        "graph_revision": 2,
        "conversation_sequence": conversation_sequence,
        "actor_id": ACTOR_ID,
        "occurred_at_us": BASE_US + 50 + head_version,
        "schema_version": 1,
    }
    values.update(overrides)
    return values


def view_state_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "actor_id": ACTOR_ID,
        "device_id": DEVICE_ID,
        "active_branch_id": BRANCH_ID,
        "focused_message_id": EDITED_MESSAGE_ID,
        "last_seen_graph_revision": 2,
        "schema_version": 1,
        "version": 1,
        "updated_at_us": BASE_US + 60,
    }
    values.update(overrides)
    return values


def draft_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "actor_id": ACTOR_ID,
        "device_id": DEVICE_ID,
        "draft_id": "draft-1",
        "mode": "normal",
        "source_message_id": None,
        "text_content": "hello",
        "references_json": "{}",
        "target_json": None,
        "stashed_from_draft_id": None,
        "schema_version": 1,
        "version": 1,
        "updated_at_us": BASE_US + 70,
        "expires_at_us": BASE_US + 1_000,
    }
    values.update(overrides)
    return values


def queued_submission_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "actor_id": ACTOR_ID,
        "queued_submission_id": QUEUE_ID,
        "queue_sequence": 1,
        "branch_id": BRANCH_ID,
        "editable_parts_json": '{"parts":[]}',
        "references_json": "{}",
        "idempotency_key": "queue-key-1",
        "state": "queued",
        "version": 1,
        "claimed_by": None,
        "claim_epoch": None,
        "claim_expires_at_us": None,
        "submitted_message_id": None,
        "submitted_generation_job_id": None,
        "sanitized_error_code": None,
        "sanitized_error_detail": None,
        "created_at_us": BASE_US + 80,
        "updated_at_us": BASE_US + 80,
    }
    values.update(overrides)
    return values


def generation_job_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "branch_id": BRANCH_ID,
        "trigger_message_id": TRIGGER_MESSAGE_ID,
        "generation_job_id": GENERATION_JOB_ID,
        "state": "queued",
        "graph_revision_observed": 2,
        "idempotency_key": "generation-key-1",
        "current_attempt_id": None,
        "result_message_id": None,
        "lease_owner": None,
        "lease_epoch": 0,
        "lease_expires_at_us": None,
        "heartbeat_at_us": None,
        "last_event_sequence": 0,
        "sanitized_error_code": None,
        "sanitized_error_detail": None,
        "schema_version": 1,
        "created_at_us": BASE_US + 100,
        "updated_at_us": BASE_US + 100,
        "started_at_us": None,
        "finished_at_us": None,
    }
    values.update(overrides)
    return values


def provider_invocation_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "invocation_id": PROVIDER_INVOCATION_ID,
        "conversation_id": CONVERSATION_ID,
        "job_id": GENERATION_JOB_ID,
        "generation_attempt_id": GENERATION_ATTEMPT_ID,
        "operation": "language.stream",
        "configured_connection_id": "connection-primary",
        "configured_model_id": "model-primary",
        "created_at_us": BASE_US + 105,
    }
    values.update(overrides)
    return values


def generation_attempt_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "generation_job_id": GENERATION_JOB_ID,
        "generation_attempt_id": GENERATION_ATTEMPT_ID,
        "attempt_number": 1,
        "retry_of_attempt_id": None,
        "state": "running",
        "provider_invocation_id": PROVIDER_INVOCATION_ID,
        "schema_version": 1,
        "started_at_us": BASE_US + 110,
        "ended_at_us": None,
    }
    values.update(overrides)
    return values


def generation_event_row(
    sequence: int,
    event_type: str,
    *,
    generation_attempt_id: str | None = None,
    result_message_id: str | None = None,
    provider_event_id: str | None = None,
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "conversation_id": CONVERSATION_ID,
        "branch_id": BRANCH_ID,
        "generation_job_id": GENERATION_JOB_ID,
        "generation_attempt_id": generation_attempt_id,
        "event_id": f"generation-event-{sequence}",
        "event_type": event_type,
        "generation_event_sequence": sequence,
        "trigger_message_id": TRIGGER_MESSAGE_ID,
        "result_message_id": result_message_id,
        "provider_event_id": provider_event_id,
        "cursor": f"generation-cursor-{sequence}",
        "payload_json": f'{{"event":"{event_type}"}}',
        "occurred_at_us": BASE_US + 120 + sequence,
        "schema_version": 1,
    }
    values.update(overrides)
    return values


def outbox_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "outbox_cursor": 1,
        "domain_event_id": OUTBOX_EVENT_ID,
        "event_kind": "chat.generation.succeeded",
        "conversation_id": CONVERSATION_ID,
        "generation_job_id": GENERATION_JOB_ID,
        "payload_json": '{"event":"succeeded"}',
        "delivery_state": "pending",
        "delivery_attempts": 0,
        "next_delivery_after_us": BASE_US + 300,
        "delivered_at_us": None,
        "retained_until_us": BASE_US + 10_000,
        "created_at_us": BASE_US + 250,
    }
    values.update(overrides)
    return values


def row_for_table(table: str) -> dict[str, object]:
    return {
        CONVERSATIONS: conversation_row(),
        MESSAGES: message_row(ROOT_MESSAGE_ID, 1),
        PARTS: part_row("part-root-0", ROOT_MESSAGE_ID, 0),
        DERIVATIONS: derivation_row(),
        BRANCHES: branch_row(),
        HEAD_EVENTS: head_event_row(
            1,
            previous_head_message_id=None,
            new_head_message_id=EDITED_MESSAGE_ID,
            conversation_sequence=2,
        ),
        VIEW_STATES: view_state_row(),
        DRAFTS: draft_row(),
        QUEUED: queued_submission_row(),
        JOBS: generation_job_row(),
        ATTEMPTS: generation_attempt_row(),
        EVENTS: generation_event_row(1, "chat.generation.queued"),
        OUTBOX: outbox_row(),
    }[table]


def seed_conversation_graph(holder: m1.Owned) -> None:
    with guarded(holder):
        insert(holder, CONVERSATIONS, conversation_row())
        insert(holder, MESSAGES, message_row(ROOT_MESSAGE_ID, 1))
        insert(holder, MESSAGES, message_row(EDITED_MESSAGE_ID, 2))
        insert(
            holder,
            MESSAGES,
            message_row(TRIGGER_MESSAGE_ID, 3, parent_message_id=EDITED_MESSAGE_ID),
        )
        insert(holder, PARTS, part_row("part-root-0", ROOT_MESSAGE_ID, 0))
        insert(holder, PARTS, part_row("part-edit-0", EDITED_MESSAGE_ID, 0))
        insert(holder, PARTS, part_row("part-trigger-0", TRIGGER_MESSAGE_ID, 0))
        insert(holder, DERIVATIONS, derivation_row())
        insert(holder, BRANCHES, branch_row())
        insert(
            holder,
            HEAD_EVENTS,
            head_event_row(
                1,
                previous_head_message_id=None,
                new_head_message_id=EDITED_MESSAGE_ID,
                conversation_sequence=2,
            ),
        )
        insert(holder, VIEW_STATES, view_state_row())
        insert(holder, DRAFTS, draft_row())
        insert(holder, QUEUED, queued_submission_row())


def seed_queued_generation_job(holder: m1.Owned) -> None:
    seed_conversation_graph(holder)
    with guarded(holder):
        insert(holder, JOBS, generation_job_row())


def seed_running_generation_job(holder: m1.Owned) -> None:
    seed_queued_generation_job(holder)
    with guarded(holder):
        insert(holder, PROVIDER_INVOCATIONS, provider_invocation_row())
        insert(holder, ATTEMPTS, generation_attempt_row())
        insert(holder, EVENTS, generation_event_row(1, "chat.generation.queued"))
        holder.connection.execute(
            f"UPDATE {JOBS} SET state = 'running', current_attempt_id = ?, "
            "lease_owner = 'worker-1', lease_epoch = 1, "
            "lease_expires_at_us = ?, heartbeat_at_us = ?, "
            "last_event_sequence = 1, updated_at_us = ?, started_at_us = ? "
            "WHERE workspace_id = ? AND generation_job_id = ?",
            (
                GENERATION_ATTEMPT_ID,
                BASE_US + 500,
                BASE_US + 111,
                BASE_US + 111,
                BASE_US + 111,
                WORKSPACE_ID,
                GENERATION_JOB_ID,
            ),
        )


def seed_every_table(holder: m1.Owned) -> None:
    seed_running_generation_job(holder)
    with guarded(holder):
        insert(
            holder,
            EVENTS,
            generation_event_row(
                2,
                "chat.generation.started",
                generation_attempt_id=GENERATION_ATTEMPT_ID,
                provider_event_id="provider-event-started",
            ),
        )
        insert(
            holder,
            MESSAGES,
            message_row(
                ASSISTANT_MESSAGE_ID,
                4,
                role="assistant",
                parent_message_id=TRIGGER_MESSAGE_ID,
                generation_job_id=GENERATION_JOB_ID,
            ),
        )
        insert(holder, PARTS, part_row("part-assistant-0", ASSISTANT_MESSAGE_ID, 0))
        insert(
            holder,
            EVENTS,
            generation_event_row(
                3,
                "chat.generation.succeeded",
                generation_attempt_id=GENERATION_ATTEMPT_ID,
                result_message_id=ASSISTANT_MESSAGE_ID,
                provider_event_id="provider-event-succeeded",
            ),
        )
        insert(
            holder,
            HEAD_EVENTS,
            head_event_row(
                2,
                previous_head_message_id=EDITED_MESSAGE_ID,
                new_head_message_id=ASSISTANT_MESSAGE_ID,
                conversation_sequence=4,
            ),
        )
        holder.connection.execute(
            f"UPDATE {BRANCHES} SET current_head_message_id = ?, "
            "head_version = 2, state = 'open' "
            "WHERE workspace_id = ? AND branch_id = ?",
            (ASSISTANT_MESSAGE_ID, WORKSPACE_ID, BRANCH_ID),
        )
        holder.connection.execute(
            f"UPDATE {JOBS} SET state = 'succeeded', result_message_id = ?, "
            "last_event_sequence = 3, updated_at_us = ?, finished_at_us = ? "
            "WHERE workspace_id = ? AND generation_job_id = ?",
            (
                ASSISTANT_MESSAGE_ID,
                BASE_US + 210,
                BASE_US + 210,
                WORKSPACE_ID,
                GENERATION_JOB_ID,
            ),
        )
        holder.connection.execute(
            f"UPDATE {QUEUED} SET state = 'claimed', version = 2, "
            "claimed_by = 'worker-1', claim_epoch = 1, "
            "claim_expires_at_us = ?, updated_at_us = ? "
            "WHERE workspace_id = ? AND queued_submission_id = ?",
            (BASE_US + 500, BASE_US + 115, WORKSPACE_ID, QUEUE_ID),
        )
        holder.connection.execute(
            f"UPDATE {QUEUED} SET state = 'submitted', version = 3, "
            "submitted_message_id = ?, submitted_generation_job_id = ?, "
            "updated_at_us = ? "
            "WHERE workspace_id = ? AND queued_submission_id = ?",
            (
                ASSISTANT_MESSAGE_ID,
                GENERATION_JOB_ID,
                BASE_US + 220,
                WORKSPACE_ID,
                QUEUE_ID,
            ),
        )
        insert(holder, OUTBOX, outbox_row())


def test_0029_is_the_unique_consecutive_successor_to_0028() -> None:
    versions = [migration.version for migration in load_migrations()]
    assert versions == sorted(versions)
    assert versions[:MIGRATION_VERSION] == list(range(1, MIGRATION_VERSION + 1))
    assert MIGRATION.version == PREDECESSOR_VERSION + 1
    assert MIGRATION.name == MIGRATION_NAME


def test_the_ledger_records_this_exact_migration_text(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        recorded = applied_migrations(connection)
    finally:
        connection.close()
    assert recorded[MIGRATION_VERSION] == MIGRATION.checksum
    assert (
        hashlib.sha256(MIGRATION.sql.encode("utf-8")).hexdigest() == MIGRATION.checksum
    )


def test_schema_inventory_contains_expected_chat_objects(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        named = {
            kind: {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
                )
            }
            for kind in ("table", "index", "trigger")
        }
        assert set(TABLES) <= named["table"]
        assert INDEXES <= named["index"]
        assert TRIGGERS <= named["trigger"]
        assert len(TABLES) == 13
        assert len(INDEXES) == 14
        assert len(TRIGGERS) == 39
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


def test_no_new_object_is_named_for_forbidden_storage_concerns(
    migrated: Path,
) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        names = list(TABLES) + list(INDEXES)
        for table in TABLES:
            names.extend(
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            )
    finally:
        connection.close()

    offending = [
        (name, forbidden)
        for name in names
        for forbidden in FORBIDDEN_STORAGE_NAMES
        if forbidden in name.lower()
    ]
    assert offending == []


@pytest.mark.parametrize("table", TABLES)
def test_inserts_require_the_fenced_owner(owned: m1.Owned, table: str) -> None:
    with pytest.raises(sqlite3.DatabaseError, match="not authorized|unguarded INSERT"):
        insert(owned, table, row_for_table(table))


def test_seeded_chat_graph_populates_every_table_and_stays_clean(
    owned: m1.Owned,
) -> None:
    seed_every_table(owned)

    for table in TABLES:
        assert owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


@pytest.mark.parametrize("table", TABLES)
def test_deletes_are_refused_even_for_the_current_owner(
    owned: m1.Owned,
    table: str,
) -> None:
    seed_every_table(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="forbids DELETE"):
        owned.connection.execute(f"DELETE FROM {table}")


def test_committed_messages_are_immutable(owned: m1.Owned) -> None:
    seed_conversation_graph(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="immutable"):
        owned.connection.execute(
            f"UPDATE {MESSAGES} SET visibility = 'internal' "
            "WHERE workspace_id = ? AND message_id = ?",
            (WORKSPACE_ID, ROOT_MESSAGE_ID),
        )


def test_message_parent_must_be_in_same_conversation_and_not_future(
    owned: m1.Owned,
) -> None:
    with guarded(owned):
        insert(
            owned,
            CONVERSATIONS,
            conversation_row(conversation_id="conv-parent", latest_conversation_sequence=5),
        )
        insert(
            owned,
            MESSAGES,
            message_row("parent-root", 1, conversation_id="conv-parent"),
        )
        insert(
            owned,
            MESSAGES,
            message_row(
                "future-parent",
                5,
                conversation_id="conv-parent",
                parent_message_id="parent-root",
            ),
        )

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="future"):
        insert(
            owned,
            MESSAGES,
            message_row(
                "future-child",
                4,
                conversation_id="conv-parent",
                parent_message_id="future-parent",
            ),
        )

    with guarded(owned):
        insert(
            owned,
            CONVERSATIONS,
            conversation_row(conversation_id="conv-other", latest_conversation_sequence=1),
        )

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        insert(
            owned,
            MESSAGES,
            message_row(
                "cross-conv-child",
                1,
                conversation_id="conv-other",
                parent_message_id="parent-root",
            ),
        )


def test_message_parts_are_contiguous_and_unique_per_message(
    owned: m1.Owned,
) -> None:
    with guarded(owned):
        insert(owned, CONVERSATIONS, conversation_row(latest_conversation_sequence=1))
        insert(owned, MESSAGES, message_row("msg-parts", 1))

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert(owned, PARTS, part_row("part-gap", "msg-parts", 1))

    with guarded(owned):
        insert(owned, PARTS, part_row("part-ok", "msg-parts", 0))

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(owned, PARTS, part_row("part-ok", "msg-parts", 1))


def test_branch_head_events_are_contiguous_and_prior_head_backed(
    owned: m1.Owned,
) -> None:
    seed_conversation_graph(owned)

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert(
            owned,
            HEAD_EVENTS,
            head_event_row(
                3,
                previous_head_message_id=EDITED_MESSAGE_ID,
                new_head_message_id=TRIGGER_MESSAGE_ID,
                conversation_sequence=3,
            ),
        )

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="prior head"):
        insert(
            owned,
            HEAD_EVENTS,
            head_event_row(
                2,
                previous_head_message_id=ROOT_MESSAGE_ID,
                new_head_message_id=TRIGGER_MESSAGE_ID,
                conversation_sequence=3,
            ),
        )


def test_generation_job_transitions_are_terminal_and_result_consistent(
    owned: m1.Owned,
) -> None:
    seed_running_generation_job(owned)

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        owned.connection.execute(
            f"UPDATE {JOBS} SET state = 'succeeded', updated_at_us = ?, "
            "finished_at_us = ? WHERE workspace_id = ? AND generation_job_id = ?",
            (BASE_US + 120, BASE_US + 120, WORKSPACE_ID, GENERATION_JOB_ID),
        )

    with guarded(owned):
        insert(
            owned,
            EVENTS,
            generation_event_row(
                2,
                "chat.generation.started",
                generation_attempt_id=GENERATION_ATTEMPT_ID,
                provider_event_id="provider-event-started",
            ),
        )
        insert(
            owned,
            MESSAGES,
            message_row(
                ASSISTANT_MESSAGE_ID,
                4,
                role="assistant",
                parent_message_id=TRIGGER_MESSAGE_ID,
                generation_job_id=GENERATION_JOB_ID,
            ),
        )
        insert(
            owned,
            EVENTS,
            generation_event_row(
                3,
                "chat.generation.succeeded",
                generation_attempt_id=GENERATION_ATTEMPT_ID,
                result_message_id=ASSISTANT_MESSAGE_ID,
                provider_event_id="provider-event-succeeded",
            ),
        )
        owned.connection.execute(
            f"UPDATE {JOBS} SET state = 'succeeded', result_message_id = ?, "
            "last_event_sequence = 3, updated_at_us = ?, finished_at_us = ? "
            "WHERE workspace_id = ? AND generation_job_id = ?",
            (
                ASSISTANT_MESSAGE_ID,
                BASE_US + 210,
                BASE_US + 210,
                WORKSPACE_ID,
                GENERATION_JOB_ID,
            ),
        )

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="transition"):
        owned.connection.execute(
            f"UPDATE {JOBS} SET state = 'running', result_message_id = NULL, "
            "lease_owner = 'worker-1', lease_epoch = 2, "
            "lease_expires_at_us = ?, heartbeat_at_us = ?, "
            "updated_at_us = ?, finished_at_us = NULL "
            "WHERE workspace_id = ? AND generation_job_id = ?",
            (
                BASE_US + 900,
                BASE_US + 300,
                BASE_US + 300,
                WORKSPACE_ID,
                GENERATION_JOB_ID,
            ),
        )


def test_generation_attempts_are_contiguous_and_match_provider_invocation(
    owned: m1.Owned,
) -> None:
    seed_queued_generation_job(owned)

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert(
            owned,
            ATTEMPTS,
            generation_attempt_row(
                generation_attempt_id="generation-attempt-2",
                attempt_number=2,
                retry_of_attempt_id=GENERATION_ATTEMPT_ID,
                provider_invocation_id=None,
            ),
        )

    with guarded(owned):
        insert(
            owned,
            PROVIDER_INVOCATIONS,
            provider_invocation_row(generation_attempt_id="wrong-attempt"),
        )

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="same job"):
        insert(owned, ATTEMPTS, generation_attempt_row())


def test_generation_events_are_ordered_and_dedupe_provider_events(
    owned: m1.Owned,
) -> None:
    seed_running_generation_job(owned)

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert(
            owned,
            EVENTS,
            generation_event_row(
                3,
                "chat.generation.started",
                generation_attempt_id=GENERATION_ATTEMPT_ID,
                provider_event_id="provider-event-gap",
            ),
        )

    with guarded(owned):
        insert(
            owned,
            EVENTS,
            generation_event_row(
                2,
                "chat.generation.started",
                generation_attempt_id=GENERATION_ATTEMPT_ID,
                provider_event_id="provider-event-repeat",
            ),
        )

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(
            owned,
            EVENTS,
            generation_event_row(
                3,
                "chat.generation.started",
                generation_attempt_id=GENERATION_ATTEMPT_ID,
                provider_event_id="provider-event-repeat",
                event_id="generation-event-repeat",
                cursor="generation-cursor-repeat",
            ),
        )


def test_queued_submission_transitions_are_versioned_and_ordered(
    owned: m1.Owned,
) -> None:
    seed_conversation_graph(owned)

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="transition"):
        owned.connection.execute(
            f"UPDATE {QUEUED} SET state = 'submitted', version = 2, "
            "submitted_message_id = ?, submitted_generation_job_id = ?, "
            "updated_at_us = ? "
            "WHERE workspace_id = ? AND queued_submission_id = ?",
            (
                TRIGGER_MESSAGE_ID,
                GENERATION_JOB_ID,
                BASE_US + 90,
                WORKSPACE_ID,
                QUEUE_ID,
            ),
        )

    with guarded(owned):
        owned.connection.execute(
            f"UPDATE {QUEUED} SET state = 'claimed', version = 2, "
            "claimed_by = 'worker-1', claim_epoch = 1, "
            "claim_expires_at_us = ?, updated_at_us = ? "
            "WHERE workspace_id = ? AND queued_submission_id = ?",
            (BASE_US + 500, BASE_US + 91, WORKSPACE_ID, QUEUE_ID),
        )

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="transition"):
        owned.connection.execute(
            f"UPDATE {QUEUED} SET state = 'queued', version = 3, updated_at_us = ? "
            "WHERE workspace_id = ? AND queued_submission_id = ?",
            (BASE_US + 92, WORKSPACE_ID, QUEUE_ID),
        )


def test_outbox_cursor_domain_event_and_workspace_consistency(
    owned: m1.Owned,
) -> None:
    seed_every_table(owned)

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="contiguous"):
        insert(
            owned,
            OUTBOX,
            outbox_row(outbox_cursor=3, domain_event_id="domain-event-gap"),
        )

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(
            owned,
            OUTBOX,
            outbox_row(outbox_cursor=2, domain_event_id=OUTBOX_EVENT_ID),
        )

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(
            owned,
            OUTBOX,
            outbox_row(
                outbox_cursor=2,
                domain_event_id="domain-event-orphan-job",
                conversation_id=None,
                generation_job_id=GENERATION_JOB_ID,
            ),
        )


def test_view_draft_and_outbox_projections_are_update_guarded(
    owned: m1.Owned,
) -> None:
    seed_every_table(owned)

    with guarded(owned):
        owned.connection.execute(
            f"UPDATE {VIEW_STATES} SET version = 2, updated_at_us = ? "
            "WHERE workspace_id = ? AND conversation_id = ? "
            "AND actor_id = ? AND device_id = ?",
            (BASE_US + 310, WORKSPACE_ID, CONVERSATION_ID, ACTOR_ID, DEVICE_ID),
        )
        owned.connection.execute(
            f"UPDATE {DRAFTS} SET text_content = 'updated', version = 2, "
            "updated_at_us = ? WHERE workspace_id = ? AND draft_id = 'draft-1'",
            (BASE_US + 320, WORKSPACE_ID),
        )
        owned.connection.execute(
            f"UPDATE {OUTBOX} SET delivery_state = 'delivered', "
            "delivery_attempts = 1, delivered_at_us = ? "
            "WHERE workspace_id = ? AND outbox_cursor = 1",
            (BASE_US + 330, WORKSPACE_ID),
        )

    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="cannot reopen"):
        owned.connection.execute(
            f"UPDATE {OUTBOX} SET delivery_state = 'pending' "
            "WHERE workspace_id = ? AND outbox_cursor = 1",
            (WORKSPACE_ID,),
        )
