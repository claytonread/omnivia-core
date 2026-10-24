"""Acceptance for the allow-listed structured diagram-flow response path."""

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
    DIAGRAM_FLOW_PART_TYPE,
    DIAGRAM_FLOW_RESPONSE_SCHEMA_REF,
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
    return "sha256:" + hashlib.sha256(
        to_canonical_json(dict(value)).encode("utf-8")
    ).hexdigest()


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
    directive = {"schemaRef": DIAGRAM_FLOW_RESPONSE_SCHEMA_REF}
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
            event_id="head-event-structured-diagram-trigger",
            conversation_id=base.CONVERSATION_ID,
            branch_id=base.BRANCH_ID,
            head_version=2,
            previous_head_message_id=base.ROOT_MESSAGE_ID,
            new_head_message_id=base.TRIGGER_MESSAGE_ID,
            cause="user_message_appended",
            command_id="command-structured-diagram-trigger",
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


DIAGRAM = {
    "title": "Release flow",
    "nodes": [
        {"id": "plan", "label": "Plan", "column": 0, "row": 0, "state": "done"},
        {"id": "build", "label": "Build", "column": 1, "row": 0, "state": "active"},
        {"id": "ship", "label": "Ship", "column": 2, "row": 0, "state": "pending"},
    ],
    "edges": [
        {"from": "plan", "to": "build"},
        {"from": "build", "to": "ship"},
    ],
}


def diagram_stream(output: Mapping[str, Any] = DIAGRAM) -> Any:
    def script(request: Any) -> Iterator[dict[str, Any]]:
        yield wire(request, 0, "stream-start")
        yield wire(
            request,
            1,
            "structured-output",
            schemaRef=DIAGRAM_FLOW_RESPONSE_SCHEMA_REF,
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


def test_structured_request_is_durable_and_materialises_one_diagram_flow(
    seeded: m1.Owned,
) -> None:
    provider = Scripted(diagram_stream())
    run(seeded, provider)

    assert status(seeded).state == "succeeded"
    assert len(provider.requests) == 1
    assert provider.requests[0].response_format == {
        "kind": "structured",
        "schemaRef": DIAGRAM_FLOW_RESPONSE_SCHEMA_REF,
    }
    outputs = _outputs(seeded)
    assert len(outputs) == 1
    assert outputs[0].schema_ref == DIAGRAM_FLOW_RESPONSE_SCHEMA_REF
    assert dict(outputs[0].output) == DIAGRAM
    parts = _parts(seeded)
    assert [part.part_type for part in parts] == [DIAGRAM_FLOW_PART_TYPE]
    assert dict(parts[0].payload) == DIAGRAM


def test_crash_replay_reuses_the_same_diagram_output_row(seeded: m1.Owned) -> None:
    def dies(*args: Any, **kwargs: Any) -> Any:
        raise Crash

    with pytest.raises(Crash), pytest.MonkeyPatch.context() as patch:
        patch.setattr(executor_module.ChatGenerationExecutor, "_materialize_assistant", dies)
        run(seeded, Scripted(diagram_stream()))

    before = [
        tuple(row)
        for row in seeded.connection.execute(
            "SELECT * FROM omnivia_chat_generation_structured_outputs ORDER BY output_ordinal"
        )
    ]
    assert len(before) == 1
    assert status(seeded).state == "running"

    run(seeded, Scripted(diagram_stream()))
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
        {**DIAGRAM, "title": ""},
        {**DIAGRAM, "nodes": []},
        {**DIAGRAM, "nodes": [{**DIAGRAM["nodes"][0], "id": "bad/id"}]},
        {**DIAGRAM, "nodes": [DIAGRAM["nodes"][0], {**DIAGRAM["nodes"][0]}]},
        {**DIAGRAM, "nodes": [{**DIAGRAM["nodes"][0], "column": 16}]},
        {**DIAGRAM, "nodes": [{**DIAGRAM["nodes"][0], "state": "running"}]},
        {**DIAGRAM, "edges": [{"from": "plan", "to": "missing"}]},
        {**DIAGRAM, "edges": [{"from": "plan", "to": "plan"}]},
        {**DIAGRAM, "html": "<script>unsafe</script>"},
    ),
)
def test_malformed_diagram_fails_without_persisting_or_committing(
    seeded: m1.Owned,
    output: Mapping[str, Any],
) -> None:
    run(seeded, Scripted(diagram_stream(output)))

    assert status(seeded).state == "failed"
    count = seeded.connection.execute(
        "SELECT COUNT(*) FROM omnivia_chat_generation_structured_outputs"
    ).fetchone()
    assert count is not None and count[0] == 0
    assert assistant_messages(seeded) == []


def test_diagram_is_model_visible_in_ordinary_and_compacted_projection(
    seeded: m1.Owned,
) -> None:
    run(seeded, Scripted(diagram_stream()))
    message = assistant_messages(seeded)[0]
    expected = {
        "kind": "text",
        "text": "Diagram:\n" + to_canonical_json(DIAGRAM),
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
