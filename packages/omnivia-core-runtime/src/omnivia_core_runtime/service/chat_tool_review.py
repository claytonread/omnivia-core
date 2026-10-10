"""Chat C8: tool-call lifecycle, Review decisions and bounded human input.

Layers over the 0032 proposal/result records and the 0033 waits; storage is migration
0071. Everything here is Core state. Tool code is called through the in-process
registry the caller passes in, never from a renderer, and assistant-ui sees only the
projection this module's reads make.

**State.** Each governed tool call has an append-only ledger of nine states --
`prepared`, `policy_checking`, `awaiting_review`, `running`, `succeeded`, `failed`,
`cancelled`, `interrupted`, `outcome_unknown` -- and replay is a read of that ledger.
`running` is written *before* the tool is called, so a process that dies mid-call
leaves `running` behind, and recovery turns that into `outcome_unknown`, which has no
exit: the schema refuses to start it again. Nothing in this module treats an
ambiguous execution as safe to retry; a retry is a new proposal with a new
`tool_call_id` that a person or policy chooses to make.

**Commands.** A Review decision (`decide_tool_review`) and a human answer
(`submit_human_input`) are separate commands over separate wait kinds. Each carries a
`command_id`, is recorded once in `omnivia_chat_wait_decisions` and replays to the same
recorded decision when repeated unchanged. Each is refused, with no state change, for an
unauthorised actor, a wrong-kind wait, a duplicate decision, an expired request, a
stale request version or a stale target action or resource revision.

**Atomicity.** Opening a Review wait is its own transaction (`open_chat_wait`); the
`awaiting_review` event follows in a second. A crash between them leaves an open wait
and a `policy_checking` call, and repeating `request_tool_review` converges.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.chat_contract.v1 import ChatContractDecodeError, to_canonical_json
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.service.chat_interactions import (
    ChatWaitConflict,
    ChatWaitInteraction,
    ChatWaitUnauthorized,
    _digest,
    open_chat_wait,
    read_chat_wait,
)
from omnivia_core_runtime.service.chat_tool_lifecycle import (
    ChatToolLifecycleConflict,
    ChatToolNotFound,
    GovernedTool,
    MalformedToolProposal,
    _assert_safe_json,
    _require_step,
    _require_tool_call,
    approve_tool_call,
)
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.connection import StorageError

__all__ = [
    "MAX_ANSWER_BYTES",
    "ChatToolReviewStale",
    "ChatWaitExpired",
    "ToolCallReplay",
    "ToolNotExecuted",
    "allow_tool_call",
    "authorised_responder_basis",
    "begin_policy_check",
    "cancel_tool_call",
    "decide_tool_review",
    "deny_prepared_tool_call",
    "open_human_input_request",
    "recover_tool_calls",
    "replay_tool_call",
    "replay_turn_tool_calls",
    "request_tool_review",
    "run_governed_tool",
    "settle_running_tool_call",
    "start_tool_run",
    "submit_human_input",
]

#: The ceiling on a human answer, whatever the prompt asks for.
MAX_ANSWER_BYTES: Final = 16_384
_DEFAULT_ANSWER_BYTES: Final = 4_096
_MAX_REFS: Final = 64
_MAX_REF_LENGTH: Final = 256
_TERMINAL: Final = frozenset({"succeeded", "failed", "cancelled", "outcome_unknown"})
_DENIAL_REASONS: Final = frozenset({"policy_denied", "review_denied"})
_REVIEW_KINDS: Final = frozenset({"approval", "policy_pause"})


class ChatWaitExpired(ChatWaitConflict):
    """The request passed its expiry. The wait is durably `expired` before this raises."""


class ChatToolReviewStale(ChatWaitConflict):
    """The request version, action or resource revision moved since the actor looked."""


class ToolNotExecuted(Exception):
    """Raised by a tool handler that refused *before* it caused any effect."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ToolCallReplay:
    """Everything durable about one tool call, as a deterministic read."""

    call: chat.ChatToolCall
    state: str
    events: tuple[chat.ChatToolLifecycleEvent, ...]
    wait: ChatWaitInteraction | None
    decision: chat.ChatWaitDecision | None
    result: chat.ChatToolResult | None


def _fenced(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity | None,
    workspace_id: str,
    fencing_generation: int | None,
    writer: chat.ChatWriter | None = None,
) -> AbstractContextManager[chat.ChatWriter]:
    """The fenced writer these commands write through.

    `writer` lets a CALLER THAT ALREADY HOLDS ONE lend it, which is what the
    `chat.command` seam does: its resolver runs inside a transaction it opened,
    and a second `chat_writer` on the same connection would nest a transaction
    inside that one. Lending keeps the Review decision in the same unit of work
    as the rest of the command, which is also what makes it atomic with the
    seam's own idempotency record.

    Called without one -- every in-process caller and all 32 of this module's
    own tests -- it opens and owns a writer exactly as before.

    THE LENT WRITER'S WORKSPACE IS CHECKED, because lending otherwise drops
    it silently. A `ChatWriter` binds its workspace at construction, so a
    lent writer and the `workspace_id` argument are two independent claims
    about where to write -- and `_decide` uses BOTH: its raw `UPDATE` binds
    the argument while `append_chat_wait_decision` binds the writer's. A
    mismatch would update the wait in one workspace and record its decision
    in another, inside one committed transaction, with no error. The fencing
    guarantee itself survives either way (0029's triggers enforce it at write
    time whatever object issues the statement); the workspace binding is what
    lending weakens, so it is asserted here rather than trusted.
    """
    if writer is not None:
        if writer.workspace_id != workspace_id:
            raise ChatWaitConflict(
                "a lent writer is bound to a different workspace than this command"
            )
        return nullcontext(writer)
    # `identity` and `fencing_generation` OPEN a fence, so they are required
    # exactly when this module opens one -- and are unused above, where the
    # caller's fence is already open. Optional rather than mandatory so the
    # `chat.command` resolver can lend its writer without inventing a service
    # identity it does not hold: a ChatCommand closure receives only
    # `(writer, settlement)`, and a fabricated identity passed to be ignored is
    # exactly the kind of value that stops being ignored after a refactor.
    if identity is None or fencing_generation is None:
        raise ChatWaitConflict(
            "a service identity and fencing generation are required without a lent writer"
        )
    return chat.chat_writer(
        connection, identity, workspace_id=workspace_id, fencing_generation=fencing_generation
    )


