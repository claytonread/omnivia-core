"""`document.original.relink` through the governed application dispatcher."""

from __future__ import annotations

from typing import Any

import pytest
from omnivia_core_runtime.service import document_intake
from omnivia_core_runtime.service.handlers import document_capture as document_handlers
from omnivia_core_runtime.service.handlers.document_capture import (
    DOCUMENT_ORIGINAL_DISCONNECT_OPERATION,
    DOCUMENT_ORIGINAL_RELINK_OPERATION,
)
from omnivia_core_runtime.storage import document_intake as store
from test_document_capture_vertical import captured, payload
from test_document_capture_vertical import request as capture_request
from test_evidence_capture_vertical import (
    AUDIT,
    Served,
    _metadata,
    build_dispatcher,
    count,
    rows,
)

from omnivia_core.contracts.v1 import (
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ErrorResponseEnvelope,
    RequestEnvelope,
    SuccessResponseEnvelope,
    decode_request,
    encode_request,
    get_operation_metadata,
)
from omnivia_core.contracts.v1.generated import (
    DocumentCaptureResult,
    DocumentOriginalRelinkResult,
)

pytest_plugins = ("test_evidence_capture_vertical",)

WORKSPACE = "ws-capture-0001"
BINDINGS = "omnivia_document_original_bindings"
CAPTURES = "omnivia_document_captures"


def relink_request(
    *,
    request_id: str,
    key: str = "idem-relink-1",
    body: dict[str, Any] | None = None,
) -> RequestEnvelope:
    entry = get_operation_metadata(DOCUMENT_ORIGINAL_RELINK_OPERATION)
    envelope = RequestEnvelope(
        operation=DOCUMENT_ORIGINAL_RELINK_OPERATION,
        metadata=_metadata(entry, request_id=request_id, key=key),
        input=body
        or {
            "document_id": "doc-native-1",
            "owner_ref": "access-native-1",
            "binding_generation": 2,
        },
    )
    # Every transport hands the handler a decoded, read-only mapping.
    return decode_request(encode_request(envelope))


def disconnect_request(*, request_id: str) -> RequestEnvelope:
    entry = get_operation_metadata(DOCUMENT_ORIGINAL_DISCONNECT_OPERATION)
    envelope = RequestEnvelope(
        operation=DOCUMENT_ORIGINAL_DISCONNECT_OPERATION,
        metadata=_metadata(entry, request_id=request_id, key="idem-disconnect-1"),
        input={
            "document_id": "doc-native-1",
            "owner_ref": "access-native-1",
            "route_kind": "direct_file",
        },
    )
    return decode_request(encode_request(envelope))


def body(
    generation: Any,
    *,
    document_id: str = "doc-native-1",
    owner_ref: str = "access-native-1",
) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "owner_ref": owner_ref,
        "binding_generation": generation,
    }


def binding(
    owned: Served, owner_ref: str = "access-native-1"
) -> store.StoredBinding | None:
    return store.read_binding(
        owned.connection, workspace_id=WORKSPACE, owner_ref=owner_ref
    )


def succeeded(response: Any) -> DocumentOriginalRelinkResult:
    assert isinstance(response, SuccessResponseEnvelope), response
    return DocumentOriginalRelinkResult.from_wire(response.result)


