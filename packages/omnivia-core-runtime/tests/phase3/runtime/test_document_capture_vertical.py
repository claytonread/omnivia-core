"""`document.capture` through the governed application dispatcher."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import pytest
from omnivia_core_runtime.service.document_intake import (
    DocumentIntakeRefused,
    expire_capture,
    read_retained_source,
)
from omnivia_core_runtime.service.document_processing import (
    approved_reprocess_profiles,
)
from omnivia_core_runtime.service.document_transfer import (
    DocumentTransferRefused,
    stage_document_bytes,
)
from omnivia_core_runtime.service.document_worker import (
    REASON_ISOLATION,
    DocumentWorkerFailed,
)
from omnivia_core_runtime.service.handlers import document_capture as document_handlers
from omnivia_core_runtime.service.handlers.document_capture import (
    DOCUMENT_CAPTURE_OPERATION,
    DOCUMENT_REPROCESS_OPERATION,
)
from omnivia_core_runtime.service.ocr_resource_store import OcrResourceSetIdentity
from omnivia_core_runtime.service.ocr_resources import TESSERACT_REVISION
from omnivia_core_runtime.service.processing_settings import LanguageStorageFull
from omnivia_core_runtime.storage import document_intake as store
from omnivia_core_runtime.workspace.blob_publication import BlobPublicationRefused
from test_evidence_capture_vertical import (
    Served,
    _metadata,
    build_dispatcher,
    count,
)

from omnivia_core.contracts.v1 import (
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INTERNAL_RECOVERABLE,
    RETRY_CLASS_RETRYABLE,
    ErrorResponseEnvelope,
    RequestEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
)
from omnivia_core.contracts.v1.generated import (
    DocumentCaptureResult,
    DocumentReprocessResult,
)

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


def approved_selection(media_type: str = "text/plain") -> dict[str, Any]:
    """The one approved selection for this media type, as Core itself resolves it.

    A reprocess names a reference, a revision *and* the immutable profile identity that pair
    was reported to resolve to. All three come from `approved_reprocess_profiles`, because a
    selection this build did not approve, or a digest it did not compute, is refused before
    anything runs -- which is the point of the field.
    """
    option = approved_reprocess_profiles(media_type=media_type)[0]
    return {
        "profile_ref": option.profile_ref,
        "revision": option.revision,
        "expected_profile_digest": option.profile_digest,
    }


def reprocess_request(
    *,
    capture_id: str,
    request_id: str,
    key: str = "idem-document-reprocess-1",
    profile: dict[str, Any] | None = None,
    expected: str | None = None,
    extra: dict[str, Any] | None = None,
) -> RequestEnvelope:
    entry = get_operation_metadata(DOCUMENT_REPROCESS_OPERATION)
    body: dict[str, Any] = {
        "capture_id": capture_id,
        "processing_profile": approved_selection() if profile is None else profile,
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


@pytest.mark.parametrize(
    ("target", "refusal"),
    (
        ("admit_document_capture_in_transaction", DocumentIntakeRefused),
        ("publish_blob", BlobPublicationRefused),
    ),
)
def test_temporarily_unavailable_capture_work_is_served_as_retryable(
    owned: Served, monkeypatch: Any, target: str, refusal: type[Exception]
) -> None:
    """A capture that could not be preserved *yet* must not tell the caller to stop.

    Admission and blob publication both refuse this way while the workspace is momentarily
    unable to take the bytes, and resending the identical request is the recovery. The code
    says so -- `internal_recoverable` is frozen retryable -- so a refusal built with the
    default `non_retryable` contradicts its own code, and the response validator answers
    that contradiction with a bare `internal_non_recoverable`: the opposite instruction,
    carrying none of the original message.
    """
    dispatcher = build_dispatcher(owned, tag="document-unavailable")

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise refusal("the workspace cannot take these bytes right now")

    monkeypatch.setattr(document_handlers, target, unavailable)
    response = dispatcher.dispatch(request(request_id="req-document-unavailable"))

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == ERROR_CODE_INTERNAL_RECOVERABLE
    assert response.error.retry_class == RETRY_CLASS_RETRYABLE
    assert response.error.message == "the transferred document could not be preserved"
    assert count(owned, "omnivia_document_captures") == 0


def test_a_capture_deferred_for_language_storage_is_its_own_retryable_state(
    owned: Served, monkeypatch: Any
) -> None:
    """§18.4's **Language storage is full**, as a caller actually receives it.

    The deferral is retryable for the same reason the unavailable case above is
    -- resending the identical request is the recovery -- but it is not the same
    answer, and the message is what makes that visible: nothing about this
    document or its bytes is wrong, and what the caller has to wait for is the
    work holding the managed resources rather than the workspace taking bytes.

    And the capacity §18.4 requires to be *visible* arrives as typed fields, not
    as a sentence to be parsed: the bytes the admission required, the bytes the
    cache could have supplied, and the exact resource sets it was asked for. A
    settings surface showing **Review pending processing** reads those; the
    message beside them stays the operator's.

    The refusal is injected at the admission seam, because what is under test
    here is the wire state it becomes. That the production admission raises it,
    with these figures, against a cache accounted to its full cap is
    `test_document_processing.test_a_new_profile_meeting_a_full_cache_is_deferred_not_refused_as_missing`.
    """
    dispatcher = build_dispatcher(owned, tag="document-language-storage")
    wanted = OcrResourceSetIdentity(
        language="deu",
        engine_version=TESSERACT_REVISION,
        model_version="tessdata_fast-4.1.0",
        byte_size=4096,
        sha256="b" * 64,
    )

    def full(*args: Any, **kwargs: Any) -> Any:
        raise LanguageStorageFull(
            "the managed OCR resource cap is fully occupied",
            required_bytes=4096,
            available_bytes=512,
            identities=(wanted,),
        )

    monkeypatch.setattr(
        document_handlers, "admit_document_capture_in_transaction", full
    )
    response = dispatcher.dispatch(request(request_id="req-document-language-full"))

    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == ERROR_CODE_INTERNAL_RECOVERABLE
    assert response.error.retry_class == RETRY_CLASS_RETRYABLE
    assert "language storage is full" in response.error.message
    assert "could not be preserved" not in response.error.message
    # The typed facts, on the wire, in the shape a caller may rely on.
    assert response.error.details == {
        "resource_condition": "language_storage_full",
        "required_bytes": 4096,
        "available_bytes": 512,
        "requested_languages": ["deu"],
        "requested_resources": [
            {
                "language": "deu",
                "engine_version": TESSERACT_REVISION,
                "model_version": "tessdata_fast-4.1.0",
                "byte_size": 4096,
                "sha256": "b" * 64,
            }
        ],
    }
    # And they survive the contract encoding rather than only the handler's call.
    assert response.error.to_wire()["details"] == response.error.details
    # Deferred, so nothing of the capture exists to be resumed or reconciled.
    assert count(owned, "omnivia_document_captures") == 0
    assert count(owned, "omnivia_document_capture_outbox") == 0


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


def test_document_reprocess_records_a_worker_failure_as_its_own_reason(
    owned: Served, monkeypatch: Any
) -> None:
    """DOC-16. Reprocess extracts in the same confined child the outbox uses, and
    a child that produced no outcome is not a verdict on the document.

    So the reprocess settles `failed` under the worker's own typed reason rather
    than under `source_invalid`, and the retained immutable bytes it was asked to
    reprocess are untouched -- the same reprocess can be asked for again once the
    condition is gone.
    """
    dispatcher = build_dispatcher(owned, tag="document-reprocess-worker")
    source = captured(dispatcher)

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise DocumentWorkerFailed(REASON_ISOLATION, "no adapter on this host")

    monkeypatch.setattr(document_handlers, "extract_document", refuse)
    response = dispatcher.dispatch(
        reprocess_request(capture_id=source.capture_id, request_id="req-reprocess-worker")
    )

    assert isinstance(response, SuccessResponseEnvelope), response
    result = DocumentReprocessResult.from_wire(response.result)
    assert result.result_state == "failed"
    assert result.publication_disposition == "not_published"
    stored = store.read_result(
        owned.connection,
        workspace_id="ws-capture-0001",
        extraction_revision_id=result.extraction_revision_id,
    )
    assert stored is not None
    # The worker's own reason, and never `source_invalid`.
    assert json.loads(stored.limitations_json)["code"] == REASON_ISOLATION
    # The source survived: nothing about the document was decided here, so the
    # same reprocess can be asked for again.
    assert count(owned, "omnivia_document_captures") == 1
    retained = read_retained_source(
        SimpleNamespace(
            connection=owned.connection,
            identity=owned.identity,
            generation=owned.generation,
            workspace_id="ws-capture-0001",
            layout=owned.layout,
        ),
        capture_id=source.capture_id,
    )
    assert retained == b"native document body"


def test_document_reprocess_changed_request_is_idempotency_conflict_without_extraction(
    owned: Served, monkeypatch: Any
) -> None:
    """One key covers one request, and the frozen base is part of that request.

    The varied field is the frozen extraction base rather than the profile revision: an
    ordinary document has exactly one approved selection, so a changed revision is now
    refused as an unapproved selection before the key is ever consulted. The property under
    test is unchanged -- a key reused for a different request conflicts, and conflicts
    without extracting anything a second time.
    """
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
            expected=DocumentReprocessResult.from_wire(
                first.result
            ).extraction_revision_id,
        )
    )
    assert isinstance(conflict, ErrorResponseEnvelope), conflict
    assert conflict.error.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
    assert count(owned, "omnivia_extraction_revisions") == 1


def test_document_reprocess_refuses_a_selection_it_never_approved(owned: Served) -> None:
    """A reference, revision or digest Core did not report is refused before execution."""
    dispatcher = build_dispatcher(owned, tag="document-reprocess-unapproved")
    source = captured(dispatcher)
    approved = approved_selection()
    for profile in (
        {**approved, "profile_ref": "profile-reprocess"},
        {**approved, "revision": "revision-3"},
        {**approved, "expected_profile_digest": "sha256:" + "0" * 64},
    ):
        response = dispatcher.dispatch(
            reprocess_request(
                capture_id=source.capture_id,
                request_id="req-reprocess-unapproved",
                key=f"idem-unapproved-{profile['profile_ref']}-{profile['revision']}"
                f"-{profile['expected_profile_digest'][7:11]}",
                profile=profile,
            )
        )
        assert isinstance(response, ErrorResponseEnvelope), response
        assert response.error.code == "conflict"
    assert count(owned, "omnivia_extraction_revisions") == 0


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
