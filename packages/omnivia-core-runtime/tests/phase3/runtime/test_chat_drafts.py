"""C5A: durable draft commands behind the public `chat.command` seam.

Reuses the GB-01/GB-05 harnesses for their dispatcher, workspace fixtures and
`seeded` conversation rather than re-deriving them, so a fixture drift between
these suites is impossible. The slice is the C0 plan's focused one: stale
versions, duplicate command IDs, same ID with different content, discard
replay, and the identity-continuity save-after-discard cycle.
"""

from __future__ import annotations

import pytest
import test_application_audit_idempotency_migration as m1
import test_chat_offline_queue as gb05
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.storage import chat
from test_chat_offline_queue import owned, seeded  # noqa: F401  (pytest fixtures)
from test_chat_submit_message_resolver import (
    ACTOR_ID,
    CHAT_TABLES,
    CONVERSATION_ID,
    PRINCIPAL,
    ROOT_MESSAGE_ID,
    WORKSPACE_ID,
    _counts,
    _dispatcher,
    _request,
)

from omnivia_core.contracts.v1 import (
    ErrorResponseEnvelope,
    SuccessResponseEnvelope,
)

SAVE_COMMAND_ID = "cmd-c5a-save"
DISCARD_COMMAND_ID = "cmd-c5a-discard"


def _save_command(
    *,
    command_id: str = SAVE_COMMAND_ID,
    actor_id: str = PRINCIPAL,
    text: str = "a composition worth keeping",
    mode: str = "normal",
    source_message_id: str | None = None,
    draft_id: str | None = None,
    expected_version: int | None = None,
    attachment_references: Any = [],
    context_references: Any = [],
    **extra: Any,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "protocolVersion": "1.0",
        "commandId": command_id,
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "actorId": actor_id,
        "mode": mode,
        "text": text,
        "attachmentReferences": attachment_references,
        "contextReferences": context_references,
    }
    if source_message_id is not None:
        command["sourceMessageId"] = source_message_id
    if draft_id is not None:
        command["draftId"] = draft_id
    if expected_version is not None:
        command["expectedVersion"] = expected_version
    command.update(extra)
    return command


