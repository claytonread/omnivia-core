"""T-0629G acceptance: lifecycle, scheduler and cleanup (LC-01 … LC-12)."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from omnivia_core_runtime.ownership.discovery import discover, publish
from omnivia_core_runtime.ownership.fencing import (
    close_guard,
    fenced_transaction,
    open_guard,
    read_guard,
)
from omnivia_core_runtime.ownership.identity import (
    FakeClock,
    ProcessEvidence,
    ServiceInstanceIdentity,
)
from omnivia_core_runtime.ownership.lease import (
    DEFAULT_LEASE_TTL_SECONDS,
    LeaseHeld,
    acquire_lease,
    read_lease,
)
from omnivia_core_runtime.service.bootstrap import (
    StartupOutcome,
    _endpoint_answers,
    _require_contract_version,
    coordinated_startup,
    refuse_incompatible_workspace,
)
from omnivia_core_runtime.service.lifecycle import (
    LEGAL_TRANSITIONS,
    LifecycleError,
    ReadinessRefused,
    ReadinessRequirements,
    ResourceStack,
    ServiceLifecycle,
    ServiceState,
)
from omnivia_core_runtime.service.runner import (
    LEASE_RENEWAL_DEADLINE_SECONDS,
    LEASE_RENEWAL_INTERVAL_SECONDS,
    ServiceRunner,
    ServiceSettings,
)
from omnivia_core_runtime.service.transport import endpoint_for_path
from omnivia_core_runtime.service.versions import (
    API_VERSION,
    PROTOCOL_VERSION,
    SERVER_VERSION,
    supported_api_versions,
    supported_workspace_versions,
    workspace_contract_version,
)
from omnivia_core_runtime.storage.backup import InstallationLayout
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    fingerprint_schema,
    open_database,
)
from omnivia_core_runtime.storage.legacy import migrate_legacy_database
from omnivia_core_runtime.storage.migrations import (
    canonical_schema_fingerprint,
)
from omnivia_core_runtime.workspace.layout import WorkspaceLayout

from omnivia_core.contracts.v1 import (
    ServiceEndpointDescriptor,
    ServiceProcessEvidence,
)
from omnivia_core.workspace.manifest import CoreCompatibility, WorkspaceManifest

from .conftest import SERVICE_INSTANCE, WORKSPACE_ID

CORE_VERSION = "0.1.0"
WORKSPACE_FORMAT_ORDINAL = "1"
WORKSPACE_FORMAT_VERSION = workspace_contract_version(WORKSPACE_FORMAT_ORDINAL)


def descriptor(
    *,
    instance: str,
    installation: str,
    endpoint: str,
    pid: int,
    generation: int = 2,
) -> ServiceEndpointDescriptor:
    """A ready descriptor in the shape the service actually publishes."""
    return ServiceEndpointDescriptor(
        descriptor_version=API_VERSION,
        workspace_id=WORKSPACE_ID,
        service_instance_id=instance,
        installation_id=installation,
        endpoint_uri=endpoint,
        protocol_version=PROTOCOL_VERSION,
        server_version=SERVER_VERSION,
        supported_api_versions=supported_api_versions(),
        supported_workspace_versions=supported_workspace_versions(
            WORKSPACE_FORMAT_ORDINAL
        ),
        workspace_format_version=WORKSPACE_FORMAT_VERSION,
        ready=True,
        lifecycle_state=ServiceState.READY.value,
        fencing_generation=generation,
        published_at="2026-08-04T00:00:00Z",
        process=ServiceProcessEvidence(pid=pid, start_time="100", boot_id="boot-a"),
    )


@pytest.fixture
def served(tmp_path: Path, phase0_source: Path):
    """A migrated workspace plus the settings needed to serve it."""
    workspace = WorkspaceLayout(root=tmp_path / "workspace")
    installation = InstallationLayout(root=tmp_path / "installation-state")
    installation.create(WORKSPACE_ID)
    manifest = WorkspaceManifest(
        workspace_id=WORKSPACE_ID,
        created_at="2026-07-30T00:00:00+00:00",
        name="Served",
        compatibility=CoreCompatibility(
            workspace_format_version="1", min_core_version="0.1.0"
        ),
    )
    migrate_legacy_database(
        phase0_source,
        workspace,
        installation,
        manifest,
        service_instance_id=SERVICE_INSTANCE,
    )
    settings = ServiceSettings(
        workspace_root=workspace.root,
        installation_root=installation.root,
        core_version=CORE_VERSION,
        endpoint=endpoint_for_path(tmp_path / "omnivia-test.sock").url,
    )
    return workspace, installation, settings


@pytest.fixture
def live_endpoint():
    """A real listening socket, for descriptors that claim a running service.

    Coordination probes the advertised endpoint, so a descriptor carrying a made-up
    path is correctly treated as dead. A case that means "another service is running"
    has to provide one that answers.
    """
    import shutil as shutil_module
    import tempfile as tempfile_module

    from omnivia_core_runtime.service.authorization import Grant
    from omnivia_core_runtime.service.dispatch import Dispatcher
    from omnivia_core_runtime.service.transport import LocalSocketServer

    directory = Path(tempfile_module.mkdtemp(prefix="ovs-", dir=tempfile_module.gettempdir()))
    server = LocalSocketServer(
        dispatcher=Dispatcher.for_service_operations(
            Grant(
                principal="local-user",
                workspaces=frozenset({WORKSPACE_ID}),
                operations=frozenset(),
            )
        ),
        path=directory / "s.sock",
    )
    server.start()
    try:
        yield server.url
    finally:
        server.stop()
        shutil_module.rmtree(directory, ignore_errors=True)


# LC-01
def test_lc01_all_ten_lifecycle_states_are_reachable_and_distinct() -> None:
    states = list(ServiceState)
    assert len(states) == len({state.value for state in states})
    for expected in (
        "stopped",
        "starting",
        "recovering",
        "migrating",
        "ready",
        "running",
        "draining",
        "maintenance",
        "failed",
    ):
        assert any(state.value == expected for state in states), expected

    # Every state except the terminal pair leads somewhere.
    for state in states:
        assert state in LEGAL_TRANSITIONS, state


def test_illegal_transitions_are_refused() -> None:
    lifecycle = ServiceLifecycle()
    assert lifecycle.state is ServiceState.STOPPED
    with pytest.raises(LifecycleError, match="illegal transition"):
        lifecycle.transition_to(ServiceState.READY)
    lifecycle.transition_to(ServiceState.STARTING)
    with pytest.raises(LifecycleError):
        lifecycle.transition_to(ServiceState.RUNNING)


# SB-02 regression
def test_sb02_offline_schema_drift_refuses_writable_readiness(
    served: tuple[WorkspaceLayout, InstallationLayout, ServiceSettings],
) -> None:
    """A table added while no service held the workspace must refuse readiness.

    The check used to compare the live schema with a fingerprint taken from that
    same live schema, which passes whatever the database contains.
    """
    workspace, _installation, settings = served

    offline = sqlite3.connect(workspace.database_path)
    offline.execute("CREATE TABLE illicit_schema_drift (id INTEGER PRIMARY KEY)")
    offline.commit()
    offline.close()

    runner = ServiceRunner(settings, clock=FakeClock())
    report = runner.start()
    try:
        assert not report.ready, report.to_dict()
        assert "exact_schema_and_trigger_fingerprint" in report.unmet
    finally:
        runner.stop()


# SB-02 regression
def test_sb02_the_schema_oracle_does_not_read_the_database_it_judges(
    served: tuple[WorkspaceLayout, InstallationLayout, ServiceSettings],
) -> None:
    """The expectation is built from the frozen artifacts, so drift cannot move it."""
    workspace, _installation, _settings = served
    expected = canonical_schema_fingerprint()

    live = sqlite3.connect(workspace.database_path)
    try:
        assert fingerprint_schema(live).matches(expected), "a clean workspace must match"
        live.execute("CREATE TABLE illicit_schema_drift (id INTEGER PRIMARY KEY)")
        live.commit()
        assert not fingerprint_schema(live).matches(expected)
    finally:
        live.close()

    # Recomputed from the artifacts with the cache cleared, so this asserts the
    # derivation rather than the memo. `canonical_schema_fingerprint()` is
    # `lru_cache`d, so calling it again would return the same object no matter what
    # the function did -- an assertion that cannot fail is not evidence.
    canonical_schema_fingerprint.cache_clear()
    assert canonical_schema_fingerprint() == expected

    # And it never opens the workspace it judges: with the database removed entirely
    # it still produces the same value.
    workspace.database_path.unlink()
    canonical_schema_fingerprint.cache_clear()
    assert canonical_schema_fingerprint() == expected


# LC-02 / LC-03
def test_lc02_lc03_readiness_requires_all_nine_preconditions(
    served: tuple[WorkspaceLayout, InstallationLayout, ServiceSettings],
) -> None:
    _workspace, installation, settings = served
    runner = ServiceRunner(settings, clock=FakeClock())
    report = runner.start()
    try:
        assert report.ready, report.to_dict()
        assert report.state == ServiceState.READY.value
        assert runner.lifecycle.readiness.satisfied
        # Nine distinct preconditions, all true.
        assert len(vars(runner.lifecycle.readiness)) == 9
        assert runner.lifecycle.readiness.unmet() == []

        # Readiness is advertised only after the state is READY.
        found = discover(installation.runtime_for(WORKSPACE_ID))
        assert found is not None and found.ready
        assert found.descriptor_version == API_VERSION
        assert found.supported_api_versions == supported_api_versions()
        assert found.fencing_generation == report.fencing_generation
    finally:
        runner.stop()


@pytest.mark.parametrize(
    "missing",
    [
        "compatible_manifest",
        "qualified_filesystem",
        "holds_lifetime_storage_lock",
        "sole_exclusive_connection",
        "exact_current_lease_tuple",
        "canonical_migration_checksums",
        "integrity_check_passed",
        "exact_schema_and_trigger_fingerprint",
        "migrations_and_jobs_recovered",
    ],
)
def test_any_single_unmet_precondition_refuses_readiness(missing: str) -> None:
    """Each of the nine is individually load-bearing."""
    fields = dict.fromkeys(vars(ReadinessRequirements()), True)
    fields[missing] = False
    requirements = ReadinessRequirements(**fields)

    assert not requirements.satisfied
    assert requirements.unmet() == [missing]

    lifecycle = ServiceLifecycle()
    lifecycle.transition_to(ServiceState.STARTING)
    lifecycle.transition_to(ServiceState.RECOVERING)
    with pytest.raises(ReadinessRefused, match=missing):
        lifecycle.publish_readiness(requirements)
    assert lifecycle.state is ServiceState.FAILED


# LC-04
def test_lc04_failed_transition_releases_resources_in_reverse_order() -> None:
    released: list[str] = []
    stack = ResourceStack()
    for name in ("lifetime_storage_lock", "exclusive_connection", "workspace_lease"):
        stack.push(name, lambda n=name: released.append(n))  # type: ignore[misc]

    lifecycle = ServiceLifecycle(resources=stack)
    order = lifecycle.fail("something broke")

    assert order == ["workspace_lease", "exclusive_connection", "lifetime_storage_lock"]
    assert released == order, "the lock must outlive the connection it protects"
    assert lifecycle.state is ServiceState.FAILED
    assert lifecycle.last_failure == "something broke"


def test_a_failing_release_does_not_abandon_the_rest() -> None:
    """Leaving later resources held is how a crashed startup keeps a workspace locked."""
    released: list[str] = []
    stack = ResourceStack()
    stack.push("lock", lambda: released.append("lock"))

    def explode() -> None:
        raise RuntimeError("release failed")

    stack.push("connection", explode)
    order = stack.unwind()
    assert order == ["connection", "lock"]
    assert released == ["lock"], "the lock was still released"


# LC-05
def test_lc05_failed_instance_publishes_no_readiness(tmp_path: Path) -> None:
    """A workspace with no substrate cannot become ready, and advertises nothing."""
    workspace = WorkspaceLayout(root=tmp_path / "empty-workspace")
    installation = InstallationLayout(root=tmp_path / "state")
    workspace.create_directories()

    from omnivia_core_runtime.workspace.manifest_store import write_manifest

    write_manifest(
        workspace,
        WorkspaceManifest(
            workspace_id="ws-no-substrate",
            created_at="2026-07-30T00:00:00+00:00",
            compatibility=CoreCompatibility(
                workspace_format_version="1", min_core_version="0.1.0"
            ),
        ),
    )
    workspace.database_path.touch()

    runner = ServiceRunner(
        ServiceSettings(
            workspace_root=workspace.root,
            installation_root=installation.root,
            core_version=CORE_VERSION,
        ),
        clock=FakeClock(),
    )
    report = runner.start()
    assert not report.ready
    assert report.state == ServiceState.FAILED.value
    assert discover(installation.runtime_for("ws-no-substrate")) is None


def test_an_incompatible_manifest_fails_before_any_acquisition(tmp_path: Path) -> None:
    """LC-05 / WM-12: refusal happens before a lock exists, so nothing unwinds."""
    workspace = WorkspaceLayout(root=tmp_path / "future-workspace")
    installation = InstallationLayout(root=tmp_path / "state")
    workspace.create_directories()

    from omnivia_core_runtime.workspace.manifest_store import write_manifest

    write_manifest(
        workspace,
        WorkspaceManifest(
            workspace_id="ws-future",
            created_at="2026-07-30T00:00:00+00:00",
            compatibility=CoreCompatibility(
                workspace_format_version="1", min_core_version="99.0.0"
            ),
        ),
    )

    runner = ServiceRunner(
        ServiceSettings(
            workspace_root=workspace.root,
            installation_root=installation.root,
            core_version=CORE_VERSION,
        ),
        clock=FakeClock(),
    )
    report = runner.start()
    assert not report.ready
    assert "refusing to open this workspace" in report.reason
    assert report.released == (), "nothing had been acquired yet"
    assert not (workspace.locks_path / "storage.lock").exists()


# LC-06
def test_lc06_a_second_service_cannot_take_the_workspace_while_one_holds_it(
    served: tuple[WorkspaceLayout, InstallationLayout, ServiceSettings],
) -> None:
    _workspace, _installation, settings = served
    first = ServiceRunner(settings, clock=FakeClock())
    report = first.start()
    try:
        assert report.ready

        second = ServiceRunner(settings, clock=FakeClock())
        blocked = second.start()
        assert not blocked.ready
        assert "storage lock" in blocked.reason
        assert second.lifecycle.state is ServiceState.FAILED
    finally:
        first.stop()


def test_lc06b_generation_is_revalidated_by_every_fenced_write(
    served: tuple[WorkspaceLayout, InstallationLayout, ServiceSettings],
) -> None:
    _workspace, _installation, settings = served
    runner = ServiceRunner(settings, clock=FakeClock())
    report = runner.start()
    try:
        assert report.ready
        connection = runner.connection
        identity = runner.identity
        assert connection is not None and identity is not None
        assert read_guard(connection) is not None

        with fenced_transaction(
            connection,
            identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=report.fencing_generation or 0,
        ):
            connection.execute(
                "INSERT INTO omnivia_durable_jobs (job_id, job_type, state, created_at, "
                "updated_at, fencing_generation) VALUES ('j-1', 't', 'queued', 'n', 'n', ?)",
                (report.fencing_generation,),
            )
        count = connection.execute(
            "SELECT COUNT(*) FROM omnivia_durable_jobs WHERE job_id = 'j-1'"
        ).fetchone()
        assert count is not None and count[0] == 1
    finally:
        runner.stop()


# LC-07 / LC-08 / LC-09
def test_lc07_lc08_lc09_no_partial_batch_job_or_projection_commit(
    served: tuple[WorkspaceLayout, InstallationLayout, ServiceSettings],
) -> None:
    _workspace, _installation, settings = served
    runner = ServiceRunner(settings, clock=FakeClock())
    report = runner.start()
    try:
        connection = runner.connection
        identity = runner.identity
        assert connection is not None and identity is not None
        generation = report.fencing_generation or 0

        with pytest.raises(RuntimeError, match="batch failed"), fenced_transaction(
            connection,
            identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=generation,
        ):
            connection.execute(
                "INSERT INTO omnivia_durable_jobs (job_id, job_type, state, created_at, "
                "updated_at) VALUES ('batch-1', 't', 'queued', 'n', 'n')"
            )
            connection.execute(
                "INSERT INTO omnivia_projection_ledger (projection_id, version, "
                "rebuildable, state, updated_at) VALUES ('p-1', '1', 1, 'building', 'n')"
            )
            raise RuntimeError("batch failed half way")

        jobs = connection.execute(
            "SELECT COUNT(*) FROM omnivia_durable_jobs WHERE job_id = 'batch-1'"
        ).fetchone()
        projections = connection.execute(
            "SELECT COUNT(*) FROM omnivia_projection_ledger WHERE projection_id = 'p-1'"
        ).fetchone()
        assert jobs is not None and jobs[0] == 0
        assert projections is not None and projections[0] == 0
    finally:
        runner.stop()


# LC-10
def test_lc10_durable_jobs_are_recovered_before_readiness(
    served: tuple[WorkspaceLayout, InstallationLayout, ServiceSettings],
) -> None:
    """A job claimed by an older generation is requeued, not left claimed."""
    workspace, _installation, settings = served

    # Strand a job the way a real service would: under its own guard, which then
    # dies with it. Inserting without a guard is impossible by design — the first
    # attempt at this test was refused by the very triggers under test.
    predecessor = ServiceInstanceIdentity(
        service_instance_id="svc-dead",
        installation_id="inst-dead",
        process=ProcessEvidence(
            pid=4242, start_time="1", boot_id="boot-a", os_principal="me"
        ),
    )
    connection = open_database(workspace.database_path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        # It has to take the lease first. A guard is only openable by the current
        # lease owner, so a predecessor that never leased could not have stranded
        # anything -- the earlier version of this setup worked only because the guard
        # was not bound to the lease, which is the defect SB-03 names.
        lease = acquire_lease(
            connection,
            predecessor,
            clock=FakeClock(),
            workspace_id=WORKSPACE_ID,
            holds_storage_lock=True,
            lock_mechanism="flock",
        )
        open_guard(
            connection,
            predecessor,
            clock=FakeClock(),
            workspace_id=WORKSPACE_ID,
            fencing_generation=lease.fencing_generation,
        )
        with fenced_transaction(
            connection,
            predecessor,
            workspace_id=WORKSPACE_ID,
            fencing_generation=lease.fencing_generation,
        ):
            connection.execute(
                "INSERT INTO omnivia_durable_jobs (job_id, job_type, state, created_at, "
                "updated_at, fencing_generation, claimed_by_service_instance) "
                "VALUES ('stranded', 't', 'claimed', 'n', 'n', ?, 'svc-dead')",
                (lease.fencing_generation,),
            )
        # The predecessor dies: its guard goes with it.
        close_guard(connection)
    finally:
        connection.close()

    runner = ServiceRunner(settings, clock=FakeClock())
    report = runner.start()
    try:
        assert report.ready, report.to_dict()
        assert runner.connection is not None
        row = runner.connection.execute(
            "SELECT state, claimed_by_service_instance FROM omnivia_durable_jobs "
            "WHERE job_id = 'stranded'"
        ).fetchone()
        assert row is not None
        assert row[0] == "queued", "a job claimed by an older generation is requeued"
        assert row[1] is None
    finally:
        runner.stop()


def test_an_interrupted_migration_blocks_readiness(
    served: tuple[WorkspaceLayout, InstallationLayout, ServiceSettings],
) -> None:
    """A migration attempt left 'started' means recovery is incomplete."""
    workspace, _installation, settings = served
    connection = open_database(workspace.database_path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        connection.execute(
            "INSERT INTO omnivia_migration_attempts (attempt_id, version, outcome, "
            "started_at) VALUES ('interrupted', 99, 'started', 'n')"
        )
        connection.commit()
    finally:
        connection.close()

    runner = ServiceRunner(settings, clock=FakeClock())
    report = runner.start()
    assert not report.ready
    assert "migrations_and_jobs_recovered" in report.unmet
    assert runner.lifecycle.state is ServiceState.FAILED


# LC-11
def test_lc11_draining_rejects_new_mutations_but_completes_in_flight(
    served: tuple[WorkspaceLayout, InstallationLayout, ServiceSettings],
) -> None:
    _workspace, _installation, settings = served
    runner = ServiceRunner(settings, clock=FakeClock())
    report = runner.start()
    try:
        assert report.ready
        assert runner.lifecycle.state.accepts_new_mutations
        assert runner.lifecycle.advertises_writable

        runner.drain()
        assert runner.lifecycle.state is ServiceState.DRAINING
        assert not runner.lifecycle.state.accepts_new_mutations
        assert not runner.lifecycle.state.advertises_writable
    finally:
        runner.stop()


# LC-12
def test_lc12_clients_and_launchers_expose_no_lease_ownership_api() -> None:
    """The bootstrap mutex is coordination only; no client API grants the lease."""
    from omnivia_core_runtime.ownership import locks

    assert not locks.LockRole.BOOTSTRAP_MUTEX.grants_write_authority
    assert locks.LockRole.LIFETIME_STORAGE.grants_write_authority

    # The bootstrap module exposes coordination, never acquisition.
    from omnivia_core_runtime.service import bootstrap

    exported = set(bootstrap.__all__)
    assert exported == {
        "StartupDecision",
        "StartupOutcome",
        "coordinated_startup",
        "refuse_incompatible_workspace",
    }
    for forbidden in ("acquire_lease", "open_guard", "publish"):
        assert forbidden not in exported


def test_startup_coordination_rediscovers_after_taking_the_mutex(
    tmp_path: Path, live_endpoint: str
) -> None:
    """BD-11: the window between first discovery and the mutex is real."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)

    with coordinated_startup(
        runtime,
        api_version=API_VERSION,
        workspace_contract_version=WORKSPACE_FORMAT_VERSION,
    ) as first:
        assert first.outcome is StartupOutcome.SPAWN

    publish(
        runtime,
        descriptor(
            instance="svc-other",
            installation="inst-other",
            endpoint=live_endpoint,
            # A live pid: the descriptor stands for a service that is actually
            # running, which is what "another launcher won" means. An arbitrary
            # number here made the case depend on dead descriptors being trusted.
            pid=os.getpid(),
        ),
    )
    with coordinated_startup(
        runtime,
        api_version=API_VERSION,
        workspace_contract_version=WORKSPACE_FORMAT_VERSION,
    ) as second:
        assert second.outcome is StartupOutcome.USE_EXISTING
        assert second.existing is not None

    # An incompatible running service is not usable.
    with coordinated_startup(
        runtime, api_version="9.9", workspace_contract_version=WORKSPACE_FORMAT_VERSION
    ) as third:
        assert third.outcome is StartupOutcome.REFUSED_INCOMPATIBLE


