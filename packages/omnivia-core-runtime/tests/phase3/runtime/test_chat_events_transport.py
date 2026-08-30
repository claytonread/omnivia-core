"""The `chat.events` handler's exact Chat Contract v1 transport projection.

`test_chat_generation_lifecycle.py` holds the durable writes; these hold what the
handler publishes over them: that every `transport_events` item is a document the
Chat contract itself validates strictly, that the legacy `events` array is
untouched and still carries no generated text, that a cursor answers with the same
suffix in both arrays, and that a history the chunks do not corroborate fails
closed rather than emitting a guessed event.

The durable storage is real, seeded through the lifecycle module's own fixtures,
because a transport event that names a chunk is only true if that chunk is there.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from typing import Any

import pytest
import test_chat_generation_lifecycle as lc
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.identity import SystemClock
from omnivia_core_runtime.service.application import chat_family_session
from omnivia_core_runtime.service.authorization import ServiceBinding
from omnivia_core_runtime.service.chat_generation import GenerationReplay
from omnivia_core_runtime.service.handlers import chat as chat_handler_module
from omnivia_core_runtime.service.handlers.chat import (
    CHAT_EVENTS_OPERATION,
    ChatHandlers,
)
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage import chat
from omnivia_core_runtime.storage.memory import random_identifier

from omnivia_core.chat_contract.v1 import ChatEvent
from omnivia_core.contracts.v1 import (
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    get_operation_metadata,
)

# The lifecycle fixtures, registered in this module: the same seeded conversation,
# queued submission and ownership holder those tests write their durable rows into.
owned = lc.owned
seeded = lc.seeded

ENTRY = get_operation_metadata(CHAT_EVENTS_OPERATION)
WORKSPACE_ID = lc.WORKSPACE_ID
INSTALLATION_ID = "inst-chat-events"


class _FakeService:
    """Just what `ChatHandlers._authority()` reads: a connection and an identity."""

    def __init__(self, holder: lc.m1.Owned) -> None:
        self.connection = holder.connection
        self.identity = holder.identity


def _handlers(holder: lc.m1.Owned) -> ChatHandlers:
    return ChatHandlers(
        service=_FakeService(holder),
        session=chat_family_session(
            principal_id=s0.PRINCIPAL,
            installation_id=INSTALLATION_ID,
            workspace_id=WORKSPACE_ID,
        ),
        binding=ServiceBinding(installation_id=INSTALLATION_ID, workspace_id=WORKSPACE_ID),
        clock=SystemClock(),
        allocate_identifier=random_identifier,
    )


def _context(after_cursor: str | None = None) -> OperationContext:
    operation_input: dict[str, Any] = {"generation_job_id": lc.JOB_ID}
    if after_cursor is not None:
        operation_input["after_cursor"] = after_cursor
    return OperationContext(
        request=s0.envelope_for(
            ENTRY,
            operation_input=operation_input,
            request_id="req-chat-events-1",
            workspace_id=WORKSPACE_ID,
        ),
        principal=s0.PRINCIPAL,
        workspace_id=WORKSPACE_ID,
        granted_operations=frozenset({CHAT_EVENTS_OPERATION}),
    )


def _events(holder: lc.m1.Owned, after_cursor: str | None = None) -> Mapping[str, Any]:
    return _handlers(holder).chat_events(_context(after_cursor))


def _streamed(holder: lc.m1.Owned) -> None:
    """Queued, started, two text deltas and succeeded: the five durable positions."""
    lc.claim(holder)
    lc.commit_result_message(holder)
    lc.append(holder, lc.STREAM_START, now_us=lc.BASE_US + 30)
    lc.append_text(holder, lc.delta_wire(delta="hel"), chunk_ordinal=0, now_us=lc.BASE_US + 40)
    lc.append_text(
        holder,
        lc.delta_wire(ordinal=2, providerEventId="provider-event-m4-d2", delta="lo"),
        chunk_ordinal=1,
        now_us=lc.BASE_US + 41,
    )
    lc.append(
        holder, lc.FINISH, now_us=lc.BASE_US + 50, result_message_id=lc.RESULT_MESSAGE_ID
    )


def _replace(record: Any, **overrides: Any) -> Any:
    """One durable record with a field changed, to stage a history no writer allows."""
    return dataclasses.replace(record, **overrides)


def _replay(events: tuple[chat.GenerationEvent, ...]) -> GenerationReplay:
    return GenerationReplay(events=events)


def _cursors(holder: lc.m1.Owned) -> list[str]:
    return [
        event.cursor
        for event in chat.read_generation_events(
            holder.connection, workspace_id=WORKSPACE_ID, generation_job_id=lc.JOB_ID
        )
    ]


# --- 1: the exact transport stream ----------------------------------------------------


def test_the_five_durable_positions_project_to_five_exact_chat_events(
    seeded: lc.m1.Owned,
) -> None:
    _streamed(seeded)

    result = _events(seeded)
    transport = result["transport_events"]
    cursors = _cursors(seeded)

    assert [ChatEvent.from_wire(item, strict=True).event_type for item in transport] == [
        "chat.generation.queued",
        "chat.generation.started",
        "chat.generation.text_appended",
        "chat.generation.text_appended",
        "chat.generation.succeeded",
    ]
    assert [item["cursor"] for item in transport] == cursors
    assert [item["generationEventSequence"] for item in transport] == [1, 2, 3, 4, 5]
    for item in transport:
        assert item["workspaceId"] == WORKSPACE_ID
        assert item["conversationId"] == lc.CONVERSATION_ID
        assert item["branchId"] == lc.BRANCH_ID
        assert item["generationJobId"] == lc.JOB_ID
        assert item["triggerMessageId"] == lc.TRIGGER_MESSAGE_ID
        assert item["schemaVersion"] == 1
        assert item["occurredAt"].endswith("Z")

    queued, started, first, second, succeeded = transport
    # The queued event is written before any attempt exists, so it names none.
    assert "generationAttemptId" not in queued
    assert "resultMessageId" not in queued
    assert started["generationAttemptId"] == lc.ATTEMPT_ID
    assert "resultMessageId" not in started
    assert (first["chunkOrdinal"], first["textDelta"]) == (0, "hel")
    assert (second["chunkOrdinal"], second["textDelta"]) == (1, "lo")
    assert first["generationAttemptId"] == lc.ATTEMPT_ID
    assert succeeded["resultMessageId"] == lc.RESULT_MESSAGE_ID

    # Nothing of the provider's own trace crosses the seam.
    document = json.dumps(transport)
    assert "providerEvent" not in document
    assert "provider-event-m4-d1" not in document


def test_the_legacy_events_projection_is_unchanged_and_carries_no_text(
    seeded: lc.m1.Owned,
) -> None:
    _streamed(seeded)

    result = _events(seeded)

    assert [event["event_type"] for event in result["events"]] == [
        "chat.generation.queued",
        "chat.generation.started",
        "chat.generation.text_appended",
        "chat.generation.text_appended",
        "chat.generation.succeeded",
    ]
    assert [event["cursor"] for event in result["events"]] == _cursors(seeded)
    assert "textDelta" not in json.dumps(result["events"])
    assert "hel" not in json.dumps(result["events"])


# --- 2: cursors ------------------------------------------------------------------------


def test_a_cursor_returns_the_matching_suffix_in_both_arrays(seeded: lc.m1.Owned) -> None:
    _streamed(seeded)
    cursors = _cursors(seeded)

    result = _events(seeded, cursors[2])

    assert [event["cursor"] for event in result["events"]] == cursors[3:]
    assert [item["cursor"] for item in result["transport_events"]] == cursors[3:]
    assert [item["eventType"] for item in result["transport_events"]] == [
        "chat.generation.text_appended",
        "chat.generation.succeeded",
    ]
    assert result["transport_events"][0]["textDelta"] == "lo"


def test_the_cursor_of_the_last_event_returns_both_arrays_empty(
    seeded: lc.m1.Owned,
) -> None:
    _streamed(seeded)

    result = _events(seeded, _cursors(seeded)[-1])

    assert result["events"] == []
    assert result["transport_events"] == []
    assert result["requires_resnapshot"] is False


@pytest.mark.parametrize(
    ("cursor", "reason"),
    [
        ("not-a-cursor", "cursor_unknown_or_expired"),
        (f"{lc.JOB_ID}:000009", "cursor_unknown_or_expired"),
        ("generation-job-m4-other:000001", "unauthorized_cursor"),
    ],
)
def test_a_cursor_this_history_cannot_place_resnapshots_with_both_arrays_empty(
    seeded: lc.m1.Owned, cursor: str, reason: str
) -> None:
    _streamed(seeded)

    result = _events(seeded, cursor)

    assert result["requires_resnapshot"] is True
    assert result["resnapshot_reason"] == reason
    assert result["events"] == []
    assert result["transport_events"] == []


# --- 3: fail-closed --------------------------------------------------------------------


def _internal(raised: pytest.ExceptionInfo[OperationError]) -> None:
    assert raised.value.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE
    assert raised.value.message == (
        "this service instance is not serving authoritative chat storage"
    )


def test_a_missing_text_chunk_fails_closed(
    seeded: lc.m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0030 forbids DELETE, so the torn history is staged at the read instead."""
    _streamed(seeded)
    monkeypatch.setattr(
        chat_handler_module, "read_generation_text_chunks", lambda *a, **k: ()
    )

    with pytest.raises(OperationError) as raised:
        _events(seeded)
    _internal(raised)


