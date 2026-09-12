"""T-0664 acceptance for durable Chat waits, approvals and attention projection."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import test_chat_request_manifest as manifest
from omnivia_core_runtime.service.chat_interactions import (
    ChatWaitConflict,
    ChatWaitInteraction,
    ChatWaitUnauthorized,
    attention_projection,
    decide_chat_wait,
    expire_chat_wait,
    open_chat_wait,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

WAIT_ID = "wait-t0664-1"
ACTOR_ALLOWED = "actor-allowed"
ACTOR_OTHER = "actor-other"


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[manifest.m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    manifest.m1.bootstrap_and_migrate(path, workspace_id=manifest.WORKSPACE_ID)
    holder = manifest.m1.take_ownership(path)
    manifest.seed_conversation_and_queue(holder)
    yield holder
    holder.connection.close()


def _open_wait(
    holder: manifest.m1.Owned,
    *,
    wait_id: str = WAIT_ID,
    actor_id: str = ACTOR_ALLOWED,
    kind: str = "approval",
    now_us: int = manifest.BASE_US + 100,
) -> ChatWaitInteraction:
    return open_chat_wait(
        holder.connection,
        holder.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=holder.generation,
        conversation_id=manifest.CONVERSATION_ID,
        wait_id=wait_id,
        kind=kind,
        requester_actor_id="actor-core-chat",
        authorised_responder_policy={"actorId": actor_id},
        prompt={"title": "Approve safe action?", "body": "Proceed with safe action."},
        resume_token=f"resume-{wait_id}",
        now_us=now_us,
        generation_job_id=manifest.JOB_ID,
        expires_at_us=now_us + 1_000,
    )

def test_open_chat_wait_is_idempotent_and_identity_strict(
    owned: manifest.m1.Owned,
) -> None:
    first = _open_wait(owned)
    second = _open_wait(owned)

    assert second == first
    with pytest.raises(ChatWaitConflict):
        open_chat_wait(
            owned.connection,
            owned.identity,
            workspace_id=manifest.WORKSPACE_ID,
            fencing_generation=owned.generation,
            conversation_id=manifest.CONVERSATION_ID,
            wait_id=WAIT_ID,
            kind="approval",
            requester_actor_id="actor-different-requester",
            authorised_responder_policy={"actorId": ACTOR_ALLOWED},
            prompt={
                "title": "Approve safe action?",
                "body": "Proceed with safe action.",
            },
            resume_token=WAIT_ID,
            now_us=manifest.BASE_US + 101,
            generation_job_id=manifest.JOB_ID,
            expires_at_us=manifest.BASE_US + 1_100,
        )


def test_decide_chat_wait_is_compare_and_set_and_redacts_answer(
    owned: manifest.m1.Owned,
) -> None:
    wait = _open_wait(owned)

    decided = decide_chat_wait(
        owned.connection,
        owned.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=owned.generation,
        wait_id=WAIT_ID,
        expected_version=wait.version,
        decided_by_actor_id=ACTOR_ALLOWED,
        decision="approved",
        sensitive_answer={"answer": "private approval text"},
        audit_ref="audit-wait-t0664-1",
        now_us=manifest.BASE_US + 110,
    )

    assert decided.state == "decided"
    assert decided.decision == "approved"
    assert decided.version == 2
    assert decided.sensitive_answer_ciphertext_digest is not None
    serialized_rows = json.dumps(
        [
            tuple(row)
            for row in owned.connection.execute(
                "SELECT * FROM omnivia_chat_wait_interactions WHERE workspace_id = ?",
                (manifest.WORKSPACE_ID,),
            )
        ],
        sort_keys=True,
    )
    assert "private approval text" not in serialized_rows

    with pytest.raises(ChatWaitConflict):
        decide_chat_wait(
            owned.connection,
            owned.identity,
            workspace_id=manifest.WORKSPACE_ID,
            fencing_generation=owned.generation,
            wait_id=WAIT_ID,
            expected_version=wait.version,
            decided_by_actor_id=ACTOR_ALLOWED,
            decision="denied",
            now_us=manifest.BASE_US + 111,
        )


def test_unauthorised_actor_fails_closed_without_decision(
    owned: manifest.m1.Owned,
) -> None:
    wait = _open_wait(owned)

    with pytest.raises(ChatWaitUnauthorized):
        decide_chat_wait(
            owned.connection,
            owned.identity,
            workspace_id=manifest.WORKSPACE_ID,
            fencing_generation=owned.generation,
            wait_id=WAIT_ID,
            expected_version=wait.version,
            decided_by_actor_id=ACTOR_OTHER,
            decision="approved",
            now_us=manifest.BASE_US + 120,
        )

    row = owned.connection.execute(
        "SELECT state, decision, decided_by_actor_id FROM omnivia_chat_wait_interactions "
        "WHERE workspace_id = ? AND wait_id = ?",
        (manifest.WORKSPACE_ID, WAIT_ID),
    ).fetchone()
    assert row == ("asked", None, None)


def test_expire_race_wins_once_and_blocks_late_decision(
    owned: manifest.m1.Owned,
) -> None:
    wait = _open_wait(owned)

    expired = expire_chat_wait(
        owned.connection,
        owned.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=owned.generation,
        wait_id=WAIT_ID,
        expected_version=wait.version,
        now_us=manifest.BASE_US + 130,
    )

    assert expired.state == "expired"
    assert expired.version == 2
    with pytest.raises(ChatWaitConflict):
        decide_chat_wait(
            owned.connection,
            owned.identity,
            workspace_id=manifest.WORKSPACE_ID,
            fencing_generation=owned.generation,
            wait_id=WAIT_ID,
            expected_version=wait.version,
            decided_by_actor_id=ACTOR_ALLOWED,
            decision="approved",
            now_us=manifest.BASE_US + 131,
        )


def test_attention_projection_only_returns_authorised_pending_waits(
    owned: manifest.m1.Owned,
) -> None:
    wait_a = _open_wait(owned, wait_id="wait-t0664-a", actor_id=ACTOR_ALLOWED)
    wait_b = _open_wait(
        owned,
        wait_id="wait-t0664-b",
        actor_id=ACTOR_OTHER,
        now_us=manifest.BASE_US + 101,
    )
    open_chat_wait(
        owned.connection,
        owned.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=owned.generation,
        conversation_id=manifest.CONVERSATION_ID,
        wait_id="wait-t0664-any",
        kind="human_input",
        requester_actor_id="actor-core-chat",
        authorised_responder_policy={"anyActor": True},
        prompt={"title": "Need input", "body": "Provide a safe value."},
        resume_token="resume-wait-any",
        now_us=manifest.BASE_US + 102,
    )
    expire_chat_wait(
        owned.connection,
        owned.identity,
        workspace_id=manifest.WORKSPACE_ID,
        fencing_generation=owned.generation,
        wait_id=wait_b.wait_id,
        expected_version=wait_b.version,
        now_us=manifest.BASE_US + 103,
    )

    projected = attention_projection(
        owned.connection,
        workspace_id=manifest.WORKSPACE_ID,
        actor_id=ACTOR_ALLOWED,
    )

    assert [wait.wait_id for wait in projected] == [
        wait_a.wait_id,
        "wait-t0664-any",
    ]
    assert all(wait.state == "asked" for wait in projected)


def test_wait_rows_reject_unguarded_mutation(
    owned: manifest.m1.Owned,
) -> None:
    _open_wait(owned)

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        owned.connection.execute(
            "UPDATE omnivia_chat_wait_interactions "
            "SET state = 'expired' WHERE workspace_id = ? AND wait_id = ?",
            (manifest.WORKSPACE_ID, WAIT_ID),
        )
