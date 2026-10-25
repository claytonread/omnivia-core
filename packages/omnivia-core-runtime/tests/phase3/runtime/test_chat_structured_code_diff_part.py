"""Acceptance for the allow-listed structured code-diff response path."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import omnivia_core_runtime.service.chat_compaction as compaction_module
import omnivia_core_runtime.service.chat_generation_executor as executor_module
import pytest
import test_chat_request_manifest as base
from omnivia_core_runtime.service.chat_part_registry import (
    CODE_DIFF_PART_TYPE,
    CODE_DIFF_RESPONSE_SCHEMA_REF,
)
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

from omnivia_core.chat_contract.v1 import to_canonical_json

m1 = base.m1


def _digest(value: Mapping[str, Any]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(to_canonical_json(dict(value)).encode("utf-8")).hexdigest()
    )


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
    directive = {"schemaRef": CODE_DIFF_RESPONSE_SCHEMA_REF}
    with base.writer(owned) as writer:
        writer.append_message_part(
            part_id="trigger-response-format",
            message_id=base.TRIGGER_MESSAGE_ID,
            conversation_id=base.CONVERSATION_ID,
            part_index=1,
            part_type="response.format",
            schema_version=1,
            visibility="internal",
            payload=directive,
            provenance="human",
            content_hash=_digest(directive),
            created_at_us=base.BASE_US + 102,
        )
        writer.append_branch_head_event(
            event_id="head-event-structured-code-diff-trigger",
            conversation_id=base.CONVERSATION_ID,
            branch_id=base.BRANCH_ID,
            head_version=2,
            previous_head_message_id=base.ROOT_MESSAGE_ID,
            new_head_message_id=base.TRIGGER_MESSAGE_ID,
            cause="user_message_appended",
            command_id="command-structured-code-diff-trigger",
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


CODE_DIFF = {
    "file": "src/app.py",
    "detail": "Raise the limit",
    "hunks": [
        {
            "header": "@@ -1,2 +1,2 @@",
            "lines": [
                {"change": "context", "text": "import os"},
                {"change": "remove", "text": "LIMIT = 1"},
                {"change": "add", "text": "LIMIT = 2"},
                {"change": "context", "text": ""},
            ],
        },
        {"lines": [{"change": "add", "text": "# end"}]},
    ],
}


def code_diff_stream(output: Mapping[str, Any] = CODE_DIFF) -> Any:
    def script(request: Any) -> Iterator[dict[str, Any]]:
        yield wire(request, 0, "stream-start")
        yield wire(
            request,
            1,
            "structured-output",
            schemaRef=CODE_DIFF_RESPONSE_SCHEMA_REF,
            output=dict(output),
        )
        yield wire(request, 2, "finish", finishReason="stop")

    return script


def _parts(holder: m1.Owned) -> tuple[chat.MessagePart, ...]:
    message = assistant_messages(holder)[0]
    return chat.read_message_parts(
        holder.connection,
        workspace_id=WORKSPACE_ID,
        message_id=message.message_id,
    )


def _outputs(holder: m1.Owned) -> tuple[chat.GenerationStructuredOutput, ...]:
    message = assistant_messages(holder)[0]
    assert message.generation_job_id is not None
    return chat.read_generation_structured_outputs(
        holder.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=message.generation_job_id,
    )


def test_structured_request_is_durable_and_materialises_one_code_diff(
    seeded: m1.Owned,
) -> None:
    provider = Scripted(code_diff_stream())
    run(seeded, provider)

    assert status(seeded).state == "succeeded"
    assert provider.requests[0].response_format == {
        "kind": "structured",
        "schemaRef": CODE_DIFF_RESPONSE_SCHEMA_REF,
    }
    outputs = _outputs(seeded)
    assert len(outputs) == 1
    assert outputs[0].schema_ref == CODE_DIFF_RESPONSE_SCHEMA_REF
    assert dict(outputs[0].output) == CODE_DIFF
    parts = _parts(seeded)
    assert [part.part_type for part in parts] == [CODE_DIFF_PART_TYPE]
    assert dict(parts[0].payload) == CODE_DIFF


def test_provider_null_optionals_are_removed_before_code_diff_persistence(
    seeded: m1.Owned,
) -> None:
    output = {
        "file": "src/app.py",
        "detail": None,
        "hunks": [
            {"header": None, "lines": [{"change": "add", "text": "x"}]},
            {"header": "@@ -9 +9 @@", "lines": [{"change": "remove", "text": "y"}]},
        ],
    }
    run(seeded, Scripted(code_diff_stream(output)))

    assert status(seeded).state == "succeeded"
    expected = {
        "file": "src/app.py",
        "hunks": [
            {"lines": [{"change": "add", "text": "x"}]},
            {"header": "@@ -9 +9 @@", "lines": [{"change": "remove", "text": "y"}]},
        ],
    }
    assert dict(_outputs(seeded)[0].output) == expected
    assert dict(_parts(seeded)[0].payload) == expected


def test_crash_replay_reuses_the_same_code_diff_output_row(seeded: m1.Owned) -> None:
    def dies(*args: Any, **kwargs: Any) -> Any:
        raise Crash

    with pytest.raises(Crash), pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            executor_module.ChatGenerationExecutor,
            "_materialize_assistant",
            dies,
        )
        run(seeded, Scripted(code_diff_stream()))

    before = [
        tuple(row)
        for row in seeded.connection.execute(
            "SELECT * FROM omnivia_chat_generation_structured_outputs ORDER BY output_ordinal"
        )
    ]
    assert len(before) == 1
    assert status(seeded).state == "running"

    run(seeded, Scripted(code_diff_stream()))
    after = [
        tuple(row)
        for row in seeded.connection.execute(
            "SELECT * FROM omnivia_chat_generation_structured_outputs ORDER BY output_ordinal"
        )
    ]
    assert after == before
    assert status(seeded).state == "succeeded"


@pytest.mark.parametrize(
    "output",
    (
        {**CODE_DIFF, "file": ""},
        {**CODE_DIFF, "file": "é" * 513},
        {**CODE_DIFF, "detail": "é" * 2_049},
        {**CODE_DIFF, "hunks": []},
        {**CODE_DIFF, "hunks": [{"lines": [{"change": "add", "text": "x"}]}] * 65},
        {**CODE_DIFF, "hunks": [{"lines": []}]},
        {**CODE_DIFF, "hunks": [{"lines": [{"change": "add", "text": "x"}] * 501}]},
        {**CODE_DIFF, "hunks": [{"lines": [{"change": "add", "text": "x"}] * 401}] * 5},
        {**CODE_DIFF, "hunks": [{"lines": [{"change": "modify", "text": "x"}]}]},
        {**CODE_DIFF, "hunks": [{"lines": [{"change": "add", "text": None}]}]},
        {**CODE_DIFF, "hunks": [{"lines": [{"change": "add", "text": "é" * 2_049}]}]},
        {**CODE_DIFF, "hunks": [{"header": "é" * 513, "lines": [{"change": "add", "text": "x"}]}]},
        {**CODE_DIFF, "hunks": [{"start": 1, "lines": [{"change": "add", "text": "x"}]}]},
        {**CODE_DIFF, "hunks": [{"lines": [{"change": "add", "text": "x", "number": 1}]}]},
        {**CODE_DIFF, "language": "python"},
    ),
)
def test_malformed_code_diff_fails_without_persisting_or_committing(
    seeded: m1.Owned,
    output: Mapping[str, Any],
) -> None:
    run(seeded, Scripted(code_diff_stream(output)))

    assert status(seeded).state == "failed"
    count = seeded.connection.execute(
        "SELECT COUNT(*) FROM omnivia_chat_generation_structured_outputs"
    ).fetchone()
    assert count is not None and count[0] == 0
    assert assistant_messages(seeded) == []


def test_code_diff_is_model_visible_in_ordinary_and_compacted_projection(
    seeded: m1.Owned,
) -> None:
    run(seeded, Scripted(code_diff_stream()))
    message = assistant_messages(seeded)[0]
    expected = {
        "kind": "text",
        "text": "Code diff:\n" + to_canonical_json(CODE_DIFF),
    }

    context = executor_module._provider_context(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=base.CONVERSATION_ID,
        branch_id=base.BRANCH_ID,
        trigger_message_id=message.message_id,
        blobs_root=None,
    )
    assert context.messages[-1] == {"role": "assistant", "parts": [expected]}
    assert compaction_module._message_text_parts(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        message_id=message.message_id,
    ) == (expected,)
