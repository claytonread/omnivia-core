"""AT-32 and AT-33 as Core stores and serves them.

AT-32: a refresh that fails after a previous success leaves the prior capture available, and
the failure is shown separately. AT-33: identical bytes whose evidence is no longer retained
are not read as "already up to date"; the retention decision is respected.

Every assertion is on a stored or served fact: the validated `document.history` result, or
the rows `storage.document_intake` reads back.
"""

from __future__ import annotations

import dataclasses
from threading import RLock
from typing import Any

from omnivia_core_runtime.service.document_intake import (
    capture_document_bytes,
    expire_capture,
    owned_workspace,
    set_document_source_state,
)
from omnivia_core_runtime.service.document_processing import process_capture_outbox
from omnivia_core_runtime.service.versions import SERVER_VERSION
from omnivia_core_runtime.storage import document_intake as store
from test_document_history_vertical import (
    DOCUMENT,
    answered,
    build_dispatcher,
    capture,
    capture_payload,
    history,
    refusal,
    service_view,
)
from test_evidence_capture_vertical import Served, _metadata

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INTERNAL_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    RequestEnvelope,
    get_operation_metadata,
)
from omnivia_core.contracts.v1.generated import DocumentCaptureResult

pytest_plugins = ("test_evidence_capture_vertical", "test_document_intake")

CORRUPT_PDF = b"%PDF-1.7\n"
CAPTURE_ENTRY = get_operation_metadata("document.capture")


def processed(view: Any) -> list[store.StoredResult]:
    """Settle every queued obligation, as the service's executor does, over `view`."""
    if not hasattr(view, "sqlite_gate"):
        view.sqlite_gate = RLock()
    return process_capture_outbox(view)


def send(dispatcher: Any, payload: dict[str, Any], *, tag: str) -> Any:
    return dispatcher.dispatch(
        RequestEnvelope(
            operation="document.capture",
            metadata=_metadata(CAPTURE_ENTRY, request_id=f"req-{tag}", key=f"idem-{tag}"),
            input=payload,
        )
    )


def consent() -> dict[str, Any]:
    return {
        "subscription_id": "sub-refresh-1",
        "consent_generation": 1,
        "config_generation": 1,
        "dirty_epoch": 1,
    }


def automatic_payload(content: bytes, base: str) -> dict[str, Any]:
    body = capture_payload(content=content, intent=store.INTENT_AUTOMATIC, expected_base=base)
    body["consent"] = consent()
    return body


def wire(dispatcher: Any) -> dict[str, Any]:
    return history(dispatcher, request_id="req-history-wire").to_wire()


def settled_first_capture(owned: Served, dispatcher: Any) -> tuple[DocumentCaptureResult, Any]:
    first = capture(dispatcher, request_id="req-c1", key="idem-c1", content=b"first body")
    view = service_view(owned)
    results = processed(view)
    assert [r.result_state for r in results] == ["succeeded"]
    assert [r.publication_disposition for r in results] == ["promoted"]
    return first, view


def without_currentness(entry: Any) -> Any:
    return dataclasses.replace(entry, is_current_source=False)


# --- AT-32 ----------------------------------------------------------------------


def test_a_refresh_whose_processing_fails_leaves_the_prior_capture_retrievable_and_is_its_own_history_entry(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="at32-dispatch")
    first, view = settled_first_capture(owned, dispatcher)

    before = history(dispatcher, request_id="req-history-before")
    assert before.extraction.retrieval_state == "retrievable"
    extraction_1 = before.extractions[0]
    assert extraction_1.is_current_extraction
    assert extraction_1.capture_id == first.capture_id

    refresh = capture(
        dispatcher,
        request_id="req-c2",
        key="idem-c2",
        content=CORRUPT_PDF,
        media_type="application/pdf",
        intent="refresh",
        expected_base=first.capture_id,
    )
    admitted = history(dispatcher, request_id="req-history-admitted")
    results = processed(view)
    assert [r.result_state for r in results] == ["failed"]
    after = history(dispatcher, request_id="req-history-after")

    # The refresh is its own capture entry, attributed as a refresh over capture 1.
    captures = {entry.capture_id: entry for entry in after.captures}
    assert after.capture_count == 2 and set(captures) == {first.capture_id, refresh.capture_id}
    assert captures[refresh.capture_id].intent_kind == "refresh"
    assert captures[refresh.capture_id].expected_base_capture_id == first.capture_id
    assert captures[refresh.capture_id].media_type == "application/pdf"
    # Capture 1 is still retained, and it is still the capture behind the current extraction.
    assert captures[first.capture_id].retention_state == store.RETENTION_RETAINED
    assert after.extraction.current_extraction_id == extraction_1.extraction_revision_id
    assert after.extractions[0].capture_id == first.capture_id

    # Retrieval is exactly what it was: same head, same ordinal, same publication time.
    assert after.extraction.retrieval_state == "retrievable"
    assert after.extraction.extraction_head_ordinal == before.extraction.extraction_head_ordinal
    assert after.extraction.published_at == before.extraction.published_at

    # The failure is a separate extraction entry over the refresh capture, not current.
    assert after.extraction_count == 2
    failed = next(e for e in after.extractions if e.capture_id == refresh.capture_id)
    assert failed.result_state == "failed"
    assert failed.publication_disposition == "not_published"
    assert not failed.is_current_extraction
    assert failed.extraction_revision_id != extraction_1.extraction_revision_id

    # Nothing about extraction 1 was rewritten by the failure; capture 1 differs only in no
    # longer being the source head, which happened at admission and not at processing.
    assert after.extractions[0] == extraction_1
    assert captures[first.capture_id] == without_currentness(before.captures[0])
    assert after.captures[0] == admitted.captures[0]
    assert after.source.current_capture_id == refresh.capture_id