def _events(
    connection: sqlite3.Connection, workspace_id: str, tool_call_id: str
) -> tuple[chat.ChatToolLifecycleEvent, ...]:
    return chat.read_chat_tool_lifecycle(
        connection, workspace_id=workspace_id, tool_call_id=tool_call_id
    )


def _state(events: Sequence[chat.ChatToolLifecycleEvent]) -> str | None:
    return events[-1].state if events else None


#: The generation event types that CLOSE a job's stream. Appending after one of
#: these would contradict the invariant the steering path checks, so a tool call
#: whose state changes after its generation ended publishes no transport event.
_TERMINAL_GENERATION_EVENTS: Final = frozenset(
    {"chat.generation.succeeded", "chat.generation.failed", "chat.generation.cancelled"}
)

#: C8's transport event type. Admitted by migration 0072, which rebuilt the
#: events table to store it -- without that, every append here raises on the
#: table's own `event_type` CHECK.
_TOOL_CALL_STATE_CHANGED: Final = "chat.tool_call.state_changed"


def _publish_state_changed(
    writer: chat.ChatWriter,
    *,
    tool_call_id: str,
    state: str,
    lifecycle_sequence: int,
    now_us: int,
    wait_id: str | None,
    reason_code: str | None,
) -> None:
    """Publish one `chat.tool_call.state_changed` on the job's event stream.

    A tool call belongs to a generation attempt, so its transport event rides
    the SAME job-scoped stream as `chat.direction.state_changed` rather than a
    second one of its own. That is the convention C6 set and the only stream
    `chat events` serves.

    THE SEQUENCE AND THE COUNTER MOVE TOGETHER, in this transaction. A job's
    `last_event_sequence` is advanced by whoever appends, never by the append
    itself, and the steering path REFUSES when the two disagree
    (`sequence != job.last_event_sequence`). Advancing one without the other
    would make every later steer conflict.

    Silent in three cases, each real rather than convenient: no job row; a
    stream already CLOSED by a terminal event, which is the state a late
    expiry or cancellation runs in and where appending would contradict the
    invariant above; and a call row that is gone.

    Identity only, per the event's own contract. The wait's detail reaches an
    admitted responder through `ToolCallProjection.review`; this reaches
    everyone, so it carries none of it.
    """
    call = chat.read_chat_tool_call(
        writer.connection, workspace_id=writer.workspace_id, tool_call_id=tool_call_id
    )
    if call is None:
        return
    job = chat.read_generation_job(
        writer.connection,
        workspace_id=writer.workspace_id,
        generation_job_id=call.generation_job_id,
    )
    if job is None:
        return
    existing = chat.read_generation_events(
        writer.connection,
        workspace_id=writer.workspace_id,
        generation_job_id=call.generation_job_id,
    )
    if any(event.event_type in _TERMINAL_GENERATION_EVENTS for event in existing):
        return
    sequence = job.last_event_sequence + 1
    payload: dict[str, Any] = {
        "toolCallId": tool_call_id,
        "turnId": call.turn_id,
        "state": state,
        "lifecycleSequence": lifecycle_sequence,
    }
    if wait_id is not None:
        payload["waitId"] = wait_id
    if reason_code is not None:
        payload["reasonCode"] = reason_code
    writer.append_generation_event(
        event_id=f"{call.generation_job_id}.{sequence}",
        conversation_id=call.conversation_id,
        branch_id=job.branch_id,
        generation_job_id=call.generation_job_id,
        event_type=_TOOL_CALL_STATE_CHANGED,
        generation_event_sequence=sequence,
        trigger_message_id=job.trigger_message_id,
        cursor=f"{call.generation_job_id}:{sequence}",
        payload=payload,
        occurred_at_us=now_us,
        schema_version=1,
    )
    # Every column this update does not own is read from the row and written
    # back unchanged -- the writer replaces them all at once, so a default
    # passed for any would silently drop a lease, an attempt or a result.
    writer.update_generation_job(
        generation_job_id=call.generation_job_id,
        expected_state=job.state,
        expected_lease_epoch=job.lease_epoch,
        state=job.state,
        lease_epoch=job.lease_epoch,
        updated_at_us=job.updated_at_us,
        current_attempt_id=job.current_attempt_id,
        result_message_id=job.result_message_id,
        lease_owner=job.lease_owner,
        lease_expires_at_us=job.lease_expires_at_us,
        heartbeat_at_us=job.heartbeat_at_us,
        last_event_sequence=sequence,
        sanitized_error_code=job.sanitized_error_code,
        sanitized_error_detail=job.sanitized_error_detail,
        started_at_us=job.started_at_us,
        finished_at_us=job.finished_at_us,
    )


def _append(
    writer: chat.ChatWriter,
    events: Sequence[chat.ChatToolLifecycleEvent],
    tool_call_id: str,
    state: str,
    now_us: int,
    **fields: Any,
) -> None:
    lifecycle_sequence = len(events) + 1
    writer.append_chat_tool_lifecycle_event(
        tool_call_id=tool_call_id,
        lifecycle_sequence=lifecycle_sequence,
        state=state,
        occurred_at_us=now_us,
        **fields,
    )
    # One choke point for the durable ledger IS the one choke point for the
    # transport event: a state that is recorded is a state that is published, or
    # neither is, in the same transaction.
    _publish_state_changed(
        writer,
        tool_call_id=tool_call_id,
        state=state,
        lifecycle_sequence=lifecycle_sequence,
        now_us=now_us,
        wait_id=fields.get("wait_id"),
        reason_code=fields.get("reason_code"),
    )


