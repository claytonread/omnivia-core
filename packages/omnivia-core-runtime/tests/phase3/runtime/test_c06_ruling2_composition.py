"""C06 Ruling-2 evidence: Core composes the authoritative Workflow release resolver.

Founder Ruling 2 settles composition ownership. The authority that resolves a released
Workflow to the exact material a Run binds is composed in the Core runtime service
bootstrap, Platform and hosted Dev consume it only through the registered operations,
and every case it cannot answer is an explicit refusal rather than a quiet substitution.
These are the four claims that can actually be measured here.

*The bootstrap composes it, with nothing injected.* The production surface `main()`'s own
`serve` builds now answers release resolution by itself: a version this workspace holds
no release for comes back `not_found` -- "no such released workflow version is available
here" -- rather than the `dependency_unavailable` a build with no authority at all used
to give. The difference is the whole ruling: authority now exists in Core and has an
answer, instead of being somebody else's to supply.

*It is the accepted-contract construction.* It reads 0027's sealed plan and the
`RuntimeDefinitionBinding` 0035 committed for that exact version, through T-0688's
verifying reader, and restates exactly the pinned release fields. No second release
store, and none of the admission's own facts.

*The full chain runs.* Registered operation → the Workflow handler → the composed
resolver → an accepted release resolution → a durable bound Run the scheduler can claim,
with the second Run of a version binding the same material as the first and re-sealing no
second plan.

*Every failure is explicit.* Missing release, a version that is not the one asked for, a
stored release whose bytes no longer address themselves, and an instance serving no
storage each refuse with their own code, and none of them binds a Run.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_t0688_workflow_runtime_hardening_repository as ip06
import test_t0693_workflow_application as app
import test_workflow_runs_migration as m27
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.application import (
    build_installation_application_dispatcher,
)
from omnivia_core_runtime.service.dispatch import Dispatcher
from omnivia_core_runtime.service.handlers.workflow import WORKFLOW_START_OPERATION
from omnivia_core_runtime.service.main import (
    LOCAL_PRINCIPAL,
    _build_production_application_surface,
    build_parser,
)
from omnivia_core_runtime.service.runner import ServiceRunner, ServiceSettings
from omnivia_core_runtime.service.workflow_release import (
    WorkspaceWorkflowReleaseAuthority,
)
from omnivia_core_runtime.service.workspace_init import (
    WorkspaceInitStatus,
    initialise_workspace,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.workflow_runtime_hardening import bound_material

from omnivia_core.contracts.v1 import SuccessResponseEnvelope

WORKSPACE_ID = app.WORKSPACE_ID
WALL = app.WALL
STEPS = m27.STEPS
BINDINGS = ip06.BINDINGS

#: A version nothing in this repository ever seals, so it is the honest "not released
#: here" case rather than a near miss of the one the fixtures do.
UNRELEASED_VERSION = "9.9.9"


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    """The same migrated, owned workspace the T-0693 suite drives."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def authority(service: Any) -> WorkspaceWorkflowReleaseAuthority:
    """The authority exactly as `_build_production_application_surface` composes it."""
    return WorkspaceWorkflowReleaseAuthority(
        service=service, workspace_id=WORKSPACE_ID
    )


def _release_into(owned: m1.Owned) -> str:
    """Put one release into this workspace the only way anything can: admit a Run of it.

    Core does not own release publication, so the first release of a version still
    arrives through whoever authored it -- here, the injected development resolver. What
    is under test is everything *after* that: the composed authority reading back what
    this workspace committed.
    """
    served = app.dispatcher(owned, releases=(app.release(),))
    return app.run_id_of(app.start(served))


def _start_through(
    served: Any, *, version: str = app.WORKFLOW_VERSION, tag: str
) -> Any:
    return served.dispatch(
        app.request(
            WORKFLOW_START_OPERATION,
            {"workflow_id": app.WORKFLOW_ID, "workflow_version": version},
            request_id=f"req-{tag}",
            idempotency_key=f"idem-{tag}",
        )
    )


# --- the bootstrap composes it ------------------------------------------------------


