"""Production handlers for ``memory.create``, ``memory.get`` and ``memory.list``."""

from __future__ import annotations

import base64
import binascii
import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final, Protocol

from omnivia_core.contracts.v1 import (
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ContractDecodeError,
    ContractSemanticError,
    MemoryCreateResult,
    MemoryGetInput,
    MemoryGetResult,
    MemoryListInput,
    MemoryListResult,
    PageMetadata,
    decode_memory_create_input,
    idempotency_equivalence,
    resolve_governed_record_view,
    validate_memory_create_result,
)
from omnivia_core.contracts.v1.canonical_json import canonicalize, parse_json_document
from omnivia_core.contracts.v1.semantics_knowledge import (
    _validate_memory_get_request,
    _validate_memory_list_request,
    validate_memory_get_result,
    validate_memory_list_result,
)
from omnivia_core_runtime.ownership.fencing import read_guard
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
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
from omnivia_core_runtime.storage.memory import (
    IdentifierAllocator,
    create_memory_record,
    read_authorized_memory_snapshot,
)
from omnivia_core_runtime.storage.retrieval import EvidenceLabelGrant

MEMORY_CREATE_OPERATION: Final = "memory.create"
MEMORY_GET_OPERATION: Final = "memory.get"
MEMORY_LIST_OPERATION: Final = "memory.list"
MEMORY_FAMILY_OPERATIONS: Final = frozenset(
    {MEMORY_CREATE_OPERATION, MEMORY_GET_OPERATION, MEMORY_LIST_OPERATION}
)

_MESSAGE_INVALID: Final = "the request payload is not valid for this memory operation"
_MESSAGE_NOT_FOUND: Final = "the requested memory record was not found"
_MESSAGE_NO_STORAGE: Final = (
    "this service instance is not serving authoritative storage"
)
_MESSAGE_VIEW_DENIED: Final = "the requested governed view is not granted"
_TOKEN_KEYS: Final = frozenset({"b", "k", "o", "s", "t", "v"})