def _refs(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if (
        not isinstance(value, list | tuple)
        or len(value) > _MAX_REFS
        or any(not isinstance(item, str) or not 0 < len(item) <= _MAX_REF_LENGTH for item in value)
        or len(set(value)) != len(value)
    ):
        raise MalformedToolProposal(f"{label} must be a short list of distinct identifiers")
    return tuple(value)


def _mirror_terminal(
    writer: chat.ChatWriter,
    call: chat.ChatToolCall,
    step: chat.ChatTurnStep,
    *,
    state: str,
    reason_code: str,
    now_us: int,
) -> None:
    """Keep the 0032 records in step. The ledger is the authority; 0032 cannot say every state."""
    if state == "cancelled" and reason_code in _DENIAL_REASONS:
        legacy, policy = "denied", "denied"
    elif state == "cancelled" and call.post_policy_arguments is not None:
        legacy, policy = "cancelled", call.policy_state
    else:
        # 0032 has no `cancelled` before a post-policy record and no `outcome_unknown`.
        legacy, policy = "failed", call.policy_state
    writer.update_chat_tool_call(
        tool_call_id=call.tool_call_id,
        expected_version=call.version,
        state=legacy,
        policy_state=policy,
        post_policy_arguments=call.post_policy_arguments,
        executed_arguments_digest=call.executed_arguments_digest,
        failure_code=(
            ("outcome_unknown" if state == "outcome_unknown" else reason_code)
            if legacy in {"failed", "denied"}
            else None
        ),
        updated_at_us=now_us,
        finished_at_us=now_us,
    )
    writer.update_chat_turn_step(
        step_id=step.step_id,
        expected_version=step.version,
        state=legacy,
        updated_at_us=now_us,
        finished_at_us=now_us,
    )


def _sync_turn(
    connection: sqlite3.Connection,
    writer: chat.ChatWriter,
    call: chat.ChatToolCall,
    *,
    waiting: bool,
    now_us: int,
) -> None:
    turn = chat.read_chat_turn(connection, workspace_id=call.workspace_id, turn_id=call.turn_id)
    if turn is None:  # pragma: no cover - the call's foreign key guarantees it
        raise ChatToolLifecycleConflict(f"chat turn {call.turn_id!r} is missing")
    if waiting:
        target = "waiting" if turn.state == "running" else None
    else:
        still_waiting = connection.execute(
            "SELECT COUNT(*) FROM omnivia_chat_tool_calls c WHERE c.workspace_id = ? "
            "AND c.turn_id = ? AND c.tool_call_id <> ? AND ("
            "SELECT e.state FROM omnivia_chat_tool_lifecycle_events e "
            "WHERE e.workspace_id = c.workspace_id AND e.tool_call_id = c.tool_call_id "
            "ORDER BY e.lifecycle_sequence DESC LIMIT 1) = 'awaiting_review'",
            (call.workspace_id, call.turn_id, call.tool_call_id),
        ).fetchone()[0]
        target = "running" if turn.state == "waiting" and still_waiting == 0 else None
    if target is not None:
        writer.update_chat_turn(
            turn_id=turn.turn_id,
            expected_version=turn.version,
            state=target,
            current_step_id=turn.current_step_id,
            updated_at_us=now_us,
        )


def _terminal(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    tool_call_id: str,
    state: str,
    reason_code: str,
    now_us: int,
    wait_id: str | None = None,
    close_wait: bool = False,
    evidence_refs: Sequence[str] = (),
    artifact_refs: Sequence[str] = (),
) -> chat.ChatToolCall:
    call = _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)
    step = _require_step(connection, workspace_id=workspace_id, step_id=call.step_id)
    events = _events(connection, workspace_id, tool_call_id)
    leaving_review = _state(events) == "awaiting_review"
    with _fenced(connection, identity, workspace_id, fencing_generation) as writer:
        _append(
            writer,
            events,
            tool_call_id,
            state,
            now_us,
            reason_code=reason_code,
            wait_id=wait_id,
            evidence_refs=evidence_refs,
            artifact_refs=artifact_refs,
        )
        _mirror_terminal(writer, call, step, state=state, reason_code=reason_code, now_us=now_us)
        if close_wait and wait_id is not None:
            _close_wait(writer, workspace_id, wait_id, to_state="failed", now_us=now_us)
        if leaving_review:
            _sync_turn(connection, writer, call, waiting=False, now_us=now_us)
    return _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)


def _close_wait(
    writer: chat.ChatWriter, workspace_id: str, wait_id: str, *, to_state: str, now_us: int
) -> None:
    """Close an open wait without a decision: `expired` by time, `failed` by its target ending."""
    cursor = writer.connection.execute(
        "UPDATE omnivia_chat_wait_interactions SET state = ?, version = version + 1, "
        "updated_at_us = ? WHERE workspace_id = ? AND wait_id = ? AND state = 'asked'",
        (to_state, now_us, workspace_id, wait_id),
    )
    if cursor.rowcount != 1:
        raise ChatWaitConflict("chat wait is no longer open")


def replay_tool_call(
    connection: sqlite3.Connection, *, workspace_id: str, tool_call_id: str
) -> ToolCallReplay:
    """The durable story of one tool call. A proposal with no ledger yet reads `prepared`."""
    call = _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)
    events = _events(connection, workspace_id, tool_call_id)
    wait_id = next((e.wait_id for e in reversed(events) if e.wait_id is not None), None)
    return ToolCallReplay(
        call=call,
        state=_state(events) or "prepared",
        events=events,
        wait=None if wait_id is None else read_chat_wait(
            connection, workspace_id=workspace_id, wait_id=wait_id
        ),
        decision=None if wait_id is None else chat.read_chat_wait_decision_by_wait(
            connection, workspace_id=workspace_id, wait_id=wait_id
        ),
        result=chat.read_chat_tool_result(
            connection, workspace_id=workspace_id, tool_call_id=tool_call_id
        ),
    )