def test_a_failed_refresh_outcome_and_the_prior_head_survive_a_restart(
    paths: tuple[Any, Any],
) -> None:
    with owned_workspace(
        workspace_root=paths[0], installation_root=paths[1], core_version=SERVER_VERSION
    ) as first_run:
        workspace_id = first_run.workspace_id
        assert workspace_id is not None
        base = capture_document_bytes(
            first_run,
            content=b"first body",
            document_id=DOCUMENT,
            expected_base_capture_id=None,
            operation_key="capture-at32-1",
            media_type="text/plain",
            intent_kind=store.INTENT_NEW_CAPTURE,
        )
        assert [r.result_state for r in process_capture_outbox(first_run)] == ["succeeded"]
        refresh = capture_document_bytes(
            first_run,
            content=CORRUPT_PDF,
            document_id=DOCUMENT,
            expected_base_capture_id=base.capture.capture_id,
            operation_key="capture-at32-2",
            media_type="application/pdf",
            intent_kind=store.INTENT_REFRESH,
        )
        assert [r.result_state for r in process_capture_outbox(first_run)] == ["failed"]
        assert first_run.connection is not None
        head_before = store.read_head(
            first_run.connection, workspace_id=workspace_id, document_id=DOCUMENT
        )
        assert head_before is not None and head_before.current_extraction_id is not None

    with owned_workspace(
        workspace_root=paths[0], installation_root=paths[1], core_version=SERVER_VERSION
    ) as second_run:
        assert second_run.connection is not None
        assert second_run.workspace_id == workspace_id
        connection = second_run.connection
        head = store.read_head(connection, workspace_id=workspace_id, document_id=DOCUMENT)
        # The prior extraction is still the head; the capture head is the refresh.
        assert head is not None
        assert head.current_extraction_id == head_before.current_extraction_id
        assert head.extraction_head_ordinal == head_before.extraction_head_ordinal
        assert head.current_capture_id == refresh.capture.capture_id
        retention = store.read_retention(
            connection, workspace_id=workspace_id, capture_id=base.capture.capture_id
        )
        assert retention is not None and retention.retention_state == store.RETENTION_RETAINED

        rows = store.read_extraction_history(
            connection, workspace_id=workspace_id, document_id=DOCUMENT, offset=0, limit=10
        )
        assert len(rows) == 2
        kept, failed = rows
        assert kept.revision.extraction_revision_id == head.current_extraction_id
        assert kept.revision.capture_id == base.capture.capture_id
        assert kept.result is not None and kept.result.result_state == "succeeded"
        assert kept.result.publication_disposition == "promoted"
        assert failed.revision.capture_id == refresh.capture.capture_id
        assert failed.result is not None and failed.result.result_state == "failed"
        assert failed.result.publication_disposition == "not_published"
        # No work is left to replay: restart does not re-run, or re-promote, anything.
        assert process_capture_outbox(second_run) == []

        # And the same facts through the real dispatcher over the reopened workspace.
        reopened = Served(
            connection=connection,
            identity=second_run.identity,
            generation=second_run.generation,
            layout=second_run.layout,
        )
        dispatcher = build_dispatcher(reopened, tag="at32-reopen", workspace_id=workspace_id)
        served = history(dispatcher, workspace_id=workspace_id)
        assert served.extraction.current_extraction_id == kept.revision.extraction_revision_id
        assert served.extraction.retrieval_state == "retrievable"
        by_id = {e.extraction_revision_id: e for e in served.extractions}
        failed_entry = by_id[failed.revision.extraction_revision_id]
        assert failed_entry.result_state == "failed"
        assert failed_entry.publication_disposition == "not_published"
        assert not failed_entry.is_current_extraction


