"""Acceptance for the allow-listed structured file-tree response path."""

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
    FILE_TREE_PART_TYPE,
    FILE_TREE_RESPONSE_SCHEMA_REF,
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
    directive = {"schemaRef": FILE_TREE_RESPONSE_SCHEMA_REF}
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
            event_id="head-event-structured-file-tree-trigger",
            conversation_id=base.CONVERSATION_ID,
            branch_id=base.BRANCH_ID,
            head_version=2,
            previous_head_message_id=base.ROOT_MESSAGE_ID,
            new_head_message_id=base.TRIGGER_MESSAGE_ID,
            cause="user_message_appended",
            command_id="command-structured-file-tree-trigger",
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


FILE_TREE = {
    "title": "Changed files",
    "detail": "Two files touched",
    "items": [
        {"path": "src", "name": "src", "depth": 0, "kind": "folder"},
        {
            "path": "src/app.py",
            "name": "app.py",
            "depth": 1,
            "kind": "file",
            "additions": 3,
            "deletions": 1,
        },
        {"path": "README.md", "name": "README.md", "depth": 0, "kind": "file"},
    ],
}


def file_tree_stream(output: Mapping[str, Any] = FILE_TREE) -> Any:
    def script(request: Any) -> Iterator[dict[str, Any]]:
        yield wire(request, 0, "stream-start")
        yield wire(
            request,
            1,
            "structured-output",
            schemaRef=FILE_TREE_RESPONSE_SCHEMA_REF,
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


def test_structured_request_is_durable_and_materialises_one_file_tree(
    seeded: m1.Owned,
) -> None:
    provider = Scripted(file_tree_stream())
    run(seeded, provider)

    assert status(seeded).state == "succeeded"
    assert provider.requests[0].response_format == {
        "kind": "structured",
        "schemaRef": FILE_TREE_RESPONSE_SCHEMA_REF,
    }
    outputs = _outputs(seeded)
    assert len(outputs) == 1
    assert outputs[0].schema_ref == FILE_TREE_RESPONSE_SCHEMA_REF
    assert dict(outputs[0].output) == FILE_TREE
    parts = _parts(seeded)
    assert [part.part_type for part in parts] == [FILE_TREE_PART_TYPE]
    assert dict(parts[0].payload) == FILE_TREE


def test_provider_null_detail_and_counts_are_removed_before_file_tree_persistence(
    seeded: m1.Owned,
) -> None:
    output = {
        "title": "Changed files",
        "detail": None,
        "items": [
            {
                "path": "src",
                "name": "src",
                "depth": 0,
                "kind": "folder",
                "additions": None,
                "deletions": None,
            },
            {
                "path": "src/app.py",
                "name": "app.py",
                "depth": 1,
                "kind": "file",
                "additions": 2,
                "deletions": None,
            },
        ],
    }
    run(seeded, Scripted(file_tree_stream(output)))

    assert status(seeded).state == "succeeded"
    expected = {
        "title": "Changed files",
        "items": [
            {"path": "src", "name": "src", "depth": 0, "kind": "folder"},
            {"path": "src/app.py", "name": "app.py", "depth": 1, "kind": "file", "additions": 2},
        ],
    }
    assert dict(_outputs(seeded)[0].output) == expected
    assert dict(_parts(seeded)[0].payload) == expected


def test_crash_replay_reuses_the_same_file_tree_output_row(seeded: m1.Owned) -> None:
    def dies(*args: Any, **kwargs: Any) -> Any:
        raise Crash

    with pytest.raises(Crash), pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            executor_module.ChatGenerationExecutor,
            "_materialize_assistant",
            dies,
        )
        run(seeded, Scripted(file_tree_stream()))

    before = [
        tuple(row)
        for row in seeded.connection.execute(
            "SELECT * FROM omnivia_chat_generation_structured_outputs ORDER BY output_ordinal"
        )
    ]
    assert len(before) == 1
    assert status(seeded).state == "running"

    run(seeded, Scripted(file_tree_stream()))
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
        {**FILE_TREE, "title": ""},
        {**FILE_TREE, "title": "é" * 129},
        {**FILE_TREE, "detail": 7},
        {**FILE_TREE, "items": []},
        {**FILE_TREE, "items": [{"path": "a", "name": "a", "depth": 0, "kind": "file"}] * 257},
        {**FILE_TREE, "items": [{"path": "~a", "name": "~a", "depth": 0, "kind": "file"}]},
        {**FILE_TREE, "items": [{"path": "a\\b", "name": "a\\b", "depth": 0, "kind": "file"}]},
        {**FILE_TREE, "items": [{"path": "C:a", "name": "C:a", "depth": 0, "kind": "file"}]},
        {**FILE_TREE, "items": [{"path": "a/../b", "name": "b", "depth": 2, "kind": "file"}]},
        {**FILE_TREE, "items": [{"path": "a", "name": "b", "depth": 0, "kind": "file"}]},
        {**FILE_TREE, "items": [{"path": "a", "name": "a", "depth": 1, "kind": "file"}]},
        {**FILE_TREE, "items": [{"path": "a", "name": "a", "depth": 0, "kind": "dir"}]},
        {
            **FILE_TREE,
            "items": [{"path": "a", "name": "a", "depth": 0, "kind": "folder", "additions": 1}],
        },
        {
            **FILE_TREE,
            "items": [{"path": "a", "name": "a", "depth": 0, "kind": "file", "additions": True}],
        },
        {
            **FILE_TREE,
            "items": [
                {"path": "a", "name": "a", "depth": 0, "kind": "file"},
                {"path": "a", "name": "a", "depth": 0, "kind": "file"},
            ],
        },
        {
            **FILE_TREE,
            "items": [
                {"path": "src/app.py", "name": "app.py", "depth": 1, "kind": "file"},
                {"path": "src", "name": "src", "depth": 0, "kind": "folder"},
            ],
        },
        {
            **FILE_TREE,
            "items": [{"path": "a", "name": "a", "depth": 0, "kind": "file", "status": "new"}],
        },
        {**FILE_TREE, "root": "/"},
    ),
)
def test_malformed_file_tree_fails_without_persisting_or_committing(
    seeded: m1.Owned,
    output: Mapping[str, Any],
) -> None:
    run(seeded, Scripted(file_tree_stream(output)))

    assert status(seeded).state == "failed"
    count = seeded.connection.execute(
        "SELECT COUNT(*) FROM omnivia_chat_generation_structured_outputs"
    ).fetchone()
    assert count is not None and count[0] == 0
    assert assistant_messages(seeded) == []


def test_file_tree_is_model_visible_in_ordinary_and_compacted_projection(
    seeded: m1.Owned,
) -> None:
    run(seeded, Scripted(file_tree_stream()))
    message = assistant_messages(seeded)[0]
    expected = {
        "kind": "text",
        "text": "File tree:\n" + to_canonical_json(FILE_TREE),
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
