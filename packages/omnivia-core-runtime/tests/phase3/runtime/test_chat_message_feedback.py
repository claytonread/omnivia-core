"""Durable, private message feedback through Chat's public command and snapshot seams."""

# ruff: noqa: F811

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import test_application_audit_idempotency_migration as m1
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.service.chat_snapshot import resolve_chat_snapshot
from omnivia_core_runtime.service.operations import OperationContext
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.chat import read_conversation, read_message_feedback
from test_chat_offline_queue import owned, seeded  # noqa: F401  (pytest fixtures)
from test_chat_submit_message_resolver import (
    BRANCH_ID,
    CONVERSATION_ID,
    PRINCIPAL,
    ROOT_MESSAGE_ID,
    WORKSPACE_ID,
    _dispatcher,
    _request,
)

from omnivia_core.chat_contract.v1 import PROTOCOL_VERSION
from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
)
from omnivia_core.contracts.v1.generated import ChatSnapshotInput

ASSISTANT_MESSAGE_ID = "msg-feedback-assistant"
GENERATION_JOB_ID = "job-feedback-assistant"
COMMAND_ID = "cmd-message-feedback-1"
GRAPH_REVISION = 1
LATEST_SEQUENCE = 2


def _feedback_command(
    *,
    command_id: str = COMMAND_ID,
    message_id: str = ASSISTANT_MESSAGE_ID,
    actor_id: str = PRINCIPAL,
    reaction: str = "helpful",
    reasons: tuple[str, ...] = (),
    note: str | None = None,
    expected_version: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "commandId": command_id,
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "messageId": message_id,
        "actorId": actor_id,
        "reaction": reaction,
        "reasons": list(reasons),
    }
    if note is not None:
        command["note"] = note
    if expected_version is not None:
        command["expectedVersion"] = expected_version
    command.update(extra)
    return command


def _feedback(holder: m1.Owned, command: Mapping[str, Any]) -> Any:
    command_id = str(command["commandId"])
    return _dispatcher(holder).dispatch(
        _request(
            command,
            command_name="SetMessageFeedback",
            idempotency_key=command_id,
            request_id=f"req-{command_id}",
            expected_conversation={
                "conversation_id": CONVERSATION_ID,
                "graph_revision": GRAPH_REVISION,
                "latest_conversation_sequence": LATEST_SEQUENCE,
            },
        )
    )


def _seed_assistant(holder: m1.Owned) -> m1.Owned:
    with chat.chat_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ) as writer:
        conversation = read_conversation(
            holder.connection,
            workspace_id=WORKSPACE_ID,
            conversation_id=CONVERSATION_ID,
        )
        assert conversation is not None
        writer.update_conversation(
            conversation_id=CONVERSATION_ID,
            expected_graph_revision=conversation.graph_revision,
            graph_revision=GRAPH_REVISION,
            latest_conversation_sequence=LATEST_SEQUENCE,
            state="active",
            title=conversation.title,
            title_source=conversation.title_source,
            default_branch_id=conversation.default_branch_id,
            updated_at_us=3_000_000_000_000_010,
        )
        writer.append_generation_job(
            generation_job_id=GENERATION_JOB_ID,
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            trigger_message_id=ROOT_MESSAGE_ID,
            graph_revision_observed=GRAPH_REVISION,
            idempotency_key="feedback-seed-generation",
            schema_version=1,
            created_at_us=3_000_000_000_000_010,
            updated_at_us=3_000_000_000_000_010,
        )
        writer.insert_generation_job_status_projection(
            generation_job_id=GENERATION_JOB_ID,
            conversation_id=CONVERSATION_ID,
            state="queued",
            updated_at_us=3_000_000_000_000_010,
        )
        writer.append_message(
            message_id=ASSISTANT_MESSAGE_ID,
            conversation_id=CONVERSATION_ID,
            parent_message_id=ROOT_MESSAGE_ID,
            role="assistant",
            author_type="provider",
            conversation_sequence=LATEST_SEQUENCE,
            schema_version=1,
            content_hash="sha256:" + "c" * 64,
            completion_status="complete",
            visibility="standard",
            created_on_branch_id=BRANCH_ID,
            generation_job_id=GENERATION_JOB_ID,
            created_at_us=3_000_000_000_000_011,
            committed_at_us=3_000_000_000_000_012,
        )
        writer.append_message_part(
            part_id="part-feedback-assistant",
            message_id=ASSISTANT_MESSAGE_ID,
            conversation_id=CONVERSATION_ID,
            part_index=0,
            part_type="text",
            schema_version=1,
            visibility="standard",
            payload={"text": "A committed answer"},
            provenance="ai",
            content_hash="sha256:" + "d" * 64,
            created_at_us=3_000_000_000_000_011,
        )
        writer.append_branch_head_event(
            event_id="head-event-feedback-2",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            head_version=2,
            previous_head_message_id=ROOT_MESSAGE_ID,
            new_head_message_id=ASSISTANT_MESSAGE_ID,
            cause="assistant_message_materialised",
            command_id="cmd-feedback-seed",
            graph_revision=GRAPH_REVISION,
            conversation_sequence=LATEST_SEQUENCE,
            actor_id=PRINCIPAL,
            occurred_at_us=3_000_000_000_000_012,
            schema_version=1,
        )
        writer.update_branch_head(
            branch_id=BRANCH_ID,
            expected_head_version=1,
            head_version=2,
            current_head_message_id=ASSISTANT_MESSAGE_ID,
            state="open",
        )
        writer.insert_view_state(
            conversation_id=CONVERSATION_ID,
            actor_id=PRINCIPAL,
            active_branch_id=BRANCH_ID,
            last_seen_graph_revision=GRAPH_REVISION,
            schema_version=1,
            updated_at_us=3_000_000_000_000_013,
        )
    return holder


