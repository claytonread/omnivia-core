"""T-0663 acceptance for replay-safe Chat compaction and model-input derivation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import test_chat_request_manifest as manifest
from omnivia_core_runtime.service.chat_compaction import (
    ChatCompactionConflict,
    UnsafeCompactionBoundary,
    derive_model_visible_context,
    record_completed_compaction,
)
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.migrations import (
    load_migrations,
    materialise_phase0_baseline,
)

COMPACTION_ID = "compaction-t0663-1"
PROJECTION_ID = "projection-t0663-1"
POLICY_VERSION = "chat-compaction-policy-v1"
SUMMARIZER_VERSION = "chat-summarizer-v1"


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[manifest.m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    manifest.m1.bootstrap_and_migrate(path, workspace_id=manifest.WORKSPACE_ID)
    holder = manifest.m1.take_ownership(path)
    yield holder
    holder.connection.close()


def _record_prefix_compaction(
    holder: manifest.m1.Owned,
    *,
    compaction_id: str = COMPACTION_ID,
    projection_id: str = PROJECTION_ID,
    source_start_sequence: int = 1,
    source_end_sequence: int = 1,
    summary_text: str = "Earlier safe context summary.",
) -> Any:
    return record_completed_compaction(
        holder.connection,
        holder.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=holder.generation,
        conversation_id=manifest.CONVERSATION_ID,
        branch_id=manifest.BRANCH_ID,
        compaction_id=compaction_id,
        projection_id=projection_id,
        source_start_sequence=source_start_sequence,
        source_end_sequence=source_end_sequence,
        summary_message={"kind": "summary", "text": summary_text},
        model_input=[
            {
                "role": "system",
                "parts": [{"kind": "text", "text": summary_text}],
            }
        ],
        policy_version=POLICY_VERSION,
        summarizer_version=SUMMARIZER_VERSION,
        now_us=manifest.BASE_US + 80,
    )


def _extend_lineage_with_tool_pair(holder: manifest.m1.Owned) -> None:
    with manifest.writer(holder) as writer:
        writer.update_conversation(
            conversation_id=manifest.CONVERSATION_ID,
            expected_graph_revision=1,
            graph_revision=1,
            latest_conversation_sequence=5,
            state="active",
            updated_at_us=manifest.BASE_US + 60,
        )
        writer.append_message(
            message_id="msg-t0663-tool-proposal",
            conversation_id=manifest.CONVERSATION_ID,
            role="assistant",
            author_type="provider",
            author_id="provider-chat",
            parent_message_id=manifest.TRIGGER_MESSAGE_ID,
            conversation_sequence=4,
            schema_version=1,
            content_hash=manifest.digest("a"),
            completion_status="complete",
            visibility="standard",
            created_on_branch_id=manifest.BRANCH_ID,
            generation_job_id=manifest.JOB_ID,
            created_at_us=manifest.BASE_US + 61,
            committed_at_us=manifest.BASE_US + 61,
        )
        writer.append_message_part(
            part_id="part-t0663-tool-proposal",
            message_id="msg-t0663-tool-proposal",
            conversation_id=manifest.CONVERSATION_ID,
            part_index=0,
            part_type="tool_proposal",
            schema_version=1,
            visibility="standard",
            payload={"toolCallId": "toolcall-t0663-1"},
            provenance="ai",
            content_hash=manifest.digest("b"),
            created_at_us=manifest.BASE_US + 62,
        )
        writer.append_message(
            message_id="msg-t0663-tool-result",
            conversation_id=manifest.CONVERSATION_ID,
            role="tool",
            author_type="service",
            author_id="actor-core-chat",
            parent_message_id="msg-t0663-tool-proposal",
            conversation_sequence=5,
            schema_version=1,
            content_hash=manifest.digest("c"),
            completion_status="complete",
            visibility="standard",
            created_on_branch_id=manifest.BRANCH_ID,
            created_at_us=manifest.BASE_US + 63,
            committed_at_us=manifest.BASE_US + 63,
        )
        writer.append_message_part(
            part_id="part-t0663-tool-result",
            message_id="msg-t0663-tool-result",
            conversation_id=manifest.CONVERSATION_ID,
            part_index=0,
            part_type="tool_result",
            schema_version=1,
            visibility="standard",
            payload={"toolCallId": "toolcall-t0663-1", "status": "succeeded"},
            provenance="system",
            content_hash=manifest.digest("d"),
            created_at_us=manifest.BASE_US + 64,
        )


def test_0033_schema_inventory_contains_expected_objects(
    owned: manifest.m1.Owned,
) -> None:
    migration = [item for item in load_migrations() if item.version == 33]
    assert len(migration) == 1
    assert migration[0].name == "0033_chat_compaction_waits_agent_runs.sql"

    objects = {
        (row[0], row[1])
        for row in owned.connection.execute(
            "SELECT name, type FROM sqlite_master WHERE name LIKE 'omnivia_%'"
        )
    }

    assert {
        ("omnivia_chat_compaction_events", "table"),
        ("omnivia_chat_model_input_projections", "table"),
        ("omnivia_chat_wait_interactions", "table"),
        ("omnivia_chat_agent_runs", "table"),
        ("omnivia_chat_agent_run_mailbox", "table"),
    }.issubset(objects)
    assert {
        ("omnivia_idx_chat_compaction_events_conversation", "index"),
        ("omnivia_idx_chat_model_input_projections_branch", "index"),
        ("omnivia_idx_chat_wait_interactions_attention", "index"),
        ("omnivia_idx_chat_agent_runs_conversation", "index"),
        ("omnivia_idx_chat_agent_run_mailbox_run", "index"),
    }.issubset(objects)
    assert {
        ("omnivia_guard_chat_compaction_events_insert", "trigger"),
        ("omnivia_guard_chat_compaction_events_update", "trigger"),
        ("omnivia_guard_chat_compaction_events_delete", "trigger"),
        ("omnivia_guard_chat_model_input_projections_insert", "trigger"),
        ("omnivia_guard_chat_model_input_projections_update", "trigger"),
        ("omnivia_guard_chat_model_input_projections_delete", "trigger"),
        ("omnivia_guard_chat_wait_interactions_insert", "trigger"),
        ("omnivia_guard_chat_wait_interactions_update", "trigger"),
        ("omnivia_guard_chat_wait_interactions_delete", "trigger"),
        ("omnivia_guard_chat_agent_runs_insert", "trigger"),
        ("omnivia_guard_chat_agent_runs_update", "trigger"),
        ("omnivia_guard_chat_agent_runs_delete", "trigger"),
        ("omnivia_guard_chat_agent_run_mailbox_insert", "trigger"),
        ("omnivia_guard_chat_agent_run_mailbox_update", "trigger"),
        ("omnivia_guard_chat_agent_run_mailbox_delete", "trigger"),
    }.issubset(objects)
    assert owned.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert owned.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_completed_compaction_records_brackets_and_preserves_raw_history(
    owned: manifest.m1.Owned,
) -> None:
    manifest.seed_conversation_and_queue(owned)
    before = tuple(
        (message.message_id, message.content_hash)
        for message in chat.read_messages_by_conversation_sequence(
            owned.connection,
            workspace_id=manifest.WORKSPACE_ID,
            conversation_id=manifest.CONVERSATION_ID,
        )
    )

    compacted = _record_prefix_compaction(owned)

    assert compacted.messages == (
        {
            "role": "system",
            "parts": [
                {"kind": "text", "text": "Earlier safe context summary."}
            ],
        },
    )
    assert compacted.manifest_message_refs[0]["kind"] == "compaction"
    events = owned.connection.execute(
        "SELECT event_sequence, event_type, payload_json "
        "FROM omnivia_chat_compaction_events "
        "WHERE workspace_id = ? AND compaction_id = ? ORDER BY event_sequence",
        (manifest.WORKSPACE_ID, COMPACTION_ID),
    ).fetchall()
    assert [(row[0], row[1]) for row in events] == [
        (1, "started"),
        (2, "summary"),
        (3, "completed"),
    ]
    assert json.loads(events[0][2]) == {"sourceRange": {"end": 1, "start": 1}}

    after = tuple(
        (message.message_id, message.content_hash)
        for message in chat.read_messages_by_conversation_sequence(
            owned.connection,
            workspace_id=manifest.WORKSPACE_ID,
            conversation_id=manifest.CONVERSATION_ID,
        )
    )
    assert after == before

    derived = derive_model_visible_context(
        owned.connection,
        workspace_id=manifest.WORKSPACE_ID,
        conversation_id=manifest.CONVERSATION_ID,
        branch_id=manifest.BRANCH_ID,
        trigger_message_id=manifest.TRIGGER_MESSAGE_ID,
    )
    assert derived is not None
    assert [item["role"] for item in derived.messages] == ["system", "user"]
    assert derived.messages[1]["parts"] == (
        {"kind": "text", "text": "trigger text must not be copied"},
    )
    assert [item.get("kind") for item in derived.manifest_message_refs] == [
        "compaction",
        None,
    ]
    assert derived.manifest_message_refs[1]["messageId"] == manifest.TRIGGER_MESSAGE_ID
    serialized_refs = json.dumps(derived.manifest_message_refs, sort_keys=True)
    assert "root text must not be copied" not in serialized_refs


def test_compaction_replay_is_idempotent_but_conflict_fails_closed(
    owned: manifest.m1.Owned,
) -> None:
    manifest.seed_conversation_and_queue(owned)

    first = _record_prefix_compaction(owned)
    second = _record_prefix_compaction(owned)

    assert second == first
    with pytest.raises(ChatCompactionConflict):
        _record_prefix_compaction(owned, summary_text="Changed safe summary.")


def test_unsafe_tool_proposal_result_split_is_refused(
    owned: manifest.m1.Owned,
) -> None:
    manifest.seed_running_attempt(owned)
    _extend_lineage_with_tool_pair(owned)

    with pytest.raises(UnsafeCompactionBoundary):
        _record_prefix_compaction(
            owned,
            source_start_sequence=4,
            source_end_sequence=4,
        )

    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_chat_model_input_projections "
            "WHERE workspace_id = ?",
            (manifest.WORKSPACE_ID,),
        ).fetchone()[0]
        == 0
    )


def test_executor_manifest_uses_completed_compaction_projection(
    owned: manifest.m1.Owned,
) -> None:
    manifest.seed_conversation_and_queue(owned)
    _record_prefix_compaction(owned)
    calls: list[Any] = []

    def provider(request: Any) -> Iterator[Mapping[str, Any]]:
        calls.append(request)
        assert [item["role"] for item in request.messages] == ["system", "user"]
        assert request.messages[0]["parts"] == [
            {"kind": "text", "text": "Earlier safe context summary."}
        ]
        manifest_row = chat.read_request_manifest(
            owned.connection,
            workspace_id=manifest.WORKSPACE_ID,
            generation_attempt_id=request.attempt_id,
        )
        assert manifest_row is not None
        refs = manifest_row.manifest_body["modelVisible"]["messages"]
        assert refs[0]["kind"] == "compaction"
        assert refs[0]["projectionId"] == PROJECTION_ID
        assert refs[1]["messageId"] == manifest.TRIGGER_MESSAGE_ID
        manifest_text = json.dumps(dict(manifest_row.manifest_body), sort_keys=True)
        assert "Earlier safe context summary." not in manifest_text
        assert "root text must not be copied" not in manifest_text
        yield from manifest._stream(request)

    manifest._executor(owned, provider).execute_submission(
        queued_submission_id=manifest.QUEUE_ID,
        generation_job_id=manifest.JOB_ID,
        trigger_message_id=manifest.TRIGGER_MESSAGE_ID,
    )

    assert len(calls) == 1


def test_compaction_tables_reject_unguarded_mutation(
    owned: manifest.m1.Owned,
) -> None:
    manifest.seed_conversation_and_queue(owned)
    _record_prefix_compaction(owned)

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        owned.connection.execute(
            "UPDATE omnivia_chat_model_input_projections "
            "SET state = 'completed' WHERE workspace_id = ?",
            (manifest.WORKSPACE_ID,),
        )
