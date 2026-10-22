"""C10: a fork-created generation job (no queue row) is claimed, run and recovered.

`RegenerateResponse` and `CommitMessageAmendment` commit a `<commandId>.gen` job of their
own -- `queued`, no attempt, no event, no queued submission. The executor claims it through
`execute` (the hint `chat.command` wakes it with) or `execute_next` (the startup drain),
under the identities a submission's claim gives, and never touches a job that is already
running or finished.
"""

# ruff: noqa: F811

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.chat_generation import (
    GenerationConflict,
    GenerationNotFound,
)
from omnivia_core_runtime.service.chat_generation_executor import (
    ChatGenerationExecutor,
    GenerationExecutorError,
)
from omnivia_core_runtime.storage import chat
from test_chat_offline_queue import owned, seeded  # noqa: F401  (pytest fixtures)
from test_chat_rename_conversation import (
    AMEND_COMMAND_ID,
    REGENERATE_COMMAND_ID,
    ROOT_MESSAGE_ID,
    _amend,
    _regenerate,
    _seed_assistant_reply,
)
from test_chat_submit_message_resolver import (
    WALL,
    WORKSPACE_ID,
    _dispatcher,
    _executor_config,
    _request,
    _stream,
    _submit_command,
)

from omnivia_core.contracts.v1 import SuccessResponseEnvelope

REGEN_JOB = f"{REGENERATE_COMMAND_ID}.gen"
AMEND_JOB = f"{AMEND_COMMAND_ID}.gen"
SUBMIT_COMMAND_ID = "cmd-c10-fork-submit"
SUBMIT_JOB = f"{SUBMIT_COMMAND_ID}.gen"


class Crash(BaseException):
    """Process death: nothing in the executor catches it."""


def _executor(seeded: m1.Owned, requests: list[Any], *, crash: bool = False) -> ChatGenerationExecutor:
    def provider(request: Any) -> Iterator[Any]:
        requests.append(request)
        if crash:
            raise Crash
        yield from _stream(request, ("forked ", "reply"))

    return ChatGenerationExecutor(
        connection=seeded.connection,
        identity=seeded.identity,
        fencing_generation=seeded.generation,
        workspace_id=WORKSPACE_ID,
        clock=FakeClock(wall=WALL),
        invoke=provider,
        config=_executor_config(),
    )


def _fork(seeded: m1.Owned, kind: str) -> str:
    """Commit one fork with no wake; returns its job id."""
    if kind == "regenerate":
        _seed_assistant_reply(seeded)
        response = _regenerate(seeded)
        job_id = REGEN_JOB
    else:
        response = _amend(seeded)
        job_id = AMEND_JOB
    assert isinstance(response, SuccessResponseEnvelope), response
    return job_id


def _job(seeded: m1.Owned, job_id: str) -> chat.GenerationJob:
    job = chat.read_generation_job(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=job_id
    )
    assert job is not None
    return job


def _events(seeded: m1.Owned, job_id: str) -> list[tuple[str, int, str | None]]:
    return [
        (event.event_type, event.generation_event_sequence, event.generation_attempt_id)
        for event in chat.read_generation_events(
            seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=job_id
        )
    ]


def _status(seeded: m1.Owned, job_id: str) -> chat.GenerationJobStatusProjection:
    status = chat.read_generation_job_status_projection(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=job_id
    )
    assert status is not None
    return status


def _trigger(kind: str) -> str:
    return ROOT_MESSAGE_ID if kind == "regenerate" else "msg-c10-amended"