def test_a_chunk_under_another_provider_event_fails_closed(
    seeded: lc.m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    _streamed(seeded)
    durable = chat.read_generation_text_chunks(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=lc.JOB_ID
    )
    mismatched = tuple(
        chunk if index else _replace(chunk, provider_event_id="provider-event-elsewhere")
        for index, chunk in enumerate(durable)
    )
    monkeypatch.setattr(
        chat_handler_module, "read_generation_text_chunks", lambda *a, **k: mismatched
    )

    with pytest.raises(OperationError) as raised:
        _events(seeded)
    _internal(raised)


@pytest.mark.parametrize("ordinal", [None, "0", True, 1.0])
def test_text_metadata_this_version_cannot_read_fails_closed(
    seeded: lc.m1.Owned, monkeypatch: pytest.MonkeyPatch, ordinal: Any
) -> None:
    _streamed(seeded)
    events = chat.read_generation_events(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=lc.JOB_ID
    )
    payload = dict(events[2].payload)
    if ordinal is None:
        payload.pop("chunkOrdinal")
    else:
        payload["chunkOrdinal"] = ordinal
    corrupt = tuple(
        _replace(event, payload=payload) if index == 2 else event
        for index, event in enumerate(events)
    )
    monkeypatch.setattr(
        chat_handler_module,
        "replay_generation_events",
        lambda *a, **k: _replay(corrupt),
    )

    with pytest.raises(OperationError) as raised:
        _events(seeded)
    _internal(raised)


def _with_third_event_payload(
    seeded: lc.m1.Owned, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    events = chat.read_generation_events(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=lc.JOB_ID
    )
    corrupt = tuple(
        _replace(event, payload=payload) if index == 2 else event
        for index, event in enumerate(events)
    )
    monkeypatch.setattr(
        chat_handler_module,
        "replay_generation_events",
        lambda *a, **k: _replay(corrupt),
    )


@pytest.mark.parametrize(
    "provider_event_id", ["provider-event-elsewhere", 1, True, {"id": "x"}, None]
)
def test_a_payload_provider_event_id_the_row_denies_fails_closed(
    seeded: lc.m1.Owned, monkeypatch: pytest.MonkeyPatch, provider_event_id: Any
) -> None:
    _streamed(seeded)
    events = chat.read_generation_events(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=lc.JOB_ID
    )
    payload = dict(events[2].payload) | {"providerEventId": provider_event_id}
    _with_third_event_payload(seeded, monkeypatch, payload)

    with pytest.raises(OperationError) as raised:
        _events(seeded)
    _internal(raised)


def test_a_payload_without_a_provider_event_id_still_projects(
    seeded: lc.m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Durable history written before the key carries it; that is not a tear."""
    _streamed(seeded)
    events = chat.read_generation_events(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=lc.JOB_ID
    )
    payload = dict(events[2].payload)
    payload.pop("providerEventId", None)
    _with_third_event_payload(seeded, monkeypatch, payload)

    transport = _events(seeded)["transport_events"]

    assert (transport[2]["chunkOrdinal"], transport[2]["textDelta"]) == (0, "hel")
    assert "providerEvent" not in json.dumps(transport)


def test_an_event_shape_the_chat_contract_refuses_fails_closed(
    seeded: lc.m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A succeeded event with no branch is a document `events.schema.json` rejects."""
    _streamed(seeded)
    events = chat.read_generation_events(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=lc.JOB_ID
    )
    corrupt = (_replace(events[0], branch_id="not a branch id"),) + events[1:]
    monkeypatch.setattr(
        chat_handler_module,
        "replay_generation_events",
        lambda *a, **k: _replay(corrupt),
    )

    with pytest.raises(OperationError) as raised:
        _events(seeded)
    _internal(raised)


# --- 4: the page bound -----------------------------------------------------------------


def test_one_page_is_the_first_thousand_and_the_last_cursor_continues(
    seeded: lc.m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1005 stored-like events, so the bound is exercised without 1001 provider chunks."""
    _streamed(seeded)
    template = chat.read_generation_events(
        seeded.connection, workspace_id=WORKSPACE_ID, generation_job_id=lc.JOB_ID
    )[1]
    stream = tuple(
        _replace(
            template,
            event_id=f"{lc.JOB_ID}.event.{sequence}",
            generation_event_sequence=sequence,
            cursor=f"{lc.JOB_ID}:{sequence:06d}",
        )
        for sequence in range(1, 1006)
    )

    def _suffix(*args: Any, after_cursor: str | None = None, **kwargs: Any) -> Any:
        start = 0 if after_cursor is None else int(after_cursor.rpartition(":")[2])
        return _replay(stream[start:])

    monkeypatch.setattr(chat_handler_module, "replay_generation_events", _suffix)

    page = _events(seeded)
    assert len(page["events"]) == 1000
    assert len(page["transport_events"]) == 1000
    assert page["transport_events"][-1]["cursor"] == page["events"][-1]["cursor"]

    rest = _events(seeded, page["events"][-1]["cursor"])
    assert len(rest["events"]) == 5
    assert len(rest["transport_events"]) == 5
    assert [item["generationEventSequence"] for item in rest["transport_events"]] == [
        1001,
        1002,
        1003,
        1004,
        1005,
    ]


