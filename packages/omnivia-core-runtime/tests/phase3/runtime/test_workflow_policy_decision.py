"""C2 effective-policy/budget decision authority for Workflow Run admission.

The resolver is not allowed to be a convenient dictionary merge. Runtime-significant
fields combine by their own safety rule: capabilities intersect, ceilings narrow,
evidence accumulates and deny wins. The result is still only a decision; C3 owns
writing it in the same fenced mutation that admits the run.
"""

from __future__ import annotations

import pytest
import test_rt202_policy_budget_snapshot_repository as r202
import test_workflow_runs_repository as repo
from omnivia_core_runtime.service.workflow_policy import (
    DECISION_REASON,
    DecisionRefused,
    PolicySource,
    resolve_effective_policy,
)

WORKSPACE_ID = repo.WORKSPACE_ID
RUN_ID = repo.RUN_ID


def source(
    kind: str,
    source_id: str,
    **overrides: object,
) -> PolicySource:
    values: dict[str, object] = {
        "kind": kind,
        "source_id": source_id,
    }
    values.update(overrides)
    return PolicySource(**values)


def valid_sources() -> tuple[PolicySource, ...]:
    return (
        source(
            "platform_safety_boundary",
            "platform-default",
            allowed_capabilities=("memory.read", "memory.write", "tools.execute"),
            offered_capabilities=("memory.read", "memory.write", "tools.execute"),
            max_cost_units=1_000,
            max_token_units=200_000,
            max_wall_clock_ms=600_000,
            side_effects_allowed=True,
        ),
        source(
            "workspace",
            "workspace-standard",
            allowed_capabilities=("memory.read", "memory.write"),
            required_capabilities=("memory.read",),
            offered_capabilities=("memory.search",),
            max_cost_units=250,
            required_evidence=("workflow.start.policy_trace",),
        ),
        source(
            "workflow_settings",
            "workflow-release",
            allowed_capabilities=("memory.read",),
            required_evidence=("workflow.start.policy_trace", "workflow.start.input"),
            max_token_units=50_000,
            side_effects_allowed=False,
        ),
    )


def test_effective_policy_resolves_fields_by_their_own_rules() -> None:
    decision = resolve_effective_policy(valid_sources())

    assert decision.granted_capabilities == ("memory.read",)
    assert decision.discovered_capabilities == (
        "memory.read",
        "memory.search",
        "memory.write",
        "tools.execute",
    )
    assert decision.required_capabilities == ("memory.read",)
    assert decision.required_evidence == (
        "workflow.start.input",
        "workflow.start.policy_trace",
    )
    assert decision.max_cost_units == 250
    assert decision.max_token_units == 50_000
    assert decision.max_wall_clock_ms == 600_000
    assert decision.side_effects_allowed is False
    assert decision.source_trace == (
        "platform_safety_boundary:platform-default",
        "workspace:workspace-standard",
        "workflow_settings:workflow-release",
    )


def test_run_decision_materialises_stable_contract_records() -> None:
    effective = resolve_effective_policy(valid_sources())

    first = effective.decide(
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        pinned_at=r202.pinned_at(10),
        audit_reference="audit-workflow.start",
        scopes=("workflow.run",),
        purpose="workflow_run",
    )
    replay = effective.decide(
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        pinned_at=r202.pinned_at(10),
        audit_reference="audit-workflow.start",
        scopes=("workflow.run",),
        purpose="workflow_run",
    )

    assert first == replay
    assert first.policy.decision_reason == DECISION_REASON
    assert first.policy.granted_capabilities == ("memory.read",)
    assert first.policy.discovered_capabilities == (
        "memory.read",
        "memory.search",
        "memory.write",
        "tools.execute",
    )
    assert first.budget.max_cost_units == 250
    assert first.budget.max_token_units == 50_000
    assert first.budget.consumed_cost_units == 0
    assert first.budget.consumed_token_units == 0
    assert tuple(grant.capability_id for grant in first.grants) == ("memory.read",)
    assert first.grants[0].policy_snapshot_id == first.policy.policy_snapshot_id


