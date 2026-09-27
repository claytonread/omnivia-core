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
6. refusals carry no caller value: every message is a frozen module constant;
7. a session is its registering principal's alone. Append, close (with its
   final checkpoint) and handoff resolve it as the effective principal
   (`context.principal`, never the principal this owner-composed handler was
   issued for), in the precondition read and again inside the fenced write.
   Another principal's session or checkpoint is `not_found` exactly as a
   missing one, before any stated version is compared. There is no sharing
   grant, so continuity is same-principal only.
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
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
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
from omnivia_core_runtime.storage import repository_identity as repo_identity
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
_MESSAGE_UNACCOUNTED_PAYLOAD_FIELD: Final = (
    "the stored continuity checkpoint carries a payload field this handoff has no policy for"
)
_MESSAGE_INTEGRITY: Final = (
    "the stored continuity checkpoint failed an internal integrity check"
)

# A handoff is a deliberately small projection of checkpoint evidence.  These
# regions either require their own current authorisation check, describe the
# sender's runtime, or could be mistaken for authority to act.  The receiver is
# told which *region* was withheld, never which record, source, run or effect was
# inside it.  A tuple gives both the response and its digest one stable order.
# `checkpoint_kind` is here too: it is sender-side taxonomy, not delivered
# working context, and is withheld the same way as the other redacted regions.
_HANDOFF_OMITTED_REGIONS: Final[tuple[tuple[str, str], ...]] = (
    ("accepted_record_refs", "retrieve_current_separately"),
    ("candidate_record_refs", "working_context_redacted"),
    ("checkpoint_kind", "working_context_redacted"),
    ("completed_work", "working_context_redacted"),
    ("context_receipt", "not_a_persisted_handle"),
    ("external_effects", "owner_reconciliation_required"),
    ("external_run_ref", "sender_runtime_context"),
    ("failed_approaches", "working_context_redacted"),
    ("observations", "requires_fresh_authorization"),
    ("relevant_sources", "requires_fresh_authorization"),
    ("repository_snapshots", "requires_fresh_authorization"),
)

#: The fields a handoff renders directly, rather than omitting with a reason.
_HANDOFF_RENDERED_FIELDS: Final[frozenset[str]] = frozenset(
    {"objective", "unresolved_work", "next_actions"}
)

#: Every field of the persisted checkpoint payload this handoff accounts for,
#: rendered or omitted.  A field the payload contract adds and this set does
#: not name has no handoff policy yet; `continuity_handoff_read` fails closed
#: rather than silently passing it through or silently dropping it uncounted.
_HANDOFF_KNOWN_FIELDS: Final[frozenset[str]] = _HANDOFF_RENDERED_FIELDS | frozenset(
    field for field, _reason in _HANDOFF_OMITTED_REGIONS
)

