"""W5 GB-01: production `SubmitMessage` behind the public `chat.command` seam."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.application import (
    CHAT_FAMILY_PURPOSES,
    build_chat_application_dispatcher,
)
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.chat_generation import claim_queued_generation
from omnivia_core_runtime.service.chat_generation_executor import (
    ChatGenerationExecutor,
    GenerationExecutorConfig,
)
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

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
BASE_US = 2_600_000_000_000_000
WALL = datetime.fromtimestamp((BASE_US + 1_000_000) / 1_000_000, tz=UTC)

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


def _request(
    command: Mapping[str, Any],
    *,
    command_name: str = "SubmitMessage",
    idempotency_key: str = "idem-gb01-submit",
    request_id: str = "req-gb01-submit",
    expected_sequence: int = SEEDED_SEQUENCE,
) -> RequestEnvelope:
    return s0.envelope_for(
        ENTRY,
        operation_input={
            "command_name": command_name,
            "command": dict(command),
            "expected_conversation": {
                "conversation_id": CONVERSATION_ID,
                "graph_revision": GRAPH_REVISION,
                "latest_conversation_sequence": expected_sequence,
            },
        },
        request_id=request_id,
        correlation_id=f"cor-{request_id}",
        trace_id=f"trc-{request_id}",
        idempotency_key=idempotency_key,
        purpose=CHAT_FAMILY_PURPOSES[OPERATION],
        workspace_id=WORKSPACE_ID,
    )


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
        "chat.generation.succeeded",
    ]
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
