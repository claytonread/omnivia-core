"""C8 step 3: what a snapshot publishes about a tool call, and to whom.

This projection carries a disclosure decision, so the tests are about the
boundary rather than the plumbing:

* identity, lifecycle state and timing reach ANY reader of the conversation --
  a turn blocked on a tool call is a fact about the conversation, and hiding it
  leaves a reader unable to explain why nothing is happening;
* the `review` member reaches ONLY an actor the wait's own
  authorised-responder policy admits;
* an absent `review` is indistinguishable between "no wait" and "not your wait",
  deliberately, because a reader who could tell them apart would learn who may
  review;
* tool ARGUMENTS are published to nobody, in any form, and neither is any
  digest of a secret.

The last one is asserted by scanning the whole projected document for the
argument values and for every secret-derived field name, rather than by listing
the fields I expect -- a list only proves the fields I remembered.
"""


from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import test_chat_request_manifest as manifest
from omnivia_core_runtime.service.chat_tool_lifecycle import (
    open_chat_turn,
    record_tool_proposal,
)
from omnivia_core_runtime.service.chat_tool_review import (
    begin_policy_check,
    request_tool_review,
)
from omnivia_core_runtime.storage.chat import read_conversation_snapshot_inputs
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

WS = manifest.WORKSPACE_ID
T = manifest.BASE_US
CALL_ID = "toolcall-c8-snap"
TURN_ID = "turn-c8-snap"
WAIT_ID = "wait-c8-snap"
REVIEWER = "actor-reviewer-snap"
BYSTANDER = "actor-bystander-snap"
RESOURCE = "resource:notes.md"

#: Distinctive, so a leak is unmistakable wherever it surfaces.
SECRET_PATH = "a-path-that-must-never-be-projected.md"
SECRET_TEXT = "a-body-that-must-never-be-projected"
ARGS = {"path": SECRET_PATH, "text": SECRET_TEXT}


@pytest.fixture
def owned(tmp_path: Path) -> Any:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    manifest.m1.bootstrap_and_migrate(path, workspace_id=WS)
    holder = manifest.m1.take_ownership(path)
    manifest.seed_running_attempt(holder)
    yield holder
    holder.connection.close()


def _kw(holder: Any) -> dict[str, Any]:
    return {"workspace_id": WS, "fencing_generation": holder.generation}


def _call_awaiting_review(holder: Any, *, policy: dict[str, Any]) -> None:
    open_chat_turn(
        holder.connection,
        holder.identity,
        generation_job_id=manifest.JOB_ID,
        generation_attempt_id=manifest.ATTEMPT_ID,
        turn_id=TURN_ID,
        now_us=T + 40,
        **_kw(holder),
    )
    record_tool_proposal(
        holder.connection,
        holder.identity,
        turn_id=TURN_ID,
        step_id="step-c8-snap",
        step_ordinal=0,
        tool_call_id=CALL_ID,
        tool_name="notes.append",
        tool_version="1",
        registry_ref="registry-c8-v1",
        proposed_arguments=ARGS,
        now_us=T + 41,
        **_kw(holder),
    )
    begin_policy_check(
        holder.connection, holder.identity, tool_call_id=CALL_ID, now_us=T + 50, **_kw(holder)
    )
    request_tool_review(
        holder.connection,
        holder.identity,
        tool_call_id=CALL_ID,
        post_policy_arguments=ARGS,
        wait_id=WAIT_ID,
        requester_actor_id="actor-core-chat",
        authorised_responder_policy=policy,
        prompt={"title": "Append to notes.md?"},
        resume_token=f"resume-{WAIT_ID}",
        target_resource_ref=RESOURCE,
        target_resource_revision=7,
        expires_at_us=T + 600_000_000,
        now_us=T + 60,
        **_kw(holder),
    )


def _tool_calls(holder: Any, actor_id: str) -> list[dict[str, Any]]:
    """The projected `toolCalls`, as the snapshot resolver builds them."""
    from omnivia_core_runtime.service import chat_snapshot

    inputs = read_conversation_snapshot_inputs(
        holder.connection,
        workspace_id=WS,
        conversation_id=manifest.CONVERSATION_ID,
        actor_id=actor_id,
    )
    assert inputs is not None
    return [
        chat_snapshot._tool_call(
            call,
            chat_snapshot.replay_tool_call(
                holder.connection, workspace_id=WS, tool_call_id=call.tool_call_id
            ).wait,
            actor_id,
        )
        for call in inputs.tool_calls
    ]


