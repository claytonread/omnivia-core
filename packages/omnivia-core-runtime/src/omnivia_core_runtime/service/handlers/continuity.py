"""The real `continuity.*` handlers (SPEC-CORE-ENGMEM-001, plan PR-B).

Four of the nine engineering-memory operations are durable here: session
registration, checkpoint append, session close, and the handoff read. The
other five remain the honest `dependency_unavailable` refusals in
`handlers.engineering` until their producers land (preview projections, the
pack builder, the preference store, the review attestation path).

The security shape is the one the decision family established:

1. decode and validate through the contract's own decoder — never a local
   opinion about the payload's shape;
2. take the workspace, the effective principal and the purpose from the
   *authorised* context, never from the payload — the caller cannot bind
   another principal's session, and the server issues the session identity;
3. mutations run through the existing idempotency coordinator, so an
   honest replay returns the original receipt and a reused key with a
   different request is an explicit conflict;
4. every write lands inside the fenced transaction the guard triggers of
   migration 0048 protect, which is also where the workspace writer
   generation is enforced — a stale writer cannot commit a checkpoint;
5. the session-state and expected-predecessor checks re-read *inside* that
   transaction, so a session that closed or advanced under the request is
   honoured, and a competing successor loses as a precondition failure
   rather than silently replacing a newer checkpoint (§9.2);
6. refusals carry no caller value: every message is a frozen module constant.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    DEFAULT_RETRY_CLASSIFICATION,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_MUTATION_PRECONDITION_FAILED,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    ContinuityCheckpointAppendInput,
    ContinuityCheckpointAppendResult,
    ContinuityHandoffReadInput,
    ContinuitySessionCloseInput,
    ContinuitySessionCloseResult,
    ContinuitySessionRegisterInput,
    ContinuitySessionRegisterResult,
    ContractDecodeError,
    ContractSemanticError,
    idempotency_equivalence,
)
from omnivia_core_runtime.ownership.fencing import read_guard
from omnivia_core_runtime.service.mutation import (
    MutationIdempotencyConflict,
    MutationPreconditionFailed,
    MutationSettlementContext,
    execute_mutation,
    issue_mutation_grant,
)
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    OperationError,
)
from omnivia_core_runtime.storage import continuity as storage
from omnivia_core_runtime.storage.continuity import (
    ParentCheckpointMismatch,
    PayloadTooLarge,
    SequencePreconditionFailed,
    SessionNotActive,
    SessionNotFound,
)
from omnivia_core_runtime.storage.decisions import canonical_document, content_digest
from omnivia_core_runtime.storage.memory import IdentifierAllocator, random_identifier

_MESSAGE_INVALID: Final = "the request payload is not valid for this continuity operation"
_MESSAGE_NOT_FOUND: Final = "the requested continuity record was not found"
_MESSAGE_NO_STORAGE: Final = (
    "this service instance is not serving authoritative storage"
)
_MESSAGE_CONFLICT: Final = (
    "this continuity request conflicts with the session's current state"
)
_MESSAGE_PRECONDITION: Final = (
    "the continuity session advanced under this request; re-read and re-decide"
)

_ERROR_FOR_STORAGE: Final[tuple[tuple[type[BaseException], str, str], ...]] = (
    (
        SessionNotFound,
        ERROR_CODE_NOT_FOUND,
        _MESSAGE_NOT_FOUND,
    ),
    (
        SessionNotActive,
        ERROR_CODE_CONFLICT,
        _MESSAGE_CONFLICT,
    ),
    (
        ParentCheckpointMismatch,
        ERROR_CODE_CONFLICT,
        _MESSAGE_CONFLICT,
    ),
    (
        SequencePreconditionFailed,
        ERROR_CODE_MUTATION_PRECONDITION_FAILED,
        _MESSAGE_PRECONDITION,
    ),
    (
        PayloadTooLarge,
        ERROR_CODE_SIZE_LIMIT_EXCEEDED,
        "the checkpoint payload exceeds this workspace's size limit",
    ),
)


def _as_operation_error(error: BaseException) -> OperationError:
    for raised, code, message in _ERROR_FOR_STORAGE:
        if isinstance(error, raised):
            return OperationError(code, message)
    raise error


def _timestamp(us: int) -> str:
    return (
        _dt.datetime.fromtimestamp(us / 1_000_000, tz=_dt.UTC)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


@dataclass(frozen=True)
class ContinuityHandlers:
    """The four durable continuity handlers, wired as the other families are."""

    service: Any
    session: Any
    binding: Any
    clock: Any
    allocate_identifier: IdentifierAllocator = random_identifier

    def _authority(self) -> tuple[sqlite3.Connection, Any, Any]:
        connection = getattr(self.service, "connection", None)
        identity = getattr(self.service, "identity", None)
        guard = None if connection is None else read_guard(connection)
        if connection is None or identity is None or guard is None:
            raise OperationError(
                "internal_non_recoverable", _MESSAGE_NO_STORAGE
            )
        return connection, identity, guard

    # --- continuity.session.register -----------------------------------------

    def continuity_session_register(
        self, context: OperationContext
    ) -> AuditedOperationResult:
        try:
            request = ContinuitySessionRegisterInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection, identity, guard = self._authority()
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
            session_id = self.allocate_identifier("esess")
            now_us = settlement.settled_at_us
            storage.register_session(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                session_id=session_id,
                principal_id=context.principal,
                host_session_ref=request.host_session_ref,
                checkout_hint=request.checkout_hint,
                repository_target=(
                    None if request.repository_target is None
                    else request.repository_target.to_wire()
                ),
                registered_at_us=now_us,
            )
            session_wire: dict[str, Any] = {
                "session_id": session_id,
                "principal_id": context.principal,
                "workspace_id": context.workspace_id,
                "binding_generation": 1,
                "lease_expires_at": _timestamp(
                    now_us + storage.SESSION_LEASE_SECONDS * 1_000_000
                ),
                "state": "active",
            }
            if request.repository_target is not None:
                session_wire["repository_target"] = request.repository_target.to_wire()
            return {"session": session_wire}

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                ContinuitySessionRegisterResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        outcome = self._execute(context, connection, identity, grant, equivalence, mutate, valid_result)
        return AuditedOperationResult(outcome.result, audit_reference=outcome.audit_ref)

    # --- continuity.checkpoint.append ----------------------------------------

    def continuity_checkpoint_append(
        self, context: OperationContext
    ) -> AuditedOperationResult:
        try:
            request = ContinuityCheckpointAppendInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection, identity, guard = self._authority()
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
            receipt = storage.append_checkpoint(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                checkpoint_id=self.allocate_identifier("eck"),
                session_id=request.session_id,
                parent_checkpoint_id=request.parent_checkpoint_id,
                expected_parent_sequence=request.expected_parent_sequence,
                checkpoint_kind=request.payload.checkpoint_kind,
                payload=request.payload.to_wire(),
                recorded_at_us=settlement.settled_at_us,
            )
            return {
                "receipt": {
                    "checkpoint_id": receipt["checkpoint_id"],
                    "session_id": receipt["session_id"],
                    "sequence": receipt["sequence"],
                    "content_digest": receipt["content_digest"],
                    "recorded_at": _timestamp(receipt["recorded_at"]),
                    "audit_reference": settlement.audit_ref,
                }
            }

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                ContinuityCheckpointAppendResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        def precondition(fenced: Any) -> str | None:
            session = storage.read_session(
                fenced, workspace_id=context.workspace_id, session_id=request.session_id
            )
            if session is None:
                return None
            return f"seq-{session['last_checkpoint_sequence'] or 0}"

        outcome = self._execute(context, connection, identity, grant, equivalence, mutate, valid_result, precondition)
        return AuditedOperationResult(outcome.result, audit_reference=outcome.audit_ref)

    # --- continuity.session.close ---------------------------------------------

    def continuity_session_close(
        self, context: OperationContext
    ) -> AuditedOperationResult:
        try:
            request = ContinuitySessionCloseInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection, identity, guard = self._authority()
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
            final = request.final_checkpoint
            closed = storage.close_session(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                session_id=request.session_id,
                expected_sequence=request.expected_sequence,
                final_checkpoint=None if final is None else final.to_wire(),
                final_checkpoint_id=self.allocate_identifier("eck"),
                closed_at_us=settlement.settled_at_us,
            )
            receipt = closed.get("receipt")
            result: dict[str, Any] = {
                "session_id": closed["session_id"],
                "state": closed["state"],
                "checkpoint_recorded": closed["checkpoint_recorded"],
            }
            if receipt is not None:
                result["receipt"] = {
                    "checkpoint_id": receipt["checkpoint_id"],
                    "session_id": receipt["session_id"],
                    "sequence": receipt["sequence"],
                    "content_digest": receipt["content_digest"],
                    "recorded_at": _timestamp(receipt["recorded_at"]),
                    "audit_reference": settlement.audit_ref,
                }
            return result

        def valid_result(wire: Mapping[str, Any]) -> bool:
            try:
                ContinuitySessionCloseResult.from_wire(wire)
            except (ContractDecodeError, ContractSemanticError):
                return False
            return True

        def precondition(fenced: Any) -> str | None:
            session = storage.read_session(
                fenced, workspace_id=context.workspace_id, session_id=request.session_id
            )
            if session is None:
                return None
            return f"seq-{session['last_checkpoint_sequence'] or 0}"

        outcome = self._execute(context, connection, identity, grant, equivalence, mutate, valid_result, precondition)
        return AuditedOperationResult(outcome.result, audit_reference=outcome.audit_ref)

    # --- continuity.handoff.read -----------------------------------------------

    def continuity_handoff_read(
        self, context: OperationContext
    ) -> Mapping[str, Any]:
        try:
            request = ContinuityHandoffReadInput.from_wire(context.request.input)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID) from error
        connection, _identity, _guard = self._authority()
        connection.execute("BEGIN")
        try:
            if request.checkpoint_id is not None:
                record = storage.read_checkpoint(
                    connection,
                    workspace_id=context.workspace_id,
                    checkpoint_id=request.checkpoint_id,
                )
            elif request.session_id is not None and request.sequence is not None:
                record = self._read_by_sequence(
                    connection,
                    workspace_id=context.workspace_id,
                    session_id=request.session_id,
                    sequence=request.sequence,
                )
            else:
                raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        finally:
            connection.execute("ROLLBACK")
        if record is None:
            raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
        payload = record["payload"]
        view: dict[str, Any] = {
            "format_version": "continuity_handoff.v1",
            "checkpoint_id": record["checkpoint_id"],
            "content_digest": content_digest(
                canonical_document(
                    {
                        "checkpoint_id": record["checkpoint_id"],
                        "sequence": record["sequence"],
                        "payload": payload,
                    }
                )
            ),
            "redacted": False,
            "objective": str(payload.get("objective", ""))[:2000] or "(no objective recorded)",
            "applicability": (
                "not_evaluated" if request.target_snapshot is None else "unknown"
            ),
        }
        if payload.get("unresolved_work"):
            view["unresolved_work"] = [str(item)[:2000] for item in payload["unresolved_work"]]
        if payload.get("next_actions"):
            view["next_actions"] = [str(item)[:2000] for item in payload["next_actions"]]
        view["omissions"] = []
        return {"handoff": view}

    def _read_by_sequence(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        session_id: str,
        sequence: int,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT checkpoint_id FROM omnivia_engineering_checkpoints "
            "WHERE workspace_id = ? AND session_id = ? AND sequence = ?",
            (workspace_id, session_id, sequence),
        ).fetchone()
        if row is None:
            return None
        return storage.read_checkpoint(
            connection, workspace_id=workspace_id, checkpoint_id=row[0]
        )

    def _execute(
        self,
        context: OperationContext,
        connection: sqlite3.Connection,
        identity: Any,
        grant: Any,
        equivalence: Any,
        mutate: Any,
        valid_result: Any,
        precondition: Any = None,
    ) -> Any:
        try:
            return execute_mutation(
                connection,
                identity,
                grant=grant,
                context=context.authorization,
                equivalence=equivalence,
                precondition=precondition,
                mutate=mutate,
                validate_result=valid_result,
                clock=self.clock,
                allocate_identifier=self.allocate_identifier,
            )
        except MutationIdempotencyConflict as error:
            raise OperationError(
                error.code, error.message, retry_class=error.retry_class
            ) from error
        except (SessionNotFound, SessionNotActive, ParentCheckpointMismatch,
                SequencePreconditionFailed, PayloadTooLarge) as error:
            raise _as_operation_error(error) from error
        except MutationPreconditionFailed as error:
            raise OperationError(
                ERROR_CODE_MUTATION_PRECONDITION_FAILED,
                _MESSAGE_PRECONDITION,
                retry_class=(
                    DEFAULT_RETRY_CLASSIFICATION[ERROR_CODE_MUTATION_PRECONDITION_FAILED]
                ),
            ) from error
