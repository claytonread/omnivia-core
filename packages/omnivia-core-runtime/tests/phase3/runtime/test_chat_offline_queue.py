"""W5 GB-05: offline queue commands, drain, and the authoritative queue snapshot.

Reuses the GB-01 harness (`test_chat_submit_message_resolver.py`) for its
dispatcher, workspace/actor/conversation fixtures and its `seeded` conversation
rather than re-deriving them, so a fixture drift between the two suites is
impossible.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_v06_5_s0_mutation_foundation as s0
from jsonschema import Draft202012Validator
from omnivia_core_runtime.service.chat_generation import claim_queued_generation
from omnivia_core_runtime.service.chat_snapshot import resolve_chat_snapshot
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from test_chat_submit_message_resolver import (
    _SCHEMA_REGISTRY,
    ACTOR_ID,
    BASE_US,
    BRANCH_ID,
    CHAT_TABLES,
    CONVERSATION_ID,
    GRAPH_REVISION,
    PRINCIPAL,
    ROOT_MESSAGE_ID,
    SEEDED_SEQUENCE,
    WORKSPACE_ID,
    _counts,
    _dispatcher,
    _request,
    _snapshot_schema_errors,
    _submit_command,
    _submit_queued,
    owned,
    seeded,
)

from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
)
from omnivia_core.contracts.v1.generated import ChatSnapshotInput

__all__ = ["owned", "seeded"]  # re-exported fixtures

_SNAPSHOT_ENTRY = get_operation_metadata("chat.snapshot")
_BRIDGE_SNAPSHOT_ITEM_REF = (
    "https://contracts.omnivia.dev/chat/v1/bridge.schema.json#/$defs/BridgeSnapshotItem"
)


def _schema_errors(ref: str, document: Any) -> list[str]:
    validator = Draft202012Validator({"$ref": ref}, registry=_SCHEMA_REGISTRY)
    return [error.message for error in validator.iter_errors(document)]


def _enqueue_command(
    *,
    command_id: str = "cmd-gb05-enqueue",
    branch_id: str = BRANCH_ID,
    actor_id: str = PRINCIPAL,
    text: str = "queued offline",
    **extra: Any,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "protocolVersion": "1.0",
        "commandId": command_id,
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "branchId": branch_id,
        "actorId": actor_id,
        "editableParts": [{"type": "text", "payload": {"text": text}}],
        "attachmentReferences": [],
        "contextReferences": [],
    }
    command.update(extra)
    return command


def _update_queue_command(
    *,
    command_id: str = "cmd-gb05-update",
    queued_submission_id: str,
    expected_version: int = 1,
    text: str = "edited offline",
    actor_id: str = PRINCIPAL,
    **extra: Any,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "protocolVersion": "1.0",
        "commandId": command_id,
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "actorId": actor_id,
        "queuedSubmissionId": queued_submission_id,
        "editableParts": [{"type": "text", "payload": {"text": text}}],
        "expectedVersion": expected_version,
    }
    command.update(extra)
    return command


def _reorder_command(
    *,
    command_id: str,
    queued_submission_id: str,
    target_position: int,
    expected_version: int,
    actor_id: str = PRINCIPAL,
) -> dict[str, Any]:
    return {
        "protocolVersion": "1.0",
        "commandId": command_id,
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "actorId": actor_id,
        "queuedSubmissionId": queued_submission_id,
        "targetPosition": target_position,
        "expectedVersion": expected_version,
    }


def _cancel_command(*, command_id: str, queued_submission_id: str, actor_id: str = PRINCIPAL) -> dict[str, Any]:
    return {
        "protocolVersion": "1.0",
        "commandId": command_id,
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "actorId": actor_id,
        "queuedSubmissionId": queued_submission_id,
    }


def _enqueue(
    holder: m1.Owned,
    *,
    command_id: str = "cmd-gb05-enqueue",
    text: str = "queued offline",
    request_id: str | None = None,
) -> SuccessResponseEnvelope:
    response = _dispatcher(holder).dispatch(
        _request(
            _enqueue_command(command_id=command_id, text=text),
            command_name="EnqueueMessage",
            idempotency_key=f"idem-{command_id}",
            request_id=request_id or f"req-{command_id}",
        )
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    return response


def _seed_foreign_row(
    holder: m1.Owned,
    *,
    queued_submission_id: str,
    queue_position: int,
    actor_id: str = "actor-gb05-other",
    text: str = "not mine",
) -> None:
    """Write a queued row directly, as another actor's own request would have.

    `EnqueueMessage` always requires `actorId == context.principal`, so a second
    actor's row can only be produced here the way it would be in production: by
    that actor's own request, simulated by writing the row directly rather than
    through this fixture's single-principal dispatcher.
    """
    with chat.chat_writer(
        holder.connection, holder.identity, workspace_id=WORKSPACE_ID, fencing_generation=holder.generation
    ) as writer:
        writer.append_queued_submission(
            queued_submission_id=queued_submission_id,
            conversation_id=CONVERSATION_ID,
            actor_id=actor_id,
            queue_sequence=queue_position,
            branch_id=BRANCH_ID,
            editable_parts=({"type": "text", "visibility": "standard", "payload": {"text": text}},),
            references=(),
            idempotency_key=queued_submission_id.removesuffix(".sub"),
            created_at_us=BASE_US + queue_position,
            updated_at_us=BASE_US + queue_position,
        )
        writer.insert_queue_order_projection(
            queued_submission_id=queued_submission_id,
            conversation_id=CONVERSATION_ID,
            queue_position=queue_position,
            updated_by_actor_id=actor_id,
            updated_at_us=BASE_US + queue_position,
        )


def _give_principal_a_view_state(holder: m1.Owned, *, actor_id: str = PRINCIPAL) -> None:
    """A `chat.snapshot` answers only where the actor has view state (REF-042 §15.1).

    `seeded` (GB-01) writes its conversation, message, branch and head event
    directly rather than through `SubmitMessage`, so it -- like this restart
    fixture's own direct seed -- opens no view state for any actor. These tests
    read the offline queue, not the branch path, so they only need one to exist.
    """
    with chat.chat_writer(
        holder.connection, holder.identity, workspace_id=WORKSPACE_ID, fencing_generation=holder.generation
    ) as writer:
        writer.insert_view_state(
            conversation_id=CONVERSATION_ID,
            actor_id=actor_id,
            active_branch_id=BRANCH_ID,
            last_seen_graph_revision=GRAPH_REVISION,
            schema_version=1,
            updated_at_us=BASE_US + 5,
        )


def _snapshot_context(request_id: str, *, actor_id: str = PRINCIPAL) -> OperationContext:
    return OperationContext(
        request=s0.envelope_for(
            _SNAPSHOT_ENTRY,
            operation_input={
                "conversation_id": CONVERSATION_ID,
                "snapshot_query": {
                    "requestId": request_id,
                    "workspaceId": WORKSPACE_ID,
                    "conversationId": CONVERSATION_ID,
                    "actorId": actor_id,
                },
            },
            request_id=request_id,
            workspace_id=WORKSPACE_ID,
        ),
        principal=actor_id,
        workspace_id=WORKSPACE_ID,
        granted_operations=frozenset({"chat.snapshot"}),
    )


def _snapshot_input(request_id: str, *, actor_id: str = PRINCIPAL) -> ChatSnapshotInput:
    return ChatSnapshotInput(
        conversation_id=CONVERSATION_ID,
        snapshot_query={
            "requestId": request_id,
            "workspaceId": WORKSPACE_ID,
            "conversationId": CONVERSATION_ID,
            "actorId": actor_id,
        },
    )


def _snapshot(holder: m1.Owned, *, request_id: str, actor_id: str = PRINCIPAL) -> Mapping[str, Any]:
    return resolve_chat_snapshot(
        holder.connection,
        _snapshot_input(request_id, actor_id=actor_id),
        _snapshot_context(request_id, actor_id=actor_id),
    )["snapshot"]


# --- 1. EnqueueMessage writes only queue + order, and replays idempotently ---------


def test_enqueue_writes_only_queue_and_order_rows_and_replays_idempotently(
    seeded: m1.Owned,
) -> None:
    before = _counts(seeded)
    response = _enqueue(seeded)

    after = _counts(seeded)
    delta = {table: after[table] - before[table] for table in CHAT_TABLES}
    assert {table: n for table, n in delta.items() if n} == {
        "omnivia_chat_queued_submissions": 1,
        "omnivia_chat_queue_order_projection": 1,
    }
    assert response.result["command_result"]["resultRef"] == "queued-submission:cmd-gb05-enqueue.sub"

    queued = chat.read_queued_submission(
        seeded.connection, workspace_id=WORKSPACE_ID, queued_submission_id="cmd-gb05-enqueue.sub"
    )
    assert queued is not None
    assert queued.state == "queued"
    assert queued.actor_id == PRINCIPAL
    assert queued.submitted_message_id is None
    assert queued.submitted_generation_job_id is None

    # No generation job exists yet: an enqueued row is not generation-executable.
    assert chat.read_next_executable_queued_submission(seeded.connection, workspace_id=WORKSPACE_ID) is None

    replay_before = _counts(seeded)
    replay = _dispatcher(seeded).dispatch(
        _request(
            _enqueue_command(),
            command_name="EnqueueMessage",
            idempotency_key="idem-cmd-gb05-enqueue",
            request_id="req-cmd-gb05-enqueue",
        )
    )
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == response.result
    # The mutation seam itself records one execution row per replayed attempt
    # (`test_gb01_submit_replay_returns_stored_result_without_duplicate_rows`);
    # every domain write is otherwise unchanged.
    assert _counts(seeded) == {
        **replay_before,
        s0.EXECUTIONS_TABLE: replay_before[s0.EXECUTIONS_TABLE] + 1,
    }


# --- 2. strict refusal paths write nothing ------------------------------------------


def test_enqueue_refuses_a_foreign_actor_before_writing(seeded: m1.Owned) -> None:
    before = _counts(seeded)
    response = _dispatcher(seeded).dispatch(
        _request(
            _enqueue_command(actor_id=ACTOR_ID),
            command_name="EnqueueMessage",
            idempotency_key="idem-gb05-foreign-actor",
            request_id="req-gb05-foreign-actor",
        )
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "invalid_request"
    assert _counts(seeded) == before


def test_enqueue_refuses_an_unopened_branch_without_an_existence_oracle(
    seeded: m1.Owned,
) -> None:
    before = _counts(seeded)
    response = _dispatcher(seeded).dispatch(
        _request(
            _enqueue_command(branch_id="branch-does-not-exist"),
            command_name="EnqueueMessage",
            idempotency_key="idem-gb05-no-branch",
            request_id="req-gb05-no-branch",
        )
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before


def test_enqueue_with_a_nonempty_reference_stays_dependency_unavailable(
    seeded: m1.Owned,
) -> None:
    before = _counts(seeded)
    response = _dispatcher(seeded).dispatch(
        _request(
            _enqueue_command(attachmentReferences=[{"referenceId": "att-1"}]),
            command_name="EnqueueMessage",
            idempotency_key="idem-gb05-attach",
            request_id="req-gb05-attach",
        )
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "dependency_unavailable"
    assert _counts(seeded) == before


def test_enqueue_malformed_editable_parts_refuses_before_writing(seeded: m1.Owned) -> None:
    before = _counts(seeded)
    response = _dispatcher(seeded).dispatch(
        _request(
            _enqueue_command(editableParts=[]),
            command_name="EnqueueMessage",
            idempotency_key="idem-gb05-empty-parts",
            request_id="req-gb05-empty-parts",
        )
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "invalid_request"
    assert _counts(seeded) == before


# --- 3. UpdateQueuedSubmission changes only content + version -----------------------


def test_update_queued_submission_changes_only_content_and_version(seeded: m1.Owned) -> None:
    _enqueue(seeded)
    before = chat.read_queued_submission(
        seeded.connection, workspace_id=WORKSPACE_ID, queued_submission_id="cmd-gb05-enqueue.sub"
    )
    assert before is not None

    response = _dispatcher(seeded).dispatch(
        _request(
            _update_queue_command(
                queued_submission_id="cmd-gb05-enqueue.sub", expected_version=1, text="revised"
            ),
            command_name="UpdateQueuedSubmission",
            idempotency_key="idem-gb05-update",
            request_id="req-gb05-update",
        )
    )
    assert isinstance(response, SuccessResponseEnvelope), response

    after = chat.read_queued_submission(
        seeded.connection, workspace_id=WORKSPACE_ID, queued_submission_id="cmd-gb05-enqueue.sub"
    )
    assert after is not None
    assert after.version == before.version + 1
    assert after.editable_parts[0]["payload"]["text"] == "revised"
    # Identity, order, branch and idempotency are untouched.
    assert after.queue_sequence == before.queue_sequence
    assert after.branch_id == before.branch_id
    assert after.idempotency_key == before.idempotency_key
    assert after.state == "queued"


def test_update_queued_submission_stale_version_conflicts_without_writes(
    seeded: m1.Owned,
) -> None:
    _enqueue(seeded)
    before = _counts(seeded)
    response = _dispatcher(seeded).dispatch(
        _request(
            _update_queue_command(queued_submission_id="cmd-gb05-enqueue.sub", expected_version=2),
            command_name="UpdateQueuedSubmission",
            idempotency_key="idem-gb05-update-stale",
            request_id="req-gb05-update-stale",
        )
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before


def test_update_queued_submission_after_claim_conflicts_without_writes(
    seeded: m1.Owned,
) -> None:
    _enqueue(seeded)
    claim_queued_generation(
        seeded.connection,
        seeded.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=seeded.generation,
        queued_submission_id="cmd-gb05-enqueue.sub",
        generation_job_id="cmd-gb05-enqueue.gen",
        generation_attempt_id="cmd-gb05-enqueue.attempt1",
        trigger_message_id=ROOT_MESSAGE_ID,
        lease_owner="runner-gb05",
        now_us=BASE_US + 2_000_000,
    )
    before = _counts(seeded)
    response = _dispatcher(seeded).dispatch(
        _request(
            _update_queue_command(queued_submission_id="cmd-gb05-enqueue.sub", expected_version=1),
            command_name="UpdateQueuedSubmission",
            idempotency_key="idem-gb05-update-claimed",
            request_id="req-gb05-update-claimed",
        )
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before


def test_update_queued_submission_after_drain_conflicts_without_writes(
    seeded: m1.Owned,
) -> None:
    """Once `SubmitMessage.fromQueuedSubmissionId` opens this row's correlated
    generation job, the row is executable and its content is frozen -- even
    though the row itself stays `queued` (drained, not yet claimed).
    """
    _enqueue(seeded, command_id="cmd-gb05-drain-freeze", text="freeze me")
    drain = _dispatcher(seeded).dispatch(
        _request(
            _submit_command(
                command_id="cmd-gb05-drain-freeze-drain",
                message_id="cmd-gb05-drain-freeze-drain.msg",
                actorId=PRINCIPAL,
                editable_parts=({"type": "text", "payload": {"text": "freeze me"}},),
                fromQueuedSubmissionId="cmd-gb05-drain-freeze.sub",
            ),
            idempotency_key="idem-gb05-drain-freeze-drain",
            request_id="req-gb05-drain-freeze-drain",
        )
    )
    assert isinstance(drain, SuccessResponseEnvelope), drain

    before = chat.read_queued_submission(
        seeded.connection, workspace_id=WORKSPACE_ID, queued_submission_id="cmd-gb05-drain-freeze.sub"
    )
    assert before is not None
    assert before.state == "queued"  # drained but not yet claimed
    assert chat.read_next_executable_queued_submission(seeded.connection, workspace_id=WORKSPACE_ID) is not None
    before_counts = _counts(seeded)

    response = _dispatcher(seeded).dispatch(
        _request(
            _update_queue_command(queued_submission_id="cmd-gb05-drain-freeze.sub", expected_version=1),
            command_name="UpdateQueuedSubmission",
            idempotency_key="idem-gb05-update-after-drain",
            request_id="req-gb05-update-after-drain",
        )
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before_counts

    after = chat.read_queued_submission(
        seeded.connection, workspace_id=WORKSPACE_ID, queued_submission_id="cmd-gb05-drain-freeze.sub"
    )
    assert after is not None
    assert after.version == before.version
    assert after.editable_parts == before.editable_parts


# --- 4. Reorder never moves a cross-actor or terminal row ---------------------------


def test_reorder_never_moves_a_cross_actor_row(seeded: m1.Owned) -> None:
    _enqueue(seeded, command_id="cmd-gb05-mine-1", text="mine one")
    _enqueue(seeded, command_id="cmd-gb05-mine-2", text="mine two")
    _seed_foreign_row(seeded, queued_submission_id="cmd-gb05-foreign.sub", queue_position=3)

    before = _counts(seeded)
    response = _dispatcher(seeded).dispatch(
        _request(
            _reorder_command(
                command_id="cmd-gb05-reorder-onto-foreign",
                queued_submission_id="cmd-gb05-mine-1.sub",
                target_position=3,
                expected_version=1,
            ),
            command_name="ReorderQueuedSubmission",
            idempotency_key="idem-gb05-reorder-onto-foreign",
            request_id="req-gb05-reorder-onto-foreign",
        )
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before

    mine_1 = chat.read_queue_order_projection(
        seeded.connection, workspace_id=WORKSPACE_ID, queued_submission_id="cmd-gb05-mine-1.sub"
    )
    foreign = chat.read_queue_order_projection(
        seeded.connection, workspace_id=WORKSPACE_ID, queued_submission_id="cmd-gb05-foreign.sub"
    )
    assert mine_1 is not None and mine_1.queue_position == 1
    assert foreign is not None and foreign.queue_position == 3

    # Reordering within the caller's own active rows still works.
    ok = _dispatcher(seeded).dispatch(
        _request(
            _reorder_command(
                command_id="cmd-gb05-reorder-own",
                queued_submission_id="cmd-gb05-mine-2.sub",
                target_position=1,
                expected_version=1,
            ),
            command_name="ReorderQueuedSubmission",
            idempotency_key="idem-gb05-reorder-own",
            request_id="req-gb05-reorder-own",
        )
    )
    assert isinstance(ok, SuccessResponseEnvelope), ok
    mine_1_after = chat.read_queue_order_projection(
        seeded.connection, workspace_id=WORKSPACE_ID, queued_submission_id="cmd-gb05-mine-1.sub"
    )
    mine_2_after = chat.read_queue_order_projection(
        seeded.connection, workspace_id=WORKSPACE_ID, queued_submission_id="cmd-gb05-mine-2.sub"
    )
    foreign_after = chat.read_queue_order_projection(
        seeded.connection, workspace_id=WORKSPACE_ID, queued_submission_id="cmd-gb05-foreign.sub"
    )
    assert mine_2_after is not None and mine_2_after.queue_position == 1
    assert mine_1_after is not None and mine_1_after.queue_position == 2
    assert foreign_after is not None and foreign_after.queue_position == 3


def test_reorder_between_own_positions_fails_closed_crossing_an_outsiders_position(
    seeded: m1.Owned,
) -> None:
    """Moving between two of the caller's own positions can still cross an
    outsider-held intermediate position; the shift must refuse closed rather
    than colliding with that position's storage-level uniqueness.
    """
    _enqueue(seeded, command_id="cmd-gb05-cross-1", text="cross one")
    _seed_foreign_row(seeded, queued_submission_id="cmd-gb05-cross-mid.sub", queue_position=2)
    _enqueue(seeded, command_id="cmd-gb05-cross-3", text="cross three")

    before = _counts(seeded)
    response = _dispatcher(seeded).dispatch(
        _request(
            _reorder_command(
                command_id="cmd-gb05-reorder-cross",
                queued_submission_id="cmd-gb05-cross-1.sub",
                target_position=3,
                expected_version=1,
            ),
            command_name="ReorderQueuedSubmission",
            idempotency_key="idem-gb05-reorder-cross",
            request_id="req-gb05-reorder-cross",
        )
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before

    one = chat.read_queue_order_projection(
        seeded.connection, workspace_id=WORKSPACE_ID, queued_submission_id="cmd-gb05-cross-1.sub"
    )
    mid = chat.read_queue_order_projection(
        seeded.connection, workspace_id=WORKSPACE_ID, queued_submission_id="cmd-gb05-cross-mid.sub"
    )
    three = chat.read_queue_order_projection(
        seeded.connection, workspace_id=WORKSPACE_ID, queued_submission_id="cmd-gb05-cross-3.sub"
    )
    assert one is not None and one.queue_position == 1 and one.version == 1
    assert mid is not None and mid.queue_position == 2 and mid.version == 1
    assert three is not None and three.queue_position == 3 and three.version == 1


# --- 5. Cancel removes the row from active snapshot/executable selection ------------


def test_cancel_removes_row_from_snapshot_without_deleting_it(seeded: m1.Owned) -> None:
    _enqueue(seeded)
    response = _dispatcher(seeded).dispatch(
        _request(
            _cancel_command(command_id="cmd-gb05-cancel", queued_submission_id="cmd-gb05-enqueue.sub"),
            command_name="CancelQueuedSubmission",
            idempotency_key="idem-gb05-cancel",
            request_id="req-gb05-cancel",
        )
    )
    assert isinstance(response, SuccessResponseEnvelope), response

    queued = chat.read_queued_submission(
        seeded.connection, workspace_id=WORKSPACE_ID, queued_submission_id="cmd-gb05-enqueue.sub"
    )
    assert queued is not None  # never deleted
    assert queued.state == "cancelled"
    assert (
        chat.read_active_queue_for_actor(
            seeded.connection,
            workspace_id=WORKSPACE_ID,
            conversation_id=CONVERSATION_ID,
            actor_id=PRINCIPAL,
        )
        == ()
    )
    assert chat.read_next_executable_queued_submission(seeded.connection, workspace_id=WORKSPACE_ID) is None

    _give_principal_a_view_state(seeded)
    snapshot = _snapshot(seeded, request_id="req-gb05-cancel-snapshot")
    assert "queuedSubmissions" not in snapshot


# --- 6. Pre-submit rows survive reconstruction, appear FIFO, don't block drain -------


def _seed_restart_workspace(holder: m1.Owned) -> None:
    """The GB-01 conversation, message, branch and head event, written directly.

    `seeded` is connection-scoped, and these tests need a workspace file that
    outlives one connection, so the same rows are written here against a holder
    the caller owns.
    """
    with chat.chat_writer(
        holder.connection, holder.identity, workspace_id=WORKSPACE_ID, fencing_generation=holder.generation
    ) as writer:
        writer.append_conversation(
            conversation_id=CONVERSATION_ID,
            state="active",
            graph_revision=GRAPH_REVISION,
            latest_conversation_sequence=0,
            schema_version=1,
            created_by_actor_id=PRINCIPAL,
            created_at_us=BASE_US,
            updated_at_us=BASE_US,
        )
        writer.update_conversation(
            conversation_id=CONVERSATION_ID,
            expected_graph_revision=GRAPH_REVISION,
            graph_revision=GRAPH_REVISION,
            latest_conversation_sequence=SEEDED_SEQUENCE,
            state="active",
            updated_at_us=BASE_US + 1,
        )
        writer.append_message(
            message_id=ROOT_MESSAGE_ID,
            conversation_id=CONVERSATION_ID,
            role="user",
            author_type="human",
            author_id=PRINCIPAL,
            conversation_sequence=SEEDED_SEQUENCE,
            schema_version=1,
            content_hash="sha256:" + "a" * 64,
            completion_status="complete",
            visibility="standard",
            created_at_us=BASE_US + 2,
            committed_at_us=BASE_US + 2,
        )
        writer.append_branch(
            branch_id=BRANCH_ID,
            conversation_id=CONVERSATION_ID,
            origin_kind="original",
            initial_head_message_id=ROOT_MESSAGE_ID,
            created_by_actor_id=PRINCIPAL,
            created_at_us=BASE_US + 4,
            created_conversation_sequence=SEEDED_SEQUENCE,
            schema_version=1,
        )
        writer.append_branch_head_event(
            event_id="head-event-gb05-restart",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            head_version=1,
            previous_head_message_id=None,
            new_head_message_id=ROOT_MESSAGE_ID,
            cause="branch_created",
            command_id="command-gb05-restart-seed",
            graph_revision=GRAPH_REVISION,
            conversation_sequence=SEEDED_SEQUENCE,
            actor_id=PRINCIPAL,
            occurred_at_us=BASE_US + 4,
            schema_version=1,
        )


def test_pre_submit_rows_survive_reconnection_and_dont_block_executable_work(tmp_path) -> None:
    path = tmp_path / "gb05-restart.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    try:
        _seed_restart_workspace(holder)
        _give_principal_a_view_state(holder)
        _enqueue(holder, command_id="cmd-gb05-restart-1", text="offline one")
        _enqueue(holder, command_id="cmd-gb05-restart-2", text="offline two")
        # No job exists for either row yet, so nothing is executable.
        assert chat.read_next_executable_queued_submission(holder.connection, workspace_id=WORKSPACE_ID) is None
    finally:
        holder.connection.close()

    # Simulate a process restart: close and reopen a connection to the same file.
    reopened = m1.take_ownership(path)
    try:
        entries = chat.read_active_queue_for_actor(
            reopened.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID, actor_id=PRINCIPAL
        )
        assert [entry.submission.queued_submission_id for entry in entries] == [
            "cmd-gb05-restart-1.sub",
            "cmd-gb05-restart-2.sub",
        ]
        assert [entry.order.queue_position for entry in entries] == [1, 2]
        for entry in entries:
            assert entry.submission.version == 1
            assert entry.order.version == 1

        snapshot = _snapshot(reopened, request_id="req-gb05-restart-snapshot")
        assert [item["queuedSubmissionId"] for item in snapshot["queuedSubmissions"]] == [
            "cmd-gb05-restart-1.sub",
            "cmd-gb05-restart-2.sub",
        ]

        # A later, unrelated eligible row (an ordinary direct SubmitMessage, which
        # opens its own job in the same transaction) is picked up by the
        # executable-queue read without erroring on the two pre-submit rows.
        _submit_queued(
            reopened,
            command_id="cmd-gb05-restart-direct",
            expected_head_message_id=ROOT_MESSAGE_ID,
            expected_head_version=1,
            expected_sequence=SEEDED_SEQUENCE,
        )
        executable = chat.read_next_executable_queued_submission(reopened.connection, workspace_id=WORKSPACE_ID)
        assert executable is not None
        assert executable.queued_submission_id == "cmd-gb05-restart-direct.sub"
    finally:
        reopened.connection.close()


# --- 7 & 8. SubmitMessage.fromQueuedSubmissionId drains exactly once ----------------


def test_submit_message_drains_a_queued_submission_exactly_once(seeded: m1.Owned) -> None:
    _enqueue(seeded, text="drain me")
    before = _counts(seeded)

    response = _dispatcher(seeded).dispatch(
        _request(
            _submit_command(
                command_id="cmd-gb05-drain",
                message_id="cmd-gb05-drain.msg",
                actorId=PRINCIPAL,
                editable_parts=({"type": "text", "payload": {"text": "drain me"}},),
                fromQueuedSubmissionId="cmd-gb05-enqueue.sub",
            ),
            idempotency_key="idem-gb05-drain",
            request_id="req-gb05-drain",
        )
    )
    assert isinstance(response, SuccessResponseEnvelope), response

    after = _counts(seeded)
    delta = {table: after[table] - before[table] for table in before}
    assert delta["omnivia_chat_messages"] == 1
    assert delta["omnivia_chat_generation_jobs"] == 1
    assert delta["omnivia_chat_transactional_outbox"] == 1
    # No second queue or order row: the drain reused the existing pair.
    assert delta["omnivia_chat_queued_submissions"] == 0
    assert delta["omnivia_chat_queue_order_projection"] == 0

    queued = chat.read_queued_submission(
        seeded.connection, workspace_id=WORKSPACE_ID, queued_submission_id="cmd-gb05-enqueue.sub"
    )
    assert queued is not None
    assert queued.state == "queued"  # left for the generation claimant to walk

    job_id = "cmd-gb05-enqueue.gen"
    job = chat.read_generation_job(seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=job_id)
    assert job is not None
    assert job.trigger_message_id == "cmd-gb05-drain.msg"

    executable = chat.read_next_executable_queued_submission(seeded.connection, workspace_id=WORKSPACE_ID)
    assert executable is not None
    assert executable.queued_submission_id == "cmd-gb05-enqueue.sub"

    claimed = claim_queued_generation(
        seeded.connection,
        seeded.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=seeded.generation,
        queued_submission_id="cmd-gb05-enqueue.sub",
        generation_job_id=job_id,
        generation_attempt_id="cmd-gb05-enqueue.attempt1",
        trigger_message_id="cmd-gb05-drain.msg",
        lease_owner="runner-gb05-drain",
        now_us=BASE_US + 5_000_000,
    )
    assert claimed.submission.state == "submitted"
    assert claimed.submission.submitted_message_id == "cmd-gb05-drain.msg"
    assert claimed.job.state == "running"
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_drain_refuses_mismatched_content_without_writing(seeded: m1.Owned) -> None:
    _enqueue(seeded, text="original content")
    before = _counts(seeded)

    response = _dispatcher(seeded).dispatch(
        _request(
            _submit_command(
                command_id="cmd-gb05-drain-mismatch",
                message_id="cmd-gb05-drain-mismatch.msg",
                actorId=PRINCIPAL,
                editable_parts=({"type": "text", "payload": {"text": "a stale renderer's content"}},),
                fromQueuedSubmissionId="cmd-gb05-enqueue.sub",
            ),
            idempotency_key="idem-gb05-drain-mismatch",
            request_id="req-gb05-drain-mismatch",
        )
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before


def test_a_second_drain_of_an_already_drained_row_conflicts_without_a_second_job(
    seeded: m1.Owned,
) -> None:
    _enqueue(seeded, text="drain once")
    first = _dispatcher(seeded).dispatch(
        _request(
            _submit_command(
                command_id="cmd-gb05-drain-first",
                message_id="cmd-gb05-drain-first.msg",
                actorId=PRINCIPAL,
                editable_parts=({"type": "text", "payload": {"text": "drain once"}},),
                fromQueuedSubmissionId="cmd-gb05-enqueue.sub",
            ),
            idempotency_key="idem-gb05-drain-first",
            request_id="req-gb05-drain-first",
        )
    )
    assert isinstance(first, SuccessResponseEnvelope), first
    before = _counts(seeded)

    second = _dispatcher(seeded).dispatch(
        _request(
            _submit_command(
                command_id="cmd-gb05-drain-second",
                message_id="cmd-gb05-drain-second.msg",
                actorId=PRINCIPAL,
                editable_parts=({"type": "text", "payload": {"text": "drain once"}},),
                fromQueuedSubmissionId="cmd-gb05-enqueue.sub",
            ),
            idempotency_key="idem-gb05-drain-second",
            request_id="req-gb05-drain-second",
        )
    )
    assert isinstance(second, ErrorResponseEnvelope), second
    assert second.error.code == "conflict"
    assert _counts(seeded) == before

    # Exactly one job exists, addressable by the queue row's own correlation.
    job = chat.read_generation_job(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id="cmd-gb05-enqueue.gen"
    )
    assert job is not None
    assert job.trigger_message_id == "cmd-gb05-drain-first.msg"


def test_repeated_submit_of_the_same_drain_replays_without_duplicating(seeded: m1.Owned) -> None:
    _enqueue(seeded, text="idempotent drain")
    command = _submit_command(
        command_id="cmd-gb05-drain-replay",
        message_id="cmd-gb05-drain-replay.msg",
        actorId=PRINCIPAL,
        editable_parts=({"type": "text", "payload": {"text": "idempotent drain"}},),
        fromQueuedSubmissionId="cmd-gb05-enqueue.sub",
    )
    first = _dispatcher(seeded).dispatch(
        _request(command, idempotency_key="idem-gb05-drain-replay", request_id="req-gb05-drain-replay-1")
    )
    assert isinstance(first, SuccessResponseEnvelope), first
    before = _counts(seeded)

    replay = _dispatcher(seeded).dispatch(
        _request(command, idempotency_key="idem-gb05-drain-replay", request_id="req-gb05-drain-replay-2")
    )
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == first.result
    assert _counts(seeded) == {**before, s0.EXECUTIONS_TABLE: before[s0.EXECUTIONS_TABLE] + 1}


def test_read_next_executable_queued_submission_skips_a_correlated_non_queued_job(
    seeded: m1.Owned,
) -> None:
    """A submission row stays `queued` only until a claim moves it and its job
    together, so a correlated job away from its freshly-opened state is a
    combination this build's own writers never produce -- exercised here as
    defence in depth on the join predicate itself, proving it neither selects
    the disqualified row nor starves a later, genuinely eligible one.
    """
    _enqueue(seeded, command_id="cmd-gb05-stale-job", text="stale job")
    drain = _dispatcher(seeded).dispatch(
        _request(
            _submit_command(
                command_id="cmd-gb05-stale-job-drain",
                message_id="cmd-gb05-stale-job-drain.msg",
                actorId=PRINCIPAL,
                editable_parts=({"type": "text", "payload": {"text": "stale job"}},),
                fromQueuedSubmissionId="cmd-gb05-stale-job.sub",
            ),
            idempotency_key="idem-gb05-stale-job-drain",
            request_id="req-gb05-stale-job-drain",
        )
    )
    assert isinstance(drain, SuccessResponseEnvelope), drain

    job_id = "cmd-gb05-stale-job.gen"
    job = chat.read_generation_job(seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=job_id)
    assert job is not None
    assert job.state == "queued"
    assert chat.read_next_executable_queued_submission(seeded.connection, workspace_id=WORKSPACE_ID) is not None

    attempt_started_at_us = job.created_at_us + 100
    with chat.chat_writer(
        seeded.connection, seeded.identity, workspace_id=WORKSPACE_ID, fencing_generation=seeded.generation
    ) as writer:
        writer.append_generation_attempt(
            generation_attempt_id="cmd-gb05-stale-job.attempt1",
            conversation_id=CONVERSATION_ID,
            generation_job_id=job_id,
            attempt_number=1,
            state="running",
            schema_version=1,
            started_at_us=attempt_started_at_us,
        )
        writer.update_generation_job(
            generation_job_id=job_id,
            expected_state="queued",
            expected_lease_epoch=0,
            state="running",
            lease_epoch=1,
            current_attempt_id="cmd-gb05-stale-job.attempt1",
            lease_owner="runner-gb05-stale",
            lease_expires_at_us=attempt_started_at_us + 500,
            heartbeat_at_us=attempt_started_at_us,
            updated_at_us=attempt_started_at_us,
            started_at_us=attempt_started_at_us,
        )

    stale_submission = chat.read_queued_submission(
        seeded.connection, workspace_id=WORKSPACE_ID, queued_submission_id="cmd-gb05-stale-job.sub"
    )
    assert stale_submission is not None
    assert stale_submission.state == "queued"  # unaffected: only the job moved
    assert chat.read_next_executable_queued_submission(seeded.connection, workspace_id=WORKSPACE_ID) is None

    # The first drain already advanced the conversation's own
    # `latest_conversation_sequence` past the fixture baseline, so both remaining
    # dispatches must state that up-to-date sequence explicitly.
    fresh_enqueue = _dispatcher(seeded).dispatch(
        _request(
            _enqueue_command(command_id="cmd-gb05-fresh-job", text="fresh job"),
            command_name="EnqueueMessage",
            idempotency_key="idem-cmd-gb05-fresh-job",
            request_id="req-cmd-gb05-fresh-job",
            expected_sequence=SEEDED_SEQUENCE + 1,
        )
    )
    assert isinstance(fresh_enqueue, SuccessResponseEnvelope), fresh_enqueue
    fresh_drain = _dispatcher(seeded).dispatch(
        _request(
            _submit_command(
                command_id="cmd-gb05-fresh-job-drain",
                message_id="cmd-gb05-fresh-job-drain.msg",
                actorId=PRINCIPAL,
                expected_head_message_id="cmd-gb05-stale-job-drain.msg",
                expected_head_version=2,
                editable_parts=({"type": "text", "payload": {"text": "fresh job"}},),
                fromQueuedSubmissionId="cmd-gb05-fresh-job.sub",
            ),
            idempotency_key="idem-gb05-fresh-job-drain",
            request_id="req-gb05-fresh-job-drain",
            expected_sequence=SEEDED_SEQUENCE + 1,
        )
    )
    assert isinstance(fresh_drain, SuccessResponseEnvelope), fresh_drain

    executable = chat.read_next_executable_queued_submission(seeded.connection, workspace_id=WORKSPACE_ID)
    assert executable is not None
    assert executable.queued_submission_id == "cmd-gb05-fresh-job.sub"


# --- 9. Snapshot is actor/workspace isolated, deterministic and fails closed --------


def test_snapshot_shows_only_the_authenticated_actors_own_active_queue(seeded: m1.Owned) -> None:
    _enqueue(seeded, command_id="cmd-gb05-mine", text="mine")
    _seed_foreign_row(seeded, queued_submission_id="cmd-gb05-other.sub", queue_position=2)
    _give_principal_a_view_state(seeded)

    snapshot = _snapshot(seeded, request_id="req-gb05-isolated-snapshot")
    assert [item["queuedSubmissionId"] for item in snapshot["queuedSubmissions"]] == ["cmd-gb05-mine.sub"]
    assert _snapshot_schema_errors(snapshot) == []


def test_snapshot_fails_closed_when_the_active_queue_overflows_the_bound(seeded: m1.Owned) -> None:
    _give_principal_a_view_state(seeded)
    with chat.chat_writer(
        seeded.connection, seeded.identity, workspace_id=WORKSPACE_ID, fencing_generation=seeded.generation
    ) as writer:
        for index in range(1, 202):
            queued_submission_id = f"cmd-gb05-overflow-{index}.sub"
            writer.append_queued_submission(
                queued_submission_id=queued_submission_id,
                conversation_id=CONVERSATION_ID,
                actor_id=PRINCIPAL,
                queue_sequence=index,
                branch_id=BRANCH_ID,
                editable_parts=({"type": "text", "visibility": "standard", "payload": {"text": "x"}},),
                references=(),
                idempotency_key=f"cmd-gb05-overflow-{index}",
                created_at_us=BASE_US + index,
                updated_at_us=BASE_US + index,
            )
            writer.insert_queue_order_projection(
                queued_submission_id=queued_submission_id,
                conversation_id=CONVERSATION_ID,
                queue_position=index,
                updated_by_actor_id=PRINCIPAL,
                updated_at_us=BASE_US + index,
            )

    with pytest.raises(OperationError) as excinfo:
        resolve_chat_snapshot(
            seeded.connection,
            _snapshot_input("req-gb05-overflow"),
            _snapshot_context("req-gb05-overflow"),
        )
    assert excinfo.value.code == "size_limit_exceeded"


# --- 9b. The queued projection carries its created job correlation ------------------


def test_an_enqueued_row_carries_no_job_correlation_until_one_exists(seeded: m1.Owned) -> None:
    _enqueue(seeded)
    _give_principal_a_view_state(seeded)

    entries = chat.read_active_queue_for_actor(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        actor_id=PRINCIPAL,
    )
    assert [entry.generation_job_id for entry in entries] == [None]

    snapshot = _snapshot(seeded, request_id="req-gb05-precorrelation")
    row = snapshot["queuedSubmissions"][0]
    assert row["state"] == "queued"
    assert "generationJobId" not in row
    assert _snapshot_schema_errors(snapshot) == []


def test_a_drained_row_stays_queued_but_names_the_job_it_was_handed_to(seeded: m1.Owned) -> None:
    """The distinction the actor-facing queue could not previously state: a drained
    row is left `queued` for the generation claimant to walk, so without the
    correlation a reconnecting consumer cannot tell it from work still awaiting
    submission and risks a duplicate drain.
    """
    _enqueue(seeded, text="drain me")
    drain = _dispatcher(seeded).dispatch(
        _request(
            _submit_command(
                command_id="cmd-gb05-correlated-drain",
                message_id="cmd-gb05-correlated-drain.msg",
                actorId=PRINCIPAL,
                editable_parts=({"type": "text", "payload": {"text": "drain me"}},),
                fromQueuedSubmissionId="cmd-gb05-enqueue.sub",
            ),
            idempotency_key="idem-gb05-correlated-drain",
            request_id="req-gb05-correlated-drain",
        )
    )
    assert isinstance(drain, SuccessResponseEnvelope), drain
    _give_principal_a_view_state(seeded)

    snapshot = _snapshot(seeded, request_id="req-gb05-correlated-snapshot")
    row = snapshot["queuedSubmissions"][0]
    assert row["queuedSubmissionId"] == "cmd-gb05-enqueue.sub"
    assert row["state"] == "queued"
    assert row["generationJobId"] == "cmd-gb05-enqueue.gen"
    assert _snapshot_schema_errors(snapshot) == []

    # And once the claimant takes the row, it leaves the active queue entirely --
    # unchanged by this correlation.
    claim_queued_generation(
        seeded.connection,
        seeded.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=seeded.generation,
        queued_submission_id="cmd-gb05-enqueue.sub",
        generation_job_id="cmd-gb05-enqueue.gen",
        generation_attempt_id="cmd-gb05-enqueue.attempt1",
        trigger_message_id="cmd-gb05-correlated-drain.msg",
        lease_owner="runner-gb05-correlated",
        now_us=BASE_US + 6_000_000,
    )
    claimed_snapshot = _snapshot(seeded, request_id="req-gb05-correlated-claimed")
    assert "queuedSubmissions" not in claimed_snapshot


def test_the_job_correlation_survives_a_restart_so_a_consumer_can_skip_a_second_submit(
    tmp_path,
) -> None:
    path = tmp_path / "gb05-correlation-restart.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    try:
        _seed_restart_workspace(holder)
        _give_principal_a_view_state(holder)
        _enqueue(holder, command_id="cmd-gb05-restart-drained", text="drain across restart")
        _enqueue(holder, command_id="cmd-gb05-restart-waiting", text="still waiting")
        drain = _dispatcher(holder).dispatch(
            _request(
                _submit_command(
                    command_id="cmd-gb05-restart-drain",
                    message_id="cmd-gb05-restart-drain.msg",
                    actorId=PRINCIPAL,
                    expected_head_message_id=ROOT_MESSAGE_ID,
                    expected_head_version=1,
                    editable_parts=({"type": "text", "payload": {"text": "drain across restart"}},),
                    fromQueuedSubmissionId="cmd-gb05-restart-drained.sub",
                ),
                idempotency_key="idem-gb05-restart-drain",
                request_id="req-gb05-restart-drain",
            )
        )
        assert isinstance(drain, SuccessResponseEnvelope), drain
    finally:
        holder.connection.close()

    reopened = m1.take_ownership(path)
    try:
        snapshot = _snapshot(reopened, request_id="req-gb05-restart-correlation")
        rows = {row["queuedSubmissionId"]: row for row in snapshot["queuedSubmissions"]}
        # Both rows are still `queued`; only the drained one names a job, which is
        # what lets a reconnecting consumer submit the second and skip the first.
        assert set(rows) == {"cmd-gb05-restart-drained.sub", "cmd-gb05-restart-waiting.sub"}
        assert rows["cmd-gb05-restart-drained.sub"]["state"] == "queued"
        assert (
            rows["cmd-gb05-restart-drained.sub"]["generationJobId"]
            == "cmd-gb05-restart-drained.gen"
        )
        assert "generationJobId" not in rows["cmd-gb05-restart-waiting.sub"]
        assert _snapshot_schema_errors(snapshot) == []
    finally:
        reopened.connection.close()


def test_a_foreign_or_underivable_job_never_becomes_a_correlation(seeded: m1.Owned) -> None:
    """The correlation is the exact `.sub` -> `.gen` replacement, verified against a
    real job in the same workspace, conversation and branch. A job on another branch
    is not this row's job, and a queue id this convention did not derive names no
    job at all -- even where an id-shaped match happens to exist.
    """
    _enqueue(seeded, command_id="cmd-gb05-foreign", text="foreign branch job")
    _give_principal_a_view_state(seeded)
    with chat.chat_writer(
        seeded.connection, seeded.identity, workspace_id=WORKSPACE_ID, fencing_generation=seeded.generation
    ) as writer:
        # The derived id exists -- on another branch of the same conversation.
        writer.append_branch(
            branch_id="branch-gb05-fork",
            conversation_id=CONVERSATION_ID,
            origin_kind="explicit_fork",
            initial_head_message_id=ROOT_MESSAGE_ID,
            created_by_actor_id=PRINCIPAL,
            created_at_us=BASE_US + 10,
            created_conversation_sequence=SEEDED_SEQUENCE,
            schema_version=1,
        )
        writer.append_generation_job(
            generation_job_id="cmd-gb05-foreign.gen",
            conversation_id=CONVERSATION_ID,
            branch_id="branch-gb05-fork",
            trigger_message_id=ROOT_MESSAGE_ID,
            graph_revision_observed=GRAPH_REVISION,
            idempotency_key="idem-gb05-foreign-branch-job",
            schema_version=1,
            created_at_us=BASE_US + 11,
            updated_at_us=BASE_US + 11,
        )
        # A queue id no `<source command id>.sub` derivation produced, beside a job
        # whose id merely looks like its continuation.
        writer.append_queued_submission(
            queued_submission_id="queue-gb05-underivable",
            conversation_id=CONVERSATION_ID,
            actor_id=PRINCIPAL,
            queue_sequence=2,
            branch_id=BRANCH_ID,
            editable_parts=({"type": "text", "visibility": "standard", "payload": {"text": "x"}},),
            references=(),
            idempotency_key="idem-gb05-underivable",
            created_at_us=BASE_US + 12,
            updated_at_us=BASE_US + 12,
        )
        writer.insert_queue_order_projection(
            queued_submission_id="queue-gb05-underivable",
            conversation_id=CONVERSATION_ID,
            queue_position=2,
            updated_by_actor_id=PRINCIPAL,
            updated_at_us=BASE_US + 12,
        )
        writer.append_generation_job(
            generation_job_id="queue-gb05-underivable.gen",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            trigger_message_id=ROOT_MESSAGE_ID,
            graph_revision_observed=GRAPH_REVISION,
            idempotency_key="idem-gb05-underivable-job",
            schema_version=1,
            created_at_us=BASE_US + 13,
            updated_at_us=BASE_US + 13,
        )

    entries = chat.read_active_queue_for_actor(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        actor_id=PRINCIPAL,
    )
    assert [entry.submission.queued_submission_id for entry in entries] == [
        "cmd-gb05-foreign.sub",
        "queue-gb05-underivable",
    ]
    assert [entry.generation_job_id for entry in entries] == [None, None]

    snapshot = _snapshot(seeded, request_id="req-gb05-foreign-correlation")
    assert all("generationJobId" not in row for row in snapshot["queuedSubmissions"])
    assert _snapshot_schema_errors(snapshot) == []


# --- 10. F2b accepts the exact queued_submission item and rejects the rest ----------


def _queued_submission_payload() -> dict[str, Any]:
    return {
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "actorId": PRINCIPAL,
        "queuedSubmissionId": "cmd-gb05-bridge.sub",
        "branchId": BRANCH_ID,
        "editableParts": [{"type": "text", "payload": {"text": "hello"}}],
        "attachmentReferences": [],
        "contextReferences": [],
        "state": "queued",
        "version": 1,
        "position": 1,
        "orderVersion": 1,
        "createdAt": "2026-08-20T10:00:00Z",
        "updatedAt": "2026-08-20T10:00:00Z",
    }


def test_f2b_snapshot_item_accepts_the_exact_queued_submission_payload() -> None:
    item = {"itemKind": "queued_submission", "payload": _queued_submission_payload()}
    assert _schema_errors(_BRIDGE_SNAPSHOT_ITEM_REF, item) == []


def test_f2b_snapshot_item_rejects_a_mismatched_payload_for_queued_submission() -> None:
    item = {
        "itemKind": "queued_submission",
        "payload": {
            "workspaceId": WORKSPACE_ID,
            "conversationId": CONVERSATION_ID,
            "messageId": ROOT_MESSAGE_ID,
            "role": "user",
            "authorType": "human",
            "conversationSequence": 1,
            "schemaVersion": 1,
            "contentHash": "a" * 64,
            "completionStatus": "complete",
            "visibility": "standard",
            "parts": [],
            "createdAt": "2026-08-20T10:00:00Z",
            "committedAt": "2026-08-20T10:00:00Z",
        },
    }
    assert _schema_errors(_BRIDGE_SNAPSHOT_ITEM_REF, item) != []


def test_f2b_snapshot_item_rejects_an_unknown_item_kind() -> None:
    item = {"itemKind": "not_a_real_kind", "payload": _queued_submission_payload()}
    assert _schema_errors(_BRIDGE_SNAPSHOT_ITEM_REF, item) != []


def test_f2b_snapshot_item_queued_submission_keeps_carrier_bounds() -> None:
    """A within-schema-maxItems payload still validates; the further F2b outer-carrier
    byte ceiling (freeze §12.0.2) is a transport-level check this widening does not
    touch, already exercised end-to-end by
    `tests/chat_contract/test_fixtures.py::test_every_fixture_meets_its_governed_expectation`
    (`f2b_encoded_envelope_within_carrier_bound`)."""
    payload = _queued_submission_payload()
    payload["editableParts"] = [{"type": "text", "payload": {"text": "x" * 200}} for _ in range(4096)]
    item = {"itemKind": "queued_submission", "payload": payload}
    assert _schema_errors(_BRIDGE_SNAPSHOT_ITEM_REF, item) == []
