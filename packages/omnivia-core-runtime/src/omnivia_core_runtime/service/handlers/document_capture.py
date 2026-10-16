"""Governed application handlers for document capture, retained-copy reprocessing and the
authoritative intake history the two of them write.

`document_history` is the read, and it is at the bottom of this module with its own long
comment. Two properties belong here because they are properties of the *family* rather than
of any one handler:

**The history is derived from the heads, never from the order it reads rows in.** A capture
and an extraction are each append-only and each carry an ordering number, and neither number
says which member is current. `omnivia_document_heads` says, in two independent columns that
move through two independent writes, and the read copies those columns rather than inferring
them. A page of history that implied currentness from its last row would contradict the one
transaction that decides it.

**What a caller may reprocess is Core's statement, not the caller's.** The retained capture,
the approved profile selections and the frozen extraction base all come back from the read,
and `document.reprocess` accepts exactly those. Nothing in the read invites a caller to
compose a profile of its own, and nothing in the mutation accepts one.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INTERNAL_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    RETRY_CLASS_RETRYABLE,
    ContractDecodeError,
    ContractSemanticError,
    decode_document_history_input,
    idempotency_equivalence,
    validate_document_history_result,
)
from omnivia_core.contracts.v1.generated import (
    DocumentCaptureHistoryEntry,
    DocumentCaptureInput,
    DocumentCaptureResult,
    DocumentExtractionHeadState,
    DocumentExtractionHistoryEntry,
    DocumentExtractionProfileSummary,
    DocumentHistoryAction,
    DocumentHistoryInput,
    DocumentHistoryResult,
    DocumentOriginalDisconnectInput,
    DocumentOriginalDisconnectResult,
    DocumentOriginalRelinkInput,
    DocumentOriginalRelinkResult,
    DocumentProcessingProfileInput,
    DocumentReprocessInput,
    DocumentReprocessProfileOption,
    DocumentReprocessResult,
    DocumentSourceHeadState,
    PageMetadata,
    is_evidence_checksum,
)
from omnivia_core_runtime.ownership.fencing import read_guard
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.document_extractors import (
    DocumentExtractionInvalid,
    DocumentExtractionUnsupported,
    extract_document,
)
from omnivia_core_runtime.service.document_intake import (
    BindingClaim,
    DocumentCaptureSuppressed,
    DocumentIntakeRefused,
    ExtractionOutcome,
    _check_route,
    _identifier,
    admit_document_capture_in_transaction,
    complete_reprocess_in_transaction,
    move_original_in_transaction,
    read_retained_source,
    relink_original_in_transaction,
)
from omnivia_core_runtime.service.document_processing import (
    CatalogueSource,
    ReprocessProfileOption,
    approved_reprocess_profiles,
    held_selection,
)
from omnivia_core_runtime.service.document_transfer import (
    DocumentTransferRefused,
    read_staged_document,
)
from omnivia_core_runtime.service.mutation import (
    MutationSettlementContext,
    execute_mutation,
    issue_mutation_grant,
)
from omnivia_core_runtime.service.ocr_resources import OcrSelectionInvalid
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
)
from omnivia_core_runtime.service.pagination import (
    PROCESS_CONTINUATION_TOKENS,
    ContinuationTokenCodec,
    token_digest,
)
from omnivia_core_runtime.service.processing_settings import (
    ProcessingSettings,
    ProcessingSettingsInvalid,
    managed_resources,
    read_settings,
)
from omnivia_core_runtime.service.source_capture import (
    SourceCaptureRefused,
    _validate_request,
    publish_blob,
)
from omnivia_core_runtime.storage import document_intake as store
from omnivia_core_runtime.storage.app_settings import AppSettingsError
from omnivia_core_runtime.storage.memory import IdentifierAllocator
from omnivia_core_runtime.workspace.blob_publication import BlobPublicationRefused

DOCUMENT_CAPTURE_OPERATION: Final = "document.capture"
DOCUMENT_REPROCESS_OPERATION: Final = "document.reprocess"
DOCUMENT_HISTORY_OPERATION: Final = "document.history"
DOCUMENT_ORIGINAL_DISCONNECT_OPERATION: Final = "document.original.disconnect"
DOCUMENT_ORIGINAL_RELINK_OPERATION: Final = "document.original.relink"
_MAX_INLINE_BYTES: Final = 3 * 1024 * 1024 - 64 * 1024
_MAX_ENCODED_BYTES: Final = 4 * ((_MAX_INLINE_BYTES + 2) // 3)
_MESSAGE_INVALID: Final = "the request payload is not a valid document capture"
_MESSAGE_REPROCESS_INVALID: Final = "the request payload is not a valid document reprocess"
_MESSAGE_TOO_LARGE: Final = "the transferred document exceeds the capture size limit"
_MESSAGE_NO_STORAGE: Final = "this service instance is not serving authoritative storage"
_MESSAGE_UNAVAILABLE: Final = "the transferred document could not be preserved"
_TOP_LEVEL_KEYS: Final = frozenset(
    {
        "document_id", "content_base64", "staged_source", "media_type", "requested_source_identity",
        "intent_kind", "base", "target_binding_revision", "processing_profile",
        "original_binding", "consent", "bulk_origin", "display_name",
    }
)
_NESTED_KEYS: Final[dict[str, frozenset[str]]] = {
    "base": frozenset({"kind", "capture_id"}),
    "processing_profile": frozenset({"profile_ref", "revision"}),
    "original_binding": frozenset({"owner_ref", "generation", "route_kind", "root_ref", "member_ref"}),
    "consent": frozenset({"subscription_id", "consent_generation", "config_generation", "dirty_epoch"}),
    "bulk_origin": frozenset({"bulk_run_id", "manifest_revision", "member_id"}),
    "staged_source": frozenset(
        {"staged_source_ref", "source_kind", "content_checksum", "content_length_bytes", "media_type", "source_version"}
    ),
}
_REPROCESS_TOP_LEVEL_KEYS: Final = frozenset(
    {"capture_id", "processing_profile", "expected_base"}
)
_REPROCESS_NESTED_KEYS: Final[dict[str, frozenset[str]]] = {
    "processing_profile": frozenset(
        {"profile_ref", "revision", "expected_profile_digest"}
    ),
    "expected_base": frozenset({"extraction_id"}),
}
#: Why a reprocess was refused before anything ran. Kept distinct from the decode refusal
#: because this one is about the *world* having moved, which is what a caller re-reads
#: history to recover from, rather than about a payload it could correct on its own.
_MESSAGE_REPROCESS_SUBSTITUTED: Final = (
    "the selected processing profile is no longer the approved profile this capture "
    "resolves to"
)
_MESSAGE_DISCONNECT_INVALID: Final = "the request payload is not a valid original disconnect"
_MESSAGE_DISCONNECT_REFUSED: Final = (
    "the original route belongs to another document or is on a different route"
)
_MESSAGE_DISCONNECT_UNAVAILABLE: Final = "the original disconnect could not be recorded"
_DISCONNECT_KEYS: Final = frozenset(
    {"document_id", "owner_ref", "route_kind", "root_ref", "member_ref"}
)
_MESSAGE_RELINK_INVALID: Final = "the request payload is not a valid original relink"
_MESSAGE_RELINK_REFUSED: Final = (
    "the original route belongs to another document, is on a different route, or the "
    "relink generation is not a newer generation Core can reach"
)
_MESSAGE_RELINK_UNAVAILABLE: Final = "the original relink could not be recorded"
_MAX_BINDING_GENERATION: Final = 2**63 - 1
_RELINK_KEYS: Final = frozenset({"document_id", "owner_ref", "binding_generation"})
_MESSAGE_HISTORY_INVALID: Final = "the request payload is not a valid document history read"
_MESSAGE_HISTORY_NOT_FOUND: Final = "this workspace holds no such document"
_MESSAGE_HISTORY_UNREADABLE: Final = "this document's recorded intake history is not readable"
_HISTORY_TOP_LEVEL_KEYS: Final = frozenset({"document_id", "limit", "page"})
_HISTORY_NESTED_KEYS: Final[dict[str, frozenset[str]]] = {
    "page": frozenset({"continuation_token"})
}
_MESSAGE_HISTORY_MOVED: Final = (
    "this document changed since the page this continuation was issued for; read the "
    "history again from its first page"
)
#: The caller did nothing wrong here: the document moved while this page's own rows were
#: being read, so the page could only be served from two instants. Recoverable rather than
#: invalid, because the same request retried against a settled document answers.
_MESSAGE_HISTORY_RACED: Final = (
    "this document changed while this page was being assembled; read the history again"
)
#: The continuation token's exact key set: the request binding, the state snapshot, the two
#: independent offsets and the version. A token carrying anything else, or missing one of
#: these, is refused.
_HISTORY_TOKEN_KEYS: Final = frozenset({"b", "c", "s", "v", "x"})
#: The page a request that names no limit gets. Well under the catalogue's `max_page_size`,
#: and stated here so a page's size is always a decision this build made.
DEFAULT_HISTORY_PAGE_LIMIT: Final = 20
#: The ceiling this handler serves, whatever larger limit the catalogue's schema admits.
MAX_HISTORY_PAGE_LIMIT: Final = store.MAX_HISTORY_PAGE
_ACTION_REPROCESS: Final = "reprocess_captured_copy"
_ACTION_REFRESH: Final = "refresh_original"
_REASON_SOURCE_REMOVED: Final = "source_removed"
_REASON_NOT_RETAINED: Final = "source_not_retained"
_REASON_NOT_QUALIFIED: Final = "format_not_qualified"
_REASON_NO_BINDING: Final = "no_original_binding"
_REASON_DISCONNECTED: Final = "original_disconnected"
_PROCESSING_NOT_QUEUED: Final = "not_queued"


def _closed(payload: object) -> bool:
    if not isinstance(payload, Mapping) or not set(payload) <= _TOP_LEVEL_KEYS:
        return False
    for name, allowed in _NESTED_KEYS.items():
        value = payload.get(name)
        if value is not None and (not isinstance(value, Mapping) or not set(value) <= allowed):
            return False
    return True


def _closed_reprocess(payload: object) -> bool:
    if not isinstance(payload, Mapping) or not set(payload) <= _REPROCESS_TOP_LEVEL_KEYS:
        return False
    for name, allowed in _REPROCESS_NESTED_KEYS.items():
        value = payload.get(name)
        if value is not None and (not isinstance(value, Mapping) or not set(value) <= allowed):
            return False
    return True


def _closed_disconnect(payload: object) -> bool:
    return isinstance(payload, Mapping) and set(payload) <= _DISCONNECT_KEYS


def _closed_relink(payload: object) -> bool:
    return isinstance(payload, Mapping) and set(payload) <= _RELINK_KEYS


def _closed_history(payload: object) -> bool:
    """Whether the wire payload carries only this v1 read's declared fields.

    The generated decoder is deliberately tolerant of an unknown field so a newer peer's
    additive release still decodes. This read is not: an unrecognised selector is a request
    this build cannot answer as asked, and answering it under the fields it does recognise
    would silently serve a different read.
    """
    if not isinstance(payload, Mapping) or not set(payload) <= _HISTORY_TOP_LEVEL_KEYS:
        return False
    for name, allowed in _HISTORY_NESTED_KEYS.items():
        value = payload.get(name)
        if value is not None and (not isinstance(value, Mapping) or not set(value) <= allowed):
            return False
    return True


def _history_snapshot(
    *,
    document: store.StoredDocument,
    head: store.DocumentHead,
    binding_state: str,
    published_at_us: int | None,
    captures: Sequence[tuple[Any, ...]],
    extractions: Sequence[tuple[Any, ...]],
    reprocess_options: Mapping[str, Sequence[ReprocessProfileOption]],
) -> str:
    """One digest of every fact that can change what a page of this history says.

    Not a cheap proxy for one. The two heads decide every `is_current_*` flag; the two
    fingerprint lists decide the totals, each capture's retention and processing state and
    each revision's outcome and disposition; the binding aggregate and the published
    projection decide the document-level action and the retrieval state. A caller continuing
    under this digest is continuing within one state of all of them, and a page that would
    have been answered from any other state is refused instead.

    `reprocess_options` is in here because the database is not the whole of what a page
    says. The approved selections a capture is offered are resolved from the installation's
    decoder build, its OCR catalogue and its installed recognition resources, and none of
    those leaves a trace in any row this digest reads. Without them a newly installed or
    newly missing model would change page two's offered profiles and digests while every
    database fact stood still, and the series would be stitched across two resolutions. The
    canonical form here is the complete option identity a page publishes -- reference,
    revision, processing mode and profile digest, in the resolved order -- so a change to any
    of them refuses the continuation.

    `fencing_generation` is in here as well: it is not read into the result, but a document
    whose ownership generation advanced was written to by another owner, which is not a state
    a half-read history should be stitched across.

    The service connection is shared with the instance's other threads and `BEGIN IMMEDIATE`
    does not nest, so a read handler here does not open a transaction of its own -- the same
    posture every other read in this build takes. This digest is therefore used twice per
    page rather than relied on once: once before the page's rows are read, to bind the
    *series*, and once after they are assembled, to bind the *page*. A document that moved
    mid-assembly fails the second comparison and the page is refused as retriable rather than
    served from two instants; a document that moved between two pages fails the first and the
    caller re-reads from the beginning instead of stitching.
    """
    return token_digest(
        {
            "binding_state": binding_state,
            "captures": [list(row) for row in captures],
            "created_at_us": document.created_at_us,
            "extractions": [list(row) for row in extractions],
            "fencing_generation": document.fencing_generation,
            "head": [
                head.source_state,
                head.source_removal_generation,
                head.current_capture_id,
                head.capture_head_sequence,
                head.current_extraction_id,
                head.extraction_head_ordinal,
                head.updated_at_us,
            ],
            "published_at_us": published_at_us,
            "reprocess_options": {
                media_type: [
                    [
                        option.profile_ref,
                        option.revision,
                        option.processing_mode,
                        option.profile_digest,
                    ]
                    for option in options
                ]
                for media_type, options in reprocess_options.items()
            },
            "snapshot_version": 2,
        }
    )


def _history_offsets(
    supplied: Mapping[str, Any] | None, snapshot: str
) -> tuple[int, int]:
    """The two offsets this page starts at, once the snapshot it was issued for is known.

    A first page starts at zero. A continuation starts where its token says, but only if the
    document is still in the state that token was issued from: otherwise the pages either
    side of the change would describe a history no instant ever held, and the caller is told
    to read again from the beginning rather than handed a composite.
    """
    if supplied is None:
        return 0, 0
    if supplied.get("s") != snapshot:
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_HISTORY_MOVED)
    return int(supplied["c"]), int(supplied["x"])


def _timestamp(value: int) -> str:
    """One durable microsecond instant as the contract's RFC 3339 `Timestamp`."""
    moment = datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    milliseconds = moment.microsecond // 1000
    if milliseconds == 0:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds:03d}Z"


