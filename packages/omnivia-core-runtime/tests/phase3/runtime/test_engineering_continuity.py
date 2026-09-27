"""The engineering continuity vertical, over the real substrate (plan PR-B).

Register → append → close → handoff, driven through the real handlers with the
real twelve-check authorisation seam, the real guard triggers of migration 0048
and the real mutation coordinator: a replayed key returns the original receipt,
a competing successor loses as a precondition failure, an append to a closed
session is a conflict, an oversized payload is refused whole, and the durable
receipt survives a service-restart-shaped reopen of the same database.

Two contributors sharing the workspace, driven through the production
application surface, each see and change only their own sessions and
checkpoints: in the continuity operations, the `working_context` search and
the `resume` pack. Another principal's are indistinguishable from missing ones.
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import socket
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier, RLock
from types import SimpleNamespace
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_blobs_staged_sources_and_evidence_migration as m2
import test_engineering_source_coverage as sc
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.fencing import StaleGeneration, fenced_transaction
from omnivia_core_runtime.service.application import (
    ENGINEERING_FAMILY_PURPOSES,
    authorize_application_request,
    engineering_family_session,
)
from omnivia_core_runtime.service.authorization import (
    AuthenticatedSession,
    ContinuityAssociationProvenance,
    ContinuityBindingProvenance,
    TrustedContinuityAssociation,
    TrustedContinuityBinding,
)
from omnivia_core_runtime.service.handlers.continuity import ContinuityHandlers
from omnivia_core_runtime.service.http_transport import (
    APPLICATION_PATH,
    CONTENT_TYPE,
    HttpBind,
    HttpListener,
)
from omnivia_core_runtime.service.operations import (
    OperationContext,
    OperationError,
)
from omnivia_core_runtime.service.ovc1 import (
    HEADER_BYTES,
    canonical_json_bytes,
    decode_frame,
    encode_frame,
)
from omnivia_core_runtime.service.pagination import PROCESS_CONTINUATION_TOKENS
from omnivia_core_runtime.service.probes import ProbeRouter, ServiceFacts
from omnivia_core_runtime.service.protocol import DocumentRouter
from omnivia_core_runtime.service.transport import LocalSocketServer, endpoint_for_path
from omnivia_core_runtime.storage import continuity as continuity_storage
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    foreign_key_check,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.decisions import canonical_document, content_digest
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    load_migrations,
    read_workspace_state,
)

from omnivia_core.contracts.v1 import (
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_MUTATION_PRECONDITION_FAILED,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    CapabilityRef,
    ContinuitySessionBinding,
    EngineeringCheckpointPayload,
    ErrorResponseEnvelope,
    MutationPrecondition,
    SuccessResponseEnvelope,
    decode_response,
    encode_request,
    get_operation_metadata,
)

WORKSPACE_ID = s0.WORKSPACE_ID

REGISTER = get_operation_metadata("continuity.session.register")
APPEND = get_operation_metadata("continuity.checkpoint.append")
CLOSE = get_operation_metadata("continuity.session.close")
HANDOFF = get_operation_metadata("continuity.handoff.read")


def _owned(tmp_path: Any) -> Any:
    path = tmp_path / "workspace.sqlite"
    s0.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    return m1.take_ownership(path)


def _session(entry: Any) -> AuthenticatedSession:
    return s0.session_for(entry)


_DIRECT_BINDINGS: dict[tuple[str, str], TrustedContinuityBinding] = {}


def _trusted_registration_binding(result: Any) -> TrustedContinuityBinding:
    """What a trusted adapter retains from the typed registration result."""
    return TrustedContinuityBinding.from_registration(
        ContinuitySessionBinding.from_wire(result["session"])
    )


def _with_binding(
    session: AuthenticatedSession,
    binding: TrustedContinuityBinding,
) -> AuthenticatedSession:
    return dataclasses.replace(session, continuity_binding=binding)


def _direct_binding_key(holder: Any, principal_id: str) -> tuple[str, str]:
    return str(holder.path), principal_id


def _handlers(
    holder: Any, entry: Any, *, clock: Any | None = None
) -> ContinuityHandlers:
    return ContinuityHandlers(
        service=SimpleNamespace(
            connection=holder.connection, identity=holder.identity
        ),
        session=_session(entry),
        binding=s0.BINDING,
        clock=s0.clock_at() if clock is None else clock,
    )


def _context(
    holder: Any,
    entry: Any,
    operation_input: dict[str, Any],
    *,
    session: AuthenticatedSession | None = None,
    stated_version: str | None = None,
    idempotency_key: str | None = None,
) -> OperationContext:
    overrides: dict[str, Any] = {}
    if stated_version is not None:
        overrides["mutation_precondition"] = MutationPrecondition(
            record_version=stated_version
        )
    if idempotency_key is not None:
        overrides["idempotency_key"] = idempotency_key
    envelope = s0.envelope_for(entry, operation_input=operation_input, **overrides)
    authorized = authorize_application_request(
        envelope,
        session=_session(entry) if session is None else session,
        binding=s0.BINDING,
        supported_capabilities=s0.SUPPORTED,
    )
    return OperationContext(
        request=envelope,
        principal=authorized.principal_id,
        workspace_id=authorized.workspace_id or WORKSPACE_ID,
        granted_operations=frozenset({entry.name}),
        authorization=authorized,
    )


def _register_input(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "engineering.1",
        "checkout_hint": "/home/dev/app",
        "host_session_ref": "host-conv-77",
    }
    base.update(overrides)
    return base


def _append_input(session_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": session_id,
        "payload": {
            "objective": "Investigate the session-restoration failure",
            "checkpoint_kind": "periodic",
            "observations": [
                {"statement": "Retries did not change the failure.", "support": "claimed"}
            ],
            "unresolved_work": ["Why does restore fail after credential validation?"],
            "next_actions": ["Inspect the session-invalidation path"],
        },
    }
    base.update(overrides)
    return base


def _close_input(session_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": session_id,
        "expected_sequence": 1,
        "final_checkpoint": {
            "objective": "Wrap up the investigation",
            "checkpoint_kind": "session_close",
            "unresolved_work": ["Root cause still unconfirmed"],
        },
    }
    base.update(overrides)
    return base


def _call(
    holder: Any,
    entry: Any,
    handler_name: str,
    operation_input: dict[str, Any],
    *,
    session: AuthenticatedSession | None = None,
    stated_version: str | None = None,
    idempotency_key: str | None = None,
    clock: Any | None = None,
) -> Any:
    caller = _session(entry) if session is None else session
    if caller.continuity_binding is None and entry.name != REGISTER.name:
        retained = _DIRECT_BINDINGS.get(
            _direct_binding_key(holder, caller.principal_id)
        )
        if retained is not None:
            caller = _with_binding(caller, retained)
    handlers = _handlers(holder, entry, clock=clock)
    context = _context(
        holder,
        entry,
        operation_input,
        session=caller,
        stated_version=stated_version,
        idempotency_key=idempotency_key,
    )
    outcome = getattr(handlers, handler_name)(context)
    if entry.name == REGISTER.name:
        retained = _trusted_registration_binding(outcome.result)
        _DIRECT_BINDINGS[
            _direct_binding_key(holder, retained.principal_id)
        ] = retained
    return outcome


def _rendered_handoff_digest(view: dict[str, Any]) -> str:
    """Recompute the documented digest preimage from a delivered view."""
    preimage = dict(view)
    preimage.pop("content_digest")
    return content_digest(canonical_document(preimage))


def test_register_append_close_handoff_is_one_durable_vertical(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        registered = _call(holder, REGISTER, "continuity_session_register", _register_input())
        session = registered.result["session"]
        assert session["state"] == "active"
        assert session["principal_id"] == registered.result["session"]["principal_id"]
        session_id = session["session_id"]

        appended = _call(
            holder,
            APPEND,
            "continuity_checkpoint_append",
            _append_input(session_id),
            stated_version="seq-0",
        )
        receipt = appended.result["receipt"]
        assert receipt["sequence"] == 1
        assert receipt["content_digest"].startswith("sha256:")
        assert receipt["audit_reference"] == appended.audit_reference

        closed = _call(
            holder,
            CLOSE,
            "continuity_session_close",
            _close_input(session_id, expected_sequence=1),
            stated_version="seq-1",
        )
        assert closed.result["checkpoint_recorded"] is True
        assert closed.result["state"] == "closed"
        assert closed.result["receipt"]["sequence"] == 2

        handoff = _call(
            holder,
            HANDOFF,
            "continuity_handoff_read",
            {"checkpoint_id": closed.result["receipt"]["checkpoint_id"]},
        )
        view = handoff["handoff"]
        assert view["format_version"] == "continuity_handoff.v1"
        # `checkpoint_kind` is never delivered, so every handoff is redacted;
        # this one withholds nothing else.
        assert view["redacted"] is True
        assert view["omissions"] == [
            {"field": "checkpoint_kind", "reason": "working_context_redacted"}
        ]
        assert view["applicability"] == "not_evaluated"
        assert "Root cause still unconfirmed" in view["unresolved_work"]
        assert view["content_digest"] == _rendered_handoff_digest(view)
        assert view["content_digest"] != closed.result["receipt"]["content_digest"]

        tampered = dict(view)
        tampered["objective"] = "tampered after delivery"
        assert tampered["content_digest"] != _rendered_handoff_digest(tampered)

        # The durable receipt survives a reopen of the same database: the
        # acknowledged sequence and digest are still there after the service
        # stops and starts again (AC-021's storage half).
        holder.connection.close()
        reopened = m1.take_ownership(holder.path)
        try:
            row = reopened.connection.execute(
                "SELECT sequence, content_digest FROM omnivia_engineering_checkpoints "
                "WHERE workspace_id = ? AND checkpoint_id = ?",
                (WORKSPACE_ID, closed.result["receipt"]["checkpoint_id"]),
            ).fetchone()
            assert row is not None
            assert row[0] == 2
            assert row[1] == closed.result["receipt"]["content_digest"]
        finally:
            reopened.connection.close()
    finally:
        try:
            holder.connection.close()
        except sqlite3.ProgrammingError:
            pass


def test_a_replayed_register_key_returns_the_same_binding(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        first = _call(holder, REGISTER, "continuity_session_register", _register_input())
        again = _call(holder, REGISTER, "continuity_session_register", _register_input())
        assert again.result["session"]["session_id"] == first.result["session"]["session_id"]
        assert again.audit_reference == first.audit_reference
    finally:
        holder.connection.close()


def test_a_reused_key_with_a_different_payload_is_an_idempotency_conflict(
    tmp_path: Any,
) -> None:
    holder = _owned(tmp_path)
    try:
        _call(holder, REGISTER, "continuity_session_register", _register_input())
        with pytest.raises(OperationError) as conflict:
            _call(
                holder,
                REGISTER,
                "continuity_session_register",
                _register_input(checkout_hint="/home/dev/other-app"),
            )
        assert conflict.value.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
    finally:
        holder.connection.close()


def test_a_competing_successor_loses_as_a_precondition_failure(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        session_id = _call(
            holder, REGISTER, "continuity_session_register", _register_input()
        ).result["session"]["session_id"]
        _call(
            holder,
            APPEND,
            "continuity_checkpoint_append",
            _append_input(session_id),
            stated_version="seq-0",
        )

        # The session's head is sequence 1; a caller that still expects the
        # session to be empty is refused rather than silently replacing it.
        with pytest.raises(OperationError) as stale:
            _call(
                holder,
                CLOSE,
                "continuity_session_close",
                _close_input(session_id, expected_sequence=0),
                stated_version="seq-0",
            )
        assert stale.value.code == ERROR_CODE_MUTATION_PRECONDITION_FAILED

        # The same refusal arrives through append's own expected predecessor.
        with pytest.raises(OperationError) as racing:
            _call(
                holder,
                APPEND,
                "continuity_checkpoint_append",
                _append_input(session_id, expected_parent_sequence=0),
                stated_version="seq-0",
                idempotency_key="idem-append-competing",
            )
        assert racing.value.code == ERROR_CODE_MUTATION_PRECONDITION_FAILED
    finally:
        holder.connection.close()


def test_an_append_to_a_closed_session_is_a_conflict(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        session_id = _call(
            holder, REGISTER, "continuity_session_register", _register_input()
        ).result["session"]["session_id"]
        closed = _call(
            holder,
            CLOSE,
            "continuity_session_close",
            {
                "session_id": session_id,
                "expected_sequence": 0,
            },
            stated_version="seq-0",
        )
        assert closed.result["checkpoint_recorded"] is False
        with pytest.raises(OperationError) as closed_error:
            _call(
                holder,
                APPEND,
                "continuity_checkpoint_append",
                _append_input(session_id),
                stated_version="seq-0",
            )
        assert closed_error.value.code == ERROR_CODE_CONFLICT
    finally:
        holder.connection.close()


def test_failed_final_checkpoint_stage_rolls_back_and_replays_after_recovery(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staged final row, its close, and settlement are one atomic mutation."""
    holder = _owned(tmp_path)
    try:
        session_id = _call(
            holder,
            REGISTER,
            "continuity_session_register",
            _register_input(),
            idempotency_key="idem-register-close-rollback",
        ).result["session"]["session_id"]
        acknowledged = _call(
            holder,
            APPEND,
            "continuity_checkpoint_append",
            _append_input(session_id),
            stated_version="seq-0",
            idempotency_key="idem-acknowledged-before-close",
        ).result["receipt"]
        handoff_before = _call(
            holder,
            HANDOFF,
            "continuity_handoff_read",
            {"checkpoint_id": acknowledged["checkpoint_id"]},
        )["handoff"]
        settled_before = _settled_holder(holder)
        original_append = continuity_storage.append_checkpoint

        def fail_after_staging(*args: Any, **kwargs: Any) -> dict[str, Any]:
            receipt = original_append(*args, **kwargs)
            if kwargs["checkpoint_kind"] == "session_close":
                raise RuntimeError("injected final checkpoint staging failure")
            return receipt

        monkeypatch.setattr(continuity_storage, "append_checkpoint", fail_after_staging)
        close_request = _close_input(session_id, expected_sequence=1)
        with pytest.raises(RuntimeError, match="injected final checkpoint staging failure"):
            _call(
                holder,
                CLOSE,
                "continuity_session_close",
                close_request,
                stated_version="seq-1",
                idempotency_key="idem-close-after-staging",
            )

        assert _settled_holder(holder) == settled_before
        assert holder.connection.execute(
            "SELECT state, last_checkpoint_sequence, last_checkpoint_id "
            "FROM omnivia_engineering_sessions WHERE workspace_id = ? AND session_id = ?",
            (WORKSPACE_ID, session_id),
        ).fetchone() == ("active", 1, acknowledged["checkpoint_id"])
        assert holder.connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_checkpoints "
            "WHERE workspace_id = ? AND session_id = ?",
            (WORKSPACE_ID, session_id),
        ).fetchone() == (1,)
        assert _call(
            holder,
            HANDOFF,
            "continuity_handoff_read",
            {"checkpoint_id": acknowledged["checkpoint_id"]},
        )["handoff"] == handoff_before

        # Because the failed mutation left no claim, the owning runtime can make a
        # deliberate recovery attempt.  Once committed, an identical retry replays
        # the same close receipt instead of appending a third checkpoint.
        monkeypatch.setattr(continuity_storage, "append_checkpoint", original_append)
        recovered = _call(
            holder,
            CLOSE,
            "continuity_session_close",
            close_request,
            stated_version="seq-1",
            idempotency_key="idem-close-after-staging",
        )
        replayed = _call(
            holder,
            CLOSE,
            "continuity_session_close",
            close_request,
            stated_version="seq-1",
            idempotency_key="idem-close-after-staging",
        )
        assert replayed.result == recovered.result
        assert replayed.audit_reference == recovered.audit_reference
        assert holder.connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_checkpoints "
            "WHERE workspace_id = ? AND session_id = ?",
            (WORKSPACE_ID, session_id),
        ).fetchone() == (2,)
    finally:
        holder.connection.close()