class ContinuationTokenCodec(Protocol):
    def encode(self, payload: Mapping[str, Any]) -> str: ...
    def decode(self, token: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class HmacContinuationTokenCodec:
    secret: bytes

    @classmethod
    def secure(cls) -> HmacContinuationTokenCodec:
        return cls(secrets.token_bytes(32))

    def encode(self, payload: Mapping[str, Any]) -> str:
        document = canonicalize(dict(payload)).encode("utf-8")
        signature = hmac.new(self.secret, document, sha256).digest()
        encoded_document = base64.urlsafe_b64encode(document).rstrip(b"=").decode()
        encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        return f"{encoded_document}.{encoded_signature}"

    def decode(self, token: str) -> Mapping[str, Any]:
        try:
            encoded_document, encoded_signature = token.split(".")
            document = base64.b64decode(
                encoded_document + "=" * (-len(encoded_document) % 4),
                altchars=b"-_",
                validate=True,
            )
            signature = base64.b64decode(
                encoded_signature + "=" * (-len(encoded_signature) % 4),
                altchars=b"-_",
                validate=True,
            )
        except (ValueError, binascii.Error, UnicodeEncodeError) as error:
            raise ValueError("invalid continuation encoding") from error
        expected = hmac.new(self.secret, document, sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid continuation signature")
        value = parse_json_document(document)
        if (
            not isinstance(value, dict)
            or canonicalize(value).encode("utf-8") != document
        ):
            raise ValueError("invalid continuation document")
        return value


def _token_digest(value: Any) -> str:
    document = canonicalize(value).encode("utf-8")
    return base64.urlsafe_b64encode(sha256(document).digest()).rstrip(b"=").decode()


@dataclass(frozen=True)
class MemoryHandlers:
    service: Any
    session: AuthenticatedSession
    binding: ServiceBinding
    label_grant: EvidenceLabelGrant
    authorized_views: frozenset[str]
    clock: Clock
    allocate_identifier: IdentifierAllocator
    token_codec: ContinuationTokenCodec

    def memory_create(self, context: OperationContext) -> AuditedOperationResult:
        claim: Any = None
        invalid_request = False
        try:
            claim = decode_memory_create_input(context.request.input)
        except (ContractDecodeError, ContractSemanticError):
            invalid_request = True
        if invalid_request:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        assert claim is not None
        if context.authorization is None:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        connection = getattr(self.service, "connection", None)
        identity = getattr(self.service, "identity", None)
        guard = None if connection is None else read_guard(connection)
        if connection is None or identity is None or guard is None:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            claim.to_wire(),
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
            return create_memory_record(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                claim=claim,
                label_grant=self.label_grant,
                allocate_identifier=self.allocate_identifier,
            )

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                result = MemoryCreateResult.from_wire(wire)
                validate_memory_create_result(result, context.workspace_id)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

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

    def memory_get(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        request: MemoryGetInput | None = None
        view: str | None = None
        invalid_request = False
        try:
            request = _validate_memory_get_request(
                MemoryGetInput.from_wire(context.request.input)
            )
            view = resolve_governed_record_view(request.view)
        except (ContractDecodeError, ContractSemanticError, ValueError):
            invalid_request = True
        if invalid_request:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        assert request is not None
        assert view is not None
        if view in {"candidates", "history"} and view not in self.authorized_views:
            raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
        snapshot = self._snapshot(context, request.view, self._now_us())
        matches = tuple(
            value.record
            for value in snapshot.values
            if value.record.provenance.identity.record_id == request.record_id
            and (
                request.version is None
                or value.record.provenance.identity.version == request.version
            )
        )
        if len(matches) != 1:
            raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
        result = MemoryGetResult(record=matches[0])
        validate_memory_get_result(
            result,
            request,
            context.workspace_id,
            _timestamp(snapshot.resolution_instant_us),
            self.authorized_views,
        )
        wire = result.to_wire()
        if view == "candidates":
            return wire
        return AuditedOperationResult(
            wire,
            canonical_resolution_time=_timestamp(snapshot.resolution_instant_us),
        )

    def memory_list(
        self, context: OperationContext
    ) -> Mapping[str, Any] | AuditedOperationResult:
        request: MemoryListInput | None = None
        view: str | None = None
        invalid_request = False
        try:
            request = _validate_memory_list_request(
                MemoryListInput.from_wire(context.request.input)
            )
            view = resolve_governed_record_view(request.view)
        except (ContractDecodeError, ContractSemanticError, ValueError):
            invalid_request = True
        if invalid_request:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        assert request is not None
        assert view is not None
        if view in {"candidates", "history"} and view not in self.authorized_views:
            raise OperationError(ERROR_CODE_AUTHORIZATION_DENIED, _MESSAGE_VIEW_DENIED)
        limit = 50 if request.limit is None else min(request.limit, 500)
        binding = {
            "v": 1,
            "principal": context.principal,
            "workspace": context.workspace_id,
            "operation": MEMORY_LIST_OPERATION,
            "view": view,
            "record_type": request.record_type,
            "limit": limit,
        }
        binding_digest = _token_digest(binding)
        supplied: Mapping[str, Any] | None = None
        if request.page is not None:
            token = request.page.continuation_token
            assert token is not None
            invalid_token = False
            try:
                supplied = self.token_codec.decode(token)
            except (ValueError, binascii.Error, ContractSemanticError):
                invalid_token = True
            if invalid_token:
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            assert supplied is not None
            if (
                set(supplied) != _TOKEN_KEYS
                or supplied.get("v") != 1
                or supplied.get("b") != binding_digest
            ):
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            instant = supplied.get("t")
            if type(instant) is not int or instant <= 0:
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        else:
            instant = self._now_us()
        snapshot = self._snapshot(context, request.view, instant)
        if supplied is not None and supplied.get("s") != snapshot.digest:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        ordered = sorted(
            (
                value
                for value in snapshot.values
                if request.record_type is None
                or value.record.record_type == request.record_type
            ),
            key=lambda value: (
                -value.recorded_at_us,
                value.record.provenance.identity.record_id,
                value.record.provenance.identity.version,
            ),
        )
        start = 0
        if supplied is not None:
            offset = supplied.get("o")
            if (
                not isinstance(offset, int)
                or isinstance(offset, bool)
                or not 0 < offset < len(ordered)
            ):
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
            start = offset
            previous = ordered[start - 1]
            cursor = [
                previous.recorded_at_us,
                previous.record.provenance.identity.record_id,
                previous.record.provenance.identity.version,
            ]
            if supplied.get("k") != _token_digest(cursor):
                raise OperationError(
                    ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID
                ) from None
        page_values = ordered[start : start + limit]
        continuation = None
        if start + limit < len(ordered):
            last = page_values[-1]
            cursor = [
                last.recorded_at_us,
                last.record.provenance.identity.record_id,
                last.record.provenance.identity.version,
            ]
            continuation = self.token_codec.encode(
                {
                    "b": binding_digest,
                    "k": _token_digest(cursor),
                    "o": start + len(page_values),
                    "s": snapshot.digest,
                    "t": snapshot.resolution_instant_us,
                    "v": 1,
                }
            )
        result = MemoryListResult(
            records=tuple(value.record for value in page_values),
            page=PageMetadata(continuation_token=continuation),
        )
        validate_memory_list_result(
            result,
            request,
            context.workspace_id,
            _timestamp(snapshot.resolution_instant_us),
            self.authorized_views,
        )
        wire = result.to_wire()
        if view == "candidates":
            return wire
        return AuditedOperationResult(
            wire,
            canonical_resolution_time=_timestamp(snapshot.resolution_instant_us),
        )

    def _snapshot(
        self, context: OperationContext, view: str | None, instant: int
    ) -> Any:
        connection = getattr(self.service, "connection", None)
        if connection is None:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        return read_authorized_memory_snapshot(
            connection,
            workspace_id=context.workspace_id,
            resolution_instant_us=instant,
            view=view,
            label_grant=self.label_grant,
        )

    def _now_us(self) -> int:
        return int(self.clock.wall_time().timestamp() * 1_000_000)


def _timestamp(value: int) -> str:
    from datetime import UTC, datetime

    moment = datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


__all__ = [
    "MEMORY_CREATE_OPERATION",
    "MEMORY_FAMILY_OPERATIONS",
    "MEMORY_GET_OPERATION",
    "MEMORY_LIST_OPERATION",
    "ContinuationTokenCodec",
    "HmacContinuationTokenCodec",
    "MemoryHandlers",
]