def _action(name: str, reason: str | None) -> DocumentHistoryAction:
    """One action, available exactly when nothing makes it unavailable."""
    return DocumentHistoryAction(
        action=name, available=reason is None, unavailable_reason=reason
    )


def _reprocess_refusal(
    *, source_active: bool, retained: bool, options: Sequence[Any]
) -> str | None:
    """Why this capture cannot be reprocessed, or `None` when it can.

    Order is the order Core itself refuses in: a removed logical source stops every capture
    under it, a capture whose bytes are gone cannot be read again whatever the source says,
    and a format with no qualified adapter has no profile to run.
    """
    if not source_active:
        return _REASON_SOURCE_REMOVED
    if not retained:
        return _REASON_NOT_RETAINED
    if not options:
        return _REASON_NOT_QUALIFIED
    return None


def _refresh_refusal(*, source_active: bool, binding_state: str) -> str | None:
    """Why this document's original cannot be read again, or `None` when it can.

    Core's half of the answer only. It says whether a live route is recorded and the source
    is not removed; whether the native owner can still resolve that route is the owner's own
    check, which no canonical row can make on its behalf.
    """
    if not source_active:
        return _REASON_SOURCE_REMOVED
    if binding_state == store.BINDING_NONE:
        return _REASON_NO_BINDING
    if binding_state == store.BINDING_DISCONNECTED:
        return _REASON_DISCONNECTED
    return None