def replay_turn_tool_calls(
    connection: sqlite3.Connection, *, workspace_id: str, turn_id: str
) -> tuple[ToolCallReplay, ...]:
    return tuple(
        replay_tool_call(connection, workspace_id=workspace_id, tool_call_id=call.tool_call_id)
        for call in chat.read_chat_tool_calls(connection, workspace_id=workspace_id, turn_id=turn_id)
    )


def begin_policy_check(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    tool_call_id: str,
    now_us: int,
) -> ToolCallReplay:
    """`prepared` or `interrupted` -> `policy_checking`. Replays when already checking."""
    call = _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)
    events = _events(connection, workspace_id, tool_call_id)
    state = _state(events)
    if state == "policy_checking":
        return replay_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)
    if state not in {None, "prepared", "interrupted"}:
        raise ChatToolLifecycleConflict(f"a {state or 'proposed'} tool call cannot start a policy check")
    with _fenced(connection, identity, workspace_id, fencing_generation) as writer:
        if state is None:
            # A proposal recorded before the ledger existed is `prepared` by definition.
            _append(writer, events, tool_call_id, "prepared", call.created_at_us)
            events = _events(connection, workspace_id, tool_call_id)
        _append(writer, events, tool_call_id, "policy_checking", now_us)
    return replay_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)


def request_tool_review(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    tool_call_id: str,
    post_policy_arguments: Mapping[str, Any],
    wait_id: str,
    requester_actor_id: str,
    authorised_responder_policy: Mapping[str, Any],
    prompt: Mapping[str, Any],
    resume_token: str,
    expires_at_us: int,
    target_resource_ref: str,
    target_resource_revision: int,
    now_us: int,
) -> ChatWaitInteraction:
    """`policy_checking` -> `awaiting_review`, binding the Review to the exact action."""
    _assert_safe_json(post_policy_arguments, label="chat tool post-policy arguments")
    if expires_at_us <= now_us:
        raise ChatWaitConflict("a Review request must expire after it opens")
    call = _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)
    events = _events(connection, workspace_id, tool_call_id)
    action_digest = _digest(dict(post_policy_arguments))
    if _state(events) == "awaiting_review":
        wait = read_chat_wait(connection, workspace_id=workspace_id, wait_id=wait_id)
        if wait is not None and wait.tool_call_id == tool_call_id and (
            wait.target_action_digest == action_digest
        ):
            return wait
        raise ChatToolLifecycleConflict("tool call is already awaiting another Review")
    if _state(events) != "policy_checking":
        raise ChatToolLifecycleConflict("a Review can only be requested during the policy check")
    wait = open_chat_wait(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
        conversation_id=call.conversation_id,
        wait_id=wait_id,
        kind="approval",
        requester_actor_id=requester_actor_id,
        authorised_responder_policy=authorised_responder_policy,
        prompt=prompt,
        resume_token=resume_token,
        now_us=now_us,
        generation_job_id=call.generation_job_id,
        turn_id=call.turn_id,
        tool_call_id=tool_call_id,
        expires_at_us=expires_at_us,
        target_action_digest=action_digest,
        target_resource_ref=target_resource_ref,
        target_resource_revision=target_resource_revision,
    )
    with _fenced(connection, identity, workspace_id, fencing_generation) as writer:
        writer.update_chat_tool_call(
            tool_call_id=tool_call_id,
            expected_version=call.version,
            state="proposed",
            policy_state="pending",
            post_policy_arguments=post_policy_arguments,
            updated_at_us=now_us,
        )
        _append(writer, events, tool_call_id, "awaiting_review", now_us, wait_id=wait_id)
        _sync_turn(connection, writer, call, waiting=True, now_us=now_us)
    return wait


def deny_prepared_tool_call(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    tool_call_id: str,
    now_us: int,
) -> chat.ChatToolCall:
    """Policy refused the action: `policy_checking` -> `cancelled` (`policy_denied`)."""
    events = _events(connection, workspace_id, tool_call_id)
    if _state(events) == "cancelled" and events[-1].reason_code == "policy_denied":
        return _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)
    call = _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)
    if _state(events) != "policy_checking" or call.policy_state != "pending":
        raise ChatToolLifecycleConflict("only a call under policy check can be denied by policy")
    return _terminal(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
        tool_call_id=tool_call_id,
        state="cancelled",
        reason_code="policy_denied",
        now_us=now_us,
    )


def allow_tool_call(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    tool_call_id: str,
    post_policy_arguments: Mapping[str, Any],
    now_us: int,
) -> chat.ChatToolCall:
    """Policy allowed the action without a Review; the call stays `policy_checking` until run."""
    if _state(_events(connection, workspace_id, tool_call_id)) != "policy_checking":
        raise ChatToolLifecycleConflict("only a call under policy check can be allowed by policy")
    return approve_tool_call(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
        tool_call_id=tool_call_id,
        post_policy_arguments=post_policy_arguments,
        now_us=now_us,
    )


def cancel_tool_call(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    tool_call_id: str,
    reason_code: str,
    now_us: int,
) -> chat.ChatToolCall:
    """Cancel a call that has not started. A running call settles through its own outcome."""
    events = _events(connection, workspace_id, tool_call_id)
    state = _state(events)
    if state == "cancelled":
        return _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)
    if state not in {"prepared", "policy_checking", "awaiting_review", "interrupted"}:
        raise ChatToolLifecycleConflict(f"a {state or 'proposed'} tool call cannot be cancelled here")
    return _terminal(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
        tool_call_id=tool_call_id,
        state="cancelled",
        reason_code=reason_code,
        wait_id=events[-1].wait_id,
        close_wait=state == "awaiting_review",
        now_us=now_us,
    )


