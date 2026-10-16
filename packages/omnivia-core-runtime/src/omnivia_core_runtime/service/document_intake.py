"""Service-owned document intake: capture a source once, extract it as often as asked, order the two separately.

This is a maintenance path in the shape of `service/source_capture.py`, not an application operation. It runs only
while a `ServiceRunner` owns the workspace, so every write uses the same lifetime lock, exclusive connection, lease,
mutation guard and fencing transaction as the live service, and nothing here is reachable over the application
catalogue.

*Capture* reads one local file once, publishes its bytes as a content-addressed blob outside the database
transaction (as source capture does), and then, in one fenced transaction, writes the evidence through
`source_capture._write_capture` and associates it with the logical document. The capture is admitted with the
capture sequence of the document and is the document's current capture only if the caller's expected base was still
the current capture. A capture on a stale base is retained as history and never becomes current, whenever it arrives.
A refresh that fails before the transaction leaves no row, so the previous current capture is untouched.

*Extraction* is a revision over one capture, its exact input digest and one content-addressed profile. Admitting a
revision freezes the document's source removal generation and the capture's retention generation, and a terminal
result is published, and the extraction head moved, in one fenced transaction that rechecks both. The extraction head
and the capture head never move together: an extraction, whatever capture it is over, cannot move the capture head.

*Reprocess captured copy* reads only the retained workspace blob of a capture and re-hashes it. It takes no path,
opens no original and creates no evidence and no capture. Running an extractor is the caller's: this slice ships no
format adapters, OCR, folder monitoring, native bookmarks or retrieval projection. `reprocess_captured_copy` takes the
extractor as a function and records the outcome it returns, or a failed outcome when it raises.

Operation keys are scoped to one operation kind in the workspace: a capture key and an extraction key are separate
namespaces. Replaying a key with the same request returns the stored record and writes nothing, and the same key with
a different request is refused.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from omnivia_core.contracts.v1 import to_canonical_json
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service import processing_settings
from omnivia_core_runtime.service.ocr_resource_lifecycle import ManagedOcrResources
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.source_capture import (
    _IDENTIFIER,
    SourceCaptureRefused,
    _read_source,
    _validate_request,
    _write_capture,
    publish_blob,
)
from omnivia_core_runtime.storage import document_intake as store
from omnivia_core_runtime.workspace.blob_publication import (
    BlobPublicationRefused,
    _opened_blob,
    blob_path,
)

CAPTURE_STATUS_CAPTURED: Final = "captured"
CAPTURE_STATUS_REPLAYED: Final = "replayed"
CAPTURE_METADATA_KIND: Final = "document_intake"
MAX_DOCUMENT_CAPTURE_BYTES: Final = 50 * 1024 * 1024
#: The profile reference the governed-transfer boundary admits under. Named rather
#: than spelled inline because the admission now has to ask what that reference
#: resolves to -- a Text-only reference pins no OCR resources -- and the question and
#: the stored value must be about the same string.
_LEGACY_PROFILE_REF: Final = "legacy-default"
#: Its revision, named for the same reason one step further on: the reference and the
#: revision are both inputs to the profile digest this admission freezes, so the pair
#: the digest was taken over has to be the pair the row stores.
_LEGACY_PROFILE_REVISION: Final = "1"
#: The most generations one relink may advance a route. The storage triggers allow only
#: disconnected-to-connected at generation + 1, so a relink walks the route one cycle per
#: generation inside its one transaction; the companion reserves generations one at a time,
#: so a larger gap is not a relink it could have sent.
_MAX_RELINK_ADVANCE: Final = 16


class DocumentIntakeRefused(RuntimeError):
    """The request cannot be admitted without weakening a boundary; the text is fixed and names no path."""


class DocumentCaptureSuppressed(DocumentIntakeRefused):
    """An automatic or folder capture of bytes already captured and then expired or deleted; nothing was written."""


@dataclass(frozen=True, slots=True)
class BindingClaim:
    """The opaque original-binding lineage a capture was taken under: owner, generation and route; no path."""

    owner_ref: str
    generation: int
    route_kind: str
    root_ref: str | None = None
    member_ref: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentCaptureResult:
    """One capture admission. `became_head` is whether the document's current capture is now this capture."""

    status: str
    capture: store.StoredCapture
    became_head: bool