def test_repinning_carries_consumption_and_cannot_broaden() -> None:
    narrow = resolve_effective_policy(valid_sources()).decide(
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        pinned_at=r202.pinned_at(10),
        audit_reference="audit-workflow.start",
        scopes=("workflow.run",),
        purpose="workflow_run",
    )
    narrowed_again = resolve_effective_policy(
        (
            source(
                "platform_safety_boundary",
                "platform-default",
                allowed_capabilities=("memory.read",),
                max_cost_units=200,
                max_token_units=40_000,
                max_wall_clock_ms=500_000,
            ),
        )
    ).decide(
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        pinned_at=r202.pinned_at(20),
        audit_reference="audit-workflow.start",
        scopes=("workflow.run",),
        purpose="workflow_run",
        previous=narrow,
    )

    assert narrowed_again.policy.revision == 2
    assert narrowed_again.budget.revision == 2
    assert narrowed_again.budget.max_cost_units == 200
    assert narrowed_again.budget.max_wall_clock_ms == 500_000

    wider = resolve_effective_policy(
        (
            source(
                "platform_safety_boundary",
                "platform-default",
                allowed_capabilities=("memory.read", "memory.write"),
                max_cost_units=300,
                max_token_units=40_000,
            ),
        )
    )
    with pytest.raises(DecisionRefused, match="not a valid one"):
        wider.decide(
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            pinned_at=r202.pinned_at(30),
            audit_reference="audit-workflow.start",
            scopes=("workflow.run",),
            purpose="workflow_run",
            previous=narrowed_again,
        )


@pytest.mark.parametrize(
    "sources",
    [
        (),
        (source("workspace", "workspace-standard", allowed_capabilities=("memory.read",)),),
        (
            source(
                "workflow_settings",
                "workflow-release",
                allowed_capabilities=("memory.read",),
            ),
            source(
                "platform_safety_boundary",
                "platform-default",
                allowed_capabilities=("memory.read",),
            ),
        ),
        (
            source(
                "platform_safety_boundary",
                "platform-default",
                allowed_capabilities=("memory.read",),
            ),
            source("unknown", "mystery", allowed_capabilities=("memory.read",)),
        ),
    ],
)
def test_trace_must_be_authoritative_ordered_and_known(
    sources: tuple[PolicySource, ...],
) -> None:
    with pytest.raises(DecisionRefused):
        resolve_effective_policy(sources)


def test_missing_capability_or_budget_authority_refuses() -> None:
    with pytest.raises(DecisionRefused, match="allowed_capabilities"):
        resolve_effective_policy(
            (
                source(
                    "platform_safety_boundary",
                    "platform-default",
                    max_cost_units=1,
                    max_token_units=1,
                ),
            )
        )
    with pytest.raises(DecisionRefused, match="requires capabilities"):
        resolve_effective_policy(
            (
                source(
                    "platform_safety_boundary",
                    "platform-default",
                    allowed_capabilities=("memory.read",),
                    required_capabilities=("memory.write",),
                    max_cost_units=1,
                    max_token_units=1,
                ),
            )
        )
    with pytest.raises(DecisionRefused, match="max_token_units"):
        resolve_effective_policy(
            (
                source(
                    "platform_safety_boundary",
                    "platform-default",
                    allowed_capabilities=("memory.read",),
                    max_cost_units=1,
                ),
            )
        )


@pytest.mark.parametrize("field", ["max_cost_units", "max_token_units", "max_wall_clock_ms"])
def test_invalid_ceilings_refuse(field: str) -> None:
    ceilings = {
        "max_cost_units": 1,
        "max_token_units": 1,
        "max_wall_clock_ms": 1,
    }
    ceilings[field] = -1
    with pytest.raises(DecisionRefused, match=field):
        resolve_effective_policy(
            (
                source(
                    "platform_safety_boundary",
                    "platform-default",
                    allowed_capabilities=("memory.read",),
                    **ceilings,
                ),
            )
        )
