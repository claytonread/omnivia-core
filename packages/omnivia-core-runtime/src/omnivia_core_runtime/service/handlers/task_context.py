"""The four task-context operations: an export and its read, an outcome request and its read (DEV-REQ-159, DEV-REQ-008).

An export and an outcome request are written through `execute_mutation`, so their grant, idempotency,
audit and rollback are the ones every mutation has. Their reads are observations. Nothing here reads a
workspace, a principal or a policy from a request: the workspace and principal come from the authenticated
`OperationContext`, the grant is server-issued, and the policy is the one `service/task_context.py` serves.
Every handler decodes its request through the contract and then refuses any key the operation does not
declare, because the contract's decoder ignores unknown members.

The domain rules are in `service/task_context.py` and the persistence is in `storage/task_context.py`. This
module maps their closed refusals onto the contract's error codes and builds the wire results. A refusal
carries a fixed message and no caller, identifier or stored value. A stored row that fails its identity
check is a corruption fault, never a missing or ineligible row.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    ContractDecodeError,
    ContractSemanticError,
    OutcomeAdmission,
    OutcomeRequestCreateInput,
    OutcomeRequestCreateResult,
    OutcomeRequestReadInput,
    OutcomeRequestReadResult,
    ProjectContextReadInput,
    ProjectContextReadResult,
    ProjectContextSwitchInput,
    ProjectContextSwitchResult,
    TaskContextExportInput,
    TaskContextExportReadInput,
    TaskContextExportReadResult,
    TaskContextExportResult,
    idempotency_equivalence,
)
from omnivia_core_runtime.ownership.fencing import MutationGuard, read_guard
from omnivia_core_runtime.ownership.identity import Clock
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ServiceBinding,
)
from omnivia_core_runtime.service.mutation import (
    MutationGrant,
    MutationSettlementContext,
    execute_mutation,
    issue_mutation_grant,
)
from omnivia_core_runtime.service.operations import (
    AuditedOperationResult,
    OperationContext,
    application_refusal,
)
from omnivia_core_runtime.service.outcome_admission import (
    NO_OUTCOME_ADMISSIONS,
    OutcomeAdmissionAuthority,
)
from omnivia_core_runtime.service.task_context import (
    EXPORT_OPERATION as OPERATION_EXPORT,
)
from omnivia_core_runtime.service.task_context import (
    REFUSED_ADMISSION_INVALID,
    REFUSED_ADMISSION_NOT_FOUND,
    REFUSED_BUDGET_INSUFFICIENT,
    REFUSED_BUDGET_INVALID,
    REFUSED_CONTEXT_MISMATCH,
    REFUSED_EXPORT_MISMATCH,
    REFUSED_HANDOFF_INVALID,
    REFUSED_HANDOFF_MISSING,
    REFUSED_INELIGIBLE,
    REFUSED_LIFECYCLE_CLOSED,
    REFUSED_NOT_FOUND,
    REFUSED_NOT_MEMBER,
    REFUSED_OBJECTIVE_INVALID,
    REFUSED_OBJECTIVE_UNBOUNDED,
    REFUSED_SIZE_EXCEEDED,
    REFUSED_STALE_FENCE,
    TaskContextRefused,
    build_export,
    build_outcome_request,
    check_standing,
    decide_switch,
    parse_admission,
    plain_copy,
)
from omnivia_core_runtime.storage.task_context import (
    CONTEXT_TOKEN_PREFIX,
    StoredExport,
    StoredOutcomeRequest,
    StoredProjectContext,
    TaskContextInvalid,
    read_export,
    read_outcome_request,
    read_project_context,
    record_export,
    record_outcome_request,
    record_project_context,
)

OPERATION_EXPORT_READ: Final = "task_context.export.read"
OPERATION_OUTCOME_CREATE: Final = "outcome.request.create"
OPERATION_OUTCOME_READ: Final = "outcome.request.read"
OPERATION_PROJECT_CONTEXT_READ: Final = "project.context.read"
OPERATION_PROJECT_CONTEXT_SWITCH: Final = "project.context.switch"

TASK_CONTEXT_FAMILY_OPERATIONS: Final = frozenset(
    {
        OPERATION_EXPORT,
        OPERATION_EXPORT_READ,
        OPERATION_OUTCOME_CREATE,
        OPERATION_OUTCOME_READ,
        OPERATION_PROJECT_CONTEXT_READ,
        OPERATION_PROJECT_CONTEXT_SWITCH,
    }
)

_MESSAGE_NO_STORAGE: Final = (
    "the task-context store is not reachable from this service instance"
)
_MESSAGE_INVALID: Final = (
    "the request payload is not valid for this task-context operation"
)
_MESSAGE_BUDGET: Final = "the explicit budget cannot hold the export"
_MESSAGE_SIZE: Final = "the export or objective exceeds its size bound"
_MESSAGE_NOT_FOUND: Final = "no such export or outcome request is visible to the caller"
_MESSAGE_CONFLICT: Final = "the export is not in a state that allows this request"
_MESSAGE_CORRUPT: Final = "the stored task-context row failed its integrity check"
_MESSAGE_ADMISSION_NOT_FOUND: Final = (
    "no such Project, Work, source or revision is bound for this Workspace"
)
_MESSAGE_NOT_MEMBER: Final = "the caller is not an owner or member of the Project"

_STATUS_BY_REASON: Final[Mapping[str, tuple[str, str]]] = {
    REFUSED_HANDOFF_MISSING: (ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID),
    REFUSED_HANDOFF_INVALID: (ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID),
    REFUSED_BUDGET_INVALID: (ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID),
    REFUSED_OBJECTIVE_INVALID: (ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID),
    REFUSED_BUDGET_INSUFFICIENT: (ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_BUDGET),
    REFUSED_SIZE_EXCEEDED: (ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_SIZE),
    REFUSED_OBJECTIVE_UNBOUNDED: (ERROR_CODE_SIZE_LIMIT_EXCEEDED, _MESSAGE_SIZE),
    REFUSED_NOT_FOUND: (ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND),
    REFUSED_STALE_FENCE: (ERROR_CODE_CONFLICT, _MESSAGE_CONFLICT),
    REFUSED_INELIGIBLE: (ERROR_CODE_CONFLICT, _MESSAGE_CONFLICT),
    REFUSED_ADMISSION_INVALID: (ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID),
    REFUSED_ADMISSION_NOT_FOUND: (ERROR_CODE_NOT_FOUND, _MESSAGE_ADMISSION_NOT_FOUND),
    REFUSED_NOT_MEMBER: (ERROR_CODE_AUTHORIZATION_DENIED, _MESSAGE_NOT_MEMBER),
    REFUSED_CONTEXT_MISMATCH: (ERROR_CODE_CONFLICT, _MESSAGE_CONFLICT),
    REFUSED_EXPORT_MISMATCH: (ERROR_CODE_CONFLICT, _MESSAGE_CONFLICT),
    REFUSED_LIFECYCLE_CLOSED: (ERROR_CODE_CONFLICT, _MESSAGE_CONFLICT),
}


def _timestamp(microseconds: int) -> str:
    """One microsecond instant, spelled as the wire's UTC `Timestamp`."""
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=microseconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond:06d}Z"