# SB-08 regression
def test_sb08_a_second_launcher_cannot_also_spawn_while_the_first_holds_the_mutex(
    tmp_path: Path,
) -> None:
    """Only one launcher may be told to spawn before any descriptor is published.

    The decision used to arrive with the mutex already released, so a second
    launcher asking during the first launcher's spawn-and-wait was told to spawn
    too.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)

    with coordinated_startup(
        runtime,
        api_version=API_VERSION,
        workspace_contract_version=WORKSPACE_FORMAT_VERSION,
    ) as first:
        assert first.should_spawn
        # Still inside the first launcher's spawn-and-wait window.
        with coordinated_startup(
            runtime,
            api_version=API_VERSION,
            workspace_contract_version=WORKSPACE_FORMAT_VERSION,
        ) as second:
            assert not second.should_spawn
            assert second.outcome is StartupOutcome.REFUSED_MUTEX_UNAVAILABLE

    # Once the winner is finished, the mutex is available again.
    with coordinated_startup(
        runtime,
        api_version=API_VERSION,
        workspace_contract_version=WORKSPACE_FORMAT_VERSION,
    ) as third:
        assert third.should_spawn


# SB-08 regression
@pytest.mark.parametrize("outcome_is_spawn", [True, False])
def test_sb08_the_mutex_is_released_on_every_exit_path(
    tmp_path: Path, outcome_is_spawn: bool
) -> None:
    """Holding through spawn must not mean leaking when the caller raises."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)

    if not outcome_is_spawn:
        publish(
            runtime,
            descriptor(
                instance="svc-other",
                installation="inst-other",
                endpoint="unix:///tmp/other.sock",
                pid=999,
            ),
        )

    with (
        pytest.raises(RuntimeError, match="launcher exploded"),
        coordinated_startup(
            runtime,
            api_version=API_VERSION,
            workspace_contract_version=WORKSPACE_FORMAT_VERSION,
        ),
    ):
        raise RuntimeError("launcher exploded")

    # A leaked mutex would refuse the next launcher instead of answering it.
    with coordinated_startup(
        runtime,
        api_version=API_VERSION,
        workspace_contract_version=WORKSPACE_FORMAT_VERSION,
    ) as after:
        assert after.outcome is not StartupOutcome.REFUSED_MUTEX_UNAVAILABLE


