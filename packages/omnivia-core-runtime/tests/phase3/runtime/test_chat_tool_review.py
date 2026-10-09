"""C8 acceptance: tool-call lifecycle ledger, Review decisions and bounded human input (0071)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import test_chat_request_manifest as manifest
from omnivia_core_runtime.service.chat_interactions import (
    ChatWaitConflict,
    ChatWaitUnauthorized,
    decide_chat_wait,
    open_chat_wait,
    read_chat_wait,
)
from omnivia_core_runtime.service.chat_tool_lifecycle import (
    ChatToolLifecycleConflict,
    GovernedTool,
    open_chat_turn,
    record_tool_proposal,
    replay_chat_turn,
)
from omnivia_core_runtime.service.chat_tool_review import (
    ChatToolReviewStale,
    ChatWaitExpired,
    ToolNotExecuted,
    allow_tool_call,
    begin_policy_check,
    cancel_tool_call,
    decide_tool_review,
    deny_prepared_tool_call,
    open_human_input_request,
    recover_tool_calls,
    replay_tool_call,
    replay_turn_tool_calls,
    request_tool_review,
    run_governed_tool,
    start_tool_run,
    submit_human_input,
)
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    load_migrations,
    materialise_phase0_baseline,
)

WS = manifest.WORKSPACE_ID
TURN_ID = "turn-c8-1"
STEP_ID = "step-c8-1"
CALL_ID = "toolcall-c8-1"
WAIT_ID = "wait-c8-1"
REVIEWER = "actor-reviewer"
OUTSIDER = "actor-outsider"
ARGS = {"path": "notes.md", "mode": "append"}
RESOURCE = "resource:notes.md"
T = manifest.BASE_US


class Crash(BaseException):
    """Stands in for process death: nothing in the service catches it."""


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[manifest.m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    manifest.m1.bootstrap_and_migrate(path, workspace_id=WS)
    holder = manifest.m1.take_ownership(path)
    manifest.seed_running_attempt(holder)
    yield holder
    holder.connection.close()


def _kw(holder: manifest.m1.Owned) -> dict[str, Any]:
    return {
        "workspace_id": WS,
        "fencing_generation": holder.generation,
    }


def _propose(
    holder: manifest.m1.Owned,
    *,
    call_id: str = CALL_ID,
    step_id: str = STEP_ID,
    ordinal: int = 0,
) -> chat.ChatToolCall:
    open_chat_turn(
        holder.connection,
        holder.identity,
        generation_job_id=manifest.JOB_ID,
        generation_attempt_id=manifest.ATTEMPT_ID,
        turn_id=TURN_ID,
        now_us=T + 40,
        **_kw(holder),
    )
    return record_tool_proposal(
        holder.connection,
        holder.identity,
        turn_id=TURN_ID,
        step_id=step_id,
        step_ordinal=ordinal,
        tool_call_id=call_id,
        tool_name="notes.append",
        tool_version="1",
        registry_ref="registry-c8-v1",
        proposed_arguments=ARGS,
        now_us=T + 41,
        **_kw(holder),
    )


def _check(holder: manifest.m1.Owned, call_id: str = CALL_ID) -> None:
    begin_policy_check(
        holder.connection, holder.identity, tool_call_id=call_id, now_us=T + 50, **_kw(holder)
    )


def _review(
    holder: manifest.m1.Owned,
    *,
    call_id: str = CALL_ID,
    wait_id: str = WAIT_ID,
    revision: int = 7,
    expires: int = T + 1_000,
) -> Any:
    return request_tool_review(
        holder.connection,
        holder.identity,
        tool_call_id=call_id,
        post_policy_arguments=ARGS,
        wait_id=wait_id,
        requester_actor_id="actor-core-chat",
        authorised_responder_policy={"actorId": REVIEWER},
        prompt={"title": "Append to notes.md?"},
        resume_token=f"resume-{wait_id}",
        expires_at_us=expires,
        target_resource_ref=RESOURCE,
        target_resource_revision=revision,
        now_us=T + 60,
        **_kw(holder),
    )


def _decide(
    holder: manifest.m1.Owned,
    wait: Any,
    *,
    command_id: str = "cmd-1",
    actor: str = REVIEWER,
    decision: str = "approved",
    now_us: int = T + 100,
    **overrides: Any,
) -> chat.ChatWaitDecision:
    fields: dict[str, Any] = {
        "command_id": command_id,
        "wait_id": wait.wait_id,
        "actor_id": actor,
        "decision": decision,
        "expected_request_version": wait.version,
        "target_action_digest": wait.target_action_digest,
        "target_resource_revision": wait.target_resource_revision,
        "now_us": now_us,
    }
    fields.update(overrides)
    return decide_tool_review(holder.connection, holder.identity, **fields, **_kw(holder))


def _states(holder: manifest.m1.Owned, call_id: str = CALL_ID) -> list[str]:
    return [e.state for e in replay_tool_call(
        holder.connection, workspace_id=WS, tool_call_id=call_id
    ).events]


def _tool(handler: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> dict[str, GovernedTool]:
    return {"notes.append": GovernedTool("notes.append", "1", "registry-c8-v1", handler)}


def _run(holder: manifest.m1.Owned, tools: Mapping[str, GovernedTool], **extra: Any) -> Any:
    return run_governed_tool(
        holder.connection,
        holder.identity,
        tool_call_id=CALL_ID,
        tools=tools,
        now_us=T + 200,
        **extra,
        **_kw(holder),
    )


def _wait_state(holder: manifest.m1.Owned) -> str:
    wait = read_chat_wait(holder.connection, workspace_id=WS, wait_id=WAIT_ID)
    assert wait is not None
    return wait.state


# --- schema ----------------------------------------------------------------------------


def test_0071_is_the_next_forward_only_migration(owned: manifest.m1.Owned) -> None:
    catalogue = load_migrations()
    assert catalogue[-1].version == 71
    assert catalogue[-1].name == "0071_chat_tool_review_lifecycle.sql"
    assert catalogue[-2].name == "0070_chat_staged_attachments.sql"
    assert applied_migrations(owned.connection)[71] == catalogue[-1].checksum


def _append_event(holder: manifest.m1.Owned, sequence: int, state: str, **fields: Any) -> None:
    with chat.chat_writer(
        holder.connection, holder.identity, workspace_id=WS, fencing_generation=holder.generation
    ) as writer:
        writer.append_chat_tool_lifecycle_event(
            tool_call_id=CALL_ID,
            lifecycle_sequence=sequence,
            state=state,
            occurred_at_us=T + 300,
            **fields,
        )


def test_the_schema_refuses_to_restart_an_outcome_unknown_call(owned: manifest.m1.Owned) -> None:
    _propose(owned)
    _check(owned)
    allow_tool_call(
        owned.connection, owned.identity, tool_call_id=CALL_ID, post_policy_arguments=ARGS,
        now_us=T + 55, **_kw(owned),
    )
    start_tool_run(owned.connection, owned.identity, tool_call_id=CALL_ID, now_us=T + 56, **_kw(owned))
    recover_tool_calls(owned.connection, owned.identity, now_us=T + 57, **_kw(owned))
    assert _states(owned) == ["prepared", "policy_checking", "running", "outcome_unknown"]

    for state, extra in (
        ("running", {}),
        ("policy_checking", {}),
        ("succeeded", {"result_id": "x"}),
        ("cancelled", {"reason_code": "retry"}),
    ):
        with pytest.raises(sqlite3.DatabaseError):
            _append_event(owned, 5, state, **extra)
    assert _states(owned)[-1] == "outcome_unknown"


def test_the_ledger_is_append_only_and_cannot_skip_a_state(owned: manifest.m1.Owned) -> None:
    _propose(owned)
    _check(owned)
    with pytest.raises(sqlite3.DatabaseError):  # policy_checking -> succeeded is not a transition
        _append_event(owned, 3, "succeeded", result_id="x")
    with pytest.raises(sqlite3.DatabaseError):  # a sequence gap
        _append_event(owned, 9, "failed", reason_code="x")
    with pytest.raises(sqlite3.DatabaseError):
        owned.connection.execute("DELETE FROM omnivia_chat_tool_lifecycle_events")
    with pytest.raises(sqlite3.DatabaseError):
        owned.connection.execute("UPDATE omnivia_chat_tool_lifecycle_events SET state = 'failed'")


# --- allowed-by-policy path ------------------------------------------------------------


def test_an_allowed_call_runs_once_and_replays_deterministically(owned: manifest.m1.Owned) -> None:
    _propose(owned)
    _check(owned)
    calls: list[Mapping[str, Any]] = []

    def handler(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(arguments)
        return {"written": 12, "evidenceRefs": ["evidence-1"], "artifactRefs": ["artifact-1"]}

    allow_tool_call(
        owned.connection, owned.identity, tool_call_id=CALL_ID, post_policy_arguments=ARGS,
        now_us=T + 55, **_kw(owned),
    )
    first = _run(owned, _tool(handler), agent_run_id=None)
    again = _run(owned, _tool(handler))

    assert again == first
    assert len(calls) == 1
    replay = replay_tool_call(owned.connection, workspace_id=WS, tool_call_id=CALL_ID)
    assert replay.state == "succeeded"
    assert [e.state for e in replay.events] == [
        "prepared", "policy_checking", "running", "succeeded",
    ]
    assert replay.events[-1].evidence_refs == ("evidence-1",)
    assert replay.events[-1].artifact_refs == ("artifact-1",)
    assert replay.events[-1].result_id == replay.call.result_id == first.result_id
    assert replay.call.state == "succeeded"
    # The 0032 turn replay still reads the same call and result.
    legacy = replay_chat_turn(owned.connection, workspace_id=WS, turn_id=TURN_ID)
    assert [c.state for c in legacy.tool_calls] == ["succeeded"]
    assert replay_turn_tool_calls(owned.connection, workspace_id=WS, turn_id=TURN_ID)[0] == replay


def test_a_call_cannot_run_before_policy_allows_it(owned: manifest.m1.Owned) -> None:
    _propose(owned)
    with pytest.raises(ChatToolLifecycleConflict):
        start_tool_run(owned.connection, owned.identity, tool_call_id=CALL_ID, now_us=T + 60, **_kw(owned))
    _check(owned)
    with pytest.raises(ChatToolLifecycleConflict):  # checking, but not allowed
        start_tool_run(owned.connection, owned.identity, tool_call_id=CALL_ID, now_us=T + 61, **_kw(owned))
    with pytest.raises(sqlite3.DatabaseError):  # the schema agrees
        _append_event(owned, 3, "running")


def test_policy_denial_cancels_with_a_stable_reason(owned: manifest.m1.Owned) -> None:
    _propose(owned)
    _check(owned)
    denied = deny_prepared_tool_call(
        owned.connection, owned.identity, tool_call_id=CALL_ID, now_us=T + 70, **_kw(owned)
    )
    assert denied.state == "denied"
    replay = replay_tool_call(owned.connection, workspace_id=WS, tool_call_id=CALL_ID)
    assert replay.state == "cancelled"
    assert replay.events[-1].reason_code == "policy_denied"


# --- Review ----------------------------------------------------------------------------


def test_review_approval_authorises_exactly_one_run(owned: manifest.m1.Owned) -> None:
    _propose(owned)
    _check(owned)
    wait = _review(owned)
    assert _states(owned) == ["prepared", "policy_checking", "awaiting_review"]
    turn = chat.read_chat_turn(owned.connection, workspace_id=WS, turn_id=TURN_ID)
    assert turn is not None and turn.state == "waiting"

    decision = _decide(owned, wait)
    assert (decision.command_kind, decision.outcome, decision.authorization_basis) == (
        "review_decision", "approved", "actorId",
    )
    assert decision.request_version == wait.version
    ran: list[int] = []
    result = _run(
        owned,
        _tool(lambda args: ran.append(1) or {"ok": True}),
        current_resource_revision=7,
    )
    assert result.status == "succeeded" and ran == [1]
    replay = replay_tool_call(owned.connection, workspace_id=WS, tool_call_id=CALL_ID)
    assert [e.state for e in replay.events] == [
        "prepared", "policy_checking", "awaiting_review", "running", "succeeded",
    ]
    assert replay.events[3].wait_id == WAIT_ID and replay.decision == decision
    turn = chat.read_chat_turn(owned.connection, workspace_id=WS, turn_id=TURN_ID)
    assert turn is not None and turn.state == "running"
    assert replay.wait is not None and replay.wait.state == "decided"


def test_review_denial_cancels_and_nothing_can_run_it(owned: manifest.m1.Owned) -> None:
    _propose(owned)
    _check(owned)
    wait = _review(owned)
    _decide(owned, wait, decision="denied")

    replay = replay_tool_call(owned.connection, workspace_id=WS, tool_call_id=CALL_ID)
    assert replay.state == "cancelled" and replay.events[-1].reason_code == "review_denied"
    assert replay.call.state == "denied"
    with pytest.raises(ChatToolLifecycleConflict):
        _run(owned, _tool(lambda args: pytest.fail("a denied call must not run")))


def test_an_unreviewed_or_legacy_decided_wait_cannot_authorise_a_run(
    owned: manifest.m1.Owned,
) -> None:
    _propose(owned)
    _check(owned)
    wait = _review(owned)
    with pytest.raises(ChatToolLifecycleConflict):  # still asking
        start_tool_run(owned.connection, owned.identity, tool_call_id=CALL_ID, now_us=T + 90,
                       current_resource_revision=7, **_kw(owned))
    # The pre-C8 decide path leaves no command record, so it cannot open the gate.
    decide_chat_wait(
        owned.connection, owned.identity, wait_id=WAIT_ID, expected_version=wait.version,
        decided_by_actor_id=REVIEWER, decision="approved", now_us=T + 91, **_kw(owned),
    )
    with pytest.raises(ChatToolLifecycleConflict):
        start_tool_run(owned.connection, owned.identity, tool_call_id=CALL_ID, now_us=T + 92,
                       current_resource_revision=7, **_kw(owned))
    with pytest.raises(sqlite3.DatabaseError):
        _append_event(owned, 4, "running", wait_id=WAIT_ID)


def test_duplicate_decisions_are_refused_and_the_same_command_replays(
    owned: manifest.m1.Owned,
) -> None:
    _propose(owned)
    _check(owned)
    wait = _review(owned)
    first = _decide(owned, wait)

    assert _decide(owned, wait) == first  # same command id, same request: the recorded answer
    with pytest.raises(ChatWaitConflict):  # same command id, different decision
        _decide(owned, wait, decision="denied")
    with pytest.raises(ChatWaitConflict):  # a new command id cannot decide twice
        _decide(owned, wait, command_id="cmd-2", decision="denied")
    count = owned.connection.execute("SELECT COUNT(*) FROM omnivia_chat_wait_decisions").fetchone()
    assert count == (1,)
    assert replay_tool_call(owned.connection, workspace_id=WS, tool_call_id=CALL_ID).state == (
        "awaiting_review"
    )


def test_an_unauthorised_actor_changes_nothing(owned: manifest.m1.Owned) -> None:
    _propose(owned)
    _check(owned)
    wait = _review(owned)
    with pytest.raises(ChatWaitUnauthorized):
        _decide(owned, wait, actor=OUTSIDER)
    assert _wait_state(owned) == "asked"
    assert owned.connection.execute("SELECT COUNT(*) FROM omnivia_chat_wait_decisions").fetchone() == (0,)
    assert _states(owned)[-1] == "awaiting_review"


def test_the_two_commands_do_not_answer_each_others_waits(owned: manifest.m1.Owned) -> None:
    _propose(owned)
    _check(owned)
    review = _review(owned)
    human = open_human_input_request(
        owned.connection, owned.identity, conversation_id=manifest.CONVERSATION_ID,
        wait_id="wait-human-1", requester_actor_id="actor-core-chat",
        authorised_responder_policy={"actorId": REVIEWER}, prompt={"title": "Which file?"},
        resume_token="resume-h", expires_at_us=T + 1_000, now_us=T + 61,
        generation_job_id=manifest.JOB_ID, **_kw(owned),
    )
    with pytest.raises(ChatWaitConflict):
        submit_human_input(
            owned.connection, owned.identity, command_id="cmd-a", wait_id=review.wait_id,
            actor_id=REVIEWER, expected_request_version=review.version, answer={"file": "a"},
            now_us=T + 100, **_kw(owned),
        )
    with pytest.raises(ChatWaitConflict):
        _decide(owned, human, command_id="cmd-b", target_action_digest=None,
                target_resource_revision=None)
    # The schema also keeps the two kinds apart.
    with pytest.raises(sqlite3.DatabaseError):
        owned.connection.execute("UPDATE omnivia_chat_wait_decisions SET command_kind = 'human_input'")


def test_an_expired_review_is_refused_and_cancels_the_call(owned: manifest.m1.Owned) -> None:
    _propose(owned)
    _check(owned)
    wait = _review(owned, expires=T + 500)
    with pytest.raises(ChatWaitExpired):
        _decide(owned, wait, now_us=T + 500)

    assert _wait_state(owned) == "expired"
    replay = replay_tool_call(owned.connection, workspace_id=WS, tool_call_id=CALL_ID)
    assert replay.state == "cancelled" and replay.events[-1].reason_code == "review_expired"
    with pytest.raises(ChatWaitExpired):  # and it stays refused
        _decide(owned, wait, command_id="cmd-2", now_us=T + 100)


def test_recovery_expires_lapsed_reviews_and_keeps_pending_ones(owned: manifest.m1.Owned) -> None:
    _propose(owned)
    _check(owned)
    _review(owned, expires=T + 500)
    assert recover_tool_calls(owned.connection, owned.identity, now_us=T + 400, **_kw(owned)) == ()
    assert _states(owned)[-1] == "awaiting_review"
    assert recover_tool_calls(owned.connection, owned.identity, now_us=T + 600, **_kw(owned)) == (
        (CALL_ID, "cancelled"),
    )
    assert _wait_state(owned) == "expired"


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_request_version": 99},
        {"target_action_digest": "sha256:" + "0" * 64},
        {"target_resource_revision": 6},
        {"current_resource_revision": 8},
    ],
    ids=["request-version", "action-digest", "echoed-revision", "current-revision"],
)
def test_a_stale_review_is_refused_without_a_decision(
    owned: manifest.m1.Owned, overrides: dict[str, Any]
) -> None:
    _propose(owned)
    _check(owned)
    wait = _review(owned)
    with pytest.raises(ChatToolReviewStale):
        _decide(owned, wait, **overrides)
    assert _wait_state(owned) == "asked"
    assert _states(owned)[-1] == "awaiting_review"


def test_an_approval_goes_stale_if_the_resource_moves_before_the_run(
    owned: manifest.m1.Owned,
) -> None:
    _propose(owned)
    _check(owned)
    _decide(owned, _review(owned))
    with pytest.raises(ChatToolReviewStale):
        _run(owned, _tool(lambda args: pytest.fail("a stale approval must not run")),
             current_resource_revision=8)
    replay = replay_tool_call(owned.connection, workspace_id=WS, tool_call_id=CALL_ID)
    assert replay.state == "cancelled" and replay.events[-1].reason_code == "stale_review_target"


def test_a_decision_after_the_call_was_cancelled_is_stale(owned: manifest.m1.Owned) -> None:
    _propose(owned)
    _check(owned)
    wait = _review(owned)
    cancel_tool_call(owned.connection, owned.identity, tool_call_id=CALL_ID,
                     reason_code="user_stop", now_us=T + 80, **_kw(owned))
    assert _wait_state(owned) == "failed"  # the prompt is closed, not left dangling
    with pytest.raises(ChatWaitConflict):
        _decide(owned, wait)


def test_review_waits_are_immutable_once_open(owned: manifest.m1.Owned) -> None:
    _propose(owned)
    _check(owned)
    _review(owned)
    for column, value in (
        ("expires_at_us", T + 9_999),
        ("target_resource_revision", 8),
        ("target_action_digest", "sha256:" + "1" * 64),
    ):
        with pytest.raises(sqlite3.DatabaseError), chat.chat_writer(
            owned.connection, owned.identity, workspace_id=WS,
            fencing_generation=owned.generation,
        ) as writer:
            writer.connection.execute(
                f"UPDATE omnivia_chat_wait_interactions SET {column} = ?, "
                "version = version + 1 WHERE wait_id = ?",
                (value, WAIT_ID),
            )


# --- failure, interruption and restart --------------------------------------------------


def _allowed_and_ready(holder: manifest.m1.Owned) -> None:
    _propose(holder)
    _check(holder)
    allow_tool_call(holder.connection, holder.identity, tool_call_id=CALL_ID,
                    post_policy_arguments=ARGS, now_us=T + 55, **_kw(holder))


def test_a_handler_that_raises_is_outcome_unknown_and_never_retried(
    owned: manifest.m1.Owned,
) -> None:
    _allowed_and_ready(owned)

    def boom(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError("socket closed mid-write")

    with pytest.raises(RuntimeError):
        _run(owned, _tool(boom))
    replay = replay_tool_call(owned.connection, workspace_id=WS, tool_call_id=CALL_ID)
    assert replay.state == "outcome_unknown" and replay.events[-1].reason_code == "handler_raised"
    assert replay.call.state == "failed" and replay.call.failure_code == "outcome_unknown"
    with pytest.raises(ChatToolLifecycleConflict):
        _run(owned, _tool(lambda args: pytest.fail("an ambiguous call must not be re-run")))


def test_a_handler_that_refuses_before_any_effect_is_failed_not_unknown(
    owned: manifest.m1.Owned,
) -> None:
    _allowed_and_ready(owned)

    def refuse(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        raise ToolNotExecuted("quota_exceeded")

    with pytest.raises(ToolNotExecuted):
        _run(owned, _tool(refuse))
    replay = replay_tool_call(owned.connection, workspace_id=WS, tool_call_id=CALL_ID)
    assert (replay.state, replay.events[-1].reason_code) == ("failed", "quota_exceeded")


def test_an_unstorable_result_is_outcome_unknown(owned: manifest.m1.Owned) -> None:
    _allowed_and_ready(owned)
    with pytest.raises(Exception):  # noqa: B017 - any refusal; the settled state is the assertion
        _run(owned, _tool(lambda args: {"value": float("nan")}))
    assert replay_tool_call(owned.connection, workspace_id=WS, tool_call_id=CALL_ID).state == (
        "outcome_unknown"
    )


def test_a_crash_inside_the_tool_survives_a_restart_as_outcome_unknown(tmp_path: Path) -> None:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    manifest.m1.bootstrap_and_migrate(path, workspace_id=WS)
    holder = manifest.m1.take_ownership(path)
    manifest.seed_running_attempt(holder)
    _allowed_and_ready(holder)

    def die(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        raise Crash

    with pytest.raises(Crash):
        _run(holder, _tool(die))
    assert _states(holder)[-1] == "running"  # written before the tool was called
    holder.connection.close()

    reopened = manifest.m1.take_ownership(path)
    try:
        assert _states(reopened)[-1] == "running"
        assert recover_tool_calls(reopened.connection, reopened.identity, now_us=T + 300,
                                  **_kw(reopened)) == ((CALL_ID, "outcome_unknown"),)
        assert recover_tool_calls(reopened.connection, reopened.identity, now_us=T + 301,
                                  **_kw(reopened)) == ()
        replay = replay_tool_call(reopened.connection, workspace_id=WS, tool_call_id=CALL_ID)
        assert replay.state == "outcome_unknown"
        assert replay.events[-1].reason_code == "process_lost"
        with pytest.raises(ChatToolLifecycleConflict):
            _run(reopened, _tool(lambda args: pytest.fail("no retry may be inferred")))
    finally:
        reopened.connection.close()


def test_a_call_interrupted_before_it_ran_can_be_checked_again(tmp_path: Path) -> None:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    manifest.m1.bootstrap_and_migrate(path, workspace_id=WS)
    holder = manifest.m1.take_ownership(path)
    manifest.seed_running_attempt(holder)
    _propose(holder)
    _check(holder)
    holder.connection.close()

    reopened = manifest.m1.take_ownership(path)
    try:
        assert recover_tool_calls(reopened.connection, reopened.identity, now_us=T + 300,
                                  **_kw(reopened)) == ((CALL_ID, "interrupted"),)
        _check(reopened)  # no handler ran, so policy can look again
        allow_tool_call(reopened.connection, reopened.identity, tool_call_id=CALL_ID,
                        post_policy_arguments=ARGS, now_us=T + 310, **_kw(reopened))
        assert _run(reopened, _tool(lambda args: {"ok": True})).status == "succeeded"
        assert _states(reopened) == [
            "prepared", "policy_checking", "interrupted", "policy_checking", "running", "succeeded",
        ]
    finally:
        reopened.connection.close()


def test_a_pending_review_and_its_decision_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    manifest.m1.bootstrap_and_migrate(path, workspace_id=WS)
    holder = manifest.m1.take_ownership(path)
    manifest.seed_running_attempt(holder)
    _propose(holder)
    _check(holder)
    wait = _review(holder)
    holder.connection.close()

    reopened = manifest.m1.take_ownership(path)
    try:
        assert recover_tool_calls(reopened.connection, reopened.identity, now_us=T + 70,
                                  **_kw(reopened)) == ()
        decision = _decide(reopened, wait)
        reopened.connection.close()
        again = manifest.m1.take_ownership(path)
        try:
            assert _decide(again, wait) == decision  # the reconnecting client's retry
            assert _run(again, _tool(lambda args: {"ok": 1}), current_resource_revision=7).status == (
                "succeeded"
            )
        finally:
            again.connection.close()
    finally:
        if reopened.connection:
            try:
                reopened.connection.close()
            except sqlite3.ProgrammingError:
                pass


def test_an_unknown_tool_cancels_a_call_that_has_not_started(owned: manifest.m1.Owned) -> None:
    _allowed_and_ready(owned)
    from omnivia_core_runtime.service.chat_tool_lifecycle import ChatToolNotFound

    with pytest.raises(ChatToolNotFound):
        _run(owned, {})
    replay = replay_tool_call(owned.connection, workspace_id=WS, tool_call_id=CALL_ID)
    assert (replay.state, replay.events[-1].reason_code) == ("cancelled", "unknown_tool")


# --- bounded human input -----------------------------------------------------------------


def _human(holder: manifest.m1.Owned, **prompt: Any) -> Any:
    return open_human_input_request(
        holder.connection, holder.identity, conversation_id=manifest.CONVERSATION_ID,
        wait_id="wait-human-1", requester_actor_id="actor-core-chat",
        authorised_responder_policy={"actorIds": [REVIEWER, "actor-second"]},
        prompt={"title": "Which file?", **prompt}, resume_token="resume-h",
        expires_at_us=T + 1_000, now_us=T + 61, generation_job_id=manifest.JOB_ID,
        **_kw(holder),
    )


def _answer(holder: manifest.m1.Owned, wait: Any, **overrides: Any) -> chat.ChatWaitDecision:
    fields: dict[str, Any] = {
        "command_id": "cmd-h1", "wait_id": wait.wait_id, "actor_id": "actor-second",
        "expected_request_version": wait.version, "answer": {"file": "plan.md"},
        "now_us": T + 100,
    }
    fields.update(overrides)
    return submit_human_input(holder.connection, holder.identity, **fields, **_kw(holder))


def test_human_input_is_bounded_idempotent_and_stored_as_a_digest(owned: manifest.m1.Owned) -> None:
    wait = _human(owned, maxAnswerBytes=64)
    with pytest.raises(ChatWaitConflict):
        _answer(owned, wait, answer={"file": "x" * 200})
    with pytest.raises(ChatWaitConflict):
        _answer(owned, wait, answer={})
    assert read_chat_wait(owned.connection, workspace_id=WS, wait_id=wait.wait_id).state == "asked"  # type: ignore[union-attr]

    done = _answer(owned, wait)
    assert (done.command_kind, done.outcome, done.authorization_basis) == (
        "human_input", "answered", "actorIds",
    )
    assert done.answer_bytes == len('{"file":"plan.md"}') and done.answer_digest
    assert _answer(owned, wait) == done
    with pytest.raises(ChatWaitConflict):
        _answer(owned, wait, command_id="cmd-h2", answer={"file": "other.md"})
    stored = repr(owned.connection.execute("SELECT * FROM omnivia_chat_wait_decisions").fetchall())
    assert "plan.md" not in stored


def test_human_input_refuses_strangers_stale_versions_and_expiry(owned: manifest.m1.Owned) -> None:
    wait = _human(owned)
    with pytest.raises(ChatWaitUnauthorized):
        _answer(owned, wait, actor_id=OUTSIDER)
    with pytest.raises(ChatToolReviewStale):
        _answer(owned, wait, expected_request_version=wait.version + 1)
    with pytest.raises(ChatWaitExpired):
        _answer(owned, wait, now_us=T + 1_000)
    assert read_chat_wait(owned.connection, workspace_id=WS, wait_id=wait.wait_id).state == "expired"  # type: ignore[union-attr]


def test_human_input_bounds_are_validated_when_it_is_requested(owned: manifest.m1.Owned) -> None:
    for bad in (0, 16_385, "big", True):
        with pytest.raises(ChatWaitConflict):
            _human(owned, maxAnswerBytes=bad)
    with pytest.raises(ChatWaitConflict):  # a request must be able to lapse
        open_human_input_request(
            owned.connection, owned.identity, conversation_id=manifest.CONVERSATION_ID,
            wait_id="wait-human-2", requester_actor_id="actor-core-chat",
            authorised_responder_policy={"actorId": REVIEWER}, prompt={"title": "t"},
            resume_token="r", expires_at_us=T + 10, now_us=T + 10, **_kw(owned),
        )


def test_open_chat_wait_target_pair_is_validated(owned: manifest.m1.Owned) -> None:
    with pytest.raises(ChatWaitConflict):
        open_chat_wait(
            owned.connection, owned.identity, conversation_id=manifest.CONVERSATION_ID,
            wait_id="wait-x", kind="approval", requester_actor_id="a",
            authorised_responder_policy={"actorId": "b"}, prompt={"t": 1}, resume_token="r",
            now_us=T + 1, target_resource_ref="only-a-ref", **_kw(owned),
        )
