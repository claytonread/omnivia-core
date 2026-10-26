"""Plan views: the provider's own reported plan, committed as the one view asked for.

A `plan.request` on the trigger turn adds fixed Core copy asking the provider to
plan, and names one view (`agent-plan`, `todo-list`, `job-progress`). The
provider's reported plan arrives as a `codex.plan` fact; Core commits only the
requested view of the LAST plan, never a `tool.call` part for it, and never a
card the plan cannot honestly fill.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Any

import omnivia_core_runtime.service.chat_generation_executor as executor_module
import pytest
import test_chat_request_manifest as base
from omnivia_core_runtime.service.chat_generation_executor import (
    PLAN_REQUEST_INSTRUCTION,
    PROVIDER_PLAN_TOOL_NAME,
)
from omnivia_core_runtime.service.chat_part_registry import (
    AGENT_PLAN_PART_TYPE,
    JOB_PROGRESS_PART_TYPE,
    TODO_LIST_PART_TYPE,
    PartPayloadInvalid,
    validate_registered_payload,
)
from test_chat_direction_continuation import Scripted, run, status, wire
from test_chat_tool_call_part import (
    owned,
    parts,
    provider_context_through_assistant,
    seeded,
)

__all__ = ["owned", "seeded"]

FIRST = [{"step": "Run pwd", "status": "inProgress"}, {"step": "Run ls -a", "status": "pending"}]
LAST = [{"step": "Run pwd", "status": "completed"}, {"step": "Run ls -a", "status": "inProgress"}]
DONE = [{"step": "Run pwd", "status": "completed"}, {"step": "Run ls -a", "status": "completed"}]


def request_plan(holder: Any, view: str) -> None:
    payload = {"view": view}
    with base.writer(holder) as writer:
        writer.append_message_part(
            part_id="trigger-plan-request",
            message_id=base.TRIGGER_MESSAGE_ID,
            conversation_id=base.CONVERSATION_ID,
            part_index=1,
            part_type="plan.request",
            schema_version=1,
            visibility="internal",
            payload=payload,
            provenance="human",
            content_hash="sha256:" + hashlib.sha256(b"plan-request").hexdigest(),
            created_at_us=base.BASE_US + 150,
        )


def plan_stream(*plans: list[dict[str, Any]], text: str = "Done.") -> Any:
    def script(request: Any) -> Iterator[dict[str, Any]]:
        sequence = 0

        def event(kind: str, **fields: Any) -> dict[str, Any]:
            nonlocal sequence
            sequence += 1
            return wire(request, sequence - 1, kind, **fields)

        yield event("stream-start")
        for index, steps in enumerate(plans):
            yield event("tool-call", toolCallId=f"plan-{index}", stepId=f"plan-step-{index}", toolName=PROVIDER_PLAN_TOOL_NAME, input={})
            yield event("tool-result", toolCallId=f"plan-{index}", stepId=f"plan-step-{index}", output={"steps": steps})
        yield event("text-delta", delta=text, partId="part-1", stepId="answer")
        yield event("finish", finishReason="stop")

    return script


def committed(holder: Any) -> list[tuple[str, dict[str, Any]]]:
    return [(part.part_type, dict(part.payload)) for part in parts(holder)]


def test_todo_list_is_the_last_reported_plan_with_mapped_statuses(seeded: Any) -> None:
    request_plan(seeded, "todo-list")
    run(seeded, Scripted(plan_stream(FIRST, LAST)))
    assert status(seeded).state == "succeeded", status(seeded)
    assert committed(seeded) == [
        ("text", {"text": "Done."}),
        (TODO_LIST_PART_TYPE, {"title": "Plan checklist", "items": [{"text": "Run pwd", "status": "done"}, {"text": "Run ls -a", "status": "active"}]}),
    ], "the plan is a fact carrier: no tool.call part, only the requested view"


def test_agent_plan_marks_the_first_unfinished_step(seeded: Any) -> None:
    request_plan(seeded, "agent-plan")
    run(seeded, Scripted(plan_stream(LAST)))
    assert committed(seeded)[1] == (AGENT_PLAN_PART_TYPE, {"title": "Plan", "steps": [{"label": "Run pwd"}, {"label": "Run ls -a"}], "activeIndex": 1})


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        (LAST, {"stageIndex": 1, "stageProgress": 0}),
        (DONE, {"stageIndex": 2, "stageProgress": 1, "outcome": {"status": "success"}}),
    ],
)
def test_job_progress_claims_success_only_when_every_step_is_done(seeded: Any, steps: list[dict[str, Any]], expected: dict[str, Any]) -> None:
    request_plan(seeded, "job-progress")
    run(seeded, Scripted(plan_stream(steps)))
    assert committed(seeded)[1] == (JOB_PROGRESS_PART_TYPE, {"title": "Plan progress", "stages": [{"name": "Run pwd", "weight": 1}, {"name": "Run ls -a", "weight": 1}], **expected})


def test_without_a_request_or_without_a_plan_there_is_no_card(seeded: Any) -> None:
    run(seeded, Scripted(plan_stream(LAST)))
    assert committed(seeded) == [("text", {"text": "Done."})], "a plan nobody asked to see is not drawn"


def test_a_request_the_provider_did_not_plan_for_has_no_card(seeded: Any) -> None:
    request_plan(seeded, "todo-list")
    run(seeded, Scripted(plan_stream()))
    assert committed(seeded) == [("text", {"text": "Done."})]


@pytest.mark.parametrize(
    "steps",
    [
        [{"step": "Run pwd", "status": "skipped"}],
        [{"step": "Run pwd", "status": "completed", "owner": "x"}],
        [{"step": "Same", "status": "pending"}, {"step": "Same", "status": "pending"}],
    ],
)
def test_a_plan_outside_the_shape_yields_no_card_and_keeps_the_answer(seeded: Any, steps: list[dict[str, Any]]) -> None:
    request_plan(seeded, "job-progress")
    run(seeded, Scripted(plan_stream(steps)))
    assert status(seeded).state == "succeeded", status(seeded)
    assert committed(seeded) == [("text", {"text": "Done."})]


def test_the_request_adds_fixed_core_copy_and_the_plan_reaches_follow_ups(seeded: Any) -> None:
    request_plan(seeded, "todo-list")
    run(seeded, Scripted(plan_stream(DONE)))
    # At generation time the turn that asked carries the fixed Core copy ...
    asking = executor_module._provider_context(
        seeded.connection,
        workspace_id=base.WORKSPACE_ID,
        conversation_id=base.CONVERSATION_ID,
        branch_id=base.BRANCH_ID,
        trigger_message_id=base.TRIGGER_MESSAGE_ID,
        blobs_root=None,
    )
    assert asking.messages[-1]["parts"][-1] == {"kind": "text", "text": PLAN_REQUEST_INSTRUCTION}
    # ... and only then: later turns see the committed plan, not the request again.
    context = provider_context_through_assistant(seeded)
    assert PLAN_REQUEST_INSTRUCTION not in str(context.messages)
    answer_texts = [part["text"] for part in context.messages[-1]["parts"]]
    assert answer_texts[0] == "Done."
    assert answer_texts[1].startswith("Plan (reported by the provider):\n") and "Run ls -a" in answer_texts[1]


def test_the_registry_admits_only_the_shapes_core_produces() -> None:
    validate_registered_payload(JOB_PROGRESS_PART_TYPE, {"title": "P", "stages": [{"name": "a", "weight": 1}], "stageIndex": 0, "stageProgress": 0})
    for bad in (
        {"title": "P", "stages": [{"name": "a", "weight": 1}], "stageIndex": 1, "stageProgress": 1},  # done without outcome
        {"title": "P", "stages": [{"name": "a", "weight": 1}], "stageIndex": 0, "stageProgress": 0, "outcome": {"status": "success"}},
        {"title": "P", "stages": [{"name": "a", "weight": 2}], "stageIndex": 0, "stageProgress": 0},
        {"title": "P", "stages": [{"name": "a", "weight": 1}], "stageIndex": 0, "stageProgress": 0.5},
    ):
        with pytest.raises(PartPayloadInvalid):
            validate_registered_payload(JOB_PROGRESS_PART_TYPE, bad)
    with pytest.raises(PartPayloadInvalid):
        validate_registered_payload(TODO_LIST_PART_TYPE, {"title": "T", "items": [{"text": "a", "status": "failed"}]})
    with pytest.raises(PartPayloadInvalid):
        validate_registered_payload(AGENT_PLAN_PART_TYPE, {"title": "T", "steps": [{"label": "a"}], "activeIndex": 2})
