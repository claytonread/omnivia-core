"""`document.capture` through the governed application dispatcher."""

from __future__ import annotations

import base64
from typing import Any

from omnivia_core_runtime.service.handlers.document_capture import (
    DOCUMENT_CAPTURE_OPERATION,
)
from omnivia_core_runtime.storage import document_intake as store
from test_evidence_capture_vertical import (
    Served,
    _metadata,
    build_dispatcher,
    count,
)

from omnivia_core.contracts.v1 import (
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ErrorResponseEnvelope,
    RequestEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
)
from omnivia_core.contracts.v1.generated import DocumentCaptureResult

pytest_plugins = ("test_evidence_capture_vertical",)


def payload(content: bytes = b"native document body") -> dict[str, Any]:
    return {
        "document_id": "doc-native-1",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "media_type": "text/plain",
        "requested_source_identity": "source-native-1",
        "intent_kind": "new_capture",
        "base": {"kind": "creation"},
        "target_binding_revision": "target-binding-1",
        "processing_profile": {"profile_ref": "profile-1", "revision": "revision-1"},
        "original_binding": {
            "owner_ref": "access-native-1",
            "generation": 1,
            "route_kind": "direct_file",
        },
        "display_name": "document.txt",
    }


def request(*, request_id: str, content: bytes = b"native document body") -> RequestEnvelope:
    entry = get_operation_metadata(DOCUMENT_CAPTURE_OPERATION)
    return RequestEnvelope(
        operation=DOCUMENT_CAPTURE_OPERATION,
        metadata=_metadata(entry, request_id=request_id, key="idem-document-1"),
        input=payload(content),
    )


def test_document_capture_commits_audited_rows_and_exact_replay(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="document-capture")

    first = dispatcher.dispatch(request(request_id="req-document-1"))
    replay = dispatcher.dispatch(request(request_id="req-document-2"))

    assert isinstance(first, SuccessResponseEnvelope), first
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == first.result
    assert replay.metadata.audit_reference == first.metadata.audit_reference
    result = DocumentCaptureResult.from_wire(first.result)
    assert result.document_id == "doc-native-1"
    assert result.became_head
    assert result.capture_disposition == store.DISPOSITION_HEAD_ELIGIBLE
    assert first.metadata.audit_reference is not None
    assert count(owned, "omnivia_document_captures") == 1
    assert count(owned, "omnivia_document_capture_outbox") == 1
    assert count(owned, "omnivia_document_original_bindings") == 1
    assert count(owned, "omnivia_idempotency_outcomes") == 1


def test_document_capture_changed_body_replay_is_refused_without_domain_effect(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="document-conflict")
    first = dispatcher.dispatch(request(request_id="req-document-1"))

    conflict = dispatcher.dispatch(
        request(request_id="req-document-2", content=b"different body")
    )

    assert isinstance(first, SuccessResponseEnvelope), first
    assert isinstance(conflict, ErrorResponseEnvelope), conflict
    assert conflict.error.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
    assert count(owned, "omnivia_document_captures") == 1
    assert count(owned, "omnivia_document_capture_outbox") == 1


def test_document_capture_rejects_unknown_fields_before_mutation(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="document-closed")
    changed = payload()
    changed["original_path"] = "/private/secret.txt"
    entry = get_operation_metadata(DOCUMENT_CAPTURE_OPERATION)
    response = dispatcher.dispatch(
        RequestEnvelope(
            operation=DOCUMENT_CAPTURE_OPERATION,
            metadata=_metadata(entry, request_id="req-document-invalid", key="idem-invalid"),
            input=changed,
        )
    )

    assert isinstance(response, ErrorResponseEnvelope), response
    assert count(owned, "omnivia_document_captures") == 0
