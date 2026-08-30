"""T-0659 acceptance for provider-event-neutral generation heartbeat recovery."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import test_chat_request_manifest as manifest
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.chat_generation import (
    DEFAULT_LEASE_US,
    GenerationConflict,
    GenerationHeartbeat,
    GenerationTerminal,
    append_provider_generation_event,
    recover_generation_attempt_lease,
)
from omnivia_core_runtime.service.chat_generation_executor import (
    ChatGenerationExecutor,
)
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline


@pytest.fixture
def running(manifest_owned: manifest.m1.Owned) -> manifest.m1.Owned:
    manifest.seed_running_attempt(manifest_owned)
    return manifest_owned


@pytest.fixture
def manifest_owned(owned: manifest.m1.Owned) -> manifest.m1.Owned:
    return owned


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[manifest.m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    manifest.m1.bootstrap_and_migrate(path, workspace_id=manifest.WORKSPACE_ID)
    holder = manifest.m1.take_ownership(path)
    yield holder
    holder.connection.close()


def _job(holder: manifest.m1.Owned) -> chat.GenerationJob:
    job = chat.read_generation_job(
        holder.connection,
        workspace_id=manifest.WORKSPACE_ID,
        generation_job_id=manifest.JOB_ID,
    )
    assert job is not None
    return job


def _move_branch_head_to_trigger(holder: manifest.m1.Owned) -> None:
    with manifest.writer(holder) as writer:
        writer.append_branch_head_event(
            event_id="head-event-heartbeat-trigger",
            conversation_id=manifest.CONVERSATION_ID,
            branch_id=manifest.BRANCH_ID,
            head_version=2,
            previous_head_message_id=manifest.ROOT_MESSAGE_ID,
            new_head_message_id=manifest.TRIGGER_MESSAGE_ID,
            cause="user_message_appended",
            command_id="command-heartbeat-trigger",
            graph_revision=1,
            conversation_sequence=2,
            actor_id=manifest.ACTOR_ID,
            occurred_at_us=manifest.BASE_US + 20,
            schema_version=1,
        )
        writer.update_branch_head(
            branch_id=manifest.BRANCH_ID,
            expected_head_version=1,
            head_version=2,
            current_head_message_id=manifest.TRIGGER_MESSAGE_ID,
            state="open",
        )


def _heartbeat(
    holder: manifest.m1.Owned,
    clock: FakeClock,
    *,
    lease_owner: str = manifest.LEASE_OWNER,
) -> GenerationHeartbeat:
    return GenerationHeartbeat(
        connection=holder.connection,
        identity=holder.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=holder.generation,
        generation_job_id=manifest.JOB_ID,
        generation_attempt_id=manifest.ATTEMPT_ID,
        lease_owner=lease_owner,
        clock=clock,
    )


def _error_event() -> Mapping[str, Any]:
    return {
        "eventType": "error",
        "errorCode": "timeout",
        "retryable": False,
        "statusClass": "timeout",
        "safeMessage": "provider timed out safely",
        "providerEventId": "provider-event-heartbeat-error",
    }


def test_independent_heartbeat_renews_without_provider_events(
    running: manifest.m1.Owned,
) -> None:
    clock = FakeClock(wall=manifest.WALL)
    initial = _job(running)
    assert initial.lease_expires_at_us is not None
    heartbeat = _heartbeat(running, clock)
    heartbeat.start(initial.lease_expires_at_us)

    clock.advance_wall(DEFAULT_LEASE_US / 1_000_000)
    renewed_expiry = heartbeat.tick()
    after = _job(running)

    assert renewed_expiry == after.lease_expires_at_us
    assert after.lease_expires_at_us is not None
    assert after.lease_expires_at_us > initial.lease_expires_at_us
    assert after.heartbeat_at_us == int(clock.wall_time().timestamp() * 1_000_000)
    assert after.lease_epoch == initial.lease_epoch
    assert [
        event.event_type
        for event in chat.read_generation_events(
            running.connection,
            workspace_id=manifest.WORKSPACE_ID,
            generation_job_id=manifest.JOB_ID,
        )
    ] == ["chat.generation.queued"]


def test_heartbeat_stops_on_terminal_generation(running: manifest.m1.Owned) -> None:
    clock = FakeClock(wall=manifest.WALL)
    heartbeat = _heartbeat(running, clock)
    heartbeat.start(_job(running).lease_expires_at_us)
    append_provider_generation_event(
        running.connection,
        running.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=running.generation,
        generation_job_id=manifest.JOB_ID,
        generation_attempt_id=manifest.ATTEMPT_ID,
        provider_event=_error_event(),
        now_us=manifest.BASE_US + 50,
    )

    assert heartbeat.tick() == _job(running).lease_expires_at_us
    assert heartbeat.active is False
    with pytest.raises(GenerationTerminal):
        recover_generation_attempt_lease(
            running.connection,
            running.identity,
            workspace_id=manifest.WORKSPACE_ID,
            fencing_generation=running.generation,
            generation_job_id=manifest.JOB_ID,
            generation_attempt_id=manifest.ATTEMPT_ID,
            recovery_owner="worker-heartbeat-recovery",
            now_us=manifest.BASE_US + DEFAULT_LEASE_US + 1,
        )


def test_expired_lease_recovery_has_one_winner_and_refuses_late_old_owner(
    running: manifest.m1.Owned,
) -> None:
    before = _job(running)
    assert before.lease_expires_at_us is not None
    now_us = before.lease_expires_at_us + 1

    recovered = recover_generation_attempt_lease(
        running.connection,
        running.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=running.generation,
        generation_job_id=manifest.JOB_ID,
        generation_attempt_id=manifest.ATTEMPT_ID,
        recovery_owner="worker-heartbeat-recovery",
        now_us=now_us,
    )

    assert recovered.lease_owner == "worker-heartbeat-recovery"
    assert recovered.lease_epoch == before.lease_epoch + 1
    assert recovered.heartbeat_at_us == now_us
    assert recovered.lease_expires_at_us == now_us + DEFAULT_LEASE_US

    with pytest.raises(GenerationConflict, match="active lease"):
        recover_generation_attempt_lease(
            running.connection,
            running.identity,
            workspace_id=manifest.WORKSPACE_ID,
            fencing_generation=running.generation,
            generation_job_id=manifest.JOB_ID,
            generation_attempt_id=manifest.ATTEMPT_ID,
            recovery_owner="worker-heartbeat-second",
            now_us=now_us,
        )

    with pytest.raises(GenerationConflict, match="leased by another instance"):
        append_provider_generation_event(
            running.connection,
            running.identity,
            workspace_id=manifest.WORKSPACE_ID,
            fencing_generation=running.generation,
            generation_job_id=manifest.JOB_ID,
            generation_attempt_id=manifest.ATTEMPT_ID,
            provider_event={
                "eventType": "stream-start",
                "providerEventId": "provider-event-heartbeat-late-start",
            },
            now_us=now_us + 1,
            lease_owner=manifest.LEASE_OWNER,
        )


def test_executor_heartbeat_can_be_ticked_during_provider_silence(
    owned: manifest.m1.Owned,
) -> None:
    manifest.seed_conversation_and_queue(owned)
    _move_branch_head_to_trigger(owned)
    clock = FakeClock(wall=manifest.WALL)
    heartbeats: list[GenerationHeartbeat] = []
    expiries: list[int] = []

    def heartbeat_factory(**fields: Any) -> GenerationHeartbeat:
        heartbeat = GenerationHeartbeat(**fields)
        heartbeats.append(heartbeat)
        return heartbeat

    def provider(request: Any) -> Iterator[Mapping[str, Any]]:
        initial = chat.read_generation_job(
            owned.connection,
            workspace_id=manifest.WORKSPACE_ID,
            generation_job_id=manifest.JOB_ID,
        )
        assert initial is not None and initial.lease_expires_at_us is not None
        expiries.append(initial.lease_expires_at_us)
        clock.advance_wall(100)
        assert heartbeats[0].tick() is not None
        renewed = chat.read_generation_job(
            owned.connection,
            workspace_id=manifest.WORKSPACE_ID,
            generation_job_id=manifest.JOB_ID,
        )
        assert renewed is not None and renewed.lease_expires_at_us is not None
        expiries.append(renewed.lease_expires_at_us)
        yield from manifest._stream(request)

    executor = ChatGenerationExecutor(
        connection=owned.connection,
        identity=owned.identity,
        fencing_generation=owned.generation,
        workspace_id=manifest.WORKSPACE_ID,
        clock=clock,
        invoke=provider,
        config=manifest._config(),
        heartbeat_factory=heartbeat_factory,
    )
    executor.execute_submission(
        queued_submission_id=manifest.QUEUE_ID,
        generation_job_id=manifest.JOB_ID,
        trigger_message_id=manifest.TRIGGER_MESSAGE_ID,
    )

    assert len(heartbeats) == 1
    assert heartbeats[0].active is False
    assert len(expiries) == 2
    assert expiries[1] > expiries[0]
    assert _job(owned).state == "succeeded"