def test_the_authoritative_read_carries_the_conversations_tool_calls(owned: Any) -> None:
    _call_awaiting_review(owned, policy={"actorId": REVIEWER})
    inputs = read_conversation_snapshot_inputs(
        owned.connection,
        workspace_id=WS,
        conversation_id=manifest.CONVERSATION_ID,
        actor_id=REVIEWER,
    )
    assert inputs is not None
    assert [call.tool_call_id for call in inputs.tool_calls] == [CALL_ID]
    assert inputs.tool_calls_truncated is False


def test_identity_and_state_reach_a_reader_who_may_not_review(owned: Any) -> None:
    _call_awaiting_review(owned, policy={"actorId": REVIEWER})
    [projected] = _tool_calls(owned, BYSTANDER)
    # The call itself IS conversation state: the turn is blocked, and a reader
    # unable to see that cannot explain why nothing is happening.
    assert projected["toolCallId"] == CALL_ID
    assert projected["toolName"] == "notes.append"
    assert projected["turnId"] == TURN_ID
    assert "state" in projected and "policyState" in projected
    # ... but the wait is not theirs to answer, so they learn nothing about it.
    assert "review" not in projected


def test_the_review_reaches_the_admitted_responder(owned: Any) -> None:
    _call_awaiting_review(owned, policy={"actorId": REVIEWER})
    [projected] = _tool_calls(owned, REVIEWER)
    review = projected["review"]
    assert review["waitId"] == WAIT_ID
    # `approval` is the WAIT kind (migration 0033's CHECK); `review_decision`
    # is the command name. Conflating them is what this assertion caught.
    assert review["kind"] == "approval"
    # Exactly what the decision command requires the reviewer to echo, so a
    # reviewer who can see this can comply and one who cannot, cannot.
    assert review["targetResourceRevision"] == 7
    assert review["targetResourceRef"] == RESOURCE
    assert review["requestVersion"] >= 1
    # The human-readable action, which is what a reviewer actually reads.
    assert review["prompt"] == {"title": "Append to notes.md?"}


def test_every_policy_form_is_honoured_by_the_shared_predicate(owned: Any) -> None:
    # `anyActor` admits the bystander; a list admits a member and no one else.
    # Asserted because the projection must agree with the decision command, and
    # it agrees by CALLING the same predicate rather than restating it.
    _call_awaiting_review(owned, policy={"anyActor": True})
    assert "review" in _tool_calls(owned, BYSTANDER)[0]


def test_an_actor_ids_policy_admits_only_its_members(owned: Any) -> None:
    _call_awaiting_review(owned, policy={"actorIds": [REVIEWER]})
    assert "review" in _tool_calls(owned, REVIEWER)[0]
    assert "review" not in _tool_calls(owned, BYSTANDER)[0]


def test_the_arguments_are_published_to_nobody(owned: Any) -> None:
    # Scanned, not enumerated. A list of fields I expect only proves the fields
    # I remembered; scanning the serialised document catches a leak wherever it
    # surfaces, including nested inside `prompt` or a field added later.
    _call_awaiting_review(owned, policy={"anyActor": True})
    for actor in (REVIEWER, BYSTANDER):
        document = json.dumps(_tool_calls(owned, actor))
        assert SECRET_PATH not in document, actor
        assert SECRET_TEXT not in document, actor
        assert "rguments" not in document, actor


def test_no_digest_of_a_secret_is_published(owned: Any) -> None:
    _call_awaiting_review(owned, policy={"anyActor": True})
    document = json.dumps(_tool_calls(owned, REVIEWER))
    for forbidden in (
        "resumeToken",
        "resume_token",
        "answerDigest",
        "answer_digest",
        "sensitiveAnswer",
        "sensitive_answer",
        "argumentsDigest",
        "proposed_arguments",
    ):
        assert forbidden not in document, forbidden


def test_a_call_with_no_wait_and_a_call_not_yours_look_the_same(owned: Any) -> None:
    # Deliberate: a reader able to distinguish them would learn who may review.
    _call_awaiting_review(owned, policy={"actorId": REVIEWER})
    not_mine = _tool_calls(owned, BYSTANDER)[0]
    # A call before any review is requested has no wait at all.
    # SAME turn: a generation attempt holds exactly one chat turn, which
    # `open_chat_turn` enforces. A turn holds many calls.
    record_tool_proposal(
        owned.connection,
        owned.identity,
        turn_id=TURN_ID,
        step_id="step-c8-snap-2",
        step_ordinal=1,
        tool_call_id="toolcall-c8-snap-2",
        tool_name="notes.append",
        tool_version="1",
        registry_ref="registry-c8-v1",
        proposed_arguments=ARGS,
        now_us=T + 141,
        **_kw(owned),
    )
    waitless = next(
        call for call in _tool_calls(owned, BYSTANDER) if call["toolCallId"] != CALL_ID
    )
    assert set(not_mine) - {"review"} == set(not_mine)
    assert "review" not in waitless
    assert "review" not in not_mine
