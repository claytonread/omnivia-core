"""W2-S acceptance for the Chat command and its atomic domain/outbox transaction.

`service/chat_command.py` is a composition, so these tests are about what the
composition guarantees rather than about re-proving the seam underneath it. Three
properties, and every test here is one of them.

*One transaction, or none of it.* A command's Chat writes -- the message it appends,
the branch head event, the branch projection, the conversation counters -- and the
transactional outbox row that announces them land with 0007's audit event, claim and
terminal outcome and 0013's execution record, or nothing lands at all. That is the
whole point of an outbox: the fact and the announcement of the fact are one write.
Every failure this file can reach is checked against the same complete row tally: the
callback raising after it wrote, a result the server refuses to serve, a grant outside
its window, a fencing generation that moved, a conversation the request's expectation
is stale about, and a command reaching past its writer into another workspace.

*Command replay is the idempotency authority, and there is only one.* An equal request
under the same caller-scoped idempotency key returns the stored answer without running
the command again, without appending a second message and without consuming a second
outbox cursor; the same key under a different canonical request is the typed conflict,
again without running it.

*The workspace is the grant's.* The writer handed to a command is bound to the
workspace the server issued the grant for, not to anything the request said, and a
grant that does not cover the request's workspace is refused before a transaction is
opened.

The fixtures are the accepted ones: `m1` owns the workspace and `s0` owns the request,
session, authorization and equivalence a mutation is served under. W2-S registers no
public Chat operation -- the Application Contract catalogue is frozen and RT-111 owns
the public surface -- so the operation named below is chosen for its posture only.

**The graph-revision invariant these tests are built around.**
`omnivia_chat_branch_head_events.graph_revision` foreign-keys
`omnivia_chat_conversations (workspace_id, conversation_id, graph_revision)` and the
conversation is a single mutable row, so bumping `graph_revision` after a durable head
event cites the old value would orphan that event. The seeded conversation therefore
stays at `graph_revision = 1` for the life of every test, and each command advances
`latest_conversation_sequence` alone -- the same stable same-revision update
`test_chat_repository.py` uses for exactly this reason.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.fencing import (
    MutationGuard,
    StaleGeneration,
    read_guard,
)
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.authorization import (
    AuthorizedApplicationContext,
    ServiceBinding,
    authorize_application_request,
)
from omnivia_core_runtime.service.chat_command import (
    ChatAggregateConflict,
    ChatAggregateExpectation,
    execute_chat_command,
    is_governed_command_result,
)
from omnivia_core_runtime.service.mutation import (
    MutationDenied,
    MutationGrant,
    MutationIdempotencyConflict,
    MutationSeamFault,
    MutationSettlementContext,
    issue_mutation_grant,
)
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.chat import ChatWriter
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ERROR_CODE_CONFLICT,
    OPERATION_CATALOGUE,
    RETRY_CLASS_NON_RETRYABLE,
    RequestEnvelope,
    get_operation_metadata,
    idempotency_equivalence,
)

# --- the command this file issues ----------------------------------------------
#
# The seam names no operation: the Application Contract catalogue is frozen, W2-S adds
# no public Chat operation to it, and a caller brings its own authorized context and
# grant. So the worked example here is chosen for its posture rather than its meaning --
# `memory.create` is workspace-scoped and side-effecting, takes an idempotency key,
# requires no mutation precondition, and is not one of the operations 0015 requires an
# application bridge settlement from. What is under test is therefore the Chat command
# envelope and its writes, with no second precondition or bridge mechanism standing in
# front of them.

OPERATION = "memory.create"
ENTRY = get_operation_metadata(OPERATION)

WORKSPACE_ID = m1.WORKSPACE_ID
OTHER_WORKSPACE_ID = m1.OTHER_WORKSPACE_ID

BASE_US = 2_500_000_000_000_000
COMMAND_WALL = datetime.fromtimestamp((BASE_US + 1_000_000) / 1_000_000, tz=UTC)

CONVERSATION_ID = "conv-w2s-01"
BRANCH_ID = "branch-w2s-main"
ACTOR_ID = "actor-w2s-human"
ROOT_MESSAGE_ID = "msg-w2s-root"
ROOT_PART_ID = "part-w2s-root-0"

#: The conversation's revision, held constant for the life of every test. See the module
#: docstring: durable branch head events cite it through a non-deferrable foreign key.
GRAPH_REVISION = 1
SEEDED_SEQUENCE = 1

IDEMPOTENCY_KEY = "w2s-idem-0001"

OPERATION_INPUT: Mapping[str, Any] = {"content": "the message this command appends"}
OTHER_INPUT: Mapping[str, Any] = {"content": "a different request under one key"}

#: Everything one accepted Chat command may write: the application's four durable
#: settlement relations and every canonical Chat relation a command could reach. Counted
#: as a whole rather than one table at a time, because "no partial materialisation" is a
#: statement about the set.
CHAT_TABLES = (
    "omnivia_chat_conversations",
    "omnivia_chat_messages",
    "omnivia_chat_message_parts",
    "omnivia_chat_message_derivations",
    "omnivia_chat_message_branches",
    "omnivia_chat_branch_head_events",
    "omnivia_chat_conversation_view_states",
    "omnivia_chat_drafts",
    "omnivia_chat_queued_submissions",
    "omnivia_chat_generation_jobs",
    "omnivia_chat_generation_attempts",
    "omnivia_chat_generation_events",
    "omnivia_chat_transactional_outbox",
)
COUNTED_TABLES = s0.DURABLE_TABLES + CHAT_TABLES


def digest(char: str) -> str:
    return "sha256:" + char * 64


# --- fixtures -------------------------------------------------------------------


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    """A workspace at the full canonical schema, owned by this service instance."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