def _refusing(action: Callable[[], Any]) -> Any:
    """Run one domain step, mapping its closed refusals onto the contract's error codes.

    The messages are fixed, so no identifier, handoff, objective or principal reaches a caller. A stored
    row that fails its identity check is a fault, and a storage constraint the domain checks allowed is a
    conflict: the request lost a race, it did not find a row.
    """
    try:
        return action()
    except TaskContextRefused as refused:
        code, message = _STATUS_BY_REASON.get(
            refused.reason, (ERROR_CODE_CONFLICT, _MESSAGE_CONFLICT)
        )
        raise application_refusal(code, message) from refused
    except TaskContextInvalid as invalid:
        raise application_refusal(
            ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_CORRUPT
        ) from invalid
    except sqlite3.IntegrityError as integrity:
        raise application_refusal(ERROR_CODE_CONFLICT, _MESSAGE_CONFLICT) from integrity


def _export_fields(export: StoredExport) -> dict[str, Any]:
    columns = export.columns()
    return {
        "export_id": columns["export_id"],
        "source_handoff_identity": columns["source_handoff_identity"],
        "exported_by": columns["exported_by"],
        "policy_digest": columns["policy_digest"],
        "fencing_generation": columns["fencing_generation"],
        "token_budget": columns["token_budget"],
        "byte_budget": columns["byte_budget"],
        "token_estimate": columns["token_estimate"],
        "byte_estimate": columns["byte_estimate"],
        "created_at": _timestamp(columns["created_at_us"]),
        "document": export.document,
    }


