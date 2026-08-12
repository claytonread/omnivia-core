"""Production handlers for ``import.start`` and the four synchronous job operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ContractDecodeError,
    ContractSemanticError,
    ImportStartResult,
    JobCancelResult,
    JobEventsPageBinding,
    JobEventsResult,
    JobGetResult,
    JobReference,
    JobRetryResult,
    decode_import_start_input,
    decode_job_cancel_input,
    decode_job_events_input,
    decode_job_get_input,
    decode_job_retry_input,
    idempotency_equivalence,
    validate_import_start_result,
    validate_job_cancel_result,
    validate_job_cancel_result_shape,
    validate_job_events_page_binding,
    validate_job_events_result,
    validate_job_get_result,
    validate_job_retry_result,
    validate_job_retry_result_shape,
)
from omnivia_core_runtime.ownership.fencing import read_guard
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.handlers.memory import ContinuationTokenCodec
from omnivia_core_runtime.service.mutation import (
    MutationIdempotencyConflict,
    MutationSettlementContext,
    execute_mutation,
    issue_mutation_grant,
)
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
)
from omnivia_core_runtime.storage.jobs import (
    application_job_event_count,
    read_accepted_import_source,
    read_application_job_events,
    read_application_job_snapshot,
    read_job_id_by_origin_audit,
    request_job_cancellation,
    request_job_retry,
    start_import_job,
)
from omnivia_core_runtime.storage.memory import IdentifierAllocator

IMPORT_START_OPERATION: Final = "import.start"
JOB_GET_OPERATION: Final = "job.get"
JOB_CANCEL_OPERATION: Final = "job.cancel"
JOB_RETRY_OPERATION: Final = "job.retry"
JOB_EVENTS_OPERATION: Final = "job.events"
JOB_FAMILY_OPERATIONS: Final = frozenset(
    {
        IMPORT_START_OPERATION,
        JOB_GET_OPERATION,
        JOB_CANCEL_OPERATION,
        JOB_RETRY_OPERATION,
        JOB_EVENTS_OPERATION,
    }
)

_MESSAGE_INVALID: Final = "the request payload is not valid for this job operation"
_MESSAGE_NOT_FOUND: Final = "the requested job was not found"
_MESSAGE_NO_STORAGE: Final = (
    "this service instance is not serving authoritative job storage"
)
_TOKEN_KEYS: Final = frozenset({"f", "j", "n", "o", "p", "s", "v", "w"})


@dataclass(frozen=True)
class JobHandlers:
    service: Any
    session: AuthenticatedSession
    binding: ServiceBinding
    clock: Clock
    allocate_identifier: IdentifierAllocator
    token_codec: ContinuationTokenCodec

    def _authority(self) -> tuple[Any, Any, Any]:
        connection = getattr(self.service, "connection", None)
        identity = getattr(self.service, "identity", None)
        guard = None if connection is None else read_guard(connection)
        if connection is None or identity is None or guard is None:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        return connection, identity, guard

    def import_start(self, context: OperationContext) -> AuditedOperationResult:
        request = None
        try:
            request = decode_import_start_input(context.request.input)
        except (ContractDecodeError, ContractSemanticError):
            pass
        if request is None:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        connection, identity, guard = self._authority()
        if context.authorization is None:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
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
            return start_import_job(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                claim=request,
                fencing_generation=guard.fencing_generation,
                claimed_by_service_instance=identity.service_instance_id,
                allocate_identifier=self.allocate_identifier,
            )

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                result = ImportStartResult.from_wire(wire)
                validate_import_start_result(
                    result,
                    response_job_reference=JobReference(
                        job_id=result.job.identity.job_id
                    ),
                )
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        conflict: MutationIdempotencyConflict | None = None
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
        except MutationIdempotencyConflict as error:
            conflict = error
        if conflict is not None:
            job_id = (
                None
                if conflict.audit_reference is None
                else read_job_id_by_origin_audit(
                    connection,
                    workspace_id=context.workspace_id,
                    audit_ref=conflict.audit_reference,
                )
            )
            raise OperationError(
                conflict.code,
                conflict.message,
                retry_class=conflict.retry_class,
                audit_reference=conflict.audit_reference,
                job_reference=None if job_id is None else JobReference(job_id=job_id),
            )
        result = ImportStartResult.from_wire(outcome.result)
        reference = JobReference(job_id=result.job.identity.job_id)
        return AuditedOperationResult(
            outcome.result,
            audit_reference=outcome.audit_ref,
            job_reference=reference,
        )

    def job_get(self, context: OperationContext) -> Mapping[str, Any]:
        request = None
        try:
            request = decode_job_get_input(context.request.input)
        except (ContractDecodeError, ContractSemanticError):
            pass
        if request is None:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        connection, _identity, _guard = self._authority()
        connection.execute("BEGIN")
        try:
            wire = read_application_job_snapshot(
                connection, workspace_id=context.workspace_id, job_id=request.job_id
            )
            if wire is None:
                connection.commit()
                raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
            accepted_source = read_accepted_import_source(
                connection, workspace_id=context.workspace_id, job_id=request.job_id
            )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        result = JobGetResult.from_wire(wire)
        validate_job_get_result(
            result,
            request,
            accepted_import_source=(
                None
                if accepted_source is None
                else request_import_source(accepted_source)
            ),
        )
        return wire

    def job_cancel(self, context: OperationContext) -> AuditedOperationResult:
        request = None
        try:
            request = decode_job_cancel_input(context.request.input)
        except (ContractDecodeError, ContractSemanticError):
            pass
        if request is None:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        return self._control(context, request, cancel=True)

    def job_retry(self, context: OperationContext) -> AuditedOperationResult:
        request = None
        try:
            request = decode_job_retry_input(context.request.input)
        except (ContractDecodeError, ContractSemanticError):
            pass
        if request is None:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        return self._control(context, request, cancel=False)

    def _control(
        self, context: OperationContext, request: Any, *, cancel: bool
    ) -> AuditedOperationResult:
        connection, identity, guard = self._authority()
        previous_wire = read_application_job_snapshot(
            connection, workspace_id=context.workspace_id, job_id=request.job_id
        )
        previous = (
            None
            if previous_wire is None
            else JobGetResult.from_wire(previous_wire).job
        )
        if context.authorization is None:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
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
            if cancel:
                return request_job_cancellation(
                    fenced,
                    settlement,
                    workspace_id=context.workspace_id,
                    job_id=request.job_id,
                    reason=request.reason,
                    fencing_generation=guard.fencing_generation,
                    allocate_identifier=self.allocate_identifier,
                )
            return request_job_retry(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                job_id=request.job_id,
                fencing_generation=guard.fencing_generation,
                allocate_identifier=self.allocate_identifier,
            )

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                if cancel:
                    validate_job_cancel_result_shape(
                        JobCancelResult.from_wire(wire), request
                    )
                else:
                    validate_job_retry_result_shape(
                        JobRetryResult.from_wire(wire), request
                    )
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
        if not outcome.replayed:
            assert previous is not None
            if cancel:
                validate_job_cancel_result(
                    JobCancelResult.from_wire(outcome.result),
                    request,
                    previous=previous,
                )
            else:
                validate_job_retry_result(
                    JobRetryResult.from_wire(outcome.result),
                    request,
                    previous=previous,
                )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)

    def job_events(self, context: OperationContext) -> Mapping[str, Any]:
        request = None
        try:
            request = decode_job_events_input(context.request.input)
        except (ContractDecodeError, ContractSemanticError):
            pass
        if request is None:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        connection, _identity, _guard = self._authority()
        request_binding: JobEventsPageBinding | None = None
        if request.page is None:
            snapshot = application_job_event_count(
                connection, workspace_id=context.workspace_id, job_id=request.job_id
            )
            if snapshot is None:
                raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
            start = 0
        else:
            assert request.page.continuation_token is not None
            valid_page = True
            try:
                payload = self.token_codec.decode(request.page.continuation_token)
                if frozenset(payload) != _TOKEN_KEYS:
                    raise ValueError("wrong token fields")
                request_binding = JobEventsPageBinding(
                    int(payload["v"]),
                    str(payload["p"]),
                    str(payload["w"]),
                    str(payload["o"]),
                    str(payload["j"]),
                    str(payload["f"]),
                    int(payload["s"]),
                    int(payload["n"]),
                )
                validate_job_events_page_binding(
                    request_binding,
                    principal_id=context.principal,
                    workspace_id=context.workspace_id,
                    job_id=request.job_id,
                )
                snapshot = request_binding.snapshot_event_count
                start = request_binding.next_sequence
            except (ContractSemanticError, TypeError, ValueError, KeyError):
                valid_page = False
            if not valid_page:
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        assert snapshot is not None
        limit = 1000 if request.limit is None else request.limit
        events = read_application_job_events(
            connection,
            workspace_id=context.workspace_id,
            job_id=request.job_id,
            start_sequence=start,
            snapshot_event_count=snapshot,
            limit=limit,
        )
        if events is None:
            raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
        next_sequence = start + len(events)
        result_binding: JobEventsPageBinding | None = None
        continuation: str | None = None
        if next_sequence < snapshot:
            result_binding = JobEventsPageBinding(
                1,
                context.principal,
                context.workspace_id,
                JOB_EVENTS_OPERATION,
                request.job_id,
                "sequence_asc",
                snapshot,
                next_sequence,
            )
            continuation = self.token_codec.encode(
                {
                    "v": 1,
                    "p": context.principal,
                    "w": context.workspace_id,
                    "o": JOB_EVENTS_OPERATION,
                    "j": request.job_id,
                    "f": "sequence_asc",
                    "s": snapshot,
                    "n": next_sequence,
                }
            )
        wire: dict[str, Any] = {
            "job_id": request.job_id,
            "events": list(events),
            "snapshot_event_count": snapshot,
            "page": (
                {}
                if continuation is None
                else {"continuation_token": continuation}
            ),
        }
        result = JobEventsResult.from_wire(wire)
        validate_job_events_result(
            result,
            request,
            principal_id=context.principal,
            workspace_id=context.workspace_id,
            request_binding=request_binding,
            result_binding=result_binding,
        )
        return wire


def request_import_source(value: Mapping[str, object]) -> Any:
    """Decode the accepted descriptor without widening the storage module's API."""
    return decode_import_start_input({"source": dict(value)}).source


__all__ = [
    "IMPORT_START_OPERATION",
    "JOB_CANCEL_OPERATION",
    "JOB_EVENTS_OPERATION",
    "JOB_FAMILY_OPERATIONS",
    "JOB_GET_OPERATION",
    "JOB_RETRY_OPERATION",
    "JobHandlers",
]