def test_a_refresh_core_refuses_writes_no_outcome_and_leaves_the_head(owned: Served) -> None:
    dispatcher = build_dispatcher(owned, tag="at32-refused")
    settled_first_capture(owned, dispatcher)
    before = wire(dispatcher)

    # A base that is not a capture of this document is refused at admission.
    unknown_base = capture_payload(
        content=CORRUPT_PDF,
        intent="refresh",
        expected_base="capture-of-no-document",
        media_type="application/pdf",
    )
    error = refusal(send(dispatcher, unknown_base, tag="bad-base"))
    # Today Core reports this as a retryable internal condition, not an invalid request.
    assert error.error.code == ERROR_CODE_INTERNAL_RECOVERABLE

    # An unusable media type is refused as an invalid request.
    bad_media = capture_payload(
        content=CORRUPT_PDF,
        intent="refresh",
        expected_base=before["source"]["current_capture_id"],
        media_type="not a media type",
    )
    assert refusal(send(dispatcher, bad_media, tag="bad-media")).error.code in {
        ERROR_CODE_INVALID_REQUEST,
        ERROR_CODE_INTERNAL_RECOVERABLE,
    }

    assert wire(dispatcher) == before
    assert before["capture_count"] == 1 and before["extraction_count"] == 1
    assert processed(service_view(owned)) == []
    assert wire(dispatcher) == before


# --- AT-33 ----------------------------------------------------------------------


def test_identical_bytes_after_expiry_are_not_up_to_date_and_automatic_intents_cannot_reintroduce_them(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="at33-expiry")
    first, _view = settled_first_capture(owned, dispatcher)
    generation = expire_capture(service_view(owned), capture_id=first.capture_id)

    expired = history(dispatcher, request_id="req-history-expired")
    assert expired.captures[0].retention_state == "expired"
    assert expired.captures[0].capture_retention_generation == generation
    assert expired.captures[0].actions[0].unavailable_reason == "source_not_retained"
    before = expired.to_wire()

    # An automatic intent with the identical bytes is refused: the expiry is respected.
    refused = refusal(
        send(dispatcher, automatic_payload(b"first body", first.capture_id), tag="auto-same")
    )
    assert refused.error.code == ERROR_CODE_CONFLICT
    assert wire(dispatcher) == before

    # A manual refresh is an explicit new decision, so it is a new head-eligible capture.
    refresh = capture(
        dispatcher,
        request_id="req-c-refresh",
        key="idem-c-refresh",
        content=b"first body",
        intent="refresh",
        expected_base=first.capture_id,
    )
    assert refresh.capture_id != first.capture_id
    assert refresh.became_head
    after = history(dispatcher, request_id="req-history-after")
    entries = {entry.capture_id: entry for entry in after.captures}
    assert after.capture_count == 2
    # The expired capture is never revived and never the head again.
    assert entries[first.capture_id].retention_state == "expired"
    assert entries[first.capture_id].capture_retention_generation == generation
    assert not entries[first.capture_id].is_current_source
    assert entries[refresh.capture_id].retention_state == store.RETENTION_RETAINED
    assert entries[refresh.capture_id].capture_disposition == store.DISPOSITION_HEAD_ELIGIBLE
    assert entries[refresh.capture_id].is_current_source
    assert after.source.current_capture_id == refresh.capture_id
    # The old extraction is not re-pointed at the new capture either.
    assert after.extractions[0] == expired.extractions[0]


def test_identical_bytes_after_the_source_is_removed_do_not_become_the_head(
    owned: Served,
) -> None:
    dispatcher = build_dispatcher(owned, tag="at33-removed")
    first, _view = settled_first_capture(owned, dispatcher)
    set_document_source_state(service_view(owned), document_id=DOCUMENT, removed=True)
    removed = history(dispatcher, request_id="req-history-removed")
    assert removed.source.source_state == store.SOURCE_REMOVED

    for tag, payload in (
        ("auto", automatic_payload(b"first body", first.capture_id)),
        (
            "refresh",
            capture_payload(content=b"first body", intent="refresh", expected_base=first.capture_id),
        ),
    ):
        result = DocumentCaptureResult.from_wire(answered(send(dispatcher, payload, tag=tag)).result)
        # Core admits the bytes as history, never as the head.
        assert result.capture_disposition == store.DISPOSITION_HISTORICAL_ONLY
        assert not result.became_head

    after = history(dispatcher, request_id="req-history-after")
    assert after.capture_count == 3
    assert after.source.source_state == store.SOURCE_REMOVED
    assert after.source.current_capture_id == first.capture_id
    assert after.source.capture_head_sequence == removed.source.capture_head_sequence
    assert after.source.source_removal_generation == removed.source.source_removal_generation
    assert after.extraction == removed.extraction
    extra = [e for e in after.captures if e.capture_id != first.capture_id]
    assert len(extra) == 2
    for entry in extra:
        assert entry.capture_disposition == store.DISPOSITION_HISTORICAL_ONLY
        assert entry.disposition_reason == store.REASON_REMOVED_SOURCE
        assert not entry.is_current_source
    assert [e.intent_kind for e in extra] == [store.INTENT_AUTOMATIC, store.INTENT_REFRESH]
    assert after.captures[0] == removed.captures[0]

