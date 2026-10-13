"""`document.capture` through the governed application dispatcher."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

from omnivia_core_runtime.service.document_intake import read_retained_source
from omnivia_core_runtime.service.document_transfer import (
    DocumentTransferRefused,
    stage_document_bytes,
)
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


def test_staged_binary_document_is_admitted_without_inline_content(owned: Served) -> None:
    content = b"x" * (4 * 1024 * 1024 + 1)
    service = SimpleNamespace(
        connection=owned.connection,
        identity=owned.identity,
        generation=owned.generation,
        workspace_id="ws-capture-0001",
        layout=owned.layout,
    )
    descriptor = stage_document_bytes(
        service,
        content=content,
        media_type="application/pdf",
        transfer_key="idem-staged-document-1",
    )
    # A lost stage acknowledgement can repeat the same transfer and gets exactly
    # the same server-issued descriptor rather than creating a second source.
    assert stage_document_bytes(
        service,
        content=content,
        media_type="application/pdf",
        transfer_key="idem-staged-document-1",
    ) == descriptor

    staged_payload = payload()
    staged_payload.pop("content_base64")
    staged_payload["media_type"] = "application/pdf"
    staged_payload["staged_source"] = descriptor
    entry = get_operation_metadata(DOCUMENT_CAPTURE_OPERATION)
    response = build_dispatcher(owned, tag="document-staged").dispatch(
        RequestEnvelope(
            operation=DOCUMENT_CAPTURE_OPERATION,
            metadata=_metadata(
                entry,
                request_id="req-document-staged",
                key="idem-document-staged",
            ),
            input=staged_payload,
        )
    )

    assert isinstance(response, SuccessResponseEnvelope), response
    result = DocumentCaptureResult.from_wire(response.result)
    assert result.source_length_bytes == len(content)
    assert read_retained_source(service, capture_id=result.capture_id) == content
    assert count(owned, "omnivia_document_captures") == 1
    assert count(owned, "omnivia_staged_sources") == 2  # transfer plus capture evidence


def test_staged_transfer_key_cannot_be_rebound_to_changed_bytes(owned: Served) -> None:
    service = SimpleNamespace(
        connection=owned.connection,
        identity=owned.identity,
        generation=owned.generation,
        workspace_id="ws-capture-0001",
        layout=owned.layout,
    )
    stage_document_bytes(
        service,
        content=b"first",
        media_type="text/plain",
        transfer_key="idem-staged-conflict",
    )
    try:
        stage_document_bytes(
            service,
            content=b"second",
            media_type="text/plain",
            transfer_key="idem-staged-conflict",
        )
    except DocumentTransferRefused:
        pass
    else:
        raise AssertionError("changed bytes rebound an immutable transfer key")
