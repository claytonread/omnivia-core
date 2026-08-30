"""W5 GB-01: production `SubmitMessage` behind the public `chat.command` seam."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
import test_application_audit_idempotency_migration as m1
import test_v06_5_s0_mutation_foundation as s0
from jsonschema import Draft202012Validator
from omnivia_core_runtime.ownership.identity import FakeClock, SystemClock
from omnivia_core_runtime.service import chat_provider_route
from omnivia_core_runtime.service.application import (
    CHAT_FAMILY_PURPOSES,
    build_chat_application_dispatcher,
)
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.chat_generation import (
    GenerationConflict,
    claim_queued_generation,
    renew_generation_lease,
)
from omnivia_core_runtime.service.chat_generation_executor import (
    ChatGenerationExecutor,
    GenerationExecutorConfig,
)
from omnivia_core_runtime.service.chat_submit import (
    resolve_chat_command,
    retry_generation_attempt_id,
)
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.main import _default_chat_generation
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    RequestEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
)

OPERATION = "chat.command"
ENTRY = get_operation_metadata(OPERATION)

WORKSPACE_ID = m1.WORKSPACE_ID
INSTALLATION_ID = s0.INSTALLATION_ID
PRINCIPAL = s0.PRINCIPAL
CONVERSATION_ID = "conv-gb01-01"
BRANCH_ID = "branch-gb01-main"
ACTOR_ID = "actor-gb01-human"
ROOT_MESSAGE_ID = "msg-gb01-root"
ROOT_PART_ID = "part-gb01-root-0"
GRAPH_REVISION = 1
SEEDED_SEQUENCE = 1

#: The cold start: `CreateConversation`, then the branchless first `SubmitMessage`.
#: Every server-owned identity below is derived from the caller's own `commandId`
#: exactly as `service/chat_submit.py` derives it, so a test that spells one wrong
#: fails rather than quietly asserting the resolver's own arithmetic back at itself.
CREATE_COMMAND_ID = "cmd-cold-create"
COLD_CONVERSATION_ID = f"{CREATE_COMMAND_ID}.conv"
FIRST_SEND_COMMAND_ID = "cmd-cold-first"
COLD_BRANCH_ID = f"{FIRST_SEND_COMMAND_ID}.br"
COLD_ROOT_MESSAGE_ID = f"{FIRST_SEND_COMMAND_ID}.msg"
BASE_US = 2_600_000_000_000_000
WALL = datetime.fromtimestamp((BASE_US + 1_000_000) / 1_000_000, tz=UTC)

#: The canonical Chat Contract v1 schema bundle, loaded the way
#: `test_chat_snapshot.py` and `tests/chat_contract/test_fixtures.py` do:
#: `jsonschema`/`referencing` are development-only test dependencies.
_SCHEMAS_DIR = (
    Path(__file__).resolve().parents[5] / "contracts" / "chat" / "v1" / "schemas"
)
_SCHEMA_REGISTRY = Registry().with_resources(
    (schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012))
    for schema in (
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(_SCHEMAS_DIR.glob("*.schema.json"))
    )
)
_SUBMIT_MESSAGE_REF = (
    "https://contracts.omnivia.dev/chat/v1/commands.schema.json#/$defs/SubmitMessageRequest"
)
_CREATE_CONVERSATION_REF = (
    "https://contracts.omnivia.dev/chat/v1/"
    "commands.schema.json#/$defs/CreateConversationRequest"
)
_SNAPSHOT_RESULT_REF = (
    "https://contracts.omnivia.dev/chat/v1/"
    "queries.schema.json#/$defs/ConversationSnapshotResult"
)


def _schema_errors(ref: str, document: Any) -> list[str]:
    validator = Draft202012Validator({"$ref": ref}, registry=_SCHEMA_REGISTRY)
    return [error.message for error in validator.iter_errors(document)]


def _snapshot_schema_errors(document: Any) -> list[str]:
    return _schema_errors(_SNAPSHOT_RESULT_REF, document)


CHAT_TABLES = (
    "omnivia_chat_conversations",
    "omnivia_chat_messages",
    "omnivia_chat_message_parts",
    "omnivia_chat_message_derivations",
    "omnivia_chat_message_branches",
    "omnivia_chat_branch_head_events",
    "omnivia_chat_conversation_view_states",
    "omnivia_chat_drafts",
    "omnivia_chat_queued_submissions",
    "omnivia_chat_generation_jobs",
    "omnivia_chat_generation_attempts",
    "omnivia_chat_generation_events",
    "omnivia_chat_generation_job_status_projection",
    "omnivia_chat_generation_attempt_outcomes",
    "omnivia_chat_generation_text_chunks",
    "omnivia_chat_queue_order_projection",
    "omnivia_chat_transactional_outbox",
)
COUNTED_TABLES = s0.DURABLE_TABLES + CHAT_TABLES


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


@pytest.fixture
def seeded(owned: m1.Owned) -> m1.Owned:
    with chat.chat_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        writer.append_conversation(
            conversation_id=CONVERSATION_ID,
            state="active",
            graph_revision=GRAPH_REVISION,
            latest_conversation_sequence=0,
            schema_version=1,
            created_by_actor_id=ACTOR_ID,
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
            author_id=ACTOR_ID,
            conversation_sequence=SEEDED_SEQUENCE,
            schema_version=1,
            content_hash=_digest("a"),
            completion_status="complete",
            visibility="standard",
            created_at_us=BASE_US + 2,
            committed_at_us=BASE_US + 2,
        )
        writer.append_message_part(
            part_id=ROOT_PART_ID,
            message_id=ROOT_MESSAGE_ID,
            conversation_id=CONVERSATION_ID,
            part_index=0,
            part_type="text",
            schema_version=1,
            visibility="standard",
            payload={"text": "hello"},
            content_hash=_digest("b"),
            created_at_us=BASE_US + 3,
            provenance="human",
        )
        writer.append_branch(
            branch_id=BRANCH_ID,
            conversation_id=CONVERSATION_ID,
            origin_kind="original",
            initial_head_message_id=ROOT_MESSAGE_ID,
            created_by_actor_id=ACTOR_ID,
            created_at_us=BASE_US + 4,
            created_conversation_sequence=SEEDED_SEQUENCE,
            schema_version=1,
        )
        writer.append_branch_head_event(
            event_id="head-event-gb01-1",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            head_version=1,
            previous_head_message_id=None,
            new_head_message_id=ROOT_MESSAGE_ID,
            cause="branch_created",
            command_id="command-gb01-seed",
            graph_revision=GRAPH_REVISION,
            conversation_sequence=SEEDED_SEQUENCE,
            actor_id=ACTOR_ID,
            occurred_at_us=BASE_US + 4,
            schema_version=1,
        )
    return owned


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _counts(holder: m1.Owned) -> dict[str, int]:
    return {table: m1.count(holder.connection, table) for table in COUNTED_TABLES}


def _fallback() -> Dispatcher:
    return Dispatcher.for_service_operations(
        Grant(
            principal=PRINCIPAL,
            workspaces=frozenset({WORKSPACE_ID}),
            operations=frozenset(SERVICE_OPERATIONS),
        )
    )


_UNSET = object()


def _dispatcher(
    holder: m1.Owned,
    *,
    resolve_command: object = _UNSET,
    execute_generation: object = _UNSET,
) -> Any:
    fields: dict[str, Any] = {
        "service": holder,
        "principal_id": PRINCIPAL,
        "installation_id": INSTALLATION_ID,
        "workspace_id": WORKSPACE_ID,
        "fallback": _fallback(),
        "clock": FakeClock(wall=WALL),
        "execute_generation": (
            (lambda **_fields: None)
            if execute_generation is _UNSET
            else execute_generation
        ),
    }
    if resolve_command is not _UNSET:
        fields["resolve_command"] = resolve_command
    return build_chat_application_dispatcher(**fields)


def _submit_command(
    *,
    command_id: str = "cmd-gb01-submit",
    message_id: str = "msg-gb01-submitted",
    workspace_id: str = WORKSPACE_ID,
    expected_head_message_id: str = ROOT_MESSAGE_ID,
    expected_head_version: int = 1,
    editable_parts: tuple[Mapping[str, Any], ...] | None = None,
    attachment_references: tuple[Mapping[str, Any], ...] = (),
    context_references: tuple[Mapping[str, Any], ...] = (),
    **extra: Any,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "protocolVersion": "1.0",
        "commandId": command_id,
        "workspaceId": workspace_id,
        "conversationId": CONVERSATION_ID,
        "branchId": BRANCH_ID,
        "actorId": ACTOR_ID,
        "expectedHeadMessageId": expected_head_message_id,
        "expectedHeadVersion": expected_head_version,
        "newMessageId": message_id,
        "editableParts": list(
            editable_parts
            if editable_parts is not None
            else ({"type": "text", "payload": {"text": "hello from GB-01"}},)
        ),
        "attachmentReferences": list(attachment_references),
        "contextReferences": list(context_references),
    }
    command.update(extra)
    return command


def _reorder_command(
    *,
    command_id: str = "cmd-gb01-reorder",
    queued_submission_id: str = "cmd-gb01-submit.sub",
    target_position: int = 1,
    expected_version: int = 1,
    workspace_id: str = WORKSPACE_ID,
    conversation_id: str = CONVERSATION_ID,
    actor_id: str = ACTOR_ID,
    **extra: Any,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "protocolVersion": "1.0",
        "commandId": command_id,
        "workspaceId": workspace_id,
        "conversationId": conversation_id,
        "actorId": actor_id,
        "queuedSubmissionId": queued_submission_id,
        "targetPosition": target_position,
        "expectedVersion": expected_version,
    }
    command.update(extra)
    return command


def _cancel_queue_command(
    *,
    command_id: str = "cmd-gb01-cancel-queue",
    queued_submission_id: str = "cmd-gb01-submit.sub",
    workspace_id: str = WORKSPACE_ID,
    conversation_id: str = CONVERSATION_ID,
    actor_id: str = ACTOR_ID,
    **extra: Any,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "protocolVersion": "1.0",
        "commandId": command_id,
        "workspaceId": workspace_id,
        "conversationId": conversation_id,
        "actorId": actor_id,
        "queuedSubmissionId": queued_submission_id,
    }
    command.update(extra)
    return command


def _stop_generation_command(
    *,
    command_id: str = "cmd-gb01-stop-generation",
    job_id: str = "cmd-gb01-submit.gen",
    workspace_id: str = WORKSPACE_ID,
    conversation_id: str = CONVERSATION_ID,
    actor_id: str = ACTOR_ID,
    **extra: Any,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "protocolVersion": "1.0",
        "commandId": command_id,
        "workspaceId": workspace_id,
        "conversationId": conversation_id,
        "actorId": actor_id,
        "jobId": job_id,
    }
    command.update(extra)
    return command


def _retry_generation_command(
    *,
    command_id: str = "cmd-gb01-retry-generation",
    job_id: str = "cmd-gb01-submit.gen",
    workspace_id: str = WORKSPACE_ID,
    conversation_id: str = CONVERSATION_ID,
    actor_id: str = ACTOR_ID,
    **extra: Any,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "protocolVersion": "1.0",
        "commandId": command_id,
        "workspaceId": workspace_id,
        "conversationId": conversation_id,
        "actorId": actor_id,
        "jobId": job_id,
    }
    command.update(extra)
    return command


def _create_conversation_command(
    *,
    command_id: str = CREATE_COMMAND_ID,
    workspace_id: str = WORKSPACE_ID,
    actor_id: str = PRINCIPAL,
    **extra: Any,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "protocolVersion": "1.0",
        "commandId": command_id,
        "workspaceId": workspace_id,
        "actorId": actor_id,
        "requestedAt": "2052-05-22T14:13:20Z",
    }
    command.update(extra)
    return command


def _first_send_command(
    *,
    command_id: str = FIRST_SEND_COMMAND_ID,
    conversation_id: str = COLD_CONVERSATION_ID,
    workspace_id: str = WORKSPACE_ID,
    actor_id: str = PRINCIPAL,
    **extra: Any,
) -> dict[str, Any]:
    """A `SubmitMessageRequest` in the first-send variant: no expected-head group."""
    command: dict[str, Any] = {
        "protocolVersion": "1.0",
        "commandId": command_id,
        "workspaceId": workspace_id,
        "conversationId": conversation_id,
        "actorId": actor_id,
        "editableParts": [{"type": "text", "payload": {"text": "first send"}}],
        "attachmentReferences": [],
        "contextReferences": [],
    }
    command.update(extra)
    return command


def _request(
    command: Mapping[str, Any],
    *,
    command_name: str = "SubmitMessage",
    idempotency_key: str = "idem-gb01-submit",
    request_id: str = "req-gb01-submit",
    expected_sequence: int = SEEDED_SEQUENCE,
    expected_conversation: Any = _UNSET,
) -> RequestEnvelope:
    operation_input: dict[str, Any] = {
        "command_name": command_name,
        "command": dict(command),
    }
    if expected_conversation is _UNSET:
        operation_input["expected_conversation"] = {
            "conversation_id": CONVERSATION_ID,
            "graph_revision": GRAPH_REVISION,
            "latest_conversation_sequence": expected_sequence,
        }
    elif expected_conversation is not None:
        operation_input["expected_conversation"] = dict(expected_conversation)
    return s0.envelope_for(
        ENTRY,
        operation_input=operation_input,
        request_id=request_id,
        correlation_id=f"cor-{request_id}",
        trace_id=f"trc-{request_id}",
        idempotency_key=idempotency_key,
        purpose=CHAT_FAMILY_PURPOSES[OPERATION],
        workspace_id=WORKSPACE_ID,
    )


def _submit_queued(
    holder: m1.Owned,
    *,
    command_id: str,
    expected_head_message_id: str,
    expected_head_version: int,
    expected_sequence: int,
) -> SuccessResponseEnvelope:
    response = _dispatcher(holder).dispatch(
        _request(
            _submit_command(
                command_id=command_id,
                message_id=f"{command_id}.msg",
                expected_head_message_id=expected_head_message_id,
                expected_head_version=expected_head_version,
            ),
            idempotency_key=f"idem-{command_id}",
            request_id=f"req-{command_id}",
            expected_sequence=expected_sequence,
        )
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    return response


def test_gb01_production_chat_command_submits_message_and_opens_generation(
    seeded: m1.Owned,
) -> None:
    before = _counts(seeded)
    dispatcher = _dispatcher(seeded)

    response = dispatcher.dispatch(_request(_submit_command()))

    assert isinstance(response, SuccessResponseEnvelope), response
    assert response.result == {
        "command_name": "SubmitMessage",
        "command_result": {"commandId": "cmd-gb01-submit", "status": "completed"},
        "conversation_id": CONVERSATION_ID,
    }
    assert _counts(seeded) == {
        **before,
        "omnivia_application_audit_events": before["omnivia_application_audit_events"]
        + 1,
        "omnivia_idempotency_claims": before["omnivia_idempotency_claims"] + 1,
        "omnivia_idempotency_outcomes": before["omnivia_idempotency_outcomes"] + 1,
        s0.EXECUTIONS_TABLE: before[s0.EXECUTIONS_TABLE] + 1,
        "omnivia_chat_messages": before["omnivia_chat_messages"] + 1,
        "omnivia_chat_message_parts": before["omnivia_chat_message_parts"] + 1,
        "omnivia_chat_branch_head_events": before[
            "omnivia_chat_branch_head_events"
        ]
        + 1,
        "omnivia_chat_queued_submissions": before[
            "omnivia_chat_queued_submissions"
        ]
        + 1,
        "omnivia_chat_generation_jobs": before["omnivia_chat_generation_jobs"] + 1,
        "omnivia_chat_queue_order_projection": before[
            "omnivia_chat_queue_order_projection"
        ]
        + 1,
        "omnivia_chat_transactional_outbox": before[
            "omnivia_chat_transactional_outbox"
        ]
        + 1,
    }

    conversation = chat.read_conversation(
        seeded.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
    )
    assert conversation is not None
    assert conversation.graph_revision == GRAPH_REVISION
    assert conversation.latest_conversation_sequence == SEEDED_SEQUENCE + 1

    branch = chat.read_branch(
        seeded.connection, workspace_id=WORKSPACE_ID, branch_id=BRANCH_ID
    )
    assert branch is not None
    assert branch.head_version == 2
    assert branch.current_head_message_id == "msg-gb01-submitted"

    messages = chat.read_messages_by_conversation_sequence(
        seeded.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
    )
    assert [message.message_id for message in messages] == [
        ROOT_MESSAGE_ID,
        "msg-gb01-submitted",
    ]
    assert messages[-1].parent_message_id == ROOT_MESSAGE_ID
    assert messages[-1].role == "user"
    assert messages[-1].author_type == "human"

    parts = chat.read_message_parts(
        seeded.connection, workspace_id=WORKSPACE_ID, message_id="msg-gb01-submitted"
    )
    assert [(part.part_index, part.part_type, part.payload) for part in parts] == [
        (0, "text", {"text": "hello from GB-01"})
    ]

    queued = chat.read_queued_submission(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        queued_submission_id="cmd-gb01-submit.sub",
    )
    assert queued is not None
    assert queued.state == "queued"
    assert queued.queue_sequence == 1
    assert queued.idempotency_key == "cmd-gb01-submit"
    assert queued.submitted_message_id is None
    assert queued.submitted_generation_job_id is None

    order = chat.read_queue_order_projection(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        queued_submission_id="cmd-gb01-submit.sub",
    )
    assert order is not None
    assert order.conversation_id == CONVERSATION_ID
    assert order.queue_position == queued.queue_sequence
    assert order.version == 1
    assert order.updated_by_actor_id == ACTOR_ID

    job = chat.read_generation_job(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id="cmd-gb01-submit.gen",
    )
    assert job is not None
    assert job.state == "queued"
    assert job.trigger_message_id == "msg-gb01-submitted"
    assert job.current_attempt_id is None
    assert job.last_event_sequence == 0

    outbox = chat.read_outbox_event(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        domain_event_id="cmd-gb01-submit.evt",
    )
    assert outbox is not None
    assert outbox.event_kind == "chat.message.committed"
    assert outbox.generation_job_id == "cmd-gb01-submit.gen"
    assert outbox.payload == {
        "branchId": BRANCH_ID,
        "conversationId": CONVERSATION_ID,
        "conversationSequence": SEEDED_SEQUENCE + 1,
        "generationJobId": "cmd-gb01-submit.gen",
        "graphRevision": GRAPH_REVISION,
        "headVersion": 2,
        "messageId": "msg-gb01-submitted",
        "parentMessageId": ROOT_MESSAGE_ID,
        "queuedSubmissionId": "cmd-gb01-submit.sub",
        "role": "user",
    }
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_reorder_queued_submission_updates_authoritative_worker_order(
    seeded: m1.Owned,
) -> None:
    _submit_queued(
        seeded,
        command_id="cmd-gb01-q1",
        expected_head_message_id=ROOT_MESSAGE_ID,
        expected_head_version=1,
        expected_sequence=SEEDED_SEQUENCE,
    )
    _submit_queued(
        seeded,
        command_id="cmd-gb01-q2",
        expected_head_message_id="cmd-gb01-q1.msg",
        expected_head_version=2,
        expected_sequence=SEEDED_SEQUENCE + 1,
    )
    _submit_queued(
        seeded,
        command_id="cmd-gb01-q3",
        expected_head_message_id="cmd-gb01-q2.msg",
        expected_head_version=3,
        expected_sequence=SEEDED_SEQUENCE + 2,
    )

    response = _dispatcher(seeded).dispatch(
        _request(
            _reorder_command(
                command_id="cmd-gb01-q3-first",
                queued_submission_id="cmd-gb01-q3.sub",
                target_position=1,
                expected_version=1,
            ),
            command_name="ReorderQueuedSubmission",
            idempotency_key="idem-gb01-q3-first",
            request_id="req-gb01-q3-first",
            expected_sequence=SEEDED_SEQUENCE + 3,
        )
    )

    assert isinstance(response, SuccessResponseEnvelope), response
    assert response.result == {
        "command_name": "ReorderQueuedSubmission",
        "command_result": {"commandId": "cmd-gb01-q3-first", "status": "completed"},
        "conversation_id": CONVERSATION_ID,
    }
    assert [
        row.queued_submission_id
        for row in chat.read_queue_order_for_conversation(
            seeded.connection,
            workspace_id=WORKSPACE_ID,
            conversation_id=CONVERSATION_ID,
        )
    ] == ["cmd-gb01-q3.sub", "cmd-gb01-q1.sub", "cmd-gb01-q2.sub"]
    assert [
        row.queue_position
        for row in chat.read_queue_order_for_conversation(
            seeded.connection,
            workspace_id=WORKSPACE_ID,
            conversation_id=CONVERSATION_ID,
        )
    ] == [1, 2, 3]
    next_queued = chat.read_next_queued_submission(
        seeded.connection, workspace_id=WORKSPACE_ID
    )
    assert next_queued is not None
    assert next_queued.queued_submission_id == "cmd-gb01-q3.sub"
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_reorder_queued_submission_stale_version_conflicts_without_writes(
    seeded: m1.Owned,
) -> None:
    _submit_queued(
        seeded,
        command_id="cmd-gb01-stale-order",
        expected_head_message_id=ROOT_MESSAGE_ID,
        expected_head_version=1,
        expected_sequence=SEEDED_SEQUENCE,
    )
    before = _counts(seeded)

    response = _dispatcher(seeded).dispatch(
        _request(
            _reorder_command(
                command_id="cmd-gb01-stale-order-reorder",
                queued_submission_id="cmd-gb01-stale-order.sub",
                target_position=2,
                expected_version=2,
            ),
            command_name="ReorderQueuedSubmission",
            idempotency_key="idem-gb01-stale-order-reorder",
            request_id="req-gb01-stale-order-reorder",
            expected_sequence=SEEDED_SEQUENCE + 1,
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before
    order = chat.read_queue_order_projection(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        queued_submission_id="cmd-gb01-stale-order.sub",
    )
    assert order is not None
    assert order.queue_position == 1
    assert order.version == 1


def test_cancel_queued_submission_marks_only_a_pending_submission_cancelled(
    seeded: m1.Owned,
) -> None:
    command_id = "c" * 128
    _submit_queued(
        seeded,
        command_id="cmd-gb01-cancellable",
        expected_head_message_id=ROOT_MESSAGE_ID,
        expected_head_version=1,
        expected_sequence=SEEDED_SEQUENCE,
    )

    response = _dispatcher(seeded).dispatch(
        _request(
            _cancel_queue_command(
                command_id=command_id,
                queued_submission_id="cmd-gb01-cancellable.sub",
            ),
            command_name="CancelQueuedSubmission",
            idempotency_key="idem-gb01-cancel-cancellable",
            request_id="req-gb01-cancel-cancellable",
            expected_sequence=SEEDED_SEQUENCE + 1,
        )
    )

    assert isinstance(response, SuccessResponseEnvelope), response
    assert response.result == {
        "command_name": "CancelQueuedSubmission",
        "command_result": {
            "commandId": command_id,
            "status": "completed",
        },
        "conversation_id": CONVERSATION_ID,
    }
    queued = chat.read_queued_submission(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        queued_submission_id="cmd-gb01-cancellable.sub",
    )
    assert queued is not None
    assert queued.state == "cancelled"
    assert queued.version == 2
    assert queued.claimed_by is None
    assert queued.submitted_generation_job_id is None
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_cancel_queued_submission_conflicts_after_submission_claim(
    seeded: m1.Owned,
) -> None:
    _submit_queued(
        seeded,
        command_id="cmd-gb01-submitted-cancel",
        expected_head_message_id=ROOT_MESSAGE_ID,
        expected_head_version=1,
        expected_sequence=SEEDED_SEQUENCE,
    )
    claim_queued_generation(
        seeded.connection,
        seeded.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=seeded.generation,
        queued_submission_id="cmd-gb01-submitted-cancel.sub",
        generation_job_id="cmd-gb01-submitted-cancel.gen",
        generation_attempt_id="cmd-gb01-submitted-cancel.attempt1",
        trigger_message_id="cmd-gb01-submitted-cancel.msg",
        lease_owner="runner-gb01",
        now_us=BASE_US + 2_000_000,
    )
    before = _counts(seeded)

    response = _dispatcher(seeded).dispatch(
        _request(
            _cancel_queue_command(
                command_id="cmd-gb01-cancel-submitted",
                queued_submission_id="cmd-gb01-submitted-cancel.sub",
            ),
            command_name="CancelQueuedSubmission",
            idempotency_key="idem-gb01-cancel-submitted",
            request_id="req-gb01-cancel-submitted",
            expected_sequence=SEEDED_SEQUENCE + 1,
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before
    queued = chat.read_queued_submission(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        queued_submission_id="cmd-gb01-submitted-cancel.sub",
    )
    assert queued is not None
    assert queued.state == "submitted"


def test_cancel_missing_queued_submission_conflicts_without_writes(
    seeded: m1.Owned,
) -> None:
    before = _counts(seeded)

    response = _dispatcher(seeded).dispatch(
        _request(
            _cancel_queue_command(
                command_id="cmd-gb01-cancel-missing",
                queued_submission_id="missing-gb01-submission",
            ),
            command_name="CancelQueuedSubmission",
            idempotency_key="idem-gb01-cancel-missing",
            request_id="req-gb01-cancel-missing",
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before


def test_stop_generation_cancels_a_running_job_and_records_successor_evidence(
    seeded: m1.Owned,
) -> None:
    _submit_queued(
        seeded,
        command_id="cmd-gb01-stoppable",
        expected_head_message_id=ROOT_MESSAGE_ID,
        expected_head_version=1,
        expected_sequence=SEEDED_SEQUENCE,
    )
    claim_queued_generation(
        seeded.connection,
        seeded.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=seeded.generation,
        queued_submission_id="cmd-gb01-stoppable.sub",
        generation_job_id="cmd-gb01-stoppable.gen",
        generation_attempt_id="cmd-gb01-stoppable.attempt1",
        trigger_message_id="cmd-gb01-stoppable.msg",
        lease_owner="runner-gb01",
        now_us=BASE_US + 2_000_000,
    )

    response = _dispatcher(seeded).dispatch(
        _request(
            _stop_generation_command(
                command_id="cmd-gb01-stop-stoppable",
                job_id="cmd-gb01-stoppable.gen",
            ),
            command_name="StopGeneration",
            idempotency_key="idem-gb01-stop-stoppable",
            request_id="req-gb01-stop-stoppable",
            expected_sequence=SEEDED_SEQUENCE + 1,
        )
    )

    assert isinstance(response, SuccessResponseEnvelope), response
    assert response.result == {
        "command_name": "StopGeneration",
        "command_result": {"commandId": "cmd-gb01-stop-stoppable", "status": "completed"},
        "conversation_id": CONVERSATION_ID,
    }
    job = chat.read_generation_job(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id="cmd-gb01-stoppable.gen",
    )
    assert job is not None
    assert job.state == "cancelled"
    assert job.current_attempt_id == "cmd-gb01-stoppable.attempt1"
    assert job.last_event_sequence == 2
    assert job.sanitized_error_code == "cancelled"
    assert job.finished_at_us is not None
    assert [
        (event.event_type, event.generation_event_sequence, dict(event.payload))
        for event in chat.read_generation_events(
            seeded.connection,
            workspace_id=WORKSPACE_ID,
            generation_job_id="cmd-gb01-stoppable.gen",
        )
    ] == [
        (
            "chat.generation.queued",
            1,
            {
                "attemptNumber": 1,
                "queuedSubmissionId": "cmd-gb01-stoppable.sub",
            },
        ),
        (
            "chat.generation.cancelled",
            2,
            {
                "finishReason": "cancelled",
                "stoppedByActorId": ACTOR_ID,
            },
        ),
    ]
    outcomes = chat.read_generation_attempt_outcomes(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id="cmd-gb01-stoppable.gen",
    )
    assert len(outcomes) == 1
    assert outcomes[0].generation_attempt_id == "cmd-gb01-stoppable.attempt1"
    assert outcomes[0].terminal_state == "cancelled"
    assert outcomes[0].retryable is False
    assert outcomes[0].sanitized_error_code == "cancelled"
    status = chat.read_generation_job_status_projection(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id="cmd-gb01-stoppable.gen",
    )
    assert status is not None
    assert status.state == "cancelled"
    assert status.current_attempt_id == "cmd-gb01-stoppable.attempt1"
    assert status.sanitized_error_code == "cancelled"
    assert status.finished_at_us == job.finished_at_us
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_stop_generation_terminal_job_conflicts_without_writes(
    seeded: m1.Owned,
) -> None:
    _submit_queued(
        seeded,
        command_id="cmd-gb01-terminal-stop",
        expected_head_message_id=ROOT_MESSAGE_ID,
        expected_head_version=1,
        expected_sequence=SEEDED_SEQUENCE,
    )
    claim_queued_generation(
        seeded.connection,
        seeded.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=seeded.generation,
        queued_submission_id="cmd-gb01-terminal-stop.sub",
        generation_job_id="cmd-gb01-terminal-stop.gen",
        generation_attempt_id="cmd-gb01-terminal-stop.attempt1",
        trigger_message_id="cmd-gb01-terminal-stop.msg",
        lease_owner="runner-gb01",
        now_us=BASE_US + 2_000_000,
    )
    assert isinstance(
        _dispatcher(seeded).dispatch(
            _request(
                _stop_generation_command(
                    command_id="cmd-gb01-stop-terminal-once",
                    job_id="cmd-gb01-terminal-stop.gen",
                ),
                command_name="StopGeneration",
                idempotency_key="idem-gb01-stop-terminal-once",
                request_id="req-gb01-stop-terminal-once",
                expected_sequence=SEEDED_SEQUENCE + 1,
            )
        ),
        SuccessResponseEnvelope,
    )
    before = _counts(seeded)

    response = _dispatcher(seeded).dispatch(
        _request(
            _stop_generation_command(
                command_id="cmd-gb01-stop-terminal-twice",
                job_id="cmd-gb01-terminal-stop.gen",
            ),
            command_name="StopGeneration",
            idempotency_key="idem-gb01-stop-terminal-twice",
            request_id="req-gb01-stop-terminal-twice",
            expected_sequence=SEEDED_SEQUENCE + 1,
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before
    assert [
        event.event_type
        for event in chat.read_generation_events(
            seeded.connection,
            workspace_id=WORKSPACE_ID,
            generation_job_id="cmd-gb01-terminal-stop.gen",
        )
    ] == ["chat.generation.queued", "chat.generation.cancelled"]


def test_stop_missing_generation_conflicts_without_writes(seeded: m1.Owned) -> None:
    before = _counts(seeded)

    response = _dispatcher(seeded).dispatch(
        _request(
            _stop_generation_command(
                command_id="cmd-gb01-stop-missing",
                job_id="missing-gb01-generation",
            ),
            command_name="StopGeneration",
            idempotency_key="idem-gb01-stop-missing",
            request_id="req-gb01-stop-missing",
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before


def test_retry_generation_opens_successor_attempt_and_executes_provider(
    seeded: m1.Owned,
) -> None:
    first = _run_executor(
        seeded,
        FakeClock(wall=WALL),
        lambda _request: (),
        _executor_config(connection_id=""),
        "cmd-gb01-retryable",
    )
    assert first is not None
    assert first.state == "failed"
    original_attempt_id = first.current_attempt_id
    assert original_attempt_id is not None
    status = chat.read_generation_job_status_projection(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=first.generation_job_id,
    )
    assert status is not None
    assert status.state == "retryable"
    assert status.current_attempt_id == original_attempt_id
    assert status.sanitized_error_code == "provider-unavailable"
    assert status.finished_at_us is None

    calls: list[Any] = []

    def provider(request: Any) -> Iterator[Any]:
        calls.append(request)
        for event in _stream(request, ("retried answer",)):
            # Only the delta carries a provider event id, so the durable text event's
            # payload is the one place `providerEventId` has to appear.
            yield (
                {**event, "providerEventId": "provider-retry-delta-1"}
                if event["eventType"] == "text-delta"
                else event
            )

    clock = FakeClock(wall=WALL)
    executor = ChatGenerationExecutor(
        connection=seeded.connection,
        identity=seeded.identity,
        fencing_generation=seeded.generation,
        workspace_id=WORKSPACE_ID,
        clock=clock,
        invoke=provider,
        config=_executor_config(),
    )
    retry_command_id = "cmd-gb01-retry-after-route"
    expected_attempt_id = retry_generation_attempt_id(
        retry_command_id, first.generation_job_id
    )

    response = build_chat_application_dispatcher(
        service=seeded,
        principal_id=PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        fallback=_fallback(),
        clock=clock,
        execute_generation=executor.execute,
    ).dispatch(
        _request(
            _retry_generation_command(
                command_id=retry_command_id,
                job_id=first.generation_job_id,
            ),
            command_name="RetryGeneration",
            idempotency_key="idem-gb01-retry-after-route",
            request_id="req-gb01-retry-after-route",
            expected_sequence=SEEDED_SEQUENCE + 1,
        )
    )

    assert isinstance(response, SuccessResponseEnvelope), response
    assert response.result == {
        "command_name": "RetryGeneration",
        "command_result": {
            "commandId": "cmd-gb01-retry-after-route",
            "status": "completed",
        },
        "conversation_id": CONVERSATION_ID,
    }
    assert len(calls) == 1
    assert calls[0].job_id == first.generation_job_id
    assert calls[0].attempt_id == expected_attempt_id
    attempts = chat.read_generation_attempts(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=first.generation_job_id,
    )
    assert [
        (attempt.generation_attempt_id, attempt.attempt_number, attempt.retry_of_attempt_id)
        for attempt in attempts
    ] == [
        (original_attempt_id, 1, None),
        (expected_attempt_id, 2, original_attempt_id),
    ]
    outcomes = chat.read_generation_attempt_outcomes(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=first.generation_job_id,
    )
    assert [
        (outcome.generation_attempt_id, outcome.terminal_state, outcome.retryable)
        for outcome in outcomes
    ] == [
        (original_attempt_id, "failed", True),
        (expected_attempt_id, "succeeded", False),
    ]
    updated_status = chat.read_generation_job_status_projection(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=first.generation_job_id,
    )
    assert updated_status is not None
    assert updated_status.state == "succeeded"
    assert updated_status.current_attempt_id == expected_attempt_id
    assert updated_status.result_message_id is not None
    parts = chat.read_message_parts(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        message_id=updated_status.result_message_id,
    )
    assert [dict(part.payload) for part in parts] == [{"text": "retried answer"}]
    events = chat.read_generation_events(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=first.generation_job_id,
    )
    # The successor attempt's delta is durable in its own right, and ordered before
    # the terminal event that ends the attempt.
    assert [(event.event_type, event.generation_attempt_id) for event in events] == [
        ("chat.generation.queued", None),
        ("chat.generation.failed", original_attempt_id),
        ("chat.generation.started", expected_attempt_id),
        ("chat.generation.text_appended", expected_attempt_id),
        ("chat.generation.succeeded", expected_attempt_id),
    ]
    assert [event.generation_event_sequence for event in events] == [1, 2, 3, 4, 5]
    # The text event takes a sequence of its own, so the terminal event that follows
    # it sits one further along than it would have without it -- and the successor
    # attempt's whole stream stays contiguous across the retry.
    assert events[-1].generation_event_sequence == 5
    retried_job = chat.read_generation_job(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=first.generation_job_id,
    )
    assert retried_job is not None
    # `last_event_sequence` on the job row is deliberately NOT 5. 0029 never reopens a
    # terminal job, so the row has been frozen at the first attempt's failure since
    # sequence 2; the status projection is the live authority across a retry, and it
    # is the one that moved. The text append respects that boundary exactly as the
    # terminal append does -- neither writes the frozen row.
    assert retried_job.state == "failed"
    assert retried_job.last_event_sequence == 2
    # The wire carried a `providerEventId`, so the payload carries it -- and still
    # nothing else. `chunkOrdinal` restarts at zero for the successor attempt.
    assert dict(events[3].payload) == {
        "providerEventType": "text-delta",
        "providerEventId": "provider-retry-delta-1",
        "chunkOrdinal": 0,
    }
    assert events[3].provider_event_id == "provider-retry-delta-1"
    assert events[3].result_message_id is None
    assert "retried answer" not in str(
        seeded.connection.execute(
            "SELECT payload_json FROM omnivia_chat_generation_events "
            "WHERE workspace_id = ? AND generation_job_id = ?",
            (WORKSPACE_ID, first.generation_job_id),
        ).fetchall()
    )
    assert [
        (chunk.generation_attempt_id, chunk.chunk_ordinal, chunk.text_content)
        for chunk in chat.read_generation_text_chunks(
            seeded.connection,
            workspace_id=WORKSPACE_ID,
            generation_job_id=first.generation_job_id,
        )
    ] == [(expected_attempt_id, 0, "retried answer")]
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_retry_generation_non_retryable_job_conflicts_without_writes(
    seeded: m1.Owned,
) -> None:
    job = _run_executor(
        seeded,
        FakeClock(wall=WALL),
        lambda _request: (_ for _ in ()).throw(RuntimeError("secret")),
        _executor_config(),
        "cmd-gb01-nonretryable",
    )
    assert job is not None
    before = _counts(seeded)

    response = _dispatcher(seeded).dispatch(
        _request(
            _retry_generation_command(
                command_id="cmd-gb01-retry-nonretryable",
                job_id=job.generation_job_id,
            ),
            command_name="RetryGeneration",
            idempotency_key="idem-gb01-retry-nonretryable",
            request_id="req-gb01-retry-nonretryable",
            expected_sequence=SEEDED_SEQUENCE + 1,
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before


def test_retry_generation_refuses_before_writing_when_executor_is_absent(
    seeded: m1.Owned,
) -> None:
    job = _run_executor(
        seeded,
        FakeClock(wall=WALL),
        lambda _request: (),
        _executor_config(connection_id=""),
        "cmd-gb01-retry-without-executor",
    )
    assert job is not None
    before = _counts(seeded)

    response = _dispatcher(seeded, execute_generation=None).dispatch(
        _request(
            _retry_generation_command(
                command_id="cmd-gb01-retry-no-executor",
                job_id=job.generation_job_id,
            ),
            command_name="RetryGeneration",
            idempotency_key="idem-gb01-retry-no-executor",
            request_id="req-gb01-retry-no-executor",
            expected_sequence=SEEDED_SEQUENCE + 1,
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "dependency_unavailable"
    assert _counts(seeded) == before


def test_gb01_submit_replay_returns_stored_result_without_duplicate_rows(
    seeded: m1.Owned,
) -> None:
    dispatcher = _dispatcher(seeded)
    request = _request(_submit_command())
    first = dispatcher.dispatch(request)
    after_first = _counts(seeded)

    second = dispatcher.dispatch(request)

    assert isinstance(first, SuccessResponseEnvelope), first
    assert isinstance(second, SuccessResponseEnvelope), second
    assert second.result == first.result
    assert _counts(seeded) == {
        **after_first,
        s0.EXECUTIONS_TABLE: after_first[s0.EXECUTIONS_TABLE] + 1,
    }
    assert [
        event.outbox_cursor
        for event in chat.read_outbox_events_since(
            seeded.connection, workspace_id=WORKSPACE_ID, after_cursor=0
        )
    ] == [1]


def test_gb01_unsupported_chat_commands_keep_dependency_refusal(
    seeded: m1.Owned,
) -> None:
    before = _counts(seeded)
    response = _dispatcher(seeded).dispatch(
        _request(
            {"protocolVersion": "1.0", "commandId": "cmd-gb01-rename"},
            command_name="RenameConversation",
            idempotency_key="idem-gb01-rename",
            request_id="req-gb01-rename",
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "dependency_unavailable"
    assert _counts(seeded) == before


def test_gb01_resolverless_wiring_still_refuses_submit_message(
    seeded: m1.Owned,
) -> None:
    before = _counts(seeded)
    response = _dispatcher(seeded, resolve_command=None).dispatch(
        _request(
            _submit_command(command_id="cmd-gb01-no-resolver"),
            idempotency_key="idem-gb01-no-resolver",
            request_id="req-gb01-no-resolver",
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "dependency_unavailable"
    assert _counts(seeded) == before


def test_submit_refuses_before_writing_when_generation_executor_is_absent(
    seeded: m1.Owned,
) -> None:
    before = _counts(seeded)

    response = _dispatcher(seeded, execute_generation=None).dispatch(
        _request(
            _submit_command(command_id="cmd-gb01-no-executor"),
            idempotency_key="idem-gb01-no-executor",
            request_id="req-gb01-no-executor",
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "dependency_unavailable"
    assert _counts(seeded) == before


def test_submit_runs_provider_and_materialises_assistant_message(
    seeded: m1.Owned,
) -> None:
    clock = FakeClock(wall=WALL)
    requests: list[Any] = []

    def provider(request: Any):
        requests.append(request)
        common = {
            "invocationId": request.invocation_id,
            "attemptId": request.attempt_id,
            "schemaVersion": 1,
            "occurredAt": "2052-05-23T00:00:01Z",
            "receivedAt": "2052-05-23T00:00:01Z",
        }
        yield {**common, "ordinal": 0, "eventType": "stream-start"}
        yield {
            **common,
            "ordinal": 1,
            "eventType": "text-delta",
            "partId": "provider-part-1",
            "stepId": "provider-step-1",
            "delta": "assistant reply",
        }
        yield {
            **common,
            "ordinal": 2,
            "eventType": "finish",
            "finishReason": "stop",
        }

    executor = ChatGenerationExecutor(
        connection=seeded.connection,
        identity=seeded.identity,
        fencing_generation=seeded.generation,
        workspace_id=WORKSPACE_ID,
        clock=clock,
        invoke=provider,
        config=GenerationExecutorConfig(
            connection_id="provider-connection-1",
            model_id="provider-model-1",
            policy_ref="policy-1",
            classification_ref="classification-1",
            residency_ref="residency-1",
            service_actor_id="actor-core-chat",
        ),
    )
    response = build_chat_application_dispatcher(
        service=seeded,
        principal_id=PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        fallback=_fallback(),
        clock=clock,
        execute_generation=executor.execute_submission,
    ).dispatch(_request(_submit_command()))

    assert isinstance(response, SuccessResponseEnvelope), response
    assert len(requests) == 1
    assert [message["role"] for message in requests[0].messages] == ["user", "user"]
    job = chat.read_generation_job(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id="cmd-gb01-submit.gen",
    )
    assert job is not None
    assert job.state == "succeeded"
    assert job.result_message_id is not None
    parts = chat.read_message_parts(
        seeded.connection, workspace_id=WORKSPACE_ID, message_id=job.result_message_id
    )
    assert [dict(part.payload) for part in parts] == [{"text": "assistant reply"}]
    events = chat.read_generation_events(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=job.generation_job_id,
    )
    # The delta is durable, and it is durable *before* the terminal event: a replay
    # that stopped at `succeeded` would otherwise have to invent the text it ordered.
    assert [event.event_type for event in events] == [
        "chat.generation.queued",
        "chat.generation.started",
        "chat.generation.text_appended",
        "chat.generation.succeeded",
    ]
    # The job's own projection of the stream counts the text event, so a reader that
    # resumes from `last_event_sequence` resumes after the delta rather than over it.
    assert [event.generation_event_sequence for event in events] == [1, 2, 3, 4]
    assert job.last_event_sequence == 4
    # Metadata only. `chunkOrdinal` names the chunk that holds the text; the text
    # itself never reaches `payload_json`. This provider event carries no
    # `providerEventId`, so the payload carries none either.
    assert dict(events[2].payload) == {
        "providerEventType": "text-delta",
        "chunkOrdinal": 0,
    }
    assert events[2].result_message_id is None
    assert "assistant reply" not in str(
        seeded.connection.execute(
            "SELECT payload_json FROM omnivia_chat_generation_events "
            "WHERE workspace_id = ? AND generation_job_id = ?",
            (WORKSPACE_ID, job.generation_job_id),
        ).fetchall()
    )
    assert job.current_attempt_id is not None
    chunks = chat.read_generation_text_chunks(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=job.generation_job_id,
        generation_attempt_id=job.current_attempt_id,
    )
    assert [chunk.text_content for chunk in chunks] == ["assistant reply"]
    assert [chunk.chunk_ordinal for chunk in chunks] == [0]
    outcomes = chat.read_generation_attempt_outcomes(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=job.generation_job_id,
    )
    assert len(outcomes) == 1
    assert outcomes[0].generation_attempt_id == job.current_attempt_id
    assert outcomes[0].terminal_state == "succeeded"
    assert outcomes[0].result_message_id == job.result_message_id
    assert outcomes[0].retryable is False
    status = chat.read_generation_job_status_projection(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=job.generation_job_id,
    )
    assert status is not None
    assert status.state == "succeeded"
    assert status.result_message_id == job.result_message_id
    assert status.current_attempt_id == job.current_attempt_id
    branch = chat.read_branch(
        seeded.connection, workspace_id=WORKSPACE_ID, branch_id=BRANCH_ID
    )
    assert branch is not None
    assert branch.current_head_message_id == job.result_message_id
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_provider_exception_becomes_sanitized_terminal_generation_failure(
    seeded: m1.Owned,
) -> None:
    clock = FakeClock(wall=WALL)

    def provider(_request: Any):
        raise RuntimeError("secret upstream response body")

    executor = ChatGenerationExecutor(
        connection=seeded.connection,
        identity=seeded.identity,
        fencing_generation=seeded.generation,
        workspace_id=WORKSPACE_ID,
        clock=clock,
        invoke=provider,
        config=GenerationExecutorConfig(
            connection_id="provider-connection-1",
            model_id="provider-model-1",
            policy_ref="policy-1",
            classification_ref="classification-1",
            residency_ref="residency-1",
            service_actor_id="actor-core-chat",
        ),
    )
    response = build_chat_application_dispatcher(
        service=seeded,
        principal_id=PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        fallback=_fallback(),
        clock=clock,
        execute_generation=executor.execute_submission,
    ).dispatch(
        _request(
            _submit_command(command_id="cmd-gb01-provider-failure"),
            idempotency_key="idem-gb01-provider-failure",
            request_id="req-gb01-provider-failure",
        )
    )

    assert isinstance(response, SuccessResponseEnvelope), response
    job = chat.read_generation_job(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id="cmd-gb01-provider-failure.gen",
    )
    assert job is not None
    assert job.state == "failed"
    assert job.sanitized_error_code == "malformed-response"
    assert "secret" not in (job.sanitized_error_detail or "")
    assert job.result_message_id is None
    assert [
        event.event_type
        for event in chat.read_generation_events(
            seeded.connection,
            workspace_id=WORKSPACE_ID,
            generation_job_id=job.generation_job_id,
        )
    ] == ["chat.generation.queued", "chat.generation.failed"]
    outcomes = chat.read_generation_attempt_outcomes(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=job.generation_job_id,
    )
    assert len(outcomes) == 1
    assert outcomes[0].terminal_state == "failed"
    assert outcomes[0].retryable is False
    assert outcomes[0].sanitized_error_code == "malformed-response"
    status = chat.read_generation_job_status_projection(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=job.generation_job_id,
    )
    assert status is not None
    assert status.state == "failed"
    assert status.sanitized_error_code == "malformed-response"


@pytest.mark.parametrize(
    ("command", "request_id"),
    (
        (
            {
                **_submit_command(command_id="cmd-gb01-missing-version"),
                "expectedHeadVersion": None,
            },
            "req-gb01-invalid-head-version",
        ),
        (
            {
                **_submit_command(command_id="cmd-gb01-bad-target"),
                "targetReference": {"kind": "app"},
            },
            "req-gb01-invalid-target",
        ),
    ),
)
def test_gb01_malformed_submit_message_refuses_before_any_durable_write(
    seeded: m1.Owned, command: Mapping[str, Any], request_id: str
) -> None:
    before = _counts(seeded)

    response = _dispatcher(seeded).dispatch(
        _request(
            command,
            idempotency_key=f"idem-{request_id}",
            request_id=request_id,
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "invalid_request"
    assert _counts(seeded) == before


def test_gb01_workspace_mismatch_refuses_before_any_durable_write(
    seeded: m1.Owned,
) -> None:
    before = _counts(seeded)

    response = _dispatcher(seeded).dispatch(
        _request(
            _submit_command(
                command_id="cmd-gb01-other-workspace",
                workspace_id=m1.OTHER_WORKSPACE_ID,
            ),
            idempotency_key="idem-gb01-other-workspace",
            request_id="req-gb01-other-workspace",
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "invalid_request"
    assert _counts(seeded) == before


def test_gb01_stale_branch_head_conflict_rolls_back_the_whole_submission(
    seeded: m1.Owned,
) -> None:
    before = _counts(seeded)

    response = _dispatcher(seeded).dispatch(
        _request(
            _submit_command(
                command_id="cmd-gb01-stale-head",
                expected_head_message_id="msg-gb01-not-the-head",
            ),
            idempotency_key="idem-gb01-stale-head",
            request_id="req-gb01-stale-head",
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before


def test_gb01_generation_claim_consumes_the_precreated_queued_job(
    seeded: m1.Owned,
) -> None:
    response = _dispatcher(seeded).dispatch(_request(_submit_command()))
    assert isinstance(response, SuccessResponseEnvelope), response

    claimed = claim_queued_generation(
        seeded.connection,
        seeded.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=seeded.generation,
        queued_submission_id="cmd-gb01-submit.sub",
        generation_job_id="cmd-gb01-submit.gen",
        generation_attempt_id="cmd-gb01-submit.attempt1",
        trigger_message_id="msg-gb01-submitted",
        lease_owner="runner-gb01",
        now_us=BASE_US + 2_000_000,
    )

    assert claimed.submission.state == "submitted"
    assert claimed.submission.submitted_message_id == "msg-gb01-submitted"
    assert claimed.submission.submitted_generation_job_id == "cmd-gb01-submit.gen"
    assert claimed.job.state == "running"
    assert claimed.job.current_attempt_id == "cmd-gb01-submit.attempt1"
    assert claimed.job.last_event_sequence == 1

    events = chat.read_generation_events(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id="cmd-gb01-submit.gen",
    )
    assert [(event.event_type, event.generation_event_sequence) for event in events] == [
        ("chat.generation.queued", 1)
    ]
    assert events[0].payload == {
        "attemptNumber": 1,
        "queuedSubmissionId": "cmd-gb01-submit.sub",
    }
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


# --- H1: route availability and lease renewal ---------------------------------------
#
# Two facts the executor could previously not tell apart or keep alive.
#
# The first is WHICH terminal failure happened. Every boundary failure used to
# terminalize as `malformed-response`, so "I never reached a provider" and "a provider
# answered badly" were the same durable outcome -- and they are not the same fact. One
# is a routing or configuration problem, the other is a response to investigate.
#
# The second is the lease. `claim_queued_generation` takes one and every event write
# re-states it, which keeps a chatty stream alive and does nothing for a quiet one. A
# provider thinking for longer than the lease before its first token would let its own
# claim lapse, leaving a job durably `running` under a dead lease.


def _executor_config(**overrides: Any) -> GenerationExecutorConfig:
    return GenerationExecutorConfig(
        **{
            "connection_id": "provider-connection-1",
            "model_id": "provider-model-1",
            "policy_ref": "policy-1",
            "classification_ref": "classification-1",
            "residency_ref": "residency-1",
            "service_actor_id": "actor-core-chat",
            **overrides,
        }
    )


def _run_executor(
    seeded: m1.Owned,
    clock: FakeClock,
    provider: Any,
    config: GenerationExecutorConfig,
    command_id: str,
) -> Any:
    executor = ChatGenerationExecutor(
        connection=seeded.connection,
        identity=seeded.identity,
        fencing_generation=seeded.generation,
        workspace_id=WORKSPACE_ID,
        clock=clock,
        invoke=provider,
        config=config,
    )
    response = build_chat_application_dispatcher(
        service=seeded,
        principal_id=PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        fallback=_fallback(),
        clock=clock,
        execute_generation=executor.execute_submission,
    ).dispatch(_request(_submit_command(command_id=command_id)))
    assert isinstance(response, SuccessResponseEnvelope), response
    return chat.read_generation_job(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=f"{command_id}.gen",
    )


def _stream(request: Any, deltas: tuple[str, ...], *, finish: bool = True) -> Iterator[Any]:
    common = {
        "invocationId": request.invocation_id,
        "attemptId": request.attempt_id,
        "schemaVersion": 1,
        "occurredAt": "2052-05-23T00:00:01Z",
        "receivedAt": "2052-05-23T00:00:01Z",
    }
    yield {**common, "ordinal": 0, "eventType": "stream-start"}
    for index, delta in enumerate(deltas, start=1):
        yield {
            **common,
            "ordinal": index,
            "eventType": "text-delta",
            "partId": f"provider-part-{index}",
            "stepId": "provider-step-1",
            "delta": delta,
        }
    if finish:
        yield {
            **common,
            "ordinal": len(deltas) + 1,
            "eventType": "finish",
            "finishReason": "stop",
        }


def test_a_non_contiguous_provider_ordinal_fails_the_generation(
    seeded: m1.Owned,
) -> None:
    """A provider stream that skips an event ordinal must fail the generation."""

    def provider(request: Any) -> Iterator[Any]:
        common = {
            "invocationId": request.invocation_id,
            "attemptId": request.attempt_id,
            "schemaVersion": 1,
            "occurredAt": "2052-05-23T00:00:01Z",
            "receivedAt": "2052-05-23T00:00:01Z",
        }
        yield {**common, "ordinal": 0, "eventType": "stream-start"}
        yield {
            **common,
            "ordinal": 2,
            "eventType": "text-delta",
            "partId": "provider-part-1",
            "stepId": "provider-step-1",
            "delta": "assistant reply",
        }
        yield {**common, "ordinal": 3, "eventType": "finish", "finishReason": "stop"}

    job = _run_executor(
        seeded,
        FakeClock(wall=WALL),
        provider,
        _executor_config(),
        "cmd-gb01-noncontiguous",
    )

    assert job is not None
    assert job.state == "failed"
    assert job.result_message_id is None
    assert [
        event.event_type
        for event in chat.read_generation_events(
            seeded.connection,
            workspace_id=WORKSPACE_ID,
            generation_job_id=job.generation_job_id,
        )
    ] == [
        "chat.generation.queued",
        "chat.generation.started",
        "chat.generation.failed",
    ]


def test_a_stale_branch_head_at_settlement_fails_the_generation(
    seeded: m1.Owned,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A branch head advance between claim and settlement must fail the generation."""
    import dataclasses

    clock = FakeClock(wall=WALL)
    generating = {"active": False}
    real_read_branch = chat.read_branch

    def provider(request: Any) -> Iterator[Any]:
        generating["active"] = True
        yield from _stream(request, ("assistant reply",))

    def read_branch_with_a_moved_head(
        connection: Any, *, workspace_id: str, branch_id: str
    ) -> Any:
        branch = real_read_branch(
            connection, workspace_id=workspace_id, branch_id=branch_id
        )
        if branch is None or not generating["active"]:
            return branch
        return dataclasses.replace(
            branch, current_head_message_id="msg-gb01-concurrent-advance"
        )

    monkeypatch.setattr(chat, "read_branch", read_branch_with_a_moved_head)

    job = _run_executor(
        seeded,
        clock,
        provider,
        _executor_config(),
        "cmd-gb01-stalehead",
    )

    assert generating["active"], "the provider never ran"
    assert job is not None
    assert job.state == "failed"
    assert job.result_message_id is None


