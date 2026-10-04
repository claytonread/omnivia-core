"""C08: structured outcome admission and the active Project context, at the domain, storage and guard layers.

`service/task_context.py` decides what a structured request may be, and `storage/task_context.py` persists it. These
tests drive both directly. Behaviour through the production surface is in `test_project_context_admission.py`.

The guards in migration 0068 are exercised with the same tamperer's connection the existing task-context tests use,
so a row or a context changed outside the service is found by the read, and a write outside the fence is refused by
the database itself.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
from omnivia_core_runtime.service.outcome_admission import (
    NO_OUTCOME_ADMISSIONS,
    AccountableRoles,
    AdmissionProjectBinding,
    AdmissionSourceBinding,
    AdmissionWorkBinding,
    OutcomeAdmissionAuthority,
)
from omnivia_core_runtime.service.task_context import (
    REFUSED_ADMISSION_INVALID,
    REFUSED_ADMISSION_NOT_FOUND,
    REFUSED_BUDGET_INSUFFICIENT,
    REFUSED_BUDGET_INVALID,
    REFUSED_CONTEXT_MISMATCH,
    REFUSED_NOT_FOUND,
    REFUSED_NOT_MEMBER,
    REFUSED_OBJECTIVE_INVALID,
    TaskContextRefused,
    build_outcome_request,
    decide_switch,
    parse_admission,
)
from omnivia_core_runtime.storage.task_context import (
    StoredExport,
    StoredOutcomeRequest,
    StoredProjectContext,
    TaskContextInvalid,
    read_outcome_request,
    read_project_context,
    record_export,
    record_outcome_request,
    record_project_context,
)
from test_task_context_outcomes import (
    PRINCIPAL,
    WS,
    _count,
    _fenced,
    _raw,
    export_of,
    handoff,
)

from omnivia_core.contracts.v1 import to_canonical_json

ALPHA = "project-alpha"
BETA = "project-beta"
OWNER = "owner-alpha"
MEMBER = "member-alpha"
REVIEWER = "reviewer-alpha"
OUTSIDER = "outsider"
OBJECTIVE = "Summarise the open risks"
CREATED = 1_800_000_000_000_000

AUTHORITY = OutcomeAdmissionAuthority.of(
    [
        AdmissionProjectBinding(
            project_id=ALPHA,
            lifecycle="active",
            owners=frozenset({OWNER}),
            members=frozenset({MEMBER, REVIEWER}),
            works=(
                AdmissionWorkBinding(
                    "work-1", (AdmissionSourceBinding("service/core", ("r1", "r2")),)
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
            owners=frozenset({"owner-beta"}),
            members=frozenset(),
            works=(
                AdmissionWorkBinding(
                    "work-2", (AdmissionSourceBinding("service/other", ("r9",)),)
                ),
            ),
            requested_scopes=("read",),
            roles=AccountableRoles(
                owner=frozenset({"owner-beta"}),
                executor=frozenset({"owner-beta"}),
                reviewer=frozenset({"owner-beta"}),
            ),
        ),
    ]
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
    entry: dict[str, Any] = {
        "summary": body,
        "identity": hashlib.sha256(to_canonical_json(body).encode("utf-8")).hexdigest(),
    }
    entry.update(overrides)
    return entry


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    m1.materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WS)
    holder = m1.take_ownership(path, workspace_id=WS)
    yield holder
    holder.connection.close()


def refusal_reason(action: Any) -> str:
    with pytest.raises(TaskContextRefused) as caught:
        action()
    return caught.value.reason


# -- the admission shape ------------------------------------------------------------------------


def test_a_canonical_admission_parses_to_its_declared_facts() -> None:
    parsed = parse_admission(claimed(summary()))

    assert parsed.project_id == ALPHA
    assert parsed.work_id == "work-1"
    assert parsed.source_target == "service/core"
    assert parsed.source_revision == "r1"
    assert parsed.context_generation == 1
    assert (parsed.owner, parsed.executor, parsed.reviewer) == (OWNER, MEMBER, REVIEWER)
    assert parsed.requested_scopes == ("prepare", "read")
    assert (
        parsed.identity
        == hashlib.sha256(to_canonical_json(summary()).encode()).hexdigest()
    )


@pytest.mark.parametrize(
    ("admission", "reason"),
    [
        pytest.param(
            {"summary": summary()}, REFUSED_ADMISSION_INVALID, id="identity-missing"
        ),
        pytest.param(
            claimed(summary(), extra="x"),
            REFUSED_ADMISSION_INVALID,
            id="entry-extra-member",
        ),
        pytest.param(
            claimed({**summary(), "extra": 1}),
            REFUSED_ADMISSION_INVALID,
            id="summary-extra-member",
        ),
        pytest.param(
            {**claimed(summary()), "identity": "0" * 63},
            REFUSED_ADMISSION_INVALID,
            id="identity-short",
        ),
        pytest.param(
            claimed(summary(), identity="A" * 64),
            REFUSED_ADMISSION_INVALID,
            id="identity-not-lowercase-hex",
        ),
        pytest.param(
            claimed(summary(), identity=hashlib.sha256(b"something else").hexdigest()),
            REFUSED_ADMISSION_INVALID,
            id="identity-does-not-verify",
        ),
        pytest.param(
            claimed(summary(revision=0)),
            REFUSED_ADMISSION_INVALID,
            id="revision-below-one",
        ),
        pytest.param(
            claimed(
                summary(
                    initiating_app={"kind": "user", "appId": "app-x", "surfaceId": "s"}
                )
            ),
            REFUSED_ADMISSION_INVALID,
            id="initiating-kind",
        ),
        pytest.param(
            claimed(summary(initiating_app={"appId": "bad id!", "surfaceId": "s"})),
            REFUSED_ADMISSION_INVALID,
            id="app-identifier",
        ),
        pytest.param(
            claimed(summary(workspace_id="bad workspace")),
            REFUSED_ADMISSION_INVALID,
            id="workspace",
        ),
        pytest.param(
            claimed(summary(source_target="   ")),
            REFUSED_ADMISSION_INVALID,
            id="target-blank",
        ),
        pytest.param(
            claimed(summary(source_target="s" * 513)),
            REFUSED_ADMISSION_INVALID,
            id="target-over-bound",
        ),
        pytest.param(
            claimed(summary(context_generation="ctxgen-0")),
            REFUSED_ADMISSION_INVALID,
            id="generation-zero",
        ),
        pytest.param(
            claimed(summary(context_generation="ctxgen-01")),
            REFUSED_ADMISSION_INVALID,
            id="generation-padded",
        ),
        pytest.param(
            claimed(summary(context_generation="ctxgen-9223372036854775808")),
            REFUSED_ADMISSION_INVALID,
            id="generation-over-range",
        ),
        pytest.param(
            claimed(summary(context_generation=1)),
            REFUSED_ADMISSION_INVALID,
            id="generation-not-text",
        ),
        pytest.param(
            claimed(summary(assumptions=["a"] * 17)),
            REFUSED_ADMISSION_INVALID,
            id="assumptions-count",
        ),
        pytest.param(
            claimed(summary(constraints=["x" * 513])),
            REFUSED_ADMISSION_INVALID,
            id="constraint-bytes",
        ),
        pytest.param(
            claimed(summary(requested_scopes=[])),
            REFUSED_ADMISSION_INVALID,
            id="scopes-empty",
        ),
        pytest.param(
            claimed(summary(requested_scopes=["read", "read"])),
            REFUSED_ADMISSION_INVALID,
            id="scopes-repeat",
        ),
        pytest.param(
            claimed(summary(requested_scopes=["read", "execute"])),
            REFUSED_ADMISSION_INVALID,
            id="scopes-not-canonical",
        ),
        pytest.param(
            claimed(summary(requested_scopes=["admin"])),
            REFUSED_ADMISSION_INVALID,
            id="scope-unknown",
        ),
        pytest.param(
            claimed(summary(token_budget=True)),
            REFUSED_BUDGET_INVALID,
            id="token-budget-bool",
        ),
        pytest.param(
            claimed(summary(byte_budget=0)),
            REFUSED_BUDGET_INVALID,
            id="byte-budget-zero",
        ),
        pytest.param(
            claimed(summary(objective="")),
            REFUSED_OBJECTIVE_INVALID,
            id="objective-empty",
        ),
        pytest.param(
            claimed(summary(roles={"owner": OWNER, "executor": MEMBER})),
            REFUSED_ADMISSION_INVALID,
            id="roles-short",
        ),
        pytest.param(
            claimed(
                summary(roles={"owner": OWNER, "executor": OWNER, "reviewer": REVIEWER})
            ),
            REFUSED_ADMISSION_INVALID,
            id="roles-duplicate",
        ),
        pytest.param(
            claimed(
                summary(
                    roles={
                        "owner": OWNER,
                        "executor": MEMBER,
                        "reviewer": REVIEWER,
                        "extra": "x",
                    }
                )
            ),
            REFUSED_ADMISSION_INVALID,
            id="roles-extra-member",
        ),
        pytest.param(
            claimed(summary(assumptions=["x" * 512] * 5, constraints=["y" * 512] * 4)),
            REFUSED_ADMISSION_INVALID,
            id="list-aggregate-bytes",
        ),
        pytest.param(
            claimed(summary(assumptions=[" "])),
            REFUSED_ADMISSION_INVALID,
            id="assumption-blank",
        ),
        pytest.param(
            claimed(summary(requested_scopes=["read", "read"])),
            REFUSED_ADMISSION_INVALID,
            id="scopes-duplicate-sorted-form",
        ),
        pytest.param(
            claimed(summary(requested_scopes=["read", "prepare"])),
            REFUSED_ADMISSION_INVALID,
            id="scopes-not-ascending",
        ),
        pytest.param(
            claimed(
                summary(
                    budgets={
                        "tokenBudget": 4000,
                        "byteBudget": 16000,
                        "tokenEstimator": "utf8-bytes-ceil-div-4-v1",
                    }
                )
            ),
            REFUSED_ADMISSION_INVALID,
            id="estimator-wrong",
        ),
        pytest.param(
            claimed(summary(budgets={"tokenBudget": 4000, "byteBudget": 16000})),
            REFUSED_ADMISSION_INVALID,
            id="budgets-estimator-missing",
        ),
        pytest.param(
            claimed(summary(token_budget=1)),
            REFUSED_BUDGET_INSUFFICIENT,
            id="token-budget-insufficient",
        ),
        pytest.param(
            claimed(summary(byte_budget=20)),
            REFUSED_BUDGET_INSUFFICIENT,
            id="byte-budget-insufficient",
        ),
        pytest.param(
            claimed(summary(byte_budget=16777217)),
            REFUSED_BUDGET_INVALID,
            id="byte-budget-over-dev-ceiling",
        ),
        pytest.param(
            claimed(summary(outcomeObjective=None)),
            REFUSED_OBJECTIVE_INVALID,
            id="objective-not-text",
        ),
        pytest.param(
            claimed(summary(disposition="accepted")),
            REFUSED_ADMISSION_INVALID,
            id="label-disposition",
        ),
        pytest.param(
            claimed(summary(executionState="authorized")),
            REFUSED_ADMISSION_INVALID,
            id="label-execution-state",
        ),
        pytest.param(
            claimed(summary(bindingStatus="verified")),
            REFUSED_ADMISSION_INVALID,
            id="label-binding-status",
        ),
        pytest.param(
            claimed(summary(scopeStatus="granted")),
            REFUSED_ADMISSION_INVALID,
            id="label-scope-status",
        ),
        pytest.param(
            claimed(summary(roleStatus="authenticated")),
            REFUSED_ADMISSION_INVALID,
            id="label-role-status",
        ),
        pytest.param(
            claimed(summary(adapter="dev-task-admission-v0")),
            REFUSED_ADMISSION_INVALID,
            id="label-adapter",
        ),
        pytest.param(
            claimed(
                {
                    **summary(),
                    "declaredBindings": {**summary()["declaredBindings"], "extra": "x"},
                }
            ),
            REFUSED_ADMISSION_INVALID,
            id="bindings-extra-member",
        ),
        pytest.param(
            claimed(
                {
                    **summary(),
                    "declaredBindings": {
                        k: v
                        for k, v in summary()["declaredBindings"].items()
                        if k != "workId"
                    },
                }
            ),
            REFUSED_ADMISSION_INVALID,
            id="bindings-missing-member",
        ),
        pytest.param(
            claimed(
                {
                    **summary(),
                    "appContext": {
                        "appId": "app-risk-review",
                        "surfaceId": "s",
                        "kind": "app",
                    },
                }
            ),
            REFUSED_ADMISSION_INVALID,
            id="app-context-extra-member",
        ),
        pytest.param(
            claimed(
                {
                    **summary(),
                    "budgets": {
                        "tokenBudget": 4000,
                        "byteBudget": 16000,
                        "tokenEstimator": "utf8-ceil4-v1",
                        "extra": 1,
                    },
                }
            ),
            REFUSED_ADMISSION_INVALID,
            id="budgets-extra-member",
        ),
        pytest.param("not a mapping", REFUSED_ADMISSION_INVALID, id="not-a-mapping"),
    ],
)
def test_a_malformed_admission_is_refused_before_anything_is_stored(
    admission: Any, reason: str
) -> None:
    assert refusal_reason(lambda: parse_admission(admission)) == reason


# -- the active Project decision -----------------------------------------------------------------


def test_the_first_choice_is_generation_one_for_a_member() -> None:
    chosen = decide_switch(
        workspace_id=WS,
        principal=OWNER,
        project_id=ALPHA,
        current=None,
        authority=AUTHORITY,
        fencing_generation=3,
        switched_at_us=CREATED,
    )

    assert chosen == StoredProjectContext(WS, ALPHA, 1, 3, OWNER, CREATED)
    assert chosen.token == "ctxgen-1"


def test_choosing_the_active_project_returns_the_same_context_object() -> None:
    current = StoredProjectContext(WS, ALPHA, 4, 3, OWNER, CREATED)

    assert (
        decide_switch(
            workspace_id=WS,
            principal=OWNER,
            project_id=ALPHA,
            current=current,
            authority=AUTHORITY,
            fencing_generation=9,
            switched_at_us=CREATED + 1,
        )
        is current
    )


def test_a_change_to_another_project_advances_by_exactly_one() -> None:
    current = StoredProjectContext(WS, ALPHA, 4, 3, OWNER, CREATED)

    moved = decide_switch(
        workspace_id=WS,
        principal="owner-beta",
        project_id=BETA,
        current=current,
        authority=AUTHORITY,
        fencing_generation=3,
        switched_at_us=CREATED + 1,
    )

    assert moved.context_generation == 5
    assert moved.project_id == BETA
    assert moved.switched_by == "owner-beta"


def test_a_project_the_workspace_does_not_bind_is_not_found() -> None:
    assert (
        refusal_reason(
            lambda: decide_switch(
                workspace_id=WS,
                principal=OWNER,
                project_id="project-zeta",
                current=None,
                authority=AUTHORITY,
                fencing_generation=1,
                switched_at_us=CREATED,
            )
        )
        == REFUSED_ADMISSION_NOT_FOUND
    )


def test_a_principal_outside_the_project_cannot_choose_it() -> None:
    assert (
        refusal_reason(
            lambda: decide_switch(
                workspace_id=WS,
                principal=OUTSIDER,
                project_id=ALPHA,
                current=None,
                authority=AUTHORITY,
                fencing_generation=1,
                switched_at_us=CREATED,
            )
        )
        == REFUSED_NOT_MEMBER
    )


def test_an_empty_authority_admits_no_Project_choice() -> None:
    assert (
        refusal_reason(
            lambda: decide_switch(
                workspace_id=WS,
                principal=OWNER,
                project_id=ALPHA,
                current=None,
                authority=NO_OUTCOME_ADMISSIONS,
                fencing_generation=1,
                switched_at_us=CREATED,
            )
        )
        == REFUSED_ADMISSION_NOT_FOUND
    )


def test_the_generation_cannot_advance_past_the_largest_storable_value() -> None:
    current = StoredProjectContext(
        WS, ALPHA, 9_223_372_036_854_775_807, 1, OWNER, CREATED
    )

    assert (
        refusal_reason(
            lambda: decide_switch(
                workspace_id=WS,
                principal="owner-beta",
                project_id=BETA,
                current=current,
                authority=AUTHORITY,
                fencing_generation=1,
                switched_at_us=CREATED,
            )
        )
        == REFUSED_CONTEXT_MISMATCH
    )


# -- building the outcome request -----------------------------------------------------------------


def _export() -> StoredExport:
    return export_of(generation=1)


def test_a_legacy_request_keeps_the_body_it_always_had() -> None:
    request = build_outcome_request(
        workspace_id=WS,
        principal=PRINCIPAL,
        objective=OBJECTIVE,
        export=_export(),
        current_generation=1,
        created_at_us=CREATED,
    )
    legacy_body = to_canonical_json(
        {
            "workspaceId": WS,
            "requestedBy": PRINCIPAL,
            "objective": OBJECTIVE,
            "exportId": request.export_id,
            "sourceHandoffIdentity": request.source_handoff_identity,
        }
    )

    assert request.admission_json is None
    assert request.context_generation is None
    assert (
        request.outcome_request_id
        == "outreq-" + hashlib.sha256(legacy_body.encode()).hexdigest()
    )


def test_an_admitted_request_records_its_summary_and_the_generation_it_was_accepted_under() -> (
    None
):
    parsed = parse_admission(claimed(summary()))
    request = build_outcome_request(
        workspace_id=WS,
        principal=OWNER,
        objective=OBJECTIVE,
        export=_export(),
        current_generation=1,
        created_at_us=CREATED,
        admission=parsed,
        authority=AUTHORITY,
        active=StoredProjectContext(WS, ALPHA, 1, 1, OWNER, CREATED),
    )

    assert request.project_id == ALPHA
    assert request.context_generation == 1
    assert request.admission_json == to_canonical_json(summary())
    assert request.admission_identity == parsed.identity


def test_the_admission_and_generation_are_part_of_the_request_identity() -> None:
    export = _export()
    base = build_outcome_request(
        workspace_id=WS,
        principal=OWNER,
        objective=OBJECTIVE,
        export=export,
        current_generation=1,
        created_at_us=CREATED,
        admission=parse_admission(claimed(summary())),
        authority=AUTHORITY,
        active=StoredProjectContext(WS, ALPHA, 1, 1, OWNER, CREATED),
    )
    changed = build_outcome_request(
        workspace_id=WS,
        principal=OWNER,
        objective=OBJECTIVE,
        export=export,
        current_generation=1,
        created_at_us=CREATED,
        admission=parse_admission(claimed(summary(constraints=["Write nothing."]))),
        authority=AUTHORITY,
        active=StoredProjectContext(WS, ALPHA, 1, 1, OWNER, CREATED),
    )

    assert base.outcome_request_id != changed.outcome_request_id


def test_a_structured_request_that_does_not_match_its_export_is_refused() -> None:
    export = export_of(handoff(target="service/other"), generation=1)

    assert (
        refusal_reason(
            lambda: build_outcome_request(
                workspace_id=WS,
                principal=OWNER,
                objective=OBJECTIVE,
                export=export,
                current_generation=1,
                created_at_us=CREATED,
                admission=parse_admission(claimed(summary())),
                authority=AUTHORITY,
                active=StoredProjectContext(WS, ALPHA, 1, 1, OWNER, CREATED),
            )
        )
        == "export_mismatch"
    )


def test_a_structured_request_for_an_export_of_another_workspace_is_not_found() -> None:
    assert (
        refusal_reason(
            lambda: build_outcome_request(
                workspace_id="ws-elsewhere",
                principal=OWNER,
                objective=OBJECTIVE,
                export=_export(),
                current_generation=1,
                created_at_us=CREATED,
                admission=parse_admission(
                    claimed(summary(workspace_id="ws-elsewhere"))
                ),
                authority=AUTHORITY,
                active=StoredProjectContext(WS, ALPHA, 1, 1, OWNER, CREATED),
            )
        )
        == REFUSED_NOT_FOUND
    )


# -- storage and the migration's guards ------------------------------------------------------------


def _admitted_request(holder: Any, *, generation: int = 1) -> StoredOutcomeRequest:
    return build_outcome_request(
        workspace_id=WS,
        principal=OWNER,
        objective=OBJECTIVE,
        export=export_of(generation=holder.generation),
        current_generation=holder.generation,
        created_at_us=CREATED,
        admission=parse_admission(
            claimed(summary(context_generation=f"ctxgen-{generation}"))
        ),
        authority=AUTHORITY,
        active=StoredProjectContext(
            WS, ALPHA, generation, holder.generation, OWNER, CREATED
        ),
    )


def _seed_context(
    holder: Any, project: str = ALPHA, generation: int = 1
) -> StoredProjectContext:
    context = StoredProjectContext(
        WS, project, generation, holder.generation, OWNER, CREATED
    )
    previous = (
        None
        if generation == 1
        else StoredProjectContext(
            WS, ALPHA, generation - 1, holder.generation, OWNER, CREATED
        )
    )
    _fenced(holder, lambda c: record_project_context(c, context, previous=previous))
    return context


def test_a_structured_request_round_trips_through_storage_with_its_identity(
    owned: Any,
) -> None:
    export = export_of(generation=owned.generation)
    _fenced(owned, lambda c: record_export(c, export))
    _seed_context(owned)
    request = _admitted_request(owned)
    _fenced(owned, lambda c: record_outcome_request(c, request))

    stored = read_outcome_request(
        owned.connection, workspace_id=WS, outcome_request_id=request.outcome_request_id
    )

    assert stored == request
    assert (
        stored is not None and stored.admission_identity == request.admission_identity
    )


def test_a_context_is_chosen_once_and_advances_only_by_one_to_another_project(
    owned: Any,
) -> None:
    _seed_context(owned, ALPHA, 1)
    with pytest.raises(sqlite3.IntegrityError):
        _seed_context(owned, BETA, 3)

    _seed_context(owned, BETA, 2)
    assert read_project_context(
        owned.connection, workspace_id=WS
    ) == StoredProjectContext(WS, BETA, 2, owned.generation, OWNER, CREATED)


def test_a_context_row_cannot_be_inserted_or_advanced_outside_the_fence(
    owned: Any,
) -> None:
    # Outside the service's own writer the authorizer refuses the statement before any trigger runs.
    with pytest.raises(sqlite3.DatabaseError):
        owned.connection.execute(
            "INSERT INTO omnivia_project_contexts VALUES (?, ?, 1, ?, ?, ?)",
            (WS, ALPHA, owned.generation, OWNER, CREATED),
        )
    _seed_context(owned, ALPHA, 1)
    with pytest.raises(sqlite3.DatabaseError):
        owned.connection.execute(
            "UPDATE omnivia_project_contexts SET context_generation = 2, project_id = ?",
            (BETA,),
        )
    assert _count(owned, "omnivia_project_contexts") == 1


def test_a_context_row_is_never_deleted(owned: Any) -> None:
    _seed_context(owned, ALPHA, 1)
    with pytest.raises(sqlite3.IntegrityError):
        _fenced(owned, lambda c: c.execute("DELETE FROM omnivia_project_contexts"))
    assert _count(owned, "omnivia_project_contexts") == 1


def test_a_structured_request_is_refused_by_the_database_unless_its_context_is_current(
    owned: Any,
) -> None:
    export = export_of(generation=owned.generation)
    _fenced(owned, lambda c: record_export(c, export))
    request = _admitted_request(owned, generation=1)

    with pytest.raises(sqlite3.IntegrityError):
        _fenced(owned, lambda c: record_outcome_request(c, request))

    _seed_context(owned, ALPHA, 1)
    _fenced(owned, lambda c: record_outcome_request(c, request))
    assert _count(owned, "omnivia_outcome_requests") == 1


def test_a_structured_request_whose_admission_is_altered_at_rest_reads_as_invalid(
    owned: Any,
) -> None:
    export = export_of(generation=owned.generation)
    _fenced(owned, lambda c: record_export(c, export))
    _seed_context(owned)
    request = _admitted_request(owned)
    _fenced(owned, lambda c: record_outcome_request(c, request))
    path = owned.path
    owned.connection.close()
    _raw(
        path,
        "DROP TRIGGER omnivia_guard_outcome_requests_update",
        "UPDATE omnivia_outcome_requests SET admission_json = replace(admission_json, 'Read only.', 'Write only.')",
    )

    reader = sqlite3.connect(path)
    try:
        with pytest.raises(TaskContextInvalid):
            read_outcome_request(
                reader, workspace_id=WS, outcome_request_id=request.outcome_request_id
            )
    finally:
        reader.close()


def test_a_non_canonical_stored_admission_is_refused_by_the_row_check() -> None:
    request = StoredOutcomeRequest(
        workspace_id=WS,
        requested_by=OWNER,
        objective=OBJECTIVE,
        export_id="tcx-" + "a" * 64,
        source_handoff_identity="c" * 64,
        fencing_generation=1,
        created_at_us=CREATED,
        project_id=ALPHA,
        admission_json='{"b": 1, "a": 2}',
        context_generation=1,
    )

    with pytest.raises(TaskContextInvalid):
        request.columns()


def test_the_database_keeps_admission_columns_all_set_or_all_null(owned: Any) -> None:
    export = export_of(generation=owned.generation)
    _fenced(owned, lambda c: record_export(c, export))
    with pytest.raises(sqlite3.IntegrityError):
        _fenced(
            owned,
            lambda c: c.execute(
                "INSERT INTO omnivia_outcome_requests (workspace_id, outcome_request_id, requested_by, "
                "objective, export_id, source_handoff_identity, status, fencing_generation, created_at_us, "
                "project_id) VALUES (?, ?, ?, ?, ?, ?, 'received', ?, ?, ?)",
                (
                    WS,
                    "outreq-" + "b" * 64,
                    OWNER,
                    OBJECTIVE,
                    export.export_id,
                    export.columns()["source_handoff_identity"],
                    owned.generation,
                    CREATED,
                    ALPHA,
                ),
            ),
        )