# The stored checkpoint is already capped at 256 KiB, but a handoff is intended
# to be a compact receiver view.  Bound each textual list independently so the
# response shape is predictable even for a valid checkpoint near that cap.
_HANDOFF_TEXT_ITEM_LIMIT: Final = 32

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
    (
        repo_identity.SnapshotNotFound,
        ERROR_CODE_NOT_FOUND,
        _MESSAGE_NOT_FOUND,
    ),
    (
        repo_identity.RepositoryNotFound,
        ERROR_CODE_NOT_FOUND,
        _MESSAGE_NOT_FOUND,
    ),
    (
        repo_identity.RepositoryAmbiguous,
        ERROR_CODE_CONFLICT,
        "the repository label matches more than one registration; resolve by id",
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


def _handoff_text_region(
    payload: Mapping[str, Any],
    field: str,
    omissions: list[dict[str, str]],
) -> list[str] | None:
    """Render one bounded textual region and account for a partial projection."""
    raw = payload.get(field)
    if not isinstance(raw, list) or not raw:
        return None
    rendered = [str(item)[:2000] for item in raw[:_HANDOFF_TEXT_ITEM_LIMIT]]
    if len(raw) > _HANDOFF_TEXT_ITEM_LIMIT:
        omissions.append({"field": field, "reason": "bounded"})
    return rendered


def _handoff_omissions(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """Describe withheld regions without disclosing any identity inside them."""
    return [
        {"field": field, "reason": reason}
        for field, reason in _HANDOFF_OMITTED_REGIONS
        if field in payload
    ]


def _session_version(
    fenced: sqlite3.Connection, context: OperationContext, session_id: str
) -> str:
    """The version append and close compare, read under the fence as the caller.

    A session the effective principal does not own is `SessionNotFound` here,
    before the stated version is compared, so its head is never disclosed as a
    precondition failure.
    """
    session = storage.read_session(
        fenced,
        workspace_id=context.workspace_id,
        session_id=session_id,
        principal_id=context.principal,
    )
    if session is None:
        raise SessionNotFound(session_id)
    return f"seq-{session['last_checkpoint_sequence'] or 0}"


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
            if request.repository_target is not None:
                repo_identity.validate_snapshot_ref(
                    fenced,
                    workspace_id=context.workspace_id,
                    repository_id=request.repository_target.repository_id,
                    snapshot_id=request.repository_target.snapshot_id,
                )
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
            for snapshot in request.payload.repository_snapshots or ():
                repo_identity.validate_snapshot_ref(
                    fenced,
                    workspace_id=context.workspace_id,
                    repository_id=snapshot.repository_id,
                    snapshot_id=snapshot.snapshot_id,
                )
            receipt = storage.append_checkpoint(
                fenced,
                settlement,
                workspace_id=context.workspace_id,
                principal_id=context.principal,
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

        def precondition(fenced: Any) -> str:
            return _session_version(fenced, context, request.session_id)

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
                principal_id=context.principal,
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

        def precondition(fenced: Any) -> str:
            return _session_version(fenced, context, request.session_id)

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
        # Exactly one selector shape is valid: `checkpoint_id` alone, or
        # `session_id` and `sequence` together. Any other combination -- both
        # named at once (whether or not they agree), or only one half of the
        # session pair -- is refused here, before any database access.
        valid_selector = (
            request.checkpoint_id is not None
            and request.session_id is None
            and request.sequence is None
        ) or (
            request.checkpoint_id is None
            and request.session_id is not None
            and request.sequence is not None
        )
        if not valid_selector:
            raise OperationError(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INVALID)
        connection, _identity, _guard = self._authority()
        # One statement resolves the checkpoint and its session's owner before the
        # payload is loaded: another principal's checkpoint is `not_found` below.
        record = storage.read_checkpoint(
            connection,
            workspace_id=context.workspace_id,
            principal_id=context.principal,
            checkpoint_id=request.checkpoint_id,
            session_id=request.session_id,
            sequence=request.sequence,
        )
        if record is None:
            raise OperationError(ERROR_CODE_NOT_FOUND, _MESSAGE_NOT_FOUND)
        payload = record["payload"]
        # The stored payload must still be exactly what its own content_digest
        # covers -- recomputed with the same canonicalization storage used when
        # writing it -- before any of it is interpreted or rendered. This is the
        # original stored payload's digest, never the rendered view's own.
        if content_digest(canonical_document(payload)) != record["content_digest"]:
            raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_INTEGRITY)
        # Every persisted payload field must be either rendered or explicitly
        # omitted with a reason. A field the payload contract added without a
        # handoff policy is never disclosed by default: fail closed instead.
        if set(payload) - _HANDOFF_KNOWN_FIELDS:
            raise OperationError(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_UNACCOUNTED_PAYLOAD_FIELD
            )
        omissions = _handoff_omissions(payload)
        view: dict[str, Any] = {
            "format_version": "continuity_handoff.v1",
            "checkpoint_id": record["checkpoint_id"],
            "objective": str(payload.get("objective", ""))[:2000] or "(no objective recorded)",
            "applicability": (
                "not_evaluated" if request.target_snapshot is None else "unknown"
            ),
        }
        for field in ("unresolved_work", "next_actions"):
            rendered = _handoff_text_region(payload, field, omissions)
            if rendered is not None:
                view[field] = rendered
        omissions.sort(key=lambda omission: (omission["field"], omission["reason"]))
        view["omissions"] = omissions
        view["redacted"] = bool(omissions)

        # A digest cannot literally include itself.  The canonical handoff digest
        # therefore covers every delivered field except `content_digest`, including
        # the exact bounded text, redaction label and omission diagnostics.  It never
        # covers sender-only checkpoint content that was not delivered.
        view["content_digest"] = content_digest(canonical_document(view))
        return {"handoff": view}

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
                SequencePreconditionFailed, PayloadTooLarge,
                repo_identity.RepositoryNotFound,
                repo_identity.RepositoryAmbiguous,
                repo_identity.SnapshotNotFound) as error:
            raise _as_operation_error(error) from error
        except MutationPreconditionFailed as error:
            raise OperationError(
                ERROR_CODE_MUTATION_PRECONDITION_FAILED,
                _MESSAGE_PRECONDITION,
                retry_class=(
                    DEFAULT_RETRY_CLASSIFICATION[ERROR_CODE_MUTATION_PRECONDITION_FAILED]
                ),
            ) from error