def test_an_unreachable_route_terminalizes_as_provider_unavailable(
    seeded: m1.Owned,
) -> None:
    """No route resolved: real lifecycle, and the outcome names the actual cause."""
    calls: list[Any] = []

    def provider(request: Any) -> Iterator[Any]:
        calls.append(request)
        yield from ()

    job = _run_executor(
        seeded,
        FakeClock(wall=WALL),
        provider,
        _executor_config(connection_id=""),
        "cmd-gb01-no-route",
    )

    # The provider is never asked, because there was nothing to ask.
    assert calls == []
    assert job is not None
    assert job.state == "failed"
    assert job.sanitized_error_code == "provider-unavailable"
    # The point of claiming first: the submission still has a durable lifecycle to
    # observe and replay, rather than disappearing between the queue and the provider.
    assert [
        event.event_type
        for event in chat.read_generation_events(
            seeded.connection,
            workspace_id=WORKSPACE_ID,
            generation_job_id=job.generation_job_id,
        )
    ] == ["chat.generation.queued", "chat.generation.failed"]
    outcomes = chat.read_generation_attempt_outcomes(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=job.generation_job_id,
    )
    assert len(outcomes) == 1
    assert outcomes[0].terminal_state == "failed"
    assert outcomes[0].retryable is True
    assert outcomes[0].sanitized_error_code == "provider-unavailable"
    status = chat.read_generation_job_status_projection(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=job.generation_job_id,
    )
    assert status is not None
    assert status.state == "retryable"
    assert status.current_attempt_id == job.current_attempt_id
    assert status.sanitized_error_code == "provider-unavailable"
    assert status.finished_at_us is None
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_stream_that_ends_without_a_terminal_event_stays_malformed_response(
    seeded: m1.Owned,
) -> None:
    """The provider WAS reached. A bad response must not read as an unreachable one."""
    job = _run_executor(
        seeded,
        FakeClock(wall=WALL),
        lambda request: _stream(request, ("half an answer",), finish=False),
        _executor_config(),
        "cmd-gb01-truncated",
    )
    assert job is not None
    assert job.state == "failed"
    assert job.sanitized_error_code == "malformed-response"
    assert job.current_attempt_id is not None
    chunks = chat.read_generation_text_chunks(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=job.generation_job_id,
        generation_attempt_id=job.current_attempt_id,
    )
    assert [chunk.text_content for chunk in chunks] == ["half an answer"]
    outcomes = chat.read_generation_attempt_outcomes(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=job.generation_job_id,
    )
    assert len(outcomes) == 1
    assert outcomes[0].terminal_state == "failed"
    assert outcomes[0].sanitized_error_code == "malformed-response"