@pytest.fixture
def seeded(owned: m1.Owned) -> m1.Owned:
    """One conversation, its root message and part, and a branch at that root.

    Seeded through W2-R's own fenced writer, in its own transaction, so what a W2-S
    command is tested appending to is a Chat graph this repository already knows how to
    produce. `graph_revision` is `1` here and stays `1` everywhere.
    """
    with chat.chat_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        writer.append_conversation(
            conversation_id=CONVERSATION_ID,
            state="active",
            graph_revision=GRAPH_REVISION,
            latest_conversation_sequence=0,
            schema_version=1,
            created_by_actor_id=ACTOR_ID,
            created_at_us=BASE_US,
            updated_at_us=BASE_US,
        )
        writer.update_conversation(
            conversation_id=CONVERSATION_ID,
            expected_graph_revision=GRAPH_REVISION,
            graph_revision=GRAPH_REVISION,
            latest_conversation_sequence=SEEDED_SEQUENCE,
            state="active",
            updated_at_us=BASE_US + 1,
        )
        writer.append_message(
            message_id=ROOT_MESSAGE_ID,
            conversation_id=CONVERSATION_ID,
            role="user",
            author_type="human",
            author_id=ACTOR_ID,
            conversation_sequence=SEEDED_SEQUENCE,
            schema_version=1,
            content_hash=digest("a"),
            completion_status="complete",
            visibility="standard",
            created_at_us=BASE_US + 2,
            committed_at_us=BASE_US + 2,
        )
        writer.append_message_part(
            part_id=ROOT_PART_ID,
            message_id=ROOT_MESSAGE_ID,
            conversation_id=CONVERSATION_ID,
            part_index=0,
            part_type="text",
            schema_version=1,
            visibility="standard",
            payload={"text": "hello"},
            content_hash=digest("b"),
            created_at_us=BASE_US + 3,
            provenance="human",
        )
        writer.append_branch(
            branch_id=BRANCH_ID,
            conversation_id=CONVERSATION_ID,
            origin_kind="original",
            initial_head_message_id=ROOT_MESSAGE_ID,
            created_by_actor_id=ACTOR_ID,
            created_at_us=BASE_US + 4,
            created_conversation_sequence=SEEDED_SEQUENCE,
            schema_version=1,
        )
        writer.append_branch_head_event(
            event_id="head-event-w2s-1",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            head_version=1,
            previous_head_message_id=None,
            new_head_message_id=ROOT_MESSAGE_ID,
            cause="branch_created",
            command_id="command-w2s-seed",
            graph_revision=GRAPH_REVISION,
            conversation_sequence=SEEDED_SEQUENCE,
            actor_id=ACTOR_ID,
            occurred_at_us=BASE_US + 4,
            schema_version=1,
        )
    return owned


# --- the request, the grant and the command -------------------------------------


def clock(monotonic: float = s0.MONOTONIC_BASE) -> FakeClock:
    return FakeClock(monotonic=monotonic, wall=COMMAND_WALL)


def authorize(
    *,
    operation_input: Mapping[str, Any] = OPERATION_INPUT,
    **overrides: Any,
) -> AuthorizedApplicationContext:
    """The real authorization seam, for this file's operation."""
    overrides.setdefault("idempotency_key", IDEMPOTENCY_KEY)
    return s0.authorize(ENTRY, operation_input=operation_input, **overrides)


def equivalence_for(
    *,
    operation_input: Mapping[str, Any] = OPERATION_INPUT,
    **overrides: Any,
) -> Any:
    """The accepted contract function's own equivalence value for this request."""
    overrides.setdefault("idempotency_key", IDEMPOTENCY_KEY)
    return s0.equivalence_for(ENTRY, operation_input=operation_input, **overrides)


