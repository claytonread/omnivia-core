"""Core Service lifecycle and writable readiness (T-0629G, ADR-037).

Writable readiness is published **last**, and only when all nine preconditions hold
at the *same service instance*. Publishing earlier would advertise a service that
cannot yet safely write, and a client that connected in that window would be
writing through an unfenced path.

Resources release in reverse acquisition order on every failed transition. That is
not tidiness: the lifetime storage lock must outlive the SQLite connection it
protects, so releasing in acquisition order would drop the lock while a connection
was still open and let a successor take over against a live writer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum


class ServiceState(str, Enum):
    """The ten lifecycle states T-0629G requires."""

    STOPPED = "stopped"
    STARTING = "starting"
    RECOVERING = "recovering"
    MIGRATING = "migrating"
    READY = "ready"
    RUNNING = "running"
    DRAINING = "draining"
    MAINTENANCE = "maintenance"
    FAILED = "failed"

    @property
    def advertises_writable(self) -> bool:
        return self in (ServiceState.READY, ServiceState.RUNNING)

    @property
    def accepts_new_mutations(self) -> bool:
        """Draining completes in-flight work but admits nothing new."""
        return self in (ServiceState.READY, ServiceState.RUNNING)


#: Permitted transitions. A move not listed here is a bug, not a policy choice.
LEGAL_TRANSITIONS: dict[ServiceState, frozenset[ServiceState]] = {
    ServiceState.STOPPED: frozenset({ServiceState.STARTING}),
    ServiceState.STARTING: frozenset(
        {
            ServiceState.RECOVERING,
            ServiceState.MIGRATING,
            ServiceState.MAINTENANCE,
            ServiceState.FAILED,
            ServiceState.STOPPED,
        }
    ),
    ServiceState.MIGRATING: frozenset(
        {ServiceState.RECOVERING, ServiceState.FAILED, ServiceState.STOPPED}
    ),
    ServiceState.RECOVERING: frozenset(
        {ServiceState.READY, ServiceState.FAILED, ServiceState.STOPPED}
    ),
    ServiceState.READY: frozenset(
        {ServiceState.RUNNING, ServiceState.DRAINING, ServiceState.FAILED}
    ),
    ServiceState.RUNNING: frozenset(
        {ServiceState.DRAINING, ServiceState.MAINTENANCE, ServiceState.FAILED}
    ),
    ServiceState.DRAINING: frozenset({ServiceState.STOPPED, ServiceState.FAILED}),
    ServiceState.MAINTENANCE: frozenset(
        {ServiceState.RUNNING, ServiceState.DRAINING, ServiceState.STOPPED, ServiceState.FAILED}
    ),
    ServiceState.FAILED: frozenset({ServiceState.STOPPED}),
}


class LifecycleError(Exception):
    """An illegal lifecycle transition was attempted."""


class ReadinessRefused(Exception):
    """Writable readiness was refused because a precondition does not hold."""


@dataclass(frozen=True)
class ReadinessRequirements:
    """The nine conditions ADR-037 requires, all at the same service instance.

    Every field is a separate fact rather than one aggregate boolean, so a refusal
    can name what was missing. An operator told only "not ready" has to guess.
    """

    compatible_manifest: bool = False
    qualified_filesystem: bool = False
    holds_lifetime_storage_lock: bool = False
    sole_exclusive_connection: bool = False
    exact_current_lease_tuple: bool = False
    canonical_migration_checksums: bool = False
    integrity_check_passed: bool = False
    exact_schema_and_trigger_fingerprint: bool = False
    migrations_and_jobs_recovered: bool = False

    def unmet(self) -> list[str]:
        return [name for name, value in vars(self).items() if not value]

    @property
    def satisfied(self) -> bool:
        return not self.unmet()


@dataclass
class ResourceStack:
    """Acquired resources, released in reverse order.

    Reverse order is a correctness requirement, not a convention. The lifetime
    storage lock is acquired before the SQLite connection and must be released
    after it: dropping the lock first would let a successor take over while this
    instance still held an open writable connection.
    """

    _entries: list[tuple[str, Callable[[], None]]] = field(default_factory=list)

    def push(self, name: str, release: Callable[[], None]) -> None:
        self._entries.append((name, release))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self._entries]

    def unwind(self) -> list[str]:
        """Release everything in reverse order, returning the order used.

        A failing release does not stop the unwind: leaving later resources held
        because an earlier one raised is how a crashed startup keeps a workspace
        locked.
        """
        released: list[str] = []
        while self._entries:
            name, release = self._entries.pop()
            try:
                release()
            except Exception:  # noqa: BLE001,S110 - cleanup must continue regardless
                pass
            released.append(name)
        return released


@dataclass
class ServiceLifecycle:
    """The service state machine, with its transition history."""

    state: ServiceState = ServiceState.STOPPED
    history: list[ServiceState] = field(default_factory=lambda: [ServiceState.STOPPED])
    resources: ResourceStack = field(default_factory=ResourceStack)
    readiness: ReadinessRequirements = field(default_factory=ReadinessRequirements)
    last_failure: str | None = None

    def can_transition_to(self, target: ServiceState) -> bool:
        return target in LEGAL_TRANSITIONS.get(self.state, frozenset())

    def transition_to(self, target: ServiceState) -> ServiceState:
        if not self.can_transition_to(target):
            raise LifecycleError(f"illegal transition {self.state.value} -> {target.value}")
        self.state = target
        self.history.append(target)
        return target

    def publish_readiness(self, requirements: ReadinessRequirements) -> ServiceState:
        """Move to READY only when every precondition holds.

        Called last in startup. A refusal records the failure, unwinds resources and
        leaves the service FAILED, so nothing is advertised.
        """
        self.readiness = requirements
        if not requirements.satisfied:
            self.fail(f"readiness refused; unmet: {requirements.unmet()}")
            raise ReadinessRefused(
                f"writable readiness refused; unmet preconditions: {requirements.unmet()}"
            )
        return self.transition_to(ServiceState.READY)

    def fail(self, reason: str) -> list[str]:
        """Enter FAILED and unwind every acquired resource in reverse order."""
        self.last_failure = reason
        released = self.resources.unwind()
        if self.state is not ServiceState.FAILED:
            self.state = ServiceState.FAILED
            self.history.append(ServiceState.FAILED)
        return released

    def stop(self) -> list[str]:
        """Stop cleanly, unwinding resources in reverse order."""
        released = self.resources.unwind()
        if self.state is not ServiceState.STOPPED:
            self.state = ServiceState.STOPPED
            self.history.append(ServiceState.STOPPED)
        return released

    @property
    def advertises_writable(self) -> bool:
        return self.state.advertises_writable


__all__ = [
    "LEGAL_TRANSITIONS",
    "LifecycleError",
    "ReadinessRefused",
    "ReadinessRequirements",
    "ResourceStack",
    "ServiceLifecycle",
    "ServiceState",
]
