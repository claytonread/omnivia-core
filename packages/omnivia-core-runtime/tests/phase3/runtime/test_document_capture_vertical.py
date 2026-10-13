"""`document.capture` through the governed application dispatcher."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

from omnivia_core_runtime.service.document_intake import expire_capture, read_retained_source
from omnivia_core_runtime.service.document_transfer import (
    DocumentTransferRefused,
    stage_document_bytes,
)
from omnivia_core_runtime.service.handlers.document_capture import (
    DOCUMENT_CAPTURE_OPERATION,
    DOCUMENT_REPROCESS_OPERATION,
)
from omnivia_core_runtime.service.handlers import document_capture as document_handlers
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
from omnivia_core.contracts.v1.generated import DocumentCaptureResult, DocumentReprocessResult

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


def reprocess_request(
    *,
    capture_id: str,
    request_id: str,
    key: str = "idem-document-reprocess-1",
    revision: str = "revision-2",
    expected: str | None = None,
    extra: dict[str, Any] | None = None,
) -> RequestEnvelope:
    entry = get_operation_metadata(DOCUMENT_REPROCESS_OPERATION)
    body: dict[str, Any] = {
        "capture_id": capture_id,
        "processing_profile": {"profile_ref": "profile-reprocess", "revision": revision},
        "expected_base": {} if expected is None else {"extraction_id": expected},
    }
    if extra:
        body.update(extra)
    return RequestEnvelope(
        operation=DOCUMENT_REPROCESS_OPERATION,
        metadata=_metadata(entry, request_id=request_id, key=key),
        input=body,
    )


def captured(dispatcher: Any) -> DocumentCaptureResult:
    response = dispatcher.dispatch(request(request_id="req-document-capture"))
    assert isinstance(response, SuccessResponseEnvelope), response
    return DocumentCaptureResult.from_wire(response.result)


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


def test_document_reprocess_publishes_retained_bytes_and_exact_replay(
    owned: Served, monkeypatch: Any
) -> None:
    dispatcher = build_dispatcher(owned, tag="document-reprocess")
    source = captured(dispatcher)

    first = dispatcher.dispatch(
        reprocess_request(capture_id=source.capture_id, request_id="req-reprocess-1")
    )
    assert isinstance(first, SuccessResponseEnvelope), first
    result = DocumentReprocessResult.from_wire(first.result)
    assert result.capture_id == source.capture_id
    assert result.result_state == "succeeded"
    assert result.publication_disposition == "promoted"
    assert result.became_head

    def must_not_extract(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an exact application replay reran extraction")

    monkeypatch.setattr(document_handlers, "extract_document", must_not_extract)
    replay = dispatcher.dispatch(
        reprocess_request(capture_id=source.capture_id, request_id="req-reprocess-2")
    )
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == first.result
    assert replay.metadata.audit_reference == first.metadata.audit_reference
    assert count(owned, "omnivia_extraction_revisions") == 1
    assert count(owned, "omnivia_extraction_results") == 1


def test_document_reprocess_changed_profile_is_idempotency_conflict_without_extraction(
    owned: Served, monkeypatch: Any
) -> None:
    dispatcher = build_dispatcher(owned, tag="document-reprocess-conflict")
    source = captured(dispatcher)
    first = dispatcher.dispatch(
        reprocess_request(capture_id=source.capture_id, request_id="req-reprocess-1")
    )
    assert isinstance(first, SuccessResponseEnvelope), first

    def must_not_extract(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a changed-key conflict reran extraction")

    monkeypatch.setattr(document_handlers, "extract_document", must_not_extract)
    conflict = dispatcher.dispatch(
        reprocess_request(
            capture_id=source.capture_id,
            request_id="req-reprocess-2",
            revision="revision-3",
        )
    )
    assert isinstance(conflict, ErrorResponseEnvelope), conflict
    assert conflict.error.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
    assert count(owned, "omnivia_extraction_revisions") == 1


def test_document_reprocess_stale_expected_head_is_historical(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="document-reprocess-stale")
    source = captured(dispatcher)
    first_response = dispatcher.dispatch(
        reprocess_request(capture_id=source.capture_id, request_id="req-reprocess-1")
    )
    assert isinstance(first_response, SuccessResponseEnvelope), first_response
    first = DocumentReprocessResult.from_wire(first_response.result)

    stale_response = dispatcher.dispatch(
        reprocess_request(
            capture_id=source.capture_id,
            request_id="req-reprocess-stale",
            key="idem-document-reprocess-stale",
            revision="revision-3",
            expected=None,
        )
    )
    assert isinstance(stale_response, SuccessResponseEnvelope), stale_response
    stale = DocumentReprocessResult.from_wire(stale_response.result)
    assert stale.publication_disposition == "historical_only"
    assert not stale.became_head
    head = store.read_head(
        owned.connection, workspace_id="ws-capture-0001", document_id=source.document_id
    )
    assert head is not None and head.current_extraction_id == first.extraction_revision_id


def test_document_reprocess_refuses_expired_capture_without_revision(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="document-reprocess-expired")
    source = captured(dispatcher)
    service = SimpleNamespace(
        connection=owned.connection,
        identity=owned.identity,
        generation=owned.generation,
        workspace_id="ws-capture-0001",
        layout=owned.layout,
    )
    expire_capture(service, capture_id=source.capture_id)

    response = dispatcher.dispatch(
        reprocess_request(capture_id=source.capture_id, request_id="req-reprocess-expired")
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "conflict"
    assert count(owned, "omnivia_extraction_revisions") == 0


def test_document_reprocess_rejects_unknown_fields(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="document-reprocess-closed")
    source = captured(dispatcher)
    response = dispatcher.dispatch(
        reprocess_request(
            capture_id=source.capture_id,
            request_id="req-reprocess-invalid",
            extra={"parser": "arbitrary"},
        )
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == "invalid_request"
    assert count(owned, "omnivia_extraction_revisions") == 0