def test_ruling2_the_production_bootstrap_answers_release_resolution_uninjected(
    tmp_path: Path,
) -> None:
    """The real `main()` composition, with no resolver passed to it at all.

    `_build_production_application_surface` is the function `main()`'s own `serve` calls.
    Before Ruling 2 the uninjected build refused `dependency_unavailable`, because it had
    no release authority; it now refuses `not_found`, because it has one and that
    authority has no such release. Nothing is injected anywhere in this test.
    """
    result = initialise_workspace(
        workspace_root=tmp_path / "workspace",
        installation_root=tmp_path / "installation",
    )
    assert result.status is not WorkspaceInitStatus.REFUSED, result.reason
    runner = ServiceRunner(
        ServiceSettings(
            workspace_root=tmp_path / "workspace",
            installation_root=tmp_path / "installation",
        ),
        clock=FakeClock(wall=WALL),
    )
    report = runner.start()
    try:
        assert report.ready, report.to_dict()
        workspace_id = report.workspace_id
        assert workspace_id is not None
        probe = Dispatcher.for_service_operations(
            app.Grant(
                principal=LOCAL_PRINCIPAL,
                workspaces=frozenset({workspace_id}),
                operations=frozenset(app.SERVICE_OPERATIONS),
            )
        )
        surface = _build_production_application_surface(
            started=runner,
            probe=probe,
            installation=build_installation_application_dispatcher(
                service=_InstallationService(),  # type: ignore[arg-type]
                principal_id=LOCAL_PRINCIPAL,
                fallback=probe,
            ),
        )

        response = surface.dispatch(
            app.request(
                WORKFLOW_START_OPERATION,
                {
                    "workflow_id": app.WORKFLOW_ID,
                    "workflow_version": app.WORKFLOW_VERSION,
                },
                request_id="req-ruling2-uninjected",
                idempotency_key="idem-ruling2-uninjected",
                workspace_id=workspace_id,
            )
        )
    finally:
        runner.stop()

    assert app.code(response) == "not_found"
    assert "no such released workflow version" in response.error.message


class _InstallationService:
    """Construction-only shape; its bound installation handlers are never invoked here."""

    authority = type("_Authority", (), {"installation_id": "inst-c06-ruling2"})()


def test_ruling2_the_console_script_exposes_no_way_to_replace_the_authority(
    owned: m1.Owned,
) -> None:
    """The escape hatch is an in-process parameter and the packaged posture cannot reach it.

    Core draws no other production/development posture distinction, so this is the whole
    of the fail-closed rule: `omnivia-core-service` parses no argument that could name a
    release resolver, which is what keeps the packaged service on Core's own authority.
    """
    actions = build_parser()._actions
    assert not [
        action
        for action in actions
        if "release" in action.dest or "resolver" in action.dest
    ]


# --- it is the accepted-contract construction, and the chain runs --------------------


def test_ruling2_the_composed_authority_restates_the_committed_release(
    owned: m1.Owned,
) -> None:
    """Registered operation → handler → composed resolver → resolution → bound Run.

    The second start goes through the composed authority alone, and the two Runs agree on
    every pinned field a resume compares -- which is the accepted predicate for "the same
    release", excluding the `bindingId`, `boundAt` and `boundBy` that belong to one
    admission rather than to the release.
    """
    first_run = _release_into(owned)
    composed = app.dispatcher(owned, tag="composed", resolve_release=authority(owned))

    answer = _start_through(composed, tag="ruling2-composed")

    assert isinstance(answer, SuccessResponseEnvelope), answer
    second_run = str(answer.result["run"]["run_id"])
    assert second_run != first_run
    assert answer.result["run"]["state"] == "queued"
    assert answer.result["run"]["plan_digest"] == app.plan().content_hash
    # One sealed plan for two Runs: re-resolving a released version re-seals nothing and
    # opens no second release record.
    assert app.count(owned, app.PLANS) == 1
    assert app.count(owned, app.RUNS) == 2
    assert _material(owned, second_run) == _material(owned, first_run)


def _material(owned: m1.Owned, run_id: str) -> str:
    stored = ip06.read_runtime_definition_binding(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=run_id
    )
    assert stored is not None, run_id
    return bound_material(stored.binding)


