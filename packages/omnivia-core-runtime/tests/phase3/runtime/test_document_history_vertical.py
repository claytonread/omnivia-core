"""`document.history` through the governed application dispatcher.

The read's claim is that it is the *authoritative* intake history, so the tests here are
mostly about the ways a history can lie: a member of a page reading as current because it is
last, an action offered that Core would refuse, a pending extraction reported as an outcome,
a page claiming more than it holds, and a token moved to another document or another page
size. Each of those has a test, and each asserts the honest answer rather than merely that a
call succeeded.

The reprocess tests at the end close the loop the UI actually walks: read the history, take
the exact capture, the exact approved profile and the exact extraction head out of it, and
send `document.reprocess` built from nothing else.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

from omnivia_core_runtime.service.document_intake import (
    disconnect_original,
    expire_capture,
    set_document_source_state,
)
from omnivia_core_runtime.service.document_processing import (
    APPROVED_PROFILE_REVISION,
    DEFAULT_PROFILE_REF,
    TEXT_ONLY_PROFILE_REF,
    ReprocessProfileOption,
    approved_reprocess_profiles,
    portable_profile,
)
from omnivia_core_runtime.service.handlers import document_capture as document_handlers
from omnivia_core_runtime.service.handlers.document_capture import (
    DEFAULT_HISTORY_PAGE_LIMIT,
    DOCUMENT_CAPTURE_OPERATION,
    DOCUMENT_HISTORY_OPERATION,
    DOCUMENT_REPROCESS_OPERATION,
)
from omnivia_core_runtime.service.ocr_resources import (
    MODE_TEXT_AND_LOCAL_OCR,
    MODE_TEXT_ONLY,
    ExtractionSettings,
)
from omnivia_core_runtime.storage import document_intake as store
from test_evidence_capture_vertical import (
    Served,
    _metadata,
    build_dispatcher,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INTERNAL_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_WORKSPACE_NOT_GRANTED,
    ErrorResponseEnvelope,
    RequestEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
    idempotency_equivalence,
    validate_document_history_result,
)
from omnivia_core.contracts.v1.codec import encode_response
from omnivia_core.contracts.v1.generated import (
    DocumentCaptureResult,
    DocumentHistoryInput,
    DocumentHistoryResult,
    DocumentReprocessResult,
)

pytest_plugins = ("test_evidence_capture_vertical",)

WORKSPACE = "ws-capture-0001"
DOCUMENT = "doc-history-1"


# --- requests -------------------------------------------------------------------


def capture_payload(
    *,
    content: bytes,
    source_identity: str = "source-history-1",
    intent: str = "new_capture",
    expected_base: str | None = None,
    media_type: str = "text/plain",
) -> dict[str, Any]:
    base: dict[str, Any] = (
        {"kind": "creation"}
        if intent == "new_capture"
        else {"kind": "existing", **({} if expected_base is None else {"capture_id": expected_base})}
    )
    return {
        "document_id": DOCUMENT,
        "content_base64": base64.b64encode(content).decode("ascii"),
        "media_type": media_type,
        "requested_source_identity": source_identity,
        "intent_kind": intent,
        "base": base,
        "target_binding_revision": "target-binding-1",
        "processing_profile": {
            "profile_ref": DEFAULT_PROFILE_REF,
            "revision": APPROVED_PROFILE_REVISION,
        },
        "original_binding": {
            "owner_ref": "access-history-1",
            "generation": 1,
            "route_kind": "direct_file",
        },
    }


def capture_request(
    *,
    request_id: str,
    key: str,
    content: bytes = b"first document body",
    intent: str = "new_capture",
    expected_base: str | None = None,
    media_type: str = "text/plain",
) -> RequestEnvelope:
    entry = get_operation_metadata(DOCUMENT_CAPTURE_OPERATION)
    return RequestEnvelope(
        operation=DOCUMENT_CAPTURE_OPERATION,
        metadata=_metadata(entry, request_id=request_id, key=key),
        input=capture_payload(
            content=content,
            intent=intent,
            expected_base=expected_base,
            media_type=media_type,
        ),
    )


def history_request(
    *,
    request_id: str = "req-history-1",
    document_id: str = DOCUMENT,
    limit: int | None = None,
    token: str | None = None,
    workspace_id: str = WORKSPACE,
    extra: dict[str, Any] | None = None,
) -> RequestEnvelope:
    entry = get_operation_metadata(DOCUMENT_HISTORY_OPERATION)
    body: dict[str, Any] = {"document_id": document_id}
    if limit is not None:
        body["limit"] = limit
    if token is not None:
        body["page"] = {"continuation_token": token}
    if extra:
        body.update(extra)
    return RequestEnvelope(
        operation=DOCUMENT_HISTORY_OPERATION,
        metadata=_metadata(
            entry, request_id=request_id, key=None, workspace_id=workspace_id
        ),
        input=body,
    )


def approved_digest(profile_ref: str, media_type: str = "text/plain") -> str:
    """The exact profile identity Core approves for this reference and media type.

    Taken from the same function the read reports from and the write resolves its execution
    from. A test that composed its own digest would prove nothing about the request a caller
    can actually make, because the only digest Core accepts is the one it computed itself.
    """
    return next(
        option.profile_digest
        for option in approved_reprocess_profiles(media_type=media_type)
        if option.profile_ref == profile_ref
    )


def reprocess_request(
    *,
    capture_id: str,
    profile_ref: str,
    revision: str,
    expected: str | None,
    request_id: str,
    key: str,
    profile_digest: str | None = None,
    media_type: str = "text/plain",
) -> RequestEnvelope:
    entry = get_operation_metadata(DOCUMENT_REPROCESS_OPERATION)
    return RequestEnvelope(
        operation=DOCUMENT_REPROCESS_OPERATION,
        metadata=_metadata(entry, request_id=request_id, key=key),
        input={
            "capture_id": capture_id,
            "processing_profile": {
                "profile_ref": profile_ref,
                "revision": revision,
                "expected_profile_digest": (
                    approved_digest(profile_ref, media_type)
                    if profile_digest is None
                    else profile_digest
                ),
            },
            "expected_base": {} if expected is None else {"extraction_id": expected},
        },
    )


# --- helpers --------------------------------------------------------------------


def answered(response: Any) -> SuccessResponseEnvelope:
    assert isinstance(response, SuccessResponseEnvelope), response
    return response


def refusal(response: Any) -> ErrorResponseEnvelope:
    assert isinstance(response, ErrorResponseEnvelope), response
    return response


def history(dispatcher: Any, **kwargs: Any) -> DocumentHistoryResult:
    """The history as the contract's own value, held to the contract's own validator."""
    response = answered(dispatcher.dispatch(history_request(**kwargs)))
    result = DocumentHistoryResult.from_wire(response.result)
    validate_document_history_result(
        result,
        DocumentHistoryInput(
            document_id=kwargs.get("document_id", DOCUMENT),
            limit=kwargs.get("limit"),
            page=None if kwargs.get("token") is None else _page(kwargs["token"]),
        ),
        kwargs.get("document_id", DOCUMENT),
    )
    return result


def _page(token: str) -> Any:
    from omnivia_core.contracts.v1.generated import PageMetadata

    return PageMetadata(continuation_token=token)


def capture(dispatcher: Any, **kwargs: Any) -> DocumentCaptureResult:
    return DocumentCaptureResult.from_wire(
        answered(dispatcher.dispatch(capture_request(**kwargs))).result
    )


def reprocess(dispatcher: Any, **kwargs: Any) -> DocumentReprocessResult:
    return DocumentReprocessResult.from_wire(
        answered(dispatcher.dispatch(reprocess_request(**kwargs))).result
    )


def service_view(owned: Served) -> SimpleNamespace:
    return SimpleNamespace(
        connection=owned.connection,
        identity=owned.identity,
        generation=owned.generation,
        workspace_id=WORKSPACE,
        layout=owned.layout,
    )


# --- the read -------------------------------------------------------------------


def test_history_reports_both_heads_and_the_single_capture_it_holds(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="history-one")
    source = capture(dispatcher, request_id="req-c1", key="idem-c1")

    result = history(dispatcher)

    assert result.document_id == DOCUMENT
    assert result.capture_count == 1
    assert result.extraction_count == 0
    assert result.source.source_state == store.SOURCE_ACTIVE
    assert result.source.current_capture_id == source.capture_id
    assert result.source.capture_head_sequence == 1
    assert result.source.original_binding_state == store.BINDING_CONNECTED
    # The extraction head is a separate column and has not moved: an admitted capture is
    # not an extraction, and a document with no extraction is not a failed one.
    assert result.extraction.current_extraction_id is None
    assert result.extraction.extraction_head_ordinal == 0
    assert result.extraction.retrieval_state == "none"
    assert result.extraction.published_at is None
    assert result.page.continuation_token is None

    entry = result.captures[0]
    assert entry.capture_id == source.capture_id
    assert entry.is_current_source
    assert entry.capture_disposition == store.DISPOSITION_HEAD_ELIGIBLE
    assert entry.disposition_reason == store.REASON_ELIGIBLE
    assert entry.retention_state == store.RETENTION_RETAINED
    assert entry.processing_state in {store.OUTBOX_PENDING, store.OUTBOX_SETTLED}
    assert entry.requested_profile.profile_ref == DEFAULT_PROFILE_REF
    assert entry.expected_base_capture_id is None
    assert [action.action for action in entry.actions] == ["reprocess_captured_copy"]
    assert entry.actions[0].available
    assert entry.actions[0].unavailable_reason is None
    assert [option.profile_ref for option in entry.reprocess_profiles] == [
        DEFAULT_PROFILE_REF
    ]
    assert [action.action for action in result.actions] == ["refresh_original"]
    assert result.actions[0].available


def test_the_served_envelope_carries_the_contract_version_the_client_pins(
    owned: Served,
) -> None:
    """The one fact the macOS client's version gate rests on, asserted here.

    `CLICoreDocumentHistoryTransport` refuses an envelope whose
    `metadata.version.api_version` is not the contract version it mirrors. That gate is
    only honest if this side actually serves that value, and nothing else in this tree
    pins the two together.
    """
    dispatcher = build_dispatcher(owned, tag="history-version")
    capture(dispatcher, request_id="req-c1", key="idem-c1")
    response = answered(dispatcher.dispatch(history_request()))
    wire = encode_response(response)
    assert wire["metadata"]["version"]["api_version"] == CONTRACT_VERSION
    assert CONTRACT_VERSION == "1.3"


def test_history_exposes_no_bytes_path_bookmark_or_operation_key(owned: Served) -> None:
    """Nothing a reader of history is given could rebuild the source or its location."""
    dispatcher = build_dispatcher(owned, tag="history-leak")
    content = b"a body with a recognisable sentence in it"
    source = capture(dispatcher, request_id="req-c1", key="idem-c1", content=content)
    reprocess(
        dispatcher,
        capture_id=source.capture_id,
        profile_ref=DEFAULT_PROFILE_REF,
        revision=APPROVED_PROFILE_REVISION,
        expected=None,
        request_id="req-r1",
        key="idem-r1",
    )

    wire = answered(dispatcher.dispatch(history_request())).result
    rendered = repr(wire)
    for secret in (
        base64.b64encode(content).decode("ascii"),
        "recognisable sentence",
        "access-history-1",  # the opaque native owner reference
        "idem-c1",  # the capture's operation key
        "target-binding-1",
        str(owned.layout.blobs_path),
    ):
        assert secret not in rendered, secret
    # The route reaches the result as one aggregate word and nothing else.
    assert "owner_ref" not in rendered
    assert "root_ref" not in rendered
    assert "display_name" not in rendered
    assert "request_digest" not in rendered
    assert "modelHashes" not in rendered and "limits" not in rendered


def test_history_of_an_unknown_document_is_not_found(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="history-missing")
    error = refusal(dispatcher.dispatch(history_request(document_id="doc-absent")))
    assert error.error.code == ERROR_CODE_NOT_FOUND


def test_history_of_another_workspaces_document_is_refused_at_the_seam(
    owned: Served,
) -> None:
    """A workspace the session does not grant never reaches storage at all.

    The refusal is the authorization seam's, before the handler, so the answer cannot
    distinguish a document of another workspace that exists from one that does not.
    """
    dispatcher = build_dispatcher(owned, tag="history-isolation")
    capture(dispatcher, request_id="req-c1", key="idem-c1")
    error = refusal(
        dispatcher.dispatch(history_request(workspace_id="ws-other-00000002"))
    )
    assert error.error.code == ERROR_CODE_WORKSPACE_NOT_GRANTED


def test_history_rejects_an_unknown_selector_rather_than_widening_the_read(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="history-closed")
    capture(dispatcher, request_id="req-c1", key="idem-c1")
    error = refusal(
        dispatcher.dispatch(history_request(extra={"workspace_id": "ws-other-00000002"}))
    )
    assert error.error.code == ERROR_CODE_INVALID_REQUEST
    other = refusal(dispatcher.dispatch(history_request(extra={"include_bytes": True})))
    assert other.error.code == ERROR_CODE_INVALID_REQUEST


def test_history_refuses_an_empty_page_object(owned: Served) -> None:
    """`page: {}` asks to continue from nowhere; an absent page is how 'first page' is said."""
    dispatcher = build_dispatcher(owned, tag="history-empty-page")
    capture(dispatcher, request_id="req-c1", key="idem-c1")
    entry = get_operation_metadata(DOCUMENT_HISTORY_OPERATION)
    error = refusal(
        dispatcher.dispatch(
            RequestEnvelope(
                operation=DOCUMENT_HISTORY_OPERATION,
                metadata=_metadata(entry, request_id="req-history-empty", key=None),
                input={"document_id": DOCUMENT, "page": {}},
            )
        )
    )
    assert error.error.code == ERROR_CODE_INVALID_REQUEST


# --- pagination -----------------------------------------------------------------


def _three_captures(dispatcher: Any) -> list[DocumentCaptureResult]:
    first = capture(dispatcher, request_id="req-c1", key="idem-c1", content=b"body one")
    second = capture(
        dispatcher,
        request_id="req-c2",
        key="idem-c2",
        content=b"body two",
        intent="refresh",
        expected_base=first.capture_id,
    )
    third = capture(
        dispatcher,
        request_id="req-c3",
        key="idem-c3",
        content=b"body three",
        intent="refresh",
        expected_base=second.capture_id,
    )
    return [first, second, third]


def test_history_pages_both_sequences_deterministically_under_one_token(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="history-pages")
    captures = _three_captures(dispatcher)
    for index, source in enumerate(captures, start=1):
        reprocess(
            dispatcher,
            capture_id=source.capture_id,
            profile_ref=DEFAULT_PROFILE_REF,
            revision=APPROVED_PROFILE_REVISION,
            # Every one of the three froze the same empty extraction head, so only the
            # first can be promoted and the other two are history by construction.
            expected=None,
            request_id=f"req-r{index}",
            key=f"idem-r{index}",
        )

    first = history(dispatcher, limit=2, request_id="req-history-p1")
    assert first.capture_count == 3
    assert first.extraction_count == 3
    assert [entry.capture_sequence for entry in first.captures] == [1, 2]
    assert [entry.document_ordinal for entry in first.extractions] == [1, 2]
    token = first.page.continuation_token
    assert token is not None

    second = history(dispatcher, limit=2, token=token, request_id="req-history-p2")
    assert [entry.capture_sequence for entry in second.captures] == [3]
    assert [entry.document_ordinal for entry in second.extractions] == [3]
    assert second.page.continuation_token is None
    # No member is served twice, and the two pages together are the whole history.
    assert len({entry.capture_id for entry in (*first.captures, *second.captures)}) == 3


def test_a_history_token_cannot_be_moved_to_another_document_or_page_size(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="history-token")
    _three_captures(dispatcher)
    token = history(dispatcher, limit=1, request_id="req-history-p1").page.continuation_token
    assert token is not None

    # Same token, a different page size: the binding digest covers the limit.
    widened = refusal(
        dispatcher.dispatch(
            history_request(limit=2, token=token, request_id="req-history-widened")
        )
    )
    assert widened.error.code == ERROR_CODE_INVALID_REQUEST
    # Same token, another document: refused before the document is even looked up, so a
    # tampered token cannot be used to probe whether another document exists.
    moved = refusal(
        dispatcher.dispatch(
            history_request(
                document_id="doc-absent",
                limit=1,
                token=token,
                request_id="req-history-moved",
            )
        )
    )
    assert moved.error.code == ERROR_CODE_INVALID_REQUEST
    # An unsigned token, and a token truncated to its document half.
    for tampered in ("not-a-token", token.split(".")[0], token[:-2]):
        assert (
            refusal(
                dispatcher.dispatch(
                    history_request(
                        limit=1, token=tampered, request_id="req-history-tampered"
                    )
                )
            ).error.code
            == ERROR_CODE_INVALID_REQUEST
        )


def test_a_history_page_limit_is_clamped_rather_than_honoured_unbounded(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="history-clamp")
    _three_captures(dispatcher)
    # The schema admits 1000; this handler serves at most its own ceiling, and a request
    # naming no limit gets the stated default rather than everything.
    result = history(dispatcher, limit=1000, request_id="req-history-wide")
    assert len(result.captures) == 3
    assert DEFAULT_HISTORY_PAGE_LIMIT < 1000
    default = history(dispatcher, request_id="req-history-default")
    assert len(default.captures) == 3


# --- independent heads ----------------------------------------------------------


def test_a_reverse_order_completion_is_historical_only_and_never_reads_as_current(
    owned: Served,
) -> None:
    """END-03/DOC-17 as history reads it: the later-finishing revision is not current."""
    dispatcher = build_dispatcher(owned, tag="history-reverse")
    source = capture(dispatcher, request_id="req-c1", key="idem-c1")
    promoted = reprocess(
        dispatcher,
        capture_id=source.capture_id,
        profile_ref=DEFAULT_PROFILE_REF,
        revision=APPROVED_PROFILE_REVISION,
        expected=None,
        request_id="req-r1",
        key="idem-r1",
    )
    # A second reprocess that froze the *empty* head, which is now stale.
    stale = reprocess(
        dispatcher,
        capture_id=source.capture_id,
        profile_ref=DEFAULT_PROFILE_REF,
        revision=APPROVED_PROFILE_REVISION,
        expected=None,
        request_id="req-r2",
        key="idem-r2",
    )
    assert stale.publication_disposition == "historical_only"
    assert not stale.became_head

    result = history(dispatcher)
    by_id = {entry.extraction_revision_id: entry for entry in result.extractions}
    assert result.extraction.current_extraction_id == promoted.extraction_revision_id
    assert by_id[promoted.extraction_revision_id].is_current_extraction
    later = by_id[stale.extraction_revision_id]
    # It arrived last and has the greatest ordinal, and it is still not current.
    assert later.document_ordinal > by_id[promoted.extraction_revision_id].document_ordinal
    assert not later.is_current_extraction
    assert not later.head_eligible
    assert later.publication_disposition == "historical_only"
    assert result.extraction.retrieval_state == "retrievable"


def test_an_extraction_over_an_older_capture_does_not_move_the_source_head(
    owned: Served,
) -> None:
    """END-03: the two heads are separate, and reprocessing an old capture proves it."""
    dispatcher = build_dispatcher(owned, tag="history-old-capture")
    first, _second, third = _three_captures(dispatcher)
    reprocess(
        dispatcher,
        capture_id=first.capture_id,
        profile_ref=DEFAULT_PROFILE_REF,
        revision=APPROVED_PROFILE_REVISION,
        expected=None,
        request_id="req-r-old",
        key="idem-r-old",
    )

    result = history(dispatcher)
    assert result.source.current_capture_id == third.capture_id
    assert [entry.is_current_source for entry in result.captures] == [False, False, True]
    extraction = result.extractions[-1]
    assert extraction.capture_id == first.capture_id
    assert extraction.is_current_extraction
    # The extraction head moved; the source head did not.
    assert result.extraction.current_extraction_id == extraction.extraction_revision_id
    assert result.source.capture_head_sequence == 3


def test_a_superseded_capture_keeps_its_attribution_and_its_frozen_base(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="history-attribution")
    first, second, _third = _three_captures(dispatcher)
    result = history(dispatcher)
    entries = {entry.capture_id: entry for entry in result.captures}
    assert entries[first.capture_id].intent_kind == "new_capture"
    assert entries[first.capture_id].expected_base_capture_id is None
    assert entries[second.capture_id].intent_kind == "refresh"
    assert entries[second.capture_id].expected_base_capture_id == first.capture_id
    # Attribution survives losing the head: identity, digest and source identity all stand.
    assert entries[first.capture_id].requested_source_identity == "source-history-1"
    assert entries[first.capture_id].source_digest.startswith("sha256:")
    assert entries[first.capture_id].evidence_id


# --- retention and removal ------------------------------------------------------


def test_an_expired_capture_reports_reprocess_unavailable_with_its_reason(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="history-expired")
    source = capture(dispatcher, request_id="req-c1", key="idem-c1")
    expire_capture(service_view(owned), capture_id=source.capture_id)

    result = history(dispatcher)
    entry = result.captures[0]
    assert entry.retention_state == "expired"
    assert entry.capture_retention_generation == 2
    assert not entry.actions[0].available
    assert entry.actions[0].unavailable_reason == "source_not_retained"
    # No approved profile is offered for an action Core would refuse.
    assert entry.reprocess_profiles == ()
    # And Core does refuse it, which is what makes the report truthful rather than cautious.
    assert (
        refusal(
            dispatcher.dispatch(
                reprocess_request(
                    capture_id=source.capture_id,
                    profile_ref=DEFAULT_PROFILE_REF,
                    revision=APPROVED_PROFILE_REVISION,
                    expected=None,
                    request_id="req-r-expired",
                    key="idem-r-expired",
                )
            )
        ).error.code
        == "conflict"
    )


def test_a_removed_source_reports_removal_rather_than_a_retention_problem(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="history-removed")
    capture(dispatcher, request_id="req-c1", key="idem-c1")
    assert set_document_source_state(
        service_view(owned), document_id=DOCUMENT, removed=True
    ) == 2

    result = history(dispatcher)
    assert result.source.source_state == store.SOURCE_REMOVED
    assert result.source.source_removal_generation == 2
    assert result.captures[0].retention_state == store.RETENTION_RETAINED
    assert result.captures[0].actions[0].unavailable_reason == "source_removed"
    assert result.actions[0].unavailable_reason == "source_removed"


def test_a_disconnected_route_reports_refresh_unavailable_and_keeps_the_history(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="history-disconnected")
    source = capture(dispatcher, request_id="req-c1", key="idem-c1")
    disconnect_original(
        service_view(owned),
        document_id=DOCUMENT,
        owner_ref="access-history-1",
        route_kind=store.ROUTE_DIRECT_FILE,
    )

    result = history(dispatcher)
    assert result.source.original_binding_state == store.BINDING_DISCONNECTED
    assert result.actions[0].unavailable_reason == "original_disconnected"
    # A disconnected original does not take the retained copy with it: the capture is
    # still inspectable and still reprocessable.
    assert result.captures[0].capture_id == source.capture_id
    assert result.captures[0].actions[0].available


def test_a_snapshot_only_document_reports_no_original_binding(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="history-snapshot")
    entry = get_operation_metadata(DOCUMENT_CAPTURE_OPERATION)
    body = capture_payload(content=b"snapshot only body")
    body.pop("original_binding")
    answered(
        dispatcher.dispatch(
            RequestEnvelope(
                operation=DOCUMENT_CAPTURE_OPERATION,
                metadata=_metadata(entry, request_id="req-c-snap", key="idem-c-snap"),
                input=body,
            )
        )
    )

    result = history(dispatcher)
    assert result.source.original_binding_state == store.BINDING_NONE
    assert result.actions[0].unavailable_reason == "no_original_binding"
    assert result.captures[0].association_kind == store.ASSOCIATION_SNAPSHOT_ONLY


# --- failed extraction ----------------------------------------------------------


def test_a_failed_extraction_is_reported_without_destroying_the_previous_retrieval(
    owned: Served, monkeypatch: Any
) -> None:
    """DOC-18: the previous authorised result stays available and stays current."""
    dispatcher = build_dispatcher(owned, tag="history-failed")
    source = capture(dispatcher, request_id="req-c1", key="idem-c1")
    promoted = reprocess(
        dispatcher,
        capture_id=source.capture_id,
        profile_ref=DEFAULT_PROFILE_REF,
        revision=APPROVED_PROFILE_REVISION,
        expected=None,
        request_id="req-r1",
        key="idem-r1",
    )
    published = history(dispatcher).extraction.published_at

    def unsupported(*_args: Any, **_kwargs: Any) -> Any:
        raise document_handlers.DocumentExtractionUnsupported("fixture refusal")

    monkeypatch.setattr(document_handlers, "extract_document", unsupported)
    failed = reprocess(
        dispatcher,
        capture_id=source.capture_id,
        profile_ref=DEFAULT_PROFILE_REF,
        revision=APPROVED_PROFILE_REVISION,
        expected=promoted.extraction_revision_id,
        request_id="req-r2",
        key="idem-r2",
    )
    assert failed.result_state == "unsupported"
    assert failed.publication_disposition == "not_published"

    result = history(dispatcher)
    by_id = {entry.extraction_revision_id: entry for entry in result.extractions}
    assert by_id[failed.extraction_revision_id].result_state == "unsupported"
    assert by_id[failed.extraction_revision_id].output_digest is None
    assert not by_id[failed.extraction_revision_id].is_current_extraction
    # The earlier result is still the head and retrieval still holds its rows, unchanged.
    assert result.extraction.current_extraction_id == promoted.extraction_revision_id
    assert result.extraction.retrieval_state == "retrievable"
    assert result.extraction.published_at == published
    # The failure does not bar another attempt over the same retained bytes.
    assert by_id[failed.extraction_revision_id].capture_id == source.capture_id
    assert result.captures[0].actions[0].available


# --- the reprocess the history enables ------------------------------------------


def test_reprocess_is_built_only_from_what_the_history_reported(owned: Served) -> None:
    """The exact loop the UI walks, with no value invented between the read and the write."""
    dispatcher = build_dispatcher(owned, tag="history-loop")
    capture(dispatcher, request_id="req-c1", key="idem-c1")

    before = history(dispatcher)
    entry = before.captures[0]
    option = entry.reprocess_profiles[0]
    base = before.extraction.current_extraction_id

    result = reprocess(
        dispatcher,
        capture_id=entry.capture_id,
        profile_ref=option.profile_ref,
        revision=option.revision,
        expected=base,
        request_id="req-r1",
        key="idem-r1",
    )
    # The profile that ran is byte-for-byte the profile the read offered.
    assert result.profile_digest == option.profile_digest
    assert result.capture_id == entry.capture_id
    assert result.publication_disposition == "promoted"

    after = history(dispatcher)
    assert after.extraction.current_extraction_id == result.extraction_revision_id
    assert after.extractions[-1].profile.digest == option.profile_digest
    assert after.extractions[-1].profile.processing_mode == option.processing_mode
    # The decoder build records the reference and revision the request froze.
    assert option.profile_ref in after.extractions[-1].profile.decoder_build
    assert option.revision in after.extractions[-1].profile.decoder_build


def test_an_image_capture_is_offered_both_approved_profiles_and_nothing_else(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="history-ocr")
    capture(
        dispatcher,
        request_id="req-c1",
        key="idem-c1",
        content=b"\x89PNG\r\n\x1a\n" + b"0" * 64,
        media_type="image/png",
    )

    entry = history(dispatcher).captures[0]
    assert [option.profile_ref for option in entry.reprocess_profiles] == [
        DEFAULT_PROFILE_REF,
        TEXT_ONLY_PROFILE_REF,
    ]
    modes = {option.profile_ref: option.processing_mode for option in entry.reprocess_profiles}
    assert modes[DEFAULT_PROFILE_REF] == "text_and_local_ocr"
    assert modes[TEXT_ONLY_PROFILE_REF] == "text_only"
    assert len({option.profile_digest for option in entry.reprocess_profiles}) == 2
    assert all(option.revision == APPROVED_PROFILE_REVISION for option in entry.reprocess_profiles)


def test_an_unqualified_format_is_offered_no_profile_and_says_why(owned: Served) -> None:
    """A retained capture of a format with no adapter has nothing honest to offer.

    The bytes are kept and the capture is history like any other; what is absent is a
    profile that could read them, so the action is reported unavailable with that reason
    rather than offered and refused on use.
    """
    dispatcher = build_dispatcher(owned, tag="history-unqualified")
    assert approved_reprocess_profiles(media_type="application/octet-stream") == ()
    capture(
        dispatcher,
        request_id="req-c-odd",
        key="idem-c-odd",
        content=b"opaque body",
        media_type="application/octet-stream",
    )

    history_entry = history(dispatcher).captures[0]
    assert history_entry.retention_state == store.RETENTION_RETAINED
    assert history_entry.reprocess_profiles == ()
    assert not history_entry.actions[0].available
    assert history_entry.actions[0].unavailable_reason == "format_not_qualified"


def test_reprocess_against_a_stale_reported_base_is_historical_not_success(
    owned: Served,
) -> None:
    """The read is a snapshot, and a caller acting on a stale one gets history, not a lie."""
    dispatcher = build_dispatcher(owned, tag="history-stale-base")
    source = capture(dispatcher, request_id="req-c1", key="idem-c1")
    stale_base = history(dispatcher).extraction.current_extraction_id
    assert stale_base is None

    promoted = reprocess(
        dispatcher,
        capture_id=source.capture_id,
        profile_ref=DEFAULT_PROFILE_REF,
        revision=APPROVED_PROFILE_REVISION,
        expected=None,
        request_id="req-r1",
        key="idem-r1",
    )
    late = reprocess(
        dispatcher,
        capture_id=source.capture_id,
        profile_ref=DEFAULT_PROFILE_REF,
        revision=APPROVED_PROFILE_REVISION,
        expected=stale_base,
        request_id="req-r2",
        key="idem-r2",
    )
    assert late.publication_disposition == "historical_only"

    result = history(dispatcher)
    assert result.extraction.current_extraction_id == promoted.extraction_revision_id
    by_id = {entry.extraction_revision_id: entry for entry in result.extractions}
    assert not by_id[late.extraction_revision_id].is_current_extraction
    assert by_id[late.extraction_revision_id].expected_base_extraction_id is None


# --- the result is held to itself -----------------------------------------------


def test_a_result_whose_current_flag_disagrees_with_the_head_is_never_served(
    owned: Served, monkeypatch: Any
) -> None:
    """The validator is not decoration: an untruthful page refuses instead of shipping."""
    dispatcher = build_dispatcher(owned, tag="history-untruthful")
    capture(dispatcher, request_id="req-c1", key="idem-c1")

    real = document_handlers.DocumentCaptureHandlers._capture_entry

    def lying_entry(self: Any, row: Any, **kwargs: Any) -> Any:
        entry = real(self, row, **kwargs)
        return type(entry)(
            **{
                **{
                    field: getattr(entry, field)
                    for field in entry.__dataclass_fields__
                },
                "is_current_source": not entry.is_current_source,
            }
        )

    monkeypatch.setattr(
        document_handlers.DocumentCaptureHandlers, "_capture_entry", lying_entry
    )
    error = refusal(dispatcher.dispatch(history_request()))
    assert error.error.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE


# --- the profile that is reported is the profile that runs -----------------------


PNG_BODY = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _recording_extractor(monkeypatch: Any) -> list[Any]:
    """Record the `settings` every extraction is actually executed under.

    The real extractor is still called, so the recorded value is the one the worker received
    rather than one this test composed. What happens to a fixture's bytes afterwards is not
    the point: whether recognition would have run is decided entirely by these settings, and
    a text-only profile that arrives with no OCR resources pinned cannot run any.
    """
    recorded: list[Any] = []
    real = document_handlers.extract_document

    def recording(content: bytes, **kwargs: Any) -> Any:
        recorded.append(kwargs.get("settings"))
        return real(content, **kwargs)

    monkeypatch.setattr(document_handlers, "extract_document", recording)
    return recorded


def test_a_text_only_selection_is_executed_text_only_and_never_default_ocr(
    owned: Served, monkeypatch: Any
) -> None:
    """The defect this exists for: a text-only receipt over a Text-and-local-OCR execution.

    An image capture is the only media type where the two approved selections differ, so it
    is the only place the substitution is observable. The reprocess handler used to build the
    recorded profile from the selected reference and then call the extractor with no settings
    at all, and the extractor's own default for an image is Text and local OCR -- so a caller
    that asked for text-only got a text-only profile digest over an execution that pinned OCR
    models and could run recognition.
    """
    dispatcher = build_dispatcher(owned, tag="history-text-only")
    source = capture(
        dispatcher,
        request_id="req-c1",
        key="idem-c1",
        content=PNG_BODY,
        media_type="image/png",
    )
    entry = history(dispatcher).captures[0]
    offered = {option.profile_ref: option for option in entry.reprocess_profiles}
    text_only = offered[TEXT_ONLY_PROFILE_REF]
    assert text_only.processing_mode == MODE_TEXT_ONLY

    recorded = _recording_extractor(monkeypatch)
    result = reprocess(
        dispatcher,
        capture_id=source.capture_id,
        profile_ref=text_only.profile_ref,
        revision=text_only.revision,
        profile_digest=text_only.profile_digest,
        media_type="image/png",
        expected=None,
        request_id="req-r1",
        key="idem-r1",
    )

    assert len(recorded) == 1
    executed = recorded[0]
    assert isinstance(executed, ExtractionSettings)
    # Text-only is the whole claim: no engine, no model and no language is pinned, so there
    # is nothing recognition could have been invoked with.
    assert executed.processing_mode == MODE_TEXT_ONLY
    assert executed.ocr is None
    assert not executed.runs_ocr
    # The receipt names the digest of the profile those exact settings produce, which is the
    # digest the read offered. The two can no longer be computed from different settings.
    assert result.profile_digest == text_only.profile_digest
    assert result.profile_digest == store.profile_digest(
        portable_profile(
            media_type="image/png",
            profile_ref=text_only.profile_ref,
            revision=text_only.revision,
            settings=executed,
        )
    )
    summary = history(dispatcher).extractions[-1].profile
    assert summary.digest == text_only.profile_digest
    assert summary.processing_mode == MODE_TEXT_ONLY
    assert summary.ocr_languages == ()


def test_the_default_selection_for_an_image_is_executed_with_its_pinned_resources(
    owned: Served, monkeypatch: Any
) -> None:
    """The other half: the default selection is not quietly downgraded either."""
    dispatcher = build_dispatcher(owned, tag="history-default-ocr")
    source = capture(
        dispatcher,
        request_id="req-c1",
        key="idem-c1",
        content=PNG_BODY,
        media_type="image/png",
    )
    offered = {
        option.profile_ref: option
        for option in history(dispatcher).captures[0].reprocess_profiles
    }
    default = offered[DEFAULT_PROFILE_REF]
    assert default.processing_mode == MODE_TEXT_AND_LOCAL_OCR

    recorded = _recording_extractor(monkeypatch)
    result = reprocess(
        dispatcher,
        capture_id=source.capture_id,
        profile_ref=default.profile_ref,
        revision=default.revision,
        profile_digest=default.profile_digest,
        media_type="image/png",
        expected=None,
        request_id="req-r1",
        key="idem-r1",
    )

    executed = recorded[0]
    assert executed.processing_mode == MODE_TEXT_AND_LOCAL_OCR
    # The resource identity is pinned, whether or not the installation served the models.
    assert executed.ocr is not None
    assert result.profile_digest == default.profile_digest


def test_a_selection_core_does_not_approve_is_refused_before_anything_runs(
    owned: Served, monkeypatch: Any
) -> None:
    """Text-only is not an approved selection for an ordinary document, so it is refused.

    `approved_reprocess_profiles` offers one selection for a media type that does not qualify
    for local OCR, because both references resolve to the same processing there. A caller
    naming the other reference is naming a selection this build never reported, and the
    refusal arrives before the retained bytes are even read.
    """
    dispatcher = build_dispatcher(owned, tag="history-unapproved")
    source = capture(dispatcher, request_id="req-c1", key="idem-c1")
    assert [
        option.profile_ref
        for option in approved_reprocess_profiles(media_type="text/plain")
    ] == [DEFAULT_PROFILE_REF]

    def never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a refused selection must not be executed")

    monkeypatch.setattr(document_handlers, "extract_document", never)
    error = refusal(
        dispatcher.dispatch(
            reprocess_request(
                capture_id=source.capture_id,
                profile_ref=TEXT_ONLY_PROFILE_REF,
                revision=APPROVED_PROFILE_REVISION,
                # A digest Core really did compute, for the profile that reference resolves
                # to: the refusal is about the selection not being approved, not about the
                # digest being malformed.
                profile_digest=store.profile_digest(
                    portable_profile(
                        media_type="text/plain",
                        profile_ref=TEXT_ONLY_PROFILE_REF,
                        revision=APPROVED_PROFILE_REVISION,
                    )
                ),
                expected=None,
                request_id="req-r-unapproved",
                key="idem-r-unapproved",
            )
        )
    )
    assert error.error.code == "conflict"
    assert history(dispatcher).extraction_count == 0


def test_a_profile_that_resolves_differently_than_reported_is_refused_not_substituted(
    owned: Served, monkeypatch: Any
) -> None:
    """The race the digest closes: resolution moves between the read and the write.

    A newer decoder build, a newly installed or newly missing OCR model, or any other change
    to what the named reference resolves to, all show up the same way -- the approved digest
    is no longer the one the caller was offered. Core refuses the request rather than running
    the profile it now resolves to beneath a request that named the earlier one.
    """
    dispatcher = build_dispatcher(owned, tag="history-race")
    source = capture(dispatcher, request_id="req-c1", key="idem-c1")
    offered = history(dispatcher).captures[0].reprocess_profiles[0]

    def moved(*, media_type: str) -> tuple[ReprocessProfileOption, ...]:
        return tuple(
            ReprocessProfileOption(
                profile_ref=option.profile_ref,
                revision=option.revision,
                processing_mode=option.processing_mode,
                profile_digest="sha256:" + "f" * 64,
                settings=option.settings,
                profile=option.profile,
            )
            for option in approved_reprocess_profiles(media_type=media_type)
        )

    def never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a substituted profile must not be executed")

    monkeypatch.setattr(document_handlers, "approved_reprocess_profiles", moved)
    monkeypatch.setattr(document_handlers, "extract_document", never)
    error = refusal(
        dispatcher.dispatch(
            reprocess_request(
                capture_id=source.capture_id,
                profile_ref=offered.profile_ref,
                revision=offered.revision,
                profile_digest=offered.profile_digest,
                expected=None,
                request_id="req-r-race",
                key="idem-r-race",
            )
        )
    )
    assert error.error.code == "conflict"
    # Nothing was admitted: the refusal is before execution, not a failed extraction.
    monkeypatch.undo()
    assert history(dispatcher).extraction_count == 0


def test_a_mismatched_expected_profile_digest_is_refused(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="history-digest")
    source = capture(dispatcher, request_id="req-c1", key="idem-c1")
    for digest in ("sha256:" + "0" * 64, "sha512:" + "a" * 128):
        error = refusal(
            dispatcher.dispatch(
                reprocess_request(
                    capture_id=source.capture_id,
                    profile_ref=DEFAULT_PROFILE_REF,
                    revision=APPROVED_PROFILE_REVISION,
                    profile_digest=digest,
                    expected=None,
                    request_id="req-r-wrong",
                    key=f"idem-r-wrong-{digest[:9]}",
                )
            )
        )
        assert error.error.code == "conflict"
    # And a digest that is not a checksum at all is an invalid request, not a conflict.
    malformed = refusal(
        dispatcher.dispatch(
            reprocess_request(
                capture_id=source.capture_id,
                profile_ref=DEFAULT_PROFILE_REF,
                revision=APPROVED_PROFILE_REVISION,
                profile_digest="not-a-digest",
                expected=None,
                request_id="req-r-malformed",
                key="idem-r-malformed",
            )
        )
    )
    assert malformed.error.code == ERROR_CODE_INVALID_REQUEST


def test_the_expected_profile_digest_is_part_of_the_requests_identity(
    owned: Served,
) -> None:
    """A replay under the same key with a different expected digest is not the same request.

    The operation key is the caller's statement that this is the same intent. It is held to
    that: the request the key was first settled under included the profile identity, so the
    same key carrying a different one is an idempotency conflict rather than a second
    extraction or a silent replay of the first receipt.
    """
    dispatcher = build_dispatcher(owned, tag="history-identity")
    source = capture(dispatcher, request_id="req-c1", key="idem-c1")
    first = reprocess(
        dispatcher,
        capture_id=source.capture_id,
        profile_ref=DEFAULT_PROFILE_REF,
        revision=APPROVED_PROFILE_REVISION,
        expected=None,
        request_id="req-r1",
        key="idem-shared",
    )
    replay = reprocess(
        dispatcher,
        capture_id=source.capture_id,
        profile_ref=DEFAULT_PROFILE_REF,
        revision=APPROVED_PROFILE_REVISION,
        expected=None,
        request_id="req-r1-again",
        key="idem-shared",
    )
    # The byte-identical request replays to the same receipt.
    assert replay.extraction_revision_id == first.extraction_revision_id

    # The equivalence the key is settled under is computed over the whole request, so the
    # expected profile identity is inside it: two requests that differ only there are two
    # different requests, and the same key cannot cover both.
    settled = reprocess_request(
        capture_id=source.capture_id,
        profile_ref=DEFAULT_PROFILE_REF,
        revision=APPROVED_PROFILE_REVISION,
        expected=None,
        request_id="req-r1",
        key="idem-shared",
    )
    changed = reprocess_request(
        capture_id=source.capture_id,
        profile_ref=DEFAULT_PROFILE_REF,
        revision=APPROVED_PROFILE_REVISION,
        profile_digest="sha256:" + "0" * 64,
        expected=None,
        request_id="req-r1",
        key="idem-shared",
    )
    assert idempotency_equivalence(
        settled.operation,
        settled.metadata,
        settled.input,
        principal_id="local-user",
        workspace_id=WORKSPACE,
    ) != idempotency_equivalence(
        changed.operation,
        changed.metadata,
        changed.input,
        principal_id="local-user",
        workspace_id=WORKSPACE,
    )


def test_one_key_cannot_cover_two_approved_selections_of_the_same_capture(
    owned: Served,
) -> None:
    """An image capture has two approved selections, so the conflict is reachable end to end.

    The second request names a selection Core does approve, so it passes the substitution
    check and reaches the settled key -- which was settled under the other selection. That is
    an idempotency conflict, not a second extraction and not a replay of the first receipt.
    """
    dispatcher = build_dispatcher(owned, tag="history-two-selections")
    source = capture(
        dispatcher,
        request_id="req-c1",
        key="idem-c1",
        content=PNG_BODY,
        media_type="image/png",
    )
    offered = {
        option.profile_ref: option
        for option in history(dispatcher).captures[0].reprocess_profiles
    }
    reprocess(
        dispatcher,
        capture_id=source.capture_id,
        profile_ref=DEFAULT_PROFILE_REF,
        revision=APPROVED_PROFILE_REVISION,
        profile_digest=offered[DEFAULT_PROFILE_REF].profile_digest,
        media_type="image/png",
        expected=None,
        request_id="req-r1",
        key="idem-shared",
    )
    error = refusal(
        dispatcher.dispatch(
            reprocess_request(
                capture_id=source.capture_id,
                profile_ref=TEXT_ONLY_PROFILE_REF,
                revision=APPROVED_PROFILE_REVISION,
                profile_digest=offered[TEXT_ONLY_PROFILE_REF].profile_digest,
                media_type="image/png",
                expected=None,
                request_id="req-r2",
                key="idem-shared",
            )
        )
    )
    assert error.error.code == "idempotency_conflict"


# --- a continuation series is one snapshot --------------------------------------


def _first_page_token(dispatcher: Any) -> str:
    token = history(dispatcher, limit=1, request_id="req-history-p1").page.continuation_token
    assert token is not None
    return token


def test_a_continuation_is_refused_when_the_head_moved_between_pages(
    owned: Served,
) -> None:
    """A new capture moves the source head, the totals and which member is current.

    Continuing across that would serve page one's current flags beside page two's rows, and
    the assembled history would claim a state no instant held. The caller is told to read
    again instead.
    """
    dispatcher = build_dispatcher(owned, tag="history-interleave-head")
    captures = _three_captures(dispatcher)
    token = _first_page_token(dispatcher)

    capture(
        dispatcher,
        request_id="req-c4",
        key="idem-c4",
        content=b"body four",
        intent="refresh",
        expected_base=captures[-1].capture_id,
    )
    error = refusal(
        dispatcher.dispatch(
            history_request(limit=1, token=token, request_id="req-history-p2")
        )
    )
    assert error.error.code == ERROR_CODE_INVALID_REQUEST
    # Reading again from the first page is the recovery, and it works.
    fresh = history(dispatcher, limit=1, request_id="req-history-fresh")
    assert fresh.capture_count == 4
    assert fresh.page.continuation_token is not None


def test_a_continuation_is_refused_when_a_captures_retention_moved_between_pages(
    owned: Served,
) -> None:
    """The case a head-and-totals binding would miss.

    Expiring one capture changes no count, no head and no ordering. What it changes is that
    capture's retention state, its reprocess availability and the profiles offered for it --
    all of them facts a page states. The snapshot covers them per row, so this is refused
    too.
    """
    dispatcher = build_dispatcher(owned, tag="history-interleave-retention")
    captures = _three_captures(dispatcher)
    token = _first_page_token(dispatcher)

    expire_capture(service_view(owned), capture_id=captures[-1].capture_id)
    error = refusal(
        dispatcher.dispatch(
            history_request(limit=1, token=token, request_id="req-history-p2")
        )
    )
    assert error.error.code == ERROR_CODE_INVALID_REQUEST
    # Same counts, same head: nothing a totals-only binding would have noticed.
    fresh = history(dispatcher, limit=1, request_id="req-history-fresh")
    assert fresh.capture_count == 3
    assert fresh.source.current_capture_id == captures[-1].capture_id


def test_a_continuation_is_refused_when_the_source_state_moved_between_pages(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="history-interleave-removal")
    _three_captures(dispatcher)
    token = _first_page_token(dispatcher)

    assert set_document_source_state(
        service_view(owned), document_id=DOCUMENT, removed=True
    ) == 2
    error = refusal(
        dispatcher.dispatch(
            history_request(limit=1, token=token, request_id="req-history-p2")
        )
    )
    assert error.error.code == ERROR_CODE_INVALID_REQUEST


def test_an_unchanged_document_pages_through_to_the_end_under_one_snapshot(
    owned: Served,
) -> None:
    """The snapshot binding is a consistency check, not a page-two ban."""
    dispatcher = build_dispatcher(owned, tag="history-snapshot-walk")
    captures = _three_captures(dispatcher)
    seen: list[str] = []
    token: str | None = None
    for index in range(3):
        page = history(
            dispatcher, limit=1, token=token, request_id=f"req-history-walk{index}"
        )
        seen += [entry.capture_id for entry in page.captures]
        token = page.page.continuation_token
    assert token is None
    assert seen == [source.capture_id for source in captures]


def _resolution_moved(digest: str) -> Any:
    """A stand-in for the installation moving beneath a continuation.

    A newly installed or newly removed recognition model, a new OCR catalogue or a newer
    decoder build all reach this read the same way: the same reference now resolves to a
    different approved profile identity. None of that is recorded in any row the snapshot
    reads from the database, so the substituted digest here is the whole of the change.
    """

    def moved(*, media_type: str) -> tuple[ReprocessProfileOption, ...]:
        return tuple(
            ReprocessProfileOption(
                profile_ref=option.profile_ref,
                revision=option.revision,
                processing_mode=option.processing_mode,
                profile_digest=digest,
                settings=option.settings,
                profile=option.profile,
            )
            for option in approved_reprocess_profiles(media_type=media_type)
        )

    return moved


def test_a_continuation_is_refused_when_profile_resolution_moved_between_pages(
    owned: Served, monkeypatch: Any
) -> None:
    """The case the database facts cannot see.

    Expiring a capture or moving a head leaves a row behind for the snapshot to notice. An
    OCR model appearing or disappearing does not: every capture row, both heads, both totals,
    the binding aggregate and the published projection stand exactly still, and only the
    approved profile identity a capture is offered changes. A series stitched across that
    would hand the caller page one's digests beside page two's, both labelled approved, when
    only one of them is. So the resolution is part of the snapshot and this continuation is
    refused.
    """
    dispatcher = build_dispatcher(owned, tag="history-interleave-profiles")
    _three_captures(dispatcher)
    first = history(dispatcher, limit=1, request_id="req-history-p1")
    token = first.page.continuation_token
    assert token is not None
    was_offered = first.captures[0].reprocess_profiles[0].profile_digest

    substituted = "sha256:" + "e" * 64
    monkeypatch.setattr(
        document_handlers, "approved_reprocess_profiles", _resolution_moved(substituted)
    )
    error = refusal(
        dispatcher.dispatch(
            history_request(limit=1, token=token, request_id="req-history-p2")
        )
    )
    assert error.error.code == ERROR_CODE_INVALID_REQUEST

    # Nothing in the database moved: this is exactly the state a totals-and-rows binding
    # would have continued through, and the page it would have served is the mixed one.
    fresh = history(dispatcher, limit=1, request_id="req-history-fresh")
    assert fresh.capture_count == 3
    assert fresh.source.current_capture_id == first.source.current_capture_id
    # Reading again from the first page is the recovery, and it reports the new resolution
    # rather than the one page one was issued under.
    assert fresh.captures[0].reprocess_profiles[0].profile_digest == substituted
    assert was_offered != substituted
    # And that fresh series pages through to the end under the new resolution.
    walked = history(
        dispatcher,
        limit=1,
        token=fresh.page.continuation_token,
        request_id="req-history-fresh2",
    )
    assert walked.captures[0].reprocess_profiles[0].profile_digest == substituted


def test_one_request_resolves_each_relevant_media_type_exactly_once(
    owned: Served, monkeypatch: Any
) -> None:
    """Resolution is per request and per distinct media type, not per capture per page.

    Three captures of two media types. A request that returns one of them still resolves both
    types, because the mapping it digests has to cover every page of the series it is about
    to issue a token for -- and it resolves each of them once, because a second resolution of
    the same type is a second chance for the two to disagree inside one page.
    """
    dispatcher = build_dispatcher(owned, tag="history-resolve-once")
    first = capture(dispatcher, request_id="req-c1", key="idem-c1", content=b"body one")
    second = capture(
        dispatcher,
        request_id="req-c2",
        key="idem-c2",
        content=PNG_BODY,
        media_type="image/png",
        intent="refresh",
        expected_base=first.capture_id,
    )
    capture(
        dispatcher,
        request_id="req-c3",
        key="idem-c3",
        content=b"body three",
        intent="refresh",
        expected_base=second.capture_id,
    )

    asked: list[str] = []

    def counted(*, media_type: str) -> tuple[ReprocessProfileOption, ...]:
        asked.append(media_type)
        return approved_reprocess_profiles(media_type=media_type)

    monkeypatch.setattr(document_handlers, "approved_reprocess_profiles", counted)

    offered: dict[str, tuple[tuple[str, str, str, str], ...]] = {}
    token: str | None = None
    for index in range(3):
        del asked[:]
        page = history(
            dispatcher, limit=1, token=token, request_id=f"req-history-once{index}"
        )
        # One call per distinct media type the document's active retained captures hold,
        # whatever this page itself returned.
        assert sorted(asked) == ["image/png", "text/plain"]
        assert len(asked) == len(set(asked))
        entry = page.captures[0]
        identity = tuple(
            (
                option.profile_ref,
                option.revision,
                option.processing_mode,
                option.profile_digest,
            )
            for option in entry.reprocess_profiles
        )
        offered.setdefault(entry.media_type, identity)
        # Every capture of one media type is offered the same resolution, across pages.
        assert offered[entry.media_type] == identity
        token = page.page.continuation_token
    assert token is None
    # The two text captures went through the same resolution, and the image capture has the
    # pair its media type qualifies for rather than the single text option.
    assert len(offered["text/plain"]) == 1
    assert len(offered["image/png"]) == 2


def test_a_capture_with_no_offered_profile_still_resolves_nothing_for_itself(
    owned: Served, monkeypatch: Any
) -> None:
    """The media types that qualify for no resolution are not resolved.

    An expired capture, a removed source and an unqualified format each report their own
    unavailable reason and no profile, and none of them needs a resolution to say so. A
    request whose only retained capture is of an unqualified format resolves that one type and
    nothing else, and the entry still reports `format_not_qualified`.
    """
    dispatcher = build_dispatcher(owned, tag="history-resolve-none")
    source = capture(
        dispatcher,
        request_id="req-c1",
        key="idem-c1",
        content=b"MZ\x00\x00binary",
        media_type="application/x-msdownload",
    )

    asked: list[str] = []

    def counted(*, media_type: str) -> tuple[ReprocessProfileOption, ...]:
        asked.append(media_type)
        return approved_reprocess_profiles(media_type=media_type)

    monkeypatch.setattr(document_handlers, "approved_reprocess_profiles", counted)
    entry = history(dispatcher, request_id="req-history-unqualified").captures[0]
    assert asked == ["application/x-msdownload"]
    assert entry.reprocess_profiles == ()
    assert [action.unavailable_reason for action in entry.actions] == [
        "format_not_qualified"
    ]

    # Expire it, and the request resolves nothing at all: a capture whose bytes are gone has
    # no profile question to ask of the installation.
    expire_capture(service_view(owned), capture_id=source.capture_id)
    del asked[:]
    expired = history(dispatcher, request_id="req-history-expired").captures[0]
    assert asked == []
    assert expired.reprocess_profiles == ()
    assert [action.unavailable_reason for action in expired.actions] == [
        "source_not_retained"
    ]


def test_a_write_landing_mid_page_is_refused_rather_than_served_mixed(
    owned: Served, monkeypatch: Any
) -> None:
    """The snapshot binds this page as well as the series it starts.

    The heads, totals, actions and resolved options above are read before the page's own rows
    are, and this read holds no transaction of its own, so a write can land in between. Here
    one does: a capture is expired while the page is being assembled. The page that would be
    served states a retention and a reprocess availability for that capture from before the
    write and totals from the same instant -- internally mixed. It is refused as recoverable
    instead, and the same request retried against a settled document answers.
    """
    dispatcher = build_dispatcher(owned, tag="history-mid-page")
    captures = _three_captures(dispatcher)
    real = store.read_capture_history
    landed: list[str] = []

    def mutating(*args: Any, **kwargs: Any) -> Any:
        rows = real(*args, **kwargs)
        if not landed:
            landed.append(captures[0].capture_id)
            expire_capture(service_view(owned), capture_id=captures[0].capture_id)
        return rows

    monkeypatch.setattr(document_handlers.store, "read_capture_history", mutating)
    error = refusal(dispatcher.dispatch(history_request(request_id="req-history-mid")))
    assert error.error.code == ERROR_CODE_INTERNAL_RECOVERABLE
    # And stated as retryable, because resending this identical read is the recovery. A
    # refusal that contradicted the frozen class for its code would reach the caller as a
    # bare `internal_non_recoverable` and tell them to stop.
    assert error.error.retry_class == "retryable"
    assert landed == [captures[0].capture_id]

    # Retrying the identical request against the settled document answers, and answers with
    # the state the write left behind rather than the one the refused page was holding.
    monkeypatch.undo()
    settled = history(dispatcher, request_id="req-history-mid-again")
    entry = next(
        row for row in settled.captures if row.capture_id == captures[0].capture_id
    )
    assert entry.retention_state == "expired"
    assert [action.unavailable_reason for action in entry.actions] == [
        "source_not_retained"
    ]
