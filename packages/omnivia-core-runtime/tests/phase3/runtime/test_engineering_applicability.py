"""Applicability, priority and review rules over the real substrate (PR-E/PR-G).

A priority is one principal's own preference: an audited upsert that never
changes governed state, and `preferred` reorders the already-ranked page as a
stable secondary sort — never as a score. A review records an attestation and a
target-specific assessment: the expected assessment version is a real
precondition, an acknowledged review without evidence cannot clear a stale
target (§15.5), and the deterministic assessment is conservative — `matched`
only when the target snapshot is the newest registered snapshot of the record's
claimed repository.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.service.application import authorize_application_request
from omnivia_core_runtime.service.handlers.engineering import EngineeringHandlers
from omnivia_core_runtime.service.operations import OperationContext, OperationError
from omnivia_core_runtime.storage import repository_identity as repo_identity

from omnivia_core.contracts.v1 import (
    ERROR_CODE_MUTATION_PRECONDITION_FAILED,
    get_operation_metadata,
)

WORKSPACE_ID = s0.WORKSPACE_ID

PRIORITY = get_operation_metadata("context.priority.set")
REVIEW = get_operation_metadata("engineering.review.record")
SEARCH = get_operation_metadata("engineering.search")

_CONTENT: dict[str, Any] = {
    "schema_version": "1.0",
    "kind": "decision",
    "title": "Authentication uses provider A on the mainline",
    "summary": "The mainline decision names provider A.",
    "what": "Provider A is the mainline authentication provider.",
    "assertion_basis": "observed",
    "applicability": {"repository_id": "erepo-1"},
}


def _owned(tmp_path: Any) -> Any:
    path = tmp_path / "workspace.sqlite"
    s0.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    return m1.take_ownership(path)


def _handlers(holder: Any, entry: Any) -> Any:
    return EngineeringHandlers(
        service=SimpleNamespace(connection=holder.connection, identity=holder.identity),
        session=s0.session_for(entry),
        binding=s0.BINDING,
        clock=s0.clock_at(),
    )


def _context(
    holder: Any,
    entry: Any,
    operation_input: dict[str, Any],
    *,
    stated_version: str | None = None,
    idempotency_key: str | None = None,
) -> OperationContext:
    overrides: dict[str, Any] = {}
    if idempotency_key is not None:
        overrides["idempotency_key"] = idempotency_key
    if stated_version is not None:
        from omnivia_core.contracts.v1 import MutationPrecondition

        overrides["mutation_precondition"] = MutationPrecondition(
            record_version=stated_version
        )
    envelope = s0.envelope_for(entry, operation_input=operation_input, **overrides)
    authorized = authorize_application_request(
        envelope,
        session=s0.session_for(entry),
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


def _settle_create(holder: Any, *, marker: str, content: dict[str, Any]) -> Any:
    from omnivia_core_runtime.ownership.fencing import read_guard
    from omnivia_core_runtime.service.mutation import (
        execute_mutation,
        issue_mutation_grant,
    )
    from omnivia_core_runtime.storage.memory import create_memory_record
    from omnivia_core_runtime.storage.retrieval import local_owner_label_grant

    from omnivia_core.contracts.v1 import MemoryCreateInput, idempotency_equivalence

    entry = get_operation_metadata("memory.create")
    operation_input = {
        "record_type": "knowledge.decision",
        "domain_scope": "engineering.codebase",
        "content": content,
        "evidence_disposition": "unavailable",
        "sources": [],
        "assertion": {
            "actor_id": "agent-1",
            "actor_kind": "agent",
            "actor_role": "contributor",
            "asserted_at": "2026-01-27T00:00:00Z",
            "evidence": [],
        },
        "marker": marker,
    }
    envelope = s0.envelope_for(
        entry, operation_input=operation_input, idempotency_key=f"idem-create-{marker}"
    )
    authorized = authorize_application_request(
        envelope,
        session=s0.session_for(entry),
        binding=s0.BINDING,
        supported_capabilities=s0.SUPPORTED,
    )
    grant = issue_mutation_grant(
        authorized,
        session=s0.session_for(entry),
        binding=s0.BINDING,
        guard=read_guard(holder.connection),
        equivalence=idempotency_equivalence(
            entry.name,
            envelope.metadata,
            operation_input,
            principal_id=authorized.principal_id,
            workspace_id=authorized.workspace_id,
        ),
        clock=s0.clock_at(),
    )

    def mutate(fenced: Any, settlement: Any) -> Any:
        return create_memory_record(
            fenced,
            settlement,
            workspace_id=WORKSPACE_ID,
            claim=MemoryCreateInput.from_wire(
                {k: v for k, v in operation_input.items() if k != "marker"}
            ),
            label_grant=local_owner_label_grant(
                principal_id=authorized.principal_id,
                workspace_id=WORKSPACE_ID,
                granted_workspace=WORKSPACE_ID,
            ),
        )

    return execute_mutation(
        holder.connection,
        holder.identity,
        grant=grant,
        context=authorized,
        equivalence=idempotency_equivalence(
            entry.name,
            envelope.metadata,
            operation_input,
            principal_id=authorized.principal_id,
            workspace_id=authorized.workspace_id,
        ),
        mutate=mutate,
        validate_result=lambda _result: True,
        clock=s0.clock_at(),
    )


def _register_repo_and_snapshot(holder: Any, *, marker: str, snapshot_id: str) -> None:
    def mutate(fenced: Any, settlement: Any) -> Any:
        if marker.endswith("repo"):
            repo_identity.register_repository(
                fenced,
                settlement,
                workspace_id=WORKSPACE_ID,
                repository_id="erepo-1",
                display_name="app",
                provider_hint=None,
                registered_at_us=settlement.settled_at_us,
            )
            return {"registered": "erepo-1"}
        repo_identity.record_snapshot(
            fenced,
            settlement,
            workspace_id=WORKSPACE_ID,
            snapshot_id=snapshot_id,
            repository_id="erepo-1",
            snapshot_kind="git_commit",
            manifest={"files": 1},
            base_commit="abc123",
            capture_status="complete",
            captured_at_us=settlement.settled_at_us,
        )
        return {"snapshot": snapshot_id}

    from omnivia_core_runtime.ownership.fencing import read_guard
    from omnivia_core_runtime.service.mutation import (
        execute_mutation,
        issue_mutation_grant,
    )

    from omnivia_core.contracts.v1 import idempotency_equivalence

    entry = get_operation_metadata("memory.create")
    operation_input = {"marker": marker}
    envelope = s0.envelope_for(
        entry, operation_input=operation_input, idempotency_key=f"idem-{marker}"
    )
    authorized = authorize_application_request(
        envelope,
        session=s0.session_for(entry),
        binding=s0.BINDING,
        supported_capabilities=s0.SUPPORTED,
    )
    grant = issue_mutation_grant(
        authorized,
        session=s0.session_for(entry),
        binding=s0.BINDING,
        guard=read_guard(holder.connection),
        equivalence=idempotency_equivalence(
            entry.name,
            envelope.metadata,
            operation_input,
            principal_id=authorized.principal_id,
            workspace_id=authorized.workspace_id,
        ),
        clock=s0.clock_at(),
    )
    execute_mutation(
        holder.connection,
        holder.identity,
        grant=grant,
        context=authorized,
        equivalence=idempotency_equivalence(
            entry.name,
            envelope.metadata,
            operation_input,
            principal_id=authorized.principal_id,
            workspace_id=authorized.workspace_id,
        ),
        mutate=mutate,
        validate_result=lambda _result: True,
        clock=s0.clock_at(),
    )


def _search(holder: Any, **overrides: Any) -> Any:
    operation_input: dict[str, Any] = {"query": "provider A"}
    operation_input.update(overrides)
    handlers = _handlers(holder, SEARCH)
    context = _context(holder, SEARCH, operation_input)
    return handlers.engineering_search(context)


def test_a_priority_is_an_audited_upsert_and_reorders_the_ranked_page(
    tmp_path: Any,
) -> None:
    holder = _owned(tmp_path)
    try:
        first = _settle_create(holder, marker="obs-1", content=dict(_CONTENT))
        second_content = dict(_CONTENT)
        second_content["title"] = "Second provider A decision"
        second = _settle_create(holder, marker="obs-2", content=second_content)
        first_id = first.result["record"]["provenance"]["identity"]["record_id"]
        second_id = second.result["record"]["provenance"]["identity"]["record_id"]

        second_version = second.result["record"]["provenance"]["identity"]["version"]
        handlers = _handlers(holder, PRIORITY)
        preferred = handlers.context_priority_set(
            _context(
                holder,
                PRIORITY,
                {
                    "target": {"record_id": second_id, "version": second_version},
                    "priority": "preferred",
                },
                idempotency_key="idem-priority-preferred",
            )
        )
        assert preferred["priority"] == "preferred"

        page = _search(holder, view="candidates")
        order = [p["record_id"] for p in page["previews"]]
        assert order == [second_id, first_id]

        # Restating as normal is the audited supersession, not a deletion.
        normal = handlers.context_priority_set(
            _context(
                holder,
                PRIORITY,
                {
                    "target": {"record_id": second_id, "version": second_version},
                    "priority": "normal",
                },
                idempotency_key="idem-priority-normal",
            )
        )
        assert normal["priority"] == "normal"
        restored = _search(holder, view="candidates")
        assert [p["record_id"] for p in restored["previews"]] == [first_id, second_id]
    finally:
        holder.connection.close()


def test_a_review_precondition_and_the_conservative_assessment(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        _register_repo_and_snapshot(holder, marker="repo", snapshot_id="esnap-a")
        _register_repo_and_snapshot(holder, marker="snap-a", snapshot_id="esnap-a")
        outcome = _settle_create(holder, marker="obs-1", content=dict(_CONTENT))
        identity = outcome.result["record"]["provenance"]["identity"]
        record_id = identity["record_id"]
        record_version = identity["version"]
        handlers = _handlers(holder, REVIEW)

        # Stating an expectation where no assessment exists is a precondition
        # failure: the caller must re-read before the first write.
        with pytest.raises(OperationError) as stale:
            handlers.engineering_review_record(
                _context(
                    holder,
                    REVIEW,
                    {
                        "record_ref": {
                            "record_id": record_id,
                            "version": record_version,
                        },
                        "target_snapshot": {"snapshot_id": "esnap-a"},
                        "review_outcome": "evidence_attached",
                        "review_evidence_id": "ev-1",
                        "expected_assessment_version": "eas-nowhere",
                    },
                    stated_version="assessment-0",
                    idempotency_key="idem-review-stale",
                )
            )
        assert stale.value.code == ERROR_CODE_MUTATION_PRECONDITION_FAILED

        # The honest first write: the count-based expectation states zero prior
        # assessments. The target is the newest registered snapshot, so the
        # deterministic assessment is `matched`.
        recorded = handlers.engineering_review_record(
            _context(
                holder,
                REVIEW,
                {
                    "record_ref": {
                        "record_id": record_id,
                        "version": record_version,
                    },
                    "target_snapshot": {"snapshot_id": "esnap-a"},
                    "review_outcome": "evidence_attached",
                    "review_evidence_id": "ev-1",
                },
                stated_version="assessment-0",
                idempotency_key="idem-review-first",
            )
        )
        assert recorded["applicability"] == "matched"

        # A newer head is registered: the same target is now potentially stale,
        # and an acknowledgement without evidence cannot clear it.
        _register_repo_and_snapshot(holder, marker="snap-b", snapshot_id="esnap-b")
        recorded_again = handlers.engineering_review_record(
            _context(
                holder,
                REVIEW,
                {
                    "record_ref": {
                        "record_id": record_id,
                        "version": record_version,
                    },
                    "target_snapshot": {"snapshot_id": "esnap-a"},
                    "review_outcome": "acknowledged",
                },
                stated_version="assessment-1",
                idempotency_key="idem-review-second",
            )
        )
        assert recorded_again["applicability"] == "potentially_stale"
    finally:
        holder.connection.close()


def test_an_unknown_target_is_unknown_never_matched(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        _register_repo_and_snapshot(holder, marker="repo", snapshot_id="esnap-a")
        _register_repo_and_snapshot(holder, marker="snap-a", snapshot_id="esnap-a")
        outcome = _settle_create(holder, marker="obs-1", content=dict(_CONTENT))
        identity = outcome.result["record"]["provenance"]["identity"]
        handlers = _handlers(holder, REVIEW)
        recorded = handlers.engineering_review_record(
            _context(
                holder,
                REVIEW,
                {
                    "record_ref": {
                        "record_id": identity["record_id"],
                        "version": identity["version"],
                    },
                    "target_snapshot": {"snapshot_id": "esnap-nowhere"},
                    "review_outcome": "acknowledged",
                },
                stated_version="assessment-0",
                idempotency_key="idem-review-unknown",
            )
        )
        assert recorded["applicability"] == "unknown"
    finally:
        holder.connection.close()