def _outcome_fields(request: StoredOutcomeRequest) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "outcome_request_id": request.outcome_request_id,
        "export_id": request.export_id,
        "source_handoff_identity": request.source_handoff_identity,
        "requested_by": request.requested_by,
        "objective": request.objective,
        "status": request.status,
        "fencing_generation": request.fencing_generation,
        "created_at": _timestamp(request.created_at_us),
    }
    # A legacy request omits the admission fields entirely, as it always has.
    if request.admission_json is not None:
        fields["admission"] = OutcomeAdmission.from_wire(
            {
                "summary": json.loads(request.admission_json),
                "identity": request.admission_identity,
            }
        )
        fields["context_generation"] = CONTEXT_TOKEN_PREFIX + str(
            request.context_generation
        )
    return fields


def _context_fields(current: StoredProjectContext | None) -> dict[str, Any]:
    """The typed empty state when no Project is active, otherwise the Project and its opaque generation token."""
    if current is None:
        return {"state": "none"}
    return {
        "state": "active",
        "project_id": current.project_id,
        "context_generation": current.token,
    }


def _servable(
    decode: Callable[[object], object],
) -> Callable[[Mapping[str, Any]], bool]:
    """Whether a result, fresh or replayed, still decodes as the contract's result type."""

    def valid(wire: Mapping[str, Any]) -> bool:
        try:
            decode(wire)
        except (ContractDecodeError, ContractSemanticError):
            return False
        return True

    return valid


_VALID_EXPORT = _servable(TaskContextExportResult.from_wire)
_VALID_OUTCOME = _servable(OutcomeRequestCreateResult.from_wire)
_VALID_CONTEXT = _servable(ProjectContextSwitchResult.from_wire)


def _read_export(connection: Any, *, workspace_id: str, export_id: str) -> StoredExport:
    """The export in this workspace, or `not_found`. A row in another workspace is never reached."""
    export = read_export(connection, workspace_id=workspace_id, export_id=export_id)
    if export is None:
        raise TaskContextRefused(
            REFUSED_NOT_FOUND, "no such export is visible to the caller"
        )
    return export


def _read_outcome(
    connection: Any, *, workspace_id: str, outcome_request_id: str
) -> StoredOutcomeRequest:
    request = read_outcome_request(
        connection, workspace_id=workspace_id, outcome_request_id=outcome_request_id
    )
    if request is None:
        raise TaskContextRefused(
            REFUSED_NOT_FOUND, "no such outcome request is visible to the caller"
        )
    return request


