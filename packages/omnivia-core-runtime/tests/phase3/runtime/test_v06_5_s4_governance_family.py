"""Focused S4 production acceptance for governed record transitions."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import omnivia_core_runtime.service.handlers.governance as governance_handlers_module
import pytest
import test_governed_truth_and_relations_migration as m3
import test_v06_5_s0_mutation_foundation as s0
import test_v06_5_s2_memory_family as s2
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.application import (
    GOVERNANCE_FAMILY_PURPOSES,
    ApplicationDispatcher,
    build_governance_application_dispatcher,
)
from omnivia_core_runtime.service.operations import OperationError
from omnivia_core_runtime.storage.retrieval import CONFIGURED_LOCAL_OWNER
from test_v06_5_s2_memory_migration import _apply_through

from omnivia_core.contracts.v1 import (
    DEFAULT_RETRY_CLASSIFICATION,
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CAPABILITY_NOT_GRANTED,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_MUTATION_PRECONDITION_FAILED,
    ERROR_CODE_NOT_FOUND,
    ErrorResponseEnvelope,
    MutationPrecondition,
    PrincipalClaim,
    RequestEnvelope,
    SuccessResponseEnvelope,
    get_operation_metadata,
)

MIGRATION_VERSION = 16


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m3.m2.Owned]:
    path = tmp_path / "workspace.sqlite"
    _apply_through(path, MIGRATION_VERSION, workspace_id=m3.WORKSPACE_ID)
    holder = m3.m2.take_ownership(path)
    yield holder
    holder.connection.close()


def _allocator(tag: str) -> Callable[[str], str]:
    counts: dict[str, int] = {}

    def allocate(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}-s4-{tag}-{counts[prefix]}"

    return allocate


def _dispatchers(
    owned: m3.m2.Owned, *, tag: str
) -> tuple[ApplicationDispatcher, ApplicationDispatcher]:
    memory = s2._dispatcher(owned, allocator_tag=f"s4-memory-{tag}")
    governance = build_governance_application_dispatcher(
        service=owned,
        principal_id=CONFIGURED_LOCAL_OWNER,
        installation_id=s0.INSTALLATION_ID,
        workspace_id=m3.WORKSPACE_ID,
        fallback=memory,
        clock=FakeClock(wall=datetime.fromtimestamp(1_900_000_000, tz=UTC)),
        allocate_identifier=_allocator(tag),
    )
    return memory, governance


def _create_source(
    memory: ApplicationDispatcher, *, tag: str
) -> SuccessResponseEnvelope:
    response = memory.dispatch(
        s2._request(
            "memory.create",
            s2._memory_input(f"governance source {tag}"),
            request_id=f"req-s4-create-{tag}",
            idempotency_key=f"idem-s4-create-{tag}",
        )
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    return response


def _request(
    operation: str,
    operation_input: dict[str, object],
    *,
    version: str,
    tag: str,
    idempotency_key: str | None = None,
) -> RequestEnvelope:
    return s0.envelope_for(
        get_operation_metadata(operation),
        operation_input=operation_input,
        request_id=f"req-s4-{tag}",
        correlation_id=f"cor-s4-{tag}",
        trace_id=f"trc-s4-{tag}",
        idempotency_key=(f"idem-s4-{tag}" if idempotency_key is None else idempotency_key),
        mutation_precondition=MutationPrecondition(record_version=version),
        purpose=GOVERNANCE_FAMILY_PURPOSES[operation],
        workspace_id=m3.WORKSPACE_ID,
    )


def _transition(
    dispatcher: ApplicationDispatcher,
    *,
    operation: str,
    record_id: str,
    version: str,
    tag: str,
    replacement: dict[str, object] | None = None,
) -> SuccessResponseEnvelope:
    operation_input: dict[str, object] = {
        "record_id": record_id,
        "rationale": {"reason_code": f"reason_{tag.replace('-', '_')}"},
    }
    if replacement is not None:
        operation_input["replacement"] = replacement
    response = dispatcher.dispatch(
        _request(operation, operation_input, version=version, tag=tag)
    )
    assert isinstance(response, SuccessResponseEnvelope), response
    return response


def _identity(response: SuccessResponseEnvelope, side: str) -> dict[str, object]:
    return response.result[f"{side}_record"]["provenance"]["identity"]


def _eligible_source(
    memory: ApplicationDispatcher,
    governance: ApplicationDispatcher,
    *,
    operation: str,
    tag: str,
) -> tuple[str, str]:
    created = _create_source(memory, tag=tag)
    identity = created.result["record"]["provenance"]["identity"]
    record_id = identity["record_id"]
    version = identity["version"]
    if operation == "knowledge.propose":
        return record_id, version
    proposed = _transition(
        governance,
        operation="knowledge.propose",
        record_id=record_id,
        version=version,
        tag=f"{tag}-setup-propose",
    )
    version = _identity(proposed, "updated")["version"]
    if operation in {"candidate.approve", "candidate.reject"}:
        return record_id, version
    approved = _transition(
        governance,
        operation="candidate.approve",
        record_id=record_id,
        version=version,
        tag=f"{tag}-setup-approve",
    )
    return record_id, _identity(approved, "updated")["version"]


def _assert_primary_replay_conflict(
    owned: m3.m2.Owned, *, operation: str, tag: str
) -> None:
    memory, governance = _dispatchers(owned, tag=tag)
    record_id, version = _eligible_source(
        memory, governance, operation=operation, tag=tag
    )
    operation_input: dict[str, object] = {
        "record_id": record_id,
        "rationale": {"reason_code": f"reason_{tag}"},
    }
    if operation == "record.supersede":
        operation_input["replacement"] = s2._memory_input(f"replacement {tag}")
    primary_request = _request(
        operation,
        operation_input,
        version=version,
        tag=f"{tag}-primary",
        idempotency_key=f"idem-s4-{tag}",
    )
    primary = governance.dispatch(primary_request)
    assert isinstance(primary, SuccessResponseEnvelope), primary
    transition_count = owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_application_governance_transitions"
    ).fetchone()

    replay = governance.dispatch(
        replace(
            primary_request,
            metadata=replace(
                primary_request.metadata, request_id=f"req-s4-{tag}-replay"
            ),
        )
    )
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == primary.result
    assert replay.metadata.audit_reference == primary.metadata.audit_reference
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_application_governance_transitions"
    ).fetchone() == transition_count

    changed = dict(operation_input)
    changed["rationale"] = {"reason_code": f"different_{tag}"}
    conflict = governance.dispatch(
        replace(
            primary_request,
            metadata=replace(
                primary_request.metadata, request_id=f"req-s4-{tag}-conflict"
            ),
            input=changed,
        )
    )
    assert isinstance(conflict, ErrorResponseEnvelope), conflict
    assert conflict.error.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
    assert conflict.metadata.audit_reference == primary.metadata.audit_reference


def test_v06_5_s4_knowledge_propose_primary_replay_conflict(
    owned: m3.m2.Owned,
) -> None:
    _assert_primary_replay_conflict(
        owned, operation="knowledge.propose", tag="propose"
    )


def test_v06_5_s4_candidate_approve_primary_replay_conflict(
    owned: m3.m2.Owned,
) -> None:
    _assert_primary_replay_conflict(
        owned, operation="candidate.approve", tag="approve"
    )


def test_v06_5_s4_candidate_reject_primary_replay_conflict(
    owned: m3.m2.Owned,
) -> None:
    _assert_primary_replay_conflict(
        owned, operation="candidate.reject", tag="reject"
    )


def test_v06_5_s4_record_supersede_primary_replay_conflict(
    owned: m3.m2.Owned,
) -> None:
    _assert_primary_replay_conflict(
        owned, operation="record.supersede", tag="supersede"
    )


def test_v06_5_s4_history_and_provenance_reconstruct(
    owned: m3.m2.Owned,
) -> None:
    memory, governance = _dispatchers(owned, tag="chain")
    created = _create_source(memory, tag="chain")
    source_identity = created.result["record"]["provenance"]["identity"]
    record_id = source_identity["record_id"]

    proposed = _transition(
        governance,
        operation="knowledge.propose",
        record_id=record_id,
        version=source_identity["version"],
        tag="propose",
    )
    proposed_identity = _identity(proposed, "updated")
    assert proposed_identity["record_id"] == record_id
    assert proposed_identity["governance_state"] == "candidate"
    assert [
        item["action"]
        for item in proposed.result["updated_record"]["provenance"]["history"]
    ] == ["knowledge.propose"]

    approved = _transition(
        governance,
        operation="candidate.approve",
        record_id=record_id,
        version=proposed_identity["version"],
        tag="approve",
    )
    approved_identity = _identity(approved, "updated")
    assert approved_identity["governance_state"] == "accepted"
    assert approved_identity["currentness"] == "current"
    assert approved.result["updated_record"]["reviewer"] == CONFIGURED_LOCAL_OWNER
    assert [
        item["action"]
        for item in approved.result["updated_record"]["provenance"]["history"]
    ] == ["knowledge.propose", "candidate.approve"]

    replacement = s2._memory_input("corrected governance claim")
    superseded = _transition(
        governance,
        operation="record.supersede",
        record_id=record_id,
        version=approved_identity["version"],
        tag="supersede",
        replacement=replacement,
    )
    updated_identity = _identity(superseded, "updated")
    previous_identity = _identity(superseded, "previous")
    assert superseded.result["updated_record"]["content"] == {
        "fact": "corrected governance claim"
    }
    assert previous_identity["currentness"] == "superseded"
    assert previous_identity["superseded_by"]["record_id"] == record_id
    assert previous_identity["superseded_by"]["version"] == updated_identity["version"]
    assert previous_identity["superseded_by"]["reason"] == "reason_supersede"
    assert updated_identity["supersedes"]["record_id"] == record_id
    assert updated_identity["supersedes"]["version"] == previous_identity["version"]
    assert updated_identity["supersedes"]["reason"] == "reason_supersede"
    assert [
        item["action"]
        for item in superseded.result["updated_record"]["provenance"]["history"]
    ] == ["knowledge.propose", "candidate.approve", "record.supersede"]

    assert owned.connection.execute(
        "SELECT operation FROM omnivia_application_governance_transitions "
        "ORDER BY target_record_version_id"
    ).fetchall() == [
        ("knowledge.propose",),
        ("candidate.approve",),
        ("record.supersede",),
    ]
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_governed_records"
    ).fetchone() == (1,)


def test_v06_5_s4_candidate_reject_is_a_terminal_governance_transition(
    owned: m3.m2.Owned,
) -> None:
    memory, governance = _dispatchers(owned, tag="reject")
    created = _create_source(memory, tag="reject")
    created_identity = created.result["record"]["provenance"]["identity"]
    proposed = _transition(
        governance,
        operation="knowledge.propose",
        record_id=created_identity["record_id"],
        version=created_identity["version"],
        tag="reject-propose",
    )
    proposed_identity = _identity(proposed, "updated")
    rejected = _transition(
        governance,
        operation="candidate.reject",
        record_id=created_identity["record_id"],
        version=proposed_identity["version"],
        tag="reject",
    )

    rejected_identity = _identity(rejected, "updated")
    assert rejected_identity["governance_state"] == "rejected"
    assert rejected_identity["currentness"] == "current"
    assert [
        item["action"]
        for item in rejected.result["updated_record"]["provenance"]["history"]
    ] == ["knowledge.propose", "candidate.reject"]


def test_v06_5_s4_transition_requires_reviewer_authority(
    owned: m3.m2.Owned,
) -> None:
    memory, governance = _dispatchers(owned, tag="authority")
    record_id, version = _eligible_source(
        memory, governance, operation="candidate.approve", tag="authority"
    )
    request = _request(
        "candidate.approve",
        {"record_id": record_id, "rationale": {"reason_code": "review"}},
        version=version,
        tag="authority-approve",
    )
    before = owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_application_governance_transitions"
    ).fetchone()
    denied = governance.dispatch(
        replace(
            request,
            metadata=replace(
                request.metadata,
                principal_claim=PrincipalClaim(
                    claimed_roles=("workspace_contributor",)
                ),
            ),
        )
    )
    assert isinstance(denied, ErrorResponseEnvelope), denied
    assert denied.error.code == ERROR_CODE_AUTHORIZATION_DENIED
    assert denied.metadata.audit_reference is None
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_application_governance_transitions"
    ).fetchone() == before


def test_v06_5_s4_http_dispatch_preserves_resolved_session_authority(
    owned: m3.m2.Owned,
) -> None:
    """A bearer cannot inherit the broader session configured on the endpoint."""
    memory, governance = _dispatchers(owned, tag="http-session")
    record_id, version = _eligible_source(
        memory, governance, operation="candidate.approve", tag="http-session"
    )
    request = _request(
        "candidate.approve",
        {"record_id": record_id, "rationale": {"reason_code": "http_review"}},
        version=version,
        tag="http-session-approve",
    )
    before = owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_application_governance_transitions"
    ).fetchone()
    restricted = replace(governance.session, roles=frozenset(), capabilities=())

    denied = s2._transport_call(
        "http", governance, request, http_session=restricted
    )

    assert isinstance(denied, ErrorResponseEnvelope), denied
    assert denied.error.code == ERROR_CODE_CAPABILITY_NOT_GRANTED
    assert denied.metadata.audit_reference is None
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_application_governance_transitions"
    ).fetchone() == before


def test_v06_5_s4_http_fallback_preserves_resolved_session_authority(
    owned: m3.m2.Owned,
) -> None:
    """A top-level family cannot drop caller authority while routing downward."""
    memory, governance = _dispatchers(owned, tag="http-fallback")
    restricted = replace(memory.session, roles=frozenset(), capabilities=())

    denied = s2._transport_call(
        "http",
        governance,
        s2._request(
            "memory.create",
            s2._memory_input("restricted fallback"),
            request_id="req-s4-http-fallback",
            idempotency_key="idem-s4-http-fallback",
        ),
        http_session=restricted,
    )

    assert isinstance(denied, ErrorResponseEnvelope), denied
    assert denied.error.code == ERROR_CODE_CAPABILITY_NOT_GRANTED
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_governed_records"
    ).fetchone() == (0,)


def test_v06_5_s4_extraction_cannot_bypass_governance(
    owned: m3.m2.Owned,
) -> None:
    m3.m2.seed_chain(owned)
    m3.seed_extracted_candidate(owned)
    _memory, governance = _dispatchers(owned, tag="extraction")
    before_audits = owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_application_audit_events"
    ).fetchone()
    response = governance.dispatch(
        _request(
            "knowledge.propose",
            {
                "record_id": "record-extracted",
                "rationale": {"reason_code": "extraction_review"},
            },
            version="version-extracted",
            tag="extraction",
        )
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == ERROR_CODE_CONFLICT
    assert response.metadata.audit_reference is None
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_application_governance_transitions"
    ).fetchone() == (0,)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_application_audit_events"
    ).fetchone() == before_audits


def test_v06_5_s4_failed_transition_rolls_back_audit_and_record(
    owned: m3.m2.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory, governance = _dispatchers(owned, tag="rollback")
    created = _create_source(memory, tag="rollback")
    identity = created.result["record"]["provenance"]["identity"]
    tables = (
        "omnivia_application_audit_events",
        "omnivia_governed_version_assemblies",
        "omnivia_governed_provenance_events",
        "omnivia_governed_version_seals",
        "omnivia_application_claim_lineage",
        "omnivia_application_governance_transitions",
        "omnivia_idempotency_claims",
        "omnivia_idempotency_outcomes",
    )
    before = {
        table: owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        for table in tables
    }
    original = governance_handlers_module.apply_governance_transition

    def fail_after_write(*args: object, **kwargs: object) -> dict[str, object]:
        original(*args, **kwargs)
        raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, "forced rollback")

    monkeypatch.setattr(
        governance_handlers_module,
        "apply_governance_transition",
        fail_after_write,
    )
    response = governance.dispatch(
        _request(
            "knowledge.propose",
            {
                "record_id": identity["record_id"],
                "rationale": {"reason_code": "rollback_review"},
            },
            version=identity["version"],
            tag="rollback",
        )
    )
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == ERROR_CODE_INTERNAL_NON_RECOVERABLE
    assert response.metadata.audit_reference is None
    assert before == {
        table: owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        for table in tables
    }


@pytest.mark.parametrize("adapter", ("in-process", "local-ipc", "http"))
@pytest.mark.parametrize(
    "operation",
    (
        "knowledge.propose",
        "candidate.approve",
        "candidate.reject",
        "record.supersede",
    ),
)
def test_v06_5_s4_primary_replay_conflict_across_real_adapters(
    owned: m3.m2.Owned, operation: str, adapter: str
) -> None:
    tag = f"adapter-{operation.replace('.', '-')}-{adapter}"
    memory, governance = _dispatchers(owned, tag=tag)
    record_id, version = _eligible_source(
        memory, governance, operation=operation, tag=tag
    )
    operation_input: dict[str, object] = {
        "record_id": record_id,
        "rationale": {"reason_code": "adapter_review"},
    }
    if operation == "record.supersede":
        operation_input["replacement"] = s2._memory_input("adapter replacement")
    request = _request(
        operation,
        operation_input,
        version=version,
        tag=tag,
        idempotency_key=f"idem-s4-{tag}",
    )
    primary = s2._transport_call(
        adapter,
        governance,
        request,
        case_id=f"{operation}/primary-success",
    )
    assert isinstance(primary, SuccessResponseEnvelope), primary
    replay = s2._transport_call(
        adapter,
        governance,
        replace(
            request,
            metadata=replace(
                request.metadata, request_id=f"req-s4-{tag}-replay"
            ),
        ),
        case_id=f"{operation}/honest-replay",
    )
    assert isinstance(replay, SuccessResponseEnvelope), replay
    assert replay.result == primary.result
    assert replay.metadata.audit_reference == primary.metadata.audit_reference

    changed = dict(operation_input)
    changed["rationale"] = {"reason_code": "different_adapter_review"}
    conflict = s2._transport_call(
        adapter,
        governance,
        replace(
            request,
            metadata=replace(request.metadata, request_id=f"req-s4-{tag}-conflict"),
            input=changed,
        ),
        case_id=f"{operation}/idempotency-conflict",
    )
    assert isinstance(conflict, ErrorResponseEnvelope), conflict
    assert conflict.error.code == ERROR_CODE_IDEMPOTENCY_CONFLICT
    assert conflict.metadata.audit_reference == primary.metadata.audit_reference


def _assert_application_error(response: object, code: str) -> None:
    assert isinstance(response, ErrorResponseEnvelope), response
    assert response.error.code == code
    assert response.error.retry_class == DEFAULT_RETRY_CLASSIFICATION[code]


@pytest.mark.parametrize("adapter", ("in-process", "local-ipc", "http"))
def test_v06_5_c1_generic_error_family_crosses_every_real_adapter(
    owned: m3.m2.Owned,
    adapter: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute the six shared handler/mutation failures through each adapter."""

    tag = f"c1-errors-{adapter}"
    memory, governance = _dispatchers(owned, tag=tag)

    invalid = s2._transport_call(
        adapter,
        governance,
        _request(
            "candidate.approve",
            {},
            version="v-invalid",
            tag=f"{tag}-invalid",
        ),
        case_id="error/invalid_request",
    )
    _assert_application_error(invalid, ERROR_CODE_INVALID_REQUEST)

    not_found = s2._transport_call(
        adapter,
        governance,
        _request(
            "candidate.approve",
            {
                "record_id": f"rec-missing-{adapter}",
                "rationale": {"reason_code": "c1_not_found"},
            },
            version="v-missing",
            tag=f"{tag}-not-found",
        ),
        case_id="error/not_found",
    )
    _assert_application_error(not_found, ERROR_CODE_NOT_FOUND)

    created = _create_source(memory, tag=f"{tag}-wrong-state")
    created_identity = created.result["record"]["provenance"]["identity"]
    conflict = s2._transport_call(
        adapter,
        governance,
        _request(
            "candidate.approve",
            {
                "record_id": created_identity["record_id"],
                "rationale": {"reason_code": "c1_wrong_state"},
            },
            version=created_identity["version"],
            tag=f"{tag}-conflict",
        ),
        case_id="error/conflict",
    )
    _assert_application_error(conflict, ERROR_CODE_CONFLICT)

    record_id, _version = _eligible_source(
        memory,
        governance,
        operation="candidate.approve",
        tag=f"{tag}-precondition",
    )
    precondition = s2._transport_call(
        adapter,
        governance,
        _request(
            "candidate.approve",
            {
                "record_id": record_id,
                "rationale": {"reason_code": "c1_stale_precondition"},
            },
            version="v-stale",
            tag=f"{tag}-precondition-approve",
        ),
        case_id="error/mutation_precondition_failed",
    )
    _assert_application_error(precondition, ERROR_CODE_MUTATION_PRECONDITION_FAILED)

    record_id, version = _eligible_source(
        memory,
        governance,
        operation="candidate.approve",
        tag=f"{tag}-idempotency",
    )
    idempotency_key = f"idem-{tag}-shared-conflict"
    primary_request = _request(
        "candidate.approve",
        {
            "record_id": record_id,
            "rationale": {"reason_code": "c1_idempotency_primary"},
        },
        version=version,
        tag=f"{tag}-idempotency-primary",
        idempotency_key=idempotency_key,
    )
    primary = s2._transport_call(adapter, governance, primary_request)
    assert isinstance(primary, SuccessResponseEnvelope), primary
    idempotency = s2._transport_call(
        adapter,
        governance,
        replace(
            primary_request,
            metadata=replace(
                primary_request.metadata,
                request_id=f"req-{tag}-idempotency-conflict",
            ),
            input={
                "record_id": record_id,
                "rationale": {"reason_code": "c1_idempotency_changed"},
            },
        ),
        case_id="error/idempotency_conflict",
    )
    _assert_application_error(idempotency, ERROR_CODE_IDEMPOTENCY_CONFLICT)

    record_id, version = _eligible_source(
        memory,
        governance,
        operation="candidate.approve",
        tag=f"{tag}-internal",
    )
    original = governance_handlers_module.apply_governance_transition

    def fail_after_write(*args: object, **kwargs: object) -> dict[str, object]:
        original(*args, **kwargs)
        raise OperationError(ERROR_CODE_INTERNAL_NON_RECOVERABLE, "forced C1 failure")

    monkeypatch.setattr(
        governance_handlers_module,
        "apply_governance_transition",
        fail_after_write,
    )
    internal = s2._transport_call(
        adapter,
        governance,
        _request(
            "candidate.approve",
            {
                "record_id": record_id,
                "rationale": {"reason_code": "c1_internal"},
            },
            version=version,
            tag=f"{tag}-internal-approve",
        ),
        case_id="error/internal_non_recoverable",
    )
    _assert_application_error(internal, ERROR_CODE_INTERNAL_NON_RECOVERABLE)