def test_an_oversized_payload_is_refused_whole(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        session_id = _call(
            holder, REGISTER, "continuity_session_register", _register_input()
        ).result["session"]["session_id"]
        bloated = _append_input(
            session_id,
            payload={
                "objective": "x" * 2000,
                "checkpoint_kind": "periodic",
                "unresolved_work": ["y" * 2000] * 200,
            },
        )
        with pytest.raises(OperationError) as too_big:
            _call(
                holder,
                APPEND,
                "continuity_checkpoint_append",
                bloated,
                stated_version="seq-0",
            )
        assert too_big.value.code == ERROR_CODE_SIZE_LIMIT_EXCEEDED
        count = holder.connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_checkpoints WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()[0]
        assert count == 0
    finally:
        holder.connection.close()


def test_an_unknown_checkpoint_is_not_found_and_unguarded_writes_refuse(
    tmp_path: Any,
) -> None:
    holder = _owned(tmp_path)
    try:
        _call(holder, REGISTER, "continuity_session_register", _register_input())
        with pytest.raises(OperationError) as missing:
            _call(
                holder,
                HANDOFF,
                "continuity_handoff_read",
                {"checkpoint_id": "ck-nowhere"},
            )
        assert missing.value.code == ERROR_CODE_NOT_FOUND

        with pytest.raises(sqlite3.DatabaseError):
            holder.connection.execute(
                "INSERT INTO omnivia_engineering_sessions "
                "(workspace_id, session_id, principal_id, state, binding_generation, "
                "lease_expires_at_us, registered_at_us, audit_ref) "
                "VALUES (?, 'esess-forced', 'user-42', 'active', 1, 1, 1, 'audit-x')",
                (WORKSPACE_ID,),
            )
    finally:
        holder.connection.close()


# --- principal isolation, through the production application surface ---------------

#: Two authenticated contributors in one workspace. There is no sharing grant.
OWNER = engineering_family_session(
    principal_id=sc.PRINCIPAL,
    installation_id=s0.INSTALLATION_ID,
    workspace_id=sc.WORKSPACE_ID,
)
OTHER = engineering_family_session(
    principal_id="intruder",
    installation_id=s0.INSTALLATION_ID,
    workspace_id=sc.WORKSPACE_ID,
)

#: Everything a continuity mutation settles; a refusal must leave all of it alone.
_SETTLED_TABLES = (
    "omnivia_engineering_sessions",
    "omnivia_engineering_session_lifecycle",
    "omnivia_engineering_session_authority",
    "omnivia_engineering_checkpoints",
    "omnivia_idempotency_claims",
    "omnivia_application_audit_events",
    "omnivia_mutation_executions",
)


@pytest.fixture
def workspace(tmp_path: Any) -> Any:
    opened = sc.Workspace(tmp_path)
    yield opened
    opened.holder.connection.close()


def _stated(version: str) -> dict[str, Any]:
    return {"mutation_precondition": MutationPrecondition(record_version=version)}


def _production_envelope(
    sequence: int,
    operation: str,
    payload: dict[str, Any],
    *,
    key: str | None = None,
    version: str | None = None,
) -> Any:
    entry = get_operation_metadata(operation)
    metadata: dict[str, Any] = {
        "request_id": f"req-associated-{sequence}",
        "correlation_id": f"cor-associated-{sequence}",
        "trace_id": f"trc-associated-{sequence}",
        "purpose": ENGINEERING_FAMILY_PURPOSES[operation],
        "workspace_id": sc.WORKSPACE_ID,
    }
    if key is not None:
        metadata["idempotency_key"] = key
    if version is not None:
        metadata["mutation_precondition"] = MutationPrecondition(
            record_version=version
        )
    return s0.envelope_for(entry, operation_input=payload, **metadata)


def _associated_session(name: str) -> AuthenticatedSession:
    return dataclasses.replace(
        OWNER,
        continuity_association=TrustedContinuityAssociation(
            association_id=name,
            principal_id=OWNER.principal_id,
            workspace_id=sc.WORKSPACE_ID,
            provenance=ContinuityAssociationProvenance.AUTHENTICATED_HTTP_CONNECTION,
        ),
    )


def _settled(workspace: Any) -> list[list[Any]]:
    connection = workspace.holder.connection
    return [
        connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
        for table in _SETTLED_TABLES
    ]


def _settle_internal_lifecycle(
    workspace: Any,
    *,
    audit_ref: str,
    settled_at_us: int,
    mutate: Any,
) -> Any:
    """Run one service-owned lifecycle transition under the real writer fence."""
    holder = workspace.holder
    settlement = SimpleNamespace(
        audit_ref=audit_ref,
        settled_at_us=settled_at_us,
    )
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=sc.WORKSPACE_ID,
        fencing_generation=holder.generation,
    ) as fenced:
        fenced.execute(
            "INSERT INTO omnivia_application_audit_events "
            "(audit_ref, workspace_id, principal_id, operation, purpose, request_id, "
            "correlation_id, trace_id, granted_authority_json, outcome_class, "
            "error_code, recorded_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', "
            "'succeeded', NULL, ?)",
            (
                audit_ref,
                sc.WORKSPACE_ID,
                OWNER.principal_id,
                "continuity.lifecycle.internal",
                "continuity_session",
                f"req-{audit_ref}",
                f"cor-{audit_ref}",
                f"trc-{audit_ref}",
                settled_at_us,
            ),
        )
        return mutate(fenced, settlement)


def test_production_local_association_survives_processes_and_restart(
    workspace: Any,
) -> None:
    """The real local dispatch path retains no caller object between calls."""
    registered = workspace.surface.dispatch(
        _production_envelope(
            1,
            "continuity.session.register",
            _register_input(host_session_ref="caller-correlation-only"),
            key="idem-associated-register",
        )
    )
    assert isinstance(registered, SuccessResponseEnvelope), registered
    binding = registered.to_wire()["result"]["session"]
    assert binding["binding_generation"] == 2
    session_id = binding["session_id"]
    stored_ref = workspace.holder.connection.execute(
        "SELECT host_session_ref FROM omnivia_engineering_sessions "
        "WHERE workspace_id = ? AND session_id = ?",
        (sc.WORKSPACE_ID, session_id),
    ).fetchone()[0]
    assert stored_ref.startswith("core-association.v1:sha256:")
    assert "caller-correlation-only" not in stored_ref

    append = workspace.surface.dispatch(
        _production_envelope(
            2,
            "continuity.checkpoint.append",
            _append_input(
                session_id,
                binding_generation=999,
                principal_id="payload-substitution",
                workspace_id="payload-substitution",
            ),
            key="idem-associated-append",
            version="seq-0",
        )
    )
    assert isinstance(append, SuccessResponseEnvelope), append
    checkpoint_id = append.to_wire()["result"]["receipt"]["checkpoint_id"]

    workspace.restart()
    handoff = workspace.surface.dispatch(
        _production_envelope(
            3,
            "continuity.handoff.read",
            {"checkpoint_id": checkpoint_id},
        )
    )
    assert isinstance(handoff, SuccessResponseEnvelope), handoff
    assert handoff.to_wire()["result"]["handoff"]["checkpoint_id"] == checkpoint_id


def test_legacy_caller_host_reference_cannot_become_a_trusted_association(
    workspace: Any,
) -> None:
    associated = _associated_session("http-upgrade-association")
    assert associated.continuity_association is not None
    forged_reference = continuity_storage.associated_host_session_ref(
        associated.continuity_association.storage_key,
        None,
    )
    registered = workspace.surface.dispatch_for_session(
        _production_envelope(
            1,
            "continuity.session.register",
            _register_input(host_session_ref=forged_reference),
            key="idem-legacy-association-lookalike",
        ),
        OWNER,
    )
    assert isinstance(registered, SuccessResponseEnvelope), registered
    binding = registered.to_wire()["result"]["session"]
    assert binding["binding_generation"] == 1

    before = _settled(workspace)
    refused = workspace.surface.dispatch_for_session(
        _production_envelope(
            2,
            "continuity.checkpoint.append",
            _append_input(binding["session_id"]),
            key="idem-legacy-association-append",
            version="seq-0",
        ),
        associated,
    )
    assert isinstance(refused, ErrorResponseEnvelope), refused
    assert refused.error.code == ERROR_CODE_AUTHORIZATION_DENIED
    assert _settled(workspace) == before


