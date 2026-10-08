"""C6 acceptance for taking a Direction in at a provider iteration boundary.

A Direction an actor wrote while a Generation runs is never applied mid-stream. The one
point it can be applied is where a provider iteration has completed: the provider's
non-error `finish`. There it takes two commits, never one:

- Phase A closes the iteration (one identity-only `chat.generation.iteration_completed`
  event) and stages the successor: its provider invocation identity, its Attempt and its
  request manifest, which binds the Direction. The Direction, the current Attempt and the
  lease do not move, and no provider is invoked.
- Phase B, a transaction of its own, applies the Direction to what Phase A committed
  (`pending` v1 -> `applied` v2), appends the continuation record and the Direction's
  identity-only state event, and moves the Job row and the status projection to the
  successor in one step. Only then is the provider invoked for the successor. The
  Generation goes on as the successor and its final assistant Message holds the text of
  every attempt once.

These tests run the real executor and the real service functions over real SQLite with
every migration, and drive them with a scripted provider. What they hold:

- no pending Direction means no change at all to what a Generation writes, and a pending
  one writes no assistant Message, no outcome and no success until the last attempt ends;
- the two commits are separate: a crash during Phase B rolls back only Phase B, one after
  Phase A finds the staged rows and writes no second set, one after Phase B runs the
  successor that exists, and the provider is invoked only once Phase B has committed;
- the manifest binds the Direction exactly, in canonical bytes, and the iteration digest
  is the SHA-256 of the exact UTF-8 bytes of the durable chunks;
- the Job row, the status projection, the current Attempt and the lease stay coherent
  through the move, every durable change advances the public token once, and a replay
  advances none;
- everything that does not match the rows (a stale token, a lease another instance holds,
  a finished job, a tampered Direction or manifest, no Attempt left, a failing store) is
  refused whole, leaving nothing behind, and a Direction that cannot be applied is ended
  honestly with a constant reason;
- a provider event's `attemptId` is the provider's transport Attempt, never the Chat
  Attempt the request names: each stream keeps one of its own, and one that does not is
  refused before its finish can become a boundary;
- the events a continuation writes are identity-only and contiguous.

Exactly-once *external* provider invocation after a crash whose outcome is unknown is not
claimed anywhere here. What is held is that the invocation, the request and its
idempotency key are the same when it is repeated, and that the durable rows recover.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import test_chat_request_manifest as base
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service import chat_generation as gen
from omnivia_core_runtime.service import chat_generation_executor as executor_module
from omnivia_core_runtime.service.chat_generation_executor import (
    ChatGenerationExecutor,
    GenerationExecutorConfig,
)
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.connection import StorageError, authorised
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

m1 = base.m1
WORKSPACE_ID = base.WORKSPACE_ID
JOB_ID = base.JOB_ID
ACTOR_ID = base.ACTOR_ID
OTHER_ACTOR_ID = "actor-other-human"
#: The instant the fixed clock reads, and so the instant every executor write is stamped.
NOW_US = base.BASE_US + 1_000_000
FIRST_ATTEMPT = "genatt-" + hashlib.sha256(JOB_ID.encode("utf-8")).hexdigest()[:40]
#: The Job-level provider idempotency key `base.seed_conversation_and_queue` queues.
JOB_KEY = "manifest-idem-1"

FIRST = "First part."
SECOND = " Second part."
THIRD = " Third part."
STEER = "Focus on the cost section."
STEER_AGAIN = "Now shorten it."

ITERATION = "chat.generation.iteration_completed"
STATE_CHANGED = "chat.direction.state_changed"
SUCCEEDED = "chat.generation.succeeded"

#: Every relation a Generation, a Direction or a continuation can touch, dumped whole so
#: "nothing was written" is a statement about all of them rather than the one a test named.
DUMPED = (
    "omnivia_chat_conversations",
    "omnivia_chat_message_branches",
    "omnivia_chat_messages",
    "omnivia_chat_message_parts",
    "omnivia_chat_branch_head_events",
    "omnivia_chat_queued_submissions",
    "omnivia_chat_generation_jobs",
    "omnivia_chat_generation_job_status_projection",
    "omnivia_chat_generation_attempts",
    "omnivia_chat_generation_attempt_outcomes",
    "omnivia_chat_generation_events",
    "omnivia_chat_generation_text_chunks",
    "omnivia_chat_request_manifests",
    "omnivia_chat_directions",
    "omnivia_chat_generation_continuations",
    "omnivia_provider_invocations",
    "omnivia_chat_transactional_outbox",
)


class Crash(BaseException):
    """A simulated process death.

    Not an `Exception`, so nothing in the executor handles it: it unwinds through every
    `finally` and leaves the durable rows exactly as the last commit left them. A
    transaction open when it is raised is rolled back, as it would be by the process dying.
    """


# --- fixtures ---------------------------------------------------------------------


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
    """The graph one Generation runs over, its queued submission, and the head at the trigger."""
    base.seed_conversation_and_queue(owned)
    with base.writer(owned) as writer:
        writer.append_branch_head_event(
            event_id="head-event-continuation-trigger",
            conversation_id=base.CONVERSATION_ID,
            branch_id=base.BRANCH_ID,
            head_version=2,
            previous_head_message_id=base.ROOT_MESSAGE_ID,
            new_head_message_id=base.TRIGGER_MESSAGE_ID,
            cause="user_message_appended",
            command_id="command-continuation-trigger",
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


# --- the provider ------------------------------------------------------------------


def wire(request: Any, ordinal: int, event_type: str, **fields: Any) -> dict[str, Any]:
    """One normalized provider event, with the identities the request fixes."""
    return {
        "invocationId": request.invocation_id,
        # The provider's own transport Attempt beneath this invocation, never the Chat
        # Generation Attempt the request names (`request.attempt_id`).
        "attemptId": f"{request.invocation_id}.transport-1",
        "ordinal": ordinal,
        "eventType": event_type,
        "schemaVersion": 1,
        "occurredAt": "2055-01-01T00:00:01Z",
        "receivedAt": "2055-01-01T00:00:01Z",
        # Derived from the attempt and position, so a replayed stream carries the very ids
        # the first one did -- which is what makes a redelivery an idempotent one.
        "providerEventId": f"{request.attempt_id}.pe{ordinal}",
        **fields,
    }


def stream(
    request: Any,
    deltas: tuple[str, ...],
    *,
    before_finish: Callable[[], None] | None = None,
    finish: Mapping[str, Any] | None = None,
    omit_on_finish: tuple[str, ...] = (),
) -> Iterator[dict[str, Any]]:
    """One provider invocation: start, deltas, a hook at the boundary, then a terminal event."""
    yield wire(request, 0, "stream-start")
    for index, delta in enumerate(deltas, start=1):
        yield wire(request, index, "text-delta", delta=delta, partId="part-1", stepId="step-1")
    if before_finish is not None:
        before_finish()
    terminal = dict(finish or {"eventType": "finish", "finishReason": "stop"})
    event_type = str(terminal.pop("eventType"))
    event = wire(request, len(deltas) + 1, event_type, **terminal)
    for key in omit_on_finish:
        del event[key]
    yield event


class Scripted:
    """A provider that plays one script per invocation, in order, recording every request."""

    def __init__(self, *scripts: Callable[[Any], Iterator[dict[str, Any]]]) -> None:
        self.scripts = list(scripts)
        self.requests: list[Any] = []

    def __call__(self, request: Any) -> Iterator[dict[str, Any]]:
        self.requests.append(request)
        return self.scripts[len(self.requests) - 1](request)


def play(*deltas: str, **options: Any) -> Callable[[Any], Iterator[dict[str, Any]]]:
    return lambda request: stream(request, deltas, **options)


def dies(request: Any) -> Iterator[dict[str, Any]]:
    """A provider call the process does not survive: the request was made, nothing came back."""
    raise Crash


def make_executor(
    holder: m1.Owned,
    provider: Callable[[Any], Iterator[dict[str, Any]]],
    *,
    clock: FakeClock | None = None,
    **fields: Any,
) -> ChatGenerationExecutor:
    return ChatGenerationExecutor(
        connection=holder.connection,
        identity=holder.identity,
        fencing_generation=holder.generation,
        workspace_id=WORKSPACE_ID,
        clock=clock or FakeClock(wall=base.WALL),
        invoke=provider,
        config=GenerationExecutorConfig(
            connection_id="provider-connection-manifest-1",
            model_id="provider-model-manifest-1",
            policy_ref="policy-manifest-1",
            classification_ref="classification-manifest-1",
            residency_ref="residency-manifest-1",
            service_actor_id="actor-core-chat",
        ),
        **fields,
    )


def run(
    holder: m1.Owned,
    provider: Callable[[Any], Iterator[dict[str, Any]]],
    **fields: Any,
) -> ChatGenerationExecutor:
    """Run the Generation's first entry point, as the SubmitMessage handler does.

    Each call is a fresh executor over the same durable rows, which is what a restarted
    process is.
    """
    executor = make_executor(holder, provider, **fields)
    executor.execute_submission(
        queued_submission_id=base.QUEUE_ID,
        generation_job_id=JOB_ID,
        trigger_message_id=base.TRIGGER_MESSAGE_ID,
    )
    return executor


def submit_direction(
    holder: m1.Owned,
    *,
    text: str = STEER,
    sequence: int = 1,
    actor_id: str = ACTOR_ID,
    target: str | None = None,
    supersedes: str | None = None,
    created_at_us: int = NOW_US,
    content: Mapping[str, Any] | None = None,
) -> str:
    """What the admission lane leaves behind: one pending Direction aimed at the running attempt."""
    attempt = target if target is not None else str(status(holder).current_attempt_id)
    direction_id = f"direction-{sequence}"
    with base.writer(holder) as writer:
        if supersedes is not None:
            writer.update_direction(
                direction_id=supersedes,
                expected_version=1,
                state="superseded",
                terminal_at_us=NOW_US,
                superseded_by_direction_id=direction_id,
                terminal_reason={
                    "code": "superseded",
                    "occurredAt": "2055-01-01T00:00:01Z",
                },
            )
        writer.append_direction(
            direction_id=direction_id,
            conversation_id=base.CONVERSATION_ID,
            branch_id=base.BRANCH_ID,
            generation_job_id=JOB_ID,
            target_attempt_id=attempt,
            direction_sequence=sequence,
            actor_id=actor_id,
            content=content or {"editableParts": [{"payload": {"text": text}, "type": "text"}]},
            created_at_us=created_at_us,
            supersedes_direction_id=supersedes,
        )
    return direction_id


def steer(holder: m1.Owned, **fields: Any) -> Callable[[], None]:
    """A boundary hook: the actor writes a Direction just before the provider finishes."""
    return lambda: submit_direction(holder, **fields)


def admit_direction(holder: m1.Owned, **fields: Any) -> str:
    """The admission lane in full: the Direction, its `pending` event and the Job's advanced token.

    Admission is itself a durable change to the Job: an event, the Job row's sequence and
    one step of the public token. The iteration event at the boundary must carry the
    token as it stands after it.
    """
    direction_id = submit_direction(holder, **fields)
    job = job_row(holder)
    current = status(holder)
    sequence = len(events(holder)) + 1
    with base.writer(holder) as writer:
        writer.append_generation_event(
            event_id=f"{JOB_ID}.e{sequence}",
            conversation_id=base.CONVERSATION_ID,
            branch_id=base.BRANCH_ID,
            generation_job_id=JOB_ID,
            event_type=STATE_CHANGED,
            generation_event_sequence=sequence,
            trigger_message_id=base.TRIGGER_MESSAGE_ID,
            cursor=f"{JOB_ID}:{sequence:06d}",
            payload={"directionId": direction_id, "directionVersion": 1, "state": "pending"},
            occurred_at_us=NOW_US,
            schema_version=1,
        )
        writer.update_generation_job(
            generation_job_id=JOB_ID,
            expected_state="running",
            expected_lease_epoch=job.lease_epoch,
            state="running",
            lease_epoch=job.lease_epoch,
            current_attempt_id=job.current_attempt_id,
            lease_owner=job.lease_owner,
            lease_expires_at_us=job.lease_expires_at_us,
            heartbeat_at_us=job.heartbeat_at_us,
            last_event_sequence=sequence,
            updated_at_us=NOW_US,
            started_at_us=job.started_at_us,
        )
        writer.update_generation_job_status_projection(
            generation_job_id=JOB_ID,
            expected_version=current.version,
            state="running",
            current_attempt_id=current.current_attempt_id,
            updated_at_us=NOW_US,
        )
    return direction_id


def stop_generation(holder: m1.Owned) -> None:
    """What the stop lane leaves: the Generation cancelled, durably, under the running attempt."""
    gen.append_provider_generation_event(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        generation_job_id=JOB_ID,
        generation_attempt_id=str(status(holder).current_attempt_id),
        provider_event={"eventType": "finish", "finishReason": "cancelled"},
        now_us=NOW_US,
        lease_owner=holder.identity.service_instance_id,
    )


def fail_generation(holder: m1.Owned) -> None:
    gen.append_provider_generation_event(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        generation_job_id=JOB_ID,
        generation_attempt_id=str(status(holder).current_attempt_id),
        provider_event={
            "eventType": "error",
            "errorCode": "timeout",
            "retryable": False,
            "statusClass": "timeout",
            "safeMessage": "provider timed out safely",
        },
        now_us=NOW_US,
        lease_owner=holder.identity.service_instance_id,
    )


# --- reading what a Generation left --------------------------------------------------


def status(holder: m1.Owned) -> chat.GenerationJobStatusProjection:
    found = chat.read_generation_job_status_projection(
        holder.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert found is not None
    return found


def job_row(holder: m1.Owned) -> chat.GenerationJob:
    found = chat.read_generation_job(
        holder.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert found is not None
    return found


def events(holder: m1.Owned, event_type: str | None = None) -> list[chat.GenerationEvent]:
    found = chat.read_generation_events(
        holder.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    return [event for event in found if event_type is None or event.event_type == event_type]


def event_types(holder: m1.Owned) -> list[str]:
    return [event.event_type for event in events(holder)]


def attempts(holder: m1.Owned) -> tuple[chat.GenerationAttempt, ...]:
    return chat.read_generation_attempts(
        holder.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )


def direction(holder: m1.Owned, sequence: int = 1) -> chat.Direction:
    found = chat.read_direction(
        holder.connection, workspace_id=WORKSPACE_ID, direction_id=f"direction-{sequence}"
    )
    assert found is not None
    return found


def continuation_of(holder: m1.Owned, sequence: int = 1) -> chat.GenerationContinuation:
    found = chat.read_generation_continuation_for_direction(
        holder.connection, workspace_id=WORKSPACE_ID, direction_id=f"direction-{sequence}"
    )
    assert found is not None
    return found


def manifest_of(holder: m1.Owned, attempt_id: str) -> chat.RequestManifest:
    found = chat.read_request_manifest(
        holder.connection, workspace_id=WORKSPACE_ID, generation_attempt_id=attempt_id
    )
    assert found is not None
    return found


def ids_of(direction_sequence: int = 1, iteration: int = 1) -> gen.ContinuationIdentities:
    return gen.continuation_identities(
        generation_job_id=JOB_ID,
        direction_id=f"direction-{direction_sequence}",
        iteration=iteration,
    )


def staged(holder: m1.Owned, identities: gen.ContinuationIdentities | None = None) -> dict[str, Any]:
    """Everything Phase A leaves, as the rows read, whichever of it exists."""
    wanted = identities or ids_of()
    return {
        "events": events(holder, ITERATION),
        "invocation": chat.read_provider_invocation(
            holder.connection,
            workspace_id=WORKSPACE_ID,
            invocation_id=wanted.provider_invocation_id,
        ),
        "attempt": next(
            (a for a in attempts(holder) if a.generation_attempt_id == wanted.generation_attempt_id),
            None,
        ),
        "manifest": chat.read_request_manifest(
            holder.connection,
            workspace_id=WORKSPACE_ID,
            generation_attempt_id=wanted.generation_attempt_id,
        ),
    }


def assistant_messages(holder: m1.Owned) -> list[chat.Message]:
    return [
        message
        for message in chat.read_messages_by_conversation_sequence(
            holder.connection, workspace_id=WORKSPACE_ID, conversation_id=base.CONVERSATION_ID
        )
        if message.role == "assistant"
    ]


def assistant_text(holder: m1.Owned) -> str:
    messages = assistant_messages(holder)
    assert len(messages) == 1, "the Generation commits its response Message exactly once"
    parts = chat.read_message_parts(
        holder.connection, workspace_id=WORKSPACE_ID, message_id=messages[0].message_id
    )
    assert len(parts) == 1
    return str(parts[0].payload["text"])


def count(holder: m1.Owned, table: str) -> int:
    return m1.count(holder.connection, table)


def dump(holder: m1.Owned) -> dict[str, list[tuple[Any, ...]]]:
    return {
        table: sorted(
            (tuple(row) for row in holder.connection.execute(f"SELECT * FROM {table}")),
            key=repr,
        )
        for table in DUMPED
    }


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def message_texts(request: Any) -> list[tuple[str, tuple[str, ...]]]:
    """(role, text parts) of a request's messages: the model-visible conversation."""
    return [
        (message["role"], tuple(part["text"] for part in message["parts"]))
        for message in request.to_wire()["messages"]
    ]