def _save(
    holder: m1.Owned,
    command: Mapping[str, Any],
    *,
    idempotency_key: str = f"idem-{SAVE_COMMAND_ID}",
    request_id: str = f"req-{SAVE_COMMAND_ID}",
) -> Any:
    return _dispatcher(holder).dispatch(
        _request(
            command,
            command_name="SaveDraft",
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
    )


def _discard(
    holder: m1.Owned,
    *,
    draft_id: str,
    command_id: str = DISCARD_COMMAND_ID,
    idempotency_key: str = f"idem-{DISCARD_COMMAND_ID}",
    request_id: str = f"req-{DISCARD_COMMAND_ID}",
    actor_id: str = PRINCIPAL,
) -> Any:
    return _dispatcher(holder).dispatch(
        _request(
            {
                "protocolVersion": "1.0",
                "commandId": command_id,
                "workspaceId": WORKSPACE_ID,
                "conversationId": CONVERSATION_ID,
                "actorId": actor_id,
                "draftId": draft_id,
            },
            command_name="DiscardDraft",
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
    )


def _draft(
    holder: m1.Owned, draft_id: str
) -> chat.Draft | None:
    return chat.read_draft(
        holder.connection, workspace_id=WORKSPACE_ID, draft_id=draft_id
    )


def _slot(holder: m1.Owned) -> chat.Draft | None:
    return chat.read_active_draft(
        holder.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        actor_id=PRINCIPAL,
    )


# --- 1. SaveDraft into an empty slot, and its idempotent replay ----------------------


def test_save_into_an_empty_slot_inserts_one_draft_and_replays_idempotently(
    seeded: m1.Owned,
) -> None:
    before = _counts(seeded)
    response = _save(seeded, _save_command())
    assert isinstance(response, SuccessResponseEnvelope), response
    assert response.result["command_result"]["resultRef"] == "draft:cmd-c5a-save.draft"

    after = _counts(seeded)
    delta = {table: after[table] - before[table] for table in CHAT_TABLES}
    assert {table: n for table, n in delta.items() if n} == {"omnivia_chat_drafts": 1}

    draft = _draft(seeded, "cmd-c5a-save.draft")
    assert draft is not None
    assert draft.version == 1
    assert draft.text_content == "a composition worth keeping"
    assert draft.references == ()
    assert draft.target is None
    assert draft.mode == "normal"
    assert draft.actor_id == PRINCIPAL

    replay_before = _counts(seeded)
    replay = _save(seeded, _save_command())
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == response.result
    assert _counts(seeded) == {
        **replay_before,
        s0.EXECUTIONS_TABLE: replay_before[s0.EXECUTIONS_TABLE] + 1,
    }


# --- 2. A second save advances the slot's own row; a stale expectation conflicts -----


def test_a_second_save_advances_the_slot_row_in_place(seeded: m1.Owned) -> None:
    _save(seeded, _save_command())
    first = _draft(seeded, "cmd-c5a-save.draft")
    assert first is not None

    response = _save(
        seeded,
        _save_command(command_id="cmd-c5a-save-2", text="revised while typing"),
        idempotency_key="idem-cmd-c5a-save-2",
        request_id="req-cmd-c5a-save-2",
    )
    assert isinstance(response, SuccessResponseEnvelope), response

    after = _draft(seeded, "cmd-c5a-save.draft")
    assert after is not None
    assert after.version == first.version + 1
    assert after.text_content == "revised while typing"


def test_a_stale_expected_version_conflicts_without_writing(seeded: m1.Owned) -> None:
    _save(seeded, _save_command())
    _save(
        seeded,
        _save_command(command_id="cmd-c5a-save-2", text="revised"),
        idempotency_key="idem-cmd-c5a-save-2",
        request_id="req-cmd-c5a-save-2",
    )
    before = _counts(seeded)

    response = _save(
        seeded,
        _save_command(
            command_id="cmd-c5a-save-stale",
            text="typed against yesterday",
            expected_version=1,
        ),
        idempotency_key="idem-cmd-c5a-save-stale",
        request_id="req-cmd-c5a-save-stale",
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before
    draft = _draft(seeded, "cmd-c5a-save.draft")
    assert draft is not None and draft.version == 2


# --- 3. Duplicate command IDs and fingerprint conflicts ------------------------------


def test_the_same_command_id_with_different_content_conflicts(seeded: m1.Owned) -> None:
    response = _save(seeded, _save_command(text="first composition"))
    assert isinstance(response, SuccessResponseEnvelope), response

    conflicting = _save(
        seeded,
        _save_command(text="a different composition"),
        idempotency_key="idem-cmd-c5a-save",  # same key, different body
        request_id="req-cmd-c5a-save-other",
    )
    assert isinstance(conflicting, ErrorResponseEnvelope), conflicting
    # The seam's own vocabulary for a same-key/different-fingerprint request.
    assert conflicting.error.code == "idempotency_conflict"

    draft = _draft(seeded, "cmd-c5a-save.draft")
    assert draft is not None
    assert draft.text_content == "first composition"


# --- 4. Refusals that write nothing ---------------------------------------------------


def test_a_foreign_actor_is_refused_before_writing(seeded: m1.Owned) -> None:
    before = _counts(seeded)
    response = _save(
        seeded, _save_command(actor_id=ACTOR_ID), idempotency_key="idem-c5a-foreign"
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "invalid_request"
    assert _counts(seeded) == before


def test_a_mode_source_pairing_the_table_would_refuse_is_refused_before_writing(
    seeded: m1.Owned,
) -> None:
    before = _counts(seeded)
    for command in (
        _save_command(mode="edit_message"),  # no sourceMessageId
        _save_command(source_message_id=ROOT_MESSAGE_ID),  # normal with a source
        _save_command(mode="edit_message", source_message_id="msg-not-here"),
    ):
        response = _save(
            seeded,
            command,
            idempotency_key=f"idem-{command['commandId']}",
            request_id=f"req-{command['commandId']}",
        )
        assert isinstance(response, ErrorResponseEnvelope), response
        assert response.error.code == "conflict" if "msg-not-here" in str(
            command
        ) else response.error.code == "invalid_request"
    assert _counts(seeded) == before


def test_a_malformed_reference_is_refused_before_writing(seeded: m1.Owned) -> None:
    before = _counts(seeded)
    response = _save(
        seeded,
        _save_command(
            attachment_references=[{"referenceId": "att-1", "path": "/etc/passwd"}]
        ),
        idempotency_key="idem-c5a-bad-ref",
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "invalid_request"
    assert _counts(seeded) == before


# --- 5. DiscardDraft: empty in place, replay idempotently, save again in place --------


def test_discard_empties_the_draft_in_place_and_replays_idempotently(
    seeded: m1.Owned,
) -> None:
    _save(seeded, _save_command())
    draft = _draft(seeded, "cmd-c5a-save.draft")
    assert draft is not None and draft.version == 1

    response = _discard(seeded, draft_id="cmd-c5a-save.draft")
    assert isinstance(response, SuccessResponseEnvelope), response

    discarded = _draft(seeded, "cmd-c5a-save.draft")
    assert discarded is not None
    assert discarded.text_content == ""
    assert discarded.references == ()
    assert discarded.target is None
    assert discarded.version == draft.version + 1

    replay_before = _counts(seeded)
    replay = _discard(seeded, draft_id="cmd-c5a-save.draft")
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == response.result
    assert _counts(seeded) == {
        **replay_before,
        s0.EXECUTIONS_TABLE: replay_before[s0.EXECUTIONS_TABLE] + 1,
    }


def test_a_save_after_a_discard_overwrites_the_same_row_in_place(
    seeded: m1.Owned,
) -> None:
    _save(seeded, _save_command())
    _discard(seeded, draft_id="cmd-c5a-save.draft")

    response = _save(
        seeded,
        _save_command(command_id="cmd-c5a-save-3", text="started again"),
        idempotency_key="idem-cmd-c5a-save-3",
        request_id="req-cmd-c5a-save-3",
    )
    assert isinstance(response, SuccessResponseEnvelope), response

    after = _draft(seeded, "cmd-c5a-save.draft")
    assert after is not None
    assert after.text_content == "started again"
    assert after.version == 3


def test_discarding_a_draft_that_is_not_this_actor_s_is_a_conflict(
    seeded: m1.Owned,
) -> None:
    before = _counts(seeded)
    response = _discard(
        seeded,
        draft_id="draft-that-does-not-exist",
        idempotency_key="idem-c5a-discard-missing",
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert _counts(seeded) == before


# --- 6. Restart: the draft survives a fresh connection to the same database ----------


def test_a_saved_draft_survives_a_restart_and_replays_against_it(
    tmp_path: Any,
) -> None:
    """A real restart, the way GB-05's own restart test simulates one.

    Full bootstrap on a fresh file, a seeded conversation, one save through the
    seam, the connection closed and the workspace re-owned on a fresh connection
    -- the same state a graceful service restart hands the next reader -- then:
    the draft is read back with its identity, text and version intact, and the
    SAME request dispatched again is answered from the seam's stored outcome
    (durable in the database), writing no second row.
    """
    path = tmp_path / "c5a-restart.sqlite"
    s0.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    try:
        gb05._seed_restart_workspace(holder)
        gb05._give_principal_a_view_state(holder)
        response = _save(holder, _save_command())
        assert isinstance(response, SuccessResponseEnvelope), response
    finally:
        holder.connection.close()

    reopened = m1.take_ownership(path)
    try:
        draft = chat.read_draft(
            reopened.connection, workspace_id=WORKSPACE_ID, draft_id="cmd-c5a-save.draft"
        )
        assert draft is not None
        assert draft.conversation_id == CONVERSATION_ID
        assert draft.actor_id == PRINCIPAL
        assert draft.text_content == "a composition worth keeping"
        assert draft.version == 1

        before = _counts(reopened)
        replay = _save(reopened, _save_command())
        assert isinstance(replay, SuccessResponseEnvelope), replay
        assert replay.result == response.result
        after = _counts(reopened)
        assert after["omnivia_chat_drafts"] == before["omnivia_chat_drafts"]
    finally:
        reopened.connection.close()
