"""C08: the active Core Project per Workspace and structured outcome admission, through the production surface.

Every behaviour below is driven through `ProductionApplicationSurface.dispatch_for_session`, composed by
`service.main` over a real migrated workspace with a real `OutcomeAdmissionAuthority`. The principal is the
session's and the workspace is the envelope's. A payload that names either, or a generation, is refused as an
unknown key. Each refusal is checked for its stable code and for no new outcome row, so nothing reached storage.

The domain rules and the storage are covered by `test_outcome_admission_request.py`. This module checks the
chain around them.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_dev_req_081_knowledge_sharing as c16
import test_knowledge_project_authority as kpa
import test_managed_start as tms
import test_task_context_outcomes as tc
from omnivia_core_runtime.ownership.identity import SystemClock
from omnivia_core_runtime.service import knowledge_projects
from omnivia_core_runtime.service import main as service_main
from omnivia_core_runtime.service.application import (
    ProductionApplicationSurface,
    build_installation_application_dispatcher,
)
from omnivia_core_runtime.service.authorization import Grant
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers.task_context import (
    OPERATION_OUTCOME_CREATE,
    OPERATION_OUTCOME_READ,
    OPERATION_PROJECT_CONTEXT_READ,
    OPERATION_PROJECT_CONTEXT_SWITCH,
)
from omnivia_core_runtime.service.knowledge_sharing import NO_PROJECTS
from omnivia_core_runtime.service.main import _build_production_application_surface
from omnivia_core_runtime.service.operations import SERVICE_OPERATIONS
from omnivia_core_runtime.service.outcome_admission import (
    ACTION_READ,
    NO_OUTCOME_ADMISSIONS,
    AccountableRoles,
    AdmissionProjectBinding,
    AdmissionSourceBinding,
    AdmissionWorkBinding,
    OutcomeAdmissionAuthority,
)
from test_managed_start import home  # noqa: F401  (fixture re-export)
from test_task_context_production import Harness as _TaskContextHarness
from test_task_context_production import restart

from omnivia_core.contracts.v1 import SuccessResponseEnvelope, to_canonical_json

WS = c16.WS
OWNER = "owner-alpha"
MEMBER = "member-alpha"
REVIEWER = "reviewer-alpha"
BETA_OWNER = "owner-beta"
BETA_MEMBER = "member-beta"
OUTSIDER = "outsider"
ALPHA = "project-alpha"
BETA = "project-beta"
PAUSED = "project-paused"
OBJECTIVE = "Summarise the open risks"
APP = "app-risk-review"

ADMISSION = OutcomeAdmissionAuthority.of(
    [
        AdmissionProjectBinding(
            project_id=ALPHA,
            lifecycle="active",
            owners=frozenset({OWNER}),
            members=frozenset({MEMBER, REVIEWER}),
            works=(
                AdmissionWorkBinding(
                    "work-1",
                    (AdmissionSourceBinding("service/core", ("r1", "r2")),),
                ),
            ),
            requested_scopes=("read", "prepare"),
            roles=AccountableRoles(
                owner=frozenset({OWNER}),
                executor=frozenset({MEMBER}),
                reviewer=frozenset({REVIEWER}),
            ),
        ),
        AdmissionProjectBinding(
            project_id=BETA,
            lifecycle="active",
            owners=frozenset({BETA_OWNER}),
            members=frozenset({BETA_MEMBER}),
            works=(
                AdmissionWorkBinding(
                    "work-2", (AdmissionSourceBinding("service/other", ("r9",)),)
                ),
            ),
            requested_scopes=("read",),
            roles=AccountableRoles(
                owner=frozenset({BETA_OWNER}),
                executor=frozenset({BETA_MEMBER}),
                reviewer=frozenset({BETA_OWNER}),
            ),
        ),
        AdmissionProjectBinding(
            project_id=PAUSED,
            lifecycle="paused",
            owners=frozenset({OWNER}),
            members=frozenset({MEMBER, REVIEWER}),
            works=(
                AdmissionWorkBinding(
                    "work-3", (AdmissionSourceBinding("service/paused", ("r1",)),)
                ),
            ),
            requested_scopes=("read",),
            roles=AccountableRoles(
                owner=frozenset({OWNER}),
                executor=frozenset({MEMBER}),
                reviewer=frozenset({REVIEWER}),
            ),
        ),
    ]
)

LEGACY_RESULT_KEYS = frozenset(
    {
        "outcome_request_id",
        "export_id",
        "source_handoff_identity",
        "requested_by",
        "objective",
        "status",
        "fencing_generation",
        "created_at",
    }
)


_BINDING_FLAT: dict[str, str] = {
    "project_id": "projectId",
    "workspace_id": "workspaceId",
    "work_id": "workId",
    "source_target": "sourceTarget",
    "source_revision": "sourceRevision",
    "context_generation": "expectedContextGeneration",
}


def summary(**overrides: Any) -> dict[str, Any]:
    """Dev's exact admission summary for the default alpha export. Flat keywords override one declared fact or
    budget, and any other keyword replaces the member of that name, so a test can break one member at a time."""
    body: dict[str, Any] = {
        "revision": 1,
        "adapter": "dev-task-admission",
        "disposition": "draft-for-review",
        "executionState": "not-authorized",
        "bindingStatus": "declared-not-verified",
        "scopeStatus": "requested-not-granted",
        "roleStatus": "declared-not-authenticated",
        "outcomeObjective": OBJECTIVE,
        "appContext": {"appId": APP, "surfaceId": "surface-review"},
        "declaredBindings": {
            "projectId": ALPHA,
            "workspaceId": WS,
            "workId": "work-1",
            "sourceTarget": "service/core",
            "sourceRevision": "r1",
            "expectedContextGeneration": "ctxgen-1",
        },
        "declaredRoles": {"owner": OWNER, "executor": MEMBER, "reviewer": REVIEWER},
        "assumptions": ["The export is current."],
        "constraints": ["Read only."],
        "requestedScopes": ["prepare", "read"],
        "budgets": {
            "tokenBudget": 4000,
            "byteBudget": 16000,
            "tokenEstimator": "utf8-ceil4-v1",
        },
    }
    for key, value in overrides.items():
        if key in _BINDING_FLAT:
            body["declaredBindings"][_BINDING_FLAT[key]] = value
        elif key == "roles":
            body["declaredRoles"] = value
        elif key == "token_budget":
            body["budgets"]["tokenBudget"] = value
        elif key == "byte_budget":
            body["budgets"]["byteBudget"] = value
        elif key == "objective":
            body["outcomeObjective"] = value
        elif key == "requested_scopes":
            body["requestedScopes"] = value
        elif key == "initiating_app":
            body["appContext"] = value
        else:
            body[key] = value
    return body


def claimed(body: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """A structured admission whose identity is the one its summary recomputes to, unless overridden."""
    identity = hashlib.sha256(to_canonical_json(body).encode("utf-8")).hexdigest()
    entry: dict[str, Any] = {"summary": body, "identity": identity}
    entry.update(overrides)
    return entry


def _surface(
    holder: Any, admission: OutcomeAdmissionAuthority
) -> ProductionApplicationSurface:
    """The production surface for one workspace, composed with `admission` as its only Project authority."""
    probe = Dispatcher.for_service_operations(
        Grant(
            principal=c16.PRINCIPAL,
            workspaces=frozenset({WS}),
            operations=frozenset(SERVICE_OPERATIONS),
        ),
        holder,
    )
    started = SimpleNamespace(**vars(holder), workspace_id=WS, clock=SystemClock())
    installation = build_installation_application_dispatcher(
        service=c16._InstallationService(),  # type: ignore[arg-type]
        principal_id=c16.PRINCIPAL,
        fallback=probe,
    )
    return _build_production_application_surface(
        started=started,  # type: ignore[arg-type]
        probe=probe,
        installation=installation,
        admission_authority=admission,
    )


class Harness(_TaskContextHarness):
    """The task-context harness, composed with the admission authority under test."""

    def __init__(
        self, holder: Any, admission: OutcomeAdmissionAuthority = ADMISSION
    ) -> None:
        self.holder = holder
        self.surface = _surface(holder, admission)


@pytest.fixture
def harness(owned: Any) -> Harness:
    return Harness(owned)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    m1.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WS)
    holder = m1.take_ownership(path, workspace_id=WS)
    yield holder
    holder.connection.close()


def switch(
    harness: Harness, project_id: str, *, principal: str = OWNER
) -> dict[str, Any]:
    return harness.ok(
        OPERATION_PROJECT_CONTEXT_SWITCH,
        {"project_id": project_id},
        principal=principal,
    )


def switch_refused(harness: Harness, project_id: str, *, principal: str = OWNER) -> str:
    return harness.code(
        OPERATION_PROJECT_CONTEXT_SWITCH,
        {"project_id": project_id},
        principal=principal,
    )


def read_context(harness: Harness, *, principal: str = OWNER) -> dict[str, Any]:
    return harness.ok(OPERATION_PROJECT_CONTEXT_READ, {}, principal=principal)


def create_structured(
    harness: Harness,
    export_id: str,
    admission_payload: dict[str, Any],
    *,
    objective: str = OBJECTIVE,
    principal: str = OWNER,
    key: str | None = None,
) -> Any:
    return harness.call(
        OPERATION_OUTCOME_CREATE,
        {
            "objective": objective,
            "export_id": export_id,
            "admission": admission_payload,
        },
        principal=principal,
        key=key,
    )


def structured_code(
    harness: Harness, export_id: str, admission_payload: dict[str, Any], **kwargs: Any
) -> str:
    return str(
        create_structured(harness, export_id, admission_payload, **kwargs).error.code
    )


def outcome_rows(harness: Harness) -> int:
    return harness.count("omnivia_outcome_requests")


def run_or_effect_rows(harness: Harness) -> dict[str, int]:
    """Row counts of any table this slice could wrongly write to if it started a Run or an effect."""
    names = [
        row[0]
        for row in harness.holder.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND (name LIKE '%run%' OR name LIKE '%effect%')"
        ).fetchall()
    ]
    return {name: harness.count(name) for name in names}


# -- the chain at bootstrap ---------------------------------------------------------------------


def test_the_task_context_family_serves_the_two_project_context_operations(
    harness: Harness,
) -> None:
    assert {OPERATION_PROJECT_CONTEXT_READ, OPERATION_PROJECT_CONTEXT_SWITCH} <= set(
        harness.surface.registry.operations
    )


def test_an_empty_admission_authority_admits_no_project(owned: Any) -> None:
    empty = Harness(owned, NO_OUTCOME_ADMISSIONS)
    assert switch_refused(empty, ALPHA) == "not_found"
    assert read_context(empty)["state"] == "none"


# -- the active Project -------------------------------------------------------------------------


def test_no_project_is_active_until_one_is_chosen(harness: Harness) -> None:
    assert read_context(harness) == {"state": "none"}
    assert harness.count("omnivia_project_contexts") == 0


def test_the_first_choice_is_generation_one_and_the_read_returns_it(
    harness: Harness,
) -> None:
    result = switch(harness, ALPHA)

    assert result == {
        "state": "active",
        "project_id": ALPHA,
        "context_generation": "ctxgen-1",
    }
    assert read_context(harness) == result
    assert harness.count("omnivia_project_contexts") == 1


def test_choosing_the_active_project_again_is_a_no_op_that_keeps_its_generation(
    harness: Harness,
) -> None:
    switch(harness, ALPHA)
    row_before = harness.holder.connection.execute(
        "SELECT * FROM omnivia_project_contexts"
    ).fetchall()

    again = switch(harness, ALPHA)

    assert again["context_generation"] == "ctxgen-1"
    assert (
        harness.holder.connection.execute(
            "SELECT * FROM omnivia_project_contexts"
        ).fetchall()
        == row_before
    )


def test_choosing_a_different_project_advances_the_generation_by_one(
    harness: Harness,
) -> None:
    switch(harness, ALPHA)
    beta = switch(harness, BETA, principal=BETA_OWNER)
    alpha_again = switch(harness, ALPHA)

    assert beta["context_generation"] == "ctxgen-2"
    assert alpha_again["context_generation"] == "ctxgen-3"
    assert read_context(harness) == alpha_again
    assert harness.count("omnivia_project_contexts") == 1


def test_the_active_project_survives_a_restart_at_its_generation(
    harness: Harness,
) -> None:
    switch(harness, ALPHA)
    switch(harness, BETA, principal=BETA_OWNER)
    holder = restart(harness.holder)
    try:
        after = Harness(holder)
        assert read_context(after) == {
            "state": "active",
            "project_id": BETA,
            "context_generation": "ctxgen-2",
        }
    finally:
        holder.connection.close()


def test_an_unknown_project_is_refused_and_leaves_the_context_unchanged(
    harness: Harness,
) -> None:
    assert switch_refused(harness, "project-zeta") == "not_found"
    assert read_context(harness) == {"state": "none"}
    assert harness.count("omnivia_project_contexts") == 0


def test_a_principal_who_is_not_an_owner_or_member_cannot_choose_the_project(
    harness: Harness,
) -> None:
    switch(harness, ALPHA)

    assert switch_refused(harness, BETA, principal=OUTSIDER) == "authorization_denied"
    assert read_context(harness)["context_generation"] == "ctxgen-1"


def test_the_switch_payload_cannot_name_a_generation_or_a_principal(
    harness: Harness,
) -> None:
    for extra in (
        {"context_generation": "ctxgen-9"},
        {"principal": OWNER},
        {"workspace_id": "ws-x"},
    ):
        assert (
            harness.code(
                OPERATION_PROJECT_CONTEXT_SWITCH, {"project_id": ALPHA, **extra}
            )
            == "invalid_request"
        )
    assert read_context(harness) == {"state": "none"}


# -- structured admission -----------------------------------------------------------------------


def export_default(harness: Harness) -> str:
    return str(harness.export(tc.handoff())["export_id"])


def test_a_structured_request_is_admitted_and_read_back_with_its_identity(
    harness: Harness,
) -> None:
    switch(harness, ALPHA)
    export_id = export_default(harness)
    body = summary()
    payload = claimed(body)

    created = harness.ok(
        OPERATION_OUTCOME_CREATE,
        {"objective": OBJECTIVE, "export_id": export_id, "admission": payload},
        principal=OWNER,
    )
    read = harness.ok(
        OPERATION_OUTCOME_READ, {"outcome_request_id": created["outcome_request_id"]}
    )

    assert created["admission"] == payload
    assert created["context_generation"] == "ctxgen-1"
    assert read == created
    assert set(created) == LEGACY_RESULT_KEYS | {"admission", "context_generation"}
    assert outcome_rows(harness) == 1


def test_a_legacy_request_is_unchanged_and_carries_no_admission_fields(
    harness: Harness,
) -> None:
    export_id = export_default(harness)

    created = harness.ok(
        OPERATION_OUTCOME_CREATE, {"objective": OBJECTIVE, "export_id": export_id}
    )

    assert set(created) == LEGACY_RESULT_KEYS
    assert (
        harness.ok(
            OPERATION_OUTCOME_READ,
            {"outcome_request_id": created["outcome_request_id"]},
        )
        == created
    )


def test_a_structured_request_can_be_made_by_a_member_who_is_not_a_declared_role(
    harness: Harness,
) -> None:
    # Membership is what the caller needs. The declared roles are claims about who acts, not who may ask.
    switch(harness, ALPHA)
    export_id = export_default(harness)

    response = create_structured(
        harness, export_id, claimed(summary()), principal=MEMBER
    )

    assert isinstance(response, SuccessResponseEnvelope), response
    assert response.to_wire()["result"]["requested_by"] == MEMBER


# -- refusals, each leaving no outcome row and starting nothing -----------------------------


REFUSALS: list[tuple[str, str, str | None, Any]] = []


def _refusal(name: str, code: str, active: str | None = ALPHA):
    """Register one refusal: the stable code, the Project to make active first (or none), and how to build it."""

    def decorate(build):
        REFUSALS.append((name, code, active, build))
        return build

    return decorate


@_refusal("objective differs from the request", "invalid_request")
def _objective(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return (
        export_default(harness),
        claimed(summary()),
        {"objective": "A different objective"},
    )


@_refusal("summary carries an extra member", "invalid_request")
def _extra(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return export_default(harness), claimed(summary(unexpected="x")), {}


@_refusal("revision is not the one this build accepts", "invalid_request")
def _revision(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return (
        export_default(harness),
        claimed(summary(revision="omnivia.outcome-admission.v0")),
        {},
    )


@_refusal("initiating context is not an App", "invalid_request")
def _app_kind(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return (
        export_default(harness),
        claimed(summary(initiating_app={"kind": "user", "app_id": APP})),
        {},
    )


@_refusal("claimed identity does not verify the summary", "invalid_request")
def _identity(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return export_default(harness), claimed(summary(), identity="0" * 64), {}


@_refusal("token budget is outside its bounds", "invalid_request")
def _budget(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return export_default(harness), claimed(summary(token_budget=0)), {}


@_refusal("requested scopes are empty", "invalid_request")
def _no_scopes(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return export_default(harness), claimed(summary(requested_scopes=[])), {}


@_refusal("requested scopes are out of ascending order", "invalid_request")
def _scope_order(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return (
        export_default(harness),
        claimed(summary(requested_scopes=["read", "prepare"])),
        {},
    )


@_refusal("a scope the Project does not admit", "invalid_request")
def _scope_policy(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return (
        export_default(harness),
        claimed(summary(requested_scopes=["read", "execute"])),
        {},
    )


@_refusal("one principal declared for two roles", "invalid_request")
def _duplicate_role(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    roles = {"owner": OWNER, "executor": OWNER, "reviewer": REVIEWER}
    return export_default(harness), claimed(summary(roles=roles)), {}


@_refusal("an executor who is not assigned to that role", "invalid_request")
def _misassigned(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    roles = {"owner": OWNER, "executor": BETA_MEMBER, "reviewer": REVIEWER}
    return export_default(harness), claimed(summary(roles=roles)), {}


@_refusal("the context token is malformed", "invalid_request")
def _token(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return export_default(harness), claimed(summary(context_generation="ctxgen-01")), {}


@_refusal("the assumptions list is over its bound", "invalid_request")
def _assumptions(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return export_default(harness), claimed(summary(assumptions=["a"] * 17)), {}


@_refusal("the declared Workspace is another Workspace", "not_found")
def _workspace(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return export_default(harness), claimed(summary(workspace_id="ws-elsewhere")), {}


@_refusal("the declared Project is not bound", "not_found")
def _unknown_project(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return export_default(harness), claimed(summary(project_id="project-zeta")), {}


@_refusal("the Work is not in the Project", "not_found")
def _unknown_work(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return export_default(harness), claimed(summary(work_id="work-9")), {}


@_refusal("the source revision is not bound to the target", "not_found")
def _unknown_revision(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    export = harness.export(tc.handoff(revision="r7"))
    return str(export["export_id"]), claimed(summary(source_revision="r7")), {}


@_refusal("the source target is not bound to the Work", "not_found")
def _unknown_source(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    export = harness.export(tc.handoff(target="service/zzz"))
    return str(export["export_id"]), claimed(summary(source_target="service/zzz")), {}


@_refusal("the export names another target than the summary", "conflict")
def _export_target(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    export = harness.export(tc.handoff(target="service/other"))
    return str(export["export_id"]), claimed(summary()), {}


@_refusal("the export names another revision than the summary", "conflict")
def _export_revision(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    export = harness.export(tc.handoff(revision="r2"))
    return str(export["export_id"]), claimed(summary()), {}


@_refusal("the export names another Project than the summary", "conflict")
def _export_project(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    export = harness.export(tc.handoff(project=BETA))
    return str(export["export_id"]), claimed(summary()), {}


@_refusal("the Project is closed for submission", "conflict", active=PAUSED)
def _paused(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    export = harness.export(
        tc.handoff(project=PAUSED, target="service/paused", revision="r1")
    )
    body = summary(
        project_id=PAUSED,
        work_id="work-3",
        source_target="service/paused",
        source_revision="r1",
        context_generation="ctxgen-1",
        roles={"owner": OWNER, "executor": MEMBER, "reviewer": REVIEWER},
        requested_scopes=["read"],
    )
    return str(export["export_id"]), claimed(body), {}


@_refusal("no Project is active yet", "conflict", active=None)
def _no_active(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return export_default(harness), claimed(summary()), {}


@_refusal("a different Project is active", "conflict", active=BETA)
def _other_active(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return export_default(harness), claimed(summary()), {}


@_refusal("the caller is not an owner or member", "authorization_denied")
def _outsider(harness: Harness) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return export_default(harness), claimed(summary()), {"principal": OUTSIDER}


@pytest.mark.parametrize(
    ("name", "code", "active", "build"), REFUSALS, ids=[r[0] for r in REFUSALS]
)
def test_a_refused_structured_request_stores_nothing_and_starts_nothing(
    harness: Harness, name: str, code: str, active: str | None, build: Any
) -> None:
    if active is not None:
        switch(harness, active, principal=BETA_OWNER if active == BETA else OWNER)
    export_id, body, overrides = build(harness)
    principal = overrides.pop("principal", OWNER)
    objective = overrides.pop("objective", OBJECTIVE)
    rows_before = outcome_rows(harness)
    effects_before = run_or_effect_rows(harness)

    response = create_structured(
        harness, export_id, body, objective=objective, principal=principal
    )

    assert response.error.code == code, (name, response)
    assert outcome_rows(harness) == rows_before
    assert run_or_effect_rows(harness) == effects_before


def test_a_structured_request_made_under_an_earlier_generation_is_refused_after_a_switch(
    harness: Harness,
) -> None:
    switch(harness, ALPHA)
    export_id = export_default(harness)
    stale = claimed(summary())
    switch(harness, BETA, principal=BETA_OWNER)
    rows_before = outcome_rows(harness)

    assert structured_code(harness, export_id, stale) == "conflict"
    assert outcome_rows(harness) == rows_before


def test_a_structured_request_names_the_declared_project_only_at_its_own_generation(
    harness: Harness,
) -> None:
    switch(harness, ALPHA)
    switch(harness, BETA, principal=BETA_OWNER)
    switch(harness, ALPHA)
    export_id = export_default(harness)

    assert structured_code(harness, export_id, claimed(summary())) == "conflict"
    fresh = claimed(summary(context_generation="ctxgen-3"))
    assert (
        create_structured(harness, export_id, fresh).to_wire()["result"][
            "context_generation"
        ]
        == "ctxgen-3"
    )


def test_a_legacy_request_is_not_affected_by_the_active_project(
    harness: Harness,
) -> None:
    export_id = export_default(harness)
    switch(harness, BETA, principal=BETA_OWNER)

    created = harness.ok(
        OPERATION_OUTCOME_CREATE, {"objective": OBJECTIVE, "export_id": export_id}
    )

    assert set(created) == LEGACY_RESULT_KEYS


# -- replay -------------------------------------------------------------------------------------


def test_a_replay_under_the_same_key_returns_the_stored_request_without_a_second_write(
    harness: Harness,
) -> None:
    switch(harness, ALPHA)
    export_id = export_default(harness)
    payload = claimed(summary())
    first = harness.ok(
        OPERATION_OUTCOME_CREATE,
        {"objective": OBJECTIVE, "export_id": export_id, "admission": payload},
        key="replay-structured-1",
        principal=OWNER,
    )
    audit_before = harness.audit_events()

    second = harness.ok(
        OPERATION_OUTCOME_CREATE,
        {"objective": OBJECTIVE, "export_id": export_id, "admission": payload},
        key="replay-structured-1",
        principal=OWNER,
    )

    assert second == first
    assert outcome_rows(harness) == 1
    assert harness.audit_events() == audit_before


def test_the_same_key_for_a_different_admission_is_an_idempotency_conflict(
    harness: Harness,
) -> None:
    switch(harness, ALPHA)
    export_id = export_default(harness)
    harness.ok(
        OPERATION_OUTCOME_CREATE,
        {
            "objective": OBJECTIVE,
            "export_id": export_id,
            "admission": claimed(summary()),
        },
        key="reuse-structured-1",
        principal=OWNER,
    )
    changed = claimed(summary(constraints=["Write nothing."]))

    response = create_structured(harness, export_id, changed, key="reuse-structured-1")

    assert response.error.code == "idempotency_conflict"
    assert outcome_rows(harness) == 1


def test_a_replay_is_refused_once_the_caller_loses_standing_in_the_declared_project(
    harness: Harness,
) -> None:
    switch(harness, ALPHA)
    export_id = export_default(harness)
    payload = claimed(summary())
    harness.ok(
        OPERATION_OUTCOME_CREATE,
        {"objective": OBJECTIVE, "export_id": export_id, "admission": payload},
        key="standing-1",
        principal=OWNER,
    )
    # The same database, served by a composition in which the caller is no longer a member of the Project.
    without_standing = OutcomeAdmissionAuthority.of(
        [
            AdmissionProjectBinding(
                project_id=ALPHA,
                lifecycle="active",
                owners=frozenset({"owner-replacement"}),
                members=frozenset({MEMBER, REVIEWER}),
                works=ADMISSION.projects[0].works,
                requested_scopes=("read", "prepare"),
                roles=AccountableRoles(
                    owner=frozenset({"owner-replacement"}),
                    executor=frozenset({MEMBER}),
                    reviewer=frozenset({REVIEWER}),
                ),
            )
        ]
    )
    later = Harness(harness.holder, without_standing)

    response = create_structured(later, export_id, payload, key="standing-1")

    assert response.error.code == "authorization_denied"
    assert outcome_rows(harness) == 1


def test_a_replay_is_refused_once_the_project_is_no_longer_the_active_context(
    harness: Harness,
) -> None:
    switch(harness, ALPHA)
    export_id = export_default(harness)
    payload = claimed(summary())
    harness.ok(
        OPERATION_OUTCOME_CREATE,
        {"objective": OBJECTIVE, "export_id": export_id, "admission": payload},
        key="generation-1",
        principal=OWNER,
    )
    switch(harness, BETA, principal=BETA_OWNER)

    response = create_structured(harness, export_id, payload, key="generation-1")

    assert response.error.code == "conflict"
    assert outcome_rows(harness) == 1


# -- the startup path ---------------------------------------------------------------------------


def _v2_document(workspace_id: str) -> str:
    """One Project in version 2, written the way an operator states it."""
    import json

    return json.dumps(
        {
            "schema": "omnivia.knowledge-projects.v2",
            "projects": [
                {
                    "workspace_id": workspace_id,
                    "project_id": ALPHA,
                    "domain_scope": "product.core",
                    "owners": [OWNER],
                    "members": [MEMBER, REVIEWER],
                    "lifecycle": "active",
                    "works": [
                        {
                            "work_id": "work-1",
                            "sources": [
                                {"target": "service/core", "revisions": ["r1", "r2"]}
                            ],
                        }
                    ],
                    "requested_scopes": ["read", "prepare"],
                    "accountable_roles": {
                        "owner": [OWNER],
                        "executor": [MEMBER],
                        "reviewer": [REVIEWER],
                    },
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("payload", "expects_project"),
    [
        pytest.param("v2", True, id="version-two-document"),
        pytest.param("none", False, id="no-document"),
    ],
)
def test_startup_reads_the_document_once_and_composes_both_authorities(
    home: Path,  # noqa: F811  (the managed-start fixture)
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
    expects_project: bool,
) -> None:
    """One read of the document yields the sharing and the admission authority the surface is composed with."""
    if payload == "v2":
        kpa._place(home, _v2_document(tms.WORKSPACE_ID))
    reads: list[Path] = []
    real_read = knowledge_projects._read

    def counting_read(path: Path) -> bytes | None:
        reads.append(path)
        return real_read(path)

    composed: dict[str, Any] = {}

    def compose_then_stop(**kwargs: Any) -> Any:
        composed.update(kwargs)
        raise kpa._ComposedWithoutServing

    monkeypatch.setattr(knowledge_projects, "_read", counting_read)
    monkeypatch.setattr(
        service_main, "_build_production_application_surface", compose_then_stop
    )
    socket_dir = Path(tempfile.mkdtemp(prefix="pca-", dir="/tmp"))
    try:
        with redirect_stderr(io.StringIO()):
            status = service_main.main(
                [
                    "--workspace",
                    str(home / "workspace"),
                    "--installation-state",
                    str(home / "installation-state"),
                    "--endpoint",
                    f"unix://{socket_dir / 's.sock'}",
                ]
            )
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)

    assert status == 1
    assert len(reads) == 1
    admission = composed["admission_authority"]
    sharing = composed["project_authority"]
    if expects_project:
        bound = admission.project(ALPHA, ACTION_READ)
        assert bound.owners == frozenset({OWNER})
        assert sharing.project(ALPHA) is not None
    else:
        assert admission == NO_OUTCOME_ADMISSIONS
        assert sharing == NO_PROJECTS