def test_ruling2_the_composed_authority_resolves_the_exact_version_and_no_other(
    owned: m1.Owned,
) -> None:
    """Exact version or nothing: no `latest`, no nearest match, no neighbouring release."""
    _release_into(owned)
    resolve = authority(owned)

    resolved = resolve(
        workflow_id=app.WORKFLOW_ID, workflow_version=app.WORKFLOW_VERSION
    )
    assert resolved is not None
    assert resolved.plan.content_hash == app.plan().content_hash
    assert set(resolved.material) <= {
        "releaseRef",
        "executionProfileDigest",
        "effectivePolicyDigest",
        "componentImplementationDigests",
        "resourceBindingSnapshots",
        "modelPolicySnapshotRef",
        "modelPolicySnapshotDigest",
    }
    assert "bindingId" not in resolved.material
    assert "boundAt" not in resolved.material
    assert "boundBy" not in resolved.material

    assert (
        resolve(workflow_id=app.WORKFLOW_ID, workflow_version=UNRELEASED_VERSION)
        is None
    )
    assert (
        resolve(workflow_id="workflow-nobody-released", workflow_version=app.WORKFLOW_VERSION)
        is None
    )


# --- every failure is explicit ------------------------------------------------------


def test_ruling2_a_version_this_workspace_never_released_refuses_and_writes_nothing(
    owned: m1.Owned,
) -> None:
    """Missing release is `not_found` through the served path, and leaves no Run."""
    composed = app.dispatcher(owned, tag="missing", resolve_release=authority(owned))
    before = (app.count(owned, app.PLANS), app.count(owned, app.RUNS))

    response = _start_through(
        composed, version=UNRELEASED_VERSION, tag="ruling2-missing"
    )

    assert app.code(response) == "not_found"
    assert "no such released workflow version" in response.error.message
    assert (app.count(owned, app.PLANS), app.count(owned, app.RUNS)) == before


def test_ruling2_a_plan_edited_outside_the_database_refuses_rather_than_binding(
    owned: m1.Owned,
) -> None:
    """A sealed plan that no longer addresses itself is not a release this may state.

    The rehydrated plan is re-addressed out of its own canonical preimage, so a step
    column edited offline fails the content hash and the authority refuses instead of
    stating a plan nobody sealed. Refusing here is refusing before the start: the
    handler resolves the release inside the mutation seam and before any write it makes.
    """
    _release_into(owned)
    connection = ip06.corrupt(
        owned,
        f"UPDATE {STEPS} SET component_version = '9.9.9'",
        table=STEPS,
        operation="update",
    )
    try:
        resolve = authority(_rebound(owned, connection))
        with pytest.raises(Exception) as refusal:
            resolve(
                workflow_id=app.WORKFLOW_ID, workflow_version=app.WORKFLOW_VERSION
            )
    finally:
        connection.close()

    assert getattr(refusal.value, "code", None) == "internal_non_recoverable"
    assert "cannot be believed" in str(getattr(refusal.value, "message", ""))


def test_ruling2_a_binding_edited_outside_the_database_refuses_rather_than_binding(
    owned: m1.Owned,
) -> None:
    """The other half of the release record, and the same answer.

    The material is read through T-0688's verifying reader, so bytes that no longer match
    their recorded digest are refused there and reported here as a refusal rather than
    substituted with whatever the edit claims.
    """
    _release_into(owned)
    connection = ip06.corrupt(
        owned,
        f"UPDATE {BINDINGS} SET binding_json = '{{\"releaseRef\":{{}}}}'",
    )
    try:
        resolve = authority(_rebound(owned, connection))
        with pytest.raises(Exception) as refusal:
            resolve(
                workflow_id=app.WORKFLOW_ID, workflow_version=app.WORKFLOW_VERSION
            )
    finally:
        connection.close()

    assert getattr(refusal.value, "code", None) == "internal_non_recoverable"
    assert "cannot be believed" in str(getattr(refusal.value, "message", ""))


def test_ruling2_an_instance_serving_no_storage_refuses_rather_than_resolving(
    owned: m1.Owned,
) -> None:
    """No connection is no authority, and no authority is a refusal a caller can act on."""
    resolve = authority(_rebound(owned, None))

    with pytest.raises(Exception) as refusal:
        resolve(
            workflow_id=app.WORKFLOW_ID, workflow_version=app.WORKFLOW_VERSION
        )

    assert getattr(refusal.value, "code", None) == "dependency_unavailable"


def _rebound(holder: m1.Owned, connection: sqlite3.Connection | None) -> Any:
    """The same owner, reading through another handle on the same file -- or through none."""

    class _Rebound:
        pass

    rebound = _Rebound()
    rebound.connection = connection  # type: ignore[attr-defined]
    rebound.identity = holder.identity  # type: ignore[attr-defined]
    return rebound
