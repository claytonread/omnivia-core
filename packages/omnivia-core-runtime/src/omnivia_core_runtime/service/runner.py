"""The startup sequence that turns a workspace into an owned, ready service.

This wires together every earlier slice in the order ADR-037 mandates, and its
ordering *is* the safety property: manifest compatibility before any acquisition,
the storage lock before the exclusive connection, the connection before the
generation, recovery before readiness, and readiness published last.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from omnivia_core.contracts.v1 import ServiceEndpointDescriptor, ServiceProcessEvidence
from omnivia_core.workspace.compatibility import evaluate_compatibility
from omnivia_core_runtime.ownership.discovery import compare_and_clean, publish
from omnivia_core_runtime.ownership.fencing import (
    assert_guards_intact,
    close_guard,
    fenced_transaction,
    open_guard,
    verify_fingerprint,
)
from omnivia_core_runtime.ownership.identity import (
    Clock,
    InstallationIdentity,
    ServiceInstanceIdentity,
    SystemClock,
    SystemProcessEvidence,
)
from omnivia_core_runtime.ownership.lease import (
    DEFAULT_LEASE_TTL_SECONDS,
    LeaseHeld,
    acquire_lease,
    assert_current_authority,
    heartbeat,
    release_lease,
)
from omnivia_core_runtime.ownership.locks import (
    LockRole,
    create_lock,
    qualify_filesystem,
)
from omnivia_core_runtime.service.bootstrap import refuse_incompatible_workspace
from omnivia_core_runtime.service.lifecycle import (
    ReadinessRefused,
    ReadinessRequirements,
    ServiceLifecycle,
    ServiceState,
)
from omnivia_core_runtime.service.probes import ServiceFacts
from omnivia_core_runtime.service.runtime_recovery import (
    RuntimeStartupRecovery,
    recover_runtime_startup,
)
from omnivia_core_runtime.service.runtime_scheduler import RuntimeScheduler
from omnivia_core_runtime.service.versions import (
    API_VERSION,
    PROTOCOL_VERSION,
    SERVER_VERSION,
    supported_api_versions,
    supported_workspace_versions,
    workspace_contract_version,
)
from omnivia_core_runtime.service.workflow_runtime import workflow_runtime_scheduler
from omnivia_core_runtime.storage.backup import InstallationLayout
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    StorageError,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.jobs import recover_stranded_application_jobs
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    canonical_schema_fingerprint,
    load_migrations,
    read_workspace_state,
    record_open_event,
)
from omnivia_core_runtime.workspace.layout import WorkspaceLayout
from omnivia_core_runtime.workspace.manifest_store import read_manifest

#: How often a served instance renews its lease.
#:
#: A third of `DEFAULT_LEASE_TTL_SECONDS`, so two consecutive attempts can be lost
#: and the heartbeat is still current. `heartbeat()` existed with no production
#: caller, which meant a live service's lease read as stale after the TTL while it
#: still held the lifetime storage lock and the exclusive connection -- the one
#: state ADR-037 invariant 8 has to investigate rather than trust.
LEASE_RENEWAL_INTERVAL_SECONDS = DEFAULT_LEASE_TTL_SECONDS / 3

#: How long renewal may keep failing before the instance stops serving.
#:
#: Failing closed on the *first* failure would stop a healthy service: the exclusive
#: connection is shared with the serving thread, so a `BEGIN IMMEDIATE` can lose a
#: race it will win 250ms later. Failing closed on none of them would serve against
#: a lease nothing renewed. This is the line between the two, and it is under the
#: TTL, so the instance stops while its lease is still demonstrably current.
LEASE_RENEWAL_DEADLINE_SECONDS = DEFAULT_LEASE_TTL_SECONDS * 2 / 3


@dataclass(frozen=True)
class ServiceSettings:
    """Everything the service needs to own one workspace."""

    workspace_root: Path
    installation_root: Path
    core_version: str = "0.1.0"
    endpoint: str | None = None
    probe_filesystem: bool = True


@dataclass(frozen=True)
class StartupReport:
    """What startup achieved, ready or not."""

    ready: bool
    state: str
    workspace_id: str | None
    fencing_generation: int | None
    service_instance_id: str | None
    unmet: tuple[str, ...]
    reason: str
    released: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "state": self.state,
            "workspace_id": self.workspace_id,
            "fencing_generation": self.fencing_generation,
            "service_instance_id": self.service_instance_id,
            "unmet": list(self.unmet),
            "reason": self.reason,
            "released": list(self.released),
        }


class ServiceRunner:
    """Owns one workspace for the lifetime of this process."""

    def __init__(
        self, settings: ServiceSettings, *, clock: Clock | None = None
    ) -> None:
        self.settings = settings
        self.clock: Clock = clock or SystemClock()
        self.layout = WorkspaceLayout(root=settings.workspace_root)
        self.installation = InstallationLayout(root=settings.installation_root)
        self.lifecycle = ServiceLifecycle()
        self.identity: ServiceInstanceIdentity | None = None
        self.connection: sqlite3.Connection | None = None
        self.generation: int | None = None
        self.workspace_id: str | None = None
        self.workspace_format_ordinal: str | None = None
        #: What the fenced runtime startup pass found and repaired, once it has run.
        #: `None` before startup and for an instance whose recovery refused, so a reader
        #: can tell "nothing was found" from "the pass never reached a verdict".
        self.runtime_recovery: RuntimeStartupRecovery | None = None
        #: Monotonic reading of the last heartbeat this instance wrote, which is the
        #: acquisition itself until the first renewal. `None` until a lease is held,
        #: so nothing can renew before there is something to renew.
        self._lease_renewed_at: float | None = None

    # --- startup -------------------------------------------------------------

    def start(
        self, *, serve: Callable[[ServiceRunner], None] | None = None
    ) -> StartupReport:
        """Run the full startup sequence, publishing readiness last.

        `serve` starts whatever answers on the advertised endpoint. It runs after
        every readiness precondition is satisfied and *before* the discovery
        descriptor is written, so readiness is never observable before the endpoint
        accepts, and a transport that cannot start leaves nothing advertised. It
        should push its own shutdown onto `lifecycle.resources` so it is released in
        reverse order with everything else.
        """
        self.lifecycle.transition_to(ServiceState.STARTING)
        try:
            return self._start(serve=serve)
        except ReadinessRefused as refused:
            return StartupReport(
                ready=False,
                state=self.lifecycle.state.value,
                workspace_id=self.workspace_id,
                fencing_generation=self.generation,
                service_instance_id=(
                    None if self.identity is None else self.identity.service_instance_id
                ),
                unmet=tuple(self.lifecycle.readiness.unmet()),
                reason=str(refused),
                released=tuple(self.lifecycle.resources.names),
            )
        except Exception as error:  # noqa: BLE001 - every failure must clean up
            released = self.lifecycle.fail(str(error))
            self._clean_discovery()
            return StartupReport(
                ready=False,
                state=self.lifecycle.state.value,
                workspace_id=self.workspace_id,
                fencing_generation=self.generation,
                service_instance_id=(
                    None if self.identity is None else self.identity.service_instance_id
                ),
                unmet=tuple(self.lifecycle.readiness.unmet()),
                reason=str(error),
                released=tuple(released),
            )

    def _start(
        self, *, serve: Callable[[ServiceRunner], None] | None = None
    ) -> StartupReport:
        settings = self.settings

        # 1. Compatibility first, before anything is acquired, so a refusal has
        #    nothing to unwind.
        manifest = read_manifest(self.layout)
        self.workspace_id = manifest.workspace_id
        self.workspace_format_ordinal = manifest.compatibility.workspace_format_version
        compatibility = evaluate_compatibility(manifest, settings.core_version)
        refuse_incompatible_workspace(compatibility)

        # 2. Filesystem qualification, also before acquisition.
        qualification = qualify_filesystem(
            self.layout.root, probe_locking=settings.probe_filesystem
        )
        if not qualification.writable:
            raise RuntimeError(f"filesystem refused: {qualification.reason}")

        self.installation.create(manifest.workspace_id)
        installation_identity = InstallationIdentity.load_or_create(
            self.installation.runtime_for(manifest.workspace_id)
        )
        self.identity = ServiceInstanceIdentity.create(
            installation_identity, SystemProcessEvidence()
        )

        # 3. The lifetime storage lock, before the connection.
        storage_lock = create_lock(
            self.layout.locks_path / "storage.lock",
            LockRole.LIFETIME_STORAGE,
            {"service_instance_id": self.identity.service_instance_id},
        )
        if not storage_lock.acquire():
            raise RuntimeError("another service holds the lifetime storage lock")
        self.lifecycle.resources.push("lifetime_storage_lock", storage_lock.release)

        # 4. The sole exclusive SQLite connection, after the lock.
        connection = open_database(self.layout.database_path, OpenMode.SERVICE_OWNED)
        self.connection = connection
        self.lifecycle.resources.push("exclusive_connection", connection.close)

        # 5. Migrations, then the generation, then recovery.
        state = read_workspace_state(connection)
        if state is None:
            raise RuntimeError(
                "workspace has no ownership substrate; migrate it before serving"
            )

        lease = acquire_lease(
            connection,
            self.identity,
            clock=self.clock,
            workspace_id=manifest.workspace_id,
            holds_storage_lock=storage_lock.held,
            lock_mechanism=("flock" if os.name != "nt" else "msvcrt"),
            endpoint=settings.endpoint,
        )
        self.generation = lease.fencing_generation
        self._lease_renewed_at = lease.heartbeat_monotonic
        self.lifecycle.resources.push("workspace_lease", self._release_lease)

        pending = [
            migration
            for migration in load_migrations()
            if migration.version not in applied_migrations(connection)
        ]
        if pending:
            self.lifecycle.transition_to(ServiceState.MIGRATING)
            apply_pending_migrations(
                connection,
                mode=OpenMode.SERVICE_OWNED,
                service_instance_id=self.identity.service_instance_id,
                fencing_generation=lease.fencing_generation,
                workspace_id=manifest.workspace_id,
            )

        self.lifecycle.transition_to(ServiceState.RECOVERING)
        open_guard(
            connection,
            self.identity,
            clock=self.clock,
            workspace_id=manifest.workspace_id,
            fencing_generation=lease.fencing_generation,
        )
        self.lifecycle.resources.push("mutation_guard", lambda: close_guard(connection))
        recovered = self._recover(connection)
        record_open_event(
            connection,
            open_mode=OpenMode.SERVICE_OWNED,
            service_instance_id=self.identity.service_instance_id,
            fencing_generation=lease.fencing_generation,
        )

        # 6. Every readiness precondition, evaluated at this instance.
        requirements = self._evaluate_readiness(
            connection,
            compatible_manifest=compatibility.writable,
            qualified_filesystem=qualification.writable,
            holds_lock=storage_lock.held,
            recovered=recovered,
        )
        self.lifecycle.publish_readiness(requirements)

        # 7. The endpoint starts before anything advertises that it exists.
        #
        # Ordering, not decoration. Publishing first left two failures: a discovery
        # race where a launcher connected to an endpoint that was not listening yet,
        # and -- when the endpoint failed to start at all -- a ready descriptor for a
        # process that then exited. A failure here raises before publication, so the
        # `except` in `start()` unwinds the resource stack and no descriptor is ever
        # written.
        if serve is not None:
            self._start_transport(serve)

        # 8. Readiness is advertised last.
        #
        # Nothing is advertised when this instance has no endpoint to advertise.
        # `endpoint_uri` is a dialable address, and an in-process runner has none
        # to give: the descriptor's whole purpose is to tell another process where
        # to connect, so publishing one that names nowhere would be advertising a
        # service no reader can reach. The same condition already governs the
        # discovery probe, and it is the same descriptor.
        descriptor = self._endpoint_descriptor(
            self._observed_at(), ready=self.lifecycle.advertises_writable
        )
        if descriptor is not None:
            publish(self.installation.runtime_for(manifest.workspace_id), descriptor)
            self.lifecycle.resources.push("discovery_descriptor", self._clean_discovery)

        return StartupReport(
            ready=True,
            state=self.lifecycle.state.value,
            workspace_id=manifest.workspace_id,
            fencing_generation=lease.fencing_generation,
            service_instance_id=self.identity.service_instance_id,
            unmet=(),
            reason="writable readiness published",
        )

    def _start_transport(self, serve: Callable[[ServiceRunner], None]) -> None:
        """Run the transport hook without publishing its raw failure diagnostics."""
        failed = False
        try:
            serve(self)
        except Exception:  # noqa: BLE001 - the public report is structural only
            failed = True
        if failed:
            raise RuntimeError("local service transport start failed")

    def _observed_at(self) -> str:
        return self.clock.wall_time().astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _endpoint_descriptor(
        self, published_at: str, *, ready: bool
    ) -> ServiceEndpointDescriptor | None:
        """This instance as the public descriptor, or None with nothing to advertise.

        One construction, two consumers: the document `publish()` writes and the
        descriptor the `service.discover` probe answers with are the same value
        built from the same facts. Building it twice is how a reader of the file
        and a caller of the probe end up being told two different things about one
        service.

        `ready` is passed in rather than read here. It is live state that a
        concurrent drain or stop moves, so a caller that also reports it -- the
        probe does, as its own `status` -- must read it once and hand the same
        value to both, or a single probe can answer `pass` beside a descriptor
        that says `ready: false`.

        None when any fact is still missing, and in particular when no endpoint was
        configured -- see the publication step for why that is a refusal to
        advertise rather than a placeholder.
        """
        if (
            self.identity is None
            or self.workspace_id is None
            or self.workspace_format_ordinal is None
            or self.generation is None
            or self.settings.endpoint is None
        ):
            return None
        return ServiceEndpointDescriptor(
            descriptor_version=API_VERSION,
            workspace_id=self.workspace_id,
            service_instance_id=self.identity.service_instance_id,
            installation_id=self.identity.installation_id,
            endpoint_uri=self.settings.endpoint,
            protocol_version=PROTOCOL_VERSION,
            server_version=SERVER_VERSION,
            supported_api_versions=supported_api_versions(),
            supported_workspace_versions=supported_workspace_versions(
                self.workspace_format_ordinal
            ),
            workspace_format_version=workspace_contract_version(
                self.workspace_format_ordinal
            ),
            ready=ready,
            lifecycle_state=self.lifecycle.state.value,
            fencing_generation=self.generation,
            published_at=published_at,
            process=ServiceProcessEvidence(
                pid=self.identity.process.pid,
                start_time=self.identity.process.start_time,
                boot_id=self.identity.process.boot_id,
            ),
        )

    def probe_facts(self) -> ServiceFacts:
        """Project this live instance into the accepted public probe snapshot."""
        observed_at = self._observed_at()
        ready = self.lifecycle.advertises_writable
        descriptor = self._endpoint_descriptor(observed_at, ready=ready)
        status = "pass" if ready else "fail"
        return ServiceFacts(
            observed_at=observed_at,
            health_status="fail"
            if self.lifecycle.state is ServiceState.FAILED
            else "pass",
            readiness_status=status,
            discovery_status="pass" if descriptor is not None else "fail",
            descriptor=descriptor,
        )

    def _evaluate_readiness(
        self,
        connection: sqlite3.Connection,
        *,
        compatible_manifest: bool,
        qualified_filesystem: bool,
        holds_lock: bool,
        recovered: bool,
    ) -> ReadinessRequirements:
        assert self.identity is not None
        assert self.workspace_id is not None
        assert self.generation is not None

        try:
            assert_current_authority(
                connection, self.identity, self.workspace_id, self.generation
            )
            lease_current = True
        except Exception:  # noqa: BLE001 - a refusal is a readiness fact
            lease_current = False

        checksums_match = all(
            migration.checksum == applied_migrations(connection).get(migration.version)
            for migration in load_migrations()
        )

        try:
            assert_guards_intact(connection)
            # The expectation comes from the frozen migration artifacts, never from
            # the database being judged.
            verify_fingerprint(connection, canonical_schema_fingerprint())
            fingerprint_ok = True
        except Exception:  # noqa: BLE001
            fingerprint_ok = False

        return ReadinessRequirements(
            compatible_manifest=compatible_manifest,
            qualified_filesystem=qualified_filesystem,
            holds_lifetime_storage_lock=holds_lock,
            sole_exclusive_connection=self.connection is connection,
            exact_current_lease_tuple=lease_current,
            canonical_migration_checksums=checksums_match,
            integrity_check_passed=not integrity_check(connection),
            exact_schema_and_trigger_fingerprint=fingerprint_ok,
            migrations_and_jobs_recovered=recovered,
        )

    def runtime_scheduler(self) -> RuntimeScheduler | None:
        """The scheduler this instance runs canonical Runs with, or `None` before startup.

        One accessor rather than a construction at each call site, because a scheduler is
        only ever this instance's: it carries the exclusive connection, this service
        identity and the generation the lease granted, and a second one built from
        anything else would be a writer this workspace has not authorised. It is composed
        with the Workflow step plan, so a Workflow Run's dependencies are respected and an
        `agent_component` Run behaves exactly as it did.

        This is the seam a caller drives execution through -- and the one recovery
        already uses, which is what makes it reachable rather than a constructor tests
        happen to know about.
        """
        if self.connection is None or self.identity is None or self.generation is None:
            return None
        if self.workspace_id is None:  # pragma: no cover - set before the connection is
            return None
        return workflow_runtime_scheduler(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.generation,
            clock=self.clock,
        )

    def _recover(self, connection: sqlite3.Connection) -> bool:
        """Recover interrupted migrations, runtime-bound runs and durable jobs.

        Any job left `claimed` by a previous generation is returned to `queued`: its
        claimant cannot still be running, because a new generation exists.

        **The runtime pass runs first, and the order is the safety property.** RT-109
        classifies every runtime-bound job from persisted evidence and repairs the two
        repairable states: a stale claim over a run suspended on a durable `Wait` is
        *adopted*, preserving the wait, its step and its running attempt so the wait's own
        resolution still resumes them, and an orphaned attempt is recovered through the
        bounded scheduler recovery. The blanket application sweep below cannot tell those
        apart -- it requeues every stale claim -- so running it first would fail the
        attempt a durable wait is still holding and destroy the suspension. Afterwards,
        every runtime-bound job it would have swept is either adopted at this generation
        or already recovered, so the sweep passes over them.

        **A pass that cannot read the history refuses readiness rather than continuing.**
        `RuntimeSchedulingError` is raised for evidence this build cannot classify at all,
        and serving a workspace whose runtime history could not be read would be serving
        one whose Runs may be claimed twice. `contradictory_history` is not that: it is a
        classification, reported and left alone, and it does not stop the service.
        """
        attempts = connection.execute(
            "SELECT COUNT(*) FROM omnivia_migration_attempts WHERE outcome = 'started'"
        ).fetchone()
        if attempts is not None and int(attempts[0]) > 0:
            return False

        assert self.generation is not None
        assert self.identity is not None
        assert self.workspace_id is not None

        scheduler = self.runtime_scheduler()
        assert scheduler is not None
        try:
            self.runtime_recovery = recover_runtime_startup(scheduler)
        except StorageError:
            # `RuntimeSchedulingError` is one of these, which is the whole point: a
            # runtime history this build cannot read and a storage refusal underneath it
            # are the same fact about this workspace, and both leave recovery unproven.
            return False

        # `omnivia_durable_jobs` is a guarded table, so requeuing is a fenced write
        # like any other. It ran as a bare transaction before, which worked only
        # because nothing checked: the triggers saw an open guard and the authorizer
        # that would have refused it was never installed. Routing it through
        # `fenced_transaction` also gives recovery the entry and pre-commit authority
        # checks it always should have had -- a takeover during recovery must not
        # commit under the superseded generation.
        now_us = int(self.clock.wall_time().timestamp() * 1_000_000)
        recover_stranded_application_jobs(
            connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.generation,
            now_us=now_us,
            clock=self.clock,
        )
        with fenced_transaction(
            connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.generation,
        ):
            connection.execute(
                "UPDATE omnivia_durable_jobs SET state = 'queued', "
                "claimed_by_service_instance = NULL "
                "WHERE state = 'claimed' AND COALESCE(fencing_generation, 0) < ? "
                "AND NOT EXISTS (SELECT 1 FROM omnivia_job_application_metadata m "
                "WHERE m.job_id = omnivia_durable_jobs.job_id)",
                (self.generation,),
            )
        return True

    # --- keeping the lease current -------------------------------------------

    def renew_lease_if_due(self) -> bool:
        """Renew this instance's lease when the interval has elapsed.

        Returns whether a heartbeat was written, so a caller sees the difference
        between "not due yet" and "renewed" without reading the lease back.

        **Called from the main service wait loop, and that is a constraint rather
        than a convenience.** SQLite has one owning thread, the exclusive connection
        was opened on the main thread, and a lease is not authority that may be held
        through a second writable connection -- so renewal is a method the loop
        drives on that same thread, not a timer with a connection of its own.

        **Fails closed by raising, and distinguishes the two ways it can fail.**
        `LeaseHeld` is positive evidence that another instance now owns this
        workspace, so it stops the run at once with no margin spent on it. Anything
        else -- contention on the shared connection above all -- is tolerated until
        `LEASE_RENEWAL_DEADLINE_SECONDS`, past which this instance can no longer show
        its lease is current and must stop serving. Both are under the TTL.
        """
        assert self.connection is not None
        assert self.identity is not None
        assert self._lease_renewed_at is not None

        now = self.clock.monotonic()
        age = now - self._lease_renewed_at
        if age < LEASE_RENEWAL_INTERVAL_SECONDS:
            return False
        try:
            heartbeat(self.connection, self.identity, clock=self.clock)
        except LeaseHeld:
            raise
        except Exception:
            if age < LEASE_RENEWAL_DEADLINE_SECONDS:
                # Retried on the next 250ms tick, not swallowed: `age` keeps growing
                # from the last heartbeat this instance actually wrote.
                return False
            raise
        self._lease_renewed_at = now
        return True

    # --- shutdown ------------------------------------------------------------

    def _release_lease(self) -> None:
        if self.connection is None or self.identity is None:
            return
        try:
            release_lease(self.connection, self.identity, clock=self.clock)
        except Exception:  # noqa: BLE001,S110 - shutdown must not raise
            pass

    def _clean_discovery(self) -> None:
        if self.identity is None or self.workspace_id is None:
            return
        compare_and_clean(
            self.installation.runtime_for(self.workspace_id),
            self.identity.service_instance_id,
        )

    def drain(self) -> ServiceState:
        """Stop admitting new mutations while completing in-flight work."""
        return self.lifecycle.transition_to(ServiceState.DRAINING)

    def stop(self) -> StartupReport:
        """Release everything in reverse acquisition order."""
        if self.lifecycle.state.advertises_writable:
            self.lifecycle.transition_to(ServiceState.DRAINING)
        released = self.lifecycle.stop()
        return StartupReport(
            ready=False,
            state=self.lifecycle.state.value,
            workspace_id=self.workspace_id,
            fencing_generation=self.generation,
            service_instance_id=(
                None if self.identity is None else self.identity.service_instance_id
            ),
            unmet=(),
            reason="stopped",
            released=tuple(released),
        )


__all__ = [
    "LEASE_RENEWAL_DEADLINE_SECONDS",
    "LEASE_RENEWAL_INTERVAL_SECONDS",
    "ServiceRunner",
    "ServiceSettings",
    "StartupReport",
]
