"""The engineering retrieval vertical (plan PR-D).

An engineering observation enters through the real `memory.create` writer,
appears under the `candidates` view but not under `accepted` (nothing has
passed governance), answers bounded previews whose applicability honestly
states `not_evaluated`, and the continuity checkpoint surface answers the
`working_context` view. Expansion returns the anchor and refuses unknown ones.
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
from omnivia_core_runtime.storage.memory import create_memory_record
from omnivia_core_runtime.storage.retrieval import local_owner_label_grant

from omnivia_core.contracts.v1 import (
    ERROR_CODE_NOT_FOUND,
    get_operation_metadata,
)

WORKSPACE_ID = s0.WORKSPACE_ID

SEARCH = get_operation_metadata("engineering.search")
EXPAND = get_operation_metadata("engineering.expand")

_OBSERVATION_CONTENT: dict[str, Any] = {
    "schema_version": "1.0",
    "kind": "failed_approach",
    "title": "Credential retries do not resolve stale session restoration",
    "summary": "The fixture authenticates but fails when cached state is restored.",
    "what": "Adding credential retries did not resolve the reproduced failure.",
    "assertion_basis": "derived",
}


def _owned(tmp_path: Any) -> Any:
    path = tmp_path / "workspace.sqlite"
    s0.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    return m1.take_ownership(path)


def _handlers(holder: Any, entry: Any) -> Any:
    return EngineeringHandlers(
        service=SimpleNamespace(connection=holder.connection, identity=holder.identity)
    )


def _context(
    holder: Any,
    entry: Any,
    operation_input: dict[str, Any],
    *,
    stated_version: str | None = None,
) -> OperationContext:
    overrides: dict[str, Any] = {}
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
    """One real `memory.create` writer settlement for an engineering observation."""

    def mutate(fenced: Any, settlement: Any) -> Any:
        claim = {
            "record_type": "knowledge.finding",
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
        }
        from omnivia_core.contracts.v1 import MemoryCreateInput

        return create_memory_record(
            fenced,
            settlement,
            workspace_id=WORKSPACE_ID,
            claim=MemoryCreateInput.from_wire(claim),
            label_grant=local_owner_label_grant(
                principal_id=authorized.principal_id,
                workspace_id=WORKSPACE_ID,
                granted_workspace=WORKSPACE_ID,
            ),
        )

    entry = s0.get_operation_metadata("memory.create") if hasattr(s0, "get_operation_metadata") else __import__("omnivia_core.contracts.v1", fromlist=["get_operation_metadata"]).get_operation_metadata("memory.create")
    envelope = s0.envelope_for(
        entry,
        operation_input={"schema_version": "engineering.1", "marker": marker},
        idempotency_key=f"idem-create-{marker}",
    )
    authorized = authorize_application_request(
        envelope,
        session=s0.session_for(entry),
        binding=s0.BINDING,
        supported_capabilities=s0.SUPPORTED,
    )
    from omnivia_core_runtime.ownership.fencing import read_guard
    from omnivia_core_runtime.service.mutation import (
        execute_mutation,
        issue_mutation_grant,
    )

    from omnivia_core.contracts.v1 import idempotency_equivalence

    grant = issue_mutation_grant(
        authorized,
        session=s0.session_for(entry),
        binding=s0.BINDING,
        guard=read_guard(holder.connection),
        equivalence=idempotency_equivalence(
            entry.name,
            envelope.metadata,
            {"schema_version": "engineering.1", "marker": marker},
            principal_id=authorized.principal_id,
            workspace_id=authorized.workspace_id,
        ),
        clock=s0.clock_at(),
    )
    return execute_mutation(
        holder.connection,
        holder.identity,
        grant=grant,
        context=authorized,
        equivalence=idempotency_equivalence(
            entry.name,
            envelope.metadata,
            {"schema_version": "engineering.1", "marker": marker},
            principal_id=authorized.principal_id,
            workspace_id=authorized.workspace_id,
        ),
        mutate=mutate,
        validate_result=lambda _result: True,
        clock=s0.clock_at(),
    )


def _search(holder: Any, **overrides: Any) -> Any:
    operation_input: dict[str, Any] = {"query": "session restoration"}
    operation_input.update(overrides)
    handlers = _handlers(holder, SEARCH)
    context = _context(holder, SEARCH, operation_input)
    return handlers.engineering_search(context)


def test_an_observation_is_visible_as_a_candidate_and_not_as_accepted(
    tmp_path: Any,
) -> None:
    holder = _owned(tmp_path)
    try:
        outcome = _settle_create(
            holder, marker="obs-1", content=dict(_OBSERVATION_CONTENT)
        )
        record_id = outcome.result["record"]["provenance"]["identity"]["record_id"]

        candidates = _search(holder, view="candidates")
        matching = [
            p for p in candidates["previews"] if p["record_id"] == record_id
        ]
        assert len(matching) == 1
        preview = matching[0]
        assert preview["governance_state"] == "candidate"
        assert preview["observation_kind"] == "failed_approach"
        assert preview["applicability"] == "not_evaluated"
        assert len(preview["preview"]) <= 480

        # §1.1: correctly no accepted engineering knowledge when nothing has
        # passed governance.
        accepted = _search(holder, view="accepted")
        assert accepted["previews"] == []

        assert candidates["coverage"] == {
            "projection": "current",
            "applicability": "unavailable",
        }
    finally:
        holder.connection.close()


def test_the_query_selects_within_the_frozen_frontier(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        _settle_create(holder, marker="obs-1", content=dict(_OBSERVATION_CONTENT))
        other = dict(_OBSERVATION_CONTENT)
        other["title"] = "Unrelated decision about caching"
        other["summary"] = "Cache invalidation strategy."
        other["what"] = "We invalidate on write."
        _settle_create(holder, marker="obs-2", content=other)

        previews = _search(holder, view="candidates")["previews"]
        assert len(previews) == 1
        assert "Credential retries" in previews[0]["title"]
    finally:
        holder.connection.close()


def test_working_context_reads_the_checkpoint_index(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        from omnivia_core.contracts.v1 import get_operation_metadata as gom

        append_entry = gom("continuity.checkpoint.append")
        from omnivia_core_runtime.service.handlers.continuity import ContinuityHandlers

        continuity = ContinuityHandlers(
            service=SimpleNamespace(
                connection=holder.connection, identity=holder.identity
            ),
            session=s0.session_for(append_entry),
            binding=s0.BINDING,
            clock=s0.clock_at(),
        )
        registered = _settle_create(
            holder, marker="obs-0", content=dict(_OBSERVATION_CONTENT)
        )
        del registered

        # Register a session and append one checkpoint through the coordinator.
        register_entry = gom("continuity.session.register")
        register_handlers = ContinuityHandlers(
            service=SimpleNamespace(
                connection=holder.connection, identity=holder.identity
            ),
            session=s0.session_for(register_entry),
            binding=s0.BINDING,
            clock=s0.clock_at(),
        )
        reg_context = _context(
            holder,
            register_entry,
            {"schema_version": "engineering.1"},
        )
        reg_outcome = register_handlers.continuity_session_register(reg_context)
        session_id = reg_outcome.result["session"]["session_id"]

        append_input = {
            "session_id": session_id,
            "payload": {
                "objective": "Investigate the session-restoration failure",
                "checkpoint_kind": "periodic",
            },
        }
        append_context = _context(
            holder,
            append_entry,
            append_input,
            stated_version="seq-0",
        )
        continuity.continuity_checkpoint_append(append_context)

        found = _search(holder, view="working_context", query="session-restoration")
        previews = found["previews"]
        assert len(previews) == 1
        assert previews[0]["governance_state"] == "continuity_evidence"
        assert "session-restoration" in previews[0]["preview"]
    finally:
        holder.connection.close()


def test_expand_refuses_an_unknown_anchor_and_serves_a_known_one(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        outcome = _settle_create(
            holder, marker="obs-1", content=dict(_OBSERVATION_CONTENT)
        )
        identity = outcome.result["record"]["provenance"]["identity"]
        handlers = _handlers(holder, EXPAND)

        with pytest.raises(OperationError) as missing:
            context = _context(
                holder,
                EXPAND,
                {"anchor": {"record_id": "rec-nowhere", "version": "v1"}},
            )
            handlers.engineering_expand(context)
        assert missing.value.code == ERROR_CODE_NOT_FOUND

        served = handlers.engineering_expand(
            _context(
                holder,
                EXPAND,
                {
                    "anchor": {
                        "record_id": identity["record_id"],
                        "version": identity["version"],
                    }
                },
            )
        )
        assert served["nodes"][0]["record_id"] == identity["record_id"]
        assert served["truncated"] is False
    finally:
        holder.connection.close()
