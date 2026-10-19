"""`document.original.disconnect` through the governed application dispatcher."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from omnivia_core_runtime.service.handlers import document_capture as document_handlers
from omnivia_core_runtime.service.handlers.document_capture import (
    DOCUMENT_ORIGINAL_DISCONNECT_OPERATION,
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
    DocumentOriginalDisconnectResult,
)

pytest_plugins = ("test_evidence_capture_vertical",)

WORKSPACE = "ws-capture-0001"
BINDINGS = "omnivia_document_original_bindings"


def disconnect_request(
    *,
    request_id: str,
    key: str = "idem-disconnect-1",
    body: dict[str, Any] | None = None,
) -> RequestEnvelope:
    entry = get_operation_metadata(DOCUMENT_ORIGINAL_DISCONNECT_OPERATION)
    envelope = RequestEnvelope(
        operation=DOCUMENT_ORIGINAL_DISCONNECT_OPERATION,
        metadata=_metadata(entry, request_id=request_id, key=key),
        input=body
        or {
            "document_id": "doc-native-1",
            "owner_ref": "access-native-1",
            "route_kind": "direct_file",
        },
    )
    # Every transport hands the handler a decoded, read-only mapping.
    return decode_request(encode_request(envelope))


def binding(owned: Served, owner_ref: str = "access-native-1") -> store.StoredBinding | None:
    return store.read_binding(owned.connection, workspace_id=WORKSPACE, owner_ref=owner_ref)


def succeeded(response: Any) -> DocumentOriginalDisconnectResult:
    assert isinstance(response, SuccessResponseEnvelope), response
    return DocumentOriginalDisconnectResult.from_wire(response.result)


def test_disconnect_after_a_linked_capture_tombstones_the_binding(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="disconnect-linked")
    source = captured(dispatcher)
    before = binding(owned)
    assert before is not None and before.connection_state == store.BINDING_CONNECTED

    result = succeeded(dispatcher.dispatch(disconnect_request(request_id="req-disconnect-1")))

    assert result.document_id == source.document_id
    assert result.owner_ref == "access-native-1"
    assert result.connection_state == store.BINDING_DISCONNECTED
    assert result.binding_generation == before.binding_generation
    after = binding(owned)
    assert after is not None
    assert after.connection_state == store.BINDING_DISCONNECTED
    assert after.binding_generation == before.binding_generation
    assert count(owned, BINDINGS) == 1
    # The captured copy and its history are untouched.
    assert count(owned, "omnivia_document_captures") == 1


def test_same_key_replay_returns_the_receipt_and_writes_nothing_more(
    owned: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost acknowledgement: the commit happened, the caller retries under the same key."""
    dispatcher = build_dispatcher(owned, tag="disconnect-replay")
    captured(dispatcher)
    first = dispatcher.dispatch(disconnect_request(request_id="req-disconnect-1"))
    assert isinstance(first, SuccessResponseEnvelope), first
    audits = count(owned, AUDIT)
    outcomes = count(owned, "omnivia_idempotency_outcomes")

    def must_not_write(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an exact replay re-ran the binding write")

    monkeypatch.setattr(document_handlers, "move_original_in_transaction", must_not_write)
    replay = dispatcher.dispatch(disconnect_request(request_id="req-disconnect-2"))

    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == first.result
    assert replay.metadata.audit_reference == first.metadata.audit_reference
    assert count(owned, BINDINGS) == 1
    assert count(owned, AUDIT) == audits
    assert count(owned, "omnivia_idempotency_outcomes") == outcomes


def test_a_new_key_on_an_already_disconnected_binding_changes_nothing(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="disconnect-again")
    captured(dispatcher)
    first = succeeded(dispatcher.dispatch(disconnect_request(request_id="req-disconnect-1")))
    stored = binding(owned)

    second = succeeded(
        dispatcher.dispatch(disconnect_request(request_id="req-disconnect-2", key="idem-disconnect-2"))
    )

    assert second == first
    assert binding(owned) == stored
    assert count(owned, BINDINGS) == 1


def test_a_disconnect_before_any_capture_keeps_a_tombstone_a_later_capture_cannot_lift(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="disconnect-first")

    result = succeeded(dispatcher.dispatch(disconnect_request(request_id="req-disconnect-1")))

    assert result.binding_generation == 1
    assert result.connection_state == store.BINDING_DISCONNECTED
    tombstone = binding(owned)
    assert tombstone is not None and tombstone.connection_state == store.BINDING_DISCONNECTED
    assert count(owned, "omnivia_document_captures") == 0

    # The stale linked capture arrives afterwards, under the tombstoned generation.
    late = dispatcher.dispatch(capture_request(request_id="req-late-capture"))
    assert isinstance(late, SuccessResponseEnvelope), late
    capture = DocumentCaptureResult.from_wire(late.result)
    assert capture.capture_disposition == store.DISPOSITION_HISTORICAL_ONLY
    assert not capture.became_head
    assert binding(owned) == tombstone
    assert count(owned, BINDINGS) == 1


def test_a_capture_under_a_disconnected_generation_is_historical_after_a_linked_capture(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="disconnect-then-capture")
    first = captured(dispatcher)
    assert first.capture_disposition == store.DISPOSITION_HEAD_ELIGIBLE
    succeeded(dispatcher.dispatch(disconnect_request(request_id="req-disconnect-1")))
    tombstone = binding(owned)

    entry = get_operation_metadata("document.capture")
    refresh = payload(b"native document body, refreshed")
    refresh.update({"intent_kind": store.INTENT_REFRESH, "base": {"kind": "existing", "capture_id": first.capture_id}})
    response = dispatcher.dispatch(
        RequestEnvelope(
            operation="document.capture",
            metadata=_metadata(entry, request_id="req-refresh", key="idem-refresh"),
            input=refresh,
        )
    )

    assert isinstance(response, SuccessResponseEnvelope), response
    refreshed = DocumentCaptureResult.from_wire(response.result)
    assert refreshed.capture_disposition == store.DISPOSITION_HISTORICAL_ONLY
    assert not refreshed.became_head
    assert binding(owned) == tombstone


def test_another_documents_or_another_routes_binding_is_a_conflict(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="disconnect-conflict")
    captured(dispatcher)
    stored = binding(owned)

    wrong_document = dispatcher.dispatch(
        disconnect_request(
            request_id="req-wrong-doc",
            key="idem-wrong-doc",
            body={"document_id": "doc-other", "owner_ref": "access-native-1", "route_kind": "direct_file"},
        )
    )
    wrong_route = dispatcher.dispatch(
        disconnect_request(
            request_id="req-wrong-route",
            key="idem-wrong-route",
            body={
                "document_id": "doc-native-1",
                "owner_ref": "access-native-1",
                "route_kind": "root_member",
                "root_ref": "root-1",
                "member_ref": "member-1",
            },
        )
    )

    for response in (wrong_document, wrong_route):
        assert isinstance(response, ErrorResponseEnvelope), response
        assert response.error.code == "conflict"
    assert binding(owned) == stored
    assert stored is not None and stored.connection_state == store.BINDING_CONNECTED
    assert count(owned, BINDINGS) == 1
    assert store.read_head(owned.connection, workspace_id=WORKSPACE, document_id="doc-other") is None


@pytest.mark.parametrize(
    ("change", "code"),
    (
        ({"workspace_id": "ws-other"}, "workspace_not_granted"),
        ({"purpose": "knowledge_retrieval"}, "invalid_purpose"),
        ({"scopes": ("memory:read",)}, "invalid_request"),
    ),
)
def test_a_request_the_session_does_not_authorize_is_refused_and_writes_nothing(
    owned: Served, change: dict[str, Any], code: str
) -> None:
    dispatcher = build_dispatcher(owned, tag="disconnect-denied")
    captured(dispatcher)
    envelope = disconnect_request(request_id="req-denied")

    response = dispatcher.dispatch(replace(envelope, metadata=replace(envelope.metadata, **change)))

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == code
    stored = binding(owned)
    assert stored is not None and stored.connection_state == store.BINDING_CONNECTED


@pytest.mark.parametrize(
    "body",
    (
        {"document_id": "doc-native-1", "owner_ref": "access-native-1"},
        {"document_id": "doc-native-1", "owner_ref": "access-native-1", "route_kind": "elsewhere"},
        {"document_id": "doc-native-1", "owner_ref": "access-native-1", "route_kind": "direct_file", "root_ref": "r"},
        {"document_id": "doc-native-1", "owner_ref": "access-native-1", "route_kind": "root_member"},
        {"document_id": "doc-native-1", "owner_ref": "access-native-1", "route_kind": "direct_file", "generation": 1},
        {"document_id": "doc native", "owner_ref": "access-native-1", "route_kind": "direct_file"},
    ),
)
def test_an_invalid_payload_is_refused_and_writes_nothing(owned: Served, body: dict[str, Any]) -> None:
    dispatcher = build_dispatcher(owned, tag="disconnect-invalid")

    response = dispatcher.dispatch(disconnect_request(request_id="req-invalid", body=body))

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "invalid_request"
    assert count(owned, BINDINGS) == 0
    assert count(owned, "omnivia_documents") == 0
    assert rows(owned, "SELECT 1 FROM omnivia_idempotency_outcomes") == []


def test_the_same_key_for_a_different_route_is_an_idempotency_conflict(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="disconnect-key-reuse")
    captured(dispatcher)
    first = dispatcher.dispatch(disconnect_request(request_id="req-disconnect-1"))
    assert isinstance(first, SuccessResponseEnvelope), first

    conflict = dispatcher.dispatch(
        disconnect_request(
            request_id="req-disconnect-2",
            body={"document_id": "doc-native-1", "owner_ref": "access-native-2", "route_kind": "direct_file"},
        )
    )

    assert isinstance(conflict, ErrorResponseEnvelope), conflict
    assert conflict.error.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
    assert binding(owned, "access-native-2") is None
    assert count(owned, BINDINGS) == 1
