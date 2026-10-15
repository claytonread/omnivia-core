"""Governed application handlers for this workspace's document-processing preferences.

Two operations over the one stored settings document
(:mod:`omnivia_core_runtime.service.processing_settings`): a passive read and one
compare-and-swap change. Three properties belong here rather than in a reader's head.

**The preferences decide future captures and nothing else.** Neither handler touches a
document, a capture, a queued obligation or an extraction, and the update never reaches
work already admitted: a capture carries the snapshot it was admitted with. That is why
the update needs no document authority and refuses nothing about documents -- there is no
document in its request to refuse about.

**The revision is the whole concurrency story.** The caller states the revision it last
read, the store compares it inside the mutation's own fenced transaction, and a change
made since that read is a `conflict` the caller re-reads and re-decides against. There is
no mutation precondition: the catalogue declares none, so `execute_mutation` is handed no
precondition reader, and the compare-and-swap lives where the write does. One transaction,
not two -- `BEGIN IMMEDIATE` does not nest, which is why the store exposes
`apply_setting` for a caller that already owns the transaction.

**A language may be selected only if its bytes are installed.** Nothing here downloads,
verifies or installs anything, and nothing substitutes a different language for a missing
one. A selection naming a language the managed cache does not hold is refused at the
settings surface, so a preference that could never take effect cannot be saved. The
product default is the one exception: it is always selectable, because an installation
with no models at all must still have a coherent default, and an extraction that needs a
language it has not got reports **Needs OCR language** rather than recognising in another.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ContractDecodeError,
    ContractSemanticError,
    idempotency_equivalence,
)
from omnivia_core.contracts.v1.generated import (
    DocumentProcessingSettingsGetInput,
    DocumentProcessingSettingsGetResult,
    DocumentProcessingSettingsUpdateInput,
    DocumentProcessingSettingsUpdateResult,
)
from omnivia_core_runtime.ownership.fencing import read_guard
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.service import processing_settings as settings_store
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.mutation import (
    MutationIdempotencyConflict,
    MutationSettlementContext,
    execute_mutation,
    issue_mutation_grant,
)
from omnivia_core_runtime.service.ocr_resource_lifecycle import (
    OCR_RESOURCES_DIR,
    ManagedOcrResources,
)
from omnivia_core_runtime.service.ocr_resources import (
    OcrSelectionInvalid,
    validated_languages,
)
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
)
from omnivia_core_runtime.storage.app_settings import AppSettingsError

DOCUMENT_SETTINGS_GET_OPERATION: Final = "document.settings.get"
DOCUMENT_SETTINGS_UPDATE_OPERATION: Final = "document.settings.update"

_MESSAGE_NO_STORAGE: Final = "this service instance is not serving authoritative storage"
_MESSAGE_GET_INVALID: Final = (
    "the request payload is not a valid document processing settings read"
)
_MESSAGE_UPDATE_INVALID: Final = (
    "the request payload is not a valid document processing settings change"
)
#: The stored document named something this build does not understand, or is not a
#: settings document at all. Not the caller's fault and not recoverable by retrying.
_MESSAGE_UNREADABLE: Final = (
    "this workspace's stored document-processing preferences are not readable"
)
_MESSAGE_CONFLICT: Final = (
    "these settings changed since the revision this request names; read them again"
)
#: Named rather than listed in the message: the refusal states the rule, not the
#: installation's inventory, because an inventory in an error message is a disclosure
#: of what the host has that the caller did not ask for.
_MESSAGE_NOT_INSTALLED: Final = (
    "a selected recognition language is not installed in this installation's managed "
    "OCR resources"
)

#: This update's declared members. The generated decoder tolerates an unknown field so a
#: newer peer's additive release still decodes; this operation does not, because an
#: unrecognised member is a preference this build would silently fail to apply while
#: reporting the change as accepted.
_UPDATE_KEYS: Final = frozenset(
    {"revision", "mode", "recognition_languages", "embedded_image_ocr"}
)


@dataclass(frozen=True)
class DocumentSettingsHandlers:
    service: Any
    session: AuthenticatedSession
    binding: ServiceBinding
    clock: Clock
    allocate_identifier: Any

    # --- the stored document ------------------------------------------------------

    def _connection(self) -> Any:
        connection = getattr(self.service, "connection", None)
        if connection is None or read_guard(connection) is None:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        return connection

    def _read(
        self, connection: Any, workspace_id: str
    ) -> tuple[settings_store.ProcessingSettings, int]:
        try:
            return settings_store.read_settings(connection, workspace_id=workspace_id)
        except (settings_store.ProcessingSettingsInvalid, AppSettingsError) as error:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_UNREADABLE
            ) from error

    def _installed(self) -> Mapping[str, Any]:
        """What the managed OCR cache can serve, or nothing when it cannot be read.

        Absence is fail-closed on purpose: an installation whose cache is unreadable or
        held by an administrator cannot prove a language is installed, and accepting a
        selection on the strength of that uncertainty would save a preference nothing
        could honour.
        """
        installation = getattr(self.service, "installation", None)
        root = getattr(installation, "root", None)
        if root is None:
            return {}
        return settings_store.installed_identities(
            ManagedOcrResources(root / OCR_RESOURCES_DIR)
        )

    # --- document.settings.get ----------------------------------------------------

    def document_settings_get(self, context: OperationContext) -> Mapping[str, Any]:
        """Report this workspace's durable document-processing preferences.

        Read-only in the literal sense: no fenced transaction, no mutation grant, no
        identifier allocation and no transaction of any kind. The workspace is the
        authorized one -- the input carries no workspace field by contract and none is
        read -- and an unconfigured workspace is revision zero and the product defaults
        rather than a `not_found`, because "no stored preference" is an answer.
        """
        payload = context.request.input
        if not isinstance(payload, Mapping) or set(payload):
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_GET_INVALID)
        try:
            DocumentProcessingSettingsGetInput.from_wire(payload)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(
                ERROR_CODE_INVALID_REQUEST, _MESSAGE_GET_INVALID
            ) from error
        connection = self._connection()
        settings, revision = self._read(connection, context.workspace_id)
        wire = {"settings": settings.to_wire(revision)}
        DocumentProcessingSettingsGetResult.from_wire(wire)
        return wire

    # --- document.settings.update -------------------------------------------------

    def _decode_update(
        self, context: OperationContext
    ) -> DocumentProcessingSettingsUpdateInput:
        payload = context.request.input
        if not isinstance(payload, Mapping) or not set(payload) <= _UPDATE_KEYS:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_UPDATE_INVALID)
        try:
            request = DocumentProcessingSettingsUpdateInput.from_wire(payload)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(
                ERROR_CODE_INVALID_REQUEST, _MESSAGE_UPDATE_INVALID
            ) from error
        if request.revision < 0:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_UPDATE_INVALID)
        return request

    def _checked(self, request: DocumentProcessingSettingsUpdateInput) -> None:
        """Refuse a change this installation could not honour, before anything runs.

        Everything checkable without reading the stored document is checked here, so a
        refusable request never opens a transaction: the mode is one of the closed pair,
        the selection is an ordered, duplicate-free, at-most-three list of accepted
        language codes, and -- unless it is the product default, which is always
        selectable -- every language it names is installed in the managed cache under
        the engine revision this build invokes.
        """
        if request.mode is not None and request.mode not in settings_store.PROCESSING_MODES:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_UPDATE_INVALID)
        languages = request.recognition_languages
        if languages is None:
            return
        # Explicitly, before `validated_languages`, which reads an empty request as
        # "nothing is configured" and answers English. Clearing a selection and
        # selecting the default are different intents, and only the second is
        # expressible here, so the first is refused rather than quietly read as it.
        if not languages:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_UPDATE_INVALID)
        try:
            validated_languages(languages)
        except OcrSelectionInvalid as error:
            raise OperationError(
                ERROR_CODE_INVALID_REQUEST, _MESSAGE_UPDATE_INVALID
            ) from error
        if tuple(languages) == settings_store.DEFAULT_SETTINGS.languages:
            return
        installed = self._installed()
        if any(language not in installed for language in languages):
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_NOT_INSTALLED)

    def document_settings_update(
        self, context: OperationContext
    ) -> AuditedOperationResult:
        """Apply one compare-and-swap change to this workspace's preferences.

        Partial by contract: a member the request does not name is held, not defaulted,
        so a caller that does not know about a preference cannot reset it. The members it
        does name are validated here -- shape, mode, the ordered unique bounded language
        selection, and whether the named languages are actually installed -- before a
        grant is issued, so a refusable request never reaches a transaction.
        """
        request = self._decode_update(context)
        operation_key = context.request.metadata.idempotency_key
        if operation_key is None or context.authorization is None:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        connection = self._connection()
        identity = getattr(self.service, "identity", None)
        guard = read_guard(connection)
        if identity is None or guard is None:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        self._checked(request)

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

        def mutate(
            fenced: Any, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            # Read and write inside the executor's one fenced transaction. A read taken
            # before it could be stale by the time the write lands, and the expected
            # revision would then be compared against an instant nobody observed. The
            # revision this read sees is deliberately discarded: `apply_settings` is the
            # one place it is compared, so a second comparison here could disagree with
            # the one that actually decides.
            current = self._read(fenced, context.workspace_id)[0]
            try:
                updated = current.replacing(
                    mode=request.mode,
                    languages=request.recognition_languages,
                    embedded_image_ocr=request.embedded_image_ocr,
                )
            except settings_store.ProcessingSettingsInvalid as error:
                raise OperationError(
                    ERROR_CODE_INVALID_REQUEST, _MESSAGE_UPDATE_INVALID
                ) from error
            try:
                stored = settings_store.apply_settings(
                    fenced,
                    workspace_id=context.workspace_id,
                    settings=updated,
                    expected_revision=request.revision,
                    now_us=settlement.settled_at_us,
                )
            except AppSettingsError as error:
                raise OperationError(
                    ERROR_CODE_CONFLICT, _MESSAGE_CONFLICT, retry_class="non_retryable"
                ) from error
            return {"settings": updated.to_wire(stored)}

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                DocumentProcessingSettingsUpdateResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        try:
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
        except MutationIdempotencyConflict as conflict:
            raise OperationError(
                conflict.code, conflict.message, retry_class=conflict.retry_class
            ) from conflict
        return AuditedOperationResult(outcome.result, outcome.audit_ref)


__all__ = [
    "DOCUMENT_SETTINGS_GET_OPERATION",
    "DOCUMENT_SETTINGS_UPDATE_OPERATION",
    "DocumentSettingsHandlers",
]