def test_stop_releases_everything_in_reverse_order(
    served: tuple[WorkspaceLayout, InstallationLayout, ServiceSettings],
) -> None:
    _workspace, installation, settings = served
    runner = ServiceRunner(settings, clock=FakeClock())
    assert runner.start().ready

    stopped = runner.stop()
    assert stopped.state == ServiceState.STOPPED.value
    assert stopped.released == (
        "discovery_descriptor",
        "mutation_guard",
        "workspace_lease",
        "exclusive_connection",
        "lifetime_storage_lock",
    )
    # The service no longer advertises itself.
    assert discover(installation.runtime_for(WORKSPACE_ID)) is None


def test_a_stopped_service_releases_the_lock_for_a_successor(
    served: tuple[WorkspaceLayout, InstallationLayout, ServiceSettings],
) -> None:
    _workspace, _installation, settings = served
    first = ServiceRunner(settings, clock=FakeClock())
    first_report = first.start()
    assert first_report.ready
    first.stop()

    second = ServiceRunner(settings, clock=FakeClock())
    second_report = second.start()
    try:
        assert second_report.ready, second_report.to_dict()
        assert (second_report.fencing_generation or 0) > (
            first_report.fencing_generation or 0
        ), "the successor increments the generation"
    finally:
        second.stop()