def test_two_same_principal_associations_are_isolated_before_storage(
    workspace: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_session = _associated_session("http-client-a")
    second_session = _associated_session("http-client-b")
    registrations: list[dict[str, Any]] = []
    for number, session in enumerate((first_session, second_session), start=1):
        response = workspace.surface.dispatch_for_session(
            _production_envelope(
                number,
                "continuity.session.register",
                _register_input(),
                key=f"idem-associated-register-{number}",
            ),
            session,
        )
        assert isinstance(response, SuccessResponseEnvelope), response
        registrations.append(response.to_wire()["result"]["session"])
    first_id, second_id = (entry["session_id"] for entry in registrations)
    assert first_id != second_id

    before = _settled(workspace)

    def unexpected_storage(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a substituted session reached continuity storage")

    monkeypatch.setattr(continuity_storage, "read_bound_session", unexpected_storage)
    refused = workspace.surface.dispatch_for_session(
        _production_envelope(
            3,
            "continuity.checkpoint.append",
            _append_input(first_id),
            key="idem-associated-substitution",
            version="seq-0",
        ),
        second_session,
    )
    assert isinstance(refused, ErrorResponseEnvelope), refused
    assert refused.error.code == ERROR_CODE_NOT_FOUND
    assert _settled(workspace) == before


def test_same_principal_associations_cannot_replay_each_others_registration(
    workspace: Any,
) -> None:
    first_session = _associated_session("http-replay-a")
    second_session = _associated_session("http-replay-b")
    request = _production_envelope(
        1,
        "continuity.session.register",
        _register_input(),
        key="idem-shared-across-associations",
    )
    first = workspace.surface.dispatch_for_session(request, first_session)
    assert isinstance(first, SuccessResponseEnvelope), first

    refused = workspace.surface.dispatch_for_session(request, second_session)
    assert isinstance(refused, ErrorResponseEnvelope), refused
    assert refused.error.code == ERROR_CODE_IDEMPOTENCY_CONFLICT

    second = workspace.surface.dispatch_for_session(
        _production_envelope(
            2,
            "continuity.session.register",
            _register_input(),
            key="idem-second-association",
        ),
        second_session,
    )
    assert isinstance(second, SuccessResponseEnvelope), second
    assert (
        second.to_wire()["result"]["session"]["session_id"]
        != first.to_wire()["result"]["session"]["session_id"]
    )


def test_association_resolution_cannot_bypass_a_configured_binding_ceiling(
    workspace: Any,
) -> None:
    first_session = _associated_session("http-ceiling-a")
    second_session = _associated_session("http-ceiling-b")
    registered: list[dict[str, Any]] = []
    for number, session in enumerate((first_session, second_session), start=1):
        response = workspace.surface.dispatch_for_session(
            _production_envelope(
                number,
                "continuity.session.register",
                _register_input(),
                key=f"idem-ceiling-register-{number}",
            ),
            session,
        )
        assert isinstance(response, SuccessResponseEnvelope), response
        registered.append(response.to_wire()["result"])

    configured_binding = _trusted_registration_binding(registered[0])
    route = workspace.surface._routes["continuity.checkpoint.append"]
    configured_route = dataclasses.replace(
        route,
        session=dataclasses.replace(
            route.session,
            continuity_binding=configured_binding,
        ),
    )
    before = _settled(workspace)
    refused = configured_route.dispatch_for_session(
        _production_envelope(
            3,
            "continuity.checkpoint.append",
            _append_input(registered[1]["session"]["session_id"]),
            key="idem-ceiling-append",
            version="seq-0",
        ),
        second_session,
    )
    assert isinstance(refused, ErrorResponseEnvelope), refused
    assert refused.error.code == ERROR_CODE_AUTHORIZATION_DENIED
    assert _settled(workspace) == before


def test_association_resolution_cannot_bypass_a_configured_association_ceiling(
    workspace: Any,
) -> None:
    first_session = _associated_session("http-association-ceiling-a")
    second_session = _associated_session("http-association-ceiling-b")
    registered = workspace.surface.dispatch_for_session(
        _production_envelope(
            1,
            "continuity.session.register",
            _register_input(),
            key="idem-association-ceiling-register",
        ),
        second_session,
    )
    assert isinstance(registered, SuccessResponseEnvelope), registered
    session_id = registered.to_wire()["result"]["session"]["session_id"]

    route = workspace.surface._routes["continuity.checkpoint.append"]
    configured_route = dataclasses.replace(
        route,
        session=dataclasses.replace(
            route.session,
            continuity_association=first_session.continuity_association,
        ),
    )
    before = _settled(workspace)
    refused = configured_route.dispatch_for_session(
        _production_envelope(
            2,
            "continuity.checkpoint.append",
            _append_input(session_id),
            key="idem-association-ceiling-append",
            version="seq-0",
        ),
        second_session,
    )
    assert isinstance(refused, ErrorResponseEnvelope), refused
    assert refused.error.code == ERROR_CODE_AUTHORIZATION_DENIED
    assert _settled(workspace) == before


def test_http_resolver_associations_bind_two_same_principal_clients(
    workspace: Any,
) -> None:
    sessions = {
        "credential-a": _associated_session("http-credential-a"),
        "credential-b": _associated_session("http-credential-b"),
        "credential-unbound": dataclasses.replace(
            OWNER,
            continuity_binding=None,
            continuity_association=None,
        ),
    }
    router = DocumentRouter(
        probes=ProbeRouter(
            facts=lambda: ServiceFacts(
                observed_at="2026-09-28T00:00:00Z",
                health_status="pass",
                readiness_status="pass",
                discovery_status="pass",
            ),
            capabilities=tuple,
            clock=lambda: 0,
        ),
        dispatch=workspace.surface.dispatch,
    )
    listener = HttpListener(
        router=router,
        principal=OWNER.principal_id,
        resolver=lambda credential: sessions.get(credential),
        authenticated_dispatch=workspace.surface.dispatch_for_session,
        bind=HttpBind(host="127.0.0.1", port=0),
        gate=RLock(),
    )

    def post(request: Any, credential: str) -> Any:
        port = int(listener.url.rsplit(":", 1)[1])
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request(
                "POST",
                APPLICATION_PATH,
                body=canonical_json_bytes(request.to_wire()),
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Content-Type": CONTENT_TYPE,
                },
            )
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        assert response.status == 200
        return decode_response(json.loads(body))

    listener.start()
    try:
        bindings: dict[str, dict[str, Any]] = {}
        for number, credential in enumerate(
            ("credential-a", "credential-b"), start=1
        ):
            registered = post(
                _production_envelope(
                    number,
                    "continuity.session.register",
                    _register_input(),
                    key=f"idem-http-register-{number}",
                ),
                credential,
            )
            assert isinstance(registered, SuccessResponseEnvelope), registered
            bindings[credential] = registered.to_wire()["result"]["session"]

        valid = post(
            _production_envelope(
                3,
                "continuity.checkpoint.append",
                _append_input(bindings["credential-a"]["session_id"]),
                key="idem-http-valid",
                version="seq-0",
            ),
            "credential-a",
        )
        assert isinstance(valid, SuccessResponseEnvelope), valid

        substituted = post(
            _production_envelope(
                4,
                "continuity.checkpoint.append",
                _append_input(bindings["credential-a"]["session_id"]),
                key="idem-http-substituted",
                version="seq-1",
            ),
            "credential-b",
        )
        assert isinstance(substituted, ErrorResponseEnvelope), substituted
        assert substituted.error.code == ERROR_CODE_NOT_FOUND

        missing = post(
            _production_envelope(
                5,
                "continuity.checkpoint.append",
                _append_input(bindings["credential-a"]["session_id"]),
                key="idem-http-missing-binding",
                version="seq-1",
            ),
            "credential-unbound",
        )
        assert isinstance(missing, ErrorResponseEnvelope), missing
        assert missing.error.code == ERROR_CODE_AUTHORIZATION_DENIED
    finally:
        listener.stop()


def test_rebinding_one_association_fences_its_stale_session_before_replay(
    workspace: Any,
) -> None:
    session = _associated_session("http-rebinding-client")
    first_response = workspace.surface.dispatch_for_session(
        _production_envelope(
            1,
            "continuity.session.register",
            _register_input(),
            key="idem-rebinding-register-1",
        ),
        session,
    )
    assert isinstance(first_response, SuccessResponseEnvelope), first_response
    first = first_response.to_wire()["result"]["session"]
    appended = workspace.surface.dispatch_for_session(
        _production_envelope(
            2,
            "continuity.checkpoint.append",
            _append_input(first["session_id"]),
            key="idem-rebinding-append",
            version="seq-0",
        ),
        session,
    )
    assert isinstance(appended, SuccessResponseEnvelope), appended

    second_response = workspace.surface.dispatch_for_session(
        _production_envelope(
            3,
            "continuity.session.register",
            _register_input(),
            key="idem-rebinding-register-2",
        ),
        session,
    )
    assert isinstance(second_response, SuccessResponseEnvelope), second_response
    second = second_response.to_wire()["result"]["session"]
    assert [first["binding_generation"], second["binding_generation"]] == [2, 3]

    stale = workspace.surface.dispatch_for_session(
        _production_envelope(
            4,
            "continuity.checkpoint.append",
            _append_input(first["session_id"]),
            key="idem-rebinding-append",
            version="seq-0",
        ),
        session,
    )
    assert isinstance(stale, ErrorResponseEnvelope), stale
    assert stale.error.code == ERROR_CODE_NOT_FOUND

    current = workspace.surface.dispatch_for_session(
        _production_envelope(
            5,
            "continuity.checkpoint.append",
            _append_input(second["session_id"]),
            key="idem-rebinding-current",
            version="seq-0",
        ),
        session,
    )
    assert isinstance(current, SuccessResponseEnvelope), current


def test_association_reconnect_records_history_and_one_current_authority(
    workspace: Any,
) -> None:
    association = _associated_session("lifecycle-reconnect")
    first_response = workspace.surface.dispatch_for_session(
        _production_envelope(
            101,
            "continuity.session.register",
            _register_input(),
            key="idem-lifecycle-reconnect-1",
        ),
        association,
    )
    second_response = workspace.surface.dispatch_for_session(
        _production_envelope(
            102,
            "continuity.session.register",
            _register_input(),
            key="idem-lifecycle-reconnect-2",
        ),
        association,
    )
    assert isinstance(first_response, SuccessResponseEnvelope), first_response
    assert isinstance(second_response, SuccessResponseEnvelope), second_response
    first = first_response.to_wire()["result"]["session"]
    second = second_response.to_wire()["result"]["session"]
    assert first["session_id"] != second["session_id"]
    assert [first["binding_generation"], second["binding_generation"]] == [2, 3]

    connection = workspace.holder.connection
    assert connection.execute(
        "SELECT current_session_id, binding_generation, state "
        "FROM omnivia_engineering_session_authority"
    ).fetchall() == [(second["session_id"], 3, "active")]
    assert connection.execute(
        "SELECT event_type, binding_generation, state "
        "FROM omnivia_engineering_session_lifecycle "
        "WHERE session_id = ? ORDER BY event_sequence",
        (first["session_id"],),
    ).fetchall() == [("registered", 2, "active"), ("superseded", 2, "revoked")]
    assert connection.execute(
        "SELECT event_type, binding_generation, state, prior_session_id "
        "FROM omnivia_engineering_session_lifecycle "
        "WHERE session_id = ? ORDER BY event_sequence",
        (second["session_id"],),
    ).fetchall() == [("registered", 3, "active", first["session_id"])]

    with pytest.raises(sqlite3.DatabaseError, match="rotates by exactly one generation"), (
        fenced_transaction(
            connection,
            workspace.holder.identity,
            workspace_id=sc.WORKSPACE_ID,
            fencing_generation=workspace.holder.generation,
        )
    ) as fenced:
        fenced.execute(
            "UPDATE omnivia_engineering_session_authority "
            "SET current_session_id = ?, binding_generation = ?, state = 'revoked' "
            "WHERE workspace_id = ? AND principal_id = ? AND association_key = ?",
            (
                first["session_id"],
                first["binding_generation"],
                sc.WORKSPACE_ID,
                OWNER.principal_id,
                association.continuity_association.storage_key,
            ),
        )
    assert connection.execute(
        "SELECT current_session_id, binding_generation, state "
        "FROM omnivia_engineering_session_authority"
    ).fetchall() == [(second["session_id"], 3, "active")]

    with pytest.raises(continuity_storage.SessionBindingMismatch):
        continuity_storage.read_bound_session(
            connection,
            workspace_id=sc.WORKSPACE_ID,
            session_id=first["session_id"],
            principal_id=OWNER.principal_id,
            binding_generation=2,
        )


def test_service_owned_renewal_rotates_exactly_one_generation_and_fences_old_binding(
    workspace: Any,
) -> None:
    association = _associated_session("lifecycle-renew")
    response = workspace.surface.dispatch_for_session(
        _production_envelope(
            103,
            "continuity.session.register",
            _register_input(),
            key="idem-lifecycle-renew-register",
        ),
        association,
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    registered = response.to_wire()["result"]["session"]
    key = association.continuity_association.storage_key
    connection = workspace.holder.connection
    old_lease = int(
        connection.execute(
            "SELECT lease_expires_at_us FROM omnivia_engineering_sessions "
            "WHERE session_id = ?",
            (registered["session_id"],),
        ).fetchone()[0]
    )

    for generation, expected_error in (
        (4, "advance by exactly one"),
        (3, "requires matching append-only history"),
    ):
        with pytest.raises(sqlite3.DatabaseError, match=expected_error), (
            fenced_transaction(
                connection,
                workspace.holder.identity,
                workspace_id=sc.WORKSPACE_ID,
                fencing_generation=workspace.holder.generation,
            )
        ) as fenced:
            fenced.execute(
                "UPDATE omnivia_engineering_sessions "
                "SET binding_generation = ?, lease_expires_at_us = ? "
                "WHERE workspace_id = ? AND session_id = ?",
                (
                    generation,
                    old_lease + 1,
                    sc.WORKSPACE_ID,
                    registered["session_id"],
                ),
            )
    assert connection.execute(
        "SELECT binding_generation, lease_expires_at_us "
        "FROM omnivia_engineering_sessions WHERE session_id = ?",
        (registered["session_id"],),
    ).fetchone() == (2, old_lease)

    renewed = _settle_internal_lifecycle(
        workspace,
        audit_ref="audit-lifecycle-renew",
        settled_at_us=old_lease - 1,
        mutate=lambda fenced, settlement: continuity_storage.renew_associated_session(
            fenced,
            settlement,
            workspace_id=sc.WORKSPACE_ID,
            principal_id=OWNER.principal_id,
            association_key=key,
            session_id=registered["session_id"],
            binding_generation=2,
        ),
    )
    assert renewed["binding_generation"] == 3
    assert renewed["lease_expires_at_us"] > old_lease
    assert connection.execute(
        "SELECT binding_generation, state, lease_expires_at_us "
        "FROM omnivia_engineering_session_authority"
    ).fetchone() == (3, "active", renewed["lease_expires_at_us"])
    assert connection.execute(
        "SELECT event_type, binding_generation FROM "
        "omnivia_engineering_session_lifecycle WHERE session_id = ? "
        "ORDER BY event_sequence",
        (registered["session_id"],),
    ).fetchall() == [("registered", 2), ("renewed", 3)]

    with pytest.raises(continuity_storage.SessionBindingMismatch):
        continuity_storage.read_bound_session(
            connection,
            workspace_id=sc.WORKSPACE_ID,
            session_id=registered["session_id"],
            principal_id=OWNER.principal_id,
            binding_generation=2,
        )


def test_revoke_is_idempotent_and_blocks_delayed_append_close_but_preserves_handoff(
    workspace: Any,
) -> None:
    association = _associated_session("lifecycle-revoke")
    registered_response = workspace.surface.dispatch_for_session(
        _production_envelope(
            104,
            "continuity.session.register",
            _register_input(),
            key="idem-lifecycle-revoke-register",
        ),
        association,
    )
    assert isinstance(registered_response, SuccessResponseEnvelope), registered_response
    registered = registered_response.to_wire()["result"]["session"]
    binding = TrustedContinuityBinding.from_registration(
        ContinuitySessionBinding.from_wire(registered)
    )
    bound = _with_binding(association, binding)
    append_request = _production_envelope(
        105,
        "continuity.checkpoint.append",
        _append_input(registered["session_id"]),
        key="idem-lifecycle-revoke-append",
        version="seq-0",
    )
    appended = workspace.surface.dispatch_for_session(append_request, bound)
    assert isinstance(appended, SuccessResponseEnvelope), appended
    checkpoint_id = appended.to_wire()["result"]["receipt"]["checkpoint_id"]
    connection = workspace.holder.connection
    lease = int(
        connection.execute(
            "SELECT lease_expires_at_us FROM omnivia_engineering_sessions "
            "WHERE session_id = ?",
            (registered["session_id"],),
        ).fetchone()[0]
    )
    key = association.continuity_association.storage_key

    def revoke_twice(fenced: Any, settlement: Any) -> Any:
        first = continuity_storage.revoke_associated_session(
            fenced,
            settlement,
            workspace_id=sc.WORKSPACE_ID,
            principal_id=OWNER.principal_id,
            association_key=key,
            session_id=registered["session_id"],
            binding_generation=2,
        )
        second = continuity_storage.revoke_associated_session(
            fenced,
            settlement,
            workspace_id=sc.WORKSPACE_ID,
            principal_id=OWNER.principal_id,
            association_key=key,
            session_id=registered["session_id"],
            binding_generation=2,
        )
        assert first == second
        return second

    _settle_internal_lifecycle(
        workspace,
        audit_ref="audit-lifecycle-revoke",
        settled_at_us=lease - 1,
        mutate=revoke_twice,
    )
    assert connection.execute(
        "SELECT event_type FROM omnivia_engineering_session_lifecycle "
        "WHERE session_id = ? ORDER BY event_sequence",
        (registered["session_id"],),
    ).fetchall() == [("registered",), ("revoked",)]
    before = _settled(workspace)

    delayed_append = workspace.surface.dispatch_for_session(
        _production_envelope(
            106,
            "continuity.checkpoint.append",
            _append_input(registered["session_id"]),
            key="idem-lifecycle-delayed-append",
            version="seq-1",
        ),
        bound,
    )
    delayed_close = workspace.surface.dispatch_for_session(
        _production_envelope(
            107,
            "continuity.session.close",
            _close_input(registered["session_id"], expected_sequence=1),
            key="idem-lifecycle-delayed-close",
            version="seq-1",
        ),
        bound,
    )
    assert isinstance(delayed_append, ErrorResponseEnvelope), delayed_append
    assert isinstance(delayed_close, ErrorResponseEnvelope), delayed_close
    assert delayed_append.error.code == ERROR_CODE_CONFLICT
    assert delayed_close.error.code == ERROR_CODE_CONFLICT
    assert _settled(workspace) == before

    replay = workspace.surface.dispatch_for_session(append_request, bound)
    assert isinstance(replay, ErrorResponseEnvelope), replay
    assert replay.error.code == ERROR_CODE_CONFLICT
    assert _settled(workspace) == before

    handoff = workspace.surface.dispatch_for_session(
        _production_envelope(
            108,
            "continuity.handoff.read",
            {"checkpoint_id": checkpoint_id},
        ),
        bound,
    )
    assert isinstance(handoff, SuccessResponseEnvelope), handoff


def test_unassociated_prefix_mimic_remains_generation_one_and_can_close(
    workspace: Any,
) -> None:
    associated = _associated_session("lifecycle-prefix-mimic")
    forged_host_ref = continuity_storage.associated_host_session_ref(
        associated.continuity_association.storage_key,
        None,
    )
    registered_response = workspace.surface.dispatch_for_session(
        _production_envelope(
            114,
            "continuity.session.register",
            _register_input(host_session_ref=forged_host_ref),
            key="idem-lifecycle-prefix-mimic-register",
        ),
        OWNER,
    )
    assert isinstance(registered_response, SuccessResponseEnvelope), registered_response
    registered = registered_response.to_wire()["result"]["session"]
    assert registered["binding_generation"] == 1
    bound = _with_binding(
        OWNER,
        TrustedContinuityBinding.from_registration(
            ContinuitySessionBinding.from_wire(registered)
        ),
    )

    appended = workspace.surface.dispatch_for_session(
        _production_envelope(
            115,
            "continuity.checkpoint.append",
            _append_input(registered["session_id"]),
            key="idem-lifecycle-prefix-mimic-append",
            version="seq-0",
        ),
        bound,
    )
    assert isinstance(appended, SuccessResponseEnvelope), appended
    closed = workspace.surface.dispatch_for_session(
        _production_envelope(
            116,
            "continuity.session.close",
            _close_input(registered["session_id"], expected_sequence=1),
            key="idem-lifecycle-prefix-mimic-close",
            version="seq-1",
        ),
        bound,
    )
    assert isinstance(closed, SuccessResponseEnvelope), closed
    assert workspace.holder.connection.execute(
        "SELECT event_type, association_key FROM "
        "omnivia_engineering_session_lifecycle WHERE session_id = ? "
        "ORDER BY event_sequence",
        (registered["session_id"],),
    ).fetchall() == [("registered", None), ("closed", None)]


def test_delayed_registration_cannot_supersede_newer_settled_authority(
    workspace: Any,
) -> None:
    association = _associated_session("lifecycle-registration-race")
    first_response = workspace.surface.dispatch_for_session(
        _production_envelope(
            117,
            "continuity.session.register",
            _register_input(),
            key="idem-lifecycle-registration-race-first",
        ),
        association,
    )
    assert isinstance(first_response, SuccessResponseEnvelope), first_response
    first = first_response.to_wire()["result"]["session"]
    key = association.continuity_association.storage_key
    stale_precondition = (
        continuity_storage.read_association_registration_precondition(
            workspace.holder.connection,
            workspace_id=sc.WORKSPACE_ID,
            principal_id=OWNER.principal_id,
            association_key=key,
        )
    )
    registered_at_us = int(
        workspace.holder.connection.execute(
            "SELECT registered_at_us FROM omnivia_engineering_sessions "
            "WHERE session_id = ?",
            (first["session_id"],),
        ).fetchone()[0]
    )

    newer_generation = _settle_internal_lifecycle(
        workspace,
        audit_ref="audit-lifecycle-registration-race-newer",
        settled_at_us=registered_at_us + 1,
        mutate=lambda fenced, settlement: continuity_storage.register_session(
            fenced,
            settlement,
            workspace_id=sc.WORKSPACE_ID,
            session_id="esess-registration-race-newer",
            principal_id=OWNER.principal_id,
            association_key=key,
            association_precondition=stale_precondition,
            host_session_ref=continuity_storage.associated_host_session_ref(
                key, "newer"
            ),
            checkout_hint=None,
            repository_target=None,
            registered_at_us=registered_at_us + 1,
        ),
    )
    assert newer_generation == 3
    before_stale = _settled(workspace)

    with pytest.raises(
        continuity_storage.SessionBindingMismatch,
        match="advanced before registration settled",
    ):
        _settle_internal_lifecycle(
            workspace,
            audit_ref="audit-lifecycle-registration-race-stale",
            settled_at_us=registered_at_us + 2,
            mutate=lambda fenced, settlement: continuity_storage.register_session(
                fenced,
                settlement,
                workspace_id=sc.WORKSPACE_ID,
                session_id="esess-registration-race-stale",
                principal_id=OWNER.principal_id,
                association_key=key,
                association_precondition=stale_precondition,
                host_session_ref=continuity_storage.associated_host_session_ref(
                    key, "stale"
                ),
                checkout_hint=None,
                repository_target=None,
                registered_at_us=registered_at_us + 2,
            ),
        )
    assert _settled(workspace) == before_stale
    assert workspace.holder.connection.execute(
        "SELECT current_session_id, binding_generation, state "
        "FROM omnivia_engineering_session_authority"
    ).fetchall() == [("esess-registration-race-newer", 3, "active")]


def test_long_renewal_can_reconnect_without_shortening_the_lease(
    workspace: Any,
) -> None:
    association = _associated_session("lifecycle-long-renew")
    first_response = workspace.surface.dispatch_for_session(
        _production_envelope(
            118,
            "continuity.session.register",
            _register_input(),
            key="idem-lifecycle-long-renew-register",
        ),
        association,
    )
    assert isinstance(first_response, SuccessResponseEnvelope), first_response
    first = first_response.to_wire()["result"]["session"]
    connection = workspace.holder.connection
    old_lease = int(
        connection.execute(
            "SELECT lease_expires_at_us FROM omnivia_engineering_sessions "
            "WHERE session_id = ?",
            (first["session_id"],),
        ).fetchone()[0]
    )
    key = association.continuity_association.storage_key
    renewed = _settle_internal_lifecycle(
        workspace,
        audit_ref="audit-lifecycle-long-renew",
        settled_at_us=old_lease - 1,
        mutate=lambda fenced, settlement: continuity_storage.renew_associated_session(
            fenced,
            settlement,
            workspace_id=sc.WORKSPACE_ID,
            principal_id=OWNER.principal_id,
            association_key=key,
            session_id=first["session_id"],
            binding_generation=2,
            lease_seconds=7 * 24 * 60 * 60,
        ),
    )

    replacement_response = workspace.surface.dispatch_for_session(
        _production_envelope(
            119,
            "continuity.session.register",
            _register_input(),
            key="idem-lifecycle-long-renew-reconnect",
        ),
        association,
    )
    assert isinstance(replacement_response, SuccessResponseEnvelope), replacement_response
    replacement = replacement_response.to_wire()["result"]["session"]
    assert replacement["binding_generation"] == 4
    replacement_lease = int(
        connection.execute(
            "SELECT lease_expires_at_us FROM omnivia_engineering_sessions "
            "WHERE session_id = ?",
            (replacement["session_id"],),
        ).fetchone()[0]
    )
    assert replacement_lease > renewed["lease_expires_at_us"]


def test_guarded_direct_session_insert_always_appends_registration_history(
    workspace: Any,
) -> None:
    settled_at_us = 1_900_000_000_000_000

    def insert(fenced: Any, settlement: Any) -> None:
        fenced.execute(
            "INSERT INTO omnivia_engineering_sessions "
            "(workspace_id, session_id, principal_id, state, binding_generation, "
            "lease_expires_at_us, host_session_ref, checkout_hint, "
            "repository_target_json, registered_at_us, closed_at_us, "
            "last_checkpoint_sequence, last_checkpoint_id, audit_ref) "
            "VALUES (?, 'esess-direct-history', ?, 'active', 1, ?, 'opaque', "
            "NULL, NULL, ?, NULL, NULL, NULL, ?)",
            (
                sc.WORKSPACE_ID,
                OWNER.principal_id,
                settled_at_us + 1,
                settled_at_us,
                settlement.audit_ref,
            ),
        )

    _settle_internal_lifecycle(
        workspace,
        audit_ref="audit-lifecycle-direct-history",
        settled_at_us=settled_at_us,
        mutate=insert,
    )
    assert workspace.holder.connection.execute(
        "SELECT event_sequence, event_type, association_key, binding_generation, "
        "state, settled_at_us, audit_ref FROM "
        "omnivia_engineering_session_lifecycle WHERE session_id = ?",
        ("esess-direct-history",),
    ).fetchall() == [
        (
            1,
            "registered",
            None,
            1,
            "active",
            settled_at_us,
            "audit-lifecycle-direct-history",
        )
    ]


def test_workspace_generation_change_rolls_back_a_delayed_checkpoint(
    workspace: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    association = _associated_session("lifecycle-workspace-fence")
    registered_response = workspace.surface.dispatch_for_session(
        _production_envelope(
            112,
            "continuity.session.register",
            _register_input(),
            key="idem-lifecycle-workspace-register",
        ),
        association,
    )
    assert isinstance(registered_response, SuccessResponseEnvelope), registered_response
    registered = registered_response.to_wire()["result"]["session"]
    bound = _with_binding(
        association,
        TrustedContinuityBinding.from_registration(
            ContinuitySessionBinding.from_wire(registered)
        ),
    )
    before = _settled(workspace)
    original = continuity_storage.append_checkpoint

    def append_then_lose_workspace_authority(*args: Any, **kwargs: Any) -> Any:
        written = original(*args, **kwargs)
        args[0].execute(
            "UPDATE omnivia_workspace_state SET fencing_generation = ? "
            "WHERE singleton = 1",
            (workspace.holder.generation + 1,),
        )
        return written

    monkeypatch.setattr(
        continuity_storage,
        "append_checkpoint",
        append_then_lose_workspace_authority,
    )
    with pytest.raises((StaleGeneration, sqlite3.DatabaseError)):
        workspace.surface.dispatch_for_session(
            _production_envelope(
                113,
                "continuity.checkpoint.append",
                _append_input(registered["session_id"]),
                key="idem-lifecycle-workspace-delayed-append",
                version="seq-0",
            ),
            bound,
        )

    assert _settled(workspace) == before
    assert workspace.holder.connection.execute(
        "SELECT fencing_generation FROM omnivia_workspace_state WHERE singleton = 1"
    ).fetchone() == (workspace.holder.generation,)
    assert workspace.holder.connection.in_transaction is False


def test_expiry_boundary_is_terminal_but_a_new_session_may_reconnect(
    workspace: Any,
) -> None:
    association = _associated_session("lifecycle-expire")
    first_response = workspace.surface.dispatch_for_session(
        _production_envelope(
            109,
            "continuity.session.register",
            _register_input(),
            key="idem-lifecycle-expire-register",
        ),
        association,
    )
    assert isinstance(first_response, SuccessResponseEnvelope), first_response
    first = first_response.to_wire()["result"]["session"]
    connection = workspace.holder.connection
    lease = int(
        connection.execute(
            "SELECT lease_expires_at_us FROM omnivia_engineering_sessions "
            "WHERE session_id = ?",
            (first["session_id"],),
        ).fetchone()[0]
    )
    key = association.continuity_association.storage_key
    with pytest.raises(continuity_storage.SessionNotActive, match="expired"):
        _settle_internal_lifecycle(
            workspace,
            audit_ref="audit-lifecycle-boundary-renew",
            settled_at_us=lease,
            mutate=lambda fenced, settlement: continuity_storage.renew_associated_session(
                fenced,
                settlement,
                workspace_id=sc.WORKSPACE_ID,
                principal_id=OWNER.principal_id,
                association_key=key,
                session_id=first["session_id"],
                binding_generation=2,
            ),
        )
    assert connection.execute(
        "SELECT COUNT(*) FROM omnivia_application_audit_events WHERE audit_ref = ?",
        ("audit-lifecycle-boundary-renew",),
    ).fetchone() == (0,)
    assert connection.execute(
        "SELECT event_type FROM omnivia_engineering_session_lifecycle "
        "WHERE session_id = ? ORDER BY event_sequence",
        (first["session_id"],),
    ).fetchall() == [("registered",)]

    _settle_internal_lifecycle(
        workspace,
        audit_ref="audit-lifecycle-expire",
        settled_at_us=lease,
        mutate=lambda fenced, settlement: continuity_storage.expire_associated_session(
            fenced,
            settlement,
            workspace_id=sc.WORKSPACE_ID,
            principal_id=OWNER.principal_id,
            association_key=key,
            session_id=first["session_id"],
            binding_generation=2,
        ),
    )

    with pytest.raises(continuity_storage.SessionNotActive):
        _settle_internal_lifecycle(
            workspace,
            audit_ref="audit-lifecycle-expired-renew",
            settled_at_us=lease + 1,
            mutate=lambda fenced, settlement: continuity_storage.renew_associated_session(
                fenced,
                settlement,
                workspace_id=sc.WORKSPACE_ID,
                principal_id=OWNER.principal_id,
                association_key=key,
                session_id=first["session_id"],
                binding_generation=2,
            ),
        )

    with pytest.raises(sqlite3.DatabaseError), fenced_transaction(
        workspace.holder.connection,
        workspace.holder.identity,
        workspace_id=sc.WORKSPACE_ID,
        fencing_generation=workspace.holder.generation,
    ) as fenced:
        fenced.execute(
            "UPDATE omnivia_engineering_sessions SET state = 'active' "
            "WHERE workspace_id = ? AND session_id = ?",
            (sc.WORKSPACE_ID, first["session_id"]),
        )

    second_response = workspace.surface.dispatch_for_session(
        _production_envelope(
            110,
            "continuity.session.register",
            _register_input(),
            key="idem-lifecycle-expire-reconnect",
        ),
        association,
    )
    assert isinstance(second_response, SuccessResponseEnvelope), second_response
    second = second_response.to_wire()["result"]["session"]
    assert second["session_id"] != first["session_id"]
    assert second["binding_generation"] == 3
    assert connection.execute(
        "SELECT state FROM omnivia_engineering_sessions WHERE session_id = ?",
        (first["session_id"],),
    ).fetchone() == ("expired",)


def test_lifecycle_history_and_authority_are_protected_from_update_and_delete(
    workspace: Any,
) -> None:
    association = _associated_session("lifecycle-immutable")
    response = workspace.surface.dispatch_for_session(
        _production_envelope(
            111,
            "continuity.session.register",
            _register_input(),
            key="idem-lifecycle-immutable-register",
        ),
        association,
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    session_id = response.to_wire()["result"]["session"]["session_id"]

    for statement in (
        (
            "UPDATE omnivia_engineering_session_lifecycle SET state = 'revoked' "
            "WHERE session_id = ?"
        ),
        "DELETE FROM omnivia_engineering_session_lifecycle WHERE session_id = ?",
        (
            "UPDATE omnivia_engineering_session_authority "
            "SET updated_at_us = updated_at_us + 1 WHERE current_session_id = ?"
        ),
        "DELETE FROM omnivia_engineering_session_authority WHERE current_session_id = ?",
    ):
        with pytest.raises(sqlite3.DatabaseError), fenced_transaction(
            workspace.holder.connection,
            workspace.holder.identity,
            workspace_id=sc.WORKSPACE_ID,
            fencing_generation=workspace.holder.generation,
        ) as fenced:
            fenced.execute(statement, (session_id,))


def test_0060_upgrade_preserves_legacy_history_and_fences_it_on_reconnect(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pre-lifecycle.sqlite"
    m1.materialise_phase0_baseline(path)
    with m2.migration_catalogue_through(59):
        m1.bootstrap_and_migrate(path, workspace_id=sc.WORKSPACE_ID)
    predecessor = m1.take_ownership(path, workspace_id=sc.WORKSPACE_ID)
    association = _associated_session("lifecycle-upgrade")
    association_key = association.continuity_association.storage_key
    registered_at_us = 1_900_000_000_000_000
    lease_expires_at_us = (
        registered_at_us + continuity_storage.SESSION_LEASE_SECONDS * 1_000_000
    )
    try:
        with fenced_transaction(
            predecessor.connection,
            predecessor.identity,
            workspace_id=sc.WORKSPACE_ID,
            fencing_generation=predecessor.generation,
        ) as fenced:
            fenced.execute(
                "INSERT INTO omnivia_application_audit_events "
                "(audit_ref, workspace_id, principal_id, operation, purpose, "
                "request_id, correlation_id, trace_id, granted_authority_json, "
                "outcome_class, error_code, recorded_at_us) "
                "VALUES ('audit-pre-0060', ?, ?, 'continuity.session.register', "
                "'continuity_session', 'req-pre-0060', 'cor-pre-0060', "
                "'trc-pre-0060', '{}', 'succeeded', NULL, ?)",
                (sc.WORKSPACE_ID, OWNER.principal_id, registered_at_us),
            )
            fenced.execute(
                "INSERT INTO omnivia_engineering_sessions "
                "(workspace_id, session_id, principal_id, state, binding_generation, "
                "lease_expires_at_us, host_session_ref, checkout_hint, "
                "repository_target_json, registered_at_us, closed_at_us, "
                "last_checkpoint_sequence, last_checkpoint_id, audit_ref) "
                "VALUES (?, 'esess-pre-0060', ?, 'active', 2, ?, ?, NULL, NULL, ?, "
                "NULL, NULL, NULL, 'audit-pre-0060')",
                (
                    sc.WORKSPACE_ID,
                    OWNER.principal_id,
                    lease_expires_at_us,
                    continuity_storage.associated_host_session_ref(
                        association_key, "legacy-host-reference"
                    ),
                    registered_at_us,
                ),
            )
    finally:
        predecessor.connection.close()

    migration = next(item for item in load_migrations() if item.version == 60)
    assert migration.name == "0060_engineering_continuity_lifecycle.sql"
    with m2.migration_catalogue_through(60):
        maintenance = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
        try:
            state = read_workspace_state(maintenance)
            assert state is not None
            applied = apply_pending_migrations(
                maintenance,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m1.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=sc.WORKSPACE_ID,
            )
            assert [item.version for item in applied] == [60]
        finally:
            maintenance.close()

    holder = m1.take_ownership(path, workspace_id=sc.WORKSPACE_ID)
    upgraded = SimpleNamespace(holder=holder)
    try:
        assert applied_migrations(holder.connection)[60] == migration.checksum
        assert holder.connection.execute(
            "SELECT event_type, association_key, binding_generation, state, audit_ref "
            "FROM omnivia_engineering_session_lifecycle WHERE session_id = ?",
            ("esess-pre-0060",),
        ).fetchall() == [("legacy_imported", None, 2, "active", "audit-pre-0060")]
        assert holder.connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_session_authority"
        ).fetchone() == (0,)
        association_precondition = (
            continuity_storage.read_association_registration_precondition(
                holder.connection,
                workspace_id=sc.WORKSPACE_ID,
                principal_id=OWNER.principal_id,
                association_key=association_key,
            )
        )

        generation = _settle_internal_lifecycle(
            upgraded,
            audit_ref="audit-post-0060",
            settled_at_us=registered_at_us + 1,
            mutate=lambda fenced, settlement: continuity_storage.register_session(
                fenced,
                settlement,
                workspace_id=sc.WORKSPACE_ID,
                session_id="esess-post-0060",
                principal_id=OWNER.principal_id,
                association_key=association_key,
                association_precondition=association_precondition,
                host_session_ref=continuity_storage.associated_host_session_ref(
                    association_key, "new-host-reference"
                ),
                checkout_hint=None,
                repository_target=None,
                registered_at_us=registered_at_us + 1,
            ),
        )
        assert generation == 3
        assert holder.connection.execute(
            "SELECT event_type, association_key, state FROM "
            "omnivia_engineering_session_lifecycle WHERE session_id = ? "
            "ORDER BY event_sequence",
            ("esess-pre-0060",),
        ).fetchall() == [
            ("legacy_imported", None, "active"),
            ("superseded", association_key, "revoked"),
        ]
        assert holder.connection.execute(
            "SELECT current_session_id, binding_generation, state FROM "
            "omnivia_engineering_session_authority"
        ).fetchall() == [("esess-post-0060", 3, "active")]
        assert integrity_check(holder.connection) == []
        assert foreign_key_check(holder.connection) == []
    finally:
        holder.connection.close()


def test_continuity_operations_require_a_server_established_binding(
    workspace: Any,
) -> None:
    registered = workspace.ok(
        "continuity.session.register",
        _register_input(),
        session=OWNER,
        key="idem-binding-required-register",
    )
    session_id = registered["session"]["session_id"]
    before = _settled(workspace)

    envelope = s0.envelope_for(
        APPEND,
        operation_input=_append_input(
            session_id,
            binding_generation=registered["session"]["binding_generation"],
            principal_id=OWNER.principal_id,
            workspace_id=sc.WORKSPACE_ID,
            continuity_binding=registered["session"],
        ),
        idempotency_key="idem-unbound-append",
        mutation_precondition=MutationPrecondition(record_version="seq-0"),
        workspace_id=sc.WORKSPACE_ID,
    )
    response = workspace.surface.dispatch_for_session(envelope, OWNER)

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == ERROR_CODE_AUTHORIZATION_DENIED
    assert response.error.message == (
        "this continuity operation requires a server-established session binding"
    )
    assert _settled(workspace) == before


def test_same_principal_cannot_substitute_another_bound_session(
    workspace: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = workspace.ok(
        "continuity.session.register",
        _register_input(host_session_ref="host-first"),
        session=OWNER,
        key="idem-first-bound-session",
    )["session"]
    first_binding = workspace.binding_for(OWNER.principal_id)
    assert (
        first_binding.provenance
        is ContinuityBindingProvenance.VALIDATED_REGISTRATION
    )
    first_session = _with_binding(OWNER, first_binding)
    first_receipt = workspace.ok(
        "continuity.checkpoint.append",
        _append_input(first["session_id"]),
        session=first_session,
        key="idem-first-bound-checkpoint",
        **_stated("seq-0"),
    )["receipt"]

    second = workspace.ok(
        "continuity.session.register",
        _register_input(host_session_ref="host-second"),
        session=OWNER,
        key="idem-second-bound-session",
    )["session"]
    second_binding = workspace.binding_for(OWNER.principal_id)
    second_session = _with_binding(OWNER, second_binding)
    assert first["session_id"] != second["session_id"]
    before = _settled(workspace)

    def unexpected_storage_read(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("payload session substitution reached storage")

    original_bound_read = continuity_storage.read_bound_session
    original_checkpoint_read = continuity_storage.read_checkpoint
    monkeypatch.setattr(
        continuity_storage, "read_bound_session", unexpected_storage_read
    )
    monkeypatch.setattr(
        continuity_storage, "read_checkpoint", unexpected_storage_read
    )
    for operation, payload, metadata in (
        (
            "continuity.checkpoint.append",
            _append_input(first["session_id"]),
            _stated("seq-1"),
        ),
        (
            "continuity.session.close",
            _close_input(first["session_id"], expected_sequence=1),
            _stated("seq-1"),
        ),
        (
            "continuity.handoff.read",
            {"session_id": first["session_id"], "sequence": 1},
            {},
        ),
    ):
        refusal = workspace.refused(
            operation,
            payload,
            session=second_session,
            **metadata,
        )
        assert refusal[0] == ERROR_CODE_NOT_FOUND
    monkeypatch.setattr(continuity_storage, "read_bound_session", original_bound_read)
    monkeypatch.setattr(continuity_storage, "read_checkpoint", original_checkpoint_read)

    # Binding is checked before the idempotency coordinator.  Reusing the
    # first session's committed request and key under the second binding cannot
    # replay its receipt.
    assert workspace.refused(
        "continuity.checkpoint.append",
        _append_input(first["session_id"]),
        session=second_session,
        key="idem-first-bound-checkpoint",
        **_stated("seq-0"),
    )[0] == ERROR_CODE_NOT_FOUND

    # A checkpoint-only selector cannot name its session in the payload.  The
    # storage query still fences it to the authenticated binding and loads no
    # payload from the other same-principal session.
    assert workspace.refused(
        "continuity.handoff.read",
        {"checkpoint_id": first_receipt["checkpoint_id"]},
        session=second_session,
    )[0] == ERROR_CODE_NOT_FOUND
    assert _settled(workspace) == before

    second_receipt = workspace.ok(
        "continuity.checkpoint.append",
        _append_input(second["session_id"]),
        session=second_session,
        key="idem-second-bound-checkpoint",
        **_stated("seq-0"),
    )["receipt"]
    assert workspace.ok(
        "continuity.handoff.read",
        {"checkpoint_id": second_receipt["checkpoint_id"]},
        session=second_session,
    )["handoff"]["checkpoint_id"] == second_receipt["checkpoint_id"]
    closed = workspace.ok(
        "continuity.session.close",
        {"session_id": second["session_id"], "expected_sequence": 1},
        session=second_session,
        key="idem-second-bound-close",
        **_stated("seq-1"),
    )
    assert closed["state"] == "closed"


def test_stale_binding_generation_cannot_append_close_or_read(
    workspace: Any,
) -> None:
    registered = workspace.ok(
        "continuity.session.register",
        _register_input(),
        session=OWNER,
        key="idem-stale-generation-register",
    )["session"]
    retained = workspace.binding_for(OWNER.principal_id)
    valid_session = _with_binding(OWNER, retained)
    append_request = _append_input(registered["session_id"])
    receipt = workspace.ok(
        "continuity.checkpoint.append",
        append_request,
        session=valid_session,
        key="idem-stale-generation-append",
        **_stated("seq-0"),
    )["receipt"]
    stale = dataclasses.replace(
        retained,
        binding_generation=retained.binding_generation + 1,
    )
    stale_session = _with_binding(OWNER, stale)
    before = _settled(workspace)

    append = workspace.refused(
        "continuity.checkpoint.append",
        append_request,
        session=stale_session,
        key="idem-stale-generation-append",
        **_stated("seq-0"),
    )
    close = workspace.refused(
        "continuity.session.close",
        {"session_id": registered["session_id"], "expected_sequence": 1},
        session=stale_session,
        key="idem-stale-generation-close",
        **_stated("seq-1"),
    )
    handoff = workspace.refused(
        "continuity.handoff.read",
        {"checkpoint_id": receipt["checkpoint_id"]},
        session=stale_session,
    )

    assert append[0] == close[0] == ERROR_CODE_CONFLICT
    assert append[1] == close[1] == (
        "this continuity request conflicts with the session's current state"
    )
    assert handoff[0] == ERROR_CODE_NOT_FOUND
    assert _settled(workspace) == before


def _register_with_lease(holder: Any, lease_delta_us: int) -> str:
    """Register so the immutable lease ends at `WALL_BASE + lease_delta_us`."""
    registered_at = s0.WALL_BASE - timedelta(
        seconds=continuity_storage.SESSION_LEASE_SECONDS
    ) + timedelta(microseconds=lease_delta_us)
    session_id = str(
        _call(
            holder,
            REGISTER,
            "continuity_session_register",
            _register_input(),
            clock=s0.clock_at(wall=registered_at),
        ).result["session"]["session_id"]
    )
    assert holder.connection.execute(
        "SELECT lease_expires_at_us FROM omnivia_engineering_sessions "
        "WHERE workspace_id = ? AND session_id = ?",
        (WORKSPACE_ID, session_id),
    ).fetchone() == (s0.WALL_BASE_US + lease_delta_us,)
    return session_id


def _settled_holder(holder: Any) -> list[list[Any]]:
    return [
        holder.connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
        for table in _SETTLED_TABLES
    ]


@pytest.mark.parametrize(
    ("lease_delta_us", "succeeds"),
    ((1, True), (0, False), (-1, False)),
    ids=("before-expiry", "at-expiry", "after-expiry"),
)
def test_checkpoint_append_uses_the_fenced_settlement_instant_for_lease_expiry(
    tmp_path: Any, lease_delta_us: int, succeeds: bool
) -> None:
    holder = _owned(tmp_path)
    try:
        session_id = _register_with_lease(holder, lease_delta_us)
        before = _settled_holder(holder)

        if succeeds:
            appended = _call(
                holder,
                APPEND,
                "continuity_checkpoint_append",
                _append_input(session_id),
                stated_version="seq-0",
            )
            assert appended.result["receipt"]["sequence"] == 1
            assert holder.connection.execute(
                "SELECT last_checkpoint_sequence FROM omnivia_engineering_sessions "
                "WHERE workspace_id = ? AND session_id = ?",
                (WORKSPACE_ID, session_id),
            ).fetchone() == (1,)
        else:
            with pytest.raises(OperationError) as expired:
                _call(
                    holder,
                    APPEND,
                    "continuity_checkpoint_append",
                    _append_input(session_id),
                    stated_version="seq-0",
                )
            assert expired.value.code == ERROR_CODE_CONFLICT
            assert _settled_holder(holder) == before
    finally:
        holder.connection.close()


@pytest.mark.parametrize("lease_delta_us", (0, -1), ids=("at-expiry", "after-expiry"))
@pytest.mark.parametrize("with_final_checkpoint", (False, True), ids=("plain", "final"))
def test_session_close_is_refused_whole_when_its_lease_has_expired(
    tmp_path: Any, lease_delta_us: int, with_final_checkpoint: bool
) -> None:
    holder = _owned(tmp_path)
    try:
        session_id = _register_with_lease(holder, lease_delta_us)
        request: dict[str, Any] = {
            "session_id": session_id,
            "expected_sequence": 0,
        }
        if with_final_checkpoint:
            request["final_checkpoint"] = {
                "objective": "This final checkpoint must roll back",
                "checkpoint_kind": "session_close",
            }
        before = _settled_holder(holder)

        with pytest.raises(OperationError) as expired:
            _call(
                holder,
                CLOSE,
                "continuity_session_close",
                request,
                stated_version="seq-0",
            )

        assert expired.value.code == ERROR_CODE_CONFLICT
        assert _settled_holder(holder) == before
        assert holder.connection.execute(
            "SELECT state, last_checkpoint_sequence, last_checkpoint_id "
            "FROM omnivia_engineering_sessions "
            "WHERE workspace_id = ? AND session_id = ?",
            (WORKSPACE_ID, session_id),
        ).fetchone() == ("active", None, None)
    finally:
        holder.connection.close()


class _SettlementCrossesLeaseClock:
    """Issue before the lease deadline, then settle after it without sleeping."""

    def __init__(self) -> None:
        self.wall_reads = 0

    def monotonic(self) -> float:
        return s0.MONOTONIC_BASE

    def wall_time(self) -> Any:
        self.wall_reads += 1
        if self.wall_reads == 1:
            return s0.WALL_BASE
        return s0.WALL_BASE + timedelta(microseconds=20)


def test_delayed_first_delivery_refuses_when_settlement_crosses_the_lease(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnivia_core_runtime.service.handlers import continuity as handlers

    holder = _owned(tmp_path)
    try:
        session_id = _register_with_lease(holder, 10)
        before = _settled_holder(holder)
        clock = _SettlementCrossesLeaseClock()
        original = handlers._session_version
        observed_before_expiry = False

        def observe_precondition(*args: Any, **kwargs: Any) -> str:
            nonlocal observed_before_expiry
            assert clock.wall_reads == 1
            observed_before_expiry = True
            return original(*args, **kwargs)

        monkeypatch.setattr(handlers, "_session_version", observe_precondition)

        with pytest.raises(OperationError) as expired:
            _call(
                holder,
                APPEND,
                "continuity_checkpoint_append",
                _append_input(session_id),
                stated_version="seq-0",
                idempotency_key="idem-delayed-first-delivery",
                clock=clock,
            )

        assert observed_before_expiry is True
        assert clock.wall_reads == 2
        assert expired.value.code == ERROR_CODE_CONFLICT
        assert _settled_holder(holder) == before
    finally:
        holder.connection.close()


def test_committed_append_replays_after_expiry_without_a_second_checkpoint(
    tmp_path: Any,
) -> None:
    holder = _owned(tmp_path)
    try:
        session_id = _register_with_lease(holder, 1)
        request = _append_input(session_id)
        first = _call(
            holder,
            APPEND,
            "continuity_checkpoint_append",
            request,
            stated_version="seq-0",
            idempotency_key="idem-append-before-expiry",
        )
        after_expiry = s0.clock_at(
            wall=s0.WALL_BASE + timedelta(microseconds=2)
        )

        replayed = _call(
            holder,
            APPEND,
            "continuity_checkpoint_append",
            request,
            stated_version="seq-0",
            idempotency_key="idem-append-before-expiry",
            clock=after_expiry,
        )

        assert replayed.result["receipt"] == first.result["receipt"]
        assert replayed.audit_reference == first.audit_reference
        assert holder.connection.execute(
            "SELECT COUNT(*), MIN(sequence), MAX(sequence) "
            "FROM omnivia_engineering_checkpoints "
            "WHERE workspace_id = ? AND session_id = ?",
            (WORKSPACE_ID, session_id),
        ).fetchone() == (1, 1, 1)

        changed = _append_input(
            session_id,
            payload={
                "objective": "A different request cannot reuse the committed key",
                "checkpoint_kind": "periodic",
            },
        )
        with pytest.raises(OperationError) as conflict:
            _call(
                holder,
                APPEND,
                "continuity_checkpoint_append",
                changed,
                stated_version="seq-0",
                idempotency_key="idem-append-before-expiry",
                clock=after_expiry,
            )
        assert conflict.value.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
    finally:
        holder.connection.close()


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(socket, "AF_UNIX"),
    reason="requires a real Unix socket",
)
def test_concurrent_successors_through_real_transport_admit_exactly_one(
    tmp_path: Any,
) -> None:
    """The socket adapter retains registration, then two client threads race."""
    workspace = sc.Workspace(tmp_path)
    try:
        router = DocumentRouter(
            probes=ProbeRouter(
                facts=lambda: ServiceFacts(
                    observed_at="2026-09-28T00:00:00Z",
                    health_status="pass",
                    readiness_status="pass",
                    discovery_status="pass",
                ),
                capabilities=tuple,
                clock=lambda: 0,
            ),
            dispatch=workspace.dispatch_connection,
        )

        def receive_exact(client: socket.socket, byte_count: int) -> bytes:
            chunks: list[bytes] = []
            while byte_count:
                chunk = client.recv(byte_count)
                if not chunk:
                    raise AssertionError("transport closed before the response completed")
                chunks.append(chunk)
                byte_count -= len(chunk)
            return b"".join(chunks)

        def exchange(address: str, request: Any) -> Any:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(10)
                client.connect(address)
                client.sendall(encode_frame(encode_request(request)))
                header = receive_exact(client, HEADER_BYTES)
                body_length = int.from_bytes(header[4:], "big")
                return decode_response(
                    decode_frame(header + receive_exact(client, body_length))
                )

        barrier = Barrier(3)

        def compete(address: str, request: Any) -> Any:
            barrier.wait(timeout=10)
            return exchange(address, request)

        with tempfile.TemporaryDirectory(prefix="ov-continuity-", dir="/tmp") as directory:
            endpoint = endpoint_for_path(Path(directory) / "service.sock")
            with LocalSocketServer(
                router=router,
                endpoint=endpoint,
                gate=RLock(),
                timeout=10,
            ):
                registered = exchange(
                    endpoint.address,
                    s0.envelope_for(
                        REGISTER,
                        operation_input=_register_input(),
                        request_id="req-socket-register",
                        correlation_id="cor-socket-register",
                        trace_id="trc-socket-register",
                        workspace_id=sc.WORKSPACE_ID,
                        idempotency_key="idem-compete-register",
                    ),
                )
                assert isinstance(registered, SuccessResponseEnvelope)
                session_id = registered.result["session"]["session_id"]
                requests = [
                    s0.envelope_for(
                        APPEND,
                        operation_input=_append_input(
                            session_id, expected_parent_sequence=0
                        ),
                        request_id=f"req-competing-{index}",
                        correlation_id=f"cor-competing-{index}",
                        trace_id=f"trc-competing-{index}",
                        workspace_id=sc.WORKSPACE_ID,
                        idempotency_key=f"idem-competing-{index}",
                        mutation_precondition=MutationPrecondition(
                            record_version="seq-0"
                        ),
                    )
                    for index in range(2)
                ]
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(compete, endpoint.address, request)
                        for request in requests
                    ]
                    barrier.wait(timeout=10)
                    outcomes = [future.result(timeout=30) for future in futures]

        winners = [
            outcome for outcome in outcomes
            if isinstance(outcome, SuccessResponseEnvelope)
        ]
        losers = [
            outcome for outcome in outcomes
            if isinstance(outcome, ErrorResponseEnvelope)
        ]
        observed = [outcome.to_wire() for outcome in outcomes]
        assert len(winners) == 1, observed
        assert len(losers) == 1, observed
        assert losers[0].error.code == ERROR_CODE_MUTATION_PRECONDITION_FAILED
        winning_receipt = winners[0].result["receipt"]
        assert workspace.holder.connection.execute(
            "SELECT last_checkpoint_sequence, last_checkpoint_id "
            "FROM omnivia_engineering_sessions "
            "WHERE workspace_id = ? AND session_id = ?",
            (sc.WORKSPACE_ID, session_id),
        ).fetchone() == (1, winning_receipt["checkpoint_id"])
        assert workspace.holder.connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_checkpoints "
            "WHERE workspace_id = ? AND session_id = ?",
            (sc.WORKSPACE_ID, session_id),
        ).fetchone() == (1,)

        fresh = workspace.ok(
            "continuity.checkpoint.append",
            _append_input(
                session_id,
                parent_checkpoint_id=winning_receipt["checkpoint_id"],
                expected_parent_sequence=1,
            ),
            key="idem-after-competing-successors",
            mutation_precondition=MutationPrecondition(record_version="seq-1"),
        )
        assert fresh["receipt"]["sequence"] == 2
    finally:
        workspace.holder.connection.close()


def _session_with(
    workspace: Any, session: AuthenticatedSession, objectives: list[str]
) -> str:
    """Register a session as `session` and append one checkpoint per objective."""
    session_id = str(
        workspace.ok("continuity.session.register", _register_input(), session=session)[
            "session"
        ]["session_id"]
    )
    for head, objective in enumerate(objectives):
        workspace.ok(
            "continuity.checkpoint.append",
            _append_input(
                session_id, payload={"objective": objective, "checkpoint_kind": "periodic"}
            ),
            session=session,
            **_stated(f"seq-{head}"),
        )
    return session_id


def test_another_principal_cannot_append_to_or_close_a_session(workspace: Any) -> None:
    """The intruder names the owner's exact session id, states its true version
    and a stale one, and repeats itself. Every append and close is refused exactly
    as for a nonexistent session, before any stated version is compared and after
    the owner closes it, and settles nothing. The owner's own stated versions,
    replay and close are unchanged."""
    owner = _session_with(workspace, OWNER, ["Investigate the restore failure"])
    _session_with(workspace, OTHER, [])
    before = _settled(workspace)

    def attempts(session_id: str) -> list[tuple[str, str, str]]:
        return [
            workspace.refused(operation, payload, session=OTHER, **_stated(version))
            # The owner's true head, then a stale version.
            for version in ("seq-1", "seq-0")
            for operation, payload in (
                ("continuity.checkpoint.append", _append_input(session_id)),
                ("continuity.session.close", _close_input(session_id, expected_sequence=1)),
            )
        ]

    denied = attempts(owner)
    assert denied == attempts("esess-nowhere")
    assert {refusal[0] for refusal in denied} == {ERROR_CODE_NOT_FOUND}
    # A denial records no claim: one key repeats the same refusal, even for a
    # different body, rather than replaying or conflicting.
    for payload in (_append_input(owner), _append_input(owner, parent_checkpoint_id="eck-x")):
        assert workspace.refused(
            "continuity.checkpoint.append",
            payload,
            session=OTHER,
            key="idem-intruder",
            **_stated("seq-1"),
        ) == denied[0]
    assert _settled(workspace) == before

    stale = workspace.refused(
        "continuity.checkpoint.append", _append_input(owner), session=OWNER, **_stated("seq-0")
    )
    assert stale[0] == ERROR_CODE_MUTATION_PRECONDITION_FAILED
    appended = workspace.ok(
        "continuity.checkpoint.append",
        _append_input(owner),
        session=OWNER,
        key="idem-owner",
        **_stated("seq-1"),
    )
    assert appended["receipt"]["sequence"] == 2
    assert workspace.ok(
        "continuity.checkpoint.append",
        _append_input(owner),
        session=OWNER,
        key="idem-owner",
        **_stated("seq-1"),
    ) == appended
    closed = workspace.ok(
        "continuity.session.close",
        _close_input(owner, expected_sequence=2),
        session=OWNER,
        **_stated("seq-2"),
    )
    assert (closed["state"], closed["receipt"]["sequence"]) == ("closed", 3)

    # Closed, the session is still `not_found` to the intruder, never a conflict.
    assert attempts(owner) == denied
    assert workspace.refused(
        "continuity.checkpoint.append", _append_input(owner), session=OWNER, **_stated("seq-3")
    )[0] == ERROR_CODE_CONFLICT


def test_the_fenced_write_refuses_another_principal_whatever_the_precondition_saw(
    workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the precondition read blinded, append and close still resolve the
    session as the effective principal inside the fenced write."""
    from omnivia_core_runtime.service.handlers import continuity as handlers

    owner = _session_with(workspace, OWNER, ["Investigate the restore failure"])
    _session_with(workspace, OTHER, [])
    before = _settled(workspace)
    monkeypatch.setattr(handlers, "_session_version", lambda *_: "seq-1")
    for operation, payload in (
        ("continuity.checkpoint.append", _append_input(owner)),
        ("continuity.session.close", _close_input(owner, expected_sequence=1)),
    ):
        refusal = workspace.refused(operation, payload, session=OTHER, **_stated("seq-1"))
        assert refusal[0] == ERROR_CODE_NOT_FOUND
    assert _settled(workspace) == before


def test_another_principal_cannot_read_a_handoff_by_either_key(workspace: Any) -> None:
    """A checkpoint id, or a session and sequence, of another principal's session
    is `not_found` exactly as a nonexistent one; each principal reads its own by
    both keys."""
    _session_with(workspace, OWNER, [])
    missing = [
        workspace.refused("continuity.handoff.read", key, session=OWNER)
        for key in ({"checkpoint_id": "eck-nowhere"}, {"session_id": "esess-nowhere", "sequence": 1})
    ]
    assert {refusal[0] for refusal in missing} == {ERROR_CODE_NOT_FOUND}

    keys: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for session in (OWNER, OTHER):
        objective = f"Objective of {session.principal_id}"
        by_sequence = {"session_id": _session_with(workspace, session, [objective]), "sequence": 1}
        view = workspace.ok("continuity.handoff.read", by_sequence, session=session)["handoff"]
        by_id = {"checkpoint_id": view["checkpoint_id"]}
        assert workspace.ok("continuity.handoff.read", by_id, session=session)["handoff"] == view
        assert view["objective"] == objective
        keys[session.principal_id] = (by_id, by_sequence)

    for reader, other in ((OWNER, OTHER), (OTHER, OWNER)):
        assert [
            workspace.refused("continuity.handoff.read", key, session=reader)
            for key in keys[other.principal_id]
        ] == missing


def test_lower_grant_receiver_gets_only_a_bounded_digest_bound_view(
    workspace: Any,
) -> None:
    """A same-principal reader inherits context but none of the sender's authority."""
    session_id = workspace.ok(
        "continuity.session.register", _register_input(), session=OWNER
    )["session"]["session_id"]
    unresolved = [f"Open question {index}" for index in range(35)]
    suggestions = [f"Suggested action {index}" for index in range(35)]
    rich_payload = {
        "objective": "Transfer only bounded working context",
        "checkpoint_kind": "handoff",
        "external_run_ref": "sender-run-secret",
        "accepted_record_refs": [
            {"record_id": "mem-sender-accepted", "version": "gvr-sender-accepted"}
        ],
        "candidate_record_refs": [
            {"record_id": "mem-sender-candidate", "version": "gvr-sender-candidate"}
        ],
        "observations": [
            {
                "statement": "Sender-only observation",
                "evidence_refs": ["ev-sender-only"],
                "support": "claimed",
            }
        ],
        "completed_work": [
            {"statement": "Sender-only completion", "support": "claimed"}
        ],
        "failed_approaches": [
            {"statement": "Sender-only failed approach", "support": "claimed"}
        ],
        "unresolved_work": unresolved,
        "external_effects": [
            {"effect_ref": "effect-sender-unknown", "status": "unknown"}
        ],
        "next_actions": suggestions,
        "context_receipt": {"pack_checksum": "sha256:" + "a" * 64},
    }
    receipt = workspace.ok(
        "continuity.checkpoint.append",
        _append_input(session_id, payload=rich_payload),
        session=OWNER,
        **_stated("seq-0"),
    )["receipt"]

    required = HANDOFF.required_capability
    lower_grant = AuthenticatedSession(
        principal_id=OWNER.principal_id,
        roles=frozenset(),
        installations=OWNER.installations,
        workspaces=OWNER.workspaces,
        operations=frozenset({HANDOFF.name}),
        scopes=frozenset(HANDOFF.scope.required_scopes),
        purposes=frozenset({ENGINEERING_FAMILY_PURPOSES[HANDOFF.name]}),
        capabilities=(
            CapabilityRef(id=required.id, version=required.minimum_version),
        ),
    )
    assert len(lower_grant.capabilities) < len(OWNER.capabilities)
    view = workspace.ok(
        "continuity.handoff.read",
        {"checkpoint_id": receipt["checkpoint_id"]},
        session=lower_grant,
    )["handoff"]

    assert view["redacted"] is True
    assert view["unresolved_work"] == unresolved[:32]
    assert view["next_actions"] == suggestions[:32]
    assert view["omissions"] == [
        {"field": "accepted_record_refs", "reason": "retrieve_current_separately"},
        {"field": "candidate_record_refs", "reason": "working_context_redacted"},
        {"field": "checkpoint_kind", "reason": "working_context_redacted"},
        {"field": "completed_work", "reason": "working_context_redacted"},
        {"field": "context_receipt", "reason": "not_a_persisted_handle"},
        {"field": "external_effects", "reason": "owner_reconciliation_required"},
        {"field": "external_run_ref", "reason": "sender_runtime_context"},
        {"field": "failed_approaches", "reason": "working_context_redacted"},
        {"field": "next_actions", "reason": "bounded"},
        {"field": "observations", "reason": "requires_fresh_authorization"},
        {"field": "unresolved_work", "reason": "bounded"},
    ]
    assert view["content_digest"] == _rendered_handoff_digest(view)

    encoded = canonical_document(view)
    for sender_only in (
        "sender-run-secret",
        "mem-sender-accepted",
        "mem-sender-candidate",
        "Sender-only observation",
        "ev-sender-only",
        "Sender-only completion",
        "Sender-only failed approach",
        "effect-sender-unknown",
        "a" * 64,
    ):
        assert sender_only not in encoded

    # Selection by the other exact key and a repeated read are byte-equivalent.
    by_sequence = workspace.ok(
        "continuity.handoff.read",
        {"session_id": session_id, "sequence": 1},
        session=lower_grant,
    )["handoff"]
    repeated = workspace.ok(
        "continuity.handoff.read",
        {"checkpoint_id": receipt["checkpoint_id"]},
        session=lower_grant,
    )["handoff"]
    assert by_sequence == repeated == view

    tampered = dict(view)
    tampered["omissions"] = [*view["omissions"]]
    tampered["omissions"][0] = {
        "field": "accepted_record_refs",
        "reason": "tampered",
    }
    assert tampered["content_digest"] != _rendered_handoff_digest(tampered)

    stored_digest = workspace.holder.connection.execute(
        "SELECT content_digest FROM omnivia_engineering_checkpoints "
        "WHERE workspace_id = ? AND checkpoint_id = ?",
        (sc.WORKSPACE_ID, receipt["checkpoint_id"]),
    ).fetchone()[0]
    assert view["content_digest"] != stored_digest


def test_every_persisted_checkpoint_payload_field_has_a_handoff_policy() -> None:
    """A field the payload contract adds without a matching handoff policy is
    either a silent leak or a silent, unaccounted drop; this fails the day the
    field is added, not the day someone notices in production."""
    from omnivia_core_runtime.service.handlers import continuity as handlers

    payload_fields = {
        field.name for field in dataclasses.fields(EngineeringCheckpointPayload)
    }
    assert payload_fields == handlers._HANDOFF_KNOWN_FIELDS


@pytest.mark.parametrize(
    "selector",
    ({}, {"session_id": "esess-nowhere"}, {"sequence": 1}),
    ids=("neither", "session-only", "sequence-only"),
)
def test_handoff_read_rejects_incomplete_or_absent_selectors(
    workspace: Any, selector: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only two selector shapes are valid: `checkpoint_id` alone, or `session_id`
    and `sequence` together. Neither field, or only one half of the session
    pair, is a fixed `invalid_request`."""
    from omnivia_core_runtime.service.handlers import continuity as handlers

    def fail_if_authority_is_read(_connection: Any) -> Any:
        pytest.fail("an invalid selector reached authoritative storage")

    monkeypatch.setattr(handlers, "read_guard", fail_if_authority_is_read)
    assert workspace.refused(
        "continuity.handoff.read", selector, session=OWNER
    ) == (
        ERROR_CODE_INVALID_REQUEST,
        "the request payload is not valid for this continuity operation",
        "non_retryable",
    )


def test_handoff_read_rejects_matching_and_conflicting_mixed_selectors(
    workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkpoint id and a session/sequence pair are exactly one selector, not
    two redundant spellings of it. A self-consistent second selector and an
    outright conflicting one are the same fixed refusal: the shape is rejected
    before storage is ever consulted to know which case it is."""
    session_id = _session_with(workspace, OWNER, ["Investigate the restore failure"])
    by_sequence = workspace.ok(
        "continuity.handoff.read", {"session_id": session_id, "sequence": 1}, session=OWNER
    )["handoff"]
    checkpoint_id = by_sequence["checkpoint_id"]
    assert workspace.ok(
        "continuity.handoff.read", {"checkpoint_id": checkpoint_id}, session=OWNER
    )["handoff"] == by_sequence
    other_session_id = _session_with(workspace, OWNER, ["A second, unrelated session"])

    from omnivia_core_runtime.service.handlers import continuity as handlers

    def fail_if_authority_is_read(_connection: Any) -> Any:
        pytest.fail("a mixed selector reached authoritative storage")

    monkeypatch.setattr(handlers, "read_guard", fail_if_authority_is_read)

    for mixed in (
        # Self-consistent: both keys, correctly naming the same checkpoint.
        {"checkpoint_id": checkpoint_id, "session_id": session_id, "sequence": 1},
        # Conflicting: checkpoint_id names one checkpoint, session_id another.
        {"checkpoint_id": checkpoint_id, "session_id": other_session_id, "sequence": 1},
        # checkpoint_id plus only one half of the session pair.
        {"checkpoint_id": checkpoint_id, "session_id": session_id},
        {"checkpoint_id": checkpoint_id, "sequence": 1},
    ):
        assert workspace.refused(
            "continuity.handoff.read", mixed, session=OWNER
        ) == (
            ERROR_CODE_INVALID_REQUEST,
            "the request payload is not valid for this continuity operation",
            "non_retryable",
        )


def test_handoff_read_fails_closed_when_the_stored_digest_no_longer_matches_the_payload(
    workspace: Any,
) -> None:
    """Storage corruption -- the persisted payload no longer matches its own
    recorded content_digest, however that happened -- is never rendered into a
    view. The test drops the append-only update guard solely to create the
    impossible-on-the-service-path persisted state the reader must distrust."""
    session_id = _session_with(workspace, OWNER, ["Investigate the restore failure"])
    checkpoint_id = workspace.ok(
        "continuity.handoff.read", {"session_id": session_id, "sequence": 1}, session=OWNER
    )["handoff"]["checkpoint_id"]

    connection = workspace.holder.connection
    payload_json, stored_digest = connection.execute(
        "SELECT payload_json, content_digest FROM omnivia_engineering_checkpoints "
        "WHERE workspace_id = ? AND checkpoint_id = ?",
        (sc.WORKSPACE_ID, checkpoint_id),
    ).fetchone()
    payload = json.loads(payload_json)
    payload["objective"] = "tampered-secret-objective"
    connection.close()
    tampered = sqlite3.connect(str(workspace.holder.path))
    try:
        tampered.execute(
            "DROP TRIGGER omnivia_guard_omnivia_engineering_checkpoints_update"
        )
        tampered.execute(
            "UPDATE omnivia_engineering_checkpoints SET payload_json = ? "
            "WHERE workspace_id = ? AND checkpoint_id = ?",
            (canonical_document(payload), sc.WORKSPACE_ID, checkpoint_id),
        )
        tampered.commit()
    finally:
        tampered.close()
    workspace.restart()
    assert content_digest(canonical_document(payload)) != stored_digest

    refusal = workspace.refused(
        "continuity.handoff.read", {"checkpoint_id": checkpoint_id}, session=OWNER
    )
    assert refusal == (
        ERROR_CODE_INTERNAL_NON_RECOVERABLE,
        "the stored continuity checkpoint failed an internal integrity check",
        "non_retryable",
    )
    assert "tampered-secret-objective" not in refusal[1]


def test_handoff_read_fails_closed_on_a_persisted_field_with_no_handoff_policy(
    workspace: Any,
) -> None:
    """A persisted payload field the handoff has no rendered-or-omitted policy
    for is never disclosed by default: the read fails closed, digest recomputed
    to isolate this from the separate integrity check."""
    session_id = _session_with(workspace, OWNER, ["Investigate the restore failure"])
    checkpoint_id = workspace.ok(
        "continuity.handoff.read", {"session_id": session_id, "sequence": 1}, session=OWNER
    )["handoff"]["checkpoint_id"]

    connection = workspace.holder.connection
    payload_json = connection.execute(
        "SELECT payload_json FROM omnivia_engineering_checkpoints "
        "WHERE workspace_id = ? AND checkpoint_id = ?",
        (sc.WORKSPACE_ID, checkpoint_id),
    ).fetchone()[0]
    payload = json.loads(payload_json)
    payload["a_field_no_policy_names"] = "secret-value"
    canonical_payload = canonical_document(payload)
    connection.close()
    tampered = sqlite3.connect(str(workspace.holder.path))
    try:
        tampered.execute(
            "DROP TRIGGER omnivia_guard_omnivia_engineering_checkpoints_update"
        )
        tampered.execute(
            "UPDATE omnivia_engineering_checkpoints "
            "SET payload_json = ?, content_digest = ? "
            "WHERE workspace_id = ? AND checkpoint_id = ?",
            (
                canonical_payload,
                content_digest(canonical_payload),
                sc.WORKSPACE_ID,
                checkpoint_id,
            ),
        )
        tampered.commit()
    finally:
        tampered.close()
    workspace.restart()

    refusal = workspace.refused(
        "continuity.handoff.read", {"checkpoint_id": checkpoint_id}, session=OWNER
    )
    assert refusal == (
        ERROR_CODE_INTERNAL_NON_RECOVERABLE,
        "the stored continuity checkpoint carries a payload field this handoff has no policy for",
        "non_retryable",
    )
    assert "a_field_no_policy_names" not in refusal[1]
    assert "secret-value" not in refusal[1]


def test_ac024_a_failed_final_checkpoint_stage_leaves_a_lower_grant_handoff_read_unaffected(
    workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC024: a failed final-checkpoint staging (rolled back whole) leaves the
    previous durable checkpoint exactly as it was, and a lower-grant,
    same-principal handoff read of it -- neither the sender's own full grant,
    nor a different principal -- succeeds and is byte-identical to a read
    taken before the failed attempt."""
    session_id = workspace.ok(
        "continuity.session.register", _register_input(), session=OWNER
    )["session"]["session_id"]
    acknowledged = workspace.ok(
        "continuity.checkpoint.append",
        _append_input(session_id),
        session=OWNER,
        key="idem-ac024-acknowledged",
        **_stated("seq-0"),
    )["receipt"]

    required = HANDOFF.required_capability
    lower_grant = AuthenticatedSession(
        principal_id=OWNER.principal_id,
        roles=frozenset(),
        installations=OWNER.installations,
        workspaces=OWNER.workspaces,
        operations=frozenset({HANDOFF.name}),
        scopes=frozenset(HANDOFF.scope.required_scopes),
        purposes=frozenset({ENGINEERING_FAMILY_PURPOSES[HANDOFF.name]}),
        capabilities=(CapabilityRef(id=required.id, version=required.minimum_version),),
    )
    handoff_before = workspace.ok(
        "continuity.handoff.read",
        {"checkpoint_id": acknowledged["checkpoint_id"]},
        session=lower_grant,
    )["handoff"]
    before = _settled(workspace)

    original_append = continuity_storage.append_checkpoint

    def fail_after_staging(*args: Any, **kwargs: Any) -> dict[str, Any]:
        receipt = original_append(*args, **kwargs)
        if kwargs["checkpoint_kind"] == "session_close":
            raise RuntimeError("injected final checkpoint staging failure")
        return receipt

    monkeypatch.setattr(continuity_storage, "append_checkpoint", fail_after_staging)
    with pytest.raises(RuntimeError, match="injected final checkpoint staging failure"):
        workspace.call(
            "continuity.session.close",
            _close_input(session_id, expected_sequence=1),
            session=OWNER,
            key="idem-ac024-close",
            **_stated("seq-1"),
        )
    monkeypatch.setattr(continuity_storage, "append_checkpoint", original_append)

    # The failed staging left no claim: the session is still active at
    # sequence 1, with no new checkpoint, receipt or settled row of any kind.
    assert _settled(workspace) == before
    assert workspace.holder.connection.execute(
        "SELECT state, last_checkpoint_sequence, last_checkpoint_id "
        "FROM omnivia_engineering_sessions WHERE workspace_id = ? AND session_id = ?",
        (sc.WORKSPACE_ID, session_id),
    ).fetchone() == ("active", 1, acknowledged["checkpoint_id"])

    # A lower-grant, same-principal read of the previous durable checkpoint is
    # exactly what it was before the failed close attempt.
    handoff_after = workspace.ok(
        "continuity.handoff.read",
        {"checkpoint_id": acknowledged["checkpoint_id"]},
        session=lower_grant,
    )["handoff"]
    assert handoff_after == handoff_before
    assert handoff_after["content_digest"] == _rendered_handoff_digest(handoff_after)

    # The owner alone can still recover cleanly afterwards.
    recovered = workspace.ok(
        "continuity.session.close",
        _close_input(session_id, expected_sequence=1),
        session=OWNER,
        key="idem-ac024-close",
        **_stated("seq-1"),
    )
    assert recovered["state"] == "closed"


def test_working_context_search_reads_only_the_callers_checkpoints(workspace: Any) -> None:
    """Another principal's newer matching checkpoints change nothing the owner's
    search returns: not the matches, their order, the page count, nor the
    continuation's offset or snapshot. Each principal sees only its own, and a
    continuation is bound to the principal it was issued to."""
    _session_with(workspace, OWNER, [f"Restore session {n}" for n in ("alpha", "beta", "gamma")])

    def search(session: AuthenticatedSession, **extra: Any) -> dict[str, Any]:
        payload = {"query": "restore session", "view": "working_context", **extra}
        return workspace.ok("engineering.search", payload, session=session)

    def previews(result: dict[str, Any], field: str = "record_id") -> list[str]:
        return [preview[field] for preview in result["previews"]]

    everything = search(OWNER)
    first = search(OWNER, limit=2)
    token = first["page"]["continuation_token"]

    _session_with(workspace, OTHER, [f"Restore session XYZZY {n}" for n in range(4)])

    second = search(OWNER, limit=2, page={"continuation_token": token})
    assert previews(everything, "preview") == [
        "Restore session gamma",
        "Restore session beta",
        "Restore session alpha",
    ]
    assert previews(first) + previews(second) == previews(everything)
    assert second["page"] == {}
    assert search(OWNER) == everything
    # Seven checkpoints match in the workspace; the owner's three fit one page.
    assert search(OWNER, limit=3)["page"] == {}
    again = search(OWNER, limit=2)
    assert previews(again) == previews(first)
    issued, reissued = (
        PROCESS_CONTINUATION_TOKENS.decode(result["page"]["continuation_token"])
        for result in (first, again)
    )
    assert (reissued["o"], reissued["s"]) == (issued["o"], issued["s"])

    assert previews(search(OTHER), "preview") == [
        f"Restore session XYZZY {n}" for n in (3, 2, 1, 0)
    ]
    assert workspace.refused(
        "engineering.search",
        {
            "query": "restore session",
            "view": "working_context",
            "limit": 2,
            "page": {"continuation_token": token},
        },
        session=OTHER,
    )[0] == ERROR_CODE_INVALID_REQUEST


def test_the_resume_pack_reads_only_the_callers_checkpoints(workspace: Any) -> None:
    """The `resume` pack's working context is the caller's own five newest
    checkpoints. Another principal's newer ones take no slot, section, omission,
    budget or byte of the rendering, and the owner's never reach theirs."""
    _session_with(workspace, OWNER, [f"Owner step {n}" for n in range(1, 7)])
    build = {
        "query": "provider",
        "targets": [{"snapshot_id": "esnap-a", "snapshot_kind": "git_commit"}],
        "profile": "resume",
    }

    def pack(session: AuthenticatedSession) -> dict[str, Any]:
        return workspace.ok("engineering.context.build", build, session=session)["pack"]

    def objectives(built: dict[str, Any]) -> list[str]:
        return [
            section["content"].partition(" Unresolved:")[0]
            for section in built["sections"]
            if section["partition"] == "working_context"
        ]

    before = pack(OWNER)
    assert objectives(before) == [f"Owner step {n}" for n in (6, 5, 4, 3, 2)]

    _session_with(workspace, OTHER, [f"XYZZY step {n}" for n in range(1, 6)])

    after = pack(OWNER)
    for field in ("sections", "citations", "omissions", "uncertainties", "rendering", "budget"):
        assert after[field] == before[field]
    assert "XYZZY" not in json.dumps(after)
    theirs = pack(OTHER)
    assert objectives(theirs) == [f"XYZZY step {n}" for n in (5, 4, 3, 2, 1)]
    assert "Owner step" not in json.dumps(theirs)
