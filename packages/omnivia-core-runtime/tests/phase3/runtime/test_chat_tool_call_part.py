"""C9 acceptance for the first non-text part family: `tool.call`.

A provider `tool-call` event used to leave no trace whatsoever. `_durable_event`
returns `None` for it, so `append_provider_generation_event` wrote nothing and
the event's content was dropped. These tests hold the link that replaces that:

- the event is recorded in C8's tool-call ledger BEFORE the stream goes on, so a
  crash mid-stream keeps what the provider already asked for -- the guarantee
  `append_provider_text_delta` gives text, and the reason the part is not
  assembled from memory at commit;
- it remains proposed until either an exact provider `tool-result` settles it
  or the stream reaches a terminal boundary. The latter denies an unresolved
  proposal so it cannot remain permanently unfinished after the generation;
- its `registry_ref` and version identify the narrowly bounded
  provider-reported lifecycle. This records what the installed provider already
  executed; it does not claim that Core admitted or executed the command;
- the committed Message carries one `tool.call` part per ledger row, after its
  text, and that part carries NO arguments in any form -- not the values, not
  the digest. The registry closes the shape so the absence is enforced;
- the Message `content_hash` covers the whole part list, and a text-only commit
  hashes exactly as it did before C9, so no durable message changes meaning;
- the whole thing is idempotent under a redelivered stream; provider results
  settle calls while unresolved proposals are denied at the terminal boundary,
  and neither path enters the C8 review lifecycle.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import omnivia_core_runtime.service.chat_generation_executor as executor_module
import pytest
import test_chat_request_manifest as base
from omnivia_core_runtime.service.chat_generation_executor import (
    NO_EXECUTION_PATH,
    PROVIDER_REPORTED_REGISTRY_REF,
    PROVIDER_REPORTED_TOOL_VERSION,
    _message_hash,
    tool_call_part_payload,
)
from omnivia_core_runtime.service.chat_part_registry import (
    TOOL_CALL_PART_TYPE,
    PartPayloadInvalid,
    is_registered_part_type,
    validate_registered_payload,
)
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

# The real executor harness. Helpers only -- the fixtures are defined locally,
# which is the convention `test_chat_direction_continuation` itself follows over
# `test_chat_request_manifest`: an imported fixture whose name a test parameter
# then shadows is reported as a redefinition and reads as an accident.
from test_chat_direction_continuation import (
    ACTOR_ID,
    Crash,
    Scripted,
    assistant_messages,
    count,
    play,
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
    """The graph one Generation runs over, its queued submission, and its head."""
    base.seed_conversation_and_queue(owned)
    with base.writer(owned) as writer:
        writer.append_branch_head_event(
            event_id="head-event-tool-call-trigger",
            conversation_id=base.CONVERSATION_ID,
            branch_id=base.BRANCH_ID,
            head_version=2,
            previous_head_message_id=base.ROOT_MESSAGE_ID,
            new_head_message_id=base.TRIGGER_MESSAGE_ID,
            cause="user_message_appended",
            command_id="command-tool-call-trigger",
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


ARGUMENTS = {"path": "/etc/passwd", "mode": "read"}


def tool_stream(
    *calls: tuple[str, str, dict[str, Any]],
    text: str = "looking that up",
) -> Any:
    """One invocation: start, a text delta, the tool calls, then a clean finish."""

    def script(request: Any) -> Iterator[dict[str, Any]]:
        yield wire(request, 0, "stream-start")
        yield wire(request, 1, "text-delta", delta=text, partId="part-1", stepId="step-1")
        for index, (call_id, name, arguments) in enumerate(calls, start=2):
            yield wire(
                request,
                index,
                "tool-call",
                toolCallId=call_id,
                stepId="step-1",
                toolName=name,
                input=arguments,
            )
        yield wire(request, len(calls) + 2, "finish", finishReason="stop")

    return script


def ledger(holder: m1.Owned) -> tuple[chat.ChatToolCall, ...]:
    message = assistant_messages(holder)[0]
    assert message.generation_job_id is not None
    return chat.read_chat_tool_calls_for_generation_job(
        holder.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=message.generation_job_id,
    )


def parts(holder: m1.Owned) -> tuple[Any, ...]:
    message = assistant_messages(holder)[0]
    return chat.read_message_parts(
        holder.connection, workspace_id=WORKSPACE_ID, message_id=message.message_id
    )


# ---------------------------------------------------------------------------
# The producer
# ---------------------------------------------------------------------------


def test_a_provider_tool_call_is_recorded_and_denied_in_one_generation(seeded: m1.Owned) -> None:
    run(seeded, Scripted(tool_stream(("provider-call-1", "read_file", ARGUMENTS))))

    calls = ledger(seeded)
    assert len(calls) == 1
    call = calls[0]
    assert call.tool_name == "read_file"
    # No result followed, so the terminal stream boundary denies the proposal.
    assert call.state == "denied"
    assert call.policy_state == "denied"
    assert call.failure_code == NO_EXECUTION_PATH
    assert call.finished_at_us is not None
    # The source marker says only that F2a reported the lifecycle. It is not a
    # claim that a governed Core tool registry admitted or executed the call.
    assert call.registry_ref == "provider-reported:f2a-tool-result"
    assert call.tool_version == "f2a-v1"
    assert PROVIDER_REPORTED_REGISTRY_REF == "provider-reported:f2a-tool-result"
    assert PROVIDER_REPORTED_TOOL_VERSION == "f2a-v1"


def test_the_arguments_are_recorded_in_the_ledger_and_published_nowhere(seeded: m1.Owned) -> None:
    run(seeded, Scripted(tool_stream(("provider-call-1", "read_file", ARGUMENTS))))

    call = ledger(seeded)[0]
    # The ledger is what the arguments are FOR: it holds them and their digest.
    assert dict(call.proposed_arguments) == ARGUMENTS
    assert call.proposed_arguments_digest.startswith("sha256:")

    # Nothing outbound carries them. Checked over the committed part bytes
    # rather than over the payload dict, because a nested or re-encoded copy
    # would pass a field-name check and still disclose the value.
    document = json.dumps([dict(part.payload) for part in parts(seeded)])
    assert "/etc/passwd" not in document
    assert call.proposed_arguments_digest not in document
    for part in parts(seeded):
        assert "input" not in part.payload
        assert "arguments" not in part.payload


def test_the_denied_call_never_enters_the_c8_review_lifecycle(seeded: m1.Owned) -> None:
    # `recover_tool_calls` reads ONLY the lifecycle-events table. A call with no
    # row there is not "open", so a restart never tries to settle one of these
    # and never reports it as interrupted.
    run(seeded, Scripted(tool_stream(("provider-call-1", "read_file", ARGUMENTS))))

    assert count(seeded, "omnivia_chat_tool_lifecycle_events") == 0
    assert count(seeded, "omnivia_chat_wait_interactions") == 0
    heads = chat.read_open_chat_tool_lifecycle_heads(seeded.connection, workspace_id=WORKSPACE_ID)
    assert heads == ()


def test_two_tool_calls_get_their_own_steps_in_provider_order(seeded: m1.Owned) -> None:
    run(
        seeded,
        Scripted(
            tool_stream(
                ("provider-call-1", "read_file", {"path": "a"}),
                ("provider-call-2", "write_file", {"path": "b"}),
            )
        ),
    )

    calls = ledger(seeded)
    assert [call.tool_name for call in calls] == ["read_file", "write_file"]
    # One step per CALL, not per provider step: both events carried the same
    # `stepId: step-1`, and the turn-step table is keyed by step.
    assert len({call.step_id for call in calls}) == 2
    assert count(seeded, "omnivia_chat_turn_steps") == 2
    # One turn for the attempt, which is what `open_chat_turn` keys on.
    assert count(seeded, "omnivia_chat_turns") == 1


def test_a_malformed_tool_call_event_fails_the_generation_and_records_nothing(
    seeded: m1.Owned,
) -> None:
    def script(request: Any) -> Iterator[dict[str, Any]]:
        yield wire(request, 0, "stream-start")
        yield wire(
            request,
            1,
            "tool-call",
            toolCallId="provider-call-1",
            stepId="step-1",
            toolName="read_file",
            input="not an object",
        )
        yield wire(request, 2, "finish", finishReason="stop")

    # NOT a raise: the executor terminalizes a stream it cannot interpret as a
    # failed generation rather than letting the error escape, which is the same
    # choice it makes for an oversized response. A test asserting `raises` here
    # passed for the wrong reason until it was run.
    run(seeded, Scripted(script))

    assert status(seeded).state == "failed"
    # Nothing half-written: no proposal, no turn, no step, and no part.
    assert count(seeded, "omnivia_chat_tool_calls") == 0
    assert count(seeded, "omnivia_chat_turns") == 0
    assert count(seeded, "omnivia_chat_turn_steps") == 0
    assert assistant_messages(seeded) == []


# ---------------------------------------------------------------------------
# The part at commit
# ---------------------------------------------------------------------------


def test_the_committed_message_carries_one_tool_call_part_after_its_text(
    seeded: m1.Owned,
) -> None:
    run(seeded, Scripted(tool_stream(("provider-call-1", "read_file", ARGUMENTS))))

    committed = parts(seeded)
    assert [part.part_type for part in committed] == ["text", TOOL_CALL_PART_TYPE]
    assert [part.part_index for part in committed] == [0, 1]
    assert committed[0].payload["text"] == "looking that up"
    payload = dict(committed[1].payload)
    assert payload["name"] == "read_file"
    assert payload["id"] == ledger(seeded)[0].tool_call_id
    assert payload["state"] == "denied"
    assert "Not run" in payload["detail"]


def test_the_commit_validates_the_payload_rather_than_trusting_the_builder(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The registry call at commit is defence against a future change to
    # `tool_call_part_payload`, so it cannot be proved by a payload that is
    # already correct. A builder that starts emitting the arguments must be
    # refused BEFORE the part is durable, not reviewed later.
    import omnivia_core_runtime.service.chat_generation_executor as module

    def leaky(call: chat.ChatToolCall) -> dict[str, Any]:
        return {"name": call.tool_name, "input": json.dumps(dict(call.proposed_arguments))}

    monkeypatch.setattr(module, "tool_call_part_payload", leaky)
    run(seeded, Scripted(tool_stream(("provider-call-1", "read_file", ARGUMENTS))))

    # Terminalized, and nothing committed: no message, so no leaked part.
    assert status(seeded).state == "failed"
    assert assistant_messages(seeded) == []
    # The seeded conversation has its own user-message parts, so the count that
    # matters is the leak: no `tool.call` part reached storage, and no part
    # anywhere carries the arguments.
    leaked = seeded.connection.execute(
        "SELECT COUNT(*) FROM omnivia_chat_message_parts WHERE part_type = ? "
        "OR payload_json LIKE '%/etc/passwd%'",
        (TOOL_CALL_PART_TYPE,),
    ).fetchone()
    assert leaked[0] == 0


def test_the_part_payload_is_exactly_the_registered_shape(seeded: m1.Owned) -> None:
    run(seeded, Scripted(tool_stream(("provider-call-1", "read_file", ARGUMENTS))))

    assert is_registered_part_type(TOOL_CALL_PART_TYPE)
    for part in parts(seeded):
        validate_registered_payload(part.part_type, part.payload)
    # The registry is what forbids arguments, so prove it refuses them.
    payload = tool_call_part_payload(ledger(seeded)[0])
    with pytest.raises(PartPayloadInvalid, match="input"):
        validate_registered_payload(TOOL_CALL_PART_TYPE, {**payload, "input": "{}"})
    with pytest.raises(PartPayloadInvalid, match="name"):
        validate_registered_payload(TOOL_CALL_PART_TYPE, {k: v for k, v in payload.items() if k != "name"})


def test_the_message_hash_covers_the_parts_not_just_the_text() -> None:
    text = "looking that up"
    bare, _ = _message_hash(text)

    class Row:
        tool_name = "read_file"
        tool_call_id = "toolcall-abc"
        state = "denied"
        registry_ref = PROVIDER_REPORTED_REGISTRY_REF

    withpart, hashes = _message_hash(text, (Row(),))  # type: ignore[arg-type]
    assert withpart != bare, "a message with a tool call is not the same message"
    assert len(hashes) == 2, "one part hash per part, in part order"


def test_a_text_only_commit_hashes_exactly_as_it_did_before_c9(seeded: m1.Owned) -> None:
    # The regression that would matter most: the idempotency check in
    # `_materialize_assistant` compares the recomputed hash against the durable
    # one, so a changed text-only hash would make every existing assistant
    # message conflict with its own content.
    run(seeded, Scripted(play("hello ", "world")))
    committed = parts(seeded)
    assert [part.part_type for part in committed] == ["text"]
    message = assistant_messages(seeded)[0]
    assert message.content_hash == _message_hash("hello world")[0]
    # The pinned pre-C9 value for this exact text, so a future change to the
    # document shape cannot pass by recomputing both sides the same new way.
    assert message.content_hash == (
        "sha256:" + __import__("hashlib").sha256(
            json.dumps(
                [{"payload": {"text": "hello world"}, "type": "text", "visibility": "standard"}],
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )


def test_a_redelivered_stream_converges_instead_of_conflicting(seeded: m1.Owned) -> None:
    # Stable ids: the same attempt and provider call id recompute the same turn,
    # step and tool-call ids, `record_tool_proposal` returns the existing row for
    # an identical proposal, and `deny_tool_call` returns the existing denial.
    script = tool_stream(("provider-call-1", "read_file", ARGUMENTS))
    run(seeded, Scripted(script))
    first = ledger(seeded)[0]

    run(seeded, Scripted(script))

    calls = ledger(seeded)
    assert len(calls) == 1, "a replay records no second proposal"
    assert calls[0].tool_call_id == first.tool_call_id
    assert calls[0].state == "denied"
    assert len(parts(seeded)) == 2, "and commits no second part"
    assert count(seeded, "omnivia_chat_turns") == 1
    assert count(seeded, "omnivia_chat_turn_steps") == 1


def test_the_snapshot_projection_carries_the_new_family_with_no_per_type_list(
    seeded: m1.Owned,
) -> None:
    # §2.3 step 3 asks for "snapshot and events carry the new part type". They
    # already do, and this is the proof rather than an inspection: `_part`
    # projects `type` and `payload` generically, so a registered family needs no
    # change there at all. A projection that filtered by type would drop this.
    from omnivia_core_runtime.service.chat_snapshot import _part

    run(seeded, Scripted(tool_stream(("provider-call-1", "read_file", ARGUMENTS))))

    projected = [_part(part) for part in parts(seeded)]
    assert [document["type"] for document in projected] == ["text", TOOL_CALL_PART_TYPE]
    tool_document = projected[1]
    assert tool_document["payload"]["name"] == "read_file"
    assert tool_document["index"] == 1
    # The projection states the bare hex: `_digest` strips the `sha256:` prefix
    # the storage column carries, which is the wire shape every other digest in
    # this bundle uses.
    assert re.fullmatch(r"[0-9a-f]{64}", str(tool_document["contentHash"]))
    # And still nothing of the arguments, through the projection too.
    assert "/etc/passwd" not in json.dumps(projected)


# ---------------------------------------------------------------------------
# Phase 2: the canonical F2a trace, run through the real executor
# ---------------------------------------------------------------------------

TRACES = Path(__file__).resolve().parents[5] / "contracts" / "chat" / "v1" / "fixtures" / "traces"


def canonical(name: str, *, mutate: Any = None) -> Any:
    """A canonical F2a trace as a provider script, exactly as the contract states it.

    The ONLY thing rewritten is `invocationId`: the executor derives that id from
    the job and attempt, so a fixed one in a fixture can never match. Every other
    field -- ordinals, step ids, tool call ids, inputs, the finish -- is the
    fixture's own, and `mutate` (tests only) edits an event before it is played.
    """
    recorded = json.loads((TRACES / f"{name}.json").read_text(encoding="utf-8"))["events"]

    def script(request: Any) -> Iterator[dict[str, Any]]:
        for event in recorded:
            played = copy.deepcopy(event)
            played["invocationId"] = request.invocation_id
            if mutate is not None:
                mutate(played)
            yield played

    return script


TRACE_03 = "f2a-trace-03-repeated-tool-id-distinct-steps"


def crash_between_ledger_and_message(holder: m1.Owned) -> None:
    """Run trace 03 until the process dies after the ledger is written, before the Message."""

    def dies(*args: Any, **kwargs: Any) -> Any:
        raise Crash

    with pytest.raises(Crash), pytest.MonkeyPatch.context() as patch:
        patch.setattr(executor_module.ChatGenerationExecutor, "_materialize_assistant", dies)
        run(holder, Scripted(canonical(TRACE_03)))


def test_canonical_trace_03_commits_two_calls_on_distinct_steps_in_provider_order(
    seeded: m1.Owned,
) -> None:
    run(seeded, Scripted(canonical(TRACE_03)))

    # The trace is tool-call-only (no text event) and ends `finish(tool-calls)`:
    # the generation succeeds rather than failing for want of text.
    assert status(seeded).state == "succeeded"
    calls = ledger(seeded)
    # Same provider `toolCallId` on both, distinct steps: two rows, not one and
    # not a conflict.
    assert [call.tool_name for call in calls] == ["search_docs", "search_docs"]
    assert len({call.tool_call_id for call in calls}) == 2
    assert len({call.step_id for call in calls}) == 2
    assert [dict(call.proposed_arguments) for call in calls] == [
        {"query": "pricing"},
        {"query": "delivery"},
    ]
    assert count(seeded, "omnivia_chat_tool_calls") == 2
    assert count(seeded, "omnivia_chat_turn_steps") == 2
    committed = parts(seeded)
    # No invented empty prose: only the two typed parts, in provider order.
    assert [part.part_type for part in committed] == [TOOL_CALL_PART_TYPE, TOOL_CALL_PART_TYPE]
    assert [part.part_index for part in committed] == [0, 1]
    assert [part.payload["id"] for part in committed] == [call.tool_call_id for call in calls]
    assert [call.state for call in calls] == ["succeeded", "succeeded"]
    assert [part.payload["state"] for part in committed] == ["succeeded", "succeeded"]
    assert [
        chat.read_chat_tool_result(
            seeded.connection, workspace_id=WORKSPACE_ID, tool_call_id=call.tool_call_id
        ).status
        for call in calls
    ] == ["succeeded", "succeeded"]


def test_provider_reported_error_settles_the_call_failed_without_publishing_output(
    seeded: m1.Owned,
) -> None:
    def script(request: Any) -> Iterator[dict[str, Any]]:
        yield wire(request, 0, "stream-start")
        yield wire(
            request,
            1,
            "tool-call",
            toolCallId="command-1",
            stepId="step-1",
            toolName="codex.read_only_command",
            input={},
        )
        yield wire(
            request,
            2,
            "tool-result",
            toolCallId="command-1",
            stepId="step-1",
            output={"exitCode": 1},
            isError=True,
        )
        yield wire(request, 3, "text-delta", delta="done", partId="part-1", stepId="step-2")
        yield wire(request, 4, "finish", finishReason="stop")

    run(seeded, Scripted(script))

    assert status(seeded).state == "succeeded"
    call = ledger(seeded)[0]
    assert call.state == "failed"
    assert call.policy_state == "approved"
    assert call.failure_code == "provider_reported_tool_failure"
    result = chat.read_chat_tool_result(
        seeded.connection, workspace_id=WORKSPACE_ID, tool_call_id=call.tool_call_id
    )
    assert result is not None
    assert result.status == "failed"
    assert dict(result.result_payload) == {"exitCode": 1}
    projected = [dict(part.payload) for part in parts(seeded)]
    assert projected[-1]["state"] == "failed"
    assert "exitCode" not in json.dumps(projected)


def test_unmatched_tool_result_fails_and_closes_the_existing_proposal(
    seeded: m1.Owned,
) -> None:
    def script(request: Any) -> Iterator[dict[str, Any]]:
        yield wire(request, 0, "stream-start")
        yield wire(
            request,
            1,
            "tool-call",
            toolCallId="command-1",
            stepId="step-1",
            toolName="codex.read_only_command",
            input={},
        )
        yield wire(
            request,
            2,
            "tool-result",
            toolCallId="command-1",
            stepId="wrong-step",
            output={"exitCode": 0},
        )

    run(seeded, Scripted(script))

    assert status(seeded).state == "failed"
    calls = chat.read_chat_tool_calls_for_generation_job(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=base.JOB_ID,
    )
    assert len(calls) == 1
    call = calls[0]
    assert call.state == "denied"
    assert call.failure_code == NO_EXECUTION_PATH
    assert chat.read_chat_tool_result(
        seeded.connection, workspace_id=WORKSPACE_ID, tool_call_id=call.tool_call_id
    ) is None
    assert assistant_messages(seeded) == []


def test_canonical_trace_03_replay_converges_to_the_same_rows(seeded: m1.Owned) -> None:
    # A real replay, not a second `run` over a settled job (which returns before
    # invoking anything): the process dies after the ledger is written and before
    # the Message is, then the same stream arrives again.
    crash_between_ledger_and_message(seeded)
    assert status(seeded).state == "running"
    assert assistant_messages(seeded) == []
    before = [
        tuple(row)
        for table in (
            "omnivia_chat_tool_calls",
            "omnivia_chat_tool_results",
            "omnivia_chat_turn_steps",
            "omnivia_chat_turns",
        )
        for row in seeded.connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3")
    ]
    assert count(seeded, "omnivia_chat_tool_calls") == 2

    run(seeded, Scripted(canonical(TRACE_03)))

    assert status(seeded).state == "succeeded"
    after = [
        tuple(row)
        for table in (
            "omnivia_chat_tool_calls",
            "omnivia_chat_tool_results",
            "omnivia_chat_turn_steps",
            "omnivia_chat_turns",
        )
        for row in seeded.connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3")
    ]
    # The ledger the replay left is the one the crash left, byte for byte.
    assert after == before
    assert len(assistant_messages(seeded)) == 1
    assert [part.part_type for part in parts(seeded)] == [TOOL_CALL_PART_TYPE] * 2


def test_the_same_identity_with_other_arguments_conflicts_and_changes_nothing(
    seeded: m1.Owned,
) -> None:
    crash_between_ledger_and_message(seeded)
    durable = [
        tuple(row) for row in seeded.connection.execute("SELECT * FROM omnivia_chat_tool_calls ORDER BY 1, 2")
    ]

    def other_arguments(event: dict[str, Any]) -> None:
        if event["eventType"] == "tool-call" and event["stepId"] == "step-2":
            event["input"] = {"query": "something else"}

    run(seeded, Scripted(canonical(TRACE_03, mutate=other_arguments)))

    # Refused: the generation fails, no Message commits, and the durable history
    # is exactly what the first delivery wrote.
    assert status(seeded).state == "failed"
    assert assistant_messages(seeded) == []
    assert [
        tuple(row) for row in seeded.connection.execute("SELECT * FROM omnivia_chat_tool_calls ORDER BY 1, 2")
    ] == durable
    assert count(seeded, "omnivia_chat_tool_calls") == 2


@pytest.mark.parametrize("step_id", [None, "", " ", "has space", "-leading", "x" * 129, 7])
def test_a_tool_call_without_a_well_formed_step_id_fails_and_records_nothing(
    seeded: m1.Owned, step_id: Any
) -> None:
    def break_step(event: dict[str, Any]) -> None:
        if event["eventType"] == "tool-call":
            if step_id is None:
                event.pop("stepId")
            else:
                event["stepId"] = step_id

    run(seeded, Scripted(canonical(TRACE_03, mutate=break_step)))

    assert status(seeded).state == "failed"
    assert count(seeded, "omnivia_chat_tool_calls") == 0
    assert assistant_messages(seeded) == []


def test_a_tool_call_and_text_commit_text_first_then_calls_in_provider_order(
    seeded: m1.Owned,
) -> None:
    def script(request: Any) -> Iterator[dict[str, Any]]:
        yield wire(request, 0, "stream-start")
        yield wire(request, 1, "text-delta", delta="checking", partId="part-1", stepId="step-1")
        for ordinal, (step, query) in enumerate((("step-1", "a"), ("step-2", "b")), start=2):
            yield wire(
                request, ordinal, "tool-call",
                toolCallId="tool-XYZ", stepId=step, toolName="search_docs", input={"query": query},
            )
        yield wire(request, 4, "finish", finishReason="tool-calls")

    run(seeded, Scripted(script))

    committed = parts(seeded)
    assert [part.part_type for part in committed] == ["text", TOOL_CALL_PART_TYPE, TOOL_CALL_PART_TYPE]
    assert [part.part_index for part in committed] == [0, 1, 2]
    assert [part.payload["id"] for part in committed[1:]] == [c.tool_call_id for c in ledger(seeded)]
    assert [dict(c.proposed_arguments) for c in ledger(seeded)] == [{"query": "a"}, {"query": "b"}]


def test_a_successful_stream_with_no_text_and_no_tool_call_still_fails(seeded: m1.Owned) -> None:
    def script(request: Any) -> Iterator[dict[str, Any]]:
        yield wire(request, 0, "stream-start")
        yield wire(request, 1, "finish", finishReason="tool-calls")

    run(seeded, Scripted(script))

    assert status(seeded).state == "failed"
    assert assistant_messages(seeded) == []
    assert count(seeded, "omnivia_chat_tool_calls") == 0


def test_a_tool_call_only_message_hash_is_over_the_typed_parts_alone() -> None:
    class Row:
        tool_name = "read_file"
        tool_call_id = "toolcall-abc"
        state = "denied"
        registry_ref = PROVIDER_REPORTED_REGISTRY_REF

    only, hashes = _message_hash("", (Row(),))  # type: ignore[arg-type]
    assert len(hashes) == 1, "no empty text part is invented"
    assert only != _message_hash("", ())[0]
    assert only != _message_hash("x", (Row(),))[0]  # type: ignore[arg-type]


def test_many_calls_in_one_tick_keep_provider_order_not_hash_order(seeded: m1.Owned) -> None:
    # The fake clock stamps every ledger row identically, so `created_at_us` cannot
    # order them; the old tie-break on `tool_call_id` (a hash) ordered them
    # arbitrarily. Eight calls make a match by luck a 1-in-40320 event.
    names = [f"tool_{index}" for index in range(8)]

    def script(request: Any) -> Iterator[dict[str, Any]]:
        yield wire(request, 0, "stream-start")
        for ordinal, name in enumerate(names, start=1):
            yield wire(
                request, ordinal, "tool-call",
                toolCallId=f"call-{ordinal}", stepId="step-1", toolName=name, input={},
            )
        yield wire(request, 9, "finish", finishReason="tool-calls")

    run(seeded, Scripted(script))

    assert len({call.created_at_us for call in ledger(seeded)}) == 1, "the premise: one tick"
    assert [call.tool_name for call in ledger(seeded)] == names
    assert [part.payload["name"] for part in parts(seeded)] == names
