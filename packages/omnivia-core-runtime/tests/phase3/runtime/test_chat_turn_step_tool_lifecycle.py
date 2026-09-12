"""T-0660 acceptance for durable Chat turn, step and governed tool lifecycle."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import test_chat_request_manifest as manifest
from omnivia_core_runtime.service.chat_tool_lifecycle import (
    ChatToolLifecycleConflict,
    ChatToolNotFound,
    GovernedTool,
    MalformedToolProposal,
    approve_tool_call,
    deny_tool_call,
    execute_governed_tool_once,
    open_chat_turn,
    record_tool_proposal,
    replay_chat_turn,
)
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.storage.migrations import (
    load_migrations,
    materialise_phase0_baseline,
)

TURN_ID = "turn-t0660-1"
STEP_ID = "step-t0660-1"
TOOL_CALL_ID = "toolcall-t0660-1"
TOOL_NAME = "chat.echo"
TOOL_VERSION = "1"
REGISTRY_REF = "registry-chat-t0660-v1"


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[manifest.m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    manifest.m1.bootstrap_and_migrate(path, workspace_id=manifest.WORKSPACE_ID)
    holder = manifest.m1.take_ownership(path)
    manifest.seed_running_attempt(holder)
    yield holder
    holder.connection.close()


def _open_turn(holder: manifest.m1.Owned) -> chat.ChatTurn:
    return open_chat_turn(
        holder.connection,
        holder.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=holder.generation,
        generation_job_id=manifest.JOB_ID,
        generation_attempt_id=manifest.ATTEMPT_ID,
        turn_id=TURN_ID,
        now_us=manifest.BASE_US + 40,
    )


def _propose(holder: manifest.m1.Owned, arguments: Mapping[str, Any]) -> chat.ChatToolCall:
    _open_turn(holder)
    return record_tool_proposal(
        holder.connection,
        holder.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=holder.generation,
        turn_id=TURN_ID,
        step_id=STEP_ID,
        step_ordinal=0,
        tool_call_id=TOOL_CALL_ID,
        tool_name=TOOL_NAME,
        tool_version=TOOL_VERSION,
        registry_ref=REGISTRY_REF,
        proposed_arguments=arguments,
        now_us=manifest.BASE_US + 41,
    )


def _approve(
    holder: manifest.m1.Owned, arguments: Mapping[str, Any]
) -> chat.ChatToolCall:
    return approve_tool_call(
        holder.connection,
        holder.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=holder.generation,
        tool_call_id=TOOL_CALL_ID,
        post_policy_arguments=arguments,
        now_us=manifest.BASE_US + 42,
    )


def test_0032_schema_inventory_contains_expected_objects(
    owned: manifest.m1.Owned,
) -> None:
    migration = [item for item in load_migrations() if item.version == 32]
    assert len(migration) == 1
    assert migration[0].name == "0032_chat_turn_step_tool_lifecycle.sql"

    objects = {
        (row[0], row[1])
        for row in owned.connection.execute(
            "SELECT name, type FROM sqlite_master WHERE name LIKE 'omnivia_%'"
        )
    }

    assert {
        ("omnivia_chat_turns", "table"),
        ("omnivia_chat_turn_steps", "table"),
        ("omnivia_chat_tool_calls", "table"),
        ("omnivia_chat_tool_results", "table"),
    }.issubset(objects)
    assert {
        ("omnivia_idx_chat_turns_conversation", "index"),
        ("omnivia_idx_chat_turn_steps_turn", "index"),
        ("omnivia_idx_chat_tool_calls_turn", "index"),
        ("omnivia_idx_chat_tool_results_turn", "index"),
    }.issubset(objects)
    assert owned.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert owned.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_fake_tool_executes_once_and_replays_after_reopen(
    owned: manifest.m1.Owned,
) -> None:
    _propose(owned, {"query": "orders", "limit": 10})
    approved = _approve(owned, {"query": "orders", "limit": 3})
    calls: list[Mapping[str, Any]] = []

    def echo(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(dict(arguments))
        return {"echoed": arguments["query"], "limit": arguments["limit"]}

    result = execute_governed_tool_once(
        owned.connection,
        owned.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=owned.generation,
        tool_call_id=TOOL_CALL_ID,
        tools={
            TOOL_NAME: GovernedTool(
                name=TOOL_NAME,
                version=TOOL_VERSION,
                registry_ref=REGISTRY_REF,
                handler=echo,
            )
        },
        now_us=manifest.BASE_US + 43,
    )
    duplicate = execute_governed_tool_once(
        owned.connection,
        owned.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=owned.generation,
        tool_call_id=TOOL_CALL_ID,
        tools={},
        now_us=manifest.BASE_US + 44,
    )

    assert duplicate == result
    assert calls == [{"query": "orders", "limit": 3}]
    settled = chat.read_chat_tool_call(
        owned.connection, workspace_id=manifest.WORKSPACE_ID, tool_call_id=TOOL_CALL_ID
    )
    assert settled is not None
    assert settled.state == "succeeded"
    assert settled.post_policy_arguments == {"limit": 3, "query": "orders"}
    assert settled.executed_arguments_digest == approved.post_policy_arguments_digest
    assert result.result_payload == {"echoed": "orders", "limit": 3}

    path = owned.path
    owned.connection.close()
    reopened = open_database(path, OpenMode.READ_ONLY)
    try:
        replay = replay_chat_turn(
            reopened, workspace_id=manifest.WORKSPACE_ID, turn_id=TURN_ID
        )
    finally:
        reopened.close()

    assert replay.turn.turn_id == TURN_ID
    assert [(step.step_id, step.state) for step in replay.steps] == [
        (STEP_ID, "succeeded")
    ]
    assert [(call.tool_call_id, call.state) for call in replay.tool_calls] == [
        (TOOL_CALL_ID, "succeeded")
    ]
    assert [(item.result_id, item.result_payload) for item in replay.tool_results] == [
        (f"{TOOL_CALL_ID}.result", {"echoed": "orders", "limit": 3})
    ]


def test_restart_between_proposal_and_execution_runs_once(
    owned: manifest.m1.Owned,
) -> None:
    _propose(owned, {"query": "restart"})
    _approve(owned, {"query": "restart"})
    path = owned.path
    owned.connection.close()
    reopened = manifest.m1.take_ownership(path)
    calls: list[Mapping[str, Any]] = []

    try:
        execute_governed_tool_once(
            reopened.connection,
            reopened.identity,
            workspace_id=manifest.WORKSPACE_ID,
            fencing_generation=reopened.generation,
            tool_call_id=TOOL_CALL_ID,
            tools={
                TOOL_NAME: GovernedTool(
                    name=TOOL_NAME,
                    version=TOOL_VERSION,
                    registry_ref=REGISTRY_REF,
                    handler=lambda arguments: calls.append(dict(arguments))
                    or {"ok": True},
                )
            },
            now_us=manifest.BASE_US + 50,
        )
        execute_governed_tool_once(
            reopened.connection,
            reopened.identity,
            workspace_id=manifest.WORKSPACE_ID,
            fencing_generation=reopened.generation,
            tool_call_id=TOOL_CALL_ID,
            tools={},
            now_us=manifest.BASE_US + 51,
        )
    finally:
        reopened.connection.close()

    assert calls == [{"query": "restart"}]


def test_denied_tool_call_cannot_later_be_approved_or_executed(
    owned: manifest.m1.Owned,
) -> None:
    _propose(owned, {"query": "delete-all"})
    denied = deny_tool_call(
        owned.connection,
        owned.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=owned.generation,
        tool_call_id=TOOL_CALL_ID,
        failure_code="policy_denied",
        now_us=manifest.BASE_US + 45,
    )

    assert denied.state == "denied"
    with pytest.raises(ChatToolLifecycleConflict):
        _approve(owned, {"query": "delete-all"})
    with pytest.raises(ChatToolLifecycleConflict):
        execute_governed_tool_once(
            owned.connection,
            owned.identity,
            workspace_id=manifest.WORKSPACE_ID,
            fencing_generation=owned.generation,
            tool_call_id=TOOL_CALL_ID,
            tools={
                TOOL_NAME: GovernedTool(
                    name=TOOL_NAME,
                    version=TOOL_VERSION,
                    registry_ref=REGISTRY_REF,
                    handler=lambda _arguments: {"ok": True},
                )
            },
            now_us=manifest.BASE_US + 46,
        )


def test_unknown_tool_fails_closed_without_execution(
    owned: manifest.m1.Owned,
) -> None:
    _propose(owned, {"query": "known-safe"})
    _approve(owned, {"query": "known-safe"})

    with pytest.raises(ChatToolNotFound):
        execute_governed_tool_once(
            owned.connection,
            owned.identity,
            workspace_id=manifest.WORKSPACE_ID,
            fencing_generation=owned.generation,
            tool_call_id=TOOL_CALL_ID,
            tools={},
            now_us=manifest.BASE_US + 47,
        )

    failed = chat.read_chat_tool_call(
        owned.connection, workspace_id=manifest.WORKSPACE_ID, tool_call_id=TOOL_CALL_ID
    )
    assert failed is not None
    assert failed.state == "failed"
    assert failed.failure_code == "unknown_tool"
    assert (
        chat.read_chat_tool_result(
            owned.connection, workspace_id=manifest.WORKSPACE_ID, tool_call_id=TOOL_CALL_ID
        )
        is None
    )


def test_sensitive_or_malformed_tool_arguments_are_refused_before_write(
    owned: manifest.m1.Owned,
) -> None:
    _open_turn(owned)
    before = owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_chat_tool_calls WHERE workspace_id = ?",
        (manifest.WORKSPACE_ID,),
    ).fetchone()[0]

    with pytest.raises(MalformedToolProposal):
        record_tool_proposal(
            owned.connection,
            owned.identity,
            workspace_id=manifest.WORKSPACE_ID,
            fencing_generation=owned.generation,
            turn_id=TURN_ID,
            step_id=STEP_ID,
            step_ordinal=0,
            tool_call_id=TOOL_CALL_ID,
            tool_name=TOOL_NAME,
            tool_version=TOOL_VERSION,
            registry_ref=REGISTRY_REF,
            proposed_arguments={"apiToken": "secret-token-value"},
            now_us=manifest.BASE_US + 48,
        )

    after = owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_chat_tool_calls WHERE workspace_id = ?",
        (manifest.WORKSPACE_ID,),
    ).fetchone()[0]
    assert after == before


def test_stored_tool_rows_do_not_contain_obvious_secret_transport_terms(
    owned: manifest.m1.Owned,
) -> None:
    _propose(owned, {"query": "ordinary"})
    _approve(owned, {"query": "ordinary"})
    execute_governed_tool_once(
        owned.connection,
        owned.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=owned.generation,
        tool_call_id=TOOL_CALL_ID,
        tools={
            TOOL_NAME: GovernedTool(
                name=TOOL_NAME,
                version=TOOL_VERSION,
                registry_ref=REGISTRY_REF,
                handler=lambda arguments: {"received": arguments["query"]},
            )
        },
        now_us=manifest.BASE_US + 49,
    )

    stored = json.dumps(
        {
            "calls": [
                tuple(row)
                for row in owned.connection.execute(
                    "SELECT * FROM omnivia_chat_tool_calls WHERE workspace_id = ?",
                    (manifest.WORKSPACE_ID,),
                )
            ],
            "results": [
                tuple(row)
                for row in owned.connection.execute(
                    "SELECT * FROM omnivia_chat_tool_results WHERE workspace_id = ?",
                    (manifest.WORKSPACE_ID,),
                )
            ],
        },
        sort_keys=True,
    ).lower()
    for forbidden in ("endpoint", "header", "token", "secret", "authorization"):
        assert forbidden not in stored
