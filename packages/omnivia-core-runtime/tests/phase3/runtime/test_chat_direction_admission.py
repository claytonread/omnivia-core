"""C6: the private `SubmitSteeringInput` admission lane behind `chat.command`.

Real SQLite, the GB-01 harness (`test_chat_submit_message_resolver.py`) for the
dispatcher, the seeded conversation and the running-job setup, and the frozen
schemas for every document the lane emits.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Mapping
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.identity import SystemClock
from omnivia_core_runtime.service import chat_submit
from omnivia_core_runtime.service.application import chat_family_session
from omnivia_core_runtime.service.authorization import ServiceBinding
from omnivia_core_runtime.service.chat_command import is_governed_command_result
from omnivia_core_runtime.service.chat_generation import claim_queued_generation
from omnivia_core_runtime.service.chat_submit import steering_direction_id
from omnivia_core_runtime.service.handlers.chat import (
    CHAT_EVENTS_OPERATION,
    ChatHandlers,
)
from omnivia_core_runtime.service.operations import OperationContext
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.memory import random_identifier
from test_chat_submit_message_resolver import (
    BASE_US,
    CONVERSATION_ID,
    INSTALLATION_ID,
    PRINCIPAL,
    ROOT_MESSAGE_ID,
    SEEDED_SEQUENCE,
    WORKSPACE_ID,
    _counts,
    _dispatcher,
    _request,
    _schema_errors,
    _stop_generation_command,
    _submit_queued,
    owned,
    seeded,
)

from omnivia_core.chat_contract.v1 import PROTOCOL_VERSION, to_canonical_json
from omnivia_core.contracts.v1 import (
    ChatCommandResult,
    ErrorResponseEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
)

__all__ = ["owned", "seeded"]  # re-exported fixtures

_CONTRACT = "https://contracts.omnivia.dev/chat/v1/"
_REQUEST_REF = f"{_CONTRACT}commands.schema.json#/$defs/SubmitSteeringInputRequest"
_RESULT_REF = f"{_CONTRACT}commands.schema.json#/$defs/CommandResultEnvelope"
_EVENT_REF = f"{_CONTRACT}events.schema.json#/$defs/DirectionStateChangedEvent"
_NAME = "SubmitSteeringInput"
_OTHER_ACTOR = "actor-c6-other"
_TEXT = "Please focus the answer on the cost section."
_PARTS = [{"type": "text", "payload": {"text": _TEXT}}]


# --- harness ------------------------------------------------------------------------


def _running(holder: m1.Owned, tag: str = "c6a", *, head: tuple[str, int, int] | None = None) -> str:
    """One submitted-and-claimed (running) job; returns its id. Status version is 2."""
    message, version, sequence = head or (ROOT_MESSAGE_ID, 1, SEEDED_SEQUENCE)
    _submit_queued(
        holder,
        command_id=f"cmd-{tag}",
        expected_head_message_id=message,
        expected_head_version=version,
        expected_sequence=sequence,
    )
    claim_queued_generation(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        queued_submission_id=f"cmd-{tag}.sub",
        generation_job_id=f"cmd-{tag}.gen",
        generation_attempt_id=f"cmd-{tag}.attempt1",
        trigger_message_id=f"cmd-{tag}.msg",
        lease_owner="runner-c6",
        now_us=BASE_US + 2_000_000,
    )
    return f"cmd-{tag}.gen"


def _steer(
    job_id: str,
    *,
    command_id: str = "cmd-c6-steer",
    version: int = 2,
    parts: list[Mapping[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "commandId": command_id,
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "actorId": PRINCIPAL,
        "targetGenerationJobId": job_id,
        "expectedGenerationVersion": version,
        "expectedActiveState": "running",
        "requestedBoundary": "earliest_safe",
        "editableParts": list(_PARTS if parts is None else parts),
        "attachmentReferences": [],
        "contextReferences": [],
    }
    command.update(overrides)
    return {key: value for key, value in command.items() if value is not _ABSENT}


_ABSENT = object()


def _send(
    holder: m1.Owned,
    command: Mapping[str, Any],
    *,
    key: str | None = None,
    name: str = _NAME,
) -> Any:
    key = str(command.get("commandId")) if key is None else key
    return _dispatcher(holder).dispatch(
        _request(
            command,
            command_name=name,
            idempotency_key=key,
            request_id=f"req-{key}",
            expected_conversation=None,
        )
    )


def _ok(response: Any) -> Mapping[str, Any]:
    """The accepted `chat.command` envelope, held to the strict contract and the wrapper."""
    assert isinstance(response, SuccessResponseEnvelope), response
    envelope = response.result["command_result"]
    assert _schema_errors(_RESULT_REF, envelope) == []
    assert is_governed_command_result(envelope)
    assert ChatCommandResult.from_wire(response.result).to_wire() == response.result
    assert envelope["status"] == "accepted"
    assert envelope["resultRef"] == "direction:" + steering_direction_id(
        WORKSPACE_ID, envelope["commandId"]
    )
    return envelope  # type: ignore[no-any-return]


def _direction_id(envelope: Mapping[str, Any]) -> str:
    return str(envelope["resultRef"]).removeprefix("direction:")


def _code(response: Any) -> str:
    assert isinstance(response, ErrorResponseEnvelope), response
    return str(response.error.code)


def _state(holder: m1.Owned, job_id: str) -> dict[str, Any]:
    kwargs = {"workspace_id": WORKSPACE_ID, "generation_job_id": job_id}
    return {
        "counts": _counts(holder) | {"directions": m1.count(holder.connection, "omnivia_chat_directions")},
        "job": chat.read_generation_job(holder.connection, **kwargs),
        "status": chat.read_generation_job_status_projection(holder.connection, **kwargs),
        "events": chat.read_generation_events(holder.connection, **kwargs),
        "directions": chat.read_directions_for_generation_job(holder.connection, **kwargs),
    }


def _status_version(holder: m1.Owned, job_id: str) -> int:
    status = chat.read_generation_job_status_projection(
        holder.connection, workspace_id=WORKSPACE_ID, generation_job_id=job_id
    )
    assert status is not None
    return status.version


def _directions(holder: m1.Owned, job_id: str) -> tuple[chat.Direction, ...]:
    return chat.read_directions_for_generation_job(
        holder.connection, workspace_id=WORKSPACE_ID, generation_job_id=job_id
    )


def _transport(holder: m1.Owned, job_id: str) -> list[Mapping[str, Any]]:
    handlers = ChatHandlers(
        service=type("S", (), {"connection": holder.connection, "identity": holder.identity})(),
        session=chat_family_session(
            principal_id=PRINCIPAL, installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID
        ),
        binding=ServiceBinding(installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID),
        clock=SystemClock(),
        allocate_identifier=random_identifier,
    )
    context = OperationContext(
        request=s0.envelope_for(
            get_operation_metadata(CHAT_EVENTS_OPERATION),
            operation_input={"generation_job_id": job_id},
            request_id="req-c6-events",
            workspace_id=WORKSPACE_ID,
        ),
        principal=PRINCIPAL,
        workspace_id=WORKSPACE_ID,
        granted_operations=frozenset({CHAT_EVENTS_OPERATION}),
    )
    return list(handlers.chat_events(context)["transport_events"])


def _seed_foreign_direction(holder: m1.Owned, job_id: str, *, actor: str = _OTHER_ACTOR) -> str:
    job = chat.read_generation_job(holder.connection, workspace_id=WORKSPACE_ID, generation_job_id=job_id)
    assert job is not None and job.current_attempt_id is not None
    with chat.chat_writer(
        holder.connection, holder.identity, workspace_id=WORKSPACE_ID, fencing_generation=holder.generation
    ) as writer:
        writer.append_direction(
            direction_id="direction-foreign",
            conversation_id=job.conversation_id,
            branch_id=job.branch_id,
            generation_job_id=job_id,
            target_attempt_id=job.current_attempt_id,
            direction_sequence=1,
            actor_id=actor,
            content={"editableParts": _PARTS},
            created_at_us=BASE_US + 3_000_000,
        )
    return "direction-foreign"


# --- accepted admission --------------------------------------------------------------


def test_first_direction_is_pending_v1_with_server_identity_and_one_job_version_step(
    seeded: m1.Owned,
) -> None:
    job_id = _running(seeded)
    assert _schema_errors(_REQUEST_REF, _steer(job_id)) == []
    before = _state(seeded, job_id)
    assert before["status"].version == 2

    result = _ok(_send(seeded, _steer(job_id)))

    direction_id = steering_direction_id(WORKSPACE_ID, "cmd-c6-steer")
    assert result == {
        "commandId": "cmd-c6-steer",
        "status": "accepted",
        "resultRef": f"direction:{direction_id}",
    }
    assert _TEXT not in str(result)

    (direction,) = _directions(seeded, job_id)
    content = {"editableParts": _PARTS}
    assert direction.direction_id == direction_id
    assert (direction.state, direction.version, direction.direction_sequence) == ("pending", 1, 1)
    assert direction.actor_id == PRINCIPAL
    assert direction.content == content
    assert direction.content_digest == "sha256:" + hashlib.sha256(
        to_canonical_json(content).encode("utf-8")
    ).hexdigest()
    assert direction.requested_boundary == "earliest_safe"
    assert direction.effective_boundary is None
    assert direction.supersedes_direction_id is None
    assert direction.superseded_by_direction_id is None
    assert direction.terminal_reason is None and direction.terminal_at_us is None
    assert direction.target_attempt_id == before["job"].current_attempt_id
    assert direction.branch_id == before["job"].branch_id

    after = _state(seeded, job_id)
    assert after["status"].version == 3
    assert after["status"] == dataclasses.replace(
        before["status"], version=3, updated_at_us=after["status"].updated_at_us
    )
    # Every live lease/attempt/result/error/timestamp fact of the job is untouched.
    assert after["job"] == dataclasses.replace(before["job"], last_event_sequence=2)
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_the_pending_state_change_event_is_identity_only_and_renders_exactly(
    seeded: m1.Owned,
) -> None:
    job_id = _running(seeded)
    _ok(_send(seeded, _steer(job_id)))
    direction_id = steering_direction_id(WORKSPACE_ID, "cmd-c6-steer")

    event = _state(seeded, job_id)["events"][-1]
    assert event.event_type == "chat.direction.state_changed"
    assert event.generation_event_sequence == 2
    assert event.generation_attempt_id is None and event.result_message_id is None
    assert event.provider_event_id is None
    assert dict(event.payload) == {
        "directionId": direction_id,
        "directionVersion": 1,
        "state": "pending",
    }

    wire = _transport(seeded, job_id)[-1]
    assert _schema_errors(_EVENT_REF, wire) == []
    assert wire["eventType"] == "chat.direction.state_changed"
    assert {key: wire[key] for key in ("actorId", "jobSequence", "directionId", "directionVersion", "state")} == {
        "actorId": PRINCIPAL,
        "jobSequence": 1,
        "directionId": direction_id,
        "directionVersion": 1,
        "state": "pending",
    }
    assert "generationAttemptId" not in wire and "triggerMessageId" not in wire
    assert _TEXT not in str(wire)


def test_supersession_is_atomic_and_emits_old_then_new_with_one_status_step(
    seeded: m1.Owned,
) -> None:
    job_id = _running(seeded)
    first = _direction_id(_ok(_send(seeded, _steer(job_id))))
    assert _status_version(seeded, job_id) == 3

    second = _ok(
        _send(
            seeded,
            _steer(job_id, command_id="cmd-c6-second", version=3, supersedesDirectionId=first,
                   parts=[{"type": "text", "payload": {"text": "Actually, the risks."}}]),
        )
    )

    old, new = _directions(seeded, job_id)
    assert _direction_id(second) == new.direction_id
    assert (old.direction_id, old.state, old.version) == (first, "superseded", 2)
    assert old.superseded_by_direction_id == new.direction_id
    assert old.terminal_reason == {"code": "superseded"}
    assert old.terminal_at_us is not None and old.effective_boundary is None
    assert (new.state, new.version, new.direction_sequence) == ("pending", 1, 2)
    assert new.supersedes_direction_id == first and new.effective_boundary is None

    state = _state(seeded, job_id)
    assert [(e.generation_event_sequence, dict(e.payload)) for e in state["events"][-3:]] == [
        (2, {"directionId": first, "directionVersion": 1, "state": "pending"}),
        (3, {"directionId": first, "directionVersion": 2, "state": "superseded"}),
        (4, {"directionId": new.direction_id, "directionVersion": 1, "state": "pending"}),
    ]
    assert state["job"].last_event_sequence == 4
    assert state["status"].version == 4  # one step for the command, not one per event
    wires = _transport(seeded, job_id)
    assert [_schema_errors(_EVENT_REF, wire) for wire in wires[-3:]] == [[], [], []]
    assert [w["state"] for w in wires[-3:]] == ["pending", "superseded", "pending"]
    assert [w["jobSequence"] for w in wires[-3:]] == [1, 1, 2]


# --- replay, identity and conflict ---------------------------------------------------


def test_exact_replay_returns_the_original_without_allocating_or_writing(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _running(seeded)
    first = _ok(_send(seeded, _steer(job_id)))
    settled = _state(seeded, job_id)

    issued: list[str] = []
    real = chat_submit.steering_direction_id
    monkeypatch.setattr(
        chat_submit, "steering_direction_id", lambda *a: issued.append("x") or real(*a)
    )
    replay = _ok(_send(seeded, _steer(job_id)))

    assert issued == []
    assert replay == first
    after = _state(seeded, job_id)
    assert after["directions"] == settled["directions"]
    assert after["status"] == settled["status"] and after["job"] == settled["job"]
    assert after["events"] == settled["events"]


def test_the_same_command_id_under_another_outer_key_cannot_mint_a_second_direction(
    seeded: m1.Owned,
) -> None:
    job_id = _running(seeded)
    _ok(_send(seeded, _steer(job_id)))
    settled = _state(seeded, job_id)

    assert _code(_send(seeded, _steer(job_id), key="idem-a-different-key")) == "invalid_request"
    assert _code(_send(seeded, _steer(job_id, version=3), key="idem-a-different-key")) == "invalid_request"

    assert _state(seeded, job_id) == settled
    # The identity is a pure function of the command, so no key can reach another one.
    assert steering_direction_id(WORKSPACE_ID, "cmd-c6-steer") == steering_direction_id(
        WORKSPACE_ID, "cmd-c6-steer"
    )
    assert steering_direction_id(WORKSPACE_ID, "cmd-c6-steer") != steering_direction_id(
        WORKSPACE_ID, "cmd-c6-other"
    )


def test_a_reused_direction_identity_collides_instead_of_inserting_twice(
    seeded: m1.Owned,
) -> None:
    job_id = _running(seeded)
    first = _direction_id(_ok(_send(seeded, _steer(job_id))))
    # A second command whose derived identity is already durable cannot be admitted.
    original = chat_submit.steering_direction_id
    chat_submit.steering_direction_id = lambda *_a: first  # type: ignore[assignment]
    try:
        settled = _state(seeded, job_id)
        response = _send(seeded, _steer(job_id, command_id="cmd-c6-collide", version=3, supersedesDirectionId=first))
    finally:
        chat_submit.steering_direction_id = original  # type: ignore[assignment]
    assert _code(response) == "conflict"
    assert _state(seeded, job_id) == settled


def test_the_same_command_id_with_changed_content_conflicts(seeded: m1.Owned) -> None:
    job_id = _running(seeded)
    _ok(_send(seeded, _steer(job_id)))
    settled = _state(seeded, job_id)

    changed = _steer(job_id, parts=[{"type": "text", "payload": {"text": "different"}}])
    response = _send(seeded, changed)

    assert isinstance(response, ErrorResponseEnvelope)
    assert _state(seeded, job_id) == settled


# --- target refusals -----------------------------------------------------------------


def test_a_stale_expected_version_refuses_and_writes_nothing(seeded: m1.Owned) -> None:
    job_id = _running(seeded)
    settled = _state(seeded, job_id)
    for version in (1, 3):
        assert _code(_send(seeded, _steer(job_id, command_id=f"cmd-c6-v{version}", version=version))) == "conflict"
    assert _state(seeded, job_id) == settled


def test_a_missing_queued_or_terminal_target_refuses_without_writes(seeded: m1.Owned) -> None:
    queued = "cmd-c6q.gen"
    _submit_queued(
        seeded,
        command_id="cmd-c6q",
        expected_head_message_id=ROOT_MESSAGE_ID,
        expected_head_version=1,
        expected_sequence=SEEDED_SEQUENCE,
    )
    running = _running(seeded, "c6t", head=("cmd-c6q.msg", 2, SEEDED_SEQUENCE + 1))
    assert not isinstance(
        _send(
            seeded,
            _stop_generation_command(command_id="cmd-c6-stop", job_id=running, actor_id=PRINCIPAL),
            name="StopGeneration",
        ),
        ErrorResponseEnvelope,
    )
    for tag, job_id, version in (("missing", "no-such-job", 1), ("queued", queued, 1), ("terminal", running, 3)):
        before = _state(seeded, running) | {"queued": _state(seeded, queued)}
        response = _send(seeded, _steer(job_id, command_id=f"cmd-c6-{tag}", version=version))
        assert _code(response) == "conflict", tag
        assert _state(seeded, running) | {"queued": _state(seeded, queued)} == before, tag


def test_a_status_projection_that_disagrees_with_the_job_refuses(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _running(seeded)
    settled = _state(seeded, job_id)
    real = chat_submit.read_generation_job_status_projection
    for tag, view in (
        ("absent", lambda *a, **k: None),
        ("attempt", lambda *a, **k: dataclasses.replace(real(*a, **k), current_attempt_id="attempt-elsewhere")),
        ("state", lambda *a, **k: dataclasses.replace(real(*a, **k), state="retryable")),
    ):
        monkeypatch.setattr(chat_submit, "read_generation_job_status_projection", view)
        assert _code(_send(seeded, _steer(job_id, command_id=f"cmd-c6-{tag}"))) == "conflict", tag
    monkeypatch.undo()
    assert _state(seeded, job_id) == settled


def test_foreign_workspace_actor_and_conversation_refuse_without_leaking(seeded: m1.Owned) -> None:
    job_id = _running(seeded)
    settled = _state(seeded, job_id)
    cases = {
        "workspace": (_steer(job_id, command_id="cmd-c6-ws", workspaceId="ws-foreign-secret"), "invalid_request", "ws-foreign-secret"),
        "actor": (_steer(job_id, command_id="cmd-c6-actor", actorId="actor-foreign-secret"), "invalid_request", "actor-foreign-secret"),
        "conversation": (_steer(job_id, command_id="cmd-c6-conv", conversationId="conv-foreign-secret"), "conflict", "conv-foreign-secret"),
    }
    for tag, (command, code, secret) in cases.items():
        response = _send(seeded, command)
        assert _code(response) == code, tag
        assert secret not in str(response.error), tag
    assert _state(seeded, job_id) == settled


@pytest.mark.parametrize(
    "overrides",
    (
        {"parts": []},
        {"parts": [{"type": "image", "payload": {"text": "x"}}]},
        {"parts": [{"type": "text", "payload": {"text": "x", "url": "https://x"}}]},
        {"parts": [{"type": "text", "payload": {"text": "a" * 4001}}]},
        {"parts": [{"type": "text", "payload": {"text": "a" * 4000}}] * 5},
        {"parts": [{"type": "text", "payload": {"text": "a"}}] * 17},
        {"protocolVersion": "1.0"},
        {"expectedActiveState": "queued"},
        {"requestedBoundary": "next_iteration"},
        {"expectedGenerationVersion": True},
        {"expectedGenerationVersion": 2.0},
        {"expectedGenerationVersion": 0},
        {"attachmentReferences": [{"referenceId": "a", "sourceRevision": "1"}]},
        {"contextReferences": [{"referenceId": "a", "sourceRevision": "1"}]},
        {"attachmentReferences": _ABSENT},
        {"editableParts": _ABSENT},
        {"supersedesDirectionId": ""},
        {"unexpected": 1},
        {"directionId": "client-minted"},
    ),
    ids=lambda o: "-".join(sorted(o)) + ":" + repr(next(iter(o.values())))[:24],
)
def test_malformed_or_reference_bearing_requests_refuse_before_any_write(
    seeded: m1.Owned, overrides: dict[str, Any]
) -> None:
    job_id = _running(seeded)
    settled = _state(seeded, job_id)
    parts = overrides.pop("parts", None)
    response = _send(seeded, _steer(job_id, parts=parts, **overrides))
    assert _code(response) == "invalid_request"
    assert _state(seeded, job_id) == settled


# --- supersession refusals -----------------------------------------------------------


def test_supersession_refuses_foreign_terminal_absent_and_other_job_directions(seeded: m1.Owned) -> None:
    job_id = _running(seeded)
    other_job = _running(seeded, "c6b", head=("cmd-c6a.msg", 2, SEEDED_SEQUENCE + 1))
    foreign = _seed_foreign_direction(seeded, other_job)  # a pending Direction on the other job
    first = _direction_id(_ok(_send(seeded, _steer(job_id))))
    _ok(_send(seeded, _steer(job_id, command_id="cmd-c6-second", version=3, supersedesDirectionId=first)))
    live = _directions(seeded, job_id)[-1].direction_id
    settled = _state(seeded, job_id) | {"other": _state(seeded, other_job)}
    version = _status_version(seeded, job_id)

    for tag, target in (("absent", "direction-absent"), ("terminal", first), ("other-job", foreign)):
        response = _send(seeded, _steer(job_id, command_id=f"cmd-c6-{tag}", version=version, supersedesDirectionId=target))
        assert _code(response) == "conflict", tag
    # A pending Direction already exists, so a plain submit must supersede explicitly.
    assert _code(_send(seeded, _steer(job_id, command_id="cmd-c6-plain", version=version))) == "conflict"
    assert live in {d.direction_id for d in _directions(seeded, job_id) if d.state == "pending"}
    assert _state(seeded, job_id) | {"other": _state(seeded, other_job)} == settled


def test_supersession_refuses_another_actors_pending_direction(seeded: m1.Owned) -> None:
    job_id = _running(seeded)
    foreign = _seed_foreign_direction(seeded, job_id)
    settled = _state(seeded, job_id)
    response = _send(seeded, _steer(job_id, supersedesDirectionId=foreign))
    assert _code(response) == "conflict"
    assert _state(seeded, job_id) == settled


# --- atomicity -----------------------------------------------------------------------


@pytest.mark.parametrize("fault", (chat.StaleVersion("moved"), RuntimeError("storage fault")))
def test_a_cas_or_storage_failure_leaves_neither_half_of_the_mutation(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch, fault: Exception
) -> None:
    job_id = _running(seeded)
    first = _direction_id(_ok(_send(seeded, _steer(job_id))))
    settled = _state(seeded, job_id)

    def fail(*_a: Any, **_k: Any) -> None:
        raise fault

    monkeypatch.setattr(chat.ChatWriter, "update_generation_job_status_projection", fail)
    command = _steer(job_id, command_id="cmd-c6-fault", version=3, supersedesDirectionId=first)
    try:
        response = _send(seeded, command)
    except RuntimeError:
        response = None
    else:
        assert isinstance(response, ErrorResponseEnvelope)
    monkeypatch.undo()

    # Supersession, successor, both events and the job update all rolled back together.
    assert _state(seeded, job_id) == settled
    assert [d.state for d in _directions(seeded, job_id)] == ["pending"]
    assert _directions(seeded, job_id)[-1].direction_sequence == 1
    assert _direction_id(_ok(_send(seeded, command))) == _directions(seeded, job_id)[-1].direction_id
    assert _directions(seeded, job_id)[-1].direction_sequence == 2