@pytest.mark.parametrize("kind", ["regenerate", "amend"])
def test_a_fork_hint_claims_the_job_and_the_provider_settles_it(
    seeded: m1.Owned, kind: str
) -> None:
    job_id = _fork(seeded, kind)
    queued = _job(seeded, job_id)
    assert queued.state == "queued" and queued.current_attempt_id is None
    assert _events(seeded, job_id) == []
    requests: list[Any] = []

    # Exactly the hint `chat.command` wakes the seam with.
    _executor(seeded, requests).execute(
        generation_job_id=job_id, trigger_message_id=_trigger(kind)
    )

    done = _job(seeded, job_id)
    attempt_id = done.current_attempt_id
    assert attempt_id is not None
    assert done.state == "succeeded", (done.sanitized_error_code, done.sanitized_error_detail)
    assert done.result_message_id is not None
    assert [request.job_id for request in requests] == [job_id]
    assert requests[0].attempt_id == attempt_id
    assert requests[0].causation_id == _trigger(kind)

    events = _events(seeded, job_id)
    assert [(kind_, sequence) for kind_, sequence, _ in events][:2] == [
        ("chat.generation.queued", 1),
        ("chat.generation.started", 2),
    ]
    queued_event = chat.read_generation_events(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=job_id
    )[0]
    assert dict(queued_event.payload) == {"attemptNumber": 1}
    assert {event[2] for event in events[1:]} == {attempt_id}

    status = _status(seeded, job_id)
    assert status.state == "succeeded" and status.result_message_id == done.result_message_id
    assert status.current_attempt_id == attempt_id

    reply = chat.read_message(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=done.conversation_id,
        message_id=done.result_message_id,
    )
    assert reply is not None and reply.created_on_branch_id == done.branch_id
    assert reply.parent_message_id == _trigger(kind)
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("kind", ["regenerate", "amend"])
def test_a_fork_job_snapshots_and_executes_on_its_command_route(
    seeded: m1.Owned, kind: str
) -> None:
    route = {
        "connectionId": "connection-selected",
        "modelId": "anthropic/claude-sonnet-4.6",
    }
    if kind == "regenerate":
        _seed_assistant_reply(seeded)
        response = _regenerate(seeded, providerRoute=route)
        job_id = REGEN_JOB
    else:
        response = _amend(seeded, providerRoute=route)
        job_id = AMEND_JOB
    assert isinstance(response, SuccessResponseEnvelope), response
    queued = _job(seeded, job_id)
    assert (queued.provider_connection_id, queued.provider_model_id) == (
        "connection-selected",
        "anthropic/claude-sonnet-4.6",
    )

    requests: list[Any] = []
    _executor(seeded, requests).execute(
        generation_job_id=job_id, trigger_message_id=_trigger(kind)
    )
    assert [(request.connection_id, request.model_id) for request in requests] == [
        ("connection-selected", "anthropic/claude-sonnet-4.6")
    ]


@pytest.mark.parametrize("kind", ["regenerate", "amend"])
def test_a_repeated_hint_neither_claims_nor_invokes_a_second_time(
    seeded: m1.Owned, kind: str
) -> None:
    job_id = _fork(seeded, kind)
    requests: list[Any] = []
    executor = _executor(seeded, requests)
    hint = {"generation_job_id": job_id, "trigger_message_id": _trigger(kind)}
    executor.execute(**hint)
    before = (_job(seeded, job_id), _events(seeded, job_id), _status(seeded, job_id))

    executor.execute(**hint)
    assert executor.execute_next() is None

    assert len(requests) == 1
    assert (_job(seeded, job_id), _events(seeded, job_id), _status(seeded, job_id)) == before


def test_a_running_fork_job_is_never_stolen_or_replayed(seeded: m1.Owned) -> None:
    job_id = _fork(seeded, "amend")
    requests: list[Any] = []
    with pytest.raises(Crash):  # the process dies with the provider stream open
        _executor(seeded, requests, crash=True).execute(
            generation_job_id=job_id, trigger_message_id=_trigger("amend")
        )
    running = _job(seeded, job_id)
    assert running.state == "running" and running.lease_epoch == 1
    before = (running, _events(seeded, job_id), _status(seeded, job_id))

    # A restart: neither the drain nor a late hint claims it or invokes the provider.
    restarted = _executor(seeded, requests)
    assert restarted.execute_next() is None
    restarted.execute(generation_job_id=job_id, trigger_message_id=_trigger("amend"))

    assert len(requests) == 1
    assert (_job(seeded, job_id), _events(seeded, job_id), _status(seeded, job_id)) == before


def test_the_startup_drain_runs_committed_forks_in_creation_order(seeded: m1.Owned) -> None:
    regen = _fork(seeded, "regenerate")
    amend_response = _amend(seeded, expected_sequence=2)
    assert isinstance(amend_response, SuccessResponseEnvelope), amend_response
    requests: list[Any] = []
    executor = _executor(seeded, requests)

    drained = []
    while (job_id := executor.execute_next()) is not None:
        drained.append(job_id)

    # Both were committed at the one frozen instant, so the job id breaks the tie.
    assert drained == [AMEND_JOB, regen]
    assert [request.job_id for request in requests] == drained
    assert {_job(seeded, regen).state, _job(seeded, AMEND_JOB).state} == {"succeeded"}