@dataclass(frozen=True, slots=True)
class ExtractionOutcome:
    """What an extractor reports for one revision. `output_digest` is required for succeeded and partial only."""

    state: str
    output_digest: str | None = None
    coverage: Mapping[str, Any] = field(default_factory=dict)
    limitations: Mapping[str, Any] = field(default_factory=dict)
    worker_build: str = "unspecified"
    segments: tuple[ExtractedSegment, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractedSegment:
    """One structurally located extraction unit; exactly one of text or structured value is present."""

    semantic_role: str
    source_locator: Mapping[str, Any]
    text: str | None = None
    structured_value: Any | None = None
    extraction_method: str = "direct"
    visibility: str = "visible"
    revision_state: str = "current"
    language: str | None = None
    confidence: float | None = None


Extractor = Callable[[bytes, Mapping[str, Any]], ExtractionOutcome]


@contextmanager
def owned_workspace(
    *, workspace_root: Path, installation_root: Path, core_version: str
) -> Iterator[ServiceRunner]:
    """Hold the workspace's full write authority for the duration of the block."""
    runner = ServiceRunner(
        ServiceSettings(
            workspace_root=workspace_root,
            installation_root=installation_root,
            core_version=core_version,
            endpoint=None,
        )
    )
    report = runner.start()
    try:
        if not report.ready:
            raise DocumentIntakeRefused(report.reason or "workspace ownership was refused")
        yield runner
    finally:
        runner.stop()


def _now_us() -> int:
    return time.time_ns() // 1000


def _fence(runner: ServiceRunner) -> Any:
    assert runner.connection is not None
    assert runner.identity is not None
    assert runner.workspace_id is not None
    assert runner.generation is not None
    return fenced_transaction(
        runner.connection,
        runner.identity,
        workspace_id=runner.workspace_id,
        fencing_generation=runner.generation,
    )


def _identifier(value: str, what: str) -> None:
    if _IDENTIFIER.fullmatch(value) is None:
        raise DocumentIntakeRefused(f"{what} is outside the accepted identifier domain")


def register_profile(runner: ServiceRunner, profile: dict[str, Any]) -> str:
    """Store an ExtractionProfileV1 and return its digest, which is the SHA-256 of its canonical JSON."""
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    try:
        with _fence(runner):
            return store.record_profile(
                runner.connection,
                workspace_id=runner.workspace_id,
                profile=profile,
                fencing_generation=runner.generation,
                now_us=_now_us(),
            )
    except store.DocumentIntakeInvalid as error:
        raise DocumentIntakeRefused(str(error)) from error


def _check_route(route_kind: str, root_ref: str | None, member_ref: str | None) -> None:
    if route_kind == store.ROUTE_DIRECT_FILE:
        if root_ref is not None or member_ref is not None:
            raise DocumentIntakeRefused("a direct file route carries no root or member")
    elif route_kind == store.ROUTE_ROOT_MEMBER:
        if root_ref is None or member_ref is None:
            raise DocumentIntakeRefused("a root member route needs a root and a member")
        _identifier(root_ref, "root reference")
        _identifier(member_ref, "member reference")
    else:
        raise DocumentIntakeRefused("unknown binding route kind")


def _binding_for(
    runner: ServiceRunner, *, document_id: str, owner_ref: str, route_kind: str,
    root_ref: str | None, member_ref: str | None,
) -> store.StoredBinding | None:  # fmt: skip
    """The stored binding of `owner_ref`, refused unless it is this document's, on this route."""
    assert runner.connection is not None and runner.workspace_id is not None
    binding = store.read_binding(runner.connection, workspace_id=runner.workspace_id, owner_ref=owner_ref)
    if binding is None:
        return None
    if binding.document_id != document_id:
        raise DocumentIntakeRefused("the original binding belongs to another document")
    if (binding.route_kind, binding.root_ref, binding.member_ref) != (route_kind, root_ref, member_ref):
        raise DocumentIntakeRefused("the original binding is on a different route")
    return binding


def _move_original(
    runner: ServiceRunner, *, document_id: str, owner_ref: str, route_kind: str,
    root_ref: str | None, member_ref: str | None, connect: bool,
) -> store.StoredBinding:  # fmt: skip
    _identifier(document_id, "document id")
    _identifier(owner_ref, "owner reference")
    _check_route(route_kind, root_ref, member_ref)
    with _fence(runner):
        return move_original_in_transaction(
            runner, document_id=document_id, owner_ref=owner_ref, route_kind=route_kind,
            root_ref=root_ref, member_ref=member_ref, connect=connect,
        )  # fmt: skip


def move_original_in_transaction(
    runner: ServiceRunner, *, document_id: str, owner_ref: str, route_kind: str,
    root_ref: str | None, member_ref: str | None, connect: bool,
) -> store.StoredBinding:  # fmt: skip
    """`_move_original`'s write, for a caller that already owns the fenced transaction.

    `BEGIN IMMEDIATE` does not nest, so a governed mutation cannot call `disconnect_original`.
    The identifiers and the route are the caller's to have validated.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    state = store.BINDING_CONNECTED if connect else store.BINDING_DISCONNECTED
    now_us = _now_us()
    binding = _binding_for(
        runner, document_id=document_id, owner_ref=owner_ref, route_kind=route_kind,
        root_ref=root_ref, member_ref=member_ref,
    )  # fmt: skip
    if binding is None:
        store.ensure_document(
            runner.connection, workspace_id=runner.workspace_id, document_id=document_id,
            fencing_generation=runner.generation, now_us=now_us,
        )  # fmt: skip
        return store.insert_binding(
            runner.connection, workspace_id=runner.workspace_id, owner_ref=owner_ref,
            document_id=document_id, route_kind=route_kind, root_ref=root_ref, member_ref=member_ref,
            state=state, fencing_generation=runner.generation, now_us=now_us,
        )  # fmt: skip
    if binding.connection_state == state:
        return binding
    return store.move_binding(
        runner.connection, binding, connect=connect, fencing_generation=runner.generation, now_us=now_us
    )


def relink_original_in_transaction(
    runner: ServiceRunner, *, document_id: str, owner_ref: str, generation: int,
) -> store.StoredBinding:  # fmt: skip
    """Leave the owner's direct-file route connected at exactly `generation`, in the caller's fenced transaction.

    Migration 0069 allows only connected-to-disconnected at the same generation and
    disconnected-to-connected one generation later, so the route advances by cycles. A route Core has not
    seen yet is recorded connected at generation one first, so a capture under an older generation that
    arrives later is historical. A generation that is not newer than the stored one, or more than
    `_MAX_RELINK_ADVANCE` ahead of it, is refused before anything is written.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    binding = _binding_for(
        runner, document_id=document_id, owner_ref=owner_ref, route_kind=store.ROUTE_DIRECT_FILE,
        root_ref=None, member_ref=None,
    )  # fmt: skip
    current = 1 if binding is None else binding.binding_generation
    if current >= generation:
        raise DocumentIntakeRefused("the relink generation is not newer than the one Core holds")
    if generation - current > _MAX_RELINK_ADVANCE:
        raise DocumentIntakeRefused("the relink generation is too far ahead of the one Core holds")
    now_us = _now_us()
    if binding is None:
        store.ensure_document(
            runner.connection, workspace_id=runner.workspace_id, document_id=document_id,
            fencing_generation=runner.generation, now_us=now_us,
        )  # fmt: skip
        binding = store.insert_binding(
            runner.connection, workspace_id=runner.workspace_id, owner_ref=owner_ref,
            document_id=document_id, route_kind=store.ROUTE_DIRECT_FILE, root_ref=None, member_ref=None,
            state=store.BINDING_CONNECTED, fencing_generation=runner.generation, now_us=now_us,
        )  # fmt: skip
    while binding.binding_generation < generation:
        if binding.connection_state == store.BINDING_CONNECTED:
            binding = store.move_binding(
                runner.connection, binding, connect=False, fencing_generation=runner.generation, now_us=now_us
            )
        binding = store.move_binding(
            runner.connection, binding, connect=True, fencing_generation=runner.generation, now_us=now_us
        )
    return binding


def connect_original(
    runner: ServiceRunner, *, document_id: str, owner_ref: str, route_kind: str,
    root_ref: str | None = None, member_ref: str | None = None,
) -> store.StoredBinding:  # fmt: skip
    """Bind (or relink) an opaque owner/device route to a document's original and return the binding.

    A new binding starts at generation one. Reconnecting a disconnected one advances its generation by one, so a
    capture taken under an older generation can never become current. Connecting a connected binding changes nothing.
    """
    return _move_original(
        runner, document_id=document_id, owner_ref=owner_ref, route_kind=route_kind,
        root_ref=root_ref, member_ref=member_ref, connect=True,
    )  # fmt: skip


def disconnect_original(
    runner: ServiceRunner, *, document_id: str, owner_ref: str, route_kind: str,
    root_ref: str | None = None, member_ref: str | None = None,
) -> store.StoredBinding:  # fmt: skip
    """Tombstone one owner/device route at its current generation; no other owner or device is touched.

    A disconnect that arrives before the document has any capture records the binding as disconnected at generation
    one, anchored to an empty Core-owned document with no current capture. Disconnecting a disconnected binding
    changes nothing.
    """
    return _move_original(
        runner, document_id=document_id, owner_ref=owner_ref, route_kind=route_kind,
        root_ref=root_ref, member_ref=member_ref, connect=False,
    )  # fmt: skip


def capture_document(
    runner: ServiceRunner,
    *,
    source_path: Path,
    document_id: str,
    expected_base_capture_id: str | None,
    operation_key: str,
    media_type: str,
    intent_kind: str,
    binding: BindingClaim | None = None,
) -> DocumentCaptureResult:
    """Capture one local file as a new immutable capture of `document_id`.

    `expected_base_capture_id` is the capture the caller saw as current, or `None` when it saw none: it is required,
    never defaulted, because the head decision depends on it. `intent_kind` is required too: `new_capture` creates a
    document (and is historical if the document already has any capture), every other intent refreshes against its
    base. `binding` is the lineage the capture was taken under, or `None` for a snapshot with no original binding.
    The source is read once and never again.

    A capture of a removed source, or under a disconnected or superseded binding generation, is admitted as
    historical only. An unknown, cross-document or inconsistent binding lineage is refused and writes nothing.
    Identical bytes taken by an automatic or folder capture after a capture of them was expired or deleted are
    suppressed (`DocumentCaptureSuppressed`); a manual capture of them is not.
    """
    try:
        _validate_request(document_id, media_type)
        content = _read_source(source_path)
    except SourceCaptureRefused as error:
        raise DocumentIntakeRefused(str(error)) from error
    return capture_document_bytes(
        runner,
        content=content,
        document_id=document_id,
        expected_base_capture_id=expected_base_capture_id,
        operation_key=operation_key,
        media_type=media_type,
        intent_kind=intent_kind,
        binding=binding,
    )


def capture_document_bytes(
    runner: ServiceRunner,
    *,
    content: bytes,
    document_id: str,
    expected_base_capture_id: str | None,
    operation_key: str,
    media_type: str,
    intent_kind: str,
    binding: BindingClaim | None = None,
    ocr_resources: ManagedOcrResources | None = None,
) -> DocumentCaptureResult:
    """Admit the exact immutable bytes supplied by a governed transfer boundary.

    This function never opens an original path. The caller owns staging/transfer and must
    provide the complete snapshot bytes; Core recomputes identity and length here.

    `ocr_resources` is the installation's managed OCR cache, which the admission
    freezes and pins the capture's exact resource identities against. It defaults to
    `None` -- an installation with no managed cache -- rather than being required,
    because this boundary admits under the fixed `legacy-default` profile reference
    and has no production caller to forget it.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    _identifier(document_id, "document id")
    _identifier(operation_key, "operation key")
    if not isinstance(content, bytes) or not content:
        raise DocumentIntakeRefused("the capture snapshot is empty or invalid")
    if len(content) > MAX_DOCUMENT_CAPTURE_BYTES:
        raise DocumentIntakeRefused("the capture snapshot exceeds the accepted bound")
    try:
        _validate_request(document_id, media_type)
    except SourceCaptureRefused as error:
        raise DocumentIntakeRefused(str(error)) from error
    if intent_kind not in store.INTENTS:
        raise DocumentIntakeRefused("unknown capture intent")
    if intent_kind == store.INTENT_NEW_CAPTURE and expected_base_capture_id is not None:
        raise DocumentIntakeRefused("a new document capture has no expected base")
    if binding is not None:
        _identifier(binding.owner_ref, "owner reference")
        _check_route(binding.route_kind, binding.root_ref, binding.member_ref)
        if (
            not isinstance(binding.generation, int)
            or isinstance(binding.generation, bool)
            or not 1 <= binding.generation <= 2**63 - 1
        ):
            raise DocumentIntakeRefused("the binding generation is outside its accepted range")
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    request: dict[str, Any] = {
        "documentId": document_id,
        "expectedBaseCaptureId": expected_base_capture_id,
        "intentKind": intent_kind,
        "lineage": None
        if binding is None
        else {
            "generation": binding.generation,
            "memberRef": binding.member_ref,
            "ownerRef": binding.owner_ref,
            "rootRef": binding.root_ref,
            "routeKind": binding.route_kind,
        },
        "mediaType": media_type,
        "sourceDigest": digest,
        "sourceLengthBytes": len(content),
    }
    workspace_id = runner.workspace_id
    replay = _captured_replay(runner, operation_key, request)
    if replay is not None:
        return replay
    try:
        publish_blob(runner.layout.blobs_path, digest, content)
    except SourceCaptureRefused as error:
        raise DocumentIntakeRefused(str(error)) from error

    with _fence(runner):
        replay = _captured_replay(runner, operation_key, request)
        if replay is not None:
            return replay
        if intent_kind in store.AUTOMATIC_INTENTS and store.has_nonretained_identical(
            runner.connection, workspace_id=workspace_id, document_id=document_id, source_digest=digest
        ):
            raise DocumentCaptureSuppressed("identical bytes were already captured and then expired or deleted")
        now_us = _now_us()
        head = store.ensure_document(
            runner.connection,
            workspace_id=workspace_id,
            document_id=document_id,
            fencing_generation=runner.generation,
            now_us=now_us,
        )
        if expected_base_capture_id is not None:
            base = store.read_capture(
                runner.connection, workspace_id=workspace_id, capture_id=expected_base_capture_id
            )
            if base is None or base.document_id != document_id:
                raise DocumentIntakeRefused("the expected base is not a capture of this document")
        stored_binding = None
        if binding is not None:
            stored_binding = _binding_for(
                runner, document_id=document_id, owner_ref=binding.owner_ref, route_kind=binding.route_kind,
                root_ref=binding.root_ref, member_ref=binding.member_ref,
            )  # fmt: skip
            if stored_binding is None:
                if intent_kind != store.INTENT_NEW_CAPTURE or binding.generation != 1:
                    raise DocumentIntakeRefused("the original binding is unknown")
                stored_binding = store.insert_binding(
                    runner.connection,
                    workspace_id=workspace_id,
                    owner_ref=binding.owner_ref,
                    document_id=document_id,
                    route_kind=binding.route_kind,
                    root_ref=binding.root_ref,
                    member_ref=binding.member_ref,
                    state=store.BINDING_CONNECTED,
                    fencing_generation=runner.generation,
                    now_us=now_us,
                )
            if binding.generation > stored_binding.binding_generation:
                raise DocumentIntakeRefused("the binding generation is newer than the binding")
        sequence = store.last_capture_sequence(
            runner.connection, workspace_id=workspace_id, document_id=document_id
        )
        association, disposition, reason = store.decide_capture(
            head,
            stored_binding,
            claimed_generation=None if binding is None else binding.generation,
            intent_kind=intent_kind,
            expected_base_capture_id=expected_base_capture_id,
            has_captures=sequence > 0,
        )
        capture_id = store.capture_id_for(workspace_id, operation_key)
        admitted_settings, admitted_resources, admitted_profile = _admitted_processing(
            runner,
            capture_id=capture_id,
            media_type=media_type,
            profile_ref=_LEGACY_PROFILE_REF,
            profile_revision=_LEGACY_PROFILE_REVISION,
            resources=ocr_resources,
        )
        try:
            written = _write_capture(
                runner,
                source_id=capture_id,
                digest=digest,
                length=len(content),
                media_type=media_type,
                capture=CAPTURE_METADATA_KIND,
            )
        except SourceCaptureRefused as error:
            raise DocumentIntakeRefused(str(error)) from error
        assert written.evidence_id is not None
        eligible = disposition == store.DISPOSITION_HEAD_ELIGIBLE
        capture = store.StoredCapture(
            workspace_id=workspace_id,
            capture_id=capture_id,
            document_id=document_id,
            capture_sequence=sequence + 1,
            evidence_id=written.evidence_id,
            source_digest=digest,
            source_length_bytes=len(content),
            media_type=media_type,
            requested_source_identity=operation_key,
            target_binding_revision=operation_key,
            processing_profile_ref=_LEGACY_PROFILE_REF,
            processing_profile_revision=_LEGACY_PROFILE_REVISION,
            consent_json=None,
            bulk_origin_json=None,
            display_name=None,
            expected_base_capture_id=expected_base_capture_id,
            intent_kind=intent_kind,
            association_kind=association,
            binding_owner_ref=None if binding is None else binding.owner_ref,
            binding_generation=None if binding is None else binding.generation,
            binding_route_kind=None if binding is None else binding.route_kind,
            binding_root_ref=None if binding is None else binding.root_ref,
            binding_member_ref=None if binding is None else binding.member_ref,
            disposition=disposition,
            disposition_reason=reason,
            source_removal_generation=head.source_removal_generation,
            operation_key=operation_key,
            request_digest=store.capture_request_digest(
                request, association=association, disposition=disposition, reason=reason
            ),
            fencing_generation=runner.generation,
            created_at_us=now_us,
            processing_settings_json=admitted_settings,
            processing_resources_json=admitted_resources,
            processing_profile_digest=admitted_profile,
        )
        store.insert_capture(runner.connection, capture, now_us=now_us)
        store.insert_capture_outbox(runner.connection, capture, now_us=now_us)
        if eligible:
            store.advance_capture_head(runner.connection, capture, now_us=now_us)
    return DocumentCaptureResult(CAPTURE_STATUS_CAPTURED, capture, eligible)


def _admitted_processing(
    runner: ServiceRunner,
    *,
    capture_id: str,
    media_type: str,
    profile_ref: str,
    profile_revision: str,
    resources: ManagedOcrResources | None,
) -> tuple[str, str, str]:
    """What this capture is admitted under, against, and as: its three frozen facts.

    All read **once**, here, at the instant the capture row is written, and stored
    on that row -- the preferences as `processing_settings_json` (migration 0071)
    and the exact OCR resource identities as `processing_resources_json`
    (migration 0072). A preference changed, a model updated or a language removed
    after this point cannot reach this capture: the queued obligation, the profile
    a worker will execute and the identity `document.history` reports for it all
    derive from these two snapshots and never from whatever the installation or the
    setting says when the obligation is finally picked up.

    The resource half is why this is no longer only an intent. 0071 froze which mode
    and which ordered languages were wanted and left *which bytes* to execution,
    which meant a capture admitted against one model revision could execute against
    another -- under an operation key derived from the profile digest, so under an
    identity nobody had been offered. :func:`admitted_resources` resolves the
    languages against the installation's managed cache now and pins the exact
    identities it resolves, so the bytes survive the queue; execution serves those
    identities or reports a resource condition, and never resolves again.

    A capture whose processing resolves no OCR resources at all -- Text-only, or a
    media type these preferences do not recognise -- records that it pinned nothing.
    It is deliberately a recorded decision rather than an absent value: a NULL means
    a capture admitted before the column existed, and the two must stay
    distinguishable.

    A managed cache that cannot be read is not an answer about it, so the admission
    is refused rather than frozen against a guess. The refusal is the same retryable
    class as a capture whose bytes could not be preserved, because resending the same
    operation key completes the work -- which is exactly the recovery, and much better
    than a capture permanently frozen out of recognition because an administrator was
    installing a language at that instant.

    The third fact is the ExtractionProfileV1 digest those two documents resolve to,
    written as `processing_profile_digest` (migration 0072). It is computed from the
    settings and the identities decided *here*, in this call, and from nothing else:
    the live preferences are not read a second time and the installation's cache is
    not consulted again, so the digest cannot describe a decision other than the one
    being written beside it. What it adds is the one input the row cannot hold --
    this build's own adapters, decoder builds, engine revision and limits -- so an
    execution by a later build can compare rather than assume. Every new capture
    carries one, the text-only captures included.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    # Imported at use: document_processing imports this module for the admission,
    # extraction and settlement it drives, so the one thing it owns that an
    # admission needs -- how a frozen decision becomes a profile -- can only be
    # reached from here at the call.
    from omnivia_core_runtime.service.document_processing import (
        admitted_profile_digest,
    )

    settings, _revision = processing_settings.read_settings(
        runner.connection, workspace_id=runner.workspace_id
    )
    if processing_settings.resolves_resources(
        media_type=media_type, profile_ref=profile_ref, settings=settings
    ):
        try:
            admitted = processing_settings.admitted_resources(
                resources,
                pin=processing_settings.capture_pin(runner.workspace_id, capture_id),
                languages=settings.languages,
            )
        except processing_settings.ResourceStateIndeterminate as error:
            raise DocumentIntakeRefused(
                "the OCR resources this capture would be admitted against could "
                "not be established"
            ) from error
    else:
        admitted = processing_settings.AdmittedResources(
            processing_settings.RESOURCES_NONE
        )
    return (
        to_canonical_json(settings.to_value()),
        to_canonical_json(admitted.to_value()),
        admitted_profile_digest(
            media_type=media_type,
            profile_ref=profile_ref,
            revision=profile_revision,
            settings=settings,
            admitted=admitted,
        ),
    )


def _captured_replay(
    runner: ServiceRunner, operation_key: str, request: dict[str, Any]
) -> DocumentCaptureResult | None:
    assert runner.connection is not None and runner.workspace_id is not None
    existing = store.read_capture_by_key(
        runner.connection, workspace_id=runner.workspace_id, operation_key=operation_key
    )
    if existing is None:
        return None
    expected = store.capture_request_digest(
        request,
        association=existing.association_kind,
        disposition=existing.disposition,
        reason=existing.disposition_reason,
    )
    if existing.request_digest != expected:
        raise DocumentIntakeRefused("the operation key already names a different capture request")
    head = store.read_head(
        runner.connection, workspace_id=runner.workspace_id, document_id=existing.document_id
    )
    assert head is not None
    return DocumentCaptureResult(
        CAPTURE_STATUS_REPLAYED, existing, head.current_capture_id == existing.capture_id
    )


def admit_document_capture_in_transaction(
    runner: ServiceRunner,
    *,
    content: bytes,
    document_id: str,
    expected_base_capture_id: str | None,
    operation_key: str,
    media_type: str,
    intent_kind: str,
    binding: BindingClaim | None,
    requested_source_identity: str,
    target_binding_revision: str,
    processing_profile_ref: str,
    processing_profile_revision: str,
    consent: Mapping[str, Any] | None,
    bulk_origin: Mapping[str, Any] | None,
    display_name: str | None,
    ocr_resources: ManagedOcrResources | None,
) -> DocumentCaptureResult:
    """Write one transferred snapshot while the application mutation owns the transaction.

    The caller must have published ``content`` to the workspace blob store first. This
    function opens no transaction and commits nothing; the application mutation seam owns
    audit, idempotency outcome, these domain rows, and rollback as one unit.

    ``ocr_resources`` is this installation's managed OCR cache, and it is a required
    argument with no default: the admission freezes and *pins* the exact resource
    identities this capture will execute against, and a caller that silently passed
    nothing would admit a capture that can never recognise anything -- which is a
    decision no caller should be able to make by omission. ``None`` says there is no
    managed cache to ask, which is a determinate answer and not an absent one.

    The pin is taken before these rows are written, and therefore inside the
    mutation's transaction. A rollback leaves the pin without the capture that owns
    it, which
    :meth:`~omnivia_core_runtime.service.ocr_resource_lifecycle.ManagedOcrResources.reclaim`
    takes back; the order cannot be reversed, because a capture durable before its
    pin is a capture whose resources are removable while it waits.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    request: dict[str, Any] = {
        "documentId": document_id,
        "expectedBaseCaptureId": expected_base_capture_id,
        "intentKind": intent_kind,
        "lineage": None
        if binding is None
        else {
            "generation": binding.generation,
            "memberRef": binding.member_ref,
            "ownerRef": binding.owner_ref,
            "rootRef": binding.root_ref,
            "routeKind": binding.route_kind,
        },
        "mediaType": media_type,
        "requestedSourceIdentity": requested_source_identity,
        "targetBindingRevision": target_binding_revision,
        "processingProfile": {
            "profileRef": processing_profile_ref,
            "revision": processing_profile_revision,
        },
        "consent": consent,
        "bulkOrigin": bulk_origin,
        "displayName": display_name,
        "sourceDigest": digest,
        "sourceLengthBytes": len(content),
    }
    replay = _captured_replay(runner, operation_key, request)
    if replay is not None:
        return replay
    workspace_id = runner.workspace_id
    if intent_kind in store.AUTOMATIC_INTENTS and store.has_nonretained_identical(
        runner.connection,
        workspace_id=workspace_id,
        document_id=document_id,
        source_digest=digest,
    ):
        raise DocumentCaptureSuppressed(
            "identical bytes were already captured and then expired or deleted"
        )
    now_us = _now_us()
    head = store.ensure_document(
        runner.connection,
        workspace_id=workspace_id,
        document_id=document_id,
        fencing_generation=runner.generation,
        now_us=now_us,
    )
    if expected_base_capture_id is not None:
        base = store.read_capture(
            runner.connection,
            workspace_id=workspace_id,
            capture_id=expected_base_capture_id,
        )
        if base is None or base.document_id != document_id:
            raise DocumentIntakeRefused(
                "the expected base is not a capture of this document"
            )
    stored_binding = None
    if binding is not None:
        stored_binding = _binding_for(
            runner,
            document_id=document_id,
            owner_ref=binding.owner_ref,
            route_kind=binding.route_kind,
            root_ref=binding.root_ref,
            member_ref=binding.member_ref,
        )
        if stored_binding is None:
            if intent_kind != store.INTENT_NEW_CAPTURE or binding.generation != 1:
                raise DocumentIntakeRefused("the original binding is unknown")
            stored_binding = store.insert_binding(
                runner.connection,
                workspace_id=workspace_id,
                owner_ref=binding.owner_ref,
                document_id=document_id,
                route_kind=binding.route_kind,
                root_ref=binding.root_ref,
                member_ref=binding.member_ref,
                state=store.BINDING_CONNECTED,
                fencing_generation=runner.generation,
                now_us=now_us,
            )
        if binding.generation > stored_binding.binding_generation:
            raise DocumentIntakeRefused("the binding generation is newer than the binding")
    sequence = store.last_capture_sequence(
        runner.connection, workspace_id=workspace_id, document_id=document_id
    )
    association, disposition, reason = store.decide_capture(
        head,
        stored_binding,
        claimed_generation=None if binding is None else binding.generation,
        intent_kind=intent_kind,
        expected_base_capture_id=expected_base_capture_id,
        has_captures=sequence > 0,
    )
    capture_id = store.capture_id_for(workspace_id, operation_key)
    admitted_settings, admitted_resources, admitted_profile = _admitted_processing(
        runner,
        capture_id=capture_id,
        media_type=media_type,
        profile_ref=processing_profile_ref,
        profile_revision=processing_profile_revision,
        resources=ocr_resources,
    )
    try:
        written = _write_capture(
            runner,
            source_id=capture_id,
            digest=digest,
            length=len(content),
            media_type=media_type,
            capture=CAPTURE_METADATA_KIND,
        )
    except SourceCaptureRefused as error:
        raise DocumentIntakeRefused(str(error)) from error
    assert written.evidence_id is not None
    eligible = disposition == store.DISPOSITION_HEAD_ELIGIBLE
    capture = store.StoredCapture(
        workspace_id=workspace_id,
        capture_id=capture_id,
        document_id=document_id,
        capture_sequence=sequence + 1,
        evidence_id=written.evidence_id,
        source_digest=digest,
        source_length_bytes=len(content),
        media_type=media_type,
        requested_source_identity=requested_source_identity,
        target_binding_revision=target_binding_revision,
        processing_profile_ref=processing_profile_ref,
        processing_profile_revision=processing_profile_revision,
        consent_json=None if consent is None else to_canonical_json(consent),
        bulk_origin_json=None if bulk_origin is None else to_canonical_json(bulk_origin),
        display_name=display_name,
        expected_base_capture_id=expected_base_capture_id,
        intent_kind=intent_kind,
        association_kind=association,
        binding_owner_ref=None if binding is None else binding.owner_ref,
        binding_generation=None if binding is None else binding.generation,
        binding_route_kind=None if binding is None else binding.route_kind,
        binding_root_ref=None if binding is None else binding.root_ref,
        binding_member_ref=None if binding is None else binding.member_ref,
        disposition=disposition,
        disposition_reason=reason,
        source_removal_generation=head.source_removal_generation,
        operation_key=operation_key,
        request_digest=store.capture_request_digest(
            request, association=association, disposition=disposition, reason=reason
        ),
        fencing_generation=runner.generation,
        created_at_us=now_us,
        processing_settings_json=admitted_settings,
        processing_resources_json=admitted_resources,
        processing_profile_digest=admitted_profile,
    )
    store.insert_capture(runner.connection, capture, now_us=now_us)
    store.insert_capture_outbox(runner.connection, capture, now_us=now_us)
    if eligible:
        store.advance_capture_head(runner.connection, capture, now_us=now_us)
    return DocumentCaptureResult(CAPTURE_STATUS_CAPTURED, capture, eligible)


def current_capture(runner: ServiceRunner, *, document_id: str) -> store.StoredCapture | None:
    """The document's current capture if it is still retained, else `None`; never an older capture instead."""
    assert runner.connection is not None and runner.workspace_id is not None
    return store.current_retrievable_capture(
        runner.connection, workspace_id=runner.workspace_id, document_id=document_id
    )


def pending_capture_obligations(runner: ServiceRunner, *, limit: int) -> list[store.StoredOutbox]:
    """The oldest unsettled capture-processing obligations, in admission order, at most `limit` (1 through 1000)."""
    assert runner.connection is not None and runner.workspace_id is not None
    try:
        return store.pending_outbox(runner.connection, workspace_id=runner.workspace_id, limit=limit)
    except store.DocumentIntakeInvalid as error:
        raise DocumentIntakeRefused(str(error)) from error


def settle_capture_obligation(runner: ServiceRunner, *, capture_id: str) -> store.StoredOutbox:
    """Settle a capture's processing obligation. Settling a settled one returns it unchanged."""
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    try:
        with _fence(runner):
            return store.settle_outbox(
                runner.connection,
                workspace_id=runner.workspace_id,
                capture_id=capture_id,
                fencing_generation=runner.generation,
                now_us=_now_us(),
            )
    except store.DocumentIntakeInvalid as error:
        raise DocumentIntakeRefused(str(error)) from error


def read_retained_source(runner: ServiceRunner, *, capture_id: str) -> bytes:
    """The retained bytes of a capture, re-hashed against the digest and length the capture recorded.

    Reads the workspace blob only. A capture that is not retained, or whose retained copy is missing or does not
    verify, is refused; nothing outside the workspace's blob store is opened.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    capture = store.read_capture(
        runner.connection, workspace_id=runner.workspace_id, capture_id=capture_id
    )
    if capture is None:
        raise DocumentIntakeRefused("no such capture")
    retention = store.read_retention(
        runner.connection, workspace_id=runner.workspace_id, capture_id=capture_id
    )
    assert retention is not None
    if retention.retention_state != store.RETENTION_RETAINED:
        raise DocumentIntakeRefused("the capture is no longer retained")
    try:
        descriptor = _opened_blob(blob_path(runner.layout.blobs_path, capture.source_digest))
    except BlobPublicationRefused as error:
        raise DocumentIntakeRefused("the retained copy is not available") from error
    try:
        chunks: list[bytes] = []
        remaining = min(capture.source_length_bytes, MAX_DOCUMENT_CAPTURE_BYTES) + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    content = b"".join(chunks)
    if (
        len(content) != capture.source_length_bytes
        or f"sha256:{hashlib.sha256(content).hexdigest()}" != capture.source_digest
    ):
        raise DocumentIntakeRefused("the retained copy does not verify")
    return content


def admit_extraction(
    runner: ServiceRunner,
    *,
    capture_id: str,
    profile_digest: str,
    expected_base_extraction_id: str | None,
    operation_key: str,
) -> tuple[store.StoredRevision, bool]:
    """Admit one extraction revision over a retained capture; returns the revision and whether it was a replay.

    The revision is bound to the capture's exact source digest, the profile, the next per-capture sequence and the
    next per-document ordinal, and it freezes the document's source removal generation and the capture's retention
    generation. It is head-eligible only if the expected base is the extraction head at this moment.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    _identifier(operation_key, "operation key")
    workspace_id = runner.workspace_id
    with _fence(runner):
        capture = store.read_capture(
            runner.connection, workspace_id=workspace_id, capture_id=capture_id
        )
        if capture is None:
            raise DocumentIntakeRefused("no such capture")
        request = store.request_digest(
            {
                "captureId": capture_id,
                "expectedBaseExtractionId": expected_base_extraction_id,
                "inputDigest": capture.source_digest,
                "profileDigest": profile_digest,
            }
        )
        existing = store.read_revision_by_key(
            runner.connection, workspace_id=workspace_id, operation_key=operation_key
        )
        if existing is not None:
            if existing.request_digest != request:
                raise DocumentIntakeRefused(
                    "the operation key already names a different extraction request"
                )
            return existing, True
        retention = store.read_retention(
            runner.connection, workspace_id=workspace_id, capture_id=capture_id
        )
        assert retention is not None
        if retention.retention_state != store.RETENTION_RETAINED:
            raise DocumentIntakeRefused("the capture is no longer retained")
        head = store.read_head(
            runner.connection, workspace_id=workspace_id, document_id=capture.document_id
        )
        assert head is not None
        if head.source_state != store.SOURCE_ACTIVE:
            raise DocumentIntakeRefused("the document's source is removed")
        if (
            store.read_profile(runner.connection, workspace_id=workspace_id, digest=profile_digest)
            is None
        ):
            raise DocumentIntakeRefused("the extraction profile is not registered")
        if expected_base_extraction_id is not None:
            base = store.read_revision(
                runner.connection,
                workspace_id=workspace_id,
                extraction_revision_id=expected_base_extraction_id,
            )
            if base is None or base.document_id != capture.document_id:
                raise DocumentIntakeRefused("the expected base is not an extraction of this document")
        sequence, ordinal = store.last_extraction_numbers(
            runner.connection,
            workspace_id=workspace_id,
            document_id=capture.document_id,
            capture_id=capture_id,
        )
        revision = store.StoredRevision(
            workspace_id=workspace_id,
            extraction_revision_id=store.revision_id_for(workspace_id, operation_key),
            document_id=capture.document_id,
            capture_id=capture_id,
            input_digest=capture.source_digest,
            profile_digest=profile_digest,
            extraction_sequence=sequence + 1,
            document_ordinal=ordinal + 1,
            expected_base_extraction_id=expected_base_extraction_id,
            head_eligible=int(expected_base_extraction_id == head.current_extraction_id),
            source_removal_generation=head.source_removal_generation,
            capture_retention_generation=retention.capture_retention_generation,
            operation_key=operation_key,
            request_digest=request,
            fencing_generation=runner.generation,
            created_at_us=_now_us(),
        )
        store.insert_revision(runner.connection, revision)
    return revision, False


def complete_extraction(
    runner: ServiceRunner, *, extraction_revision_id: str, outcome: ExtractionOutcome
) -> store.StoredResult:
    """Record the terminal result of a revision and, only if it is still eligible, make it the extraction head.

    One fenced transaction reads the current generations and heads, inserts the immutable result with the
    disposition they imply, and moves the extraction head only for `promoted`. A failed, unsupported, historical,
    fenced or superseded result never moves it. Completing a revision twice returns the stored result when the
    outcome is identical and is refused otherwise.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    workspace_id = runner.workspace_id
    if outcome.state not in store.RESULT_STATES:
        raise DocumentIntakeRefused("the extraction outcome has an unknown state")
    if outcome.state not in store.PUBLISHABLE_STATES and outcome.segments:
        raise DocumentIntakeRefused("a non-publishable extraction outcome cannot carry segments")
    try:
        limitations = to_canonical_json(dict(outcome.limitations))
    except (TypeError, ValueError) as error:
        raise DocumentIntakeRefused("limitations must be canonical JSON") from error
    with _fence(runner):
        revision = store.read_revision(
            runner.connection, workspace_id=workspace_id, extraction_revision_id=extraction_revision_id
        )
        if revision is None:
            raise DocumentIntakeRefused("no such extraction revision")
        segments, segments_digest = _validated_segments(
            outcome.segments,
            workspace_id=workspace_id,
            extraction_revision_id=revision.extraction_revision_id,
            capture_id=revision.capture_id,
            fencing_generation=runner.generation,
        )
        coverage = _validated_coverage(outcome.coverage, segments)
        now_us = _now_us()
        disposition = _disposition(runner.connection, revision, outcome.state)
        result = store.StoredResult(
            workspace_id=workspace_id,
            extraction_revision_id=extraction_revision_id,
            result_state=outcome.state,
            output_digest=outcome.output_digest,
            segments_digest=segments_digest,
            coverage_json=coverage,
            limitations_json=limitations,
            worker_build=outcome.worker_build,
            publication_disposition=disposition,
            fencing_generation=runner.generation,
            completed_at_us=now_us,
        )
        existing = store.read_result(
            runner.connection, workspace_id=workspace_id, extraction_revision_id=extraction_revision_id
        )
        if existing is not None:
            same = (
                existing.result_state,
                existing.output_digest,
                existing.segments_digest,
                existing.coverage_json,
                existing.limitations_json,
                existing.worker_build,
            ) == (
                outcome.state,
                outcome.output_digest,
                segments_digest,
                coverage,
                limitations,
                outcome.worker_build,
            )
            if not same:
                raise DocumentIntakeRefused("the extraction revision already has a different terminal result")
            return existing
        try:
            store.insert_result(runner.connection, result)
            store.insert_segments(runner.connection, segments)
        except sqlite3.IntegrityError as error:
            raise DocumentIntakeRefused("the extraction result is outside its closed shape") from error
        if disposition == "promoted":
            store.replace_projection(
                runner.connection,
                revision,
                segments,
                fencing_generation=runner.generation,
                now_us=now_us,
            )
            store.advance_extraction_head(
                runner.connection, revision, fencing_generation=runner.generation, now_us=now_us
            )
    return result


def _validated_segments(
    segments: tuple[ExtractedSegment, ...],
    *,
    workspace_id: str,
    extraction_revision_id: str,
    capture_id: str,
    fencing_generation: int,
) -> tuple[tuple[store.StoredSegment, ...], str]:
    if not isinstance(segments, tuple) or len(segments) > store.MAX_SEGMENTS_PER_REVISION:
        raise DocumentIntakeRefused("extraction segments are outside the accepted bound")
    stored: list[store.StoredSegment] = []
    identity: list[dict[str, Any]] = []
    for ordinal, segment in enumerate(segments, start=1):
        if not isinstance(segment, ExtractedSegment):
            raise DocumentIntakeRefused("an extraction segment has the wrong shape")
        if segment.semantic_role not in store.SEGMENT_ROLES:
            raise DocumentIntakeRefused("an extraction segment has an unknown semantic role")
        if segment.extraction_method not in store.SEGMENT_METHODS:
            raise DocumentIntakeRefused("an extraction segment has an unknown extraction method")
        if segment.visibility not in store.SEGMENT_VISIBILITIES:
            raise DocumentIntakeRefused("an extraction segment has an unknown visibility")
        if segment.revision_state not in store.SEGMENT_REVISIONS:
            raise DocumentIntakeRefused("an extraction segment has an unknown revision state")
        if (segment.text is None) == (segment.structured_value is None):
            raise DocumentIntakeRefused("an extraction segment needs exactly one content representation")
        if segment.text is not None and (
            not isinstance(segment.text, str)
            or not segment.text
            or len(segment.text.encode("utf-8")) > store.MAX_SEGMENT_JSON_BYTES
        ):
            raise DocumentIntakeRefused("an extraction segment's text is outside the accepted bound")
        if not isinstance(segment.source_locator, Mapping):
            raise DocumentIntakeRefused("an extraction segment locator has the wrong shape")
        locator = dict(segment.source_locator)
        if locator.get("captureId") != capture_id:
            raise DocumentIntakeRefused("an extraction segment locator must name its capture")
        try:
            locator_json = to_canonical_json(locator)
            value_json = (
                None if segment.structured_value is None else to_canonical_json(segment.structured_value)
            )
        except (TypeError, ValueError) as error:
            raise DocumentIntakeRefused("an extraction segment is not canonical JSON") from error
        if not locator or len(locator_json.encode("utf-8")) > store.MAX_SEGMENT_JSON_BYTES:
            raise DocumentIntakeRefused("an extraction segment locator is outside the accepted bound")
        if value_json is not None and len(value_json.encode("utf-8")) > store.MAX_SEGMENT_JSON_BYTES:
            raise DocumentIntakeRefused("an extraction segment value is outside the accepted bound")
        if segment.language is not None:
            _identifier(segment.language, "segment language")
        confidence = segment.confidence
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            raise DocumentIntakeRefused("an extraction segment confidence is outside zero through one")
        row = store.StoredSegment(
            workspace_id=workspace_id,
            extraction_revision_id=extraction_revision_id,
            segment_ordinal=ordinal,
            semantic_role=segment.semantic_role,
            text=segment.text,
            structured_value_json=value_json,
            source_locator_json=locator_json,
            extraction_method=segment.extraction_method,
            visibility=segment.visibility,
            revision_state=segment.revision_state,
            language=segment.language,
            confidence=None if confidence is None else float(confidence),
            fencing_generation=fencing_generation,
        )
        stored.append(row)
        identity.append(
            {
                "confidence": row.confidence,
                "extractionMethod": row.extraction_method,
                "language": row.language,
                "revisionState": row.revision_state,
                "semanticRole": row.semantic_role,
                "sourceLocator": locator,
                "structuredValue": segment.structured_value,
                "text": row.text,
                "visibility": row.visibility,
            }
        )
    canonical = to_canonical_json({"segments": identity})
    digest = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    return tuple(stored), digest


def _validated_coverage(
    coverage: Mapping[str, Any], segments: tuple[store.StoredSegment, ...]
) -> str:
    if not isinstance(coverage, Mapping):
        raise DocumentIntakeRefused("extraction coverage has the wrong shape")
    document = dict(coverage)
    if set(document) != {"schema", "units", "roleCoverage"}:
        raise DocumentIntakeRefused("extraction coverage fields are not the closed v2 shape")
    if document.get("schema") != "omnivia.extraction-coverage.v2":
        raise DocumentIntakeRefused("extraction coverage has the wrong schema")
    units = document.get("units")
    if not isinstance(units, list) or len(units) > 128:
        raise DocumentIntakeRefused("extraction coverage units are outside the accepted bound")
    count_fields = {
        "expected",
        "visited",
        "usefulText",
        "knownEmpty",
        "excludedByProfile",
        "unsupported",
        "failed",
        "unknownRemaining",
    }
    unit_types: set[str] = set()
    for unit in units:
        if not isinstance(unit, dict) or set(unit) != count_fields | {"unitType"}:
            raise DocumentIntakeRefused("an extraction coverage unit has the wrong shape")
        unit_type = unit.get("unitType")
        if (
            not isinstance(unit_type, str)
            or not unit_type
            or unit_type in unit_types
            or _IDENTIFIER.fullmatch(unit_type) is None
        ):
            raise DocumentIntakeRefused("extraction coverage unit types must be unique identifiers")
        unit_types.add(unit_type)
        if any(
            not isinstance(unit.get(field), int)
            or isinstance(unit.get(field), bool)
            or not 0 <= unit[field] <= 2**63 - 1
            for field in count_fields
        ):
            raise DocumentIntakeRefused("extraction coverage counts must be non-negative integers")
        if unit["visited"] != sum(
            unit[field]
            for field in ("usefulText", "knownEmpty", "excludedByProfile", "unsupported", "failed")
        ):
            raise DocumentIntakeRefused("extraction coverage visited count does not reconcile")
        if unit["expected"] != unit["visited"] + unit["unknownRemaining"]:
            raise DocumentIntakeRefused("extraction coverage expected count does not reconcile")
    role_coverage = document.get("roleCoverage")
    if not isinstance(role_coverage, dict):
        raise DocumentIntakeRefused("extraction role coverage has the wrong shape")
    if any(
        role not in store.SEGMENT_ROLES
        or not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        for role, value in role_coverage.items()
    ):
        raise DocumentIntakeRefused("extraction role coverage is outside the accepted role set")
    actual_roles = dict(Counter(segment.semantic_role for segment in segments))
    if role_coverage != actual_roles:
        raise DocumentIntakeRefused("extraction role coverage does not match its segments")
    try:
        return to_canonical_json(document)
    except (TypeError, ValueError) as error:
        raise DocumentIntakeRefused("extraction coverage must be canonical JSON") from error


def read_extraction_projection(
    runner: ServiceRunner,
    *,
    document_id: str,
    include_roles: frozenset[str] = store.DEFAULT_RETRIEVAL_ROLES,
    query: str | None = None,
) -> list[store.ProjectedSegment]:
    """Read/search only the current promoted projection and only the explicitly allowed content roles."""
    assert runner.connection is not None and runner.workspace_id is not None
    _identifier(document_id, "document id")
    try:
        return store.read_projection(
            runner.connection,
            workspace_id=runner.workspace_id,
            document_id=document_id,
            roles=include_roles,
            query=query,
        )
    except store.DocumentIntakeInvalid as error:
        raise DocumentIntakeRefused(str(error)) from error


def _disposition(connection: sqlite3.Connection, revision: store.StoredRevision, state: str) -> str:
    """What publishing this revision's result means right now, from the heads and generations as they stand."""
    if state not in store.PUBLISHABLE_STATES:
        return "not_published"
    if not revision.head_eligible:
        return "historical_only"
    head = store.read_head(
        connection, workspace_id=revision.workspace_id, document_id=revision.document_id
    )
    retention = store.read_retention(
        connection, workspace_id=revision.workspace_id, capture_id=revision.capture_id
    )
    assert head is not None and retention is not None
    if (
        head.source_state != store.SOURCE_ACTIVE
        or head.source_removal_generation != revision.source_removal_generation
    ):
        return "fenced_removal"
    if (
        retention.retention_state != store.RETENTION_RETAINED
        or retention.capture_retention_generation != revision.capture_retention_generation
    ):
        return "fenced_retention"
    if head.extraction_head_ordinal > revision.document_ordinal:
        return "superseded"
    return "promoted"


def reprocess_captured_copy(
    runner: ServiceRunner,
    *,
    capture_id: str,
    profile: dict[str, Any],
    expected_base_extraction_id: str | None,
    operation_key: str,
    extractor: Extractor,
) -> store.StoredResult:
    """Add an extraction revision over a capture's retained copy; never reread the original, never recapture.

    A replay of an operation key whose revision already has a result returns that result without consulting current
    retention or running the extractor again. For a new or unsettled operation, the retained copy is then read and
    verified, so a deleted, expired or corrupt copy is refused before extraction runs. An extractor that raises is
    recorded as a failed result with a fixed limitation, not as a refusal.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    try:
        digest = register_profile(runner, profile)
        with _fence(runner):
            capture = store.read_capture(
                runner.connection, workspace_id=runner.workspace_id, capture_id=capture_id
            )
            if capture is None:
                raise DocumentIntakeRefused("no such capture")
            request = store.request_digest(
                {
                    "captureId": capture_id,
                    "expectedBaseExtractionId": expected_base_extraction_id,
                    "inputDigest": capture.source_digest,
                    "profileDigest": digest,
                }
            )
            existing = store.read_revision_by_key(
                runner.connection, workspace_id=runner.workspace_id, operation_key=operation_key
            )
            if existing is not None:
                if existing.request_digest != request:
                    raise DocumentIntakeRefused(
                        "the operation key already names a different extraction request"
                    )
                stored = store.read_result(
                    runner.connection,
                    workspace_id=runner.workspace_id,
                    extraction_revision_id=existing.extraction_revision_id,
                )
                if stored is not None:
                    return stored
        content = read_retained_source(runner, capture_id=capture_id)
        revision, replayed = admit_extraction(
            runner,
            capture_id=capture_id,
            profile_digest=digest,
            expected_base_extraction_id=expected_base_extraction_id,
            operation_key=operation_key,
        )
    except store.DocumentIntakeInvalid as error:
        raise DocumentIntakeRefused(str(error)) from error
    if replayed:
        stored = store.read_result(
            runner.connection,
            workspace_id=runner.workspace_id,
            extraction_revision_id=revision.extraction_revision_id,
        )
        if stored is not None:
            return stored
    try:
        outcome = extractor(content, profile)
    except Exception:  # noqa: BLE001 - an injected extractor failure is a recorded terminal outcome
        outcome = ExtractionOutcome(
            state="failed",
            coverage={
                "schema": "omnivia.extraction-coverage.v2",
                "units": [],
                "roleCoverage": {},
            },
            limitations={"reason": "extractor_raised"},
            worker_build="unavailable",
        )
    return complete_extraction(
        runner, extraction_revision_id=revision.extraction_revision_id, outcome=outcome
    )


def complete_reprocess_in_transaction(
    runner: ServiceRunner,
    *,
    capture_id: str,
    profile: dict[str, Any],
    expected_base_extraction_id: str | None,
    operation_key: str,
    outcome: ExtractionOutcome | None,
) -> tuple[store.StoredRevision, store.StoredResult]:
    """Atomically admit and publish a retained-copy reprocess inside the caller's fence.

    The governed application mutation already owns the fenced transaction that also
    settles its audit and idempotency outcome, so this variant deliberately opens no
    nested transaction.  Expensive extraction happens before this call.  Every fact
    that can change while that work runs -- retained eligibility, source state, the
    expected extraction head and publication generations -- is read again here before
    the immutable revision and terminal result are written.

    ``outcome`` may be absent only for an exact domain replay that already has a
    terminal result.  That lets an application replay avoid running the extractor while
    still failing closed if its durable mutation settlement were ever missing.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    _identifier(operation_key, "operation key")
    workspace_id = runner.workspace_id
    connection = runner.connection
    now_us = _now_us()
    try:
        profile_digest = store.record_profile(
            connection,
            workspace_id=workspace_id,
            profile=profile,
            fencing_generation=runner.generation,
            now_us=now_us,
        )
        capture = store.read_capture(
            connection, workspace_id=workspace_id, capture_id=capture_id
        )
        if capture is None:
            raise DocumentIntakeRefused("no such capture")
        request = store.request_digest(
            {
                "captureId": capture_id,
                "expectedBaseExtractionId": expected_base_extraction_id,
                "inputDigest": capture.source_digest,
                "profileDigest": profile_digest,
            }
        )
        revision = store.read_revision_by_key(
            connection, workspace_id=workspace_id, operation_key=operation_key
        )
        if revision is not None:
            if revision.request_digest != request:
                raise DocumentIntakeRefused(
                    "the operation key already names a different extraction request"
                )
            existing = store.read_result(
                connection,
                workspace_id=workspace_id,
                extraction_revision_id=revision.extraction_revision_id,
            )
            if existing is not None:
                return revision, existing
        else:
            retention = store.read_retention(
                connection, workspace_id=workspace_id, capture_id=capture_id
            )
            assert retention is not None
            if retention.retention_state != store.RETENTION_RETAINED:
                raise DocumentIntakeRefused("the capture is no longer retained")
            head = store.read_head(
                connection, workspace_id=workspace_id, document_id=capture.document_id
            )
            assert head is not None
            if head.source_state != store.SOURCE_ACTIVE:
                raise DocumentIntakeRefused("the document's source is removed")
            if expected_base_extraction_id is not None:
                base = store.read_revision(
                    connection,
                    workspace_id=workspace_id,
                    extraction_revision_id=expected_base_extraction_id,
                )
                if base is None or base.document_id != capture.document_id:
                    raise DocumentIntakeRefused(
                        "the expected base is not an extraction of this document"
                    )
            sequence, ordinal = store.last_extraction_numbers(
                connection,
                workspace_id=workspace_id,
                document_id=capture.document_id,
                capture_id=capture_id,
            )
            revision = store.StoredRevision(
                workspace_id=workspace_id,
                extraction_revision_id=store.revision_id_for(workspace_id, operation_key),
                document_id=capture.document_id,
                capture_id=capture_id,
                input_digest=capture.source_digest,
                profile_digest=profile_digest,
                extraction_sequence=sequence + 1,
                document_ordinal=ordinal + 1,
                expected_base_extraction_id=expected_base_extraction_id,
                head_eligible=int(expected_base_extraction_id == head.current_extraction_id),
                source_removal_generation=head.source_removal_generation,
                capture_retention_generation=retention.capture_retention_generation,
                operation_key=operation_key,
                request_digest=request,
                fencing_generation=runner.generation,
                created_at_us=now_us,
            )
            store.insert_revision(connection, revision)

        if outcome is None:
            raise DocumentIntakeRefused("the reprocess has no terminal extraction outcome")
        if outcome.state not in store.RESULT_STATES:
            raise DocumentIntakeRefused("the extraction outcome has an unknown state")
        if outcome.state not in store.PUBLISHABLE_STATES and outcome.segments:
            raise DocumentIntakeRefused(
                "a non-publishable extraction outcome cannot carry segments"
            )
        try:
            limitations = to_canonical_json(dict(outcome.limitations))
        except (TypeError, ValueError) as error:
            raise DocumentIntakeRefused("limitations must be canonical JSON") from error
        segments, segments_digest = _validated_segments(
            outcome.segments,
            workspace_id=workspace_id,
            extraction_revision_id=revision.extraction_revision_id,
            capture_id=revision.capture_id,
            fencing_generation=runner.generation,
        )
        coverage = _validated_coverage(outcome.coverage, segments)
        disposition = _disposition(connection, revision, outcome.state)
        result = store.StoredResult(
            workspace_id=workspace_id,
            extraction_revision_id=revision.extraction_revision_id,
            result_state=outcome.state,
            output_digest=outcome.output_digest,
            segments_digest=segments_digest,
            coverage_json=coverage,
            limitations_json=limitations,
            worker_build=outcome.worker_build,
            publication_disposition=disposition,
            fencing_generation=runner.generation,
            completed_at_us=now_us,
        )
        store.insert_result(connection, result)
        store.insert_segments(connection, segments)
        if disposition == "promoted":
            store.replace_projection(
                connection,
                revision,
                segments,
                fencing_generation=runner.generation,
                now_us=now_us,
            )
            store.advance_extraction_head(
                connection,
                revision,
                fencing_generation=runner.generation,
                now_us=now_us,
            )
        return revision, result
    except store.DocumentIntakeInvalid as error:
        raise DocumentIntakeRefused(str(error)) from error


def expire_capture(runner: ServiceRunner, *, capture_id: str, deleted: bool = False) -> int:
    """Move a capture's retention to expired (or deleted) and return its new generation.

    This changes eligibility only: it does not remove the blob, and it does not demote a capture head.
    """
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    try:
        with _fence(runner):
            return store.set_capture_retention(
                runner.connection,
                workspace_id=runner.workspace_id,
                capture_id=capture_id,
                state="deleted" if deleted else "expired",
                fencing_generation=runner.generation,
                now_us=_now_us(),
            )
    except store.DocumentIntakeInvalid as error:
        raise DocumentIntakeRefused(str(error)) from error


def set_document_source_state(runner: ServiceRunner, *, document_id: str, removed: bool) -> int:
    """Suppress (`removed`) or restore a document's source and return its new source removal generation."""
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.generation is not None
    try:
        with _fence(runner):
            return store.set_source_state(
                runner.connection,
                workspace_id=runner.workspace_id,
                document_id=document_id,
                state=store.SOURCE_REMOVED if removed else store.SOURCE_ACTIVE,
                fencing_generation=runner.generation,
                now_us=_now_us(),
            )
    except store.DocumentIntakeInvalid as error:
        raise DocumentIntakeRefused(str(error)) from error
