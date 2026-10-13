"""Governed application handler for ``document.capture``."""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INTERNAL_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    ContractDecodeError,
    idempotency_equivalence,
)
from omnivia_core.contracts.v1.generated import (
    DocumentCaptureInput,
    DocumentCaptureResult,
)
from omnivia_core_runtime.ownership.fencing import read_guard
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.document_intake import (
    BindingClaim,
    DocumentCaptureSuppressed,
    DocumentIntakeRefused,
    _check_route,
    _identifier,
    admit_document_capture_in_transaction,
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
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
)
from omnivia_core_runtime.service.source_capture import (
    SourceCaptureRefused,
    _validate_request,
    publish_blob,
)
from omnivia_core_runtime.storage import document_intake as store
from omnivia_core_runtime.storage.memory import IdentifierAllocator
from omnivia_core_runtime.workspace.blob_publication import BlobPublicationRefused

DOCUMENT_CAPTURE_OPERATION: Final = "document.capture"
_MAX_INLINE_BYTES: Final = 3 * 1024 * 1024 - 64 * 1024
_MAX_ENCODED_BYTES: Final = 4 * ((_MAX_INLINE_BYTES + 2) // 3)
_MESSAGE_INVALID: Final = "the request payload is not a valid document capture"
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


def _closed(payload: object) -> bool:
    if not isinstance(payload, Mapping) or not set(payload) <= _TOP_LEVEL_KEYS:
        return False
    for name, allowed in _NESTED_KEYS.items():
        value = payload.get(name)
        if value is not None and (not isinstance(value, Mapping) or not set(value) <= allowed):
            return False
    return True


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
                )
            except DocumentCaptureSuppressed:
                failure = "conflict"
            except (DocumentIntakeRefused, SourceCaptureRefused, BlobPublicationRefused, OSError):
                failure = "unavailable"
            if failure == "conflict":
                raise OperationError(ERROR_CODE_CONFLICT, _MESSAGE_INVALID)
            if failure == "unavailable":
                raise OperationError(ERROR_CODE_INTERNAL_RECOVERABLE, _MESSAGE_UNAVAILABLE)
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


@dataclass(frozen=True)
class _CaptureServiceView:
    """The exact authoritative service facts the document domain writer consumes."""

    connection: Any
    identity: Any
    layout: Any
    workspace_id: str
    generation: int
