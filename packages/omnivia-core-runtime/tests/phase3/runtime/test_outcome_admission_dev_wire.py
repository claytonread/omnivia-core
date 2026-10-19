"""C08: Core consumes the accepted Dev admission wire format exactly, through the production surface.

The summary below is written out as Dev produces it (`services/omnivia-memory-dev/.../task_context/admission.py`
`AdmissionSummary.to_dict`), and its identity is computed with Dev's own algorithm, `canonical_dumps` followed by
SHA-256, reimplemented here rather than imported. A summary that Dev emits must be admitted, stored and read back
equal, and Core's canonical JSON must be byte-identical to Dev's for every shape Dev can emit.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.service.handlers.task_context import (
    OPERATION_OUTCOME_CREATE,
    OPERATION_OUTCOME_READ,
)
from test_project_context_admission import (
    ALPHA,
    MEMBER,
    OWNER,
    REVIEWER,
    WS,
    Harness,
    export_default,
    switch,
)

from omnivia_core.contracts.v1 import to_canonical_json


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    m1.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WS)
    holder = m1.take_ownership(path, workspace_id=WS)
    yield holder
    holder.connection.close()


@pytest.fixture
def harness(owned: m1.Owned) -> Harness:
    return Harness(owned)


OBJECTIVE = 'Résumé des risques ouverts, 数据 — "quoted"\ttabbed'


def dev_canonical_dumps(payload: dict[str, Any]) -> str:
    """Dev's `canonical_dumps`, verbatim in behaviour: stable key order, compact separators, non-ASCII as-is."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def dev_identity(summary: dict[str, Any]) -> str:
    """Dev's `admission_identity`: SHA-256 of the canonical summary, revision included."""
    return hashlib.sha256(dev_canonical_dumps(summary).encode("utf-8")).hexdigest()


def dev_summary(**overrides: Any) -> dict[str, Any]:
    """`AdmissionSummary.to_dict()` for one admission, with Dev's fixed labels and member names."""
    body: dict[str, Any] = {
        "revision": 1,
        "adapter": "dev-task-admission",
        "disposition": "draft-for-review",
        "executionState": "not-authorized",
        "bindingStatus": "declared-not-verified",
        "scopeStatus": "requested-not-granted",
        "roleStatus": "declared-not-authenticated",
        "outcomeObjective": OBJECTIVE,
        "appContext": {"appId": "app-risk-review", "surfaceId": "surface-review"},
        "declaredBindings": {
            "projectId": ALPHA,
            "workspaceId": WS,
            "workId": "work-1",
            "sourceTarget": "service/core",
            "sourceRevision": "r1",
            "expectedContextGeneration": "ctxgen-1",
        },
        "declaredRoles": {"owner": OWNER, "executor": MEMBER, "reviewer": REVIEWER},
        "assumptions": ["The export is current.", "Dépendances à jour ✓"],
        "constraints": ["Read only.", "Ne rien écrire :   séparateur"],
        "requestedScopes": ["prepare", "read"],
        "budgets": {
            "tokenBudget": 4000,
            "byteBudget": 16000,
            "tokenEstimator": "utf8-ceil4-v1",
        },
    }
    body.update(overrides)
    return body


def dev_admission(summary: dict[str, Any]) -> dict[str, Any]:
    """The admission envelope the Dev producer hands Core: the summary and its identity."""
    return {"summary": summary, "identity": dev_identity(summary)}


def test_a_dev_summary_is_admitted_stored_and_read_back_equal(harness: Harness) -> None:
    switch(harness, ALPHA)
    export_id = export_default(harness)
    admission = dev_admission(dev_summary())

    created = harness.ok(
        OPERATION_OUTCOME_CREATE,
        {"objective": OBJECTIVE, "export_id": export_id, "admission": admission},
        principal=OWNER,
    )
    read = harness.ok(
        OPERATION_OUTCOME_READ, {"outcome_request_id": created["outcome_request_id"]}
    )

    assert created["admission"] == admission
    assert created["admission"]["summary"] == dev_summary()
    assert created["context_generation"] == "ctxgen-1"
    assert read == created


def test_the_admission_identity_is_the_dev_identity_and_binds_the_generation(
    harness: Harness,
) -> None:
    switch(harness, ALPHA)
    export_id = export_default(harness)
    admission = dev_admission(dev_summary())

    created = harness.ok(
        OPERATION_OUTCOME_CREATE,
        {"objective": OBJECTIVE, "export_id": export_id, "admission": admission},
        principal=OWNER,
    )

    assert created["admission"]["identity"] == dev_identity(dev_summary())
    assert len(created["admission"]["identity"]) == 64


@pytest.mark.parametrize(
    ("label", "summary"),
    [
        pytest.param("plain", dev_summary(), id="plain"),
        pytest.param(
            "unicode-and-controls",
            dev_summary(outcomeObjective='naïve 😀 \x01 \x7f   "\\\n'),
            id="unicode-and-controls",
        ),
        pytest.param(
            "scopes-single",
            dev_summary(requestedScopes=["execute"]),
            id="scopes-single",
        ),
        pytest.param(
            "budgets-max-byte",
            dev_summary(
                budgets={
                    "tokenBudget": 4000000,
                    "byteBudget": 16777216,
                    "tokenEstimator": "utf8-ceil4-v1",
                }
            ),
            id="budgets-max-byte",
        ),
        pytest.param(
            "revision-large",
            dev_summary(revision=9007199254740991),
            id="revision-large",
        ),
    ],
)
def test_core_canonical_json_is_byte_identical_to_dev_canonical_dumps(
    label: str, summary: dict[str, Any]
) -> None:
    # Identity is SHA-256 over the canonical text. Core's canonical JSON must match Dev's byte for byte, or a Dev identity
    # would be refused. Each case is a shape Dev can emit: escapes, non-ASCII text, and the largest admissible values.
    assert to_canonical_json(summary) == dev_canonical_dumps(summary), label
    assert hashlib.sha256(
        to_canonical_json(summary).encode("utf-8")
    ).hexdigest() == dev_identity(summary)


def test_the_invented_snake_case_summary_is_refused_at_the_surface(
    harness: Harness,
) -> None:
    switch(harness, ALPHA)
    export_id = export_default(harness)
    invented = {
        "revision": "omnivia.outcome-admission.v1",
        "objective": OBJECTIVE,
        "initiating_app": {"kind": "app", "app_id": "app-risk-review"},
        "workspace_id": WS,
        "project_id": ALPHA,
        "work_id": "work-1",
        "source_target": "service/core",
        "source_revision": "r1",
        "context_generation": "ctxgen-1",
        "roles": {"owner": OWNER, "executor": MEMBER, "reviewer": REVIEWER},
        "assumptions": [],
        "constraints": [],
        "requested_scopes": ["read"],
        "token_budget": 4000,
        "byte_budget": 16000,
    }

    response = harness.call(
        OPERATION_OUTCOME_CREATE,
        {
            "objective": OBJECTIVE,
            "export_id": export_id,
            "admission": dev_admission(invented),
        },
        principal=OWNER,
    )

    assert response.to_wire()["error"]["code"] == "invalid_request"
