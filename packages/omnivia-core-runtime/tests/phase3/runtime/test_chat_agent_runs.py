"""T-0665 acceptance for durable delegated Agent Runs and mailbox continuation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import test_chat_request_manifest as manifest
from omnivia_core_runtime.service.chat_agent_runs import (
    AgentRunConflict,
    AgentRunMailboxMessage,
    ChatAgentRun,
    agent_run_projection,
    deliver_mailbox_message,
    enqueue_mailbox_message,
    heartbeat_agent_run,
    mark_agent_run_terminal,
    open_agent_run,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

RUN_ID = "agent-run-t0665-1"
RUNTIME_NAME = "claude-code"
RUNTIME_VERSION = "runtime-v1"
ACTOR_ID = "actor-chat-delegator"


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[manifest.m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    manifest.m1.bootstrap_and_migrate(path, workspace_id=manifest.WORKSPACE_ID)
    holder = manifest.m1.take_ownership(path)
    manifest.seed_conversation_and_queue(holder)
    yield holder
    holder.connection.close()


def _open_run(
    holder: manifest.m1.Owned,
    *,
    agent_run_id: str = RUN_ID,
    display_name: str = "Claude helper",
    mode: str = "continuable",
    configuration: Mapping[str, object] | None = None,
    now_us: int = manifest.BASE_US + 200,
) -> ChatAgentRun:
    return open_agent_run(
        holder.connection,
        holder.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=holder.generation,
        conversation_id=manifest.CONVERSATION_ID,
        agent_run_id=agent_run_id,
        parent_generation_job_id=manifest.JOB_ID,
        runtime_name=RUNTIME_NAME,
        runtime_version=RUNTIME_VERSION,
        display_name=display_name,
        mode=mode,
        configuration=configuration or {"profile": "safe-default"},
        created_by_actor_id=ACTOR_ID,
        now_us=now_us,
    )


def _enqueue(
    holder: manifest.m1.Owned,
    *,
    agent_run_id: str = RUN_ID,
    mailbox_message_id: str = "mailbox-t0665-1",
    idempotency_key: str = "mailbox-idem-t0665-1",
    direction: str = "to_agent",
    payload: Mapping[str, object] | None = None,
    now_us: int = manifest.BASE_US + 210,
) -> AgentRunMailboxMessage:
    return enqueue_mailbox_message(
        holder.connection,
        holder.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=holder.generation,
        agent_run_id=agent_run_id,
        mailbox_message_id=mailbox_message_id,
        direction=direction,
        idempotency_key=idempotency_key,
        sender_actor_id=ACTOR_ID,
        payload=payload or {"text": "please continue"},
        now_us=now_us,
    )


def test_agent_run_identity_survives_duplicate_display_names(
    owned: manifest.m1.Owned,
) -> None:
    first = _open_run(owned, agent_run_id="agent-run-t0665-a", display_name="Claude")
    second = _open_run(
        owned,
        agent_run_id="agent-run-t0665-b",
        display_name="Claude",
        now_us=manifest.BASE_US + 201,
    )

    assert first.agent_run_id != second.agent_run_id
    projected = agent_run_projection(
        owned.connection,
        workspace_id=manifest.WORKSPACE_ID,
        conversation_id=manifest.CONVERSATION_ID,
    )
    assert {item["agentRunId"] for item in projected} == {
        "agent-run-t0665-a",
        "agent-run-t0665-b",
    }
    assert {item["displayName"] for item in projected} == {"Claude"}


def test_open_agent_run_replay_is_idempotent_and_identity_strict(
    owned: manifest.m1.Owned,
) -> None:
    first = _open_run(owned)
    replay = _open_run(owned)

    assert replay == first
    with pytest.raises(AgentRunConflict):
        _open_run(owned, display_name="Different visible label")


def test_mailbox_message_is_durable_before_ack_and_replay_is_idempotent(
    owned: manifest.m1.Owned,
) -> None:
    _open_run(owned)

    first = _enqueue(owned)
    replay = _enqueue(owned)

    assert replay == first
    row = owned.connection.execute(
        "SELECT delivery_state, payload_json FROM omnivia_chat_agent_run_mailbox "
        "WHERE workspace_id = ? AND mailbox_message_id = ?",
        (manifest.WORKSPACE_ID, first.mailbox_message_id),
    ).fetchone()
    assert row[0] == "queued"
    assert json.loads(row[1]) == {"text": "please continue"}
    with pytest.raises(AgentRunConflict):
        _enqueue(
            owned,
            mailbox_message_id="mailbox-t0665-conflict",
            idempotency_key=first.idempotency_key,
        )


def test_one_shot_and_continuable_runs_have_distinct_input_rules(
    owned: manifest.m1.Owned,
) -> None:
    one_shot = _open_run(owned, agent_run_id="agent-run-t0665-one", mode="one_shot")
    continuable = _open_run(
        owned,
        agent_run_id="agent-run-t0665-cont",
        mode="continuable",
        now_us=manifest.BASE_US + 201,
    )

    _enqueue(
        owned,
        agent_run_id=one_shot.agent_run_id,
        mailbox_message_id="mailbox-t0665-one-1",
        idempotency_key="mailbox-idem-one-1",
    )
    with pytest.raises(AgentRunConflict):
        _enqueue(
            owned,
            agent_run_id=one_shot.agent_run_id,
            mailbox_message_id="mailbox-t0665-one-2",
            idempotency_key="mailbox-idem-one-2",
            now_us=manifest.BASE_US + 211,
        )

    first_continuation = _enqueue(
        owned,
        agent_run_id=continuable.agent_run_id,
        mailbox_message_id="mailbox-t0665-cont-1",
        idempotency_key="mailbox-idem-cont-1",
    )
    second_continuation = _enqueue(
        owned,
        agent_run_id=continuable.agent_run_id,
        mailbox_message_id="mailbox-t0665-cont-2",
        idempotency_key="mailbox-idem-cont-2",
        now_us=manifest.BASE_US + 211,
    )

    assert first_continuation.mailbox_message_id != second_continuation.mailbox_message_id


def test_mailbox_delivery_is_idempotent(
    owned: manifest.m1.Owned,
) -> None:
    _open_run(owned)
    message = _enqueue(owned)

    first = deliver_mailbox_message(
        owned.connection,
        owned.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=owned.generation,
        mailbox_message_id=message.mailbox_message_id,
        now_us=manifest.BASE_US + 220,
    )
    replay = deliver_mailbox_message(
        owned.connection,
        owned.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=owned.generation,
        mailbox_message_id=message.mailbox_message_id,
        now_us=manifest.BASE_US + 221,
    )

    assert first.delivery_state == "delivered"
    assert replay == first
    assert replay.delivered_at_us == manifest.BASE_US + 220


def test_terminal_agent_run_refuses_new_mailbox_and_heartbeat(
    owned: manifest.m1.Owned,
) -> None:
    run = _open_run(owned)

    terminal = mark_agent_run_terminal(
        owned.connection,
        owned.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=owned.generation,
        agent_run_id=run.agent_run_id,
        expected_version=run.version,
        state="succeeded",
        now_us=manifest.BASE_US + 230,
    )

    assert terminal.state == "succeeded"
    with pytest.raises(AgentRunConflict):
        _enqueue(owned, now_us=manifest.BASE_US + 231)
    with pytest.raises(AgentRunConflict):
        heartbeat_agent_run(
            owned.connection,
            owned.identity,
            workspace_id=manifest.WORKSPACE_ID,
            fencing_generation=owned.generation,
            agent_run_id=run.agent_run_id,
            expected_version=terminal.version,
            now_us=manifest.BASE_US + 232,
        )


def test_projection_is_bounded_and_does_not_copy_mailbox_payloads(
    owned: manifest.m1.Owned,
) -> None:
    _open_run(owned)
    _enqueue(owned, payload={"text": "private child-run instruction"})

    projected = agent_run_projection(
        owned.connection,
        workspace_id=manifest.WORKSPACE_ID,
        conversation_id=manifest.CONVERSATION_ID,
    )

    assert projected == (
        {
            "agentRunId": RUN_ID,
            "conversationId": manifest.CONVERSATION_ID,
            "runtime": {"name": RUNTIME_NAME, "version": RUNTIME_VERSION},
            "displayName": "Claude helper",
            "mode": "continuable",
            "state": "running",
            "heartbeatAtUs": manifest.BASE_US + 200,
            "queuedMailboxCount": 1,
        },
    )
    assert "private child-run instruction" not in json.dumps(projected, sort_keys=True)


def test_agent_run_rows_reject_unguarded_mutation(
    owned: manifest.m1.Owned,
) -> None:
    _open_run(owned)

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        owned.connection.execute(
            "UPDATE omnivia_chat_agent_runs "
            "SET state = 'cancelled' WHERE workspace_id = ? AND agent_run_id = ?",
            (manifest.WORKSPACE_ID, RUN_ID),
        )