def _decode_content(value: str) -> bytes:
    if len(value) > _MAX_ENCODED_BYTES:
        raise OperationError(ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_TOO_LARGE)
    invalid = False
    try:
        content = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        invalid = True
        content = b""
    if invalid:
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
    if not content:
        raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
    if len(content) > _MAX_INLINE_BYTES:
        raise OperationError(ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_TOO_LARGE)
    return content


def _binding(request: DocumentCaptureInput) -> BindingClaim | None:
    value = request.original_binding
    if value is None:
        return None
    claim = BindingClaim(
        owner_ref=value.owner_ref,
        generation=value.generation,
        route_kind=value.route_kind,
        root_ref=value.root_ref,
        member_ref=value.member_ref,
    )
    _identifier(claim.owner_ref, "owner reference")
    _check_route(claim.route_kind, claim.root_ref, claim.member_ref)
    return claim


def _validate(request: DocumentCaptureInput) -> tuple[str | None, BindingClaim | None]:
    if (request.content_base64 is None) == (request.staged_source is None):
        raise DocumentIntakeRefused("capture needs exactly one content source")
    _identifier(request.document_id, "document id")
    _identifier(request.requested_source_identity, "source identity")
    _identifier(request.target_binding_revision, "target binding revision")
    _identifier(request.processing_profile.profile_ref, "profile reference")
    _identifier(request.processing_profile.revision, "profile revision")
    _validate_request(request.document_id, request.media_type)
    if request.intent_kind not in store.INTENTS:
        raise DocumentIntakeRefused("unknown capture intent")
    if request.base.kind == "creation":
        if request.base.capture_id is not None or request.intent_kind != store.INTENT_NEW_CAPTURE:
            raise DocumentIntakeRefused("creation base contradicts the capture intent")
        expected = None
    elif request.base.kind == "existing":
        if request.intent_kind == store.INTENT_NEW_CAPTURE:
            raise DocumentIntakeRefused("existing base contradicts the capture intent")
        expected = request.base.capture_id
        if expected is not None:
            _identifier(expected, "expected capture")
    else:
        raise DocumentIntakeRefused("unknown capture base kind")
    binding = _binding(request)
    automatic = request.intent_kind == store.INTENT_AUTOMATIC
    folder = request.intent_kind == store.INTENT_FOLDER
    if automatic != (request.consent is not None) or automatic and binding is None:
        raise DocumentIntakeRefused("automatic capture consent lineage is incomplete")
    if folder != (request.bulk_origin is not None):
        raise DocumentIntakeRefused("folder capture bulk lineage is incomplete")
    if folder and (binding is None or binding.route_kind != store.ROUTE_ROOT_MEMBER):
        raise DocumentIntakeRefused("folder capture needs a root-member binding")
    return expected, binding