def start_tool_run(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    tool_call_id: str,
    now_us: int,
    current_resource_revision: int | None = None,
    agent_run_id: str | None = None,
) -> chat.ChatToolCall:
    """Write `running` *before* the tool is called. A call that already ran is refused.

    A call that waited for Review needs an approving decision for this exact action and,
    if the Review named a resource, `current_resource_revision` equal to the revision the
    reviewer saw; otherwise the stale approval cancels the call and raises.
    """
    call = _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)
    events = _events(connection, workspace_id, tool_call_id)
    state = _state(events)
    wait_id: str | None = None
    if state == "awaiting_review":
        wait_id = events[-1].wait_id
        wait = read_chat_wait(connection, workspace_id=workspace_id, wait_id=str(wait_id))
        decision = chat.read_chat_wait_decision_by_wait(
            connection, workspace_id=workspace_id, wait_id=str(wait_id)
        )
        if wait is None or decision is None or decision.outcome != "approved":
            raise ChatToolLifecycleConflict("tool call is awaiting an approving Review decision")
        if (
            wait.target_action_digest != call.post_policy_arguments_digest
            or wait.target_resource_revision != current_resource_revision
        ):
            _terminal(
                connection,
                identity,
                workspace_id=workspace_id,
                fencing_generation=fencing_generation,
                tool_call_id=tool_call_id,
                state="cancelled",
                reason_code="stale_review_target",
                wait_id=wait_id,
                now_us=now_us,
            )
            raise ChatToolReviewStale("the approved action or resource changed since the Review")
    elif state != "policy_checking" or call.policy_state != "approved":
        # `running` and every terminal state land here: never start twice.
        raise ChatToolLifecycleConflict(
            f"a {state or 'proposed'} tool call cannot start; no retry is inferred"
        )
    step = _require_step(connection, workspace_id=workspace_id, step_id=call.step_id)
    with _fenced(connection, identity, workspace_id, fencing_generation) as writer:
        _append(writer, events, tool_call_id, "running", now_us, wait_id=wait_id, agent_run_id=agent_run_id)
        version, step_version = call.version, step.version
        if call.state == "proposed":  # the Review path is approved only now
            writer.update_chat_tool_call(
                tool_call_id=tool_call_id,
                expected_version=version,
                state="approved",
                policy_state="approved",
                post_policy_arguments=call.post_policy_arguments,
                updated_at_us=now_us,
            )
            writer.update_chat_turn_step(
                step_id=step.step_id, expected_version=step_version, state="approved", updated_at_us=now_us
            )
            version, step_version = version + 1, step_version + 1
        writer.update_chat_tool_call(
            tool_call_id=tool_call_id,
            expected_version=version,
            state="executing",
            policy_state="approved",
            post_policy_arguments=call.post_policy_arguments,
            executed_arguments_digest=call.post_policy_arguments_digest,
            updated_at_us=now_us,
        )
        writer.update_chat_turn_step(
            step_id=step.step_id, expected_version=step_version, state="executing", updated_at_us=now_us
        )
        if wait_id is not None:
            _sync_turn(connection, writer, call, waiting=False, now_us=now_us)
    return _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)


def settle_running_tool_call(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    tool_call_id: str,
    state: str,
    reason_code: str,
    now_us: int,
    evidence_refs: Sequence[str] = (),
    artifact_refs: Sequence[str] = (),
) -> chat.ChatToolCall:
    """End a `running` call as `failed`, `cancelled` or `outcome_unknown`."""
    if state not in {"failed", "cancelled", "outcome_unknown"}:
        raise ChatToolLifecycleConflict("a running call settles as failed, cancelled or outcome_unknown")
    events = _events(connection, workspace_id, tool_call_id)
    if _state(events) == state and events[-1].reason_code == reason_code:
        return _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)
    if _state(events) != "running":
        raise ChatToolLifecycleConflict("only a running tool call can be settled")
    return _terminal(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
        tool_call_id=tool_call_id,
        state=state,
        reason_code=reason_code,
        now_us=now_us,
        evidence_refs=_refs(list(evidence_refs), "evidence refs"),
        artifact_refs=_refs(list(artifact_refs), "artifact refs"),
    )


def _record_success(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    tool_call_id: str,
    result_payload: Mapping[str, Any],
    evidence_refs: tuple[str, ...],
    artifact_refs: tuple[str, ...],
    now_us: int,
) -> chat.ChatToolResult:
    call = _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)
    step = _require_step(connection, workspace_id=workspace_id, step_id=call.step_id)
    events = _events(connection, workspace_id, tool_call_id)
    result_id = f"{tool_call_id}.result"
    with _fenced(connection, identity, workspace_id, fencing_generation) as writer:
        writer.append_chat_tool_result(
            conversation_id=call.conversation_id,
            turn_id=call.turn_id,
            step_id=call.step_id,
            generation_job_id=call.generation_job_id,
            generation_attempt_id=call.generation_attempt_id,
            tool_call_id=tool_call_id,
            result_id=result_id,
            status="succeeded",
            result_payload=result_payload,
            created_at_us=now_us,
        )
        _append(
            writer,
            events,
            tool_call_id,
            "succeeded",
            now_us,
            result_id=result_id,
            evidence_refs=evidence_refs,
            artifact_refs=artifact_refs,
        )
        writer.update_chat_tool_call(
            tool_call_id=tool_call_id,
            expected_version=call.version,
            state="succeeded",
            policy_state="approved",
            post_policy_arguments=call.post_policy_arguments,
            executed_arguments_digest=call.executed_arguments_digest,
            result_id=result_id,
            updated_at_us=now_us,
            finished_at_us=now_us,
        )
        writer.update_chat_turn_step(
            step_id=step.step_id,
            expected_version=step.version,
            state="succeeded",
            updated_at_us=now_us,
            finished_at_us=now_us,
        )
    result = chat.read_chat_tool_result(
        connection, workspace_id=workspace_id, tool_call_id=tool_call_id
    )
    if result is None:  # pragma: no cover - committed above
        raise StorageError(f"chat tool result for {tool_call_id!r} did not settle")
    return result


