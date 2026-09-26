"""The engineering context pack vertical (plan PR-F).

One non-persisted pack built from the frozen accepted frontier: the pack_id is
the canonical digest of the result with exactly the root `pack_id` and the
nested reproducibility checksum removed, the rendering is counted exactly under
the pinned renderer, the budget reconciles against the actual rendering, and a
budget too small for the minimum safe context is a typed refusal — never a
silently truncated pack.
"""

from __future__ import annotations

import hashlib
import json
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
    ERROR_CODE_TOKEN_LIMIT_EXCEEDED,
    get_operation_metadata,
)

WORKSPACE_ID = s0.WORKSPACE_ID

BUILD = get_operation_metadata("engineering.context.build")

_CONTENT: dict[str, Any] = {
    "schema_version": "1.0",
    "kind": "decision",
    "title": "Authentication uses provider A on the mainline",
    "summary": "The mainline decision names provider A for interactive sign-in.",
    "what": "Provider A is the mainline authentication provider.",
    "assertion_basis": "observed",
}


def _owned(tmp_path: Any) -> Any:
    path = tmp_path / "workspace.sqlite"
    s0.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    return m1.take_ownership(path)


def _handlers(holder: Any) -> Any:
    return EngineeringHandlers(
        service=SimpleNamespace(connection=holder.connection, identity=holder.identity)
    )


def _context(holder: Any, operation_input: dict[str, Any]) -> OperationContext:
    entry = BUILD
    envelope = s0.envelope_for(entry, operation_input=operation_input)
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


def _build(holder: Any, **overrides: Any) -> Any:
    operation_input: dict[str, Any] = {
        "query": "authentication provider",
        "targets": [{"snapshot_id": "esnap-a", "snapshot_kind": "git_commit"}],
        "profile": "investigate",
    }
    operation_input.update(overrides)
    handlers = _handlers(holder)
    context = _context(holder, operation_input)
    return handlers.engineering_context_build(context)


def test_a_pack_is_built_with_exact_counts_and_a_self_verifying_checksum(
    tmp_path: Any,
) -> None:
    holder = _owned(tmp_path)
    try:
        _settle_create(holder, marker="obs-1", content=dict(_CONTENT))
        built = _build(holder)
        pack = built["pack"]

        assert pack["format_version"] == "engineering_context.v1"
        assert pack["fresh_authorization_required"] is True
        assert pack["pack_id"].startswith("sha256:")

        # The checksum rule: SHA-256 of the canonical result after removing
        # exactly the root pack_id and the nested reproducibility checksum.
        from omnivia_core.contracts.v1 import to_canonical_json

        verify = json.loads(json.dumps(pack))
        verify["reproducibility"].pop("artifact_checksum")
        verify.pop("pack_id")
        expected = "sha256:" + hashlib.sha256(
            to_canonical_json(verify).encode("utf-8")
        ).hexdigest()
        assert pack["pack_id"] == expected
        assert pack["reproducibility"]["artifact_checksum"] == expected
        assert (
            pack["reproducibility"]["artifact_canonicalization"] == "rfc8785"
        )

        # The rendering counts reconcile against the budget block.
        rendering = pack["rendering"]
        assert rendering["token_count"] == len(rendering["text"].split())
        assert rendering["byte_count"] == len(rendering["text"].encode("utf-8"))
        assert pack["budget"]["rendered_tokens"] == rendering["token_count"]
        assert pack["budget"]["rendered_bytes"] == rendering["byte_count"]

        # Every substantive section cites an exact record version.
        citations = {c["citation_id"]: c for c in pack["citations"]}
        for section in pack["sections"]:
            if section["partition"] in ("accepted_knowledge", "candidate_findings"):
                assert section["citation_ids"]
                for citation_id in section["citation_ids"]:
                    assert citation_id in citations
    finally:
        holder.connection.close()


def test_an_empty_frontier_builds_a_honest_empty_pack(tmp_path: Any) -> None:
    holder = _owned(tmp_path)
    try:
        built = _build(holder)
        pack = built["pack"]
        accepted = [
            s for s in pack["sections"] if s["partition"] == "accepted_knowledge"
        ]
        assert accepted == []
        assert pack["citations"] == []
        assert pack["uncertainties"]  # the not-evaluated uncertainty is stated
    finally:
        holder.connection.close()


def test_a_budget_too_small_for_the_minimum_context_is_a_typed_refusal(
    tmp_path: Any,
) -> None:
    holder = _owned(tmp_path)
    try:
        _settle_create(holder, marker="obs-1", content=dict(_CONTENT))
        with pytest.raises(OperationError) as budget:
            _build(
                holder,
                query="provider",
                budget={"model_tokens": 1, "model_bytes": 10},
            )
        assert budget.value.code == ERROR_CODE_TOKEN_LIMIT_EXCEEDED
    finally:
        holder.connection.close()