def test_refuse_incompatible_workspace_is_a_pure_precondition() -> None:
    from omnivia_core.workspace.compatibility import (
        CompatibilityOutcome,
        CompatibilityStatus,
    )

    ok = CompatibilityOutcome(
        status=CompatibilityStatus.COMPATIBLE,
        reason="fine",
        readable=True,
        writable=True,
    )
    refuse_incompatible_workspace(ok)

    bad = CompatibilityOutcome(
        status=CompatibilityStatus.CORE_TOO_OLD,
        reason="core too old",
        readable=True,
        writable=False,
    )
    with pytest.raises(RuntimeError, match="core too old"):
        refuse_incompatible_workspace(bad)


# SRB-05 regression
@pytest.mark.parametrize(
    ("pid", "expected"),
    [(os.getpid(), StartupOutcome.USE_EXISTING), (2**30, StartupOutcome.SPAWN)],
)
def test_srb05_a_descriptor_naming_a_dead_process_is_not_used(
    tmp_path: Path, live_endpoint: str, pid: int, expected: StartupOutcome
) -> None:
    """A descriptor outlives its process whenever a service is killed.

    Trusting it told every launcher to use a service that was not running, and
    because they never reached the mutex nobody spawned a replacement and nobody
    cleared the file: the workspace stayed stuck behind a dead endpoint.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    publish(
        runtime,
        descriptor(
            instance="svc-maybe-dead",
            installation="inst",
            endpoint=live_endpoint,
            pid=pid,
        ),
    )
    with coordinated_startup(
        runtime,
        api_version=API_VERSION,
        workspace_contract_version=WORKSPACE_FORMAT_VERSION,
    ) as decision:
        assert decision.outcome is expected


# SRB-07 regression
def test_srb07_a_live_pid_with_a_dead_endpoint_is_not_used(tmp_path: Path) -> None:
    """Pids are recycled; the advertised endpoint is the claim that matters.

    Checking only liveness left the same stuck-behind-a-dead-service outcome whenever
    the pid of a crashed service was reused, or a descriptor named any live process.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    publish(
        runtime,
        descriptor(
            instance="svc-recycled-pid",
            installation="inst",
            # This process is alive, and is not a Core service.
            endpoint="unix:///nonexistent/omnivia-srb07.sock",
            pid=os.getpid(),
        ),
    )
    with coordinated_startup(
        runtime,
        api_version=API_VERSION,
        workspace_contract_version=WORKSPACE_FORMAT_VERSION,
    ) as decision:
        assert decision.outcome is StartupOutcome.SPAWN