def test_helpful_feedback_is_durable_private_chat_data(seeded: m1.Owned) -> None:
    holder = _seed_assistant(seeded)
    before = read_conversation(
        holder.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
    )
    response = _feedback(holder, _feedback_command())
    assert isinstance(response, SuccessResponseEnvelope), response
    record = read_message_feedback(
        holder.connection,
        workspace_id=WORKSPACE_ID,
        message_id=ASSISTANT_MESSAGE_ID,
        actor_id=PRINCIPAL,
    )
    assert record is not None
    assert (record.reaction, record.reasons, record.note, record.version) == (
        "helpful",
        (),
        None,
        1,
    )
    after = read_conversation(
        holder.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
    )
    assert before is not None and after is not None
    assert (after.graph_revision, after.latest_conversation_sequence) == (
        before.graph_revision,
        before.latest_conversation_sequence,
    )

    replay = _feedback(holder, _feedback_command())
    assert isinstance(replay, SuccessResponseEnvelope)
    assert replay.result == response.result
    assert (
        read_message_feedback(
            holder.connection,
            workspace_id=WORKSPACE_ID,
            message_id=ASSISTANT_MESSAGE_ID,
            actor_id=PRINCIPAL,
        ).version
        == 1
    )


def test_not_helpful_feedback_replaces_only_at_the_exact_version(
    seeded: m1.Owned,
) -> None:
    holder = _seed_assistant(seeded)
    assert isinstance(_feedback(holder, _feedback_command()), SuccessResponseEnvelope)
    replacement = _feedback_command(
        command_id="cmd-message-feedback-2",
        reaction="not_helpful",
        reasons=("wrong_source", "missing_evidence"),
        note="The signed agreement says otherwise.",
        expected_version=1,
    )
    assert isinstance(_feedback(holder, replacement), SuccessResponseEnvelope)
    record = read_message_feedback(
        holder.connection,
        workspace_id=WORKSPACE_ID,
        message_id=ASSISTANT_MESSAGE_ID,
        actor_id=PRINCIPAL,
    )
    assert record is not None
    assert record.reaction == "not_helpful"
    assert record.reasons == ("wrong_source", "missing_evidence")
    assert record.note == "The signed agreement says otherwise."
    assert record.version == 2

    stale = _feedback_command(
        command_id="cmd-message-feedback-stale",
        reaction="not_helpful",
        reasons=("incorrect",),
        expected_version=1,
    )
    assert isinstance(_feedback(holder, stale), ErrorResponseEnvelope)
    assert (
        read_message_feedback(
            holder.connection,
            workspace_id=WORKSPACE_ID,
            message_id=ASSISTANT_MESSAGE_ID,
            actor_id=PRINCIPAL,
        ).version
        == 2
    )


def test_feedback_decoder_and_target_fail_closed(seeded: m1.Owned) -> None:
    holder = _seed_assistant(seeded)
    refused = (
        _feedback_command(reaction="helpful", reasons=("incorrect",)),
        _feedback_command(reaction="not_helpful", reasons=("unknown",)),
        _feedback_command(note="x" * 601),
        _feedback_command(actor_id="actor-someone-else"),
        _feedback_command(message_id=ROOT_MESSAGE_ID),
        _feedback_command(undeclared=True),
    )
    for index, command in enumerate(refused):
        command["commandId"] = f"cmd-message-feedback-refused-{index}"
        response = _feedback(holder, command)
        assert isinstance(response, ErrorResponseEnvelope), response
    assert (
        read_message_feedback(
            holder.connection,
            workspace_id=WORKSPACE_ID,
            message_id=ASSISTANT_MESSAGE_ID,
            actor_id=PRINCIPAL,
        )
        is None
    )


def test_snapshot_projects_only_the_authenticated_actors_feedback(
    seeded: m1.Owned,
) -> None:
    holder = _seed_assistant(seeded)
    assert isinstance(
        _feedback(
            holder,
            _feedback_command(
                reaction="not_helpful", reasons=("incorrect",), note="Needs correction."
            ),
        ),
        SuccessResponseEnvelope,
    )
    with chat.chat_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ) as writer:
        writer.insert_message_feedback(
            conversation_id=CONVERSATION_ID,
            message_id=ASSISTANT_MESSAGE_ID,
            actor_id="actor-other",
            reaction="helpful",
            reasons=(),
            note=None,
            created_at_us=3_000_000_000_000_020,
        )

    entry = get_operation_metadata("chat.snapshot")
    request_id = "req-feedback-snapshot"
    query = {
        "requestId": request_id,
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "actorId": PRINCIPAL,
    }
    context = OperationContext(
        request=s0.envelope_for(
            entry,
            operation_input={
                "conversation_id": CONVERSATION_ID,
                "snapshot_query": query,
            },
            request_id=request_id,
            workspace_id=WORKSPACE_ID,
        ),
        principal=PRINCIPAL,
        workspace_id=WORKSPACE_ID,
        granted_operations=frozenset({"chat.snapshot"}),
    )
    result = resolve_chat_snapshot(
        holder.connection,
        ChatSnapshotInput(conversation_id=CONVERSATION_ID, snapshot_query=query),
        context,
    )
    feedback = result["snapshot"]["messageFeedback"]
    assert len(feedback) == 1
    assert feedback[0] == {
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "messageId": ASSISTANT_MESSAGE_ID,
        "actorId": PRINCIPAL,
        "reaction": "not_helpful",
        "reasons": ["incorrect"],
        "note": "Needs correction.",
        "schemaVersion": 1,
        "version": 1,
        "createdAt": feedback[0]["createdAt"],
        "updatedAt": feedback[0]["updatedAt"],
    }
