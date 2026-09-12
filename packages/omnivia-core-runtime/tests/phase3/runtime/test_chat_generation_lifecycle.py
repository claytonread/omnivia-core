"""W3-M4 slice 1 acceptance for `service/chat_generation.py`.

The repository tests hold `storage/chat.py` to what 0029's guarded triggers already
enforce row by row. These hold the generation service to the things a row-oriented
repository cannot state: that one queued submission becomes exactly one durable job,
attempt and event stream and does so idempotently; that an already-normalized F2a
provider trace folds into the five durable generation event types in contiguous
order; that a redelivered provider event is a replay rather than a second write,
while the same provider event id carrying a different durable fact is refused; that
a stated sequence the job is not at, an append after a terminal event and a job in a
terminal state are all refused without changing a row; that a cursor replay returns
only the suffix and answers a stale, malformed or foreign cursor with the contract's
own resnapshot reason instead of a fabricated stream; that the whole history is
readable and continuable after the SQLite connection is closed and reopened, because
durable rows are the only authority; and that nothing a provider event carries
beyond the contract's own closed vocabularies reaches a durable payload.

`graph_revision` is held at `1` for the life of every test, for the reason
`test_chat_repository.py` and `test_chat_command_transaction.py` both state: durable
branch head events cite it through a non-deferrable foreign key into the
conversation's single row, so advancing it would orphan them. This lane does not
touch the conversation graph at all -- the trigger and result messages are seeded,
never written by the service.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.service.chat_generation import (
    MAX_RESPONSE_TEXT_BYTES,
    ClaimedGeneration,
    DuplicateProviderEvent,
    GenerationConflict,
    GenerationNotFound,
    GenerationSequenceGap,
    GenerationTerminal,
    UnsupportedProviderEvent,
    append_provider_generation_event,
    append_provider_text_delta,
    claim_queued_generation,
    replay_generation_events,
)
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

WORKSPACE_ID = m1.WORKSPACE_ID
OTHER_WORKSPACE_ID = "ws-chat-m4-nope"
BASE_US = 2_500_000_000_000_000

CONVERSATION_ID = "conv-m4-01"
BRANCH_ID = "branch-m4-main"
ACTOR_ID = "actor-m4-human"
ROOT_MESSAGE_ID = "msg-m4-root"
TRIGGER_MESSAGE_ID = "msg-m4-trigger"
RESULT_MESSAGE_ID = "msg-m4-result"
QUEUE_ID = "queue-m4-1"
JOB_ID = "generation-job-m4-1"
ATTEMPT_ID = "generation-attempt-m4-1"
LEASE_OWNER = "worker-m4-1"
GRAPH_REVISION = 1

#: Every relation this lane could write, counted as one set: "nothing was mutated"
#: is a statement about the set, not about whichever table a test happened to check.
CHAT_TABLES = (
    "omnivia_chat_conversations",
    "omnivia_chat_messages",
    "omnivia_chat_queued_submissions",
    "omnivia_chat_generation_jobs",
    "omnivia_chat_generation_attempts",
    "omnivia_chat_generation_events",
    "omnivia_chat_generation_text_chunks",
    "omnivia_chat_transactional_outbox",
)


def digest(char: str) -> str:
    return "sha256:" + char * 64


# --- fixtures ---------------------------------------------------------------------


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def writer(holder: m1.Owned):
    return chat.chat_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def seed_user_message(
    w: chat.ChatWriter, message_id: str, sequence: int, parent: str | None
) -> None:
    w.append_message(
        message_id=message_id,
        conversation_id=CONVERSATION_ID,
        role="user",
        author_type="human",
        author_id=ACTOR_ID,
        parent_message_id=parent,
        conversation_sequence=sequence,
        schema_version=1,
        content_hash=digest(chr(ord("a") + sequence)),
        completion_status="complete",
        visibility="standard",
        created_on_branch_id=BRANCH_ID if parent is not None else None,
        created_at_us=BASE_US + sequence,
        committed_at_us=BASE_US + sequence,
    )


def commit_result_message(holder: m1.Owned, *, now_us: int = BASE_US + 25) -> str:
    """Commit the assistant message a succeeded generation names.

    Not this lane's work: 0029 requires an assistant message to name its generation
    job, and a message is immutable, so the result can only be written once the job
    exists -- by the separate command that commits it. The tests do it here for the
    same reason they seed the trigger message: the generation service reads these
    identities and never writes one.
    """
    with writer(holder) as w:
        w.append_message(
            message_id=RESULT_MESSAGE_ID,
            conversation_id=CONVERSATION_ID,
            role="assistant",
            author_type="provider",
            parent_message_id=TRIGGER_MESSAGE_ID,
            conversation_sequence=3,
            schema_version=1,
            content_hash=digest("d"),
            completion_status="complete",
            visibility="standard",
            created_on_branch_id=BRANCH_ID,
            generation_job_id=JOB_ID,
            created_at_us=now_us,
            committed_at_us=now_us,
        )
    return RESULT_MESSAGE_ID


@pytest.fixture
def seeded(owned: m1.Owned) -> m1.Owned:
    """The graph one generation runs over, and the queued submission that asks for it.

    Two messages -- the branch root and the user turn that triggers the generation --
    plus the branch and its head event, plus one `queued` submission. Everything the
    service is about to do starts from here; it appends no message of its own. The
    assistant result message is not seeded, because 0029 requires one to name its
    generation job and the job does not exist yet (see `commit_result_message`).
    """
    with writer(owned) as w:
        w.append_conversation(
            conversation_id=CONVERSATION_ID,
            state="active",
            graph_revision=GRAPH_REVISION,
            latest_conversation_sequence=0,
            schema_version=1,
            created_by_actor_id=ACTOR_ID,
            created_at_us=BASE_US,
            updated_at_us=BASE_US,
        )
        w.update_conversation(
            conversation_id=CONVERSATION_ID,
            expected_graph_revision=GRAPH_REVISION,
            graph_revision=GRAPH_REVISION,
            latest_conversation_sequence=3,
            state="active",
            updated_at_us=BASE_US + 1,
        )
        seed_user_message(w, ROOT_MESSAGE_ID, 1, None)
        w.append_branch(
            branch_id=BRANCH_ID,
            conversation_id=CONVERSATION_ID,
            origin_kind="original",
            initial_head_message_id=ROOT_MESSAGE_ID,
            created_by_actor_id=ACTOR_ID,
            created_at_us=BASE_US + 4,
            created_conversation_sequence=1,
            schema_version=1,
        )
        w.append_branch_head_event(
            event_id="head-event-m4-1",
            conversation_id=CONVERSATION_ID,
            branch_id=BRANCH_ID,
            head_version=1,
            previous_head_message_id=None,
            new_head_message_id=ROOT_MESSAGE_ID,
            cause="branch_created",
            command_id="command-m4-seed",
            graph_revision=GRAPH_REVISION,
            conversation_sequence=1,
            actor_id=ACTOR_ID,
            occurred_at_us=BASE_US + 4,
            schema_version=1,
        )
        seed_user_message(w, TRIGGER_MESSAGE_ID, 2, ROOT_MESSAGE_ID)
        w.append_queued_submission(
            queued_submission_id=QUEUE_ID,
            conversation_id=CONVERSATION_ID,
            actor_id=ACTOR_ID,
            queue_sequence=1,
            branch_id=BRANCH_ID,
            editable_parts=[{"partType": "text"}],
            references=[],
            idempotency_key="m4-idem-1",
            created_at_us=BASE_US + 10,
            updated_at_us=BASE_US + 10,
        )
    return owned


# --- the calls, with everything a test is not about defaulted -----------------------


def claim(holder: m1.Owned, *, now_us: int = BASE_US + 20, **overrides: Any) -> ClaimedGeneration:
    fields: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "fencing_generation": holder.generation,
        "queued_submission_id": QUEUE_ID,
        "generation_job_id": JOB_ID,
        "generation_attempt_id": ATTEMPT_ID,
        "trigger_message_id": TRIGGER_MESSAGE_ID,
        "lease_owner": LEASE_OWNER,
        "now_us": now_us,
    }
    fields.update(overrides)
    return claim_queued_generation(holder.connection, holder.identity, **fields)


def append(
    holder: m1.Owned,
    provider_event: Mapping[str, Any],
    *,
    now_us: int = BASE_US + 30,
    **overrides: Any,
) -> chat.GenerationEvent | None:
    fields: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "fencing_generation": holder.generation,
        "generation_job_id": JOB_ID,
        "generation_attempt_id": ATTEMPT_ID,
        "provider_event": provider_event,
        "now_us": now_us,
    }
    fields.update(overrides)
    return append_provider_generation_event(holder.connection, holder.identity, **fields)


def provider_event(event_type: str, **fields: Any) -> dict[str, Any]:
    """One F2a-normalized provider event wire, with the envelope every type carries."""
    wire: dict[str, Any] = {
        "invocationId": "provider-invocation-m4-1",
        "attemptId": "provider-attempt-m4-1",
        "ordinal": fields.pop("ordinal", 0),
        "eventType": event_type,
        "schemaVersion": 1,
        "occurredAt": "2049-04-01T00:00:00Z",
        "receivedAt": "2049-04-01T00:00:00Z",
    }
    wire.update(fields)
    return wire


STREAM_START = provider_event("stream-start", providerEventId="provider-event-m4-1")
TEXT_DELTA = provider_event(
    "text-delta", ordinal=1, providerEventId="provider-event-m4-2", partId="part-1", delta="hello"
)
FINISH = provider_event(
    "finish",
    ordinal=2,
    providerEventId="provider-event-m4-3",
    finishReason="stop",
    usage={"inputTokens": 1, "outputTokens": 1},
    responseMetadata={},
    cancellationState="not_cancelled",
)
ERROR = provider_event(
    "error",
    ordinal=2,
    providerEventId="provider-event-m4-9",
    errorCode="rate-limited",
    safeMessage="the provider rate limited this request",
    retryable=True,
    statusClass="rate_limited",
)


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {table: m1.count(connection, table) for table in CHAT_TABLES}


def event_types(holder: m1.Owned) -> list[str]:
    return [
        event.event_type
        for event in chat.read_generation_events(
            holder.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
        )
    ]


# --- 1: the claim ------------------------------------------------------------------


def test_claiming_a_queued_submission_opens_the_job_attempt_and_first_event(
    seeded: m1.Owned,
) -> None:
    """One submitted queue row, one running job under a lease, one running attempt.

    And exactly one durable event: `chat.generation.queued` at sequence one. The
    provider stream has not started yet, so `chat.generation.started` is not yet a
    true statement -- that event is what the provider's own `stream-start` produces.
    """
    claimed = claim(seeded)

    assert claimed.submission.state == "submitted"
    assert claimed.submission.submitted_generation_job_id == JOB_ID
    assert claimed.submission.submitted_message_id == TRIGGER_MESSAGE_ID
    assert claimed.generation_attempt_id == ATTEMPT_ID

    assert claimed.job.state == "running"
    assert claimed.job.lease_owner == LEASE_OWNER
    assert claimed.job.lease_epoch == 1
    assert claimed.job.current_attempt_id == ATTEMPT_ID
    assert claimed.job.last_event_sequence == 1
    assert claimed.job.graph_revision_observed == GRAPH_REVISION
    assert claimed.job.idempotency_key == "m4-idem-1"

    attempt = seeded.connection.execute(
        "SELECT attempt_number, state, ended_at_us FROM omnivia_chat_generation_attempts "
        "WHERE workspace_id = ? AND generation_attempt_id = ?",
        (WORKSPACE_ID, ATTEMPT_ID),
    ).fetchone()
    assert attempt == (1, "running", None)

    events = chat.read_generation_events(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert [(e.generation_event_sequence, e.event_type) for e in events] == [
        (1, "chat.generation.queued")
    ]
    # 0029 requires a queued event to name no attempt and no result message.
    assert events[0].generation_attempt_id is None
    assert events[0].result_message_id is None
    assert events[0].cursor == f"{JOB_ID}:000001"
    assert seeded.connection.in_transaction is False
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_repeated_claim_of_a_submitted_queue_row_writes_nothing(seeded: m1.Owned) -> None:
    """The same job, attempt and event stream come back; no second row anywhere."""
    first = claim(seeded)
    settled = counts(seeded.connection)

    second = claim(seeded, now_us=BASE_US + 999)

    assert second == first
    assert counts(seeded.connection) == settled


def test_a_submission_already_submitted_under_another_job_is_a_conflict(
    seeded: m1.Owned,
) -> None:
    """One queued submission never becomes two generation jobs."""
    claim(seeded)
    settled = counts(seeded.connection)

    with pytest.raises(GenerationConflict):
        claim(
            seeded,
            generation_job_id="generation-job-m4-2",
            generation_attempt_id="generation-attempt-m4-2",
        )

    assert counts(seeded.connection) == settled


def test_a_submission_this_workspace_does_not_hold_is_not_found(seeded: m1.Owned) -> None:
    with pytest.raises(GenerationNotFound):
        claim(seeded, queued_submission_id="queue-m4-does-not-exist")


# --- 2: a provider trace folds into ordered durable events --------------------------


def test_a_provider_trace_appends_ordered_durable_generation_events(
    seeded: m1.Owned,
) -> None:
    """`stream-start` starts it, a text delta is durable-silent, `finish` ends it.

    The delta is a legal part of the trace and carries no generation-lifecycle
    transition, so it writes nothing rather than being refused -- 0029 closes the
    durable event types to five, and a text delta is not one of them.
    """
    claim(seeded)
    commit_result_message(seeded)

    started = append(seeded, STREAM_START, now_us=BASE_US + 30)
    assert started is not None
    assert started.event_type == "chat.generation.started"
    assert started.generation_event_sequence == 2
    assert started.generation_attempt_id == ATTEMPT_ID
    assert started.provider_event_id == "provider-event-m4-1"

    assert append(seeded, TEXT_DELTA, now_us=BASE_US + 31) is None
    assert event_types(seeded) == ["chat.generation.queued", "chat.generation.started"]

    finished = append(
        seeded, FINISH, now_us=BASE_US + 32, result_message_id=RESULT_MESSAGE_ID
    )
    assert finished is not None
    assert finished.event_type == "chat.generation.succeeded"
    assert finished.generation_event_sequence == 3
    assert finished.result_message_id == RESULT_MESSAGE_ID

    job = chat.read_generation_job(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert job is not None
    assert job.state == "succeeded"
    assert job.result_message_id == RESULT_MESSAGE_ID
    assert job.last_event_sequence == 3
    assert job.finished_at_us == BASE_US + 32
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_an_error_event_fails_the_job_with_a_sanitised_code(seeded: m1.Owned) -> None:
    """A terminal failure carries the contract's own error code, and no result message."""
    claim(seeded)
    append(seeded, STREAM_START, now_us=BASE_US + 30)

    failed = append(seeded, ERROR, now_us=BASE_US + 31)
    assert failed is not None
    assert failed.event_type == "chat.generation.failed"
    assert failed.result_message_id is None

    job = chat.read_generation_job(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert job is not None
    assert job.state == "failed"
    assert job.sanitized_error_code == "rate-limited"
    assert job.sanitized_error_detail == "the provider rate limited this request"
    assert job.result_message_id is None


def test_a_cancelled_finish_cancels_the_job(seeded: m1.Owned) -> None:
    claim(seeded)
    cancelled = append(
        seeded,
        provider_event(
            "finish",
            ordinal=1,
            providerEventId="provider-event-m4-4",
            finishReason="cancelled",
            cancellationState="cancelled",
            cancellationInitiator="caller",
            usage={"inputTokens": 1, "outputTokens": 0},
            responseMetadata={},
        ),
        now_us=BASE_US + 31,
    )
    assert cancelled is not None
    assert cancelled.event_type == "chat.generation.cancelled"

    job = chat.read_generation_job(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert job is not None
    assert job.state == "cancelled"
    assert job.sanitized_error_code == "cancelled"


# --- 3: duplicates, gaps and terminal refusal ---------------------------------------


def test_a_redelivered_provider_event_returns_the_event_already_durable(
    seeded: m1.Owned,
) -> None:
    """The same provider event id carrying the same durable fact is a replay."""
    claim(seeded)
    first = append(seeded, STREAM_START, now_us=BASE_US + 30)
    settled = counts(seeded.connection)

    second = append(seeded, dict(STREAM_START), now_us=BASE_US + 99)

    assert second == first
    assert counts(seeded.connection) == settled
    assert event_types(seeded) == ["chat.generation.queued", "chat.generation.started"]


def test_a_provider_event_id_carrying_a_different_durable_event_is_refused(
    seeded: m1.Owned,
) -> None:
    """The same id, a different durable fact: refused, and nothing changes."""
    claim(seeded)
    commit_result_message(seeded)
    append(seeded, STREAM_START, now_us=BASE_US + 30)
    settled = counts(seeded.connection)

    with pytest.raises(DuplicateProviderEvent):
        append(
            seeded,
            provider_event(
                "finish",
                ordinal=5,
                providerEventId="provider-event-m4-1",
                finishReason="stop",
                usage={"inputTokens": 1, "outputTokens": 1},
                responseMetadata={},
                cancellationState="not_cancelled",
            ),
            now_us=BASE_US + 31,
            result_message_id=RESULT_MESSAGE_ID,
        )

    assert counts(seeded.connection) == settled
    job = chat.read_generation_job(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert job is not None
    assert job.state == "running"
    assert job.last_event_sequence == 2


def test_a_stated_sequence_the_job_is_not_at_is_refused(seeded: m1.Owned) -> None:
    """A caller whose view of the stream has a hole in it writes nothing."""
    claim(seeded)
    settled = counts(seeded.connection)

    with pytest.raises(GenerationSequenceGap):
        append(seeded, STREAM_START, now_us=BASE_US + 30, expected_sequence=5)

    assert counts(seeded.connection) == settled
    # The position it *is* at is accepted.
    assert append(seeded, STREAM_START, now_us=BASE_US + 30, expected_sequence=2) is not None


def test_nothing_appends_after_a_terminal_event(seeded: m1.Owned) -> None:
    """A terminal durable event and a terminal job both close the stream."""
    claim(seeded)
    commit_result_message(seeded)
    append(seeded, STREAM_START, now_us=BASE_US + 30)
    append(seeded, FINISH, now_us=BASE_US + 31, result_message_id=RESULT_MESSAGE_ID)
    settled = counts(seeded.connection)

    with pytest.raises(GenerationTerminal):
        append(
            seeded,
            provider_event("stream-start", ordinal=9, providerEventId="provider-event-m4-late"),
            now_us=BASE_US + 32,
        )

    assert counts(seeded.connection) == settled
    assert event_types(seeded) == [
        "chat.generation.queued",
        "chat.generation.started",
        "chat.generation.succeeded",
    ]


def test_a_provider_event_wire_this_version_cannot_read_is_refused(
    seeded: m1.Owned,
) -> None:
    """An unknown discriminator, a vocabulary outsider and a malformed field."""
    claim(seeded)
    settled = counts(seeded.connection)

    for wire in (
        provider_event("not-an-event-type"),
        {"eventType": "finish", "finishReason": "not-a-finish-reason"},
        {"eventType": "finish"},
        provider_event("stream-start", providerEventId="x" * 129),
        provider_event("stream-start", providerEventId="-not-a-durable-id"),
        provider_event("stream-start", providerEventId="not a durable id"),
        {"eventType": "error", "errorCode": "rate-limited", "safeMessage": "x", "retryable": "yes"},
        "not-a-mapping",
    ):
        with pytest.raises(UnsupportedProviderEvent):
            append(seeded, wire, now_us=BASE_US + 30)  # type: ignore[arg-type]

    assert counts(seeded.connection) == settled


def test_a_result_message_belongs_to_a_succeeded_event_and_no_other(
    seeded: m1.Owned,
) -> None:
    claim(seeded)
    commit_result_message(seeded)
    with pytest.raises(GenerationConflict):
        append(seeded, FINISH, now_us=BASE_US + 30)
    with pytest.raises(GenerationConflict):
        append(seeded, ERROR, now_us=BASE_US + 30, result_message_id=RESULT_MESSAGE_ID)


# --- 4: replay ----------------------------------------------------------------------


def test_cursor_replay_returns_only_the_suffix_after_the_cursor(seeded: m1.Owned) -> None:
    claim(seeded)
    commit_result_message(seeded)
    append(seeded, STREAM_START, now_us=BASE_US + 30)
    append(seeded, FINISH, now_us=BASE_US + 31, result_message_id=RESULT_MESSAGE_ID)

    whole = replay_generation_events(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert whole.requires_resnapshot is False
    assert [e.generation_event_sequence for e in whole.events] == [1, 2, 3]

    after_first = replay_generation_events(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=JOB_ID,
        after_cursor=whole.events[0].cursor,
    )
    assert [e.event_type for e in after_first.events] == [
        "chat.generation.started",
        "chat.generation.succeeded",
    ]

    after_last = replay_generation_events(
        seeded.connection,
        workspace_id=WORKSPACE_ID,
        generation_job_id=JOB_ID,
        after_cursor=whole.events[-1].cursor,
    )
    assert after_last.events == ()
    assert after_last.requires_resnapshot is False


def test_a_stale_malformed_or_foreign_cursor_requires_a_resnapshot(
    seeded: m1.Owned,
) -> None:
    """A cursor this job's own history cannot place never yields a guessed stream."""
    claim(seeded)

    def replay(**overrides: Any):
        return replay_generation_events(
            seeded.connection,
            workspace_id=WORKSPACE_ID,
            generation_job_id=JOB_ID,
            **overrides,
        )

    # Past the end of the durable history.
    stale = replay(after_cursor=f"{JOB_ID}:000009")
    assert stale.requires_resnapshot is True
    assert stale.reason == "cursor_unknown_or_expired"
    assert stale.events == ()

    # Not a cursor this version can parse at all.
    malformed = replay(after_cursor="not-a-cursor")
    assert malformed.reason == "cursor_unknown_or_expired"

    # A well-formed cursor for a different job: a reader on the wrong stream.
    foreign = replay(after_cursor="generation-job-m4-other:000001")
    assert foreign.requires_resnapshot is True
    assert foreign.reason == "unauthorized_cursor"

    # A job this workspace does not hold.
    other_workspace = replay_generation_events(
        seeded.connection, workspace_id=OTHER_WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert other_workspace.requires_resnapshot is True
    assert other_workspace.reason == "unauthorized_cursor"
    assert other_workspace.events == ()


# --- 5: durability across a restart --------------------------------------------------


def test_the_history_survives_closing_and_reopening_the_database(
    seeded: m1.Owned,
) -> None:
    """Close SQLite mid-generation, reopen it, and continue from the durable rows.

    The service holds nothing between calls, so the reopened process reads the same
    job, the same event stream and the same cursors, and the append it makes next
    lands at the sequence the durable job says is next -- not at one a lost in-memory
    counter would have guessed.
    """
    claim(seeded)
    commit_result_message(seeded)
    append(seeded, STREAM_START, now_us=BASE_US + 30)
    before = replay_generation_events(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    path = seeded.path
    seeded.connection.close()

    reopened = m1.take_ownership(path)
    try:
        after = replay_generation_events(
            reopened.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
        )
        assert after == before
        assert [(e.generation_event_sequence, e.event_type, e.cursor) for e in after.events] == [
            (1, "chat.generation.queued", f"{JOB_ID}:000001"),
            (2, "chat.generation.started", f"{JOB_ID}:000002"),
        ]

        job = chat.read_generation_job(
            reopened.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
        )
        assert job is not None
        assert job.state == "running"
        assert job.last_event_sequence == 2

        # The claim is still idempotent across the restart, and the stream continues.
        assert claim(reopened, now_us=BASE_US + 40).job == job
        finished = append(
            reopened, FINISH, now_us=BASE_US + 41, result_message_id=RESULT_MESSAGE_ID
        )
        assert finished is not None
        assert finished.generation_event_sequence == 3
    finally:
        reopened.connection.close()


# --- 6: nothing provider-shaped reaches a durable payload -----------------------------


def test_no_raw_provider_content_reaches_a_durable_event_payload(
    seeded: m1.Owned,
) -> None:
    """A wire carrying secrets writes a payload built only from the closed vocabularies.

    The durable payload is constructed field by field here, never copied from the
    input, so the credentials, headers, URL and raw bodies bolted onto this wire have
    no path into storage even though the wire itself carried them.
    """
    claim(seeded)
    commit_result_message(seeded)
    poisoned = provider_event(
        "finish",
        ordinal=2,
        providerEventId="provider-event-m4-3",
        finishReason="stop",
        usage={"inputTokens": 1, "outputTokens": 1},
        responseMetadata={},
        cancellationState="not_cancelled",
        authorization="Bearer sk-live-do-not-store-me",
        apiKey="sk-live-do-not-store-me",
        endpoint="https://api.example.invalid/v1/messages",
        rawResponseBody={"choices": [{"text": "raw provider body"}]},
        headers={"x-api-key": "sk-live-do-not-store-me"},
        exception="ProviderSDKError('boom')",
    )

    event = append(seeded, poisoned, now_us=BASE_US + 30, result_message_id=RESULT_MESSAGE_ID)
    assert event is not None
    assert dict(event.payload) == {
        "providerEventType": "finish",
        "providerEventId": "provider-event-m4-3",
        "finishReason": "stop",
    }

    stored = "\n".join(
        str(row[0])
        for row in seeded.connection.execute(
            "SELECT payload_json FROM omnivia_chat_generation_events WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchall()
    )
    for secret in ("sk-live", "Bearer", "api.example.invalid", "raw provider body", "SDKError"):
        assert secret not in stored

    job = chat.read_generation_job(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert job is not None
    assert job.sanitized_error_code is None
    assert job.sanitized_error_detail is None


def test_a_failed_jobs_detail_is_the_contracts_own_bounded_safe_message(
    seeded: m1.Owned,
) -> None:
    """`safeMessage` is the only free text stored, and it is bounded on the way in."""
    claim(seeded)
    long_message = "x" * 4000
    unsafe_message = "first\x00second" + long_message
    append(
        seeded,
        provider_event(
            "error",
            ordinal=1,
            providerEventId="provider-event-m4-long",
            errorCode="transport",
            safeMessage=unsafe_message,
            retryable=False,
            statusClass="server_error",
        ),
        now_us=BASE_US + 30,
    )

    job = chat.read_generation_job(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert job is not None
    assert "\x00" not in str(job.sanitized_error_detail)
    assert job.sanitized_error_detail == "firstsecond" + ("x" * 1013)


# --- 7: streamed text deltas ----------------------------------------------------------


def delta_wire(**fields: Any) -> dict[str, Any]:
    """One F2a `text-delta` wire, with the fields this lane reads defaulted."""
    wire: dict[str, Any] = {
        "ordinal": 1,
        "providerEventId": "provider-event-m4-d1",
        "partId": "part-1",
        "delta": "hello",
    }
    wire.update(fields)
    return provider_event("text-delta", **wire)


def append_text(
    holder: m1.Owned,
    provider_event_wire: Mapping[str, Any] | None = None,
    *,
    chunk_ordinal: int = 0,
    now_us: int = BASE_US + 40,
    **overrides: Any,
) -> chat.GenerationEvent:
    fields: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "fencing_generation": holder.generation,
        "generation_job_id": JOB_ID,
        "generation_attempt_id": ATTEMPT_ID,
        "provider_event": delta_wire() if provider_event_wire is None else provider_event_wire,
        "chunk_ordinal": chunk_ordinal,
        "now_us": now_us,
    }
    fields.update(overrides)
    return append_provider_text_delta(holder.connection, holder.identity, **fields)


def text_chunks(holder: m1.Owned) -> list[tuple[int, str, str | None]]:
    return [
        (chunk.chunk_ordinal, chunk.text_content, chunk.provider_event_id)
        for chunk in chat.read_generation_text_chunks(
            holder.connection,
            workspace_id=WORKSPACE_ID,
            generation_job_id=JOB_ID,
            generation_attempt_id=ATTEMPT_ID,
        )
    ]


def snapshot(holder: m1.Owned) -> tuple[Any, ...]:
    """Everything a text delta could touch: rows, chunks, the job and its projection.

    The job and the projection are compared whole rather than by state, so a write
    that only moved `version`, `updated_at_us` or the lease still shows up.
    """
    return (
        counts(holder.connection),
        text_chunks(holder),
        chat.read_generation_job(
            holder.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
        ),
        chat.read_generation_job_status_projection(
            holder.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
        ),
    )


def started(holder: m1.Owned) -> None:
    """Claim, and take the durable `chat.generation.started` a text delta requires."""
    claim(holder)
    append(holder, STREAM_START, now_us=BASE_US + 30)


def test_text_deltas_append_ordered_chunks_and_their_ordering_events(
    seeded: m1.Owned,
) -> None:
    """Two deltas: two chunks in ordinal order, two events, and no text in a payload."""
    started(seeded)

    first = append_text(seeded, delta_wire(delta="hel"), chunk_ordinal=0, now_us=BASE_US + 40)
    second = append_text(
        seeded,
        delta_wire(ordinal=2, providerEventId="provider-event-m4-d2", delta="lo"),
        chunk_ordinal=1,
        now_us=BASE_US + 41,
    )

    assert text_chunks(seeded) == [
        (0, "hel", "provider-event-m4-d1"),
        (1, "lo", "provider-event-m4-d2"),
    ]
    assert event_types(seeded) == [
        "chat.generation.queued",
        "chat.generation.started",
        "chat.generation.text_appended",
        "chat.generation.text_appended",
    ]
    assert dict(first.payload) == {
        "providerEventType": "text-delta",
        "providerEventId": "provider-event-m4-d1",
        "chunkOrdinal": 0,
    }
    assert dict(second.payload) == {
        "providerEventType": "text-delta",
        "providerEventId": "provider-event-m4-d2",
        "chunkOrdinal": 1,
    }
    events = chat.read_generation_events(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert [e.generation_event_sequence for e in events] == [1, 2, 3, 4]

    job = chat.read_generation_job(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=JOB_ID
    )
    assert job is not None
    assert job.state == "running"
    assert job.last_event_sequence == 4
    assert seeded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_redelivered_text_delta_replays_across_a_reopened_database(
    seeded: m1.Owned,
) -> None:
    """The exact same delta, after a restart, returns its event and writes nothing."""
    started(seeded)
    first = append_text(seeded)
    before = snapshot(seeded)
    path = seeded.path
    seeded.connection.close()

    reopened = m1.take_ownership(path)
    try:
        again = append_text(reopened, now_us=BASE_US + 99)
        assert again == first
        assert snapshot(reopened) == before
    finally:
        reopened.connection.close()


@pytest.mark.parametrize(
    ("wire", "chunk_ordinal"),
    [
        (delta_wire(delta="different"), 0),
        (delta_wire(), 1),
    ],
    ids=["changed-text", "changed-ordinal"],
)
def test_a_text_delta_id_carrying_a_different_durable_fact_is_refused(
    seeded: m1.Owned, wire: Mapping[str, Any], chunk_ordinal: int
) -> None:
    """The same provider event id, a different chunk: refused, and nothing changes."""
    started(seeded)
    append_text(seeded)
    before = snapshot(seeded)

    with pytest.raises(DuplicateProviderEvent):
        append_text(seeded, wire, chunk_ordinal=chunk_ordinal, now_us=BASE_US + 41)

    assert snapshot(seeded) == before


def test_provider_metadata_outside_the_durable_payload_is_still_a_replay(
    seeded: m1.Owned,
) -> None:
    """`partId` and the F2a envelope are not durable, so changing one is not a difference.

    The durable fact is the chunk and the three-field payload that orders it. A
    redelivery whose envelope differs carries the same durable fact, so it replays
    rather than being refused -- refusing it would make a legitimately re-emitted
    stream unresumable.
    """
    started(seeded)
    first = append_text(seeded)
    before = snapshot(seeded)

    again = append_text(
        seeded, delta_wire(ordinal=7, partId="part-2"), now_us=BASE_US + 41
    )

    assert again == first
    assert snapshot(seeded) == before


def claim_only(holder: m1.Owned) -> None:
    claim(holder)


def start_and_finish(holder: m1.Owned) -> None:
    started(holder)
    commit_result_message(holder)
    append(holder, FINISH, now_us=BASE_US + 31, result_message_id=RESULT_MESSAGE_ID)


def start_and_fill(holder: m1.Owned) -> None:
    started(holder)
    append_text(holder, delta_wire(delta="x" * MAX_RESPONSE_TEXT_BYTES), chunk_ordinal=0)


@pytest.mark.parametrize(
    ("prepare", "kwargs", "error"),
    [
        (started, {"provider_event_wire": delta_wire(delta="")}, UnsupportedProviderEvent),
        (started, {"provider_event_wire": delta_wire(delta="a\x00b")}, UnsupportedProviderEvent),
        (started, {"provider_event_wire": delta_wire(delta="a\ud800b")}, UnsupportedProviderEvent),
        (started, {"provider_event_wire": delta_wire(delta=7)}, UnsupportedProviderEvent),
        (started, {"chunk_ordinal": -1}, GenerationSequenceGap),
        (started, {"chunk_ordinal": 1_000_001}, GenerationSequenceGap),
        (started, {"chunk_ordinal": 5}, GenerationSequenceGap),
        (started, {"expected_sequence": 9}, GenerationSequenceGap),
        (claim_only, {}, GenerationConflict),
        (started, {"generation_attempt_id": "generation-attempt-m4-2"}, GenerationConflict),
        (start_and_finish, {}, GenerationTerminal),
        (
            start_and_fill,
            {
                "provider_event_wire": delta_wire(providerEventId="provider-event-m4-d2"),
                "chunk_ordinal": 1,
            },
            GenerationConflict,
        ),
    ],
    ids=[
        "empty-delta",
        "nul-delta",
        "surrogate-delta",
        "non-string-delta",
        "negative-ordinal",
        "ordinal-past-the-durable-bound",
        "ordinal-gap",
        "event-sequence-gap",
        "delta-before-started",
        "wrong-attempt",
        "terminal-attempt",
        "over-the-bounded-response-size",
    ],
)
def test_a_refused_text_delta_leaves_no_chunk_and_no_event(
    seeded: m1.Owned,
    prepare: Any,
    kwargs: dict[str, Any],
    error: type[Exception],
) -> None:
    """Every refusal is decided before the transaction: no orphan chunk, no event."""
    prepare(seeded)
    before = snapshot(seeded)
    appended = len([t for t in event_types(seeded) if t == "chat.generation.text_appended"])

    with pytest.raises(error):
        append_text(seeded, now_us=BASE_US + 50, **kwargs)

    assert snapshot(seeded) == before
    assert len([t for t in event_types(seeded) if t == "chat.generation.text_appended"]) == appended


@pytest.mark.parametrize(
    "faulting_write",
    ["append_generation_text_chunk", "append_generation_event"],
)
def test_a_fault_mid_transaction_rolls_the_whole_text_delta_back(
    seeded: m1.Owned, monkeypatch: pytest.MonkeyPatch, faulting_write: str
) -> None:
    """A chunk, its event and the job's advance are one fact or none of them.

    The fault is raised *after* the patched write returns, so the row really was
    issued into the transaction before the failure -- the rollback is what removes it,
    not a call that never happened.
    """
    started(seeded)
    before = snapshot(seeded)
    original = getattr(chat.ChatWriter, faulting_write)

    def boom(self: chat.ChatWriter, **fields: Any) -> None:
        original(self, **fields)
        raise RuntimeError("fault after the durable write was issued")

    monkeypatch.setattr(chat.ChatWriter, faulting_write, boom)

    with pytest.raises(RuntimeError):
        append_text(seeded, now_us=BASE_US + 50)

    monkeypatch.undo()
    assert snapshot(seeded) == before
    assert "chat.generation.text_appended" not in event_types(seeded)
    assert seeded.connection.in_transaction is False