@dataclass
class TaskContextHandlers:
    """The four task-context operations over one workspace, served to the principal the session names.

    The handlers hold no caller-selected authority. The workspace is the one this family is composed for,
    and a context from any other workspace is refused before any row is read.
    """

    service: Any
    session: AuthenticatedSession
    binding: ServiceBinding
    clock: Clock
    allocate_identifier: Callable[[str], str]
    #: The Projects this installation admits outcomes for. Composed once, and empty by default.
    admission: OutcomeAdmissionAuthority = NO_OUTCOME_ADMISSIONS

    def _connection(self) -> Any:
        connection = getattr(self.service, "connection", None)
        if connection is None:
            raise application_refusal(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        return connection

    def _authority(self) -> tuple[Any, Any, MutationGuard]:
        connection = self._connection()
        identity = getattr(self.service, "identity", None)
        guard = read_guard(connection)
        if identity is None or guard is None:
            raise application_refusal(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        return connection, identity, guard

    def _bound(self, context: OperationContext) -> None:
        """Refuse a context for any workspace other than the one this family serves."""
        if context.workspace_id != self.binding.workspace_id:
            raise application_refusal(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)

    @staticmethod
    def _input(context: OperationContext, allowed: frozenset[str], decode: Any) -> Any:
        """Decode the request, refusing a key the operation does not declare rather than dropping it."""
        raw = context.request.input
        if not isinstance(raw, Mapping) or not set(raw) <= allowed:
            raise application_refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        try:
            return decode(raw)
        except (ContractDecodeError, ContractSemanticError) as error:
            raise application_refusal(
                ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID
            ) from error

    def _grant(
        self, context: OperationContext, payload: Mapping[str, Any]
    ) -> tuple[MutationGrant, Any]:
        _connection, _identity, guard = self._authority()
        if context.authorization is None:
            raise application_refusal(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_NO_STORAGE
            )
        equivalence = idempotency_equivalence(
            context.request.operation,
            context.request.metadata,
            payload,
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
        return grant, equivalence

    def _mutate(
        self,
        context: OperationContext,
        payload: Mapping[str, Any],
        mutate: Callable[
            [Any, MutationGrant, MutationSettlementContext], Mapping[str, Any]
        ],
        valid: Callable[[Mapping[str, Any]], bool],
        replay_authority: Callable[[Any], None],
    ) -> AuditedOperationResult:
        connection, identity, _guard = self._authority()
        grant, equivalence = self._grant(context, payload)
        outcome = execute_mutation(
            connection,
            identity,
            grant=grant,
            context=context.authorization,
            equivalence=equivalence,
            replay_authority=replay_authority,
            mutate=lambda fenced, settlement: mutate(fenced, grant, settlement),
            validate_result=valid,
            clock=self.clock,
            allocate_identifier=self.allocate_identifier,
        )
        return AuditedOperationResult(outcome.result, outcome.audit_ref)

    def task_context_export(self, context: OperationContext) -> AuditedOperationResult:
        """Project one verified handoff into a bounded export in this workspace, recorded once."""
        self._bound(context)
        request = self._input(
            context,
            frozenset({"handoff", "token_budget", "byte_budget"}),
            TaskContextExportInput.from_wire,
        )
        payload = plain_copy(context.request.input)

        def mutate(
            fenced: Any, grant: MutationGrant, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            export = _refusing(
                lambda: build_export(
                    workspace_id=context.workspace_id,
                    principal=context.principal,
                    fencing_generation=grant.fencing_generation,
                    handoff=request.handoff,
                    token_budget=request.token_budget,
                    byte_budget=request.byte_budget,
                    created_at_us=settlement.settled_at_us,
                )
            )
            stored = _refusing(lambda: record_export(fenced, export))
            return TaskContextExportResult(**_export_fields(stored)).to_wire()

        def replay_authority(fenced: Any) -> None:
            self._bound(context)

        return self._mutate(context, payload, mutate, _VALID_EXPORT, replay_authority)

    def task_context_export_read(self, context: OperationContext) -> Mapping[str, Any]:
        """Serve one export recorded in this workspace, re-verified against its own identity."""
        self._bound(context)
        request = self._input(
            context, frozenset({"export_id"}), TaskContextExportReadInput.from_wire
        )
        connection = self._connection()
        export = _refusing(
            lambda: _read_export(
                connection,
                workspace_id=context.workspace_id,
                export_id=request.export_id,
            )
        )
        return TaskContextExportReadResult(**_export_fields(export)).to_wire()

    def project_context_read(self, context: OperationContext) -> Mapping[str, Any]:
        """Observe the Workspace's active Project and its generation, or the typed empty state."""
        self._bound(context)
        self._input(context, frozenset(), ProjectContextReadInput.from_wire)
        connection = self._connection()
        current = _refusing(
            lambda: read_project_context(connection, workspace_id=context.workspace_id)
        )
        return ProjectContextReadResult(**_context_fields(current)).to_wire()

    def project_context_switch(
        self, context: OperationContext
    ) -> AuditedOperationResult:
        """Make one bound Project the Workspace's active context. The first choice is generation one."""
        self._bound(context)
        request = self._input(
            context,
            frozenset({"project_id"}),
            ProjectContextSwitchInput.from_wire,
        )
        payload = plain_copy(context.request.input)

        def mutate(
            fenced: Any, grant: MutationGrant, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            current = _refusing(
                lambda: read_project_context(fenced, workspace_id=context.workspace_id)
            )
            decided = _refusing(
                lambda: decide_switch(
                    workspace_id=context.workspace_id,
                    principal=context.principal,
                    project_id=request.project_id,
                    current=current,
                    authority=self.admission,
                    fencing_generation=grant.fencing_generation,
                    switched_at_us=settlement.settled_at_us,
                )
            )
            # Choosing the Project that is already active writes nothing and keeps its generation.
            if decided is not current:
                _refusing(
                    lambda: record_project_context(fenced, decided, previous=current)
                )
            return ProjectContextSwitchResult(**_context_fields(decided)).to_wire()

        def replay_authority(fenced: Any) -> None:
            self._bound(context)

        return self._mutate(context, payload, mutate, _VALID_CONTEXT, replay_authority)

    def outcome_request_create(
        self, context: OperationContext
    ) -> AuditedOperationResult:
        """Receive one objective against an export of this workspace, under the current fence.

        A structured request also carries an admission. It is checked before anything is stored, and a replay of it
        is answered only while the caller still stands in the declared Project at the generation it named.
        """
        self._bound(context)
        request = self._input(
            context,
            frozenset({"objective", "export_id", "admission"}),
            OutcomeRequestCreateInput.from_wire,
        )
        payload = plain_copy(context.request.input)
        admission = (
            _refusing(lambda: parse_admission(payload["admission"]))
            if "admission" in payload
            else None
        )

        def mutate(
            fenced: Any, grant: MutationGrant, settlement: MutationSettlementContext
        ) -> Mapping[str, Any]:
            export = _refusing(
                lambda: _read_export(
                    fenced,
                    workspace_id=context.workspace_id,
                    export_id=request.export_id,
                )
            )
            active = _refusing(
                lambda: read_project_context(fenced, workspace_id=context.workspace_id)
            )
            outcome = _refusing(
                lambda: build_outcome_request(
                    workspace_id=context.workspace_id,
                    principal=context.principal,
                    objective=request.objective,
                    export=export,
                    current_generation=grant.fencing_generation,
                    created_at_us=settlement.settled_at_us,
                    admission=admission,
                    authority=self.admission,
                    active=active,
                )
            )
            stored = _refusing(lambda: record_outcome_request(fenced, outcome))
            return OutcomeRequestCreateResult(**_outcome_fields(stored)).to_wire()

        def replay_authority(fenced: Any) -> None:
            # A replayed request still answers only while the export it names is in this workspace, and, for a
            # structured one, while the caller stands in its Project at the generation it named.
            _refusing(
                lambda: _read_export(
                    fenced,
                    workspace_id=context.workspace_id,
                    export_id=request.export_id,
                )
            )
            if admission is not None:
                _refusing(
                    lambda: check_standing(
                        admission,
                        principal=context.principal,
                        authority=self.admission,
                        active=read_project_context(
                            fenced, workspace_id=context.workspace_id
                        ),
                    )
                )

        return self._mutate(context, payload, mutate, _VALID_OUTCOME, replay_authority)

    def outcome_request_read(self, context: OperationContext) -> Mapping[str, Any]:
        """Serve one outcome request recorded in this workspace, re-verified against its own identity."""
        self._bound(context)
        request = self._input(
            context,
            frozenset({"outcome_request_id"}),
            OutcomeRequestReadInput.from_wire,
        )
        connection = self._connection()
        stored = _refusing(
            lambda: _read_outcome(
                connection,
                workspace_id=context.workspace_id,
                outcome_request_id=request.outcome_request_id,
            )
        )
        return OutcomeRequestReadResult(**_outcome_fields(stored)).to_wire()


__all__ = [
    "OPERATION_EXPORT",
    "OPERATION_EXPORT_READ",
    "OPERATION_OUTCOME_CREATE",
    "OPERATION_OUTCOME_READ",
    "OPERATION_PROJECT_CONTEXT_READ",
    "OPERATION_PROJECT_CONTEXT_SWITCH",
    "TASK_CONTEXT_FAMILY_OPERATIONS",
    "TaskContextHandlers",
]