def capture_at(
    dispatcher: Any, generation: int, *, request_id: str, base: str | None
) -> DocumentCaptureResult:
    """A refresh (or, with no base, a first capture) claiming `generation` of the direct-file route."""
    entry = get_operation_metadata("document.capture")
    wire = payload(f"native document body, generation {generation}".encode())
    wire["original_binding"]["generation"] = generation
    if base is not None:
        wire.update(
            {
                "intent_kind": store.INTENT_REFRESH,
                "base": {"kind": "existing", "capture_id": base},
            }
        )
    response = dispatcher.dispatch(
        RequestEnvelope(
            operation="document.capture",
            metadata=_metadata(entry, request_id=request_id, key=f"idem-{request_id}"),
            input=wire,
        )
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    return DocumentCaptureResult.from_wire(response.result)


def reasons(owned: Served) -> list[str]:
    return [
        row[0]
        for row in rows(
            owned,
            f"SELECT disposition_reason FROM {CAPTURES} ORDER BY capture_sequence",
        )
    ]


def test_relink_of_a_connected_binding_moves_it_to_the_new_generation_and_replays(
    owned: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher = build_dispatcher(owned, tag="relink-linked")
    source = captured(dispatcher)
    before = binding(owned)
    assert before is not None and before.binding_generation == 1

    first = dispatcher.dispatch(relink_request(request_id="req-relink-1"))

    result = succeeded(first)
    assert result.document_id == source.document_id
    assert result.owner_ref == "access-native-1"
    assert result.binding_generation == 2
    assert result.connection_state == store.BINDING_CONNECTED
    after = binding(owned)
    assert after is not None
    assert (after.binding_generation, after.connection_state) == (
        2,
        store.BINDING_CONNECTED,
    )
    assert count(owned, BINDINGS) == 1
    assert count(owned, CAPTURES) == 1
    audits = count(owned, AUDIT)
    outcomes = count(owned, "omnivia_idempotency_outcomes")

    def must_not_write(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an exact replay re-ran the binding write")

    monkeypatch.setattr(
        document_handlers, "relink_original_in_transaction", must_not_write
    )
    replay = dispatcher.dispatch(relink_request(request_id="req-relink-2"))

    assert isinstance(first, SuccessResponseEnvelope) and isinstance(
        replay, SuccessResponseEnvelope
    )
    assert replay.result == first.result
    assert replay.metadata.audit_reference == first.metadata.audit_reference
    assert binding(owned) == after
    assert count(owned, AUDIT) == audits
    assert count(owned, "omnivia_idempotency_outcomes") == outcomes


def test_an_older_generation_capture_after_a_relink_is_history_and_the_new_one_is_head(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="relink-capture")
    first = captured(dispatcher)
    assert first.capture_disposition == store.DISPOSITION_HEAD_ELIGIBLE
    succeeded(dispatcher.dispatch(relink_request(request_id="req-relink-1")))
    moved = binding(owned)

    stale = capture_at(dispatcher, 1, request_id="req-stale", base=first.capture_id)

    assert stale.capture_disposition == store.DISPOSITION_HISTORICAL_ONLY
    assert not stale.became_head
    assert reasons(owned)[-1] == store.REASON_SUPERSEDED_GENERATION
    assert binding(owned) == moved

    current = capture_at(dispatcher, 2, request_id="req-current", base=first.capture_id)

    assert current.capture_disposition == store.DISPOSITION_HEAD_ELIGIBLE
    assert current.became_head
    assert binding(owned) == moved


def test_a_relink_before_any_capture_records_the_route_and_a_later_old_capture_is_history(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="relink-first")

    result = succeeded(dispatcher.dispatch(relink_request(request_id="req-relink-1")))

    assert result.binding_generation == 2
    assert result.connection_state == store.BINDING_CONNECTED
    recorded = binding(owned)
    assert recorded is not None
    assert (recorded.binding_generation, recorded.route_kind) == (
        2,
        store.ROUTE_DIRECT_FILE,
    )
    assert count(owned, CAPTURES) == 0

    late = dispatcher.dispatch(capture_request(request_id="req-late-capture"))
    assert isinstance(late, SuccessResponseEnvelope), late
    capture = DocumentCaptureResult.from_wire(late.result)
    assert capture.capture_disposition == store.DISPOSITION_HISTORICAL_ONLY
    assert not capture.became_head
    assert reasons(owned) == [store.REASON_SUPERSEDED_GENERATION]
    assert binding(owned) == recorded


def test_a_relink_after_a_disconnect_reconnects_one_generation_later(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="relink-after-disconnect")
    first = captured(dispatcher)
    assert isinstance(
        dispatcher.dispatch(disconnect_request(request_id="req-disconnect-1")),
        SuccessResponseEnvelope,
    )
    tombstone = binding(owned)
    assert (
        tombstone is not None
        and tombstone.connection_state == store.BINDING_DISCONNECTED
    )

    result = succeeded(dispatcher.dispatch(relink_request(request_id="req-relink-1")))

    assert (result.binding_generation, result.connection_state) == (
        2,
        store.BINDING_CONNECTED,
    )
    relinked = binding(owned)
    assert relinked is not None
    assert (relinked.binding_generation, relinked.connection_state) == (
        2,
        store.BINDING_CONNECTED,
    )
    # The tombstoned generation stays historical; the relinked one is the live route.
    assert (
        capture_at(
            dispatcher, 1, request_id="req-stale", base=first.capture_id
        ).became_head
        is False
    )
    assert capture_at(
        dispatcher, 2, request_id="req-current", base=first.capture_id
    ).became_head


def test_a_relink_several_generations_ahead_cycles_to_exactly_that_generation(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="relink-advance")
    captured(dispatcher)

    result = succeeded(
        dispatcher.dispatch(relink_request(request_id="req-relink-1", body=body(5)))
    )

    assert result.binding_generation == 5
    stored = binding(owned)
    assert stored is not None
    assert (stored.binding_generation, stored.connection_state) == (
        5,
        store.BINDING_CONNECTED,
    )
    assert count(owned, BINDINGS) == 1


def test_a_generation_equal_to_the_stored_one_is_a_conflict(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="relink-stale")
    captured(dispatcher)
    succeeded(
        dispatcher.dispatch(
            relink_request(request_id="req-relink-1", key="idem-relink-first")
        )
    )
    stored = binding(owned)
    audits = count(owned, AUDIT)

    response = dispatcher.dispatch(
        relink_request(request_id="req-stale", key="idem-relink-stale")
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert binding(owned) == stored
    assert count(owned, AUDIT) == audits


def test_an_advance_beyond_sixteen_generations_is_a_conflict_and_writes_nothing(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="relink-too-far")
    captured(dispatcher)
    stored = binding(owned)
    assert stored is not None

    limit = stored.binding_generation + document_intake._MAX_RELINK_ADVANCE
    ok = dispatcher.dispatch(
        relink_request(request_id="req-ok", key="idem-ok", body=body(limit))
    )
    assert succeeded(ok).binding_generation == limit

    too_far = dispatcher.dispatch(
        relink_request(
            request_id="req-far",
            key="idem-far",
            body=body(limit + document_intake._MAX_RELINK_ADVANCE + 1),
        )
    )

    assert isinstance(too_far, ErrorResponseEnvelope), too_far
    assert too_far.error.code == "conflict"
    after = binding(owned)
    assert after is not None and after.binding_generation == limit


def test_an_unseen_route_too_far_ahead_is_a_conflict_that_creates_nothing(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="relink-first-too-far")

    response = dispatcher.dispatch(
        relink_request(
            request_id="req-far", body=body(2 + document_intake._MAX_RELINK_ADVANCE)
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert count(owned, BINDINGS) == 0
    assert count(owned, "omnivia_documents") == 0


def test_another_documents_binding_is_a_conflict(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="relink-conflict")
    captured(dispatcher)
    stored = binding(owned)

    wrong_document = dispatcher.dispatch(
        relink_request(
            request_id="req-wrong-doc",
            key="idem-wrong-doc",
            body=body(2, document_id="doc-other"),
        )
    )

    assert isinstance(wrong_document, ErrorResponseEnvelope), wrong_document
    assert wrong_document.error.code == "conflict"
    assert binding(owned) == stored
    assert count(owned, BINDINGS) == 1
    assert (
        store.read_head(
            owned.connection, workspace_id=WORKSPACE, document_id="doc-other"
        )
        is None
    )


def test_a_root_member_binding_is_a_conflict(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="relink-root-member")
    entry = get_operation_metadata("document.capture")
    wire = payload()
    wire["original_binding"] = {
        "owner_ref": "access-native-1",
        "generation": 1,
        "route_kind": "root_member",
        "root_ref": "root-1",
        "member_ref": "member-1",
    }
    seeded = dispatcher.dispatch(
        RequestEnvelope(
            operation="document.capture",
            metadata=_metadata(
                entry, request_id="req-root-member", key="idem-root-member"
            ),
            input=wire,
        )
    )
    assert isinstance(seeded, SuccessResponseEnvelope), seeded
    stored = binding(owned)
    assert stored is not None and stored.route_kind == store.ROUTE_ROOT_MEMBER

    response = dispatcher.dispatch(relink_request(request_id="req-relink-1"))

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert binding(owned) == stored


@pytest.mark.parametrize(
    "wire",
    (
        {"document_id": "doc-native-1", "owner_ref": "access-native-1"},
        {"document_id": "doc-native-1", "binding_generation": 2},
        {
            "document_id": "doc-native-1",
            "owner_ref": "access-native-1",
            "binding_generation": 2,
            "route_kind": "direct_file",
        },
        {
            "document_id": "doc-native-1",
            "owner_ref": "access-native-1",
            "binding_generation": 2,
            "path": "/tmp/x",
        },
        body(1),
        body(0),
        body(2**63),
        body("2"),
        body(True),
        body(2, document_id="doc native"),
    ),
)
def test_an_invalid_payload_is_refused_and_writes_nothing(
    owned: Served, wire: dict[str, Any]
) -> None:
    dispatcher = build_dispatcher(owned, tag="relink-invalid")

    response = dispatcher.dispatch(relink_request(request_id="req-invalid", body=wire))

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "invalid_request"
    assert count(owned, BINDINGS) == 0
    assert count(owned, "omnivia_documents") == 0
    assert rows(owned, "SELECT 1 FROM omnivia_idempotency_outcomes") == []


def test_the_same_key_for_a_different_body_is_an_idempotency_conflict(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="relink-key-reuse")
    captured(dispatcher)
    first = dispatcher.dispatch(relink_request(request_id="req-relink-1"))
    assert isinstance(first, SuccessResponseEnvelope), first

    conflict = dispatcher.dispatch(
        relink_request(
            request_id="req-relink-2", body=body(2, owner_ref="access-native-2")
        )
    )

    assert isinstance(conflict, ErrorResponseEnvelope), conflict
    assert conflict.error.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
    assert binding(owned, "access-native-2") is None
    assert count(owned, BINDINGS) == 1


def test_a_relink_after_one_capture_returns_that_capture_as_the_current_one(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="relink-head")
    first = captured(dispatcher)

    result = succeeded(dispatcher.dispatch(relink_request(request_id="req-relink-1")))

    assert result.current_capture_id == first.capture_id


def test_a_relink_of_a_document_with_no_capture_omits_the_current_capture(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="relink-no-head")

    response = dispatcher.dispatch(relink_request(request_id="req-relink-1"))

    assert isinstance(response, SuccessResponseEnvelope), response
    assert "current_capture_id" not in response.result
    assert succeeded(response).current_capture_id is None


def test_a_relink_reports_the_head_capture_not_the_first_one(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="relink-later-head")
    first = captured(dispatcher)
    refreshed = capture_at(
        dispatcher, 1, request_id="req-refresh", base=first.capture_id
    )
    assert refreshed.became_head and refreshed.capture_id != first.capture_id

    result = succeeded(dispatcher.dispatch(relink_request(request_id="req-relink-1")))

    assert result.current_capture_id == refreshed.capture_id


def test_a_replay_returns_the_stored_current_capture_not_a_later_head(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="relink-replay-head")
    first = captured(dispatcher)
    original = dispatcher.dispatch(relink_request(request_id="req-relink-1"))
    assert succeeded(original).current_capture_id == first.capture_id

    later = capture_at(dispatcher, 2, request_id="req-after", base=first.capture_id)
    assert later.became_head and later.capture_id != first.capture_id
    replay = dispatcher.dispatch(relink_request(request_id="req-relink-2"))

    assert isinstance(original, SuccessResponseEnvelope) and isinstance(
        replay, SuccessResponseEnvelope
    )
    assert replay.result == original.result
    assert succeeded(replay).current_capture_id == first.capture_id