def test_the_drain_leaves_a_pending_submission_job_to_its_queue_row(seeded: m1.Owned) -> None:
    response = _dispatcher(seeded).dispatch(
        _request(
            _submit_command(command_id=SUBMIT_COMMAND_ID),
            command_name="SubmitMessage",
            idempotency_key=SUBMIT_COMMAND_ID,
            request_id=f"req-{SUBMIT_COMMAND_ID}",
        )
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    assert _job(seeded, SUBMIT_JOB).state == "queued"
    # A submission's job is queued and fresh, but its queue row owns it.
    assert (
        chat.read_next_executable_fork_generation_job(seeded.connection, workspace_id=WORKSPACE_ID)
        is None
    )
    requests: list[Any] = []
    executor = _executor(seeded, requests)
    with pytest.raises(GenerationConflict):
        executor.execute(generation_job_id=SUBMIT_JOB, trigger_message_id=f"{SUBMIT_COMMAND_ID}.msg")
    assert _job(seeded, SUBMIT_JOB).state == "queued" and requests == []

    # The ordinary drain still takes it, through its queue row.
    assert executor.execute_next() == SUBMIT_JOB
    assert _job(seeded, SUBMIT_JOB).state == "succeeded"


def test_a_hint_is_refused_before_any_write_when_it_does_not_match_a_fork_job(
    seeded: m1.Owned,
) -> None:
    job_id = _fork(seeded, "amend")
    requests: list[Any] = []
    executor = _executor(seeded, requests)
    before = (_job(seeded, job_id), _events(seeded, job_id), _status(seeded, job_id))

    with pytest.raises(GenerationConflict):  # not the trigger the job states
        executor.execute(generation_job_id=job_id, trigger_message_id=ROOT_MESSAGE_ID)
    with pytest.raises(GenerationNotFound):
        executor.execute(generation_job_id="job-nobody-opened", trigger_message_id=ROOT_MESSAGE_ID)
    with pytest.raises(GenerationExecutorError):  # no job, attempt or submission named
        executor.execute(trigger_message_id=ROOT_MESSAGE_ID)
    elsewhere = dataclasses.replace(executor, workspace_id="ws-elsewhere")
    with pytest.raises(GenerationNotFound):  # another workspace holds no such job
        elsewhere.execute(generation_job_id=job_id, trigger_message_id=_trigger("amend"))

    assert requests == []
    assert (_job(seeded, job_id), _events(seeded, job_id), _status(seeded, job_id)) == before


def test_a_job_whose_status_projection_moved_is_not_claimable(seeded: m1.Owned) -> None:
    job_id = _fork(seeded, "amend")
    status = _status(seeded, job_id)
    with chat.chat_writer(
        seeded.connection,
        seeded.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=seeded.generation,
    ) as writer:
        writer.update_generation_job_status_projection(
            generation_job_id=job_id,
            expected_version=status.version,
            state="queued",
            current_attempt_id=None,
            result_message_id=None,
            sanitized_error_code=None,
            sanitized_error_detail=None,
            finished_at_us=None,
            updated_at_us=status.updated_at_us + 1,
        )
    requests: list[Any] = []
    executor = _executor(seeded, requests)

    with pytest.raises(GenerationConflict):
        executor.execute(generation_job_id=job_id, trigger_message_id=_trigger("amend"))
    assert executor.execute_next() is None
    assert requests == [] and _events(seeded, job_id) == []
    assert _job(seeded, job_id).state == "queued"


def test_a_claim_that_dies_midway_leaves_the_job_queued_and_repeatable(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _fork(seeded, "amend")
    requests: list[Any] = []
    executor = _executor(seeded, requests)
    original: Callable[..., None] = chat.ChatWriter.update_generation_job_status_projection

    def die(self: Any, **fields: Any) -> None:
        raise Crash

    monkeypatch.setattr(chat.ChatWriter, "update_generation_job_status_projection", die)
    with pytest.raises(Crash):
        executor.execute(generation_job_id=job_id, trigger_message_id=_trigger("amend"))
    monkeypatch.setattr(chat.ChatWriter, "update_generation_job_status_projection", original)

    # The attempt, event and running job rolled back with the status write.
    queued = _job(seeded, job_id)
    assert queued.state == "queued" and queued.current_attempt_id is None
    assert _events(seeded, job_id) == [] and requests == []

    assert executor.execute_next() == job_id
    assert _job(seeded, job_id).state == "succeeded" and len(requests) == 1