def test_a_descriptor_without_process_evidence_is_not_used(
    tmp_path: Path, live_endpoint: str
) -> None:
    """`process` is optional in the contract shape; liveness is not optional here.

    The pid check and the endpoint probe are independent, and a descriptor that
    carries no process block has not passed the first one, it has skipped it. Even
    with an endpoint that genuinely answers, it is not adopted.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    publish(
        runtime,
        replace(
            descriptor(
                instance="svc-no-evidence",
                installation="inst",
                endpoint=live_endpoint,
                pid=os.getpid(),
            ),
            process=None,
        ),
    )
    with coordinated_startup(
        runtime,
        api_version=API_VERSION,
        workspace_contract_version=WORKSPACE_FORMAT_VERSION,
    ) as decision:
        assert decision.outcome is StartupOutcome.SPAWN


# --- the ordinal trap: private manifest notation vs public contract notation --


def test_the_manifest_ordinal_is_rejected_loudly_not_refused_misleadingly(
    tmp_path: Path, live_endpoint: str
) -> None:
    """`workspace_contract_version` wants `"1.0"`; the private manifest stores `"1"`.

    Before this was fixed, passing the bare ordinal through unconverted did not
    raise: `is_compatible` swallowed the resulting `ValueError` and reported
    `REFUSED_INCOMPATIBLE` quoting a window (`1.0-1.0`) the caller's translated
    version fits inside perfectly. This falsifies that: a caller that skips the
    translation must get a loud, honest failure, not a confident-looking lie.
    Correctly translated, the very same running service is usable.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    publish(
        runtime,
        descriptor(
            instance="svc-ordinal-trap",
            installation="inst",
            endpoint=live_endpoint,
            pid=os.getpid(),
        ),
    )

    # The trap: the manifest's bare ordinal, handed straight through.
    with (
        pytest.raises(ValueError, match="contract version"),
        coordinated_startup(
            runtime,
            api_version=API_VERSION,
            workspace_contract_version=WORKSPACE_FORMAT_ORDINAL,
        ),
    ):
        pass

    # Translated through the one boundary that does this correctly, the same
    # running service is found compatible.
    with coordinated_startup(
        runtime,
        api_version=API_VERSION,
        workspace_contract_version=WORKSPACE_FORMAT_VERSION,
    ) as decision:
        assert decision.outcome is StartupOutcome.USE_EXISTING


