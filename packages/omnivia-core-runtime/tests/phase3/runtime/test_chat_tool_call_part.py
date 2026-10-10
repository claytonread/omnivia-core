"""C9 acceptance for the first non-text part family: `tool.call`.

A provider `tool-call` event used to leave no trace whatsoever. `_durable_event`
returns `None` for it, so `append_provider_generation_event` wrote nothing and
the event's content was dropped. These tests hold the link that replaces that:

- the event is recorded in C8's tool-call ledger BEFORE the stream goes on, so a
  crash mid-stream keeps what the provider already asked for -- the guarantee
  `append_provider_text_delta` gives text, and the reason the part is not
  assembled from memory at commit;
- it is DENIED in the same breath, because nothing in this build executes a
  provider tool. Left at `proposed` it would be permanently unfinished (the
  row's own CHECK ties `finished_at_us` to the four terminal states) and would
  ride every snapshot page claiming a turn is blocked after the generation has
  committed;
- its `registry_ref` says `unadmitted:provider-proposal`, because there is no
  governed tool registry anywhere in Core to admit it against. A plausible
  reference would put an admission that never happened into the one ledger an
  auditor reads to find out what was admitted;
- the committed Message carries one `tool.call` part per ledger row, after its
  text, and that part carries NO arguments in any form -- not the values, not
  the digest. The registry closes the shape so the absence is enforced;
- the Message `content_hash` covers the whole part list, and a text-only commit
  hashes exactly as it did before C9, so no durable message changes meaning;
- the whole thing is idempotent under a redelivered stream, and the denied call
  never enters the C8 review lifecycle, so a restart's `recover_tool_calls`
  leaves it alone.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_chat_request_manifest as base
from omnivia_core_runtime.service.chat_generation_executor import (
    NO_EXECUTION_PATH,
    UNADMITTED_REGISTRY_REF,
    UNADMITTED_TOOL_VERSION,
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
    # Denied, terminal, and finished -- never left `proposed`.
    assert call.state == "denied"
    assert call.policy_state == "denied"
    assert call.failure_code == NO_EXECUTION_PATH
    assert call.finished_at_us is not None
    # And it says, in the ledger, that it was admitted against nothing.
    #
    # PINNED AS LITERALS, not compared to the constants. Comparing to
    # `UNADMITTED_REGISTRY_REF` passes whatever that constant is changed to --
    # including a plausible-looking `core.tools/read_file@1`, which is the exact
    # falsehood this value exists to avoid. The constants are checked against
    # the literals separately, so a rename still has one place to update.
    assert call.registry_ref == "unadmitted:provider-proposal"
    assert call.tool_version == "unadmitted"
    assert UNADMITTED_REGISTRY_REF == "unadmitted:provider-proposal"
    assert UNADMITTED_TOOL_VERSION == "unadmitted"
    assert call.registry_ref.startswith("unadmitted:"), "it must read as an absence"


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
        registry_ref = UNADMITTED_REGISTRY_REF

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
