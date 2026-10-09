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
    idempotency_key: str = f"idem-{RENAME_COMMAND_ID}",
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
        idempotency_key="idem-cmd-c10-rename-2",
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
) -> Any:
    return _dispatcher(holder).dispatch(
        _request(
            command,
            command_name=command_name,
            idempotency_key=f"idem-{key}",
            request_id=f"req-{key}",
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
