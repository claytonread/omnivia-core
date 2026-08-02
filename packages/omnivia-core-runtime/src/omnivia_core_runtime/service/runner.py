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
from omnivia_core_runtime.ownership.discovery import (
    ReadinessState,
    ServiceDescriptor,
    compare_and_clean,
    publish,
)
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
    acquire_lease,
    assert_current_authority,
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
from omnivia_core_runtime.service.versions import (
    API_VERSION as PUBLIC_API_VERSION,
)
from omnivia_core_runtime.service.versions import (
    PROTOCOL_VERSION,
    SERVER_VERSION,
    supported_api_versions,
    supported_workspace_versions,
    workspace_contract_version,
)
from omnivia_core_runtime.storage.backup import InstallationLayout
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    integrity_check,
    open_database,
)
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

API_VERSION = "1.0"


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
            serve(self)

        # 8. Readiness is advertised last.
        publish(
            self.installation.runtime_for(manifest.workspace_id),
            ServiceDescriptor(
                workspace_id=manifest.workspace_id,
                service_instance_id=self.identity.service_instance_id,
                installation_id=installation_identity.installation_id,
                fencing_generation=lease.fencing_generation,
                endpoint=settings.endpoint or "in-process",
                readiness=ReadinessState.READY.value,
                api_version=API_VERSION,
                workspace_format_version=manifest.compatibility.workspace_format_version,
                pid=os.getpid(),
            ),
        )
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

    def probe_facts(self) -> ServiceFacts:
        """Project this live instance into the accepted public probe snapshot."""
        observed_at = (
            self.clock.wall_time().astimezone(UTC).isoformat().replace("+00:00", "Z")
        )
        ready = self.lifecycle.advertises_writable
        descriptor = None
        if (
            self.identity is not None
            and self.workspace_id is not None
            and self.workspace_format_ordinal is not None
            and self.generation is not None
            and self.settings.endpoint is not None
        ):
            workspace_version = workspace_contract_version(
                self.workspace_format_ordinal
            )
            descriptor = ServiceEndpointDescriptor(
                descriptor_version=PUBLIC_API_VERSION,
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
                workspace_format_version=workspace_version,
                ready=ready,
                lifecycle_state=self.lifecycle.state.value,
                fencing_generation=self.generation,
                published_at=observed_at,
                process=ServiceProcessEvidence(
                    pid=self.identity.process.pid,
                    start_time=self.identity.process.start_time,
                    boot_id=self.identity.process.boot_id,
                ),
            )
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

    def _recover(self, connection: sqlite3.Connection) -> bool:
        """Recover interrupted migrations and durable jobs.

        Any job left `claimed` by a previous generation is returned to `queued`: its
        claimant cannot still be running, because a new generation exists.
        """
        attempts = connection.execute(
            "SELECT COUNT(*) FROM omnivia_migration_attempts WHERE outcome = 'started'"
        ).fetchone()
        if attempts is not None and int(attempts[0]) > 0:
            return False

        assert self.generation is not None
        assert self.identity is not None
        assert self.workspace_id is not None

        # `omnivia_durable_jobs` is a guarded table, so requeuing is a fenced write
        # like any other. It ran as a bare transaction before, which worked only
        # because nothing checked: the triggers saw an open guard and the authorizer
        # that would have refused it was never installed. Routing it through
        # `fenced_transaction` also gives recovery the entry and pre-commit authority
        # checks it always should have had -- a takeover during recovery must not
        # commit under the superseded generation.
        with fenced_transaction(
            connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.generation,
        ):
            connection.execute(
                "UPDATE omnivia_durable_jobs SET state = 'queued', "
                "claimed_by_service_instance = NULL "
                "WHERE state = 'claimed' AND COALESCE(fencing_generation, 0) < ?",
                (self.generation,),
            )
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


__all__ = ["API_VERSION", "ServiceRunner", "ServiceSettings", "StartupReport"]
