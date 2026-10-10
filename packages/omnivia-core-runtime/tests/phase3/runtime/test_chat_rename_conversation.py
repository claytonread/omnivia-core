"""C10: `RenameConversation` behind the public `chat.command` seam.

The command name and its request shape were already frozen in
`CHAT_COMMAND_NAMES` and `commands.schema.json`; what was missing was a
resolver, so every rename answered `dependency_unavailable`. These tests pin
the three things that resolver must get right: the title changes, NOTHING else
on the conversation row does, and the graph revision is carried rather than
bumped because a rename is not a graph fact.

Reuses the GB-05 harness fixtures for the same reason the draft suite does --
a fixture drift between these suites is then impossible.
"""

# ruff: noqa: F811

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import test_application_audit_idempotency_migration as m1
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.storage.chat import read_conversation
from test_chat_offline_queue import owned, seeded  # noqa: F401  (pytest fixtures)
from test_chat_submit_message_resolver import (
    CHAT_TABLES,
    CONVERSATION_ID,
    PRINCIPAL,
    WORKSPACE_ID,
    _counts,
    _dispatcher,
    _request,
)

from omnivia_core.chat_contract.v1 import PROTOCOL_VERSION
from omnivia_core.contracts.v1 import ErrorResponseEnvelope, SuccessResponseEnvelope

RENAME_COMMAND_ID = "cmd-c10-rename"


def _rename_command(
    *,
    command_id: str = RENAME_COMMAND_ID,
    actor_id: str = PRINCIPAL,
    conversation_id: str = CONVERSATION_ID,
    title: str = "A conversation worth finding again",
    **extra: Any,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "commandId": command_id,
        "workspaceId": WORKSPACE_ID,
        "conversationId": conversation_id,
        "actorId": actor_id,
        "title": title,
    }
    command.update(extra)
    return command