def run_governed_tool(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    tool_call_id: str,
    tools: Mapping[str, GovernedTool],
    now_us: int,
    current_resource_revision: int | None = None,
    agent_run_id: str | None = None,
) -> chat.ChatToolResult:
    """Run an allowed or Review-approved call exactly once; replay returns the stored result.

    A handler that returns settles `succeeded`; its payload may name `evidenceRefs` and
    `artifactRefs`. A handler that raises `ToolNotExecuted` settles `failed`. Any other
    exception, or a result that cannot be stored, settles `outcome_unknown`, because the
    effect may have happened. A process that dies in the handler leaves `running` for
    `recover_tool_calls`.
    """
    existing = chat.read_chat_tool_result(
        connection, workspace_id=workspace_id, tool_call_id=tool_call_id
    )
    if existing is not None:
        return existing
    call = _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=tool_call_id)
    tool = tools.get(call.tool_name)
    if tool is None or tool.version != call.tool_version or tool.registry_ref != call.registry_ref:
        if _state(_events(connection, workspace_id, tool_call_id)) in {
            "prepared",
            "policy_checking",
            "awaiting_review",
            "interrupted",
        }:
            cancel_tool_call(
                connection,
                identity,
                workspace_id=workspace_id,
                fencing_generation=fencing_generation,
                tool_call_id=tool_call_id,
                reason_code="unknown_tool",
                now_us=now_us,
            )
        raise ChatToolNotFound(f"chat tool {call.tool_name!r} is not available")
    started = start_tool_run(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
        tool_call_id=tool_call_id,
        now_us=now_us,
        current_resource_revision=current_resource_revision,
        agent_run_id=agent_run_id,
    )

    def settle(state: str, reason_code: str) -> None:
        settle_running_tool_call(
            connection,
            identity,
            workspace_id=workspace_id,
            fencing_generation=fencing_generation,
            tool_call_id=tool_call_id,
            state=state,
            reason_code=reason_code,
            now_us=now_us,
        )

    try:
        payload = tool.handler(started.post_policy_arguments or {})
    except ToolNotExecuted as refusal:
        settle("failed", refusal.code[:64] or "not_executed")
        raise
    except Exception:
        settle("outcome_unknown", "handler_raised")
        raise
    try:
        _assert_safe_json(payload, label="chat tool result")
        evidence = _refs(payload.get("evidenceRefs"), "evidence refs")
        artifacts = _refs(payload.get("artifactRefs"), "artifact refs")
    except (MalformedToolProposal, ChatContractDecodeError):
        settle("outcome_unknown", "malformed_result")
        raise
    return _record_success(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
        tool_call_id=tool_call_id,
        result_payload=payload,
        evidence_refs=evidence,
        artifact_refs=artifacts,
        now_us=now_us,
    )


def recover_tool_calls(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    now_us: int,
) -> tuple[tuple[str, str], ...]:
    """Settle what a restart left open; returns `(tool_call_id, new_state)` pairs.

    `running` -> `outcome_unknown` (the handler may or may not have had its effect),
    `prepared`/`policy_checking` -> `interrupted` (no handler ran, so the call can be
    checked again), an `awaiting_review` call whose Review has expired -> `cancelled`.
    A pending Review itself survives the restart untouched.
    """
    settled: list[tuple[str, str]] = []
    for head in chat.read_open_chat_tool_lifecycle_heads(connection, workspace_id=workspace_id):
        call_id = head.tool_call_id
        if head.state == "running":
            settle_running_tool_call(
                connection,
                identity,
                workspace_id=workspace_id,
                fencing_generation=fencing_generation,
                tool_call_id=call_id,
                state="outcome_unknown",
                reason_code="process_lost",
                now_us=now_us,
            )
            settled.append((call_id, "outcome_unknown"))
        elif head.state in {"prepared", "policy_checking"}:
            events = _events(connection, workspace_id, call_id)
            with _fenced(connection, identity, workspace_id, fencing_generation) as writer:
                _append(writer, events, call_id, "interrupted", now_us, reason_code="process_restart")
            settled.append((call_id, "interrupted"))
        elif head.state == "awaiting_review":
            wait = read_chat_wait(connection, workspace_id=workspace_id, wait_id=str(head.wait_id))
            if wait is not None and wait.state == "asked" and (
                wait.expires_at_us is not None and now_us >= wait.expires_at_us
            ):
                _expire_review(
                    connection,
                    identity,
                    workspace_id=workspace_id,
                    fencing_generation=fencing_generation,
                    wait=wait,
                    now_us=now_us,
                )
                settled.append((call_id, "cancelled"))
    return tuple(settled)