def test_the_claim_lease_outlives_the_invocation_deadline(seeded: m1.Owned) -> None:
    """A lease shorter than the deadline expires while the wait is still legitimate."""
    clock = FakeClock(wall=WALL)
    deadline_seconds = 600
    observed: list[int] = []

    def provider(request: Any) -> Iterator[Any]:
        running = chat.read_generation_job(
            seeded.connection,
            workspace_id=WORKSPACE_ID,
            generation_job_id="cmd-gb01-long-deadline.gen",
        )
        assert running is not None and running.lease_expires_at_us is not None
        observed.append(running.lease_expires_at_us - int(WALL.timestamp() * 1_000_000))
        yield from _stream(request, ("answer",))

    _run_executor(
        seeded,
        clock,
        provider,
        _executor_config(deadline_seconds=deadline_seconds),
        "cmd-gb01-long-deadline",
    )
    # Sized from the deadline, not left at the 60s default it would otherwise take.
    assert observed and observed[0] >= deadline_seconds * 1_000_000


def test_a_quiet_stream_renews_its_lease_rather_than_letting_it_lapse(
    seeded: m1.Owned,
) -> None:
    """The renewal that does not depend on events arriving.

    The provider goes quiet for longer than half the lease between its start and its
    first token, which is exactly the gap an event-driven re-statement cannot cover.
    """
    clock = FakeClock(wall=WALL)
    job_id = "cmd-gb01-quiet.gen"
    expiries: list[int] = []

    def read_expiry() -> int:
        running = chat.read_generation_job(
            seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=job_id
        )
        assert running is not None and running.lease_expires_at_us is not None
        return running.lease_expires_at_us

    def provider(request: Any) -> Iterator[Any]:
        common = {
            "invocationId": request.invocation_id,
            "attemptId": request.attempt_id,
            "schemaVersion": 1,
            "occurredAt": "2052-05-23T00:00:01Z",
            "receivedAt": "2052-05-23T00:00:01Z",
        }
        yield {**common, "ordinal": 0, "eventType": "stream-start"}
        expiries.append(read_expiry())
        # Long enough to pass the renewal point, short enough that a correctly sized
        # lease has not yet expired -- the window where the heartbeat is the only thing
        # keeping the claim alive.
        clock.advance_wall(100)
        yield {
            **common,
            "ordinal": 1,
            "eventType": "text-delta",
            "partId": "provider-part-1",
            "stepId": "provider-step-1",
            "delta": "eventually",
        }
        expiries.append(read_expiry())
        yield {**common, "ordinal": 2, "eventType": "finish", "finishReason": "stop"}

    job = _run_executor(
        seeded, clock, provider, _executor_config(), "cmd-gb01-quiet"
    )
    assert job is not None and job.state == "succeeded"
    assert len(expiries) == 2
    # The lease moved forward without any event having required it to.
    assert expiries[1] > expiries[0]