def issue(
    holder: m1.Owned,
    context: AuthorizedApplicationContext,
    *,
    equivalence: Any = None,
    guard: MutationGuard | None = None,
    clock_: FakeClock | None = None,
) -> MutationGrant:
    """Issue a grant from the live guard row, exactly as a server wiring would."""
    live = read_guard(holder.connection)
    assert live is not None
    return issue_mutation_grant(
        context,
        session=s0.session_for(ENTRY),
        binding=s0.BINDING,
        guard=live if guard is None else guard,
        equivalence=equivalence_for() if equivalence is None else equivalence,
        clock=clock() if clock_ is None else clock_,
    )


def next_outbox_cursor(connection: sqlite3.Connection, workspace_id: str) -> int:
    """The cursor 0029's own INSERT guard requires the next outbox row to carry."""
    row = connection.execute(
        "SELECT COALESCE(MAX(outbox_cursor), 0) + 1 "
        "FROM omnivia_chat_transactional_outbox WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    return int(row[0])


class CommandFailure(RuntimeError):
    """Injected command failure, distinguishable from any refusal the seam raises."""


class AppendMessageCommand:
    """One Chat command: append a message, move the branch head, announce it.

    Six writes in one callback, spanning four Chat relations plus the transactional
    outbox, so "the domain rows and their announcement commit together" is a fact about
    a real command rather than about a single INSERT. The order is the one 0029 admits:
    the conversation's `latest_conversation_sequence` moves first (a message may not
    point past it), then the message, then the durable head event, then the branch
    projection that event backs, then the outbox row.

    `graph_revision` deliberately does not move. The head event this command appends
    cites it through a non-deferrable foreign key into the conversation's single row,
    and so does the one the fixture seeded; advancing it would orphan both.

    Counts its own invocations, which is what makes "a replay does not run the command
    again" a measured fact rather than an inference from row counts. `fail` writes every
    row and *then* raises, so the rollback it proves is a rollback of real Chat
    materialisation rather than of a callback that never got started.
    """

    def __init__(
        self,
        *,
        message_id: str = "msg-w2s-second",
        head_event_id: str = "head-event-w2s-2",
        domain_event_id: str = "domain-event-w2s-2",
        text: str = "the appended message",
        fail: bool = False,
    ) -> None:
        self.message_id = message_id
        self.head_event_id = head_event_id
        self.domain_event_id = domain_event_id
        self.text = text
        self.fail = fail
        self.calls = 0
        self.workspaces: list[str] = []

    def __call__(
        self, writer: ChatWriter, settlement: MutationSettlementContext
    ) -> Mapping[str, Any]:
        self.calls += 1
        self.workspaces.append(writer.workspace_id)
        now = settlement.settled_at_us

        conversation = chat.read_conversation(
            writer.connection,
            workspace_id=writer.workspace_id,
            conversation_id=CONVERSATION_ID,
        )
        assert conversation is not None
        sequence = conversation.latest_conversation_sequence + 1
        head_events = chat.read_branch_head_events(
            writer.connection, workspace_id=writer.workspace_id, branch_id=BRANCH_ID
        )
        head_version = head_events[-1].head_version
        previous_head = head_events[-1].new_head_message_id

        writer.update_conversation(
            conversation_id=CONVERSATION_ID,
            expected_graph_revision=GRAPH_REVISION,
            graph_revision=GRAPH_REVISION,
            latest_conversation_sequence=sequence,
            state="active",
            updated_at_us=now,
        )
        writer.append_message(
            message_id=self.message_id,
            conversation_id=CONVERSATION_ID,
            role="user",
            author_type="human",
            author_id=ACTOR_ID,
            parent_message_id=previous_head,
            conversation_sequence=sequence,
            schema_version=1,
            content_hash=digest("c"),
            completion_status="complete",
            visibility="standard",
            created_on_branch_id=BRANCH_ID,
            created_at_us=now,
            committed_at_us=now,
        )
        writer.append_branch_head_event(
            event_id=self.head_event_id,
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            head_version=head_version + 1,
            previous_head_message_id=previous_head,
            new_head_message_id=self.message_id,
            cause="user_message_appended",
            # The seam's own claim identity, so the durable head event names the exact
            # mutation claim that settled it. Nothing correlates them if the command
            # invents its own identifier here.
            command_id=settlement.claim_id,
            graph_revision=GRAPH_REVISION,
            conversation_sequence=sequence,
            actor_id=ACTOR_ID,
            occurred_at_us=now,
            schema_version=1,
        )
        writer.update_branch_head(
            branch_id=BRANCH_ID,
            expected_head_version=head_version,
            head_version=head_version + 1,
            current_head_message_id=self.message_id,
            state="open",
        )
        writer.append_outbox_entry(
            outbox_cursor=next_outbox_cursor(writer.connection, writer.workspace_id),
            domain_event_id=self.domain_event_id,
            event_kind="chat.message.appended",
            payload={"conversationId": CONVERSATION_ID, "messageId": self.message_id},
            created_at_us=now,
            conversation_id=CONVERSATION_ID,
        )
        if self.fail:
            raise CommandFailure("the chat command refused after writing")
        return {"commandId": settlement.claim_id, "status": "completed"}


def run_command(
    holder: m1.Owned,
    *,
    command: Any,
    context: AuthorizedApplicationContext | None = None,
    grant: MutationGrant | None = None,
    equivalence: Any = None,
    expected: ChatAggregateExpectation | None = None,
    validate_result: Any = None,
    clock_: FakeClock | None = None,
) -> Any:
    """One command through the seam, defaulting every part a test is not about.

    The expectation defaults to the revision the seeded conversation is at, because that
    is what nearly every test here starts from. A test that is about *not* stating one
    calls `execute_chat_command` directly.
    """
    resolved = authorize() if context is None else context
    return execute_chat_command(
        holder.connection,
        holder.identity,
        grant=issue(holder, resolved) if grant is None else grant,
        context=resolved,
        equivalence=equivalence_for() if equivalence is None else equivalence,
        command=command,
        validate_result=(
            is_governed_command_result if validate_result is None else validate_result
        ),
        clock=clock() if clock_ is None else clock_,
        expected=(
            ChatAggregateExpectation(
                conversation_id=CONVERSATION_ID,
                graph_revision=GRAPH_REVISION,
                latest_conversation_sequence=SEEDED_SEQUENCE,
            )
            if expected is None
            else expected
        ),
    )


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {table: m1.count(connection, table) for table in COUNTED_TABLES}


# --- 1: one transaction ---------------------------------------------------------


def test_w2s_first_execution_commits_chat_rows_outbox_and_settlement_together(
    seeded: m1.Owned,
) -> None:
    """The message, the head event, the outbox row and the four settlement rows.

    Counted as one set, because that is what "one transaction" means here. The Chat
    graph is then read back through W2-R's own readers, so what committed is a legal
    history rather than merely some rows.
    """
    before = counts(seeded.connection)
    command = AppendMessageCommand()

    outcome = run_command(seeded, command=command)

    assert command.calls == 1
    assert outcome.replayed is False
    assert outcome.result == {"commandId": outcome.claim_id, "status": "completed"}
    assert counts(seeded.connection) == {
        **before,
        "omnivia_application_audit_events": before["omnivia_application_audit_events"]
        + 1,
        "omnivia_idempotency_claims": before["omnivia_idempotency_claims"] + 1,
        "omnivia_idempotency_outcomes": before["omnivia_idempotency_outcomes"] + 1,
        s0.EXECUTIONS_TABLE: before[s0.EXECUTIONS_TABLE] + 1,
        "omnivia_chat_messages": before["omnivia_chat_messages"] + 1,
        "omnivia_chat_branch_head_events": before["omnivia_chat_branch_head_events"] + 1,
        "omnivia_chat_transactional_outbox": before["omnivia_chat_transactional_outbox"]
        + 1,
    }

    conversation = chat.read_conversation(
        seeded.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
    )
    assert conversation is not None
    assert conversation.graph_revision == GRAPH_REVISION
    assert conversation.latest_conversation_sequence == SEEDED_SEQUENCE + 1

    messages = chat.read_messages_by_conversation_sequence(
        seeded.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
    )
    assert [m.message_id for m in messages] == [ROOT_MESSAGE_ID, "msg-w2s-second"]

    events = chat.read_branch_head_events(
        seeded.connection, workspace_id=WORKSPACE_ID, branch_id=BRANCH_ID
    )
    assert [e.head_version for e in events] == [1, 2]
    # The head event and the settlement name the same transaction.
    assert events[-1].command_id == outcome.claim_id
    assert events[-1].graph_revision == GRAPH_REVISION

    entry = chat.read_outbox_event(
        seeded.connection, workspace_id=WORKSPACE_ID, domain_event_id="domain-event-w2s-2"
    )
    assert entry is not None
    assert entry.outbox_cursor == 1
    assert entry.delivery_state == "pending"
    assert entry.payload == {
        "conversationId": CONVERSATION_ID,
        "messageId": "msg-w2s-second",
    }

    settled = seeded.connection.execute(
        "SELECT c.claim_id, o.claim_id, e.claim_id, e.execution_kind "
        "FROM omnivia_idempotency_claims c "
        "JOIN omnivia_idempotency_outcomes o ON o.claim_id = c.claim_id "
        f"JOIN {s0.EXECUTIONS_TABLE} e ON e.claim_id = c.claim_id "
        "WHERE c.idempotency_key = ?",
        (IDEMPOTENCY_KEY,),
    ).fetchone()
    assert settled == (outcome.claim_id, outcome.claim_id, outcome.claim_id, "executed")
    assert seeded.connection.in_transaction is False
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


# --- 2: an equal duplicate ------------------------------------------------------


def test_w2s_equal_duplicate_replays_without_a_second_message_or_outbox_row(
    seeded: m1.Owned,
) -> None:
    """The stored answer, the same claim, no second Chat write and no second cursor.

    The replay is issued a *fresh* grant, as a redelivered request would be, and the
    expectation it states is the stale one from before the first execution --
    deliberately, because a replay answers for the command that already ran and must not
    be re-checked against a conversation that has since moved.
    """
    first = run_command(seeded, command=AppendMessageCommand())
    settled = counts(seeded.connection)

    second_command = AppendMessageCommand(
        message_id="msg-w2s-replay",
        head_event_id="head-event-w2s-replay",
        domain_event_id="domain-event-w2s-replay",
    )
    second = run_command(seeded, command=second_command)

    assert second_command.calls == 0
    assert second.replayed is True
    assert second.result == first.result
    assert second.claim_id == first.claim_id
    assert second.audit_ref == first.audit_ref
    assert [
        e.outbox_cursor
        for e in chat.read_outbox_events_since(
            seeded.connection, workspace_id=WORKSPACE_ID, after_cursor=0
        )
    ] == [1]
    # One new row in the whole database: the durable spend of the second grant, which is
    # what stops a fresh grant from later authorizing a write.
    assert counts(seeded.connection) == {
        **settled,
        s0.EXECUTIONS_TABLE: settled[s0.EXECUTIONS_TABLE] + 1,
    }
    assert [
        str(row[0])
        for row in seeded.connection.execute(
            f"SELECT execution_kind FROM {s0.EXECUTIONS_TABLE} ORDER BY recorded_at_us"
        ).fetchall()
    ] == ["executed", "replayed"]


# --- 3: the same key, a different request ---------------------------------------


def test_w2s_same_key_with_a_different_request_is_the_typed_conflict(
    seeded: m1.Owned,
) -> None:
    """A different canonical request under one key raises rather than writes."""
    run_command(seeded, command=AppendMessageCommand())
    settled = counts(seeded.connection)

    conflicting = authorize(operation_input=OTHER_INPUT)
    equivalence = equivalence_for(operation_input=OTHER_INPUT)
    command = AppendMessageCommand(
        message_id="msg-w2s-conflict",
        head_event_id="head-event-w2s-conflict",
        domain_event_id="domain-event-w2s-conflict",
    )

    with pytest.raises(MutationIdempotencyConflict) as raised:
        run_command(
            seeded,
            command=command,
            context=conflicting,
            grant=issue(seeded, conflicting, equivalence=equivalence),
            equivalence=equivalence,
        )

    assert command.calls == 0
    assert raised.value.audit_reference is not None
    assert counts(seeded.connection) == settled
    assert seeded.connection.in_transaction is False


# --- 4: an expectation the conversation is no longer at -------------------------


def test_w2s_stale_expected_revision_leaves_nothing_behind(seeded: m1.Owned) -> None:
    """The command never runs, and its audit event rolls back with the transaction."""
    run_command(seeded, command=AppendMessageCommand())
    settled = counts(seeded.connection)

    # The conversation is at sequence 2 now, and this command states the sequence it was
    # at before the first one committed.
    context = authorize(idempotency_key="w2s-idem-stale")
    equivalence = equivalence_for(idempotency_key="w2s-idem-stale")
    command = AppendMessageCommand(
        message_id="msg-w2s-stale",
        head_event_id="head-event-w2s-stale",
        domain_event_id="domain-event-w2s-stale",
    )

    with pytest.raises(ChatAggregateConflict) as raised:
        run_command(
            seeded,
            command=command,
            context=context,
            grant=issue(seeded, context, equivalence=equivalence),
            equivalence=equivalence,
        )

    assert command.calls == 0
    assert raised.value.code == ERROR_CODE_CONFLICT
    assert raised.value.retry_class == RETRY_CLASS_NON_RETRYABLE
    assert counts(seeded.connection) == settled
    assert seeded.connection.in_transaction is False


def test_w2s_an_expectation_for_an_absent_conversation_is_the_same_conflict(
    seeded: m1.Owned,
) -> None:
    """A conversation this workspace does not hold is not at the stated revision."""
    before = counts(seeded.connection)
    command = AppendMessageCommand()

    with pytest.raises(ChatAggregateConflict):
        run_command(
            seeded,
            command=command,
            expected=ChatAggregateExpectation(
                conversation_id="conv-w2s-does-not-exist",
                graph_revision=GRAPH_REVISION,
                latest_conversation_sequence=SEEDED_SEQUENCE,
            ),
        )

    assert command.calls == 0
    assert counts(seeded.connection) == before


def no_chat_writes(
    _writer: ChatWriter, settlement: MutationSettlementContext
) -> Mapping[str, Any]:
    """A command whose whole effect is its answer. Nothing Chat-shaped is written."""
    return {"commandId": settlement.claim_id, "status": "accepted"}


def test_w2s_an_unstated_expectation_reads_no_conversation(seeded: m1.Owned) -> None:
    """A command with nothing to expect is not held to one it never stated."""
    context = authorize()
    outcome = execute_chat_command(
        seeded.connection,
        seeded.identity,
        grant=issue(seeded, context),
        context=context,
        equivalence=equivalence_for(),
        command=no_chat_writes,
        validate_result=is_governed_command_result,
        clock=clock(),
        expected=None,
    )
    assert outcome.replayed is False
    assert outcome.result == {"commandId": outcome.claim_id, "status": "accepted"}
    assert m1.count(seeded.connection, "omnivia_chat_messages") == 1


# --- 5: every failure leaves nothing --------------------------------------------


def test_w2s_a_failing_command_rolls_back_what_it_already_wrote(
    seeded: m1.Owned,
) -> None:
    """The message, head event, branch move and outbox row do not survive."""
    before = counts(seeded.connection)
    command = AppendMessageCommand(fail=True)

    with pytest.raises(CommandFailure, match="after writing"):
        run_command(seeded, command=command)

    assert command.calls == 1
    assert counts(seeded.connection) == before
    conversation = chat.read_conversation(
        seeded.connection, workspace_id=WORKSPACE_ID, conversation_id=CONVERSATION_ID
    )
    assert conversation is not None
    assert conversation.latest_conversation_sequence == SEEDED_SEQUENCE
    assert seeded.connection.in_transaction is False


def test_w2s_a_refused_result_rolls_back_the_chat_writes(seeded: m1.Owned) -> None:
    """A result the contract will not govern takes the Chat and outbox rows with it.

    The validator is the module's own :func:`is_governed_command_result`, and the
    command returns an envelope the Chat contract refuses -- so this is the real
    validation path rather than a validator stubbed to say no.
    """
    before = counts(seeded.connection)

    class UngovernedResultCommand(AppendMessageCommand):
        def __call__(
            self, writer: ChatWriter, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            super().__call__(writer, settlement)
            return {"commandId": settlement.claim_id, "status": "not-a-status"}

    command = UngovernedResultCommand()

    with pytest.raises(MutationSeamFault):
        run_command(seeded, command=command)

    assert command.calls == 1
    assert counts(seeded.connection) == before
    assert seeded.connection.in_transaction is False


def test_w2s_an_expired_grant_never_reaches_the_command(seeded: m1.Owned) -> None:
    """A grant outside its window is refused before a transaction is opened."""
    before = counts(seeded.connection)
    context = authorize()
    grant = issue(seeded, context)
    command = AppendMessageCommand()

    with pytest.raises(MutationDenied):
        run_command(
            seeded,
            command=command,
            context=context,
            grant=grant,
            clock_=clock(monotonic=s0.MONOTONIC_BASE + 61.0),
        )

    assert command.calls == 0
    assert counts(seeded.connection) == before


def test_w2s_a_generation_that_moved_never_reaches_the_command(
    seeded: m1.Owned,
) -> None:
    """A grant pinning a fencing generation this instance no longer holds is refused."""
    before = counts(seeded.connection)
    live = read_guard(seeded.connection)
    assert live is not None
    context = authorize()
    command = AppendMessageCommand()

    with pytest.raises(StaleGeneration):
        run_command(
            seeded,
            command=command,
            context=context,
            grant=issue(
                seeded,
                context,
                guard=MutationGuard(
                    workspace_id=live.workspace_id,
                    service_instance_id=live.service_instance_id,
                    fencing_generation=live.fencing_generation + 1,
                ),
            ),
        )

    assert command.calls == 0
    assert counts(seeded.connection) == before
    assert seeded.connection.in_transaction is False


# --- 6: the workspace is the grant's --------------------------------------------


def test_w2s_the_command_writer_is_bound_to_the_granted_workspace(
    seeded: m1.Owned,
) -> None:
    """The writer names the grant's workspace, and a crossing attempt rolls back.

    Two halves. The workspace the command is handed is the server-issued one, and a
    command that reaches past its writer to name another workspace directly is refused
    by 0029's own guard -- so the binding is not merely a convention the writer follows.
    """
    command = AppendMessageCommand()
    run_command(seeded, command=command)
    assert command.workspaces == [WORKSPACE_ID]

    settled = counts(seeded.connection)
    context = authorize(idempotency_key="w2s-idem-crossing")
    equivalence = equivalence_for(idempotency_key="w2s-idem-crossing")

    def cross_the_workspace(
        writer: ChatWriter, settlement: MutationSettlementContext
    ) -> Mapping[str, Any]:
        assert writer.workspace_id == WORKSPACE_ID
        writer.connection.execute(
            "INSERT INTO omnivia_chat_transactional_outbox "
            "(workspace_id, outbox_cursor, domain_event_id, event_kind, "
            "conversation_id, generation_job_id, payload_json, delivery_state, "
            "delivery_attempts, next_delivery_after_us, delivered_at_us, "
            "retained_until_us, created_at_us) "
            "VALUES (?, 1, 'domain-event-w2s-crossed', 'chat.message.appended', "
            "NULL, NULL, '{}', 'pending', 0, NULL, NULL, NULL, ?)",
            (OTHER_WORKSPACE_ID, settlement.settled_at_us),
        )
        return {"commandId": settlement.claim_id, "status": "completed"}

    with pytest.raises(sqlite3.Error, match="unguarded INSERT"):
        execute_chat_command(
            seeded.connection,
            seeded.identity,
            grant=issue(seeded, context, equivalence=equivalence),
            context=context,
            equivalence=equivalence,
            command=cross_the_workspace,
            validate_result=is_governed_command_result,
            clock=clock(),
            expected=ChatAggregateExpectation(
                conversation_id=CONVERSATION_ID,
                graph_revision=GRAPH_REVISION,
                latest_conversation_sequence=SEEDED_SEQUENCE + 1,
            ),
        )

    assert counts(seeded.connection) == settled
    assert seeded.connection.in_transaction is False


def foreign_workspace_grant(holder: m1.Owned) -> MutationGrant:
    """A grant honestly issued for the *other* workspace, to present against this one."""
    live = read_guard(holder.connection)
    assert live is not None
    session = s0.session_for(
        ENTRY, workspaces=frozenset({WORKSPACE_ID, OTHER_WORKSPACE_ID})
    )
    binding = ServiceBinding(installation_id=s0.INSTALLATION_ID, workspace_id=None)
    metadata = s0.metadata_for(
        ENTRY, idempotency_key=IDEMPOTENCY_KEY, workspace_id=OTHER_WORKSPACE_ID
    )
    return issue_mutation_grant(
        authorize_application_request(
            RequestEnvelope(
                operation=OPERATION, metadata=metadata, input=dict(OPERATION_INPUT)
            ),
            session=session,
            binding=binding,
            supported_capabilities=s0.SUPPORTED,
        ),
        session=session,
        binding=binding,
        guard=MutationGuard(
            workspace_id=OTHER_WORKSPACE_ID,
            service_instance_id=live.service_instance_id,
            fencing_generation=live.fencing_generation,
        ),
        equivalence=idempotency_equivalence(
            OPERATION,
            metadata,
            dict(OPERATION_INPUT),
            principal_id=s0.PRINCIPAL,
            workspace_id=OTHER_WORKSPACE_ID,
        ),
        clock=clock(),
    )


def test_w2s_a_grant_for_another_workspace_is_refused(seeded: m1.Owned) -> None:
    """A grant the request's workspace is not covered by never opens a transaction."""
    before = counts(seeded.connection)
    command = AppendMessageCommand()
    foreign = foreign_workspace_grant(seeded)
    assert foreign.workspace_id == OTHER_WORKSPACE_ID

    with pytest.raises(MutationDenied):
        run_command(seeded, command=command, context=authorize(), grant=foreign)

    assert command.calls == 0
    assert counts(seeded.connection) == before


# --- 7: outbox cursors stay contiguous ------------------------------------------


def test_w2s_outbox_cursors_are_contiguous_and_a_replay_adds_none(
    seeded: m1.Owned,
) -> None:
    """Three commands allocate 1, 2, 3; the replay of the third allocates nothing.

    0029's own INSERT guard requires an outbox cursor contiguous from one per workspace,
    so a command that ran twice -- or a replay that ran the command again -- would either
    duplicate a cursor or skip one. Interleaving three commands and then replaying the
    last is what proves neither happens.
    """
    for index in (2, 3, 4):
        key = f"w2s-idem-{index}"
        context = authorize(idempotency_key=key)
        equivalence = equivalence_for(idempotency_key=key)
        run_command(
            seeded,
            command=AppendMessageCommand(
                message_id=f"msg-w2s-{index}",
                head_event_id=f"head-event-w2s-{index}",
                domain_event_id=f"domain-event-w2s-{index}",
            ),
            context=context,
            grant=issue(seeded, context, equivalence=equivalence),
            equivalence=equivalence,
            expected=ChatAggregateExpectation(
                conversation_id=CONVERSATION_ID,
                graph_revision=GRAPH_REVISION,
                latest_conversation_sequence=index - 1,
            ),
        )

    entries = chat.read_outbox_events_since(
        seeded.connection, workspace_id=WORKSPACE_ID, after_cursor=0
    )
    assert [e.outbox_cursor for e in entries] == [1, 2, 3]
    assert [e.domain_event_id for e in entries] == [
        "domain-event-w2s-2",
        "domain-event-w2s-3",
        "domain-event-w2s-4",
    ]

    replayed_context = authorize(idempotency_key="w2s-idem-4")
    replayed_equivalence = equivalence_for(idempotency_key="w2s-idem-4")
    replay_command = AppendMessageCommand(
        message_id="msg-w2s-4-again",
        head_event_id="head-event-w2s-4-again",
        domain_event_id="domain-event-w2s-4-again",
    )
    replayed = run_command(
        seeded,
        command=replay_command,
        context=replayed_context,
        grant=issue(seeded, replayed_context, equivalence=replayed_equivalence),
        equivalence=replayed_equivalence,
        expected=ChatAggregateExpectation(
            conversation_id=CONVERSATION_ID,
            graph_revision=GRAPH_REVISION,
            latest_conversation_sequence=3,
        ),
    )

    assert replayed.replayed is True
    assert replay_command.calls == 0
    assert [
        e.outbox_cursor
        for e in chat.read_outbox_events_since(
            seeded.connection, workspace_id=WORKSPACE_ID, after_cursor=0
        )
    ] == [1, 2, 3]
    events = chat.read_branch_head_events(
        seeded.connection, workspace_id=WORKSPACE_ID, branch_id=BRANCH_ID
    )
    assert [e.head_version for e in events] == [1, 2, 3, 4]
    assert {e.graph_revision for e in events} == {GRAPH_REVISION}
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


# --- 8: the result validation helper --------------------------------------------


def test_w2s_result_validation_accepts_a_governed_envelope_and_rejects_the_rest() -> (
    None
):
    """Strict `CommandResultEnvelope`, and a boolean that leaks nothing about a refusal."""
    assert is_governed_command_result({"commandId": "cmd-w2s-1", "status": "completed"})
    assert is_governed_command_result(
        {
            "commandId": "cmd-w2s-2",
            "status": "rejected",
            "error": {
                "code": "stale_expected_version",
                "message": "the conversation moved",
            },
        }
    )
    # An error code outside the contract's own closed vocabulary.
    assert not is_governed_command_result(
        {
            "commandId": "cmd-w2s-2b",
            "status": "rejected",
            "error": {"code": "not-a-code", "message": "the conversation moved"},
        }
    )
    # A status outside the closed vocabulary, an unknown top-level field, a missing
    # required field, and an envelope whose status forbids the error it carries.
    assert not is_governed_command_result(
        {"commandId": "cmd-w2s-3", "status": "not-a-status"}
    )
    assert not is_governed_command_result(
        {"commandId": "cmd-w2s-4", "status": "completed", "unknownField": 1}
    )
    assert not is_governed_command_result({"status": "completed"})
    assert not is_governed_command_result(
        {
            "commandId": "cmd-w2s-5",
            "status": "completed",
            "error": {"code": "stale_expected_version", "message": "not allowed here"},
        }
    )


# --- 9: no public wire surface ---------------------------------------------------


def test_w2s_adds_no_public_wire_surface() -> None:
    """No catalogue operation registered, no contract moved, only W2-S symbols exported.

    W2-S composes two existing seams and must not have grown a third: the Application
    Contract catalogue is read from and never added to, this module names no operation
    of its own, and the Chat wire contract is untouched.
    """
    import omnivia_core_runtime.service.chat_command as module

    from omnivia_core.chat_contract.v1 import CONTRACT_VERSION as CHAT_CONTRACT_VERSION

    assert CONTRACT_VERSION == "1.3"
    assert CHAT_CONTRACT_VERSION == "1.0.0-rc.1"
    named = {entry.name for entry in OPERATION_CATALOGUE}
    assert not named & {
        value for value in vars(module).values() if isinstance(value, str)
    }
    assert set(module.__all__) == {
        "ChatAggregateConflict",
        "ChatAggregateExpectation",
        "ChatCommand",
        "execute_chat_command",
        "is_governed_command_result",
    }