@dataclass(frozen=True)
class DocumentCaptureHandlers:
    service: Any
    session: AuthenticatedSession
    binding: ServiceBinding
    clock: Clock
    allocate_identifier: IdentifierAllocator
    token_codec: ContinuationTokenCodec = PROCESS_CONTINUATION_TOKENS
    #: How this installation's managed OCR cache answers a selection, and holds
    #: what it answers. The same authority `DocumentProcessingExecutor` is given,
    #: composed once in `service/application.py` from the installation being
    #: served, because the profiles `document.history` offers and the one
    #: `document.reprocess` executes are resolved against installed resources and
    #: an installation that is not asked reports every one of them unavailable.
    #: ``None`` reads the packaged fail-closed catalogue, which serves nothing.
    catalogue_for: CatalogueSource | None = None

    def _processing_settings(
        self, connection: Any, workspace_id: str, message: str
    ) -> ProcessingSettings:
        """This workspace's current document-processing preferences, read once.

        A reprocess is an explicit new decision, so it resolves against what the
        workspace wants *now* -- not against the snapshot the original capture froze,
        which is the executor's authority and not this family's. Read once per request
        and threaded into the one `approved_reprocess_profiles` call, so the options a
        caller is offered and the option that then executes come from one read.

        A stored document that does not decode refuses rather than falling back to the
        product defaults: offering profiles resolved from defaults the workspace did not
        choose would report an approved selection nobody approved.
        """
        try:
            settings, _revision = read_settings(connection, workspace_id=workspace_id)
        except (ProcessingSettingsInvalid, AppSettingsError, sqlite3.Error) as error:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, message
            ) from error
        return settings

    def document_history(self, context: OperationContext) -> Mapping[str, Any]:
        """Answer one `document.history` for one document of the authorized workspace.

        Read-only in the literal sense: no fenced transaction, no mutation grant, no
        identifier allocation and no write of any kind. The live guard is read once, for the
        same reason the other reads in this build read it -- an instance that is not serving
        authoritative storage must refuse rather than answer from nothing.

        **The workspace is the authorized one.** `context.workspace_id` comes from the
        session grant and the endpoint binding after the authorization seam refused every
        workspace they disagreed on. `DocumentHistoryInput` carries no workspace field by
        contract, and none is read: a document of another workspace is `not_found` here, not
        a denial that would confirm it exists.

        **Pagination is two independent offsets under one token.** The two histories are
        separate facts with separate lengths, so one cursor would have to interleave them and
        the result would stop being two histories. The token carries both offsets, a digest
        of the request it was issued for, and a digest of the *state* the first page was
        answered from, so it cannot be replayed against another document, another principal,
        another workspace, another page size, or a document that has moved since.

        **A continuation series is one snapshot or it is refused.** Append-only tables make
        the offsets stable, but that is not enough on its own: the heads, the totals, each
        capture's retention and processing obligation, each revision's result and
        disposition, the binding aggregate and the published projection can all move between
        two pages, and every one of them changes what a page says -- which member is current,
        which action is offered. So can the installation itself and the workspace's
        document-processing preferences, neither of which is in any of these rows: the
        approved profiles a capture is offered are resolved from the decoder build, the OCR
        catalogue, the installed recognition resources and the current preferences. Pages
        stitched across two such states would be a history no instant ever held.
        `_history_snapshot` digests all of
        those facts for the whole document *including* this request's resolved profile
        options, the token carries that digest, and a continuation whose recomputed digest
        differs is refused so the caller re-reads from the first page rather than assembling
        a composite. The same digest is recomputed once more after this page's own rows are
        assembled, so a write landing mid-page is refused as retriable rather than served as
        rows from one instant beside heads from another.

        **Nothing derived is inferred.** `is_current_source` and `is_current_extraction` are
        read from `omnivia_document_heads`; the per-capture reprocess availability is the
        same retention and source-state check `complete_reprocess_in_transaction` makes; and
        the offered profiles come from the same `approved_reprocess_profiles` call the
        reprocess handler resolves its execution from, so the digest a caller is shown is
        the digest of the settings that will run. That call is made once per distinct media
        type for the whole request, before the snapshot, and page assembly reads the result
        rather than resolving again: two captures of one media type, on one page or on two,
        are therefore offered the same options by construction and not by coincidence.

        **What is not here.** No source bytes, no blob path, no original filesystem path,
        bookmark or owner reference, no credential, no consent or bulk lineage, no caller
        display claim, no operation key or request digest, and no profile body. The original
        routes reach the result only as one aggregate word.
        """
        request = self._decode_history(context)
        connection = getattr(self.service, "connection", None)
        guard = None if connection is None else read_guard(connection)
        if connection is None or guard is None:
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        workspace_id = context.workspace_id

        limit = (
            DEFAULT_HISTORY_PAGE_LIMIT
            if request.limit is None
            else min(int(request.limit), MAX_HISTORY_PAGE_LIMIT)
        )
        binding_digest = token_digest(
            {
                "binding_version": 1,
                "document": request.document_id,
                "limit": limit,
                "operation": DOCUMENT_HISTORY_OPERATION,
                "principal": context.principal,
                "workspace": workspace_id,
            }
        )
        supplied = self._history_token(request, binding_digest)

        document = store.read_document(
            connection, workspace_id=workspace_id, document_id=request.document_id
        )
        if document is None:
            raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_HISTORY_NOT_FOUND)
        head = store.read_head(
            connection, workspace_id=workspace_id, document_id=request.document_id
        )
        if head is None:
            # Every document gets its head row in the same transaction that creates it, so
            # one without a head is a broken invariant rather than a caller's mistake.
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_HISTORY_UNREADABLE
            )
        source_active = head.source_state == store.SOURCE_ACTIVE

        unreadable = False
        raced = False
        try:
            capture_facts = store.capture_fingerprints(
                connection, workspace_id=workspace_id, document_id=request.document_id
            )
            extraction_facts = store.extraction_fingerprints(
                connection, workspace_id=workspace_id, document_id=request.document_id
            )
            binding_state = store.document_binding_state(
                connection, workspace_id=workspace_id, document_id=request.document_id
            )
            published_at_us = store.projection_published_at(
                connection, workspace_id=workspace_id, document_id=request.document_id
            )
            # Both totals come from the same read the snapshot digest was taken over, so a
            # page's totals and its continuation can never describe two different instants.
            capture_count = len(capture_facts)
            extraction_count = len(extraction_facts)
            # Resolved once per distinct media type, for the whole request, before the
            # snapshot is taken. A capture is only offered profiles when its logical source
            # is active and its bytes are retained, so those are the only media types worth
            # resolving -- and resolving them here rather than inside page assembly is what
            # puts the result inside the snapshot: every page of one series then reports the
            # single resolution this mapping holds, and installed resources cannot move
            # beneath page two without the continuation being refused.
            settings = self._processing_settings(
                connection, workspace_id, _MESSAGE_HISTORY_UNREADABLE
            )
            # One lease for the whole resolution. Every media type of one workspace
            # names the same recognition order, so the installation publishes one
            # projection and holds it once: the digests this page offers are the
            # digests of resources that were still installed when they were taken.
            with held_selection(self.catalogue_for) as catalogue_for:
                reprocess_options = {
                    media_type: approved_reprocess_profiles(
                        media_type=media_type,
                        settings=settings,
                        catalogue_for=catalogue_for,
                    )
                    for media_type in sorted(
                        {
                            row[store.CAPTURE_FACT_MEDIA_TYPE]
                            for row in capture_facts
                            if source_active
                            and row[store.CAPTURE_FACT_RETENTION_STATE]
                            == store.RETENTION_RETAINED
                        }
                    )
                }
            snapshot = _history_snapshot(
                document=document,
                head=head,
                binding_state=binding_state,
                published_at_us=published_at_us,
                captures=capture_facts,
                extractions=extraction_facts,
                reprocess_options=reprocess_options,
            )
            capture_offset, extraction_offset = _history_offsets(supplied, snapshot)
            if capture_offset > capture_count or extraction_offset > extraction_count:
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_HISTORY_INVALID)
            capture_rows = store.read_capture_history(
                connection,
                workspace_id=workspace_id,
                document_id=request.document_id,
                offset=capture_offset,
                limit=limit,
            )
            extraction_rows = store.read_extraction_history(
                connection,
                workspace_id=workspace_id,
                document_id=request.document_id,
                offset=extraction_offset,
                limit=limit,
            )
            extractions = tuple(
                self._extraction_entry(connection, row, head=head, workspace_id=workspace_id)
                for row in extraction_rows
            )
            captures = tuple(
                self._capture_entry(
                    row,
                    head=head,
                    source_active=source_active,
                    reprocess_options=reprocess_options,
                )
                for row in capture_rows
            )
            # The page is now assembled, and its rows were read after the snapshot was
            # taken. On a shared connection with no transaction of its own, that is a window
            # a write can land in: the heads, counts and actions above would then come from
            # one instant and these rows from another. So the document-wide facts are read
            # again and digested again, and a page whose digest moved is refused. This is a
            # read-then-verify, not a nested transaction -- there is nothing to roll back and
            # nothing is held across it.
            settled_document = store.read_document(
                connection, workspace_id=workspace_id, document_id=request.document_id
            )
            settled_head = store.read_head(
                connection, workspace_id=workspace_id, document_id=request.document_id
            )
            raced = (
                settled_document is None
                or settled_head is None
                or snapshot
                != _history_snapshot(
                    document=settled_document,
                    head=settled_head,
                    binding_state=store.document_binding_state(
                        connection,
                        workspace_id=workspace_id,
                        document_id=request.document_id,
                    ),
                    published_at_us=store.projection_published_at(
                        connection,
                        workspace_id=workspace_id,
                        document_id=request.document_id,
                    ),
                    captures=store.capture_fingerprints(
                        connection,
                        workspace_id=workspace_id,
                        document_id=request.document_id,
                    ),
                    extractions=store.extraction_fingerprints(
                        connection,
                        workspace_id=workspace_id,
                        document_id=request.document_id,
                    ),
                    # The one resolution this request made, deliberately not a second one:
                    # a single resolution per request is what makes the page internally one
                    # state, so re-resolving here could only manufacture a disagreement with
                    # a page that is itself consistent.
                    reprocess_options=reprocess_options,
                )
            )
        except (store.DocumentIntakeInvalid, OcrSelectionInvalid, sqlite3.Error):
            # Contained rather than chained: a storage refusal names rows, digests and
            # generations, and this refusal becomes a wire error a caller reads. An invalid
            # recognition selection lands here too: there is then no approved profile to
            # report, and reporting one anyway is exactly the substitution this read refuses.
            unreadable = True
        if unreadable:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_HISTORY_UNREADABLE
            )
        if raced:
            # Stated retryable explicitly: `internal_recoverable` is frozen to that class,
            # and the default `non_retryable` would contradict it, which the response
            # validator turns into a bare `internal_non_recoverable` carrying none of this.
            raise OperationError(
                ERROR_CODE_INTERNAL_RECOVERABLE,
                _MESSAGE_HISTORY_RACED,
                retry_class=RETRY_CLASS_RETRYABLE,
            )

        next_capture = capture_offset + len(captures)
        next_extraction = extraction_offset + len(extractions)
        continuation = None
        if next_capture < capture_count or next_extraction < extraction_count:
            continuation = self.token_codec.encode(
                {
                    "b": binding_digest,
                    "c": next_capture,
                    "s": snapshot,
                    "v": 1,
                    "x": next_extraction,
                }
            )
        result = DocumentHistoryResult(
            document_id=document.document_id,
            created_at=_timestamp(document.created_at_us),
            source=DocumentSourceHeadState(
                source_state=head.source_state,
                source_removal_generation=head.source_removal_generation,
                current_capture_id=head.current_capture_id,
                capture_head_sequence=head.capture_head_sequence,
                original_binding_state=binding_state,
                updated_at=_timestamp(head.updated_at_us),
            ),
            extraction=DocumentExtractionHeadState(
                current_extraction_id=head.current_extraction_id,
                extraction_head_ordinal=head.extraction_head_ordinal,
                retrieval_state=(
                    "retrievable" if published_at_us is not None else "none"
                ),
                published_at=(
                    None if published_at_us is None else _timestamp(published_at_us)
                ),
            ),
            captures=captures,
            capture_count=capture_count,
            extractions=extractions,
            extraction_count=extraction_count,
            actions=(
                _action(
                    _ACTION_REFRESH,
                    _refresh_refusal(
                        source_active=source_active, binding_state=binding_state
                    ),
                ),
            ),
            page=PageMetadata(continuation_token=continuation),
        )
        # The result is held to the request and to itself before it is served. A build that
        # assembled an untruthful page -- a current flag that disagrees with the head, an
        # unavailable action without its reason, a pending extraction claiming an outcome --
        # refuses here rather than publishing that claim to a caller.
        invalid = False
        try:
            validate_document_history_result(result, request, document.document_id)
        except ContractSemanticError:
            invalid = True
        if invalid:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_HISTORY_UNREADABLE
            )
        return result.to_wire()

    def _decode_history(self, context: OperationContext) -> DocumentHistoryInput:
        """The payload as a validated history input, or a refusal that quotes nothing.

        Sentinel-then-raise, this directory's convention and load-bearing here: both
        contract errors quote the payload they rejected, and raising inside the `except`
        would leave that text reachable through `__context__` on the error a caller catches.
        """
        decoded: DocumentHistoryInput | None
        try:
            if not _closed_history(context.request.input):
                raise DocumentIntakeRefused("the history request is not closed")
            decoded = decode_document_history_input(context.request.input)
        except (ContractDecodeError, ContractSemanticError, DocumentIntakeRefused):
            decoded = None
        if decoded is None:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_HISTORY_INVALID)
        return decoded

    def _history_token(
        self, request: DocumentHistoryInput, binding_digest: str
    ) -> Mapping[str, Any] | None:
        """This request's continuation token, verified against the request alone.

        Split from the snapshot check on purpose. Everything checkable without touching
        storage is checked here: the signature, the exact key set, the version, the request
        binding and the two offsets. A token moved to another document, another principal,
        another workspace or another page size is therefore refused *before* any row is
        read, so a tampered token cannot be used to find out whether another document
        exists. The snapshot is the one part that needs the document's current state, and
        `_history_offsets` applies it once that state has been read.

        Every failure is the same `invalid_request`: which check a tampered token failed is
        not a caller's business.
        """
        if request.page is None:
            return None
        token = request.page.continuation_token
        assert token is not None
        supplied: Mapping[str, Any] | None = None
        try:
            supplied = self.token_codec.decode(token)
        except (ValueError, ContractDecodeError, ContractSemanticError):
            supplied = None
        if (
            supplied is None
            or set(supplied) != _HISTORY_TOKEN_KEYS
            or supplied.get("v") != 1
            or supplied.get("b") != binding_digest
            or not isinstance(supplied.get("s"), str)
        ):
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_HISTORY_INVALID)
        for key in ("c", "x"):
            value = supplied.get(key)
            if type(value) is not int or value < 0:
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_HISTORY_INVALID)
        if (supplied["c"], supplied["x"]) == (0, 0):
            # This server never issues a token for the first page, so one that asks to
            # continue from the beginning was not issued by it.
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_HISTORY_INVALID)
        return supplied

    def _capture_entry(
        self,
        row: store.CaptureHistoryRow,
        *,
        head: store.DocumentHead,
        source_active: bool,
        reprocess_options: Mapping[str, Sequence[ReprocessProfileOption]],
    ) -> DocumentCaptureHistoryEntry:
        """One capture as history, with the actions Core will actually accept for it.

        `reprocess_options` is the request's single resolution, keyed by media type, and no
        profile is resolved here. A capture that qualifies for options -- active source,
        retained bytes -- has its media type in that mapping by construction, because the
        mapping was built from the same capture facts; the `.get` default covers only the
        captures that qualify for none, which is the same empty tuple those captures were
        given before and so the same unavailable reason.
        """
        capture = row.capture
        retained = row.retention_state == store.RETENTION_RETAINED
        options: Sequence[ReprocessProfileOption] = (
            reprocess_options.get(capture.media_type, ())
            if source_active and retained
            else ()
        )
        refusal = _reprocess_refusal(
            source_active=source_active, retained=retained, options=options
        )
        return DocumentCaptureHistoryEntry(
            capture_id=capture.capture_id,
            capture_sequence=capture.capture_sequence,
            evidence_id=capture.evidence_id,
            source_digest=capture.source_digest,
            source_length_bytes=capture.source_length_bytes,
            media_type=capture.media_type,
            requested_source_identity=capture.requested_source_identity,
            intent_kind=capture.intent_kind,
            association_kind=capture.association_kind,
            capture_disposition=capture.disposition,
            disposition_reason=capture.disposition_reason,
            # From the head's own column. Not the greatest sequence, not the latest
            # `created_at_us`, and not this page's last row.
            is_current_source=capture.capture_id == head.current_capture_id,
            retention_state=row.retention_state,
            capture_retention_generation=row.capture_retention_generation,
            source_removal_generation=capture.source_removal_generation,
            requested_profile=DocumentProcessingProfileInput(
                profile_ref=capture.processing_profile_ref,
                revision=capture.processing_profile_revision,
            ),
            processing_state=(
                _PROCESSING_NOT_QUEUED if row.outbox_state is None else row.outbox_state
            ),
            expected_base_capture_id=capture.expected_base_capture_id,
            captured_at=_timestamp(capture.created_at_us),
            retention_changed_at=_timestamp(row.retention_changed_at_us),
            actions=(_action(_ACTION_REPROCESS, refusal),),
            reprocess_profiles=tuple(
                DocumentReprocessProfileOption(
                    profile_ref=option.profile_ref,
                    revision=option.revision,
                    processing_mode=option.processing_mode,
                    profile_digest=option.profile_digest,
                )
                for option in (options if refusal is None else ())
            ),
        )

    def _extraction_entry(
        self,
        connection: Any,
        row: store.ExtractionHistoryRow,
        *,
        head: store.DocumentHead,
        workspace_id: str,
    ) -> DocumentExtractionHistoryEntry:
        """One extraction revision as history, with the profile facts a reader can act on.

        `read_profile` recomputes the stored profile's own digest and canonicality, so a
        summary is built only from a profile that still verifies as the one this revision
        names. A row that does not verify raises, and the caller turns that into one refusal
        rather than a page that quietly describes a different profile.
        """
        revision = row.revision
        result = row.result
        profile = store.read_profile(
            connection, workspace_id=workspace_id, digest=revision.profile_digest
        )
        if profile is None:
            raise store.DocumentIntakeInvalid(
                "an extraction revision names a profile this workspace does not hold"
            )
        ocr = profile.get("ocr")
        languages = ocr.get("languages", ()) if isinstance(ocr, Mapping) else ()
        return DocumentExtractionHistoryEntry(
            extraction_revision_id=revision.extraction_revision_id,
            capture_id=revision.capture_id,
            document_ordinal=revision.document_ordinal,
            extraction_sequence=revision.extraction_sequence,
            profile=DocumentExtractionProfileSummary(
                digest=revision.profile_digest,
                decoder_build=str(profile["decoderBuild"]),
                processing_mode=str(profile["processingMode"]),
                embedded_image_ocr=bool(profile["embeddedImageOcr"]),
                ocr_languages=tuple(str(language) for language in languages),
            ),
            expected_base_extraction_id=revision.expected_base_extraction_id,
            head_eligible=bool(revision.head_eligible),
            # From the head's own column, which moves only through its own write and only
            # to a promoted revision. An older capture's new extraction never reads as
            # current merely because it finished last.
            is_current_extraction=(
                revision.extraction_revision_id == head.current_extraction_id
            ),
            result_state=None if result is None else result.result_state,
            publication_disposition=(
                None if result is None else result.publication_disposition
            ),
            output_digest=None if result is None else result.output_digest,
            worker_build=None if result is None else result.worker_build,
            admitted_at=_timestamp(revision.created_at_us),
            completed_at=(
                None if result is None else _timestamp(result.completed_at_us)
            ),
        )

    def document_capture(self, context: OperationContext) -> AuditedOperationResult:
        request: DocumentCaptureInput | None = None
        expected: str | None = None
        binding: BindingClaim | None = None
        invalid = False
        try:
            if not _closed(context.request.input):
                raise DocumentIntakeRefused("the capture request is not closed")
            request = DocumentCaptureInput.from_wire(context.request.input)
            expected, binding = _validate(request)
        except (ContractDecodeError, DocumentIntakeRefused, SourceCaptureRefused):
            invalid = True
        if invalid:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        assert request is not None
        if request.content_base64 is not None:
            content = _decode_content(request.content_base64)
        else:
            assert request.staged_source is not None
            staged = request.staged_source
            if staged.media_type != request.media_type or staged.source_version is not None:
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            staged_invalid = False
            try:
                content = read_staged_document(
                    self.service,
                    workspace_id=context.workspace_id,
                    staged_source_ref=staged.staged_source_ref,
                    source_kind=staged.source_kind,
                    content_checksum=staged.content_checksum,
                    content_length_bytes=staged.content_length_bytes,
                    media_type=staged.media_type,
                )
            except (DocumentTransferRefused, BlobPublicationRefused, OSError):
                staged_invalid = True
                content = b""
            if staged_invalid:
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        operation_key = context.request.metadata.idempotency_key
        if operation_key is None or context.authorization is None:
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        connection = getattr(self.service, "connection", None)
        identity = getattr(self.service, "identity", None)
        layout = getattr(self.service, "layout", None)
        blobs_root = getattr(layout, "blobs_path", None)
        guard = None if connection is None else read_guard(connection)
        if connection is None or identity is None or guard is None or not isinstance(blobs_root, Path):
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )
        grant = issue_mutation_grant(
            context.authorization,
            session=self.session,
            binding=self.binding,
            guard=guard,
            equivalence=equivalence,
            clock=self.clock,
        )
        capture_service: Any = _CaptureServiceView(
            connection=connection,
            identity=identity,
            layout=layout,
            workspace_id=context.workspace_id,
            generation=guard.fencing_generation,
        )

        def mutate(_fenced: Any, _settlement: MutationSettlementContext) -> Mapping[str, Any]:
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            admitted = None
            failure: str | None = None
            try:
                publish_blob(blobs_root, digest, content)
                admitted = admit_document_capture_in_transaction(
                    capture_service,
                    content=content,
                    document_id=request.document_id,
                    expected_base_capture_id=expected,
                    operation_key=operation_key,
                    media_type=request.media_type,
                    intent_kind=request.intent_kind,
                    binding=binding,
                    requested_source_identity=request.requested_source_identity,
                    target_binding_revision=request.target_binding_revision,
                    processing_profile_ref=request.processing_profile.profile_ref,
                    processing_profile_revision=request.processing_profile.revision,
                    consent=None if request.consent is None else request.consent.to_wire(),
                    bulk_origin=None if request.bulk_origin is None else request.bulk_origin.to_wire(),
                    display_name=request.display_name,
                    # The installation's managed OCR cache, so the admission can
                    # freeze and pin the exact resource identities this capture
                    # will execute against. The same authority the executor and
                    # the history read are given, composed once in `service/main`
                    # from the installation being served: an admission that was
                    # not given it would freeze every capture out of recognition.
                    ocr_resources=managed_resources(self.catalogue_for),
                )
            except DocumentCaptureSuppressed:
                failure = "conflict"
            except (DocumentIntakeRefused, SourceCaptureRefused, BlobPublicationRefused, OSError):
                failure = "unavailable"
            if failure == "conflict":
                raise OperationError(ERROR_CODE_CONFLICT, _MESSAGE_INVALID)
            if failure == "unavailable":
                # Same frozen class as the history race above: `internal_recoverable` is
                # retryable, and the default `non_retryable` contradicts it, which the
                # response validator turns into a bare `internal_non_recoverable` telling
                # the caller to stop over work that resending would complete.
                raise OperationError(
                    ERROR_CODE_INTERNAL_RECOVERABLE,
                    _MESSAGE_UNAVAILABLE,
                    retry_class=RETRY_CLASS_RETRYABLE,
                )
            assert admitted is not None
            capture = admitted.capture
            return DocumentCaptureResult(
                capture_id=capture.capture_id,
                document_id=capture.document_id,
                evidence_id=capture.evidence_id,
                source_digest=capture.source_digest,
                source_length_bytes=capture.source_length_bytes,
                capture_disposition=capture.disposition,
                became_head=admitted.became_head,
            ).to_wire()

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                result = DocumentCaptureResult.from_wire(wire)
            except ContractDecodeError:
                return False
            return result.document_id == request.document_id

        outcome = execute_mutation(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            mutate=mutate,
            validate_result=valid_result,
            clock=self.clock,
            allocate_identifier=self.allocate_identifier,
        )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)

    def document_reprocess(self, context: OperationContext) -> AuditedOperationResult:
        request: DocumentReprocessInput | None = None
        invalid = False
        try:
            if not _closed_reprocess(context.request.input):
                raise DocumentIntakeRefused("the reprocess request is not closed")
            request = DocumentReprocessInput.from_wire(context.request.input)
            _identifier(request.capture_id, "capture id")
            _identifier(request.processing_profile.profile_ref, "profile reference")
            _identifier(request.processing_profile.revision, "profile revision")
            if not is_evidence_checksum(
                request.processing_profile.expected_profile_digest
            ):
                raise DocumentIntakeRefused(
                    "expected profile digest is outside the accepted checksum domain"
                )
            expected = request.expected_base.extraction_id
            if expected is not None:
                _identifier(expected, "expected extraction")
        except (ContractDecodeError, DocumentIntakeRefused):
            invalid = True
        if invalid:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_REPROCESS_INVALID)
        assert request is not None
        operation_key = context.request.metadata.idempotency_key
        if operation_key is None or context.authorization is None:
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        connection = getattr(self.service, "connection", None)
        identity = getattr(self.service, "identity", None)
        layout = getattr(self.service, "layout", None)
        guard = None if connection is None else read_guard(connection)
        if connection is None or identity is None or guard is None or layout is None:
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )
        grant = issue_mutation_grant(
            context.authorization,
            session=self.session,
            binding=self.binding,
            guard=guard,
            equivalence=equivalence,
            clock=self.clock,
        )
        capture_service: Any = _CaptureServiceView(
            connection=connection,
            identity=identity,
            layout=layout,
            workspace_id=context.workspace_id,
            generation=guard.fencing_generation,
        )
        capture = store.read_capture(
            connection,
            workspace_id=context.workspace_id,
            capture_id=request.capture_id,
        )
        if capture is None:
            raise OperationError(ERROR_CODE_CONFLICT, _MESSAGE_REPROCESS_INVALID)
        # The selection is resolved against the one function `document.history` offered it
        # from, so the reference, the revision, the resolved settings and the digest are a
        # single decision rather than four that could disagree. A selection this build does
        # not approve for this capture's media type, and a selection whose profile identity
        # has moved since it was offered, are the same refusal: Core will not quietly run a
        # different profile beneath a request that named this one.
        # One lease, held from the resolution through the engine invocation to the
        # recorded outcome. A reprocess resolves exactly one selection and then runs
        # it, so the resources the recorded profile names must not be removable in
        # between -- and the release waits for the mutation, because the mutation is
        # where the extraction's outcome becomes the capture's history.
        with held_selection(self.catalogue_for) as catalogue_for:
            selection = request.processing_profile
            try:
                options = approved_reprocess_profiles(
                    media_type=capture.media_type,
                    # The *current* preferences, read once here: a reprocess is a new
                    # decision, so the workspace's live choice is the approved one and the
                    # original capture's frozen snapshot is not consulted at all.
                    settings=self._processing_settings(
                        connection, context.workspace_id, _MESSAGE_NO_STORAGE
                    ),
                    catalogue_for=catalogue_for,
                )
            except OcrSelectionInvalid as error:
                # The installation's recognition selection is not a selection at all, so there
                # is no approved profile to resolve. Refusing is the only honest answer: the
                # text-only profile would run, and that is not what was asked for.
                raise OperationError(
                    ERROR_CODE_CONFLICT, _MESSAGE_REPROCESS_SUBSTITUTED
                ) from error
            approved = next(
                (
                    option
                    for option in options
                    if option.profile_ref == selection.profile_ref
                    and option.revision == selection.revision
                ),
                None,
            )
            if (
                approved is None
                or approved.profile_digest != selection.expected_profile_digest
            ):
                raise OperationError(ERROR_CODE_CONFLICT, _MESSAGE_REPROCESS_SUBSTITUTED)
            profile = dict(approved.profile)
            expected = request.expected_base.extraction_id
            existing = store.read_revision_by_key(
                connection,
                workspace_id=context.workspace_id,
                operation_key=operation_key,
            )
            extraction_outcome = None
            if existing is None:
                try:
                    content = read_retained_source(capture_service, capture_id=capture.capture_id)
                    extraction_outcome = extract_document(
                        content,
                        media_type=capture.media_type,
                        capture_id=capture.capture_id,
                        # The same settings the recorded profile was built from. Without this
                        # the worker would resolve its own default, and a text-only selection
                        # would execute Text and local OCR under a text-only receipt.
                        settings=approved.settings,
                    )
                except DocumentExtractionUnsupported:
                    extraction_outcome = _terminal_outcome("unsupported", "format_not_qualified")
                except (DocumentExtractionInvalid, DocumentIntakeRefused):
                    extraction_outcome = _terminal_outcome("failed", "source_invalid")

            def mutate(_fenced: Any, _settlement: MutationSettlementContext) -> Mapping[str, Any]:
                try:
                    revision, result = complete_reprocess_in_transaction(
                        capture_service,
                        capture_id=capture.capture_id,
                        profile=profile,
                        expected_base_extraction_id=expected,
                        operation_key=operation_key,
                        outcome=extraction_outcome,
                    )
                except DocumentIntakeRefused as error:
                    raise OperationError(ERROR_CODE_CONFLICT, _MESSAGE_REPROCESS_INVALID) from error
                return DocumentReprocessResult(
                    extraction_revision_id=revision.extraction_revision_id,
                    document_id=revision.document_id,
                    capture_id=revision.capture_id,
                    profile_digest=revision.profile_digest,
                    result_state=result.result_state,
                    publication_disposition=result.publication_disposition,
                    became_head=result.publication_disposition == "promoted",
                    output_digest=result.output_digest,
                ).to_wire()

            def valid_result(wire: Mapping[str, Any]) -> bool:
                try:
                    result = DocumentReprocessResult.from_wire(wire)
                except ContractDecodeError:
                    return False
                return result.capture_id == request.capture_id

            outcome = execute_mutation(
                connection,
                identity,
                grant=grant,
                context=context.authorization,
                equivalence=equivalence,
                mutate=mutate,
                validate_result=valid_result,
                clock=self.clock,
                allocate_identifier=self.allocate_identifier,
            )
            return AuditedOperationResult(outcome.result, outcome.audit_ref)

    def document_original_disconnect(self, context: OperationContext) -> AuditedOperationResult:
        """Tombstone one owner route of a document's original, once per idempotency key.

        The route is tombstoned at the generation Core holds, so a stale capture or relink
        result carrying that generation or an older one stays historical and can never
        reactivate it. A disconnect that arrives before any capture still records the
        tombstone. Disconnecting a route that is already disconnected changes nothing and
        returns the same route, so a retry after a lost acknowledgement, under the same key
        or a new one, is safe.
        """
        request: DocumentOriginalDisconnectInput | None = None
        invalid = False
        try:
            if not _closed_disconnect(context.request.input):
                raise DocumentIntakeRefused("the disconnect request is not closed")
            request = DocumentOriginalDisconnectInput.from_wire(context.request.input)
            _identifier(request.document_id, "document id")
            _identifier(request.owner_ref, "owner reference")
            _check_route(request.route_kind, request.root_ref, request.member_ref)
        except (ContractDecodeError, DocumentIntakeRefused):
            invalid = True
        if invalid:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_DISCONNECT_INVALID)
        assert request is not None
        connection = getattr(self.service, "connection", None)
        identity = getattr(self.service, "identity", None)
        layout = getattr(self.service, "layout", None)
        guard = None if connection is None else read_guard(connection)
        if (
            context.authorization is None
            or connection is None
            or identity is None
            or guard is None
            or layout is None
        ):
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )
        grant = issue_mutation_grant(
            context.authorization,
            session=self.session,
            binding=self.binding,
            guard=guard,
            equivalence=equivalence,
            clock=self.clock,
        )
        capture_service: Any = _CaptureServiceView(
            connection=connection,
            identity=identity,
            layout=layout,
            workspace_id=context.workspace_id,
            generation=guard.fencing_generation,
        )

        def mutate(_fenced: Any, _settlement: MutationSettlementContext) -> Mapping[str, Any]:
            try:
                binding = move_original_in_transaction(
                    capture_service,
                    document_id=request.document_id,
                    owner_ref=request.owner_ref,
                    route_kind=request.route_kind,
                    root_ref=request.root_ref,
                    member_ref=request.member_ref,
                    connect=False,
                )
            except DocumentIntakeRefused as error:
                raise OperationError(ERROR_CODE_CONFLICT, _MESSAGE_DISCONNECT_REFUSED) from error
            except sqlite3.OperationalError as error:
                # Same frozen class as the capture path: a busy store is retryable, and the
                # default `non_retryable` would be rewritten to a bare internal failure.
                raise OperationError(
                    ERROR_CODE_INTERNAL_RECOVERABLE,
                    _MESSAGE_DISCONNECT_UNAVAILABLE,
                    retry_class=RETRY_CLASS_RETRYABLE,
                ) from error
            return DocumentOriginalDisconnectResult(
                document_id=binding.document_id,
                owner_ref=binding.owner_ref,
                binding_generation=binding.binding_generation,
                connection_state=binding.connection_state,
            ).to_wire()

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                result = DocumentOriginalDisconnectResult.from_wire(wire)
            except ContractDecodeError:
                return False
            return (
                result.document_id == request.document_id
                and result.owner_ref == request.owner_ref
                and result.connection_state == store.BINDING_DISCONNECTED
            )

        outcome = execute_mutation(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            mutate=mutate,
            validate_result=valid_result,
            clock=self.clock,
            allocate_identifier=self.allocate_identifier,
        )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)

    def document_original_relink(self, context: OperationContext) -> AuditedOperationResult:
        """Move one direct-file owner route of a document's original to a newer generation, connected.

        The companion reserved `binding_generation` locally before it read any byte of the newly
        chosen file, so a capture claiming an older generation of this route is thereafter
        historical only and can never reactivate or promote under the new one. A relink that
        arrives before any capture still records the route. Replay under the same key returns
        the stored receipt and writes nothing more.
        """
        request: DocumentOriginalRelinkInput | None = None
        invalid = False
        try:
            if not _closed_relink(context.request.input):
                raise DocumentIntakeRefused("the relink request is not closed")
            request = DocumentOriginalRelinkInput.from_wire(context.request.input)
            _identifier(request.document_id, "document id")
            _identifier(request.owner_ref, "owner reference")
            # The decoders apply no range, and a relink always names a generation after the first.
            if not 2 <= request.binding_generation <= _MAX_BINDING_GENERATION:
                raise DocumentIntakeRefused("the relink generation is out of range")
        except (ContractDecodeError, DocumentIntakeRefused):
            invalid = True
        if invalid:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_RELINK_INVALID)
        assert request is not None
        connection = getattr(self.service, "connection", None)
        identity = getattr(self.service, "identity", None)
        layout = getattr(self.service, "layout", None)
        guard = None if connection is None else read_guard(connection)
        if (
            context.authorization is None
            or connection is None
            or identity is None
            or guard is None
            or layout is None
        ):
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE)
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            request.to_wire(),
            principal_id=context.principal,
            workspace_id=context.workspace_id,
        )
        grant = issue_mutation_grant(
            context.authorization,
            session=self.session,
            binding=self.binding,
            guard=guard,
            equivalence=equivalence,
            clock=self.clock,
        )
        capture_service: Any = _CaptureServiceView(
            connection=connection,
            identity=identity,
            layout=layout,
            workspace_id=context.workspace_id,
            generation=guard.fencing_generation,
        )

        def mutate(_fenced: Any, _settlement: MutationSettlementContext) -> Mapping[str, Any]:
            try:
                binding = relink_original_in_transaction(
                    capture_service,
                    document_id=request.document_id,
                    owner_ref=request.owner_ref,
                    generation=request.binding_generation,
                )
            except DocumentIntakeRefused as error:
                raise OperationError(ERROR_CODE_CONFLICT, _MESSAGE_RELINK_REFUSED) from error
            except sqlite3.OperationalError as error:
                # Same frozen class as the capture path: a busy store is retryable, and the
                # default `non_retryable` would be rewritten to a bare internal failure.
                raise OperationError(
                    ERROR_CODE_INTERNAL_RECOVERABLE,
                    _MESSAGE_RELINK_UNAVAILABLE,
                    retry_class=RETRY_CLASS_RETRYABLE,
                ) from error
            # The head as of this commit, stored with the receipt: a replay returns this value, not
            # whatever the head has become, so the companion re-bases on what the relink saw.
            head = store.read_head(
                capture_service.connection,
                workspace_id=capture_service.workspace_id,
                document_id=binding.document_id,
            )
            return DocumentOriginalRelinkResult(
                document_id=binding.document_id,
                owner_ref=binding.owner_ref,
                binding_generation=binding.binding_generation,
                connection_state=binding.connection_state,
                current_capture_id=None if head is None else head.current_capture_id,
            ).to_wire()

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                result = DocumentOriginalRelinkResult.from_wire(wire)
            except ContractDecodeError:
                return False
            return (
                result.document_id == request.document_id
                and result.owner_ref == request.owner_ref
                and result.binding_generation == request.binding_generation
                and result.connection_state == store.BINDING_CONNECTED
            )

        outcome = execute_mutation(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            mutate=mutate,
            validate_result=valid_result,
            clock=self.clock,
            allocate_identifier=self.allocate_identifier,
        )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)


def _terminal_outcome(state: str, limitation: str) -> ExtractionOutcome:
    return ExtractionOutcome(
        state=state,
        coverage={
            "schema": "omnivia.extraction-coverage.v2",
            "units": [
                {
                    "unitType": "document",
                    "expected": 1,
                    "visited": 1,
                    "usefulText": 0,
                    "knownEmpty": 0,
                    "excludedByProfile": 0,
                    "unsupported": int(state == "unsupported"),
                    "failed": int(state == "failed"),
                    "unknownRemaining": 0,
                }
            ],
            "roleCoverage": {},
        },
        limitations={"code": limitation},
        worker_build="core-portable-text-1",
    )


@dataclass(frozen=True)
class _CaptureServiceView:
    """The exact authoritative service facts the document domain writer consumes."""

    connection: Any
    identity: Any
    layout: Any
    workspace_id: str
    generation: int