def _expire_review(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity | None,
    *,
    workspace_id: str,
    fencing_generation: int | None,
    wait: ChatWaitInteraction,
    now_us: int,
    writer: chat.ChatWriter | None = None,
) -> None:
    """Durably expire a wait and, if a tool call is awaiting it, cancel that call.

    Takes a lendable `writer` for the same reason `_decide` does, and this is
    the path that needed it most: `_decide` calls this BEFORE it reaches its
    own `_fenced`, so on the lending path -- the whole point of the lend --
    an expired wait opened a nested `BEGIN IMMEDIATE` inside the caller's
    transaction. SQLite answers that with `cannot start a transaction within
    a transaction`, so an actor approving a Review one second late would have
    got an unhandled OperationalError instead of a clean `ChatWaitExpired`,
    rolling back whatever else the command had written.

    No in-process caller lends today, which is why the 32 tests could not
    have caught it.
    """
    events = (
        _events(connection, workspace_id, wait.tool_call_id) if wait.tool_call_id else ()
    )
    if wait.tool_call_id and _state(events) == "awaiting_review":
        call = _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=wait.tool_call_id)
        step = _require_step(connection, workspace_id=workspace_id, step_id=call.step_id)
        with _fenced(connection, identity, workspace_id, fencing_generation, writer) as fenced:
            _close_wait(fenced, workspace_id, wait.wait_id, to_state="expired", now_us=now_us)
            _append(
                fenced, events, call.tool_call_id, "cancelled", now_us,
                reason_code="review_expired", wait_id=wait.wait_id,
            )
            _mirror_terminal(fenced, call, step, state="cancelled", reason_code="review_expired", now_us=now_us)
            _sync_turn(connection, fenced, call, waiting=False, now_us=now_us)
        return
    with _fenced(connection, identity, workspace_id, fencing_generation, writer) as fenced:
        _close_wait(fenced, workspace_id, wait.wait_id, to_state="expired", now_us=now_us)


def open_human_input_request(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    conversation_id: str,
    wait_id: str,
    requester_actor_id: str,
    authorised_responder_policy: Mapping[str, Any],
    prompt: Mapping[str, Any],
    resume_token: str,
    expires_at_us: int,
    now_us: int,
    generation_job_id: str | None = None,
    turn_id: str | None = None,
    agent_run_id: str | None = None,
) -> ChatWaitInteraction:
    """Ask a person for a bounded answer. `prompt.maxAnswerBytes` (default 4096) caps it."""
    limit = prompt.get("maxAnswerBytes", _DEFAULT_ANSWER_BYTES)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ANSWER_BYTES:
        raise ChatWaitConflict(f"maxAnswerBytes must be an integer from 1 to {MAX_ANSWER_BYTES}")
    if expires_at_us <= now_us:
        raise ChatWaitConflict("a human-input request must expire after it opens")
    return open_chat_wait(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
        conversation_id=conversation_id,
        wait_id=wait_id,
        kind="human_input",
        requester_actor_id=requester_actor_id,
        authorised_responder_policy=authorised_responder_policy,
        prompt=prompt,
        resume_token=resume_token,
        now_us=now_us,
        generation_job_id=generation_job_id,
        turn_id=turn_id,
        agent_run_id=agent_run_id,
        expires_at_us=expires_at_us,
    )


def authorised_responder_basis(policy: Mapping[str, Any], actor_id: str) -> str | None:
    """Why `actor_id` may answer a wait under `policy`, or `None` if it may not.

    Exported because the SNAPSHOT needs the same answer the decision command
    enforces. A projection carrying its own copy of "who may answer this wait"
    is how a snapshot comes to show a review to an actor the command would then
    refuse -- or hide one from an actor who may in fact answer it. One
    predicate, two callers.
    """
    return _authorisation_basis(policy, actor_id)


def _authorisation_basis(policy: Mapping[str, Any], actor_id: str) -> str | None:
    if policy.get("anyActor") is True:
        return "anyActor"
    if policy.get("actorId") == actor_id:
        return "actorId"
    actors = policy.get("actorIds")
    if isinstance(actors, list) and actor_id in actors:
        return "actorIds"
    return None


def decide_tool_review(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity | None,
    *,
    workspace_id: str,
    fencing_generation: int | None,
    command_id: str,
    wait_id: str,
    actor_id: str,
    decision: str,
    expected_request_version: int,
    target_action_digest: str | None,
    target_resource_revision: int | None,
    now_us: int,
    current_resource_revision: int | None = None,
    audit_ref: str | None = None,
    writer: chat.ChatWriter | None = None,
) -> chat.ChatWaitDecision:
    """The Review decision command: `approved` or `denied`, once, for the action the actor saw.

    `target_*` echo what the reviewer was shown. `current_resource_revision`, when the
    caller knows it, refuses a Review of a resource that has since moved.
    """
    if decision not in {"approved", "denied"}:
        raise ChatWaitConflict("a Review decision is approved or denied")
    return _decide(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
        command_kind="review_decision",
        command_id=command_id,
        wait_id=wait_id,
        actor_id=actor_id,
        outcome=decision,
        expected_request_version=expected_request_version,
        target_action_digest=target_action_digest,
        target_resource_revision=target_resource_revision,
        current_resource_revision=current_resource_revision,
        answer=None,
        audit_ref=audit_ref,
        now_us=now_us,
        writer=writer,
    )


def submit_human_input(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity | None,
    *,
    workspace_id: str,
    fencing_generation: int | None,
    command_id: str,
    wait_id: str,
    actor_id: str,
    expected_request_version: int,
    answer: Mapping[str, Any],
    now_us: int,
    audit_ref: str | None = None,
    writer: chat.ChatWriter | None = None,
) -> chat.ChatWaitDecision:
    """The human-input command. Only a digest and byte length of the answer are stored."""
    return _decide(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
        command_kind="human_input",
        command_id=command_id,
        wait_id=wait_id,
        actor_id=actor_id,
        outcome="answered",
        expected_request_version=expected_request_version,
        target_action_digest=None,
        target_resource_revision=None,
        current_resource_revision=None,
        answer=answer,
        audit_ref=audit_ref,
        now_us=now_us,
        writer=writer,
    )