def settled_ok(holder: m1.Owned) -> None:
    assert holder.connection.in_transaction is False
    assert holder.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert holder.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def assert_event_sequence_is_contiguous(holder: m1.Owned, *, job_row_follows: bool = True) -> None:
    found = events(holder)
    assert [event.generation_event_sequence for event in found] == list(
        range(1, len(found) + 1)
    )
    assert [event.cursor for event in found] == [f"{JOB_ID}:{n:06d}" for n in range(1, len(found) + 1)]
    if job_row_follows:
        assert job_row(holder).last_event_sequence == len(found)


def assert_text_confined(holder: m1.Owned, needle: str, *allowed: str) -> None:
    """`needle` appears in no relation but the `allowed` ones, which are where it belongs."""
    for table, rows in dump(holder).items():
        if table not in allowed:
            assert needle not in repr(rows), f"{needle!r} leaked into {table}"


def manifest_digest_of(raw_body: str) -> str:
    return "sha256:" + hashlib.sha256(raw_body.encode("utf-8")).hexdigest()


def tamper(holder: m1.Owned, *statements: str) -> None:
    """Rewrite committed rows from outside the guards, as a bad disk or a hand edit would.

    The runtime refuses this from inside; a reader still has to behave when the file was
    edited, and this is the only way to make one while the owning connection stays open.
    """
    with authorised(holder.connection, mutations=True, ddl=True):
        for statement in statements:
            holder.connection.execute(statement)


class Boundary:
    """A Generation stopped at its first iteration boundary, nothing of the seam written.

    The prior Attempt has finished streaming and its chunks are durable, a Direction is
    pending against it, and the arguments the executor would pass the service are in
    hand, so a test can call either phase itself, in any order, against real rows.
    """

    def __init__(self, holder: m1.Owned, kwargs: dict[str, Any]) -> None:
        self.holder = holder
        self.kwargs = kwargs

    @property
    def identities(self) -> gen.ContinuationIdentities:
        identities: gen.ContinuationIdentities = self.kwargs["identities"]
        return identities

    @property
    def manifest_body(self) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(json.dumps(self.kwargs["manifest_body"]))
        return body

    def _call(self, function: Callable[..., Any], **kwargs: Any) -> Any:
        return function(
            self.holder.connection,
            self.holder.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=self.holder.generation,
            **kwargs,
        )

    def stage(self, **overrides: Any) -> gen.StagedContinuation:
        result: gen.StagedContinuation = self._call(
            gen.stage_direction_continuation, **{**self.kwargs, **overrides}
        )
        return result

    def apply(self, **overrides: Any) -> gen.DirectionContinuation:
        kwargs = {
            key: value
            for key, value in self.kwargs.items()
            if key not in {"provider_event", "prior_output_digest"}
        }
        kwargs["expected_status_version"] = status(self.holder).version
        result: gen.DirectionContinuation = self._call(
            gen.apply_direction_continuation, **{**kwargs, **overrides}
        )
        return result


@pytest.fixture
def at_boundary(seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch) -> Boundary:
    captured: dict[str, Any] = {}

    def capture(connection: Any, identity: Any, **kwargs: Any) -> Any:
        captured.update(
            {
                key: value
                for key, value in kwargs.items()
                if key not in {"workspace_id", "fencing_generation"}
            }
        )
        raise Crash

    with monkeypatch.context() as patch:
        patch.setattr(executor_module, "stage_direction_continuation", capture)
        with pytest.raises(Crash):
            run(seeded, Scripted(play(FIRST, before_finish=steer(seeded))))
    return Boundary(seeded, captured)


def spy_on_the_seam(patch: pytest.MonkeyPatch, holder: m1.Owned) -> dict[str, Any]:
    """Record the public token at the instant each phase is entered and left."""
    seen: dict[str, Any] = {}
    real_stage = executor_module.stage_direction_continuation
    real_apply = executor_module.apply_direction_continuation

    def stage(*args: Any, **kwargs: Any) -> Any:
        seen["entering_a"] = status(holder).version
        result = real_stage(*args, **kwargs)
        seen["leaving_a"] = status(holder).version
        return result

    def apply(*args: Any, **kwargs: Any) -> Any:
        seen["entering_b"] = status(holder).version
        result = real_apply(*args, **kwargs)
        seen["leaving_b"] = status(holder).version
        return result

    patch.setattr(executor_module, "stage_direction_continuation", stage)
    patch.setattr(executor_module, "apply_direction_continuation", apply)
    return seen


def crash_instead_of_applying(patch: pytest.MonkeyPatch) -> None:
    def never_commits(*args: Any, **kwargs: Any) -> Any:
        raise Crash

    patch.setattr(executor_module, "apply_direction_continuation", never_commits)


# --- 1: no pending Direction -------------------------------------------------------


def test_a_boundary_with_no_pending_direction_settles_exactly_as_it_always_did(
    seeded: m1.Owned,
) -> None:
    provider = Scripted(play("hello ", "world"))

    run(seeded, provider)

    assert len(provider.requests) == 1
    assert event_types(seeded) == [
        "chat.generation.queued",
        "chat.generation.started",
        "chat.generation.text_appended",
        "chat.generation.text_appended",
        "chat.generation.succeeded",
    ]
    assert [attempt.attempt_number for attempt in attempts(seeded)] == [1]
    assert status(seeded).state == "succeeded"
    assert assistant_text(seeded) == "hello world"
    for table in (
        "omnivia_chat_directions",
        "omnivia_chat_generation_continuations",
        "omnivia_provider_invocations",
    ):
        assert count(seeded, table) == 0
    body = manifest_of(seeded, FIRST_ATTEMPT).manifest_body
    assert "direction" not in body and "continuation" not in body
    assert body["promptBundle"]["id"] == "core.chat.default-model-visible-context"
    # The first attempt keeps the Job's own provider idempotency key.
    assert provider.requests[0].idempotency_key == JOB_KEY
    settled_ok(seeded)


# --- 2: one Direction, one successor ----------------------------------------------