def test_a_lease_may_not_be_renewed_by_another_instance(seeded: m1.Owned) -> None:
    """Renewal is fenced: losing the lease must be discoverable, not silently undone."""
    clock = FakeClock(wall=WALL)
    refusals: list[str] = []

    def provider(request: Any) -> Iterator[Any]:
        try:
            renew_generation_lease(
                seeded.connection,
                seeded.identity,
                workspace_id=WORKSPACE_ID,
                fencing_generation=seeded.generation,
                generation_job_id="cmd-gb01-fenced.gen",
                lease_owner="some-other-service-instance",
                now_us=int(clock.wall_time().timestamp() * 1_000_000),
            )
        except GenerationConflict as error:
            refusals.append(str(error))
        yield from _stream(request, ("answer",))

    job = _run_executor(
        seeded, clock, provider, _executor_config(), "cmd-gb01-fenced"
    )
    assert job is not None and job.state == "succeeded"
    assert len(refusals) == 1


# --- H1: the production surface installs an executor --------------------------------


def test_the_production_surface_installs_a_generation_executor(seeded: m1.Owned) -> None:
    """A production build must consume a submission, not decline it at the door.

    `_build_production_application_surface` took an `execute_chat_generation` seam that
    nothing supplied, so the real service composed with `None` and every
    `SubmitMessage` refused with `dependency_unavailable` before mutating anything.
    The executor existed and was correct; only Core's own tests, which construct one
    directly, ever reached it.

    That is why this is worth a test rather than being obvious from the wiring: the
    parameter was present, typed and passed through, so the seam LOOKED connected at
    every point a reader would check. Nothing named the absent caller.
    """
    started = SimpleNamespace(
        connection=seeded.connection,
        identity=seeded.identity,
        generation=seeded.generation,
        workspace_id=WORKSPACE_ID,
        clock=SystemClock(),
    )
    assert _default_chat_generation(started) is not None  # type: ignore[arg-type]