def _decide(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity | None,
    *,
    workspace_id: str,
    fencing_generation: int | None,
    command_kind: str,
    command_id: str,
    wait_id: str,
    actor_id: str,
    outcome: str,
    expected_request_version: int,
    target_action_digest: str | None,
    target_resource_revision: int | None,
    current_resource_revision: int | None,
    answer: Mapping[str, Any] | None,
    audit_ref: str | None,
    now_us: int,
    writer: chat.ChatWriter | None = None,
) -> chat.ChatWaitDecision:
    if not command_id or not actor_id:
        raise ChatWaitConflict("a decision command names its command id and actor")
    answer_digest: str | None = None
    answer_bytes: int | None = None
    if answer is not None:
        if not isinstance(answer, Mapping) or not answer:
            raise ChatWaitConflict("a human answer is a non-empty JSON object")
        try:
            answer_bytes = len(to_canonical_json(dict(answer)).encode("utf-8"))
        except ChatContractDecodeError as error:
            raise ChatWaitConflict("a human answer is not representable as canonical JSON") from error
        answer_digest = _digest(dict(answer))
    request_digest = _digest(
        {
            "commandKind": command_kind,
            "waitId": wait_id,
            "actorId": actor_id,
            "outcome": outcome,
            "expectedRequestVersion": expected_request_version,
            "targetActionDigest": target_action_digest,
            "targetResourceRevision": target_resource_revision,
            "answerDigest": answer_digest,
        }
    )
    recorded = chat.read_chat_wait_decision_by_command(
        connection, workspace_id=workspace_id, command_id=command_id
    )
    if recorded is not None:
        if recorded.request_digest == request_digest:
            return recorded
        raise ChatWaitConflict("command id already names a different decision")
    wait = read_chat_wait(connection, workspace_id=workspace_id, wait_id=wait_id)
    if wait is None:
        raise ChatWaitConflict("chat wait is unknown")
    basis = _authorisation_basis(wait.authorised_responder_policy, actor_id)
    if basis is None:
        raise ChatWaitUnauthorized("actor is not authorised to decide this chat wait")
    if (command_kind == "review_decision") != (wait.kind in _REVIEW_KINDS):
        raise ChatWaitConflict("this command does not answer that kind of wait")
    if wait.state == "decided":
        raise ChatWaitConflict("chat wait has already been decided")
    if wait.state == "expired":
        raise ChatWaitExpired("chat wait has expired")
    if wait.state != "asked":
        raise ChatWaitConflict("chat wait is closed")
    if wait.expires_at_us is not None and now_us >= wait.expires_at_us:
        _expire_review(
            connection,
            identity,
            workspace_id=workspace_id,
            fencing_generation=fencing_generation,
            writer=writer,
            wait=wait,
            now_us=now_us,
        )
        raise ChatWaitExpired("chat wait has expired")
    if wait.version != expected_request_version:
        raise ChatToolReviewStale("chat wait version is stale")
    if (
        wait.target_action_digest != target_action_digest
        or wait.target_resource_revision != target_resource_revision
        or (
            current_resource_revision is not None
            and wait.target_resource_revision != current_resource_revision
        )
    ):
        raise ChatToolReviewStale("the reviewed action or resource revision is stale")
    if answer_bytes is not None:
        limit = min(
            int(wait.prompt.get("maxAnswerBytes", _DEFAULT_ANSWER_BYTES)), MAX_ANSWER_BYTES
        )
        if answer_bytes > limit:
            raise ChatWaitConflict("the answer is larger than this request allows")
    call: chat.ChatToolCall | None = None
    events: tuple[chat.ChatToolLifecycleEvent, ...] = ()
    if wait.tool_call_id is not None:
        call = _require_tool_call(connection, workspace_id=workspace_id, tool_call_id=wait.tool_call_id)
        events = _events(connection, workspace_id, call.tool_call_id)
        if _state(events) != "awaiting_review" or events[-1].wait_id != wait_id:
            raise ChatToolReviewStale("the tool call is no longer awaiting this Review")
        if call.post_policy_arguments_digest != wait.target_action_digest:
            raise ChatToolReviewStale("the tool call's action changed since the Review opened")
    with _fenced(connection, identity, workspace_id, fencing_generation, writer) as fenced:
        cursor = fenced.connection.execute(
            "UPDATE omnivia_chat_wait_interactions "
            "SET state = 'decided', decision = ?, decided_by_actor_id = ?, "
            "sensitive_answer_ciphertext_digest = ?, audit_ref = ?, version = version + 1, "
            "updated_at_us = ?, decided_at_us = ? "
            "WHERE workspace_id = ? AND wait_id = ? AND state = 'asked' AND version = ?",
            (
                outcome, actor_id, answer_digest, audit_ref, now_us, now_us,
                workspace_id, wait_id, expected_request_version,
            ),
        )
        if cursor.rowcount != 1:
            raise ChatWaitConflict("chat wait decision lost the compare-and-set race")
        fenced.append_chat_wait_decision(
            wait_id=wait_id,
            command_id=command_id,
            command_kind=command_kind,
            actor_id=actor_id,
            outcome=outcome,
            request_version=expected_request_version,
            authorization_basis=basis,
            request_digest=request_digest,
            decided_at_us=now_us,
            target_action_digest=target_action_digest,
            target_resource_revision=target_resource_revision,
            answer_digest=answer_digest,
            answer_bytes=answer_bytes,
            audit_ref=audit_ref,
        )
        if call is not None and outcome == "denied":
            step = _require_step(connection, workspace_id=workspace_id, step_id=call.step_id)
            _append(
                fenced, events, call.tool_call_id, "cancelled", now_us,
                reason_code="review_denied", wait_id=wait_id,
            )
            _mirror_terminal(fenced, call, step, state="cancelled", reason_code="review_denied", now_us=now_us)
            _sync_turn(connection, fenced, call, waiting=False, now_us=now_us)
    stored = chat.read_chat_wait_decision_by_command(
        connection, workspace_id=workspace_id, command_id=command_id
    )
    if stored is None:  # pragma: no cover - committed above
        raise StorageError(f"chat wait decision {command_id!r} did not settle")
    return stored