def _rename(
    holder: m1.Owned,
    command: Mapping[str, Any],
    *,
    idempotency_key: str = RENAME_COMMAND_ID,
    request_id: str = f"req-{RENAME_COMMAND_ID}",
) -> Any:
    return _dispatcher(holder).dispatch(
        _request(
            command,
            command_name="RenameConversation",
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
    )


def _conversation(holder: m1.Owned) -> Any:
    return read_conversation(
        holder.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
    )


def test_a_rename_changes_the_title_and_nothing_else(seeded: m1.Owned) -> None:
    before = _conversation(seeded)
    assert before is not None
    table_counts = _counts(seeded)

    response = _rename(seeded, _rename_command())
    assert isinstance(response, SuccessResponseEnvelope), response

    after = _conversation(seeded)
    assert after is not None
    assert after.title == "A conversation worth finding again"
    assert after.title_source == "user"

    # The whole point of reading the row first: `update_conversation` replaces
    # every mutable column, so a default passed for any of these would silently
    # un-archive the conversation or drop its branch selection.
    assert after.state == before.state
    assert after.default_branch_id == before.default_branch_id
    assert after.archived_at_us == before.archived_at_us
    assert after.tombstoned_at_us == before.tombstoned_at_us

    # A rename is not a graph fact. Bumping the revision would invalidate every
    # concurrently held expectation to record that a label changed.
    assert after.graph_revision == before.graph_revision
    assert after.latest_conversation_sequence == before.latest_conversation_sequence

    # No row anywhere else moved.
    after_counts = _counts(seeded)
    delta = {t: after_counts[t] - table_counts[t] for t in CHAT_TABLES}
    assert {t: n for t, n in delta.items() if n} == {}


def test_a_rename_replays_idempotently(seeded: m1.Owned) -> None:
    first = _rename(seeded, _rename_command())
    assert isinstance(first, SuccessResponseEnvelope), first
    before = _counts(seeded)

    replay = _rename(seeded, _rename_command())
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == first.result
    assert _counts(seeded) == {
        **before,
        s0.EXECUTIONS_TABLE: before[s0.EXECUTIONS_TABLE] + 1,
    }


def test_a_second_rename_replaces_the_title(seeded: m1.Owned) -> None:
    _rename(seeded, _rename_command())
    response = _rename(
        seeded,
        _rename_command(command_id="cmd-c10-rename-2", title="Renamed again"),
        idempotency_key="cmd-c10-rename-2",
        request_id="req-cmd-c10-rename-2",
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    after = _conversation(seeded)
    assert after is not None
    assert after.title == "Renamed again"


def test_an_empty_title_is_admitted(seeded: m1.Owned) -> None:
    # `graph.schema.json` sets the conversation title's minLength to 0, so an
    # empty title is a title the domain record can hold -- not a malformed
    # request this decoder may refuse.
    response = _rename(seeded, _rename_command(title=""))
    assert isinstance(response, SuccessResponseEnvelope), response
    after = _conversation(seeded)
    assert after is not None
    assert after.title == ""


def test_an_over_long_title_is_refused_without_writing(seeded: m1.Owned) -> None:
    before = _conversation(seeded)
    assert before is not None
    response = _rename(seeded, _rename_command(title="x" * 513))
    assert isinstance(response, ErrorResponseEnvelope), response
    assert _conversation(seeded).title == before.title


def test_a_title_carrying_a_nul_is_refused_as_a_request(seeded: m1.Owned) -> None:
    # 0029 refuses this at the CHECK. Refusing it here keeps it a malformed
    # request rather than an integrity failure raised mid-mutation.
    response = _rename(seeded, _rename_command(title="bad\x00title"))
    assert isinstance(response, ErrorResponseEnvelope), response


def test_an_undeclared_field_is_refused(seeded: m1.Owned) -> None:
    response = _rename(seeded, _rename_command(somethingElse="no"))
    assert isinstance(response, ErrorResponseEnvelope), response


def test_a_missing_title_is_refused(seeded: m1.Owned) -> None:
    # Unlike the create, whose title is optional, `title` is REQUIRED here.
    command = _rename_command()
    del command["title"]
    response = _rename(seeded, command)
    assert isinstance(response, ErrorResponseEnvelope), response


def test_another_actor_cannot_rename(seeded: m1.Owned) -> None:
    response = _rename(seeded, _rename_command(actor_id="actor-someone-else"))
    assert isinstance(response, ErrorResponseEnvelope), response


def test_an_unknown_conversation_conflicts_without_writing(seeded: m1.Owned) -> None:
    before = _counts(seeded)
    response = _rename(seeded, _rename_command(conversation_id="conv-does-not-exist"))
    assert isinstance(response, ErrorResponseEnvelope), response
    after = _counts(seeded)
    assert {t: after[t] - before[t] for t in CHAT_TABLES if after[t] != before[t]} == {}


# --- C10: ArchiveConversation and DeleteConversation -------------------------
#
# Same closed identity-only shape, and both terminal: 0029's update trigger
# aborts any state change out of `archived` or `tombstoned`, so an archived
# conversation can never later be tombstoned. These pin that as a clean
# conflict rather than an integrity abort raised mid-mutation.

ARCHIVE_COMMAND_ID = "cmd-c10-archive"
DELETE_COMMAND_ID = "cmd-c10-delete"


def _lifecycle_command(
    *,
    command_id: str,
    actor_id: str = PRINCIPAL,
    conversation_id: str = CONVERSATION_ID,
    **extra: Any,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "commandId": command_id,
        "workspaceId": WORKSPACE_ID,
        "conversationId": conversation_id,
        "actorId": actor_id,
    }
    command.update(extra)
    return command


def _lifecycle(
    holder: m1.Owned,
    command_name: str,
    command: Mapping[str, Any],
    *,
    key: str,
    expected_sequence: int = 1,
) -> Any:
    return _dispatcher(holder).dispatch(
        _request(
            command,
            command_name=command_name,
            idempotency_key=key,
            request_id=f"req-{key}",
            expected_sequence=expected_sequence,
        )
    )


def test_archive_marks_the_conversation_and_stamps_the_instant(seeded: m1.Owned) -> None:
    before = _conversation(seeded)
    assert before is not None
    assert before.state == "active"

    response = _lifecycle(
        seeded,
        "ArchiveConversation",
        _lifecycle_command(command_id=ARCHIVE_COMMAND_ID),
        key=ARCHIVE_COMMAND_ID,
    )
    assert isinstance(response, SuccessResponseEnvelope), response

    after = _conversation(seeded)
    assert after is not None
    assert after.state == "archived"
    assert after.archived_at_us is not None
    assert after.tombstoned_at_us is None
    # Not a graph fact, and the title is not this command's to touch.
    assert after.graph_revision == before.graph_revision
    assert after.title == before.title


def test_delete_tombstones_rather_than_removing_the_row(seeded: m1.Owned) -> None:
    # 0029 forbids DELETE on the conversations table outright.
    response = _lifecycle(
        seeded,
        "DeleteConversation",
        _lifecycle_command(command_id=DELETE_COMMAND_ID),
        key=DELETE_COMMAND_ID,
    )
    assert isinstance(response, SuccessResponseEnvelope), response

    after = _conversation(seeded)
    assert after is not None, "the row survives; only its state is terminal"
    assert after.state == "tombstoned"
    assert after.tombstoned_at_us is not None


def test_an_archived_conversation_cannot_then_be_deleted(seeded: m1.Owned) -> None:
    # The domain fact worth pinning: both states are terminal, so this is a
    # conflict and NOT a path the trigger has to abort.
    _lifecycle(
        seeded,
        "ArchiveConversation",
        _lifecycle_command(command_id=ARCHIVE_COMMAND_ID),
        key=ARCHIVE_COMMAND_ID,
    )
    response = _lifecycle(
        seeded,
        "DeleteConversation",
        _lifecycle_command(command_id=DELETE_COMMAND_ID),
        key=DELETE_COMMAND_ID,
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    after = _conversation(seeded)
    assert after is not None
    assert after.state == "archived", "the refusal wrote nothing"


def test_archiving_twice_under_different_ids_conflicts(seeded: m1.Owned) -> None:
    _lifecycle(
        seeded,
        "ArchiveConversation",
        _lifecycle_command(command_id=ARCHIVE_COMMAND_ID),
        key=ARCHIVE_COMMAND_ID,
    )
    response = _lifecycle(
        seeded,
        "ArchiveConversation",
        _lifecycle_command(command_id="cmd-c10-archive-2"),
        key="cmd-c10-archive-2",
    )
    # A different command id asking again is a conflict, not a silent success:
    # the caller believes it changed something.
    assert isinstance(response, ErrorResponseEnvelope), response


def test_archive_replays_idempotently(seeded: m1.Owned) -> None:
    first = _lifecycle(
        seeded,
        "ArchiveConversation",
        _lifecycle_command(command_id=ARCHIVE_COMMAND_ID),
        key=ARCHIVE_COMMAND_ID,
    )
    assert isinstance(first, SuccessResponseEnvelope), first
    replay = _lifecycle(
        seeded,
        "ArchiveConversation",
        _lifecycle_command(command_id=ARCHIVE_COMMAND_ID),
        key=ARCHIVE_COMMAND_ID,
    )
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == first.result


def test_a_lifecycle_command_refuses_an_undeclared_field(seeded: m1.Owned) -> None:
    response = _lifecycle(
        seeded,
        "ArchiveConversation",
        _lifecycle_command(command_id=ARCHIVE_COMMAND_ID, title="not mine to set"),
        key=ARCHIVE_COMMAND_ID,
    )
    assert isinstance(response, ErrorResponseEnvelope), response


def test_another_actor_cannot_archive(seeded: m1.Owned) -> None:
    response = _lifecycle(
        seeded,
        "ArchiveConversation",
        _lifecycle_command(command_id=ARCHIVE_COMMAND_ID, actor_id="actor-someone-else"),
        key=ARCHIVE_COMMAND_ID,
    )
    assert isinstance(response, ErrorResponseEnvelope), response


# --- C10: SelectConversationPath and SetDefaultConversationPath --------------
#
# Two rows on purpose. The select is the ACTOR's own view state, per actor and
# per device; the default is a CONVERSATION fact every actor sees. Collapsing
# them would make one person's navigation everyone's default.

from omnivia_core_runtime.storage.chat import read_actor_view_state  # noqa: E402
from test_chat_submit_message_resolver import BRANCH_ID  # noqa: E402


def _path(
    holder: m1.Owned,
    command_name: str,
    *,
    command_id: str,
    branch_id: str = BRANCH_ID,
    actor_id: str = PRINCIPAL,
    **extra: Any,
) -> Any:
    command: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "commandId": command_id,
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "actorId": actor_id,
        "branchId": branch_id,
    }
    command.update(extra)
    return _dispatcher(holder).dispatch(
        _request(
            command,
            command_name=command_name,
            idempotency_key=command_id,
            request_id=f"req-{command_id}",
        )
    )


def _view_state(holder: m1.Owned) -> Any:
    return read_actor_view_state(
        holder.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        actor_id=PRINCIPAL,
    )


def test_select_path_writes_the_actors_own_view_state(seeded: m1.Owned) -> None:
    conversation_before = _conversation(seeded)
    assert conversation_before is not None

    response = _path(seeded, "SelectConversationPath", command_id="cmd-c10-select")
    assert isinstance(response, SuccessResponseEnvelope), response

    view = _view_state(seeded)
    assert view is not None
    assert view.active_branch_id == BRANCH_ID

    # The conversation is untouched: one actor navigating is not everyone's
    # default, and it is not a graph fact either.
    after = _conversation(seeded)
    assert after is not None
    assert after.default_branch_id == conversation_before.default_branch_id
    assert after.graph_revision == conversation_before.graph_revision


def test_select_path_is_repeatable_and_advances_the_row_version(
    seeded: m1.Owned,
) -> None:
    _path(seeded, "SelectConversationPath", command_id="cmd-c10-select")
    first = _view_state(seeded)
    assert first is not None

    response = _path(seeded, "SelectConversationPath", command_id="cmd-c10-select-2")
    assert isinstance(response, SuccessResponseEnvelope), response
    second = _view_state(seeded)
    assert second is not None
    assert second.version == first.version + 1


def test_set_default_path_writes_the_conversation_fact(seeded: m1.Owned) -> None:
    before = _conversation(seeded)
    assert before is not None

    response = _path(seeded, "SetDefaultConversationPath", command_id="cmd-c10-default")
    assert isinstance(response, SuccessResponseEnvelope), response

    after = _conversation(seeded)
    assert after is not None
    assert after.default_branch_id == BRANCH_ID
    # Still not a graph fact, and no view state was invented for the actor.
    assert after.graph_revision == before.graph_revision
    assert _view_state(seeded) is None


def test_a_path_command_naming_an_unknown_branch_conflicts(seeded: m1.Owned) -> None:
    # Writing an active branch that names nothing would point the actor at a
    # path that cannot be read back.
    response = _path(
        seeded,
        "SelectConversationPath",
        command_id="cmd-c10-select-bad",
        branch_id="branch-does-not-exist",
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert _view_state(seeded) is None


def test_set_default_naming_an_unknown_branch_conflicts_without_writing(
    seeded: m1.Owned,
) -> None:
    before = _conversation(seeded)
    assert before is not None
    response = _path(
        seeded,
        "SetDefaultConversationPath",
        command_id="cmd-c10-default-bad",
        branch_id="branch-does-not-exist",
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    after = _conversation(seeded)
    assert after is not None
    assert after.default_branch_id == before.default_branch_id


def test_a_path_command_refuses_an_undeclared_field(seeded: m1.Owned) -> None:
    response = _path(
        seeded,
        "SelectConversationPath",
        command_id="cmd-c10-select",
        title="not mine to set",
    )
    assert isinstance(response, ErrorResponseEnvelope), response


def test_another_actor_cannot_select_a_path(seeded: m1.Owned) -> None:
    response = _path(
        seeded,
        "SelectConversationPath",
        command_id="cmd-c10-select",
        actor_id="actor-someone-else",
    )
    assert isinstance(response, ErrorResponseEnvelope), response


# --- C10: RegenerateResponse ------------------------------------------------
#
# A SIBLING assistant message under the same user parent. The contract is
# explicit that the original and its descendants remain, so this forks and
# never replaces.

from omnivia_core_runtime.storage import chat as chat_storage  # noqa: E402
from omnivia_core_runtime.storage.chat import read_branch, read_message  # noqa: E402
from test_chat_submit_message_resolver import (  # noqa: E402
    BASE_US,
    GRAPH_REVISION,
    ROOT_MESSAGE_ID,
)

REGENERATE_COMMAND_ID = "cmd-c10-regen"
#: The seeded fixture stops at the root USER message, so a regeneration test
#: has to put an assistant reply under it first -- that reply is what this
#: command regenerates.
ASSISTANT_MESSAGE_ID = "msg-c10-assistant"
SEEDED_JOB_ID = "job-c10-seeded"


def _seed_assistant_reply(holder: m1.Owned) -> None:
    with chat_storage.chat_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ) as writer:
        # 0029 refuses a message whose sequence points behind the
        # conversation's own, so the conversation has to keep up.
        writer.update_conversation(
            conversation_id=CONVERSATION_ID,
            expected_graph_revision=GRAPH_REVISION,
            graph_revision=GRAPH_REVISION,
            latest_conversation_sequence=2,
            state="active",
            default_branch_id=BRANCH_ID,
            updated_at_us=BASE_US + 11,
        )
        # And 0029 requires an assistant message to name the job that produced
        # it, so the reply needs a job to have come from.
        writer.append_generation_job(
            generation_job_id=SEEDED_JOB_ID,
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            trigger_message_id=ROOT_MESSAGE_ID,
            graph_revision_observed=GRAPH_REVISION,
            idempotency_key=SEEDED_JOB_ID,
            schema_version=1,
            created_at_us=BASE_US + 9,
            updated_at_us=BASE_US + 9,
        )
        writer.append_message(
            message_id=ASSISTANT_MESSAGE_ID,
            conversation_id=CONVERSATION_ID,
            parent_message_id=ROOT_MESSAGE_ID,
            role="assistant",
            author_type="provider",
            author_id="core.chat.generation",
            conversation_sequence=2,
            schema_version=1,
            content_hash=f"sha256:{'c' * 64}",
            completion_status="complete",
            visibility="standard",
            created_on_branch_id=BRANCH_ID,
            generation_job_id=SEEDED_JOB_ID,
            created_at_us=BASE_US + 10,
            committed_at_us=BASE_US + 10,
        )


def _regenerate(
    holder: m1.Owned,
    *,
    command_id: str = REGENERATE_COMMAND_ID,
    source_message_id: str = ASSISTANT_MESSAGE_ID,
    new_branch_id: str = "branch-c10-regen",
    actor_id: str = PRINCIPAL,
    **extra: Any,
) -> Any:
    command: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "commandId": command_id,
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "actorId": actor_id,
        "sourceMessageId": source_message_id,
        "newBranchId": new_branch_id,
    }
    command.update(extra)
    return _dispatcher(holder).dispatch(
        _request(
            command,
            command_name="RegenerateResponse",
            idempotency_key=command_id,
            request_id=f"req-{command_id}",
            # `_seed_assistant_reply` advanced the conversation to 2 to make
            # room for the reply, so the envelope's expectation has to match.
            expected_sequence=2,
        )
    )


def test_regenerate_forks_a_branch_at_the_user_parent_and_queues_a_job(
    seeded: m1.Owned,
) -> None:
    _seed_assistant_reply(seeded)
    source = read_message(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        message_id=ASSISTANT_MESSAGE_ID,
    )
    assert source is not None, "the fixture must seed an assistant message"
    assert source.parent_message_id is not None

    response = _regenerate(seeded)
    assert isinstance(response, SuccessResponseEnvelope), response

    branch = read_branch(
        seeded.connection, workspace_id=WORKSPACE_ID, branch_id="branch-c10-regen"
    )
    assert branch is not None
    assert branch.origin_kind == "assistant_regeneration"
    # The head is the USER parent, so the reply is a sibling of the original.
    assert branch.initial_head_message_id == source.parent_message_id
    assert branch.fork_parent_message_id == source.parent_message_id
    # The provenance the executor reads back to write the derivation.
    assert branch.fork_source_message_id == ASSISTANT_MESSAGE_ID

    # The original is untouched: regeneration never replaces.
    still_there = read_message(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        message_id=ASSISTANT_MESSAGE_ID,
    )
    assert still_there is not None
    assert still_there.tombstoned_at_us is None


def test_the_regeneration_job_is_identifiable_by_its_trigger(seeded: m1.Owned) -> None:
    """The discriminator the executor uses to write the `regeneration` edge.

    The executor cannot test the branch's provenance alone: `origin_kind` and
    `fork_source_message_id` are immutable, so every later generation on that
    branch would claim to be a regeneration of the original and append another
    false derivation -- silently, because the derived id differs each time and
    the table forbids UPDATE and DELETE.

    What distinguishes the regeneration's OWN job is that it triggers off the
    branch's initial head, the shared user parent. Any later send on the same
    branch triggers off its own new user message. This pins the half of that
    invariant the command owns; the executor owns the other half.
    """
    _seed_assistant_reply(seeded)
    assert isinstance(_regenerate(seeded), SuccessResponseEnvelope)

    branch = read_branch(
        seeded.connection, workspace_id=WORKSPACE_ID, branch_id="branch-c10-regen"
    )
    assert branch is not None
    job = seeded.connection.execute(
        "SELECT trigger_message_id FROM omnivia_chat_generation_jobs "
        "WHERE workspace_id = ? AND branch_id = ?",
        (WORKSPACE_ID, "branch-c10-regen"),
    ).fetchone()
    assert job is not None
    assert job[0] == branch.initial_head_message_id, (
        "the regeneration's job must trigger off the branch head it forked at, "
        "or the executor cannot tell it from a later turn on the same branch"
    )


def test_regenerate_replays_idempotently(seeded: m1.Owned) -> None:
    _seed_assistant_reply(seeded)
    first = _regenerate(seeded)
    assert isinstance(first, SuccessResponseEnvelope), first
    replay = _regenerate(seeded)
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == first.result


def test_regenerate_refuses_a_user_message(seeded: m1.Owned) -> None:
    # Only an assistant message has a response to regenerate.
    response = _regenerate(seeded, source_message_id=ROOT_MESSAGE_ID)
    assert isinstance(response, ErrorResponseEnvelope), response
    assert (
        read_branch(
            seeded.connection, workspace_id=WORKSPACE_ID, branch_id="branch-c10-regen"
        )
        is None
    )


def test_regenerate_refuses_an_unknown_message(seeded: m1.Owned) -> None:
    response = _regenerate(seeded, source_message_id="msg-does-not-exist")
    assert isinstance(response, ErrorResponseEnvelope), response


def test_regenerate_refuses_a_branch_id_already_in_use(seeded: m1.Owned) -> None:
    _seed_assistant_reply(seeded)
    _regenerate(seeded)
    response = _regenerate(
        seeded, command_id="cmd-c10-regen-2", new_branch_id="branch-c10-regen"
    )
    assert isinstance(response, ErrorResponseEnvelope), response


def test_regenerate_refuses_an_undeclared_field(seeded: m1.Owned) -> None:
    _seed_assistant_reply(seeded)
    response = _regenerate(seeded, title="not mine to set")
    assert isinstance(response, ErrorResponseEnvelope), response


def test_another_actor_cannot_regenerate(seeded: m1.Owned) -> None:
    _seed_assistant_reply(seeded)
    response = _regenerate(seeded, actor_id="actor-someone-else")
    assert isinstance(response, ErrorResponseEnvelope), response


# --- C10: CommitMessageAmendment --------------------------------------------
#
# A SIBLING user message under the same parent, on its own branch, with an
# `amendment` derivation. The original stays, as it does for a regeneration.

AMEND_COMMAND_ID = "cmd-c10-amend"


def _amend(
    holder: m1.Owned,
    *,
    command_id: str = AMEND_COMMAND_ID,
    source_message_id: str = ROOT_MESSAGE_ID,
    new_message_id: str = "msg-c10-amended",
    new_branch_id: str = "branch-c10-amend",
    actor_id: str = PRINCIPAL,
    text: str = "what I meant to ask",
    expected_sequence: int = 1,
    **extra: Any,
) -> Any:
    command: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "commandId": command_id,
        "workspaceId": WORKSPACE_ID,
        "conversationId": CONVERSATION_ID,
        "actorId": actor_id,
        "sourceMessageId": source_message_id,
        "newMessageId": new_message_id,
        "newBranchId": new_branch_id,
        "editableParts": [{"type": "text", "payload": {"text": text}}],
        "attachmentReferences": [],
        "contextReferences": [],
        "requestedAt": "2026-10-10T00:00:00Z",
    }
    command.update(extra)
    return _dispatcher(holder).dispatch(
        _request(
            command,
            command_name="CommitMessageAmendment",
            idempotency_key=command_id,
            request_id=f"req-{command_id}",
            expected_sequence=expected_sequence,
        )
    )


def test_an_amendment_forks_a_sibling_and_records_the_derivation(
    seeded: m1.Owned,
) -> None:
    response = _amend(seeded)
    assert isinstance(response, SuccessResponseEnvelope), response

    amended = read_message(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        message_id="msg-c10-amended",
    )
    assert amended is not None
    assert amended.role == "user"
    # A SIBLING: same parent as the message it amends.
    source = read_message(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        message_id=ROOT_MESSAGE_ID,
    )
    assert source is not None
    assert amended.parent_message_id == source.parent_message_id
    assert amended.created_on_branch_id == "branch-c10-amend"

    branch = read_branch(
        seeded.connection, workspace_id=WORKSPACE_ID, branch_id="branch-c10-amend"
    )
    assert branch is not None
    assert branch.origin_kind == "message_amendment"
    assert branch.fork_source_message_id == ROOT_MESSAGE_ID
    # The branch starts AT the amendment, which is what makes the root case
    # work without a special path.
    assert branch.initial_head_message_id == "msg-c10-amended"

    # The derivation is writable here, unlike the regeneration's.
    # Queried directly: storage has no derivations reader, and adding one for
    # a single assertion would be API nobody else needs.
    derivations = seeded.connection.execute(
        "SELECT derived_message_id, derivation_kind FROM "
        "omnivia_chat_message_derivations WHERE workspace_id = ? "
        "AND source_message_id = ?",
        (WORKSPACE_ID, ROOT_MESSAGE_ID),
    ).fetchall()
    assert ("msg-c10-amended", "amendment") in [tuple(row) for row in derivations]

    # The original survives: an amendment is a new turn, not an edit.
    assert source.tombstoned_at_us is None


def test_an_amendment_replays_idempotently(seeded: m1.Owned) -> None:
    first = _amend(seeded)
    assert isinstance(first, SuccessResponseEnvelope), first
    replay = _amend(seeded)
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == first.result


def test_an_amendment_refuses_an_assistant_message(seeded: m1.Owned) -> None:
    _seed_assistant_reply(seeded)
    response = _amend(
        seeded, source_message_id=ASSISTANT_MESSAGE_ID, expected_sequence=2
    )
    assert isinstance(response, ErrorResponseEnvelope), response


def test_a_mismatched_source_hash_refuses_without_writing(seeded: m1.Owned) -> None:
    response = _amend(
        seeded, expectedSourceContentHash=f"sha256:{'f' * 64}"
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert (
        read_message(
            seeded.connection,
            workspace_id=WORKSPACE_ID,
            conversation_id=CONVERSATION_ID,
            message_id="msg-c10-amended",
        )
        is None
    )


def test_a_matching_source_hash_is_accepted(seeded: m1.Owned) -> None:
    source = read_message(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        conversation_id=CONVERSATION_ID,
        message_id=ROOT_MESSAGE_ID,
    )
    assert source is not None
    response = _amend(seeded, expectedSourceContentHash=source.content_hash)
    assert isinstance(response, SuccessResponseEnvelope), response


def test_an_amendment_refuses_non_empty_references(seeded: m1.Owned) -> None:
    # Accepting these silently would claim a resolution this build does not
    # perform.
    response = _amend(
        seeded,
        attachmentReferences=[{"referenceId": "ref-1", "sourceRevision": 1}],
    )
    assert isinstance(response, ErrorResponseEnvelope), response


def test_an_amendment_refuses_an_undeclared_field(seeded: m1.Owned) -> None:
    response = _amend(seeded, title="not mine to set")
    assert isinstance(response, ErrorResponseEnvelope), response


def test_another_actor_cannot_amend(seeded: m1.Owned) -> None:
    response = _amend(seeded, actor_id="actor-someone-else")
    assert isinstance(response, ErrorResponseEnvelope), response


# --- C10: a terminal conversation accepts none of these commands ------------
#
# THE REGRESSION. All five shipped without the `state != "active"` guard that
# every other command in chat_submit has held since the foundation. 0029 does
# not close the gap -- its update trigger refuses only a state CHANGE out of a
# terminal state, and these commands write `state` back unchanged. So a
# tombstoned conversation could be renamed or re-pathed, and amend/regenerate
# would fork a branch and open a PAID generation job against a conversation
# the user had deleted.


def _delete_conversation(holder: m1.Owned, *, expected_sequence: int = 1) -> None:
    response = _lifecycle(
        holder,
        "DeleteConversation",
        _lifecycle_command(command_id=DELETE_COMMAND_ID),
        key=DELETE_COMMAND_ID,
        expected_sequence=expected_sequence,
    )
    assert isinstance(response, SuccessResponseEnvelope), response


def test_a_deleted_conversation_refuses_rename(seeded: m1.Owned) -> None:
    _delete_conversation(seeded)
    assert isinstance(_rename(seeded, _rename_command()), ErrorResponseEnvelope)


def test_a_deleted_conversation_refuses_both_path_commands(seeded: m1.Owned) -> None:
    _delete_conversation(seeded)
    for name in ("SelectConversationPath", "SetDefaultConversationPath"):
        response = _path(seeded, name, command_id=f"cmd-c10-{name}-dead")
        assert isinstance(response, ErrorResponseEnvelope), (name, response)


def test_a_deleted_conversation_refuses_an_amendment_and_opens_no_job(
    seeded: m1.Owned,
) -> None:
    _delete_conversation(seeded)
    before = _counts(seeded)
    response = _amend(seeded)
    assert isinstance(response, ErrorResponseEnvelope), response
    after = _counts(seeded)
    assert {t: after[t] - before[t] for t in CHAT_TABLES if after[t] != before[t]} == {}


def test_a_deleted_conversation_refuses_a_regeneration_and_opens_no_job(
    seeded: m1.Owned,
) -> None:
    # The sharpest case: an accepted regeneration leases a job, calls the
    # provider and BILLS for it, against a conversation the user deleted.
    _seed_assistant_reply(seeded)
    # The seeder advanced the conversation to 2 to make room for the reply.
    _delete_conversation(seeded, expected_sequence=2)
    jobs_before = seeded.connection.execute(
        "SELECT count(*) FROM omnivia_chat_generation_jobs WHERE workspace_id = ?",
        (WORKSPACE_ID,),
    ).fetchone()[0]

    response = _regenerate(seeded)
    assert isinstance(response, ErrorResponseEnvelope), response

    jobs_after = seeded.connection.execute(
        "SELECT count(*) FROM omnivia_chat_generation_jobs WHERE workspace_id = ?",
        (WORKSPACE_ID,),
    ).fetchone()[0]
    assert jobs_after == jobs_before, "a deleted conversation opened a generation job"


def test_an_archived_conversation_refuses_them_too(seeded: m1.Owned) -> None:
    # Archive is terminal for the same reason delete is.
    assert isinstance(
        _lifecycle(
            seeded,
            "ArchiveConversation",
            _lifecycle_command(command_id=ARCHIVE_COMMAND_ID),
            key=ARCHIVE_COMMAND_ID,
        ),
        SuccessResponseEnvelope,
    )
    assert isinstance(_rename(seeded, _rename_command()), ErrorResponseEnvelope)


# --- C10: the idempotency key must BE the command id ------------------------


def test_a_decoupled_idempotency_key_is_refused(seeded: m1.Owned) -> None:
    """The replay guarantee these commands document depends on this coupling.

    The seam keys replay on the request's idempotency key, not on `commandId`.
    Decoupled, a legitimate retry under a fresh key re-RUNS the command rather
    than replaying it -- and a second Archive then refuses "already archived",
    a second amendment collides on its message id, a second regeneration on
    its branch. `SubmitSteeringInput` has refused the mismatch since C6; these
    commands shipped without it.

    Refusing the mismatch up front is what makes "same command id, same
    answer" true rather than aspirational.
    """
    response = _rename(
        seeded,
        _rename_command(),
        idempotency_key="some-other-key",
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    after = _conversation(seeded)
    assert after is not None
    assert after.title != "A conversation worth finding again", "it wrote anyway"


def test_archive_retried_under_the_same_key_replays_rather_than_conflicting(
    seeded: m1.Owned,
) -> None:
    # The scenario the coupling protects: a host retrying after a reconnect
    # must get the first outcome back, not "already archived".
    first = _lifecycle(
        seeded,
        "ArchiveConversation",
        _lifecycle_command(command_id=ARCHIVE_COMMAND_ID),
        key=ARCHIVE_COMMAND_ID,
    )
    assert isinstance(first, SuccessResponseEnvelope), first
    retry = _lifecycle(
        seeded,
        "ArchiveConversation",
        _lifecycle_command(command_id=ARCHIVE_COMMAND_ID),
        key=ARCHIVE_COMMAND_ID,
    )
    assert isinstance(retry, SuccessResponseEnvelope), retry
    assert retry.result == first.result

