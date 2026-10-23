"""Acceptance for explicit, display-safe provider reasoning summaries.

Raw reasoning remains absent. Only the separate `reasoning-summary` F2a event is
durable, and it becomes one `reasoning.summary` Message part ahead of answer text.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import omnivia_core_runtime.service.chat_generation_executor as executor_module
import pytest
import test_chat_request_manifest as base
from omnivia_core_runtime.service.chat_part_registry import REASONING_SUMMARY_PART_TYPE
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from test_chat_direction_continuation import (
    ACTOR_ID,
    Crash,
    Scripted,
    assistant_messages,
    run,
    status,
    wire,
)
from test_chat_request_manifest import WORKSPACE_ID

m1 = base.m1


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


@pytest.fixture
def seeded(owned: m1.Owned) -> m1.Owned:
    base.seed_conversation_and_queue(owned)
    with base.writer(owned) as writer:
        writer.append_branch_head_event(
            event_id="head-event-reasoning-summary-trigger",
            conversation_id=base.CONVERSATION_ID,
            branch_id=base.BRANCH_ID,
            head_version=2,
            previous_head_message_id=base.ROOT_MESSAGE_ID,
            new_head_message_id=base.TRIGGER_MESSAGE_ID,
            cause="user_message_appended",
            command_id="command-reasoning-summary-trigger",
            graph_revision=1,
            conversation_sequence=2,
            actor_id=ACTOR_ID,
            occurred_at_us=base.BASE_US + 20,
            schema_version=1,
        )
        writer.update_branch_head(
            branch_id=base.BRANCH_ID,
            expected_head_version=1,
            head_version=2,
            current_head_message_id=base.TRIGGER_MESSAGE_ID,
            state="open",
        )
    return owned


SUMMARY = "Compared the available routes and selected the direct integration."
RAW_REASONING = "private chain-of-thought must never be stored"


def summary_stream(*, summary: str = SUMMARY, raw_content: str | None = None) -> Any:
    def script(request: Any) -> Iterator[dict[str, Any]]:
        yield wire(request, 0, "stream-start")
        event = wire(
            request,
            1,
            "reasoning-summary",
            partId="reasoning-part-1",
            stepId="step-1",
            summary=summary,
        )
        if raw_content is not None:
            # Deliberately bypass schema validation to prove the Core producer
            # reads only the explicit summary field and drops an illicit raw one.
            event["content"] = raw_content
        yield event
        yield wire(
            request,
            2,
            "text-delta",
            partId="answer-part-1",
            stepId="step-1",
            delta="Direct integration selected.",
        )
        yield wire(request, 3, "finish", finishReason="stop")

    return script


def message_parts(holder: m1.Owned) -> tuple[chat.MessagePart, ...]:
    message = assistant_messages(holder)[0]
    return chat.read_message_parts(
        holder.connection,
        workspace_id=WORKSPACE_ID,
        message_id=message.message_id,
    )


def durable_summaries(holder: m1.Owned) -> tuple[chat.GenerationReasoningSummary, ...]:
    message = assistant_messages(holder)[0]
    assert message.generation_job_id is not None
    return chat.read_generation_reasoning_summaries(
        holder.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=message.generation_job_id,
    )


def test_explicit_summary_is_durable_and_precedes_answer_text(seeded: m1.Owned) -> None:
    run(seeded, Scripted(summary_stream(raw_content=RAW_REASONING)))

    assert status(seeded).state == "succeeded"
    summaries = durable_summaries(seeded)
    assert [item.summary_text for item in summaries] == [SUMMARY]
    assert summaries[0].provider_part_id == "reasoning-part-1"
    assert summaries[0].provider_step_id == "step-1"

    parts = message_parts(seeded)
    assert [part.part_type for part in parts] == [REASONING_SUMMARY_PART_TYPE, "text"]
    assert dict(parts[0].payload) == {"title": "Reasoning summary", "detail": SUMMARY}
    published = json.dumps([dict(part.payload) for part in parts])
    stored = json.dumps([item.summary_text for item in summaries])
    assert RAW_REASONING not in published
    assert RAW_REASONING not in stored


def test_crash_replay_reuses_the_same_summary_row(seeded: m1.Owned) -> None:
    def dies(*args: Any, **kwargs: Any) -> Any:
        raise Crash

    with pytest.raises(Crash), pytest.MonkeyPatch.context() as patch:
        patch.setattr(executor_module.ChatGenerationExecutor, "_materialize_assistant", dies)
        run(seeded, Scripted(summary_stream()))

    before = [
        tuple(row)
        for row in seeded.connection.execute(
            "SELECT * FROM omnivia_chat_generation_reasoning_summaries ORDER BY summary_ordinal"
        )
    ]
    assert len(before) == 1
    assert status(seeded).state == "running"

    run(seeded, Scripted(summary_stream()))

    after = [
        tuple(row)
        for row in seeded.connection.execute(
            "SELECT * FROM omnivia_chat_generation_reasoning_summaries ORDER BY summary_ordinal"
        )
    ]
    assert after == before
    assert status(seeded).state == "succeeded"
    assert [part.part_type for part in message_parts(seeded)] == [
        REASONING_SUMMARY_PART_TYPE,
        "text",
    ]


def test_malformed_summary_fails_without_persisting_reasoning(seeded: m1.Owned) -> None:
    run(seeded, Scripted(summary_stream(summary="")))

    assert status(seeded).state == "failed"
    count = seeded.connection.execute(
        "SELECT COUNT(*) FROM omnivia_chat_generation_reasoning_summaries"
    ).fetchone()
    assert count is not None and count[0] == 0
    assert assistant_messages(seeded) == []


def test_summary_ledger_is_guarded_and_append_only(seeded: m1.Owned) -> None:
    run(seeded, Scripted(summary_stream()))

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        seeded.connection.execute(
            "INSERT INTO omnivia_chat_generation_reasoning_summaries "
            "SELECT workspace_id, conversation_id, generation_job_id, generation_attempt_id, "
            "summary_ordinal + 1, NULL, provider_part_id, provider_step_id, summary_text, "
            "content_hash, schema_version, occurred_at_us "
            "FROM omnivia_chat_generation_reasoning_summaries"
        )
    with base.writer(seeded) as writer:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            writer.connection.execute(
                "UPDATE omnivia_chat_generation_reasoning_summaries SET summary_text = 'changed'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="forbids DELETE"):
            writer.connection.execute("DELETE FROM omnivia_chat_generation_reasoning_summaries")