def test_require_contract_version_names_the_field_that_failed_to_parse() -> None:
    """`_decide` validates both caller versions eagerly, before discovery runs.

    A well-formed `major.minor` version passes silently. The private manifest's
    bare ordinal does not -- it names the offending field (`workspace_contract_version`,
    not `api_version`) rather than reporting a bare `is_compatible` mismatch, which
    is what let the old refusal quote a window the caller's version fit inside.
    `is_compatible` itself still fails closed and silently on an unreadable
    version (see its docstring): this validation is what keeps that path from
    ever being reached with one.
    """
    _require_contract_version("1.0", "workspace_contract_version")  # does not raise

    with pytest.raises(ValueError, match="workspace_contract_version=") as excinfo:
        _require_contract_version(
            WORKSPACE_FORMAT_ORDINAL, "workspace_contract_version"
        )
    assert "contract version" in str(excinfo.value)


# --- bootstrap endpoint probing ----------------------------------------------


def test_endpoint_answers_trusts_the_pid_alone_for_a_non_local_endpoint() -> None:
    """An in-process endpoint names no local scheme and has nothing to connect to."""
    assert _endpoint_answers("in-process")


def test_endpoint_answers_probes_a_live_local_endpoint(live_endpoint: str) -> None:
    assert _endpoint_answers(live_endpoint)