def test_a_pending_direction_is_taken_in_by_exactly_one_successor_attempt(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(wall=base.WALL)
    seen: dict[str, Any] = spy_on_the_seam(monkeypatch, seeded)

    def first(request: Any) -> Iterator[dict[str, Any]]:
        def at_boundary() -> None:
            submit_direction(seeded)
            seen["pending"] = direction(seeded)

        return stream(request, (FIRST,), before_finish=at_boundary)

    def second(request: Any) -> Iterator[dict[str, Any]]:
        seen["version_at_successor"] = status(seeded).version
        seen["job_at_successor"] = job_row(seeded)
        return stream(request, (SECOND,))

    provider = Scripted(first, second)
    run(seeded, provider, clock=clock)

    # Two provider invocations, one per attempt; one Generation, finished by the second.
    assert len(provider.requests) == 2
    first_attempt, successor = attempts(seeded)
    assert first_attempt.generation_attempt_id == FIRST_ATTEMPT
    assert (successor.attempt_number, successor.retry_of_attempt_id) == (2, FIRST_ATTEMPT)
    identities = ids_of()
    assert successor.generation_attempt_id == identities.generation_attempt_id
    assert successor.generation_attempt_id != FIRST_ATTEMPT
    assert successor.provider_invocation_id == identities.provider_invocation_id
    assert provider.requests[1].attempt_id == successor.generation_attempt_id
    assert provider.requests[1].invocation_id == successor.provider_invocation_id

    # The Direction: pending v1 with no boundary, then applied v2 at next_iteration, with
    # the application bound to the successor's own rows.
    pending = seen["pending"]
    assert (pending.state, pending.version, pending.effective_boundary) == ("pending", 1, None)
    assert pending.requested_boundary == "earliest_safe"
    applied = direction(seeded)
    assert (applied.state, applied.version) == ("applied", 2)
    assert (applied.requested_boundary, applied.effective_boundary) == (
        "earliest_safe",
        "next_iteration",
    )
    assert applied.target_attempt_id == FIRST_ATTEMPT
    assert applied.applied_attempt_id == successor.generation_attempt_id != applied.target_attempt_id
    assert applied.applied_provider_invocation_id == successor.provider_invocation_id
    assert applied.applied_request_manifest_id == identities.request_manifest_id
    assert applied.applied_iteration == 1
    assert applied.terminal_at_us == applied.updated_at_us == NOW_US
    assert applied.terminal_reason is None
    manifest = manifest_of(seeded, successor.generation_attempt_id)
    assert manifest.request_manifest_id == applied.applied_request_manifest_id
    assert manifest.provider_invocation_id == applied.applied_provider_invocation_id

    # The continuation record: this Direction, at its applied version, between these Attempts.
    continuation = continuation_of(seeded)
    assert continuation.continuation_id == identities.continuation_id
    assert (continuation.prior_attempt_id, continuation.next_attempt_id) == (
        FIRST_ATTEMPT,
        successor.generation_attempt_id,
    )
    assert (continuation.direction_id, continuation.direction_version) == ("direction-1", 2)
    assert continuation.prior_output_digest == digest(FIRST)
    assert count(seeded, "omnivia_chat_generation_continuations") == 1
    assert count(seeded, "omnivia_provider_invocations") == 1

    # The events, in the order the seam wrote them, and the iteration's evidence.
    assert event_types(seeded) == [
        "chat.generation.queued",
        "chat.generation.started",
        "chat.generation.text_appended",
        ITERATION,
        STATE_CHANGED,
        "chat.generation.started",
        "chat.generation.text_appended",
        SUCCEEDED,
    ]
    closed = events(seeded, ITERATION)[0]
    assert closed.generation_attempt_id == FIRST_ATTEMPT
    assert closed.generation_event_sequence == 4
    assert closed.provider_event_id == f"{FIRST_ATTEMPT}.pe2" == continuation.provider_finish_id
    assert closed.cursor == f"{JOB_ID}:000004"
    assert closed.occurred_at_us == NOW_US
    # The token the event recorded is the one that stood when the iteration completed.
    recorded = closed.payload["generationJobVersion"]
    assert dict(closed.payload) == {
        "generationJobVersion": recorded,
        "iteration": 0,
        "priorOutputDigest": digest(FIRST),
    }
    assert recorded == seen["entering_a"]
    changed = events(seeded, STATE_CHANGED)[0]
    assert changed.generation_attempt_id is None and changed.result_message_id is None
    assert dict(changed.payload) == {
        "directionId": "direction-1",
        "directionVersion": 2,
        "state": "applied",
    }

    # The public token the event recorded was advanced once by the event (Phase A) and
    # once by the transition (Phase B): the status projection moved to the successor once.
    assert (seen["leaving_a"], seen["entering_b"]) == (recorded + 1, recorded + 1)
    assert seen["leaving_b"] == seen["version_at_successor"] == recorded + 2
    assert seen["job_at_successor"].current_attempt_id == successor.generation_attempt_id
    assert seen["job_at_successor"].lease_owner == seeded.identity.service_instance_id

    # The Generation went on as the successor and ended there, with both attempts' text once.
    final = status(seeded)
    assert (final.state, final.current_attempt_id) == ("succeeded", successor.generation_attempt_id)
    assert assistant_text(seeded) == FIRST + SECOND
    assert [
        chunk.text_content
        for chunk in chat.read_generation_text_chunks(
            seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
        )
    ] == [FIRST, SECOND]
    assert_event_sequence_is_contiguous(seeded)
    settled_ok(seeded)


def test_the_successors_provider_identity_is_written_through_the_typed_writer(
    seeded: m1.Owned,
) -> None:
    run(seeded, Scripted(play(FIRST, before_finish=steer(seeded)), play(SECOND)))

    identities = ids_of()
    invocation = chat.read_provider_invocation(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        invocation_id=identities.provider_invocation_id,
    )
    assert invocation == chat.ProviderInvocation(
        workspace_id=WORKSPACE_ID,
        invocation_id=identities.provider_invocation_id,
        conversation_id=base.CONVERSATION_ID,
        job_id=JOB_ID,
        generation_attempt_id=identities.generation_attempt_id,
        operation="language.stream",
        configured_connection_id="provider-connection-manifest-1",
        configured_model_id="provider-model-manifest-1",
        created_at_us=NOW_US,
    )
    # Only the successor has one: the first attempt of a Job never did.
    assert count(seeded, "omnivia_provider_invocations") == 1
    for module in (gen, executor_module):
        assert "omnivia_provider_invocations" not in Path(module.__file__).read_text()
    settled_ok(seeded)


def test_a_second_direction_at_the_successors_boundary_makes_a_third_attempt(
    seeded: m1.Owned,
) -> None:
    provider = Scripted(
        play(FIRST, before_finish=steer(seeded)),
        play(SECOND, before_finish=steer(seeded, sequence=2, text=STEER_AGAIN)),
        play(THIRD),
    )

    run(seeded, provider)

    first_attempt, second_attempt, third_attempt = attempts(seeded)
    assert [a.attempt_number for a in (first_attempt, second_attempt, third_attempt)] == [1, 2, 3]
    assert second_attempt.retry_of_attempt_id == first_attempt.generation_attempt_id
    assert third_attempt.retry_of_attempt_id == second_attempt.generation_attempt_id
    assert direction(seeded, 1).applied_iteration == 1
    assert direction(seeded, 2).applied_iteration == 2
    assert direction(seeded, 2).target_attempt_id == second_attempt.generation_attempt_id
    assert third_attempt.generation_attempt_id == ids_of(2, 2).generation_attempt_id
    assert [event.payload["iteration"] for event in events(seeded, ITERATION)] == [0, 1]
    assert event_types(seeded).count(STATE_CHANGED) == 2

    # Each successor's context carries every earlier output and Direction, in order.
    assert message_texts(provider.requests[1])[-2:] == [("assistant", (FIRST,)), ("user", (STEER,))]
    assert message_texts(provider.requests[2])[-4:] == [
        ("assistant", (FIRST,)),
        ("user", (STEER,)),
        ("assistant", (SECOND,)),
        ("user", (STEER_AGAIN,)),
    ]
    # Three requests, three provider idempotency keys: no successor can be answered with
    # an earlier attempt's response.
    keys = [request.idempotency_key for request in provider.requests]
    assert keys[0] == JOB_KEY
    assert len(set(keys)) == 3
    assert [
        manifest_of(seeded, a.generation_attempt_id).idempotency_key
        for a in (first_attempt, second_attempt, third_attempt)
    ] == keys
    lineage = manifest_of(seeded, third_attempt.generation_attempt_id).manifest_body["continuation"]
    assert [hop["directionId"] for hop in lineage["hops"]] == ["direction-1", "direction-2"]

    assert assistant_text(seeded) == FIRST + SECOND + THIRD
    assert count(seeded, "omnivia_chat_generation_continuations") == 2
    assert count(seeded, "omnivia_provider_invocations") == 2
    assert_event_sequence_is_contiguous(seeded)
    settled_ok(seeded)


# --- 3: the two commits ------------------------------------------------------------


def test_phase_a_is_committed_before_the_direction_is_applied_and_the_provider_waits_for_phase_b(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace: list[str] = []
    seen: dict[str, Any] = {}
    real_stage = executor_module.stage_direction_continuation
    real_apply = executor_module.apply_direction_continuation

    def committed_state() -> dict[str, Any]:
        identities = ids_of()
        return {
            "in_transaction": seeded.connection.in_transaction,
            "direction": (direction(seeded).state, direction(seeded).version),
            "continuations": count(seeded, "omnivia_chat_generation_continuations"),
            "state_events": len(events(seeded, STATE_CHANGED)),
            "status": (status(seeded).current_attempt_id, status(seeded).version),
            "job": job_row(seeded).current_attempt_id,
            "staged": staged(seeded, identities),
            "assistant_messages": len(assistant_messages(seeded)),
            "outcomes": count(seeded, "omnivia_chat_generation_attempt_outcomes"),
        }

    def stage(*args: Any, **kwargs: Any) -> Any:
        trace.append("A")
        seen["before_a"] = committed_state()
        result = real_stage(*args, **kwargs)
        seen["after_a"] = committed_state()
        return result

    def apply(*args: Any, **kwargs: Any) -> Any:
        trace.append("B")
        seen["before_b"] = committed_state()
        result = real_apply(*args, **kwargs)
        seen["after_b"] = committed_state()
        return result

    def successor(request: Any) -> Iterator[dict[str, Any]]:
        trace.append("provider:successor")
        seen["at_provider"] = committed_state()
        return stream(request, (SECOND,))

    monkeypatch.setattr(executor_module, "stage_direction_continuation", stage)
    monkeypatch.setattr(executor_module, "apply_direction_continuation", apply)
    provider = Scripted(play(FIRST, before_finish=steer(seeded)), successor)
    run(seeded, provider)

    assert trace == ["A", "B", "provider:successor"]
    identities = ids_of()
    before_a, after_a, before_b = seen["before_a"], seen["after_a"], seen["before_b"]

    # Before Phase A nothing of the seam exists.
    assert before_a["staged"]["attempt"] is None and before_a["staged"]["manifest"] is None
    assert before_a["staged"]["invocation"] is None and before_a["staged"]["events"] == []

    # After Phase A, and with no transaction open, the staged rows are all committed and
    # the Direction, the current Attempt and the lease are exactly where they were.
    assert after_a["in_transaction"] is False and before_b["in_transaction"] is False
    assert after_a["staged"] == before_b["staged"]
    staged_rows = after_a["staged"]
    assert [event.generation_attempt_id for event in staged_rows["events"]] == [FIRST_ATTEMPT]
    assert staged_rows["invocation"].invocation_id == identities.provider_invocation_id
    assert staged_rows["attempt"].generation_attempt_id == identities.generation_attempt_id
    assert staged_rows["attempt"].state == "running"
    assert staged_rows["manifest"].manifest_body["direction"] == {
        "contentDigest": direction(seeded).content_digest,
        "directionId": "direction-1",
        "directionVersion": 1,
    }
    assert before_b["direction"] == ("pending", 1)
    assert before_b["continuations"] == 0 and before_b["state_events"] == 0
    assert before_b["status"][0] == before_b["job"] == FIRST_ATTEMPT
    assert before_b["status"][1] == before_a["status"][1] + 1  # the iteration event's one step

    # Phase B moved the Direction, the continuation and both authorities together...
    after_b = seen["after_b"]
    assert after_b["direction"] == ("applied", 2)
    assert (after_b["continuations"], after_b["state_events"]) == (1, 1)
    assert after_b["status"] == (identities.generation_attempt_id, before_b["status"][1] + 1)
    assert after_b["job"] == identities.generation_attempt_id
    # ...and the staged rows were not touched by it.
    assert after_b["staged"] == after_a["staged"]

    # The provider for the successor was invoked only after that, and nothing of the
    # first iteration had been settled by then.
    at_provider = seen["at_provider"]
    assert at_provider["direction"] == ("applied", 2) and at_provider["continuations"] == 1
    assert at_provider["status"][0] == at_provider["job"] == identities.generation_attempt_id
    assert at_provider["in_transaction"] is False
    assert (at_provider["assistant_messages"], at_provider["outcomes"]) == (0, 0)
    assert SUCCEEDED not in [event.event_type for event in events(seeded)[:-1]]
    assert assistant_text(seeded) == FIRST + SECOND
    settled_ok(seeded)


def test_the_two_phases_cannot_share_one_transaction(at_boundary: Boundary) -> None:
    holder = at_boundary.holder
    before = dump(holder)

    # A transaction the caller already holds: the fence cannot open another inside it.
    with base.writer(holder), pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        at_boundary.stage()

    assert dump(holder) == before
    settled_ok(holder)


def test_phase_a_stages_without_applying_and_phase_b_stages_nothing(at_boundary: Boundary) -> None:
    holder = at_boundary.holder

    # Phase B on its own has nothing to apply to, and does not create it.
    before = dump(holder)
    with pytest.raises(gen.GenerationConflict, match="no single committed iteration event"):
        at_boundary.apply()
    assert dump(holder) == before

    result = at_boundary.stage()
    assert result.replayed is False
    assert result.attempt.attempt_number == 2 and result.attempt.state == "running"
    assert result.attempt.retry_of_attempt_id == FIRST_ATTEMPT
    assert result.iteration_event.event_type == ITERATION
    assert result.manifest.provider_invocation_id == result.invocation.invocation_id
    assert direction(holder).state == "pending" and direction(holder).version == 1
    assert count(holder, "omnivia_chat_generation_continuations") == 0
    assert STATE_CHANGED not in event_types(holder)
    assert status(holder).current_attempt_id == job_row(holder).current_attempt_id == FIRST_ATTEMPT

    # With a staged row gone, Phase B refuses and writes nothing: it never creates what
    # Phase A should have staged.
    tamper(
        holder,
        "DROP TRIGGER omnivia_guard_chat_request_manifests_delete",
        f"DELETE FROM omnivia_chat_request_manifests WHERE generation_attempt_id = '{result.attempt.generation_attempt_id}'",
    )
    before = dump(holder)
    with pytest.raises(gen.GenerationConflict, match="is not staged"):
        at_boundary.apply()
    assert dump(holder) == before


def test_each_commit_advances_the_public_token_once_and_a_replay_advances_none(
    at_boundary: Boundary,
) -> None:
    holder = at_boundary.holder
    start = status(holder).version

    first = at_boundary.stage()
    assert first.replayed is False
    assert status(holder).version == start + 1
    assert first.iteration_event.payload["generationJobVersion"] == start
    after_a = dump(holder)

    again = at_boundary.stage()
    assert again.replayed is True
    assert (again.iteration_event, again.invocation, again.attempt, again.manifest) == (
        first.iteration_event,
        first.invocation,
        first.attempt,
        first.manifest,
    )
    assert dump(holder) == after_a
    assert status(holder).version == start + 1

    applied = at_boundary.apply()
    assert applied.replayed is False
    assert status(holder).version == start + 2
    after_b = dump(holder)

    # A replay of Phase B ignores a token it no longer needs, and moves nothing.
    replay = at_boundary.apply(expected_status_version=start)
    assert replay.replayed is True
    assert replay.continuation == applied.continuation and replay.direction == applied.direction
    assert dump(holder) == after_b
    assert at_boundary.stage().replayed is True  # and so is a late replay of Phase A
    assert dump(holder) == after_b
    assert status(holder).version == start + 2

    assert_event_sequence_is_contiguous(holder)
    settled_ok(holder)


def test_the_iteration_event_carries_the_token_after_a_direction_admission_at_the_boundary(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = spy_on_the_seam(monkeypatch, seeded)

    def admitted() -> None:
        before = status(seeded).version
        admit_direction(seeded)
        seen["admission_step"] = status(seeded).version - before
        seen["version_after_admission"] = status(seeded).version

    run(seeded, Scripted(play(FIRST, before_finish=admitted), play(SECOND)))

    assert seen["admission_step"] == 1
    # The event holds the token as it stood when it was inserted: that includes the
    # admission's step, and it is never rebuilt afterwards.
    closed = events(seeded, ITERATION)[0]
    assert closed.payload["generationJobVersion"] == seen["entering_a"]
    assert seen["entering_a"] >= seen["version_after_admission"]
    # Both `state_changed` events are identity-only, and the sequence runs on unbroken.
    assert [dict(e.payload) for e in events(seeded, STATE_CHANGED)] == [
        {"directionId": "direction-1", "directionVersion": 1, "state": "pending"},
        {"directionId": "direction-1", "directionVersion": 2, "state": "applied"},
    ]
    assert event_types(seeded)[2:7] == [
        "chat.generation.text_appended",
        STATE_CHANGED,
        ITERATION,
        STATE_CHANGED,
        "chat.generation.started",
    ]
    assert_event_sequence_is_contiguous(seeded)
    settled_ok(seeded)


def test_every_write_is_stamped_no_earlier_than_what_it_follows(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clock that moves between the phases, and a Direction written ahead of the clock."""
    ahead = NOW_US + 3_000_000
    later = NOW_US + 9_000_000
    real_apply = executor_module.apply_direction_continuation

    def apply_later(*args: Any, **kwargs: Any) -> Any:
        kwargs["now_us"] = later
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(executor_module, "apply_direction_continuation", apply_later)
    run(
        seeded,
        Scripted(
            play(FIRST, before_finish=steer(seeded, created_at_us=ahead)),
            play(SECOND),
        ),
    )

    staged_rows = staged(seeded)
    # Phase A's rows are never stamped before the Direction they bind was written.
    assert staged_rows["manifest"].created_at_us == staged_rows["attempt"].started_at_us == ahead
    assert staged_rows["invocation"].created_at_us == ahead
    applied = direction(seeded)
    assert applied.terminal_at_us == applied.updated_at_us == later
    assert continuation_of(seeded).created_at_us == later
    assert events(seeded, STATE_CHANGED)[0].occurred_at_us == later
    assert applied.terminal_at_us >= staged_rows["manifest"].created_at_us
    assert applied.terminal_at_us >= staged_rows["events"][0].occurred_at_us
    settled_ok(seeded)


# --- 4: crash and restart ----------------------------------------------------------


def test_a_crash_during_phase_a_rolls_back_all_of_it_and_the_restart_stages_one_set(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_manifest = chat.ChatWriter.append_request_manifest

    def manifest_then_death(self: chat.ChatWriter, **fields: Any) -> None:
        real_manifest(self, **fields)
        if fields["generation_attempt_id"] == ids_of().generation_attempt_id:
            raise Crash  # the last write of Phase A, and then the process dies

    with monkeypatch.context() as patch:
        patch.setattr(chat.ChatWriter, "append_request_manifest", manifest_then_death)
        with pytest.raises(Crash):
            run(seeded, Scripted(play(FIRST, before_finish=steer(seeded))))

    # Nothing of Phase A survived, not even the writes that came before the death.
    assert settled_after_crash(seeded)
    rows = staged(seeded)
    assert rows["events"] == [] and rows["invocation"] is None
    assert rows["attempt"] is None and rows["manifest"] is None
    assert [a.attempt_number for a in attempts(seeded)] == [1]
    assert direction(seeded).state == "pending"

    restart = Scripted(play(FIRST), play(SECOND))
    run(seeded, restart)

    assert len(restart.requests) == 2
    assert count(seeded, "omnivia_provider_invocations") == 1
    assert len(attempts(seeded)) == 2
    assert count(seeded, "omnivia_chat_request_manifests") == 2
    assert len(events(seeded, ITERATION)) == len(events(seeded, STATE_CHANGED)) == 1
    assert assistant_text(seeded) == FIRST + SECOND
    settled_ok(seeded)


def settled_after_crash(holder: m1.Owned) -> bool:
    return holder.connection.in_transaction is False


def test_a_crash_after_phase_a_reuses_the_staged_rows_and_writes_no_second_set(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        crash_instead_of_applying(patch)
        first = Scripted(play(FIRST, before_finish=steer(seeded)))
        with pytest.raises(Crash):
            run(seeded, first)

    # Phase A is durable and Phase B never ran: the Generation is still on the first
    # attempt, the Direction pending, and the provider was never asked for the successor.
    assert settled_after_crash(seeded)
    rows_after_a = staged(seeded)
    assert rows_after_a["attempt"] is not None and rows_after_a["manifest"] is not None
    assert len(rows_after_a["events"]) == 1 and rows_after_a["invocation"] is not None
    assert direction(seeded).state == "pending" and direction(seeded).version == 1
    assert count(seeded, "omnivia_chat_generation_continuations") == 0
    assert status(seeded).current_attempt_id == job_row(seeded).current_attempt_id == FIRST_ATTEMPT
    assert len(first.requests) == 1
    version_after_a = status(seeded).version
    snapshot = dump(seeded)

    # The restart, ten minutes on, replays the first attempt's stream; the boundary finds
    # Phase A's rows and does not plan them afresh under the new clock.
    clock = FakeClock(wall=base.WALL)
    clock.advance_wall(600)
    restart = Scripted(play(FIRST), play(SECOND))
    with monkeypatch.context() as patch:
        seen = spy_on_the_seam(patch, seeded)
        run(seeded, restart, clock=clock)

    assert staged(seeded) == rows_after_a  # the very rows, not equal-looking new ones
    staged_options = rows_after_a["manifest"].manifest_body["requestOptions"]
    assert restart.requests[1].requested_at == staged_options["requestedAt"]
    assert restart.requests[1].deadline_at == staged_options["deadlineAt"]
    # Phase B is stamped when it ran, after everything Phase A staged.
    assert direction(seeded).terminal_at_us == NOW_US + 600_000_000
    assert len(attempts(seeded)) == 2
    assert count(seeded, "omnivia_provider_invocations") == 1
    assert count(seeded, "omnivia_chat_request_manifests") == 2
    assert len(events(seeded, ITERATION)) == 1
    assert len(events(seeded, STATE_CHANGED)) == 1
    assert count(seeded, "omnivia_chat_generation_continuations") == 1
    # The replayed Phase A moved no token; Phase B moved it once.
    assert seen["leaving_a"] == seen["entering_a"]
    assert seen["leaving_b"] == seen["entering_b"] + 1
    assert seen["entering_a"] >= version_after_a
    assert len(restart.requests) == 2
    assert restart.requests[1].attempt_id == ids_of().generation_attempt_id
    assert assistant_text(seeded) == FIRST + SECOND
    # Everything written before the crash is where it was.
    for table in ("omnivia_chat_request_manifests", "omnivia_provider_invocations"):
        assert all(row in dump(seeded)[table] for row in snapshot[table])
    assert_event_sequence_is_contiguous(seeded)
    settled_ok(seeded)


def test_a_crash_during_phase_b_rolls_back_only_phase_b_and_the_restart_replays_it(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_record = gen._record_event_on_job

    def die_when_moving_to_the_successor(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("current_attempt_id") is not None:
            raise Crash  # the Direction, continuation and state event are written, not committed
        real_record(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(gen, "_record_event_on_job", die_when_moving_to_the_successor)
        first = Scripted(play(FIRST, before_finish=steer(seeded)))
        with pytest.raises(Crash):
            run(seeded, first)

    # Phase A survived the rollback of Phase B, which is what two commits mean.
    assert settled_after_crash(seeded)
    rows_after_a = staged(seeded)
    assert rows_after_a["attempt"] is not None and rows_after_a["manifest"] is not None
    assert len(rows_after_a["events"]) == 1
    assert direction(seeded).state == "pending" and direction(seeded).version == 1
    assert direction(seeded).applied_attempt_id is None
    assert count(seeded, "omnivia_chat_generation_continuations") == 0
    assert STATE_CHANGED not in event_types(seeded)
    assert status(seeded).current_attempt_id == job_row(seeded).current_attempt_id == FIRST_ATTEMPT
    assert len(first.requests) == 1

    restart = Scripted(play(FIRST), play(SECOND))
    run(seeded, restart)

    assert staged(seeded) == rows_after_a
    assert direction(seeded).state == "applied"
    assert count(seeded, "omnivia_chat_generation_continuations") == 1
    assert len(events(seeded, STATE_CHANGED)) == 1
    assert assistant_text(seeded) == FIRST + SECOND
    assert_event_sequence_is_contiguous(seeded)
    settled_ok(seeded)


def test_a_crash_after_phase_b_runs_the_existing_successor_with_the_same_request(
    seeded: m1.Owned,
) -> None:
    clock = FakeClock(wall=base.WALL)
    first = Scripted(play(FIRST, before_finish=steer(seeded)), dies)
    with pytest.raises(Crash):
        run(seeded, first, clock=clock)

    # Phase B committed; the provider call for the successor was made and never answered.
    assert settled_after_crash(seeded)
    assert direction(seeded).state == "applied"
    assert status(seeded).current_attempt_id == job_row(seeded).current_attempt_id
    assert status(seeded).current_attempt_id == ids_of().generation_attempt_id
    assert len(first.requests) == 2
    rows = dump(seeded)

    # Much later, a restart runs the successor that exists and does not replay the first.
    clock.advance_wall(600)
    restart = Scripted(play(SECOND))
    run(seeded, restart, clock=clock)

    # The request, its invocation identity and its idempotency key are what they were:
    # stable under repetition, which is all that is claimed for an unknown-outcome call.
    assert len(restart.requests) == 1
    assert restart.requests[0].to_wire() == first.requests[1].to_wire()
    assert restart.requests[0].idempotency_key == first.requests[1].idempotency_key
    assert restart.requests[0].invocation_id == ids_of().provider_invocation_id
    for table in ("omnivia_chat_request_manifests", "omnivia_provider_invocations"):
        assert dump(seeded)[table] == rows[table]
    assert len(attempts(seeded)) == 2
    assert len(events(seeded, ITERATION)) == len(events(seeded, STATE_CHANGED)) == 1
    assert count(seeded, "omnivia_chat_generation_continuations") == 1
    assert assistant_text(seeded) == FIRST + SECOND
    assert_event_sequence_is_contiguous(seeded)
    settled_ok(seeded)


def test_a_crash_mid_stream_of_the_successor_resumes_it_without_a_second_successor(
    seeded: m1.Owned,
) -> None:
    def partial(request: Any) -> Iterator[dict[str, Any]]:
        yield wire(request, 0, "stream-start")
        yield wire(request, 1, "text-delta", delta=SECOND, partId="part-1", stepId="step-1")
        raise Crash

    first = Scripted(play(FIRST, before_finish=steer(seeded)), partial)
    with pytest.raises(Crash):
        run(seeded, first)
    assert len(first.requests) == 2
    assert [c.text_content for c in chat.read_generation_text_chunks(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )] == [FIRST, SECOND]

    restart = Scripted(play(SECOND))
    run(seeded, restart)

    assert len(attempts(seeded)) == 2
    assert count(seeded, "omnivia_chat_generation_continuations") == 1
    assert assistant_text(seeded) == FIRST + SECOND  # the replayed delta was not appended twice
    assert_event_sequence_is_contiguous(seeded)
    settled_ok(seeded)


# --- 5: exact binding, canonical bytes, digests -------------------------------------


def test_the_manifest_binds_the_direction_exactly_and_is_stored_as_canonical_bytes(
    seeded: m1.Owned,
) -> None:
    run(seeded, Scripted(play(FIRST, before_finish=steer(seeded)), play(SECOND)))

    identities = ids_of()
    manifest = manifest_of(seeded, identities.generation_attempt_id)
    pending_digest = direction(seeded).content_digest
    body = manifest.manifest_body
    # Exactly three keys, the version the Direction was pending at, the digest it hashes to.
    assert dict(body["direction"]) == {
        "contentDigest": pending_digest,
        "directionId": "direction-1",
        "directionVersion": 1,
    }
    assert direction(seeded).version == 2  # the Direction moved on; the binding did not
    assert pending_digest == digest(
        json.dumps(
            {"editableParts": [{"payload": {"text": STEER}, "type": "text"}]},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )
    assert body["generationAttemptId"] == identities.generation_attempt_id
    assert body["providerInvocationId"] == identities.provider_invocation_id
    assert body["requestManifestId"] == identities.request_manifest_id
    assert body["promptBundle"]["id"] == "core.chat.direction-continuation-context"
    assert body["requestOptions"]["causationId"] == "direction-1"

    # What is stored is the canonical text, byte for byte, and its digest is of those bytes.
    stored = seeded.connection.execute(
        "SELECT request_manifest_body_json, manifest_digest FROM omnivia_chat_request_manifests "
        "WHERE generation_attempt_id = ?",
        (identities.generation_attempt_id,),
    ).fetchone()
    assert stored[0] == json.dumps(
        json.loads(stored[0]), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert stored[1] == manifest_digest_of(stored[0]) == manifest.manifest_digest

    # Identifiers and hashes only: neither the Direction's text nor any output is in it.
    assert STEER not in stored[0] and FIRST not in stored[0]
    assert_text_confined(
        seeded,
        STEER,
        "omnivia_chat_directions",
    )
    assert_text_confined(
        seeded,
        FIRST,
        "omnivia_chat_generation_text_chunks",
        "omnivia_chat_message_parts",
    )
    settled_ok(seeded)


def test_the_prior_output_digest_is_the_hash_of_the_exact_bytes_of_every_chunk(
    seeded: m1.Owned,
) -> None:
    chunks = ("héllo ", "wörld 🌍 ", "日本語 ", "é end")
    provider = Scripted(play(*chunks, before_finish=steer(seeded)), play(SECOND))

    run(seeded, provider)

    joined = "".join(chunks)
    expected = "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()
    assert events(seeded, ITERATION)[0].payload["priorOutputDigest"] == expected
    assert continuation_of(seeded).prior_output_digest == expected
    # No wrapper, separator or length around the bytes: a digest of any other framing differs.
    for framing in (
        json.dumps(list(chunks), ensure_ascii=False),
        json.dumps(joined, ensure_ascii=False),
        "\n".join(chunks),
        json.dumps({"text": joined}, ensure_ascii=False, sort_keys=True),
    ):
        assert digest(framing) != expected
    # The same text in one chunk and in four hashes alike: it is the bytes that count.
    assert gen.generated_text_digest(joined) == expected
    # The successor was shown the very text, in order.
    assert message_texts(provider.requests[1])[-2] == ("assistant", (joined,))
    assert manifest_of(seeded, ids_of().generation_attempt_id).manifest_body["continuation"][
        "hops"
    ][0]["priorOutputDigest"] == expected
    assert assistant_text(seeded) == joined + SECOND
    settled_ok(seeded)


# --- 6: the provider's finish identity -----------------------------------------------


DURABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@pytest.mark.parametrize(
    "event",
    [
        {"providerEventId": "provider-event-1", "attemptId": "pa-1", "ordinal": 4},
        {"providerEventId": "x" * 128, "attemptId": "pa-1", "ordinal": 4},
        {"providerEventId": "a.b:c_d-e".replace("_", "-"), "ordinal": 4},
    ],
)
def test_a_durable_provider_event_id_is_used_as_it_arrived(event: dict[str, Any]) -> None:
    assert gen.provider_finish_id(event) == event["providerEventId"]


@pytest.mark.parametrize(
    "given",
    [
        pytest.param(None, id="absent"),
        pytest.param("z" * 129, id="one-too-long"),
        pytest.param("z" * 5_000, id="far-too-long"),
        pytest.param("has space", id="space"),
        pytest.param("-leading-dash", id="leading-punctuation"),
        pytest.param("slash/inside", id="slash"),
        pytest.param("ünïcode-id", id="non-ascii"),
        pytest.param("new\nline", id="control-character"),
    ],
)
def test_an_absent_or_non_durable_provider_event_id_is_derived_not_refused(
    given: str | None,
) -> None:
    event: dict[str, Any] = {"attemptId": "provider-attempt-7", "ordinal": 4}
    if given is not None:
        event["providerEventId"] = given

    derived = gen.provider_finish_id(event)

    assert DURABLE_ID.match(derived) and len(derived) <= 128
    assert derived != given
    # Stable under replay, whatever the unstorable id said; and it is the transport
    # Attempt and the ordinal that identify the finish.
    assert gen.provider_finish_id(dict(event)) == derived
    assert gen.provider_finish_id({**event, "providerEventId": "q" * 300}) == derived
    assert gen.provider_finish_id({**event, "ordinal": 5}) != derived
    assert gen.provider_finish_id({**event, "attemptId": "provider-attempt-8"}) != derived


@pytest.mark.parametrize(
    "event",
    [
        {"providerEventId": 7, "attemptId": "pa-1", "ordinal": 1},
        {"providerEventId": "", "attemptId": "pa-1", "ordinal": 1},
        {"providerEventId": ["x"], "attemptId": "pa-1", "ordinal": 1},
        {"providerEventId": None, "attemptId": "pa-1", "ordinal": 1},
        {"attemptId": "pa-1", "ordinal": "1"},
        {"attemptId": "pa-1", "ordinal": True},
        {"attemptId": "pa-1", "ordinal": -1},
        {"attemptId": "pa-1"},
        {"attemptId": 3, "ordinal": 1},
        {"attemptId": "", "ordinal": 1},
        {"ordinal": 1},
    ],
)
def test_a_malformed_finish_identity_is_still_refused_without_echoing_it(
    event: dict[str, Any],
) -> None:
    with pytest.raises(gen.UnsupportedProviderEvent) as refused:
        gen.provider_finish_id(event)
    assert "pa-1" not in str(refused.value)


def test_a_finish_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(gen.UnsupportedProviderEvent):
        gen.provider_finish_id(["providerEventId"])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("finish_id", "durable"),
    [
        pytest.param("provider-finish-1", True, id="durable"),
        pytest.param(None, False, id="absent"),
        pytest.param("w" * 200, False, id="overlong"),
        pytest.param("not durable!", False, id="unstorable-characters"),
    ],
)
def test_a_continuation_does_not_depend_on_the_finish_having_a_storable_id(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch, finish_id: str | None, durable: bool
) -> None:
    def finishing(hook: Callable[[], None] | None) -> Callable[[Any], Iterator[dict[str, Any]]]:
        omit = ("providerEventId",) if finish_id is None else ()
        extra = {} if finish_id is None else {"providerEventId": finish_id}
        return lambda request: stream(
            request,
            (FIRST,),
            before_finish=hook,
            finish={"eventType": "finish", "finishReason": "stop", **extra},
            omit_on_finish=omit,
        )

    with monkeypatch.context() as patch:
        crash_instead_of_applying(patch)
        with pytest.raises(Crash):
            run(seeded, Scripted(finishing(steer(seeded))))
    recorded = events(seeded, ITERATION)[0].provider_event_id
    assert recorded is not None and DURABLE_ID.match(recorded)
    assert (recorded == finish_id) is durable

    # A replay of the stream derives the same identity, so the restart finds its rows.
    run(seeded, Scripted(finishing(None), play(SECOND)))

    assert len(events(seeded, ITERATION)) == 1
    assert events(seeded, ITERATION)[0].provider_event_id == recorded
    assert continuation_of(seeded).provider_finish_id == recorded
    assert assistant_text(seeded) == FIRST + SECOND
    settled_ok(seeded)


# --- 7: coherence of the foundation Job, the status projection and the lease ----------


@pytest.fixture
def after_phase_b(seeded: m1.Owned) -> m1.Owned:
    """Phase B committed and the successor's provider call lost: the state a restart finds."""
    with pytest.raises(Crash):
        run(seeded, Scripted(play(FIRST, before_finish=steer(seeded)), dies))
    return seeded


def test_after_phase_b_the_job_the_projection_the_attempt_and_the_lease_agree(
    after_phase_b: m1.Owned,
) -> None:
    holder = after_phase_b
    successor = ids_of().generation_attempt_id
    job, projection = job_row(holder), status(holder)

    assert (job.state, projection.state) == ("running", "running")
    assert job.current_attempt_id == projection.current_attempt_id == successor
    assert job.lease_owner == holder.identity.service_instance_id
    assert job.lease_epoch == 1  # a continuation is not a takeover
    assert job.lease_expires_at_us is not None and job.lease_expires_at_us > NOW_US
    assert job.heartbeat_at_us == NOW_US
    assert job.last_event_sequence == events(holder)[-1].generation_event_sequence
    assert job.finished_at_us is None and projection.finished_at_us is None
    assert job.result_message_id is None and projection.result_message_id is None
    assert (job.sanitized_error_code, projection.sanitized_error_code) == (None, None)
    assert_event_sequence_is_contiguous(holder)
    settled_ok(holder)


def test_the_successor_can_be_heartbeated_and_the_prior_attempt_can_no_longer_be(
    after_phase_b: m1.Owned,
) -> None:
    holder = after_phase_b
    successor = ids_of().generation_attempt_id
    clock = FakeClock(wall=base.WALL)
    before_job, before_status = job_row(holder), status(holder)

    def beat(attempt_id: str) -> gen.GenerationHeartbeat:
        heartbeat = gen.GenerationHeartbeat(
            connection=holder.connection,
            identity=holder.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=holder.generation,
            generation_job_id=JOB_ID,
            generation_attempt_id=attempt_id,
            lease_owner=holder.identity.service_instance_id,
            clock=clock,
            lease_duration_us=150_000_000,  # the executor sizes a lease from its deadline
        )
        heartbeat.start(before_job.lease_expires_at_us)
        return heartbeat

    clock.advance_wall(100)  # past half of the lease, with nothing arriving
    renewed = beat(successor).tick()

    after_job, after_status = job_row(holder), status(holder)
    assert renewed == after_job.lease_expires_at_us
    assert after_job.lease_expires_at_us > before_job.lease_expires_at_us
    assert after_job.heartbeat_at_us > before_job.heartbeat_at_us
    assert after_status.version == before_status.version + 1
    assert after_job.current_attempt_id == after_status.current_attempt_id == successor
    assert (after_job.lease_epoch, after_job.last_event_sequence) == (
        before_job.lease_epoch,
        before_job.last_event_sequence,
    )

    # The first attempt no longer owns the Generation, and a heartbeat for it says so
    # without writing.
    snapshot = dump(holder)
    clock.advance_wall(1_000)
    with pytest.raises(gen.GenerationConflict, match="not running this attempt"):
        beat(FIRST_ATTEMPT).tick()
    assert dump(holder) == snapshot
    settled_ok(holder)


def test_the_successors_stream_renews_its_lease_through_the_executors_heartbeat(
    seeded: m1.Owned,
) -> None:
    clock = FakeClock(wall=base.WALL)
    seen: dict[str, Any] = {}

    def second(request: Any) -> Iterator[dict[str, Any]]:
        yield wire(request, 0, "stream-start")
        yield wire(request, 1, "text-delta", delta=SECOND, partId="part-1", stepId="step-1")
        seen["before"] = (job_row(seeded), status(seeded))
        clock.advance_wall(120)  # past half the lease, with nothing arriving
        yield wire(request, 2, "text-delta", delta=THIRD, partId="part-1", stepId="step-1")
        seen["after"] = (job_row(seeded), status(seeded))
        yield wire(request, 3, "finish", finishReason="stop")

    run(seeded, Scripted(play(FIRST, before_finish=steer(seeded)), second), clock=clock)

    (job_before, status_before), (job_after, status_after) = seen["before"], seen["after"]
    successor = ids_of().generation_attempt_id
    assert job_before.current_attempt_id == job_after.current_attempt_id == successor
    assert status_after.current_attempt_id == successor
    # The delta's own write is one step; the heartbeat's renewal before it is another.
    assert status_after.version == status_before.version + 2
    assert job_after.heartbeat_at_us == NOW_US + 120_000_000
    assert job_after.lease_expires_at_us == NOW_US + 120_000_000 + gen.DEFAULT_LEASE_US
    assert job_after.lease_owner == seeded.identity.service_instance_id
    assert assistant_text(seeded) == FIRST + SECOND + THIRD
    settled_ok(seeded)


# --- 8: nothing of the first iteration is settled while a Direction is pending ---------


def test_nothing_of_a_first_iteration_is_settled_while_a_direction_is_pending(
    seeded: m1.Owned,
) -> None:
    seen: dict[str, Any] = {}

    def second(request: Any) -> Iterator[dict[str, Any]]:
        seen["settled"] = {
            "assistant_messages": len(assistant_messages(seeded)),
            "outcomes": count(seeded, "omnivia_chat_generation_attempt_outcomes"),
            "outbox": count(seeded, "omnivia_chat_transactional_outbox"),
            "succeeded": len(events(seeded, SUCCEEDED)),
            "job_state": (job_row(seeded).state, status(seeded).state),
            "result": (job_row(seeded).result_message_id, status(seeded).result_message_id),
            "head": chat.read_branch(
                seeded.connection, workspace_id=WORKSPACE_ID, branch_id=base.BRANCH_ID
            ),
        }
        return stream(request, (SECOND,))

    head_before = chat.read_branch(
        seeded.connection, workspace_id=WORKSPACE_ID, branch_id=base.BRANCH_ID
    )
    run(seeded, Scripted(play(FIRST, before_finish=steer(seeded)), second))

    settled = seen["settled"]
    assert settled["assistant_messages"] == settled["outcomes"] == settled["succeeded"] == 0
    assert settled["outbox"] == 0
    assert settled["job_state"] == ("running", "running") and settled["result"] == (None, None)
    assert settled["head"] == head_before

    # Only the last attempt's finish settles, once: one Message, one success, one outcome.
    assert len(assistant_messages(seeded)) == 1
    assert len(events(seeded, SUCCEEDED)) == 1
    outcomes = chat.read_generation_attempt_outcomes(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert [o.generation_attempt_id for o in outcomes] == [ids_of().generation_attempt_id]
    assert count(seeded, "omnivia_chat_transactional_outbox") == 1
    assert job_row(seeded).result_message_id == status(seeded).result_message_id
    settled_ok(seeded)


def test_the_text_of_a_direction_and_of_an_output_stays_where_it_belongs(
    seeded: m1.Owned,
) -> None:
    provider = Scripted(play(FIRST, before_finish=steer(seeded)), play(SECOND))

    run(seeded, provider)

    # The authored text reached the provider request, and the Direction's row...
    assert ("user", (STEER,)) in message_texts(provider.requests[1])
    assert_text_confined(seeded, STEER, "omnivia_chat_directions")
    # ...while the outputs are in their chunks and the final Message, nowhere else.
    for text in (FIRST, SECOND):
        assert_text_confined(
            seeded, text, "omnivia_chat_generation_text_chunks", "omnivia_chat_message_parts"
        )
    # Every event the seam wrote is identity-only.
    for event in (*events(seeded, ITERATION), *events(seeded, STATE_CHANGED)):
        assert STEER not in json.dumps(dict(event.payload))
        assert set(event.payload) in (
            {"generationJobVersion", "iteration", "priorOutputDigest"},
            {"directionId", "directionVersion", "state"},
        )
    settled_ok(seeded)


# --- 9: per-attempt provider idempotency ---------------------------------------------


def test_the_successors_idempotency_key_is_its_own_and_stable_across_a_restart(
    seeded: m1.Owned,
) -> None:
    first = Scripted(play(FIRST, before_finish=steer(seeded)), dies)
    with pytest.raises(Crash):
        run(seeded, first)
    restart = Scripted(play(SECOND))
    run(seeded, restart)

    prior_key, successor_key = first.requests[0].idempotency_key, first.requests[1].idempotency_key
    assert prior_key == JOB_KEY
    assert successor_key != prior_key and successor_key != JOB_KEY
    assert restart.requests[0].idempotency_key == successor_key
    assert 1 <= len(successor_key) <= 128
    assert manifest_of(seeded, ids_of().generation_attempt_id).idempotency_key == successor_key
    # And it is the successor's: another Direction's successor gets another key.
    assert successor_key != gen.continuation_identities(
        generation_job_id=JOB_ID, direction_id="direction-2", iteration=1
    ).generation_attempt_id


@pytest.mark.parametrize("reused", ["job", "prior"])
def test_a_manifest_that_reuses_the_prior_idempotency_key_is_refused(
    at_boundary: Boundary, reused: str
) -> None:
    holder = at_boundary.holder
    prior_key = manifest_of(holder, FIRST_ATTEMPT).idempotency_key
    body = at_boundary.manifest_body
    body["idempotencyKey"] = JOB_KEY if reused == "job" else prior_key
    before = dump(holder)

    with pytest.raises(gen.GenerationConflict, match="idempotency key of its own"):
        at_boundary.stage(manifest_body=body)

    assert dump(holder) == before


def test_the_request_names_the_chat_attempt_and_the_event_the_transport_attempt() -> None:
    # The two identities are different kinds of thing: a request's `attemptId` is the
    # Chat Generation Attempt, an event's is a provider transport Attempt. The finish
    # identity is derived from the second and never confuses it for the first.
    chat_attempt = ids_of().generation_attempt_id
    finish = {"eventType": "finish", "attemptId": "provider-transport-attempt-3", "ordinal": 6}
    assert gen.provider_finish_id(finish) != gen.provider_finish_id(
        {**finish, "attemptId": chat_attempt}
    )
    assert gen.provider_finish_id(finish).startswith("finish-")


def under_transport(
    transport: str, *deltas: str, **options: Any
) -> Callable[[Any], Iterator[dict[str, Any]]]:
    """A provider streaming under a transport Attempt it names itself, its finish without an id."""

    def script(request: Any) -> Iterator[dict[str, Any]]:
        for event in stream(request, deltas, omit_on_finish=("providerEventId",), **options):
            yield {**event, "attemptId": transport}

    return script


def test_each_stream_names_its_own_transport_attempt_and_the_finish_identity_follows_it(
    seeded: m1.Owned,
) -> None:
    provider = Scripted(
        under_transport("provider-transport-alpha", FIRST, before_finish=steer(seeded)),
        under_transport("provider-transport-bravo", SECOND),
    )

    run(seeded, provider)

    # The requests name the Chat Attempts; each stream named a transport Attempt of its own.
    successor = ids_of().generation_attempt_id
    assert [request.attempt_id for request in provider.requests] == [FIRST_ATTEMPT, successor]
    assert [a.generation_attempt_id for a in attempts(seeded)] == [FIRST_ATTEMPT, successor]
    # With no event id on the finish, the iteration is identified by the transport Attempt
    # and the ordinal it closed on, never by the Chat Attempt.
    by_transport = gen.provider_finish_id({"attemptId": "provider-transport-alpha", "ordinal": 2})
    assert by_transport != gen.provider_finish_id({"attemptId": FIRST_ATTEMPT, "ordinal": 2})
    assert events(seeded, ITERATION)[0].provider_event_id == by_transport
    assert continuation_of(seeded).provider_finish_id == by_transport
    # A transport Attempt is never recorded as, or in place of, a Chat Attempt.
    assert_text_confined(seeded, "provider-transport-")
    assert status(seeded).state == "succeeded"
    assert assistant_text(seeded) == FIRST + SECOND
    assert_event_sequence_is_contiguous(seeded)
    settled_ok(seeded)


def from_ordinal(first: int, **fields: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Each event from ordinal `first` on, with `fields` rewritten."""
    return lambda event: {**event, **fields} if event["ordinal"] >= first else event


def without_transport(event: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key != "attemptId"}


@pytest.mark.parametrize(
    ("change", "durable_deltas"),
    [
        # Start 0, deltas 1 and 2, finish 3.
        pytest.param(from_ordinal(2, attemptId="provider-transport-other"), 1, id="changes-mid-stream"),
        pytest.param(from_ordinal(3, attemptId="provider-transport-other"), 2, id="changes-at-finish"),
        pytest.param(from_ordinal(0, attemptId=FIRST_ATTEMPT), 0, id="is-the-chat-attempt"),
        pytest.param(from_ordinal(0, attemptId=""), 0, id="empty"),
        pytest.param(from_ordinal(0, attemptId="has space"), 0, id="not-durable"),
        pytest.param(from_ordinal(0, attemptId="x" * 129), 0, id="overlong"),
        pytest.param(from_ordinal(0, attemptId=7), 0, id="not-a-string"),
        pytest.param(without_transport, 0, id="absent"),
        # The checks a transport Attempt sits beside still hold.
        pytest.param(from_ordinal(3, invocationId="provider-inv-foreign"), 2, id="other-invocation"),
        pytest.param(from_ordinal(3, ordinal=4), 2, id="skipped-ordinal"),
    ],
)
def test_a_stream_that_is_not_one_transport_attempt_of_its_invocation_fails_and_takes_nothing_in(
    seeded: m1.Owned, change: Callable[[dict[str, Any]], dict[str, Any]], durable_deltas: int
) -> None:
    def first(request: Any) -> Iterator[dict[str, Any]]:
        submit_direction(seeded)  # pending for the whole of the stream
        return (change(event) for event in stream(request, (FIRST, SECOND)))

    provider = Scripted(first)

    run(seeded, provider)

    # Refused at the first event that breaks the stream: what came before it is durable,
    # nothing after it is, and the finish never became a boundary a Direction is taken in at.
    assert len(provider.requests) == 1
    assert status(seeded).state == job_row(seeded).state == "failed"
    assert status(seeded).sanitized_error_code == "malformed-response"
    assert len(events(seeded, "chat.generation.text_appended")) == durable_deltas
    assert events(seeded, ITERATION) == [] and staged(seeded)["attempt"] is None
    assert count(seeded, "omnivia_chat_generation_continuations") == 0
    assert len(assistant_messages(seeded)) == 0
    assert (direction(seeded).state, reason_of(direction(seeded))["code"]) == (
        "cancelled",
        "job_terminal",
    )
    assert_event_sequence_is_contiguous(seeded, job_row_follows=False)
    settled_ok(seeded)


# --- 10: racing workers, stale tokens, leases, ended jobs ------------------------------


def test_two_workers_racing_at_the_finish_write_one_successor_and_one_application(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    rival = Scripted(play(FIRST), play(SECOND))
    first = Scripted(play(FIRST, before_finish=steer(seeded)), play(SECOND))
    real_apply = executor_module.apply_direction_continuation
    raced: list[bool] = []

    def apply_after_the_rival(*args: Any, **kwargs: Any) -> Any:
        if not raced:
            raced.append(True)
            # A second worker takes the whole boundary and the successor to the end, in
            # the gap between this worker's two commits.
            make_executor(seeded, rival).execute_generation_attempt(
                generation_job_id=JOB_ID, generation_attempt_id=FIRST_ATTEMPT
            )
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(executor_module, "apply_direction_continuation", apply_after_the_rival)

    # The late worker finds the Direction applied and the Generation over: it is told so,
    # and writes nothing of its own.
    with pytest.raises(gen.GenerationTerminal):
        run(seeded, first)

    assert len(rival.requests) == 2
    assert len(attempts(seeded)) == 2
    assert count(seeded, "omnivia_provider_invocations") == 1
    assert count(seeded, "omnivia_chat_request_manifests") == 2
    assert count(seeded, "omnivia_chat_generation_continuations") == 1
    assert len(events(seeded, ITERATION)) == len(events(seeded, STATE_CHANGED)) == 1
    assert direction(seeded).state == "applied" and direction(seeded).version == 2
    assert len(events(seeded, SUCCEEDED)) == 1
    assert assistant_text(seeded) == FIRST + SECOND
    assert_event_sequence_is_contiguous(seeded, job_row_follows=False)
    settled_ok(seeded)


def test_two_workers_calling_each_phase_twice_write_each_row_once(at_boundary: Boundary) -> None:
    holder = at_boundary.holder
    results = [at_boundary.stage(), at_boundary.stage()]
    applied = [at_boundary.apply(), at_boundary.apply()]

    assert [r.replayed for r in results] == [False, True]
    assert [a.replayed for a in applied] == [False, True]
    assert len(attempts(holder)) == 2
    assert count(holder, "omnivia_provider_invocations") == 1
    assert count(holder, "omnivia_chat_generation_continuations") == 1
    assert len(events(holder, ITERATION)) == len(events(holder, STATE_CHANGED)) == 1
    assert_event_sequence_is_contiguous(holder)
    settled_ok(holder)


def test_a_stale_token_refuses_phase_b_whole(at_boundary: Boundary) -> None:
    holder = at_boundary.holder
    at_boundary.stage()
    current = status(holder).version
    before = dump(holder)

    for token in (current - 1, current + 1, 0):
        with pytest.raises(gen.StaleGenerationVersion):
            at_boundary.apply(expected_status_version=token)

    assert dump(holder) == before
    assert direction(holder).state == "pending"
    assert at_boundary.apply(expected_status_version=current).replayed is False


def test_a_token_that_moves_between_the_phases_is_re_read_and_still_one_application(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_apply = executor_module.apply_direction_continuation
    calls: list[int] = []

    def renew_then_apply(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["expected_status_version"])
        if len(calls) == 1:
            # The lease is renewed between the executor reading the token and Phase B.
            gen.renew_generation_lease(
                seeded.connection,
                seeded.identity,
                workspace_id=WORKSPACE_ID,
                fencing_generation=seeded.generation,
                generation_job_id=JOB_ID,
                lease_owner=seeded.identity.service_instance_id,
                now_us=NOW_US + 1,
            )
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(executor_module, "apply_direction_continuation", renew_then_apply)
    provider = Scripted(play(FIRST, before_finish=steer(seeded)), play(SECOND))

    run(seeded, provider)

    assert len(calls) == 2 and calls[1] == calls[0] + 1
    assert len(attempts(seeded)) == 2
    assert len(events(seeded, ITERATION)) == len(events(seeded, STATE_CHANGED)) == 1
    assert count(seeded, "omnivia_chat_generation_continuations") == 1
    assert len(provider.requests) == 2
    assert assistant_text(seeded) == FIRST + SECOND
    settled_ok(seeded)


def test_a_lease_another_instance_holds_refuses_both_phases(at_boundary: Boundary) -> None:
    holder = at_boundary.holder
    before = dump(holder)
    with pytest.raises(gen.GenerationConflict, match="leased by another instance"):
        at_boundary.stage(lease_owner="some-other-instance")
    assert dump(holder) == before

    at_boundary.stage()
    before = dump(holder)
    with pytest.raises(gen.GenerationConflict, match="leased by another instance"):
        at_boundary.apply(lease_owner="some-other-instance")
    assert dump(holder) == before


def test_a_lease_recovered_by_another_instance_between_the_phases_refuses_the_old_owner(
    at_boundary: Boundary,
) -> None:
    holder = at_boundary.holder
    at_boundary.stage()
    expired = job_row(holder).lease_expires_at_us
    assert expired is not None
    gen.recover_generation_attempt_lease(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        generation_job_id=JOB_ID,
        generation_attempt_id=FIRST_ATTEMPT,
        recovery_owner="instance-that-recovered",
        now_us=expired + 1,
    )
    before = dump(holder)

    with pytest.raises(gen.GenerationConflict, match="leased by another instance"):
        at_boundary.apply()  # fresh token, old owner

    assert dump(holder) == before
    assert direction(holder).state == "pending"
    assert job_row(holder).current_attempt_id == FIRST_ATTEMPT


@pytest.mark.parametrize("ender", [stop_generation, fail_generation], ids=["stopped", "failed"])
def test_a_job_that_ended_refuses_both_phases_and_writes_nothing(
    at_boundary: Boundary, ender: Callable[[m1.Owned], None]
) -> None:
    holder = at_boundary.holder
    ender(holder)
    before = dump(holder)
    with pytest.raises(gen.GenerationTerminal):
        at_boundary.stage()
    assert dump(holder) == before


@pytest.mark.parametrize("ender", [stop_generation, fail_generation], ids=["stopped", "failed"])
def test_a_job_that_ends_between_the_phases_leaves_the_direction_unapplied(
    at_boundary: Boundary, ender: Callable[[m1.Owned], None]
) -> None:
    holder = at_boundary.holder
    at_boundary.stage()
    ender(holder)
    before = dump(holder)

    with pytest.raises(gen.GenerationTerminal):
        at_boundary.apply()

    assert dump(holder) == before
    assert direction(holder).state == "pending" and direction(holder).version == 1
    assert count(holder, "omnivia_chat_generation_continuations") == 0
    assert len(assistant_messages(holder)) == 0


def test_a_stop_before_the_finish_closes_the_direction_and_writes_no_message(
    seeded: m1.Owned,
) -> None:
    def stopped() -> None:
        submit_direction(seeded)
        stop_generation(seeded)

    provider = Scripted(play(FIRST, before_finish=stopped))

    with pytest.raises(gen.GenerationTerminal):
        run(seeded, provider)

    assert len(provider.requests) == 1
    assert len(assistant_messages(seeded)) == 0
    assert status(seeded).state == job_row(seeded).state == "cancelled"
    assert event_types(seeded)[-2:] == ["chat.generation.cancelled", STATE_CHANGED]
    ended = direction(seeded)
    assert (ended.state, ended.version) == ("cancelled", 2)
    assert ended.terminal_reason is not None
    assert ended.terminal_reason["code"] == "generation_stopped"
    assert set(ended.terminal_reason) == {"code", "occurredAt", "safeMessage"}
    assert STEER not in json.dumps(dict(ended.terminal_reason))
    assert count(seeded, "omnivia_chat_generation_continuations") == 0
    assert staged(seeded)["attempt"] is None and events(seeded, ITERATION) == []
    assert_event_sequence_is_contiguous(seeded, job_row_follows=False)
    settled_ok(seeded)


def test_a_stop_between_the_phases_never_applies_the_direction_or_invokes_the_successor(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_apply = executor_module.apply_direction_continuation

    def stop_then_apply(*args: Any, **kwargs: Any) -> Any:
        stop_generation(seeded)
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(executor_module, "apply_direction_continuation", stop_then_apply)
    provider = Scripted(play(FIRST, before_finish=steer(seeded)))

    with pytest.raises(gen.GenerationTerminal):
        run(seeded, provider)

    assert len(provider.requests) == 1  # the successor was never invoked
    assert len(assistant_messages(seeded)) == 0
    assert (direction(seeded).state, direction(seeded).terminal_reason["code"]) == (  # type: ignore[index]
        "cancelled",
        "generation_stopped",
    )
    assert count(seeded, "omnivia_chat_generation_continuations") == 0
    assert staged(seeded)["attempt"] is not None  # staged, and never made the Generation's
    assert status(seeded).current_attempt_id == FIRST_ATTEMPT
    settled_ok(seeded)


# --- 11: a Direction that cannot be taken in --------------------------------------------


def reason_of(direction_row: chat.Direction) -> Mapping[str, Any]:
    assert direction_row.terminal_reason is not None
    return direction_row.terminal_reason


def test_with_no_attempt_left_the_direction_is_rejected_and_the_generation_settles(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gen, "MAX_GENERATION_ATTEMPTS", 1)
    provider = Scripted(play(FIRST, before_finish=steer(seeded)))

    run(seeded, provider)

    assert len(provider.requests) == 1
    rejected = direction(seeded)
    assert (rejected.state, reason_of(rejected)["code"]) == ("rejected", "boundary_unavailable")
    assert set(reason_of(rejected)) == {"code", "occurredAt", "safeMessage"}
    assert STEER not in json.dumps(dict(reason_of(rejected)))
    # Refused before it wrote anything: no iteration event, no staged row of any kind.
    assert events(seeded, ITERATION) == []
    assert staged(seeded) == {"events": [], "invocation": None, "attempt": None, "manifest": None}
    assert len(attempts(seeded)) == 1
    assert status(seeded).state == "succeeded" and assistant_text(seeded) == FIRST
    assert event_types(seeded)[-2:] == [STATE_CHANGED, SUCCEEDED]
    assert_event_sequence_is_contiguous(seeded)
    settled_ok(seeded)


def test_with_no_event_position_left_the_direction_is_rejected_before_anything_is_staged(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    def at_the_end_of_the_sequence() -> None:
        submit_direction(seeded)
        # The seam needs two positions after the last event; leave it one.
        monkeypatch.setattr(gen, "_MAX_EVENT_SEQUENCE", len(events(seeded)) + 1)

    provider = Scripted(play(FIRST, before_finish=at_the_end_of_the_sequence))

    run(seeded, provider)

    assert len(provider.requests) == 1
    assert (direction(seeded).state, reason_of(direction(seeded))["code"]) == (
        "rejected",
        "boundary_unavailable",
    )
    assert staged(seeded)["attempt"] is None and events(seeded, ITERATION) == []
    assert status(seeded).state == "succeeded" and assistant_text(seeded) == FIRST
    settled_ok(seeded)


def test_stage_refuses_a_boundary_with_no_attempt_or_event_position_left(
    at_boundary: Boundary, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = at_boundary.holder
    before = dump(holder)

    with monkeypatch.context() as patch:
        patch.setattr(gen, "MAX_GENERATION_ATTEMPTS", 1)
        with pytest.raises(gen.BoundaryUnavailable, match="no successor to open"):
            at_boundary.stage()
    with monkeypatch.context() as patch:
        patch.setattr(gen, "_MAX_EVENT_SEQUENCE", len(events(holder)) + 1)
        with pytest.raises(gen.BoundaryUnavailable, match="no event position left"):
            at_boundary.stage()

    assert dump(holder) == before
    assert at_boundary.stage().replayed is False  # and the bounds were all that stopped it


def test_a_direction_replaced_after_staging_cannot_use_the_orphaned_successor(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        crash_instead_of_applying(patch)
        with pytest.raises(Crash):
            run(seeded, Scripted(play(FIRST, before_finish=steer(seeded))))
    orphan = staged(seeded)
    assert orphan["attempt"] is not None

    # The actor replaces the Direction after Phase A and before Phase B ever ran.
    submit_direction(seeded, sequence=2, text=STEER_AGAIN, supersedes="direction-1")

    restart = Scripted(play(FIRST))
    run(seeded, restart)

    # Only one Attempt can follow the first, and the staged one is bound to the old
    # Direction, so the new one is rejected and the Generation settles on its output.
    assert len(restart.requests) == 1
    assert direction(seeded, 1).state == "superseded"
    assert (direction(seeded, 2).state, reason_of(direction(seeded, 2))["code"]) == (
        "rejected",
        "boundary_unavailable",
    )
    assert count(seeded, "omnivia_chat_generation_continuations") == 0
    assert staged(seeded) == orphan  # inert: nothing was added to it and nothing uses it
    assert status(seeded).state == "succeeded"
    assert status(seeded).current_attempt_id == FIRST_ATTEMPT
    assert assistant_text(seeded) == FIRST
    # The iteration was closed on the finish's id, so the settling event does not reuse it.
    assert len(events(seeded, ITERATION)) == 1
    assert len(events(seeded, SUCCEEDED)) == 1
    assert_event_sequence_is_contiguous(seeded)
    settled_ok(seeded)


@pytest.mark.parametrize(
    "content",
    [
        {"editableParts": []},
        {"editableParts": [{"payload": {"text": ""}, "type": "text"}]},
        {"editableParts": [{"payload": {"text": "x", "note": "y"}, "type": "text"}]},
        {"editableParts": [{"payload": {"text": "x"}, "type": "image"}]},
        {"editableParts": [{"payload": {"text": "x"}, "type": "text"}], "extra": 1},
    ],
)
def test_a_direction_whose_content_is_not_the_contracts_is_rejected_and_never_staged(
    seeded: m1.Owned, content: dict[str, Any]
) -> None:
    provider = Scripted(play(FIRST, before_finish=steer(seeded, content=content)))

    run(seeded, provider)

    assert len(provider.requests) == 1
    assert (direction(seeded).state, reason_of(direction(seeded))["code"]) == (
        "rejected",
        "policy_refused",
    )
    assert staged(seeded)["attempt"] is None and events(seeded, ITERATION) == []
    assert assistant_text(seeded) == FIRST
    settled_ok(seeded)


def test_a_direction_for_another_actor_is_rejected_and_never_staged(seeded: m1.Owned) -> None:
    provider = Scripted(play(FIRST, before_finish=steer(seeded, actor_id=OTHER_ACTOR_ID)))

    run(seeded, provider)

    assert len(provider.requests) == 1
    assert (direction(seeded).state, reason_of(direction(seeded))["code"]) == (
        "rejected",
        "policy_refused",
    )
    assert staged(seeded)["attempt"] is None
    assert assistant_text(seeded) == FIRST
    settled_ok(seeded)


# --- 12: refusals that write nothing -----------------------------------------------------


def mutate(path: tuple[str, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def apply(body: dict[str, Any]) -> None:
        target = body
        for key in path[:-1]:
            target = target[key]
        if value is _DELETE:
            del target[path[-1]]
        else:
            target[path[-1]] = value

    return apply


_DELETE = object()
MISBOUND = {
    "direction version": mutate(("direction", "directionVersion"), 2),
    "direction id": mutate(("direction", "directionId"), "direction-9"),
    "direction digest": mutate(("direction", "contentDigest"), "sha256:" + "0" * 64),
    "extra binding key": mutate(("direction", "note"), "x"),
    "no binding": mutate(("direction",), _DELETE),
    "other attempt": mutate(("generationAttemptId",), FIRST_ATTEMPT),
    "other invocation": mutate(("providerInvocationId",), "provider-inv-other"),
    "other manifest id": mutate(("requestManifestId",), "request-manifest-other"),
    "other workspace": mutate(("workspaceId",), "ws-other"),
    "other branch": mutate(("branchId",), "branch-other"),
    "non-text idempotency key": mutate(("idempotencyKey",), 5),
    "other operation": mutate(("route", "operation"), "language.generate"),
    "no route": mutate(("route",), _DELETE),
}


@pytest.mark.parametrize("name", sorted(MISBOUND))
def test_a_manifest_that_does_not_bind_this_continuation_is_refused_by_both_phases(
    at_boundary: Boundary, name: str
) -> None:
    holder = at_boundary.holder
    body = at_boundary.manifest_body
    MISBOUND[name](body)
    before = dump(holder)

    with pytest.raises(gen.GenerationConflict):
        at_boundary.stage(manifest_body=body)
    assert dump(holder) == before

    at_boundary.stage()
    before = dump(holder)
    with pytest.raises(gen.GenerationConflict):
        at_boundary.apply(manifest_body=body)
    assert dump(holder) == before


def test_arguments_that_do_not_describe_this_boundary_are_refused(at_boundary: Boundary) -> None:
    holder = at_boundary.holder
    before = dump(holder)
    other = ids_of(iteration=2)
    cases: list[tuple[str, dict[str, Any]]] = [
        ("another actor", {"actor_id": OTHER_ACTOR_ID}),
        ("another iteration's identities", {"identities": other}),
        ("an unknown direction", {"direction_id": "direction-404"}),
        ("another attempt", {"prior_attempt_id": ids_of().generation_attempt_id}),
        ("a digest of other output", {"prior_output_digest": digest("not what it wrote")}),
        ("a digest that is not one", {"prior_output_digest": "sha256:ABC"}),
    ]
    for name, overrides in cases:
        with pytest.raises(gen.GenerationLifecycleError):
            at_boundary.stage(**overrides)
        assert dump(holder) == before, name
    with pytest.raises(gen.GenerationNotFound):
        at_boundary.stage(generation_job_id="generation-job-nowhere")
    assert dump(holder) == before


def test_a_staged_row_that_differs_from_the_replay_is_a_conflict_and_is_never_repaired(
    at_boundary: Boundary,
) -> None:
    holder = at_boundary.holder
    at_boundary.stage()
    before = dump(holder)

    # The same successor identities, another route: the staged invocation is not this one's.
    body = at_boundary.manifest_body
    body["route"]["modelId"] = "provider-model-other"
    with pytest.raises(gen.GenerationConflict, match="not the ones this replay would write"):
        at_boundary.stage(manifest_body=body)
    assert dump(holder) == before

    # Output that no longer hashes to the digest the iteration recorded is a different
    # fact, not a replay: neither phase goes on.
    rewritten = "something else entirely"
    tamper(
        holder,
        "DROP TRIGGER omnivia_guard_chat_generation_text_chunks_update",
        f"UPDATE omnivia_chat_generation_text_chunks SET text_content = '{rewritten}', "
        f"content_hash = '{digest(rewritten)}'",
    )
    with pytest.raises(gen.GenerationConflict, match="digest"):
        at_boundary.stage()
    with pytest.raises(gen.GenerationConflict, match="no single committed iteration event"):
        at_boundary.apply()


# --- 13: tampered rows -----------------------------------------------------------------


def test_a_direction_whose_digest_no_longer_matches_its_content_fails_closed(
    at_boundary: Boundary,
) -> None:
    holder = at_boundary.holder
    at_boundary.stage()
    tamper(
        holder,
        "DROP TRIGGER omnivia_guard_chat_directions_update",
        f"UPDATE omnivia_chat_directions SET content_digest = 'sha256:{'a' * 64}'",
    )
    before = dump(holder)

    for call in (at_boundary.stage, at_boundary.apply):
        with pytest.raises(StorageError, match="digest does not match") as refused:
            call()
        assert STEER not in str(refused.value)
    assert dump(holder) == before


def test_a_direction_rewritten_with_a_matching_digest_no_longer_matches_the_staged_manifest(
    at_boundary: Boundary,
) -> None:
    holder = at_boundary.holder
    at_boundary.stage()
    rewritten = json.dumps(
        {"editableParts": [{"payload": {"text": "a different instruction"}, "type": "text"}]},
        sort_keys=True,
        separators=(",", ":"),
    )
    tamper(
        holder,
        "DROP TRIGGER omnivia_guard_chat_directions_update",
        "UPDATE omnivia_chat_directions SET "
        f"content_json = '{rewritten}', content_digest = '{digest(rewritten)}'",
    )
    before = dump(holder)

    # The manifest built for the original Direction no longer binds this one.
    for call in (at_boundary.stage, at_boundary.apply):
        with pytest.raises(gen.GenerationConflict, match="does not belong to this continuation"):
            call()
    assert dump(holder) == before
    assert direction(holder).state == "pending"


def test_a_staged_manifest_edited_without_its_digest_fails_closed(at_boundary: Boundary) -> None:
    holder = at_boundary.holder
    result = at_boundary.stage()
    raw = holder.connection.execute(
        "SELECT request_manifest_body_json FROM omnivia_chat_request_manifests "
        "WHERE generation_attempt_id = ?",
        (result.attempt.generation_attempt_id,),
    ).fetchone()[0]
    edited = raw.replace("direction-1", "direction-7")
    assert edited != raw
    tamper(
        holder,
        "DROP TRIGGER omnivia_guard_chat_request_manifests_update",
        f"UPDATE omnivia_chat_request_manifests SET request_manifest_body_json = '{edited}' "
        f"WHERE generation_attempt_id = '{result.attempt.generation_attempt_id}'",
    )
    before = dump(holder)

    for call in (at_boundary.stage, at_boundary.apply):
        with pytest.raises(StorageError, match="digest does not match its body"):
            call()
    assert dump(holder) == before


def test_a_staged_manifest_edited_consistently_is_still_not_the_one_a_replay_writes(
    at_boundary: Boundary,
) -> None:
    holder = at_boundary.holder
    result = at_boundary.stage()
    body = json.loads(
        holder.connection.execute(
            "SELECT request_manifest_body_json FROM omnivia_chat_request_manifests "
            "WHERE generation_attempt_id = ?",
            (result.attempt.generation_attempt_id,),
        ).fetchone()[0]
    )
    body["policy"]["policyRef"] = "policy-weakened"
    edited = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    tamper(
        holder,
        "DROP TRIGGER omnivia_guard_chat_request_manifests_update",
        f"UPDATE omnivia_chat_request_manifests SET request_manifest_body_json = '{edited}', "
        f"manifest_digest = '{manifest_digest_of(edited)}' "
        f"WHERE generation_attempt_id = '{result.attempt.generation_attempt_id}'",
    )
    before = dump(holder)

    for call in (at_boundary.stage, at_boundary.apply):
        with pytest.raises(gen.GenerationConflict, match="not the ones this replay would write|not staged"):
            call()
    assert dump(holder) == before


def test_an_iteration_event_whose_digest_was_rewritten_is_not_trusted(at_boundary: Boundary) -> None:
    holder = at_boundary.holder
    at_boundary.stage()
    event = events(holder, ITERATION)[0]
    forged = dict(event.payload)
    forged["priorOutputDigest"] = digest("forged")
    tamper(
        holder,
        "DROP TRIGGER omnivia_guard_chat_generation_events_update",
        "UPDATE omnivia_chat_generation_events SET payload_json = "
        f"'{json.dumps(forged, sort_keys=True, separators=(',', ':'))}' "
        f"WHERE event_id = '{event.event_id}'",
    )
    before = dump(holder)

    with pytest.raises(gen.GenerationConflict, match="no single committed iteration event"):
        at_boundary.apply()
    with pytest.raises(gen.DuplicateProviderEvent):
        at_boundary.stage()
    assert dump(holder) == before


def test_a_restart_over_a_tampered_stage_fails_the_generation_and_never_invokes_the_successor(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        crash_instead_of_applying(patch)
        with pytest.raises(Crash):
            run(seeded, Scripted(play(FIRST, before_finish=steer(seeded))))
    attempt = ids_of().generation_attempt_id
    tamper(
        seeded,
        "DROP TRIGGER omnivia_guard_chat_request_manifests_update",
        "UPDATE omnivia_chat_request_manifests SET "
        "request_manifest_body_json = replace(request_manifest_body_json, 'direction-1', 'direction-7') "
        f"WHERE generation_attempt_id = '{attempt}'",
    )

    restart = Scripted(play(FIRST))
    run(seeded, restart)

    assert len(restart.requests) == 1  # the successor was not invoked
    assert status(seeded).state == job_row(seeded).state == "failed"
    assert status(seeded).sanitized_error_code == "malformed-response"
    assert len(assistant_messages(seeded)) == 0
    assert count(seeded, "omnivia_chat_generation_continuations") == 0
    ended = direction(seeded)
    assert (ended.state, reason_of(ended)["code"]) == ("cancelled", "job_terminal")
    settled_ok(seeded)


# --- 14: storage failures ---------------------------------------------------------------


def storage_failure_after(method: str, monkeypatch: pytest.MonkeyPatch) -> None:
    real = getattr(chat.ChatWriter, method)

    def written_then_failed(self: chat.ChatWriter, **fields: Any) -> Any:
        real(self, **fields)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(chat.ChatWriter, method, written_then_failed)


@pytest.mark.parametrize(
    "method",
    [
        "append_generation_event",
        "append_provider_invocation",
        "append_generation_attempt",
        "append_request_manifest",
    ],
)
def test_a_storage_failure_in_phase_a_leaves_none_of_it(
    at_boundary: Boundary, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    holder = at_boundary.holder
    before = dump(holder)
    version = status(holder).version

    with monkeypatch.context() as patch:
        storage_failure_after(method, patch)
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            at_boundary.stage()

    assert dump(holder) == before
    assert status(holder).version == version
    settled_ok(holder)
    assert at_boundary.stage().replayed is False  # and the same call lands once afterwards
    assert count(holder, "omnivia_provider_invocations") == 1


@pytest.mark.parametrize(
    "method",
    ["append_generation_continuation", "append_generation_event", "update_generation_job"],
)
def test_a_storage_failure_in_phase_b_leaves_none_of_it_and_phase_a_intact(
    at_boundary: Boundary, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    holder = at_boundary.holder
    at_boundary.stage()
    before = dump(holder)
    version = status(holder).version

    with monkeypatch.context() as patch:
        storage_failure_after(method, patch)
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            at_boundary.apply()

    assert dump(holder) == before
    assert status(holder).version == version
    assert direction(holder).state == "pending" and direction(holder).version == 1
    settled_ok(holder)
    assert at_boundary.apply().replayed is False  # the same call lands once afterwards
    assert direction(holder).state == "applied"


def test_a_storage_failure_at_the_seam_fails_the_generation_without_invoking_the_successor(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = Scripted(play(FIRST, before_finish=steer(seeded)), play(SECOND))
    storage_failure_after("append_generation_continuation", monkeypatch)

    run(seeded, provider)

    assert len(provider.requests) == 1
    assert status(seeded).state == job_row(seeded).state == "failed"
    assert status(seeded).sanitized_error_code == "malformed-response"
    assert len(assistant_messages(seeded)) == 0
    assert direction(seeded).state == "cancelled"
    assert reason_of(direction(seeded))["code"] == "job_terminal"
    assert count(seeded, "omnivia_chat_generation_continuations") == 0
    assert status(seeded).current_attempt_id == FIRST_ATTEMPT
    settled_ok(seeded)


def test_the_successors_lease_can_be_recovered_and_the_prior_attempts_cannot(
    after_phase_b: m1.Owned,
) -> None:
    holder = after_phase_b
    successor = ids_of().generation_attempt_id
    expired = job_row(holder).lease_expires_at_us
    assert expired is not None

    def recover(attempt_id: str) -> chat.GenerationJob:
        return gen.recover_generation_attempt_lease(
            holder.connection,
            holder.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=holder.generation,
            generation_job_id=JOB_ID,
            generation_attempt_id=attempt_id,
            recovery_owner="instance-that-recovered",
            now_us=expired + 1,
        )

    # Both authorities moved to the successor, so recovery is of the successor...
    snapshot = dump(holder)
    with pytest.raises(gen.GenerationConflict, match="not running this attempt"):
        recover(FIRST_ATTEMPT)
    assert dump(holder) == snapshot

    recovered = recover(successor)
    assert recovered.lease_owner == "instance-that-recovered" and recovered.lease_epoch == 2
    assert recovered.current_attempt_id == status(holder).current_attempt_id == successor
    settled_ok(holder)


def test_the_continuation_attempt_runs_through_the_retry_entry_point_too(
    after_phase_b: m1.Owned,
) -> None:
    holder = after_phase_b
    successor = ids_of().generation_attempt_id

    # The first attempt no longer owns the Generation: asking for it invokes nothing.
    stale = Scripted()
    make_executor(holder, stale).execute_generation_attempt(
        generation_job_id=JOB_ID, generation_attempt_id=FIRST_ATTEMPT
    )
    assert stale.requests == []

    restart = Scripted(play(SECOND))
    make_executor(holder, restart).execute_generation_attempt(
        generation_job_id=JOB_ID, generation_attempt_id=successor
    )

    assert [request.attempt_id for request in restart.requests] == [successor]
    assert status(holder).state == "succeeded"
    assert assistant_text(holder) == FIRST + SECOND
    assert len(attempts(holder)) == 2
    assert_event_sequence_is_contiguous(holder, job_row_follows=False)
    settled_ok(holder)