def test_a_build_with_no_provider_route_still_produces_a_real_lifecycle(
    seeded: m1.Owned,
) -> None:
    """The default install carries a submission to a durable terminal.

    Not a refusal, and not an orphan. `application.py` refuses without an executor so a
    build "cannot leave a queued job that no worker can consume" -- and an installed
    executor honours that invariant more completely, because the job IS consumed: it is
    claimed, opens an attempt, and terminalizes as `provider-unavailable`, which is the
    true outcome when no route is configured.

    This is what makes the H1 restart/resume path exercisable against the real service
    without any provider adapter: there is finally a durable generation to observe.
    """
    clock = FakeClock(wall=WALL)
    started = SimpleNamespace(
        connection=seeded.connection,
        identity=seeded.identity,
        generation=seeded.generation,
        workspace_id=WORKSPACE_ID,
        clock=clock,
    )
    execute = _default_chat_generation(started)  # type: ignore[arg-type]
    assert execute is not None

    response = build_chat_application_dispatcher(
        service=seeded,
        principal_id=PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        fallback=_fallback(),
        clock=clock,
        execute_generation=execute,
    ).dispatch(_request(_submit_command(command_id="cmd-gb01-installed")))

    assert isinstance(response, SuccessResponseEnvelope), response
    job = chat.read_generation_job(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id="cmd-gb01-installed.gen",
    )
    assert job is not None
    assert job.state == "failed"
    assert job.sanitized_error_code == "provider-unavailable"
    # The durable events a restarted client would replay.
    assert [
        event.event_type
        for event in chat.read_generation_events(
            seeded.connection,
            workspace_id=WORKSPACE_ID,
            generation_job_id=job.generation_job_id,
        )
    ] == ["chat.generation.queued", "chat.generation.failed"]
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_the_default_executor_uses_an_explicit_local_provider_route(
    seeded: m1.Owned,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured local route changes only the provider boundary, not Chat's lifecycle."""

    class Response:
        status = 200

        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks

        def read(self, _size: int = -1) -> bytes:
            return self.chunks.pop(0) if self.chunks else b""

    class Connection:
        seen: ClassVar[list[Connection]] = []

        def __init__(self, host: str, port: int, *, timeout: float) -> None:
            self.host = host
            self.port = port
            self.timeout = timeout
            self.body = b""
            self.headers: Mapping[str, str] = {}
            self.path = ""
            self.closed = False
            self.__class__.seen.append(self)

        def request(
            self,
            method: str,
            path: str,
            *,
            body: bytes,
            headers: Mapping[str, str],
        ) -> None:
            assert method == "POST"
            self.path = path
            self.body = body
            self.headers = headers

        def getresponse(self) -> Response:
            request = json.loads(self.body.decode("utf-8"))
            common = {
                "invocationId": request["invocationId"],
                "attemptId": request["attemptId"],
                "schemaVersion": 1,
                "occurredAt": "2052-05-23T00:00:01Z",
                "receivedAt": "2052-05-23T00:00:01Z",
            }
            return Response(
                [
                    json.dumps(
                        {**common, "ordinal": 0, "eventType": "stream-start"}
                    ).encode("utf-8")
                    + b"\n",
                    json.dumps(
                        {
                            **common,
                            "ordinal": 1,
                            "eventType": "text-delta",
                            "partId": "provider-part-1",
                            "stepId": "provider-step-1",
                            "delta": "assistant reply through local bridge",
                        }
                    ).encode("utf-8")
                    + b"\n",
                    json.dumps(
                        {
                            **common,
                            "ordinal": 2,
                            "eventType": "finish",
                            "finishReason": "stop",
                        }
                    ).encode("utf-8")
                    + b"\n",
                ]
            )

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(chat_provider_route, "HTTPConnection", Connection)
    monkeypatch.setenv(
        chat_provider_route.ENDPOINT_ENV, "http://127.0.0.1:49152/f2a/provider-stream"
    )
    monkeypatch.setenv(chat_provider_route.TOKEN_ENV, "route-token-secret")
    monkeypatch.setenv(chat_provider_route.CONNECTION_ID_ENV, "provider-connection-1")
    monkeypatch.setenv(chat_provider_route.MODEL_ID_ENV, "provider-model-1")
    monkeypatch.setenv(chat_provider_route.POLICY_REF_ENV, "policy-1")
    monkeypatch.setenv(chat_provider_route.CLASSIFICATION_REF_ENV, "classification-1")
    monkeypatch.setenv(chat_provider_route.RESIDENCY_REF_ENV, "residency-1")

    clock = FakeClock(wall=WALL)
    started = SimpleNamespace(
        connection=seeded.connection,
        identity=seeded.identity,
        generation=seeded.generation,
        workspace_id=WORKSPACE_ID,
        clock=clock,
    )
    execute = _default_chat_generation(started)  # type: ignore[arg-type]
    assert execute is not None

    response = build_chat_application_dispatcher(
        service=seeded,
        principal_id=PRINCIPAL,
        installation_id=INSTALLATION_ID,
        workspace_id=WORKSPACE_ID,
        fallback=_fallback(),
        clock=clock,
        execute_generation=execute,
    ).dispatch(_request(_submit_command(command_id="cmd-gb01-local-route")))

    assert isinstance(response, SuccessResponseEnvelope), response
    job = chat.read_generation_job(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id="cmd-gb01-local-route.gen",
    )
    assert job is not None
    assert job.state == "succeeded"
    assert job.result_message_id is not None
    parts = chat.read_message_parts(
        seeded.connection, workspace_id=WORKSPACE_ID, message_id=job.result_message_id
    )
    assert [dict(part.payload) for part in parts] == [
        {"text": "assistant reply through local bridge"}
    ]
    assert len(Connection.seen) == 1
    assert Connection.seen[0].headers["Authorization"] == "Bearer route-token-secret"
    assert b"route-token-secret" not in Connection.seen[0].body
    assert Connection.seen[0].closed is True


def test_an_unstarted_service_installs_no_executor(seeded: m1.Owned) -> None:
    """No connection to write through means the refusal-before-mutation path stands."""
    for missing in ("connection", "identity", "generation", "workspace_id"):
        fields = {
            "connection": seeded.connection,
            "identity": seeded.identity,
            "generation": seeded.generation,
            "workspace_id": WORKSPACE_ID,
            "clock": SystemClock(),
        }
        fields[missing] = None
        assert _default_chat_generation(SimpleNamespace(**fields)) is None, missing  # type: ignore[arg-type]


# --- cold start: CreateConversation, then a branchless first SubmitMessage ------------


def _create_request(
    *,
    command: Mapping[str, Any] | None = None,
    idempotency_key: str = "idem-cold-create",
    request_id: str = "req-cold-create",
    expected_conversation: Any = None,
) -> RequestEnvelope:
    return _request(
        _create_conversation_command() if command is None else command,
        command_name="CreateConversation",
        idempotency_key=idempotency_key,
        request_id=request_id,
        expected_conversation=expected_conversation,
    )


def _first_send_request(
    *,
    command: Mapping[str, Any] | None = None,
    idempotency_key: str = "idem-cold-first",
    request_id: str = "req-cold-first",
    expected_conversation: Any = _UNSET,
) -> RequestEnvelope:
    if expected_conversation is _UNSET:
        expected_conversation = {
            "conversation_id": COLD_CONVERSATION_ID,
            "graph_revision": 1,
            "latest_conversation_sequence": 0,
        }
    return _request(
        _first_send_command() if command is None else command,
        idempotency_key=idempotency_key,
        request_id=request_id,
        expected_conversation=expected_conversation,
    )


def _create_conversation(holder: m1.Owned) -> SuccessResponseEnvelope:
    response = _dispatcher(holder).dispatch(_create_request())
    assert isinstance(response, SuccessResponseEnvelope), response
    return response


def test_create_conversation_writes_one_active_conversation_and_no_branch(
    owned: m1.Owned,
) -> None:
    before = _counts(owned)

    response = _create_conversation(owned)

    assert response.result == {
        "command_name": "CreateConversation",
        "command_result": {"commandId": CREATE_COMMAND_ID, "status": "completed"},
        "conversation_id": COLD_CONVERSATION_ID,
    }
    # REF-042 §9.1: no branch, no root message, no view state until the first send --
    # and no outbox fact either, because an empty conversation has committed no graph
    # fact to announce. The first send is the first thing there is to publish.
    assert _counts(owned) == {
        **before,
        "omnivia_application_audit_events": before["omnivia_application_audit_events"] + 1,
        "omnivia_idempotency_claims": before["omnivia_idempotency_claims"] + 1,
        "omnivia_idempotency_outcomes": before["omnivia_idempotency_outcomes"] + 1,
        s0.EXECUTIONS_TABLE: before[s0.EXECUTIONS_TABLE] + 1,
        "omnivia_chat_conversations": before["omnivia_chat_conversations"] + 1,
    }
    conversation = chat.read_conversation(
        owned.connection, workspace_id=WORKSPACE_ID, conversation_id=COLD_CONVERSATION_ID
    )
    assert conversation is not None
    assert conversation.state == "active"
    assert conversation.graph_revision == 1
    assert conversation.latest_conversation_sequence == 0
    assert conversation.default_branch_id is None
    assert conversation.title is None
    assert conversation.title_source is None
    assert conversation.created_by_actor_id == PRINCIPAL
    assert not chat.read_outbox_events_since(
        owned.connection, workspace_id=WORKSPACE_ID, after_cursor=0
    )


@pytest.mark.parametrize("title", ("cold start", ""))
def test_create_conversation_carries_a_caller_title_as_a_user_title(
    owned: m1.Owned, title: str
) -> None:
    """`CreateConversationRequest.title` states no `minLength`, and
    `graph.schema.json#/$defs/Conversation.title` sets it to `0`: an empty title is a
    title the domain record holds, not a document this decoder may refuse."""
    response = _dispatcher(owned).dispatch(
        _create_request(command=_create_conversation_command(title=title))
    )

    assert isinstance(response, SuccessResponseEnvelope), response
    conversation = chat.read_conversation(
        owned.connection, workspace_id=WORKSPACE_ID, conversation_id=COLD_CONVERSATION_ID
    )
    assert conversation is not None
    assert conversation.title == title
    # 0029 admits `user | generated | imported`, and a caller-supplied title is the
    # actor's own, never a generated one.
    assert conversation.title_source == "user"


def test_create_conversation_replay_answers_from_the_settled_outcome(
    owned: m1.Owned,
) -> None:
    dispatcher = _dispatcher(owned)
    request = _create_request()
    first = dispatcher.dispatch(request)
    after_first = _counts(owned)

    second = dispatcher.dispatch(request)

    assert isinstance(first, SuccessResponseEnvelope), first
    assert isinstance(second, SuccessResponseEnvelope), second
    assert second.result == first.result
    # Only the replayed execution record moves; no second conversation.
    assert _counts(owned) == {
        **after_first,
        s0.EXECUTIONS_TABLE: after_first[s0.EXECUTIONS_TABLE] + 1,
    }


def test_create_conversation_under_a_second_key_conflicts_without_writing(
    owned: m1.Owned,
) -> None:
    """The same `commandId` under a different idempotency key is not a replay: the
    conversation it would create is already there, so it fails closed."""
    _create_conversation(owned)
    after_first = _counts(owned)

    response = _dispatcher(owned).dispatch(
        _create_request(
            idempotency_key="idem-cold-create-again",
            request_id="req-cold-create-again",
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(owned) == after_first


def test_create_conversation_refuses_a_foreign_actor_before_writing(
    owned: m1.Owned,
) -> None:
    """`actorId` becomes the conversation's durable `createdBy`, so a command may not
    name an author this request was not authenticated as."""
    before = _counts(owned)

    response = _dispatcher(owned).dispatch(
        _create_request(
            command=_create_conversation_command(actor_id="actor-somebody-else"),
            idempotency_key="idem-cold-create-foreign-actor",
            request_id="req-cold-create-foreign-actor",
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "invalid_request"
    assert _counts(owned) == before


@pytest.mark.parametrize(
    ("command", "request_id"),
    (
        (
            {**_create_conversation_command(), "workspaceId": m1.OTHER_WORKSPACE_ID},
            "req-cold-create-other-workspace",
        ),
        (
            {**_create_conversation_command(), "requestedAt": "yesterday"},
            "req-cold-create-bad-instant",
        ),
        (
            {**_create_conversation_command(), "requestedAt": "2052-05-22T14:13:20"},
            "req-cold-create-naive-instant",
        ),
        (
            {**_create_conversation_command(), "conversationId": "conv-client-minted"},
            "req-cold-create-client-identity",
        ),
        (
            {
                key: value
                for key, value in _create_conversation_command().items()
                if key != "requestedAt"
            },
            "req-cold-create-missing-instant",
        ),
    ),
)
def test_create_conversation_refuses_a_malformed_document_before_writing(
    owned: m1.Owned, command: Mapping[str, Any], request_id: str
) -> None:
    before = _counts(owned)

    response = _dispatcher(owned).dispatch(
        _create_request(
            command=command,
            idempotency_key=f"idem-{request_id}",
            request_id=request_id,
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "invalid_request"
    assert _counts(owned) == before


def test_create_conversation_with_a_conversation_expectation_fails_closed(
    owned: m1.Owned,
) -> None:
    """There is no conversation yet, so no revision expectation can be satisfied."""
    before = _counts(owned)

    response = _dispatcher(owned).dispatch(
        _create_request(
            expected_conversation={
                "conversation_id": COLD_CONVERSATION_ID,
                "graph_revision": 1,
                "latest_conversation_sequence": 0,
            },
            idempotency_key="idem-cold-create-expectation",
            request_id="req-cold-create-expectation",
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(owned) == before


def test_first_send_creates_the_root_message_branch_and_selection(
    owned: m1.Owned,
) -> None:
    _create_conversation(owned)
    before = _counts(owned)
    generation_calls: list[Mapping[str, Any]] = []

    response = _dispatcher(
        owned, execute_generation=lambda **fields: generation_calls.append(fields)
    ).dispatch(_first_send_request())

    assert isinstance(response, SuccessResponseEnvelope), response
    assert response.result == {
        "command_name": "SubmitMessage",
        "command_result": {"commandId": FIRST_SEND_COMMAND_ID, "status": "completed"},
        "conversation_id": COLD_CONVERSATION_ID,
    }
    assert _counts(owned) == {
        **before,
        "omnivia_application_audit_events": before["omnivia_application_audit_events"] + 1,
        "omnivia_idempotency_claims": before["omnivia_idempotency_claims"] + 1,
        "omnivia_idempotency_outcomes": before["omnivia_idempotency_outcomes"] + 1,
        s0.EXECUTIONS_TABLE: before[s0.EXECUTIONS_TABLE] + 1,
        "omnivia_chat_messages": before["omnivia_chat_messages"] + 1,
        "omnivia_chat_message_parts": before["omnivia_chat_message_parts"] + 1,
        "omnivia_chat_message_branches": before["omnivia_chat_message_branches"] + 1,
        "omnivia_chat_branch_head_events": before["omnivia_chat_branch_head_events"] + 1,
        "omnivia_chat_conversation_view_states": (
            before["omnivia_chat_conversation_view_states"] + 1
        ),
        "omnivia_chat_queued_submissions": before["omnivia_chat_queued_submissions"] + 1,
        "omnivia_chat_generation_jobs": before["omnivia_chat_generation_jobs"] + 1,
        "omnivia_chat_queue_order_projection": (
            before["omnivia_chat_queue_order_projection"] + 1
        ),
        "omnivia_chat_transactional_outbox": before["omnivia_chat_transactional_outbox"] + 1,
    }

    conversation = chat.read_conversation(
        owned.connection, workspace_id=WORKSPACE_ID, conversation_id=COLD_CONVERSATION_ID
    )
    assert conversation is not None
    assert conversation.latest_conversation_sequence == 1
    assert conversation.default_branch_id == COLD_BRANCH_ID
    # The head event foreign-keys the conversation's own revision, so the first send
    # advances the sequence and leaves the revision exactly where a continuation does.
    assert conversation.graph_revision == 1

    message = chat.read_messages_by_conversation_sequence(
        owned.connection, workspace_id=WORKSPACE_ID, conversation_id=COLD_CONVERSATION_ID
    )[0]
    assert message.message_id == COLD_ROOT_MESSAGE_ID
    assert message.parent_message_id is None
    assert message.role == "user"
    assert message.author_id == PRINCIPAL
    assert message.created_on_branch_id == COLD_BRANCH_ID
    assert message.conversation_sequence == 1

    branch = chat.read_branch(
        owned.connection, workspace_id=WORKSPACE_ID, branch_id=COLD_BRANCH_ID
    )
    assert branch is not None
    assert branch.conversation_id == COLD_CONVERSATION_ID
    assert branch.origin_kind == "original"
    assert branch.state == "open"
    assert branch.head_version == 1
    assert branch.initial_head_message_id == COLD_ROOT_MESSAGE_ID
    assert branch.current_head_message_id == COLD_ROOT_MESSAGE_ID
    assert branch.created_conversation_sequence == 1
    assert branch.created_from_branch_id is None
    assert branch.fork_parent_message_id is None
    assert branch.fork_source_message_id is None

    events = chat.read_branch_head_events(
        owned.connection, workspace_id=WORKSPACE_ID, branch_id=COLD_BRANCH_ID
    )
    assert [(event.head_version, event.cause) for event in events] == [
        (1, "branch_created")
    ]
    assert events[0].previous_head_message_id is None
    assert events[0].new_head_message_id == COLD_ROOT_MESSAGE_ID
    assert events[0].conversation_sequence == 1
    assert events[0].graph_revision == 1

    view_state = chat.read_actor_view_state(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=COLD_CONVERSATION_ID,
        actor_id=PRINCIPAL,
    )
    assert view_state is not None
    assert view_state.active_branch_id == COLD_BRANCH_ID
    assert view_state.last_seen_graph_revision == 1
    assert view_state.version == 1

    job = chat.read_generation_job(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=f"{FIRST_SEND_COMMAND_ID}.gen",
    )
    assert job is not None
    assert job.state == "queued"
    assert job.branch_id == COLD_BRANCH_ID
    assert job.trigger_message_id == COLD_ROOT_MESSAGE_ID

    # `CreateConversation` announced nothing, so the first send is the workspace's
    # first Chat outbox fact and sits at the very first cursor.
    outbox = chat.read_outbox_events_since(
        owned.connection, workspace_id=WORKSPACE_ID, after_cursor=0
    )
    assert [event.event_kind for event in outbox] == ["chat.message.committed"]
    payload = dict(outbox[0].payload)
    # A root message has no parent, and `null` would say it has one that is nothing.
    assert "parentMessageId" not in payload
    assert payload["branchId"] == COLD_BRANCH_ID
    assert payload["headVersion"] == 1

    # The same execution seam a continuation uses, with the same three identities.
    assert generation_calls == [
        {
            "queued_submission_id": f"{FIRST_SEND_COMMAND_ID}.sub",
            "generation_job_id": f"{FIRST_SEND_COMMAND_ID}.gen",
            "trigger_message_id": COLD_ROOT_MESSAGE_ID,
        }
    ]


def test_first_send_then_continuation_appends_on_the_branch_it_created(
    owned: m1.Owned,
) -> None:
    """The branch a first send creates is an ordinary branch: the next send is the
    unchanged continuation, stale-head check and all."""
    _create_conversation(owned)
    dispatcher = _dispatcher(owned)
    assert isinstance(dispatcher.dispatch(_first_send_request()), SuccessResponseEnvelope)

    second = dispatcher.dispatch(
        _request(
            _submit_command(
                command_id="cmd-cold-second",
                message_id="cmd-cold-second.msg",
                expected_head_message_id=COLD_ROOT_MESSAGE_ID,
                expected_head_version=1,
                conversationId=COLD_CONVERSATION_ID,
                branchId=COLD_BRANCH_ID,
                actorId=PRINCIPAL,
            ),
            idempotency_key="idem-cold-second",
            request_id="req-cold-second",
            expected_conversation={
                "conversation_id": COLD_CONVERSATION_ID,
                "graph_revision": 1,
                "latest_conversation_sequence": 1,
            },
        )
    )

    assert isinstance(second, SuccessResponseEnvelope), second
    branch = chat.read_branch(
        owned.connection, workspace_id=WORKSPACE_ID, branch_id=COLD_BRANCH_ID
    )
    assert branch is not None
    assert branch.head_version == 2
    assert branch.current_head_message_id == "cmd-cold-second.msg"
    assert [
        (event.head_version, event.cause)
        for event in chat.read_branch_head_events(
            owned.connection, workspace_id=WORKSPACE_ID, branch_id=COLD_BRANCH_ID
        )
    ] == [(1, "branch_created"), (2, "user_message_appended")]


def test_first_send_replay_writes_nothing_a_second_time(owned: m1.Owned) -> None:
    _create_conversation(owned)
    dispatcher = _dispatcher(owned)
    request = _first_send_request()
    first = dispatcher.dispatch(request)
    after_first = _counts(owned)

    second = dispatcher.dispatch(request)

    assert isinstance(first, SuccessResponseEnvelope), first
    assert isinstance(second, SuccessResponseEnvelope), second
    assert second.result == first.result
    assert _counts(owned) == {
        **after_first,
        s0.EXECUTIONS_TABLE: after_first[s0.EXECUTIONS_TABLE] + 1,
    }


@pytest.mark.parametrize(
    ("command_id", "idempotency_key"),
    (
        # The same identity under a second key: not a replay, so it re-runs and finds
        # the branch its own first run created.
        (FIRST_SEND_COMMAND_ID, "idem-cold-first-again"),
        # And a genuinely second caller, racing the same cold start.
        ("cmd-cold-first-other", "idem-cold-first-other"),
    ),
    ids=("same-command-id", "second-command-id"),
)
def test_a_second_first_send_conflicts_and_leaves_no_partial_state(
    owned: m1.Owned, command_id: str, idempotency_key: str
) -> None:
    """The branchless variant is servable exactly once per conversation: the branch
    the first send created is read inside the fenced transaction, so the second send
    is refused rather than appending past a head it never stated."""
    _create_conversation(owned)
    dispatcher = _dispatcher(owned)
    assert isinstance(dispatcher.dispatch(_first_send_request()), SuccessResponseEnvelope)
    after_first = _counts(owned)

    response = dispatcher.dispatch(
        _first_send_request(
            command=_first_send_command(command_id=command_id),
            idempotency_key=idempotency_key,
            request_id=f"req-{idempotency_key}",
            expected_conversation={
                "conversation_id": COLD_CONVERSATION_ID,
                "graph_revision": 1,
                "latest_conversation_sequence": 1,
            },
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert {table: _counts(owned)[table] for table in CHAT_TABLES} == {
        table: after_first[table] for table in CHAT_TABLES
    }


def test_a_fault_after_the_first_send_writes_rolls_back_every_domain_row(
    owned: m1.Owned,
) -> None:
    """Not a pre-flight refusal: the whole first send is written and *then* the
    settlement fails -- here through the seam's own result validation, the last check
    that runs after every domain row is in. One transaction means the branch, root
    message, head event, view state, queue and outbox rows all go back."""
    _create_conversation(owned)
    before = _counts(owned)

    def resolve_then_fail(request: Any, context: Any) -> Any:
        command = resolve_chat_command(request, context)

        def failing(writer: Any, settlement: Any) -> Mapping[str, Any]:
            command(writer, settlement)
            return {"not": "a governed command result"}

        return failing

    response = _dispatcher(owned, resolve_command=resolve_then_fail).dispatch(
        _first_send_request()
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert {table: _counts(owned)[table] for table in CHAT_TABLES} == {
        table: before[table] for table in CHAT_TABLES
    }
    assert (
        chat.read_branch(owned.connection, workspace_id=WORKSPACE_ID, branch_id=COLD_BRANCH_ID)
        is None
    )
    conversation = chat.read_conversation(
        owned.connection, workspace_id=WORKSPACE_ID, conversation_id=COLD_CONVERSATION_ID
    )
    assert conversation is not None
    assert conversation.default_branch_id is None
    assert conversation.latest_conversation_sequence == 0


def test_first_send_into_a_branched_conversation_conflicts_and_rolls_back(
    seeded: m1.Owned,
) -> None:
    """The seeded conversation already has a branch at a real head, so a request that
    states no head is refused rather than appending past one it never checked."""
    before = _counts(seeded)

    response = _dispatcher(seeded).dispatch(
        _request(
            _first_send_command(
                command_id="cmd-cold-into-branched",
                conversation_id=CONVERSATION_ID,
                actor_id=ACTOR_ID,
            ),
            idempotency_key="idem-cold-into-branched",
            request_id="req-cold-into-branched",
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before


def test_first_send_into_an_absent_conversation_conflicts_without_writing(
    owned: m1.Owned,
) -> None:
    before = _counts(owned)

    response = _dispatcher(owned).dispatch(
        _first_send_request(
            command=_first_send_command(
                command_id="cmd-cold-absent",
                conversation_id="conv-never-created",
            ),
            idempotency_key="idem-cold-absent",
            request_id="req-cold-absent",
            expected_conversation=None,
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(owned) == before


def test_first_send_into_another_workspaces_conversation_refuses(
    owned: m1.Owned,
) -> None:
    before = _counts(owned)

    response = _dispatcher(owned).dispatch(
        _first_send_request(
            command=_first_send_command(
                command_id="cmd-cold-other-workspace",
                workspace_id=m1.OTHER_WORKSPACE_ID,
            ),
            idempotency_key="idem-cold-other-workspace",
            request_id="req-cold-other-workspace",
            expected_conversation=None,
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "invalid_request"
    assert _counts(owned) == before


@pytest.mark.parametrize(
    "stated",
    (
        ("branchId",),
        ("expectedHeadMessageId",),
        ("expectedHeadVersion",),
        ("branchId", "expectedHeadMessageId"),
        ("branchId", "expectedHeadVersion"),
        ("expectedHeadMessageId", "expectedHeadVersion"),
    ),
    ids=lambda stated: "+".join(stated),
)
def test_a_partial_expected_head_group_is_malformed(
    owned: m1.Owned, stated: tuple[str, ...]
) -> None:
    """All three or none. A caller stating one of them has a view of a head, and the
    ones it left out are exactly the checks that would then not run."""
    _create_conversation(owned)
    before = _counts(owned)
    partial = {
        "branchId": COLD_BRANCH_ID,
        "expectedHeadMessageId": COLD_ROOT_MESSAGE_ID,
        "expectedHeadVersion": 1,
    }
    request_id = f"req-cold-partial-{'-'.join(stated)}"

    response = _dispatcher(owned).dispatch(
        _first_send_request(
            command=_first_send_command(
                command_id="cmd-cold-partial",
                **{field: partial[field] for field in stated},
            ),
            idempotency_key=f"idem-{request_id}",
            request_id=request_id,
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "invalid_request"
    assert _counts(owned) == before


def test_the_cold_started_conversation_answers_a_complete_snapshot(
    owned: m1.Owned,
) -> None:
    """The handoff this slice exists for: create, first send, then `chat.snapshot`
    answers with the conversation, the path, the branch record carrying `headVersion`,
    and the actor's view state."""
    _create_conversation(owned)
    dispatcher = _dispatcher(owned)
    assert isinstance(dispatcher.dispatch(_first_send_request()), SuccessResponseEnvelope)

    snapshot_entry = get_operation_metadata("chat.snapshot")
    response = dispatcher.dispatch(
        s0.envelope_for(
            snapshot_entry,
            operation_input={
                "conversation_id": COLD_CONVERSATION_ID,
                "snapshot_query": {
                    "requestId": "req-cold-snapshot",
                    "workspaceId": WORKSPACE_ID,
                    "conversationId": COLD_CONVERSATION_ID,
                    "actorId": PRINCIPAL,
                },
            },
            request_id="req-cold-snapshot",
            purpose=CHAT_FAMILY_PURPOSES["chat.snapshot"],
            workspace_id=WORKSPACE_ID,
        )
    )

    assert isinstance(response, SuccessResponseEnvelope), response
    snapshot = response.result["snapshot"]
    assert set(snapshot) == {"conversation", "path", "branch", "viewState"}
    assert snapshot["conversation"]["defaultBranchId"] == COLD_BRANCH_ID
    assert [message["messageId"] for message in snapshot["path"]["messages"]] == [
        COLD_ROOT_MESSAGE_ID
    ]
    assert snapshot["branch"] == {
        "workspaceId": WORKSPACE_ID,
        "conversationId": COLD_CONVERSATION_ID,
        "branchId": COLD_BRANCH_ID,
        "originKind": "original",
        "initialHeadMessageId": COLD_ROOT_MESSAGE_ID,
        "currentHeadMessageId": COLD_ROOT_MESSAGE_ID,
        "createdBy": PRINCIPAL,
        "createdAt": snapshot["branch"]["createdAt"],
        "createdConversationSequence": 1,
        "headVersion": 1,
        "schemaVersion": 1,
        "state": "open",
    }
    assert snapshot["viewState"]["activeBranchId"] == COLD_BRANCH_ID
    # And the whole document is the governed one, checked against the frozen schema.
    assert _snapshot_schema_errors(snapshot) == []


def test_a_branchless_conversation_has_no_snapshot_to_answer_with(
    owned: m1.Owned,
) -> None:
    """Before the first send there is no branch, no path and no view state, and
    `ConversationSnapshotResult` requires all three: the governed `not_found` stands,
    and the `CreateConversation` result is the host's empty authoritative state."""
    created = _create_conversation(owned)
    assert created.result["conversation_id"] == COLD_CONVERSATION_ID

    snapshot_entry = get_operation_metadata("chat.snapshot")
    response = _dispatcher(owned).dispatch(
        s0.envelope_for(
            snapshot_entry,
            operation_input={
                "conversation_id": COLD_CONVERSATION_ID,
                "snapshot_query": {
                    "requestId": "req-cold-empty-snapshot",
                    "workspaceId": WORKSPACE_ID,
                    "conversationId": COLD_CONVERSATION_ID,
                    "actorId": PRINCIPAL,
                },
            },
            request_id="req-cold-empty-snapshot",
            purpose=CHAT_FAMILY_PURPOSES["chat.snapshot"],
            workspace_id=WORKSPACE_ID,
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "not_found"


# --- the widened contract itself, held to the authoritative schema --------------------


def test_both_submit_variants_validate_against_the_frozen_command_schema() -> None:
    """The all-or-none group is the contract's rule, not this resolver's opinion of
    it: both variants are checked against `commands.schema.json` itself."""
    assert _schema_errors(_SUBMIT_MESSAGE_REF, _submit_command()) == []
    assert _schema_errors(_SUBMIT_MESSAGE_REF, _first_send_command()) == []
    assert _schema_errors(_CREATE_CONVERSATION_REF, _create_conversation_command()) == []
    assert (
        _schema_errors(
            _CREATE_CONVERSATION_REF, _create_conversation_command(title="cold start")
        )
        == []
    )


@pytest.mark.parametrize(
    "stated",
    (
        ("branchId",),
        ("expectedHeadMessageId",),
        ("expectedHeadVersion",),
        ("branchId", "expectedHeadMessageId"),
        ("branchId", "expectedHeadVersion"),
        ("expectedHeadMessageId", "expectedHeadVersion"),
    ),
    ids=lambda stated: "+".join(stated),
)
def test_a_partial_expected_head_group_fails_the_frozen_command_schema(
    stated: tuple[str, ...],
) -> None:
    partial = {
        "branchId": BRANCH_ID,
        "expectedHeadMessageId": ROOT_MESSAGE_ID,
        "expectedHeadVersion": 1,
    }
    document = _first_send_command(**{field: partial[field] for field in stated})

    assert _schema_errors(_SUBMIT_MESSAGE_REF, document) != []


def test_the_widened_schema_still_refuses_what_it_always_refused() -> None:
    """Widening the head group is the only thing that moved: a document missing a
    member outside it is refused exactly as before."""
    for dropped in ("editableParts", "attachmentReferences", "contextReferences", "actorId"):
        document = {
            key: value
            for key, value in _first_send_command().items()
            if key != dropped
        }
        assert _schema_errors(_SUBMIT_MESSAGE_REF, document) != [], dropped
    assert _schema_errors(_SUBMIT_MESSAGE_REF, _first_send_command(title="nope")) != []