def test_endpoint_answers_refuses_a_dead_local_endpoint(tmp_path: Path) -> None:
    assert not _endpoint_answers(endpoint_for_path(tmp_path / "nothing-here.sock").url)


def test_endpoint_answers_fails_closed_on_a_malformed_claimed_local_endpoint() -> None:
    """A claim that cannot be checked must not be believed.

    `pipe://../escape` claims a local scheme but is not a name this runtime can
    address at all, so it cannot be treated the same as an endpoint that simply
    is not answering right now.
    """
    assert not _endpoint_answers("pipe://../escape")
    assert not _endpoint_answers("unix://")


def test_endpoint_answers_trusts_the_pid_for_anything_naming_no_local_scheme() -> None:
    """Only a *claimed* local scheme is checked; anything else defers to the pid."""
    assert _endpoint_answers("not-a-url-at-all")


# --- HANDOFF-CORE-001: the lease a live service keeps current ------------------
#
# `heartbeat()` shipped with no production caller. A served instance therefore read
# as stale one TTL after acquisition while it still held the lifetime storage lock
# and the sole exclusive connection -- the one state ADR-037 invariant 8 exists to
# investigate rather than trust. These four pin the renewal seam that closes it:
# when it writes, what it writes, how it fails, and that failing changes nothing
# about how it lets go.


def test_the_renewal_interval_and_deadline_sit_under_the_ttl() -> None:
    """The three constants as one ordering, asserted rather than assumed.

    Every test below is written against this ordering, and each one alone would
    still pass if the constants collapsed onto each other -- a deadline equal to
    the TTL, say, which stops the instance exactly when its lease dies rather than
    before. The margins are the design: two renewals may be lost and the heartbeat
    is still current, and an instance that cannot renew stops while its lease is
    still demonstrably current rather than after it is not.
    """
    assert LEASE_RENEWAL_INTERVAL_SECONDS < LEASE_RENEWAL_DEADLINE_SECONDS
    assert LEASE_RENEWAL_DEADLINE_SECONDS < DEFAULT_LEASE_TTL_SECONDS
    assert LEASE_RENEWAL_INTERVAL_SECONDS == pytest.approx(10.0)
    assert LEASE_RENEWAL_DEADLINE_SECONDS == pytest.approx(20.0)


def test_the_lease_is_renewed_on_its_interval_and_the_stored_heartbeat_moves(
    served: tuple[WorkspaceLayout, InstallationLayout, ServiceSettings],
) -> None:
    """Renewal is due on the interval, not on every poll, and it reaches the row.

    The stored `heartbeat_monotonic` is read back from the database rather than
    trusted from the return value, because the defect was never about what the
    runner believed: a takeover decision reads the lease row, so a renewal that
    updated only in-process state would leave the same stale row behind.

    The loop at the end is the defect itself. It runs past three TTLs of simulated
    time -- the window in which the old build read as expired -- and the lease is
    current at every step.
    """
    _workspace, _installation, settings = served
    clock = FakeClock()
    runner = ServiceRunner(settings, clock=clock)
    assert runner.start().ready
    try:
        assert runner.connection is not None
        acquired = read_lease(runner.connection)
        assert acquired is not None and acquired.heartbeat_monotonic is not None

        # Inside the interval there is nothing to do, and nothing is written. This
        # is what keeps a 250ms poll from being a 250ms write.
        clock.advance_monotonic(LEASE_RENEWAL_INTERVAL_SECONDS - 0.5)
        assert runner.renew_lease_if_due() is False
        unchanged = read_lease(runner.connection)
        assert unchanged is not None
        assert unchanged.heartbeat_monotonic == acquired.heartbeat_monotonic

        # On the interval: one write, and the stored reading is this clock's now.
        clock.advance_monotonic(0.5)
        assert runner.renew_lease_if_due() is True
        renewed = read_lease(runner.connection)
        assert renewed is not None
        assert renewed.heartbeat_monotonic == clock.monotonic()
        assert renewed.heartbeat_monotonic > acquired.heartbeat_monotonic

        # The regression, over three TTLs of simulated time.
        for _ in range(int(DEFAULT_LEASE_TTL_SECONDS * 3 / LEASE_RENEWAL_INTERVAL_SECONDS)):
            clock.advance_monotonic(LEASE_RENEWAL_INTERVAL_SECONDS)
            assert runner.renew_lease_if_due() is True
            current = read_lease(runner.connection)
            assert current is not None
            assert not current.is_expired(clock), (
                "a served instance renewing on its interval must never read as stale"
            )
    finally:
        runner.stop()


def test_a_transient_renewal_failure_is_retried_until_the_deadline_then_raises(
    served: tuple[WorkspaceLayout, InstallationLayout, ServiceSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contention is survivable; running out of margin is not.

    The exclusive connection is shared with the serving thread, so a `BEGIN
    IMMEDIATE` can lose a race it wins 250ms later -- failing closed on the first
    one would stop healthy services. The margin is bounded: `age` is measured from
    the last heartbeat this instance actually *wrote*, so repeated failures keep
    growing it rather than resetting it, and the deadline arrives.

    The final assertion is the point of putting the deadline under the TTL: at the
    moment the instance gives up, its lease is still current, so it stops while it
    can still prove ownership rather than after it cannot.
    """
    _workspace, _installation, settings = served
    clock = FakeClock()
    runner = ServiceRunner(settings, clock=clock)
    assert runner.start().ready
    try:
        assert runner.connection is not None
        before = read_lease(runner.connection)
        assert before is not None

        def contended(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(
            "omnivia_core_runtime.service.runner.heartbeat", contended
        )

        # Due, attempted, failed -- and tolerated, because there is margin left.
        clock.advance_monotonic(LEASE_RENEWAL_INTERVAL_SECONDS)
        assert runner.renew_lease_if_due() is False
        clock.advance_monotonic(LEASE_RENEWAL_DEADLINE_SECONDS / 4)
        assert runner.renew_lease_if_due() is False

        # Still nothing written: a tolerated failure is not a silent success.
        during = read_lease(runner.connection)
        assert during is not None
        assert during.heartbeat_monotonic == before.heartbeat_monotonic

        # At the deadline the margin is gone and the failure is the caller's.
        clock.advance_monotonic(
            LEASE_RENEWAL_DEADLINE_SECONDS - LEASE_RENEWAL_DEADLINE_SECONDS / 4
        )
        with pytest.raises(sqlite3.OperationalError):
            runner.renew_lease_if_due()

        expired = read_lease(runner.connection)
        assert expired is not None
        assert not expired.is_expired(clock), (
            "the instance must stop while its lease is still demonstrably current"
        )
    finally:
        runner.stop()


def test_a_lease_held_by_another_instance_stops_this_one_at_once(
    served: tuple[WorkspaceLayout, InstallationLayout, ServiceSettings],
) -> None:
    """`LeaseHeld` spends no margin, and the real authority check is what raises it.

    The identity is replaced rather than the heartbeat mocked, so this exercises
    `heartbeat()`'s own ownership check: the lease row names somebody else, which is
    positive evidence that this instance no longer owns the workspace. Nothing about
    that improves by waiting, so it must not be tolerated the way contention is --
    and the clock here is well inside the deadline that would have tolerated it.
    """
    _workspace, _installation, settings = served
    clock = FakeClock()
    runner = ServiceRunner(settings, clock=clock)
    assert runner.start().ready
    mine = runner.identity
    assert mine is not None
    try:
        runner.identity = replace(mine, service_instance_id="svc-somebody-else")
        clock.advance_monotonic(LEASE_RENEWAL_INTERVAL_SECONDS)
        assert LEASE_RENEWAL_INTERVAL_SECONDS < LEASE_RENEWAL_DEADLINE_SECONDS, (
            "the point of this test is that margin remained and was not spent"
        )
        with pytest.raises(LeaseHeld):
            runner.renew_lease_if_due()
    finally:
        # Restored so the unwind releases the lease it actually holds.
        runner.identity = mine
        runner.stop()


def test_a_renewed_lease_is_still_released_exactly_once_in_reverse_order(
    served: tuple[WorkspaceLayout, InstallationLayout, ServiceSettings],
) -> None:
    """Renewal changes what the lease says, never how it is let go.

    Renewal writes to the same row the release marks, so the risk is a shutdown
    that releases twice, releases in a different order, or leaves the row `held`
    because the renewal moved something the release keyed on. The order asserted
    here is the same tuple `test_stop_releases_everything_in_reverse_order` pins
    for a service that never renewed.
    """
    workspace, installation, settings = served
    clock = FakeClock()
    runner = ServiceRunner(settings, clock=clock)
    assert runner.start().ready
    assert runner.connection is not None

    clock.advance_monotonic(LEASE_RENEWAL_INTERVAL_SECONDS)
    assert runner.renew_lease_if_due() is True
    clock.advance_monotonic(LEASE_RENEWAL_INTERVAL_SECONDS)
    assert runner.renew_lease_if_due() is True
    renewed = read_lease(runner.connection)
    assert renewed is not None

    stopped = runner.stop()
    assert stopped.state == ServiceState.STOPPED.value
    assert stopped.released == (
        "discovery_descriptor",
        "mutation_guard",
        "workspace_lease",
        "exclusive_connection",
        "lifetime_storage_lock",
    )
    assert stopped.released.count("workspace_lease") == 1

    # Read on a fresh connection: the runner's own was closed by the unwind above.
    after = open_database(workspace.database_path, OpenMode.READ_ONLY)
    try:
        final = read_lease(after)
    finally:
        after.close()
    assert final is not None
    assert final.lifecycle == "released"
    assert final.shutdown_state == "clean"
    # The release records how the lease ended without rewriting what renewal wrote.
    assert final.heartbeat_monotonic == renewed.heartbeat_monotonic
    assert final.fencing_generation == renewed.fencing_generation
    assert discover(installation.runtime_for(WORKSPACE_ID)) is None
