"""Runtime Execution Planes: the static, source-qualified executor registry.

Static, and the word is load-bearing: nothing here discovers, probes, downloads
or infers an entry. A descriptor is in this registry because someone registered
it, and resolution answers only for the exact
``(source_id, <unit>_id, version)`` triple it was registered under.

One registry holds both executors and session providers. They are the same
record with two shapes -- a source-qualified exact-version key, a build hash, a
build trust state and a health posture -- and two registries would be one rule
written twice.

Four consequences worth stating separately, because each one is a way a
registry usually goes wrong:

- **Source-qualified.** Two sources may legitimately publish the same
  ``executor_id``. The source is part of the key, so one never shadows or
  satisfies a request for the other.
- **Exact version.** There is no range, no ``latest``, no nearest match. A
  request for ``1.0.0`` is not answered by ``1.0.1``, and an executor speaks a
  contract version because it *lists* that exact version.
- **Exact build.** A caller that pins the descriptor identity it resolved
  against gets a refusal, never the replacement, when that entry has been
  re-registered under a different build. An exact active reference must never
  resolve to a replacement silently.
- **Fail closed.** Resolution refuses unless the entry exists, its health is
  routable, its build trust is ``APPROVED``, it runs at least as isolated as the
  caller requires, and it declares what was asked for -- the contract version
  and capability for an executor, the session kind and lifecycle support for a
  provider. Every other state -- unknown, degraded, quarantined, removed,
  mid-replacement -- is a refusal, not a best effort.

History is append-only and outlives the entries: replacing or removing an entry
keeps the replaced descriptor's content hash *and* build hash in
:attr:`history`, so "which build was routable when this ran" stays answerable
after the entry itself is gone. That history is an in-memory record for the
caller, not canonical state -- this module writes nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Final

from omnivia_core_runtime.execution.profile import (
    BUILD_TRUST_APPROVED,
    CONTRACT_VERSION,
    HEALTH_CONFIGURED,
    HEALTH_QUARANTINED,
    HEALTH_STATES,
    ISOLATION_MIN,
    ROUTABLE_HEALTH,
    SESSION_KINDS,
    ExecutionRefused,
    ExecutorDescriptor,
    SessionProviderDescriptor,
    require_collection,
    require_identifier,
    require_isolation,
    require_version,
    require_vocabulary,
)

#: ``(source_id, unit_id, version)``. A plain tuple rather than a wrapper type:
#: it is a dictionary key first and a value second, and a class around three
#: strings would add a name without adding a rule.
EntryKey = tuple[str, str, str]

ENTRY_EXECUTOR: Final = "EXECUTOR"
ENTRY_SESSION_PROVIDER: Final = "SESSION_PROVIDER"

EVENT_REGISTERED: Final = "REGISTERED"
EVENT_REPLACED: Final = "REPLACED"
EVENT_REMOVED: Final = "REMOVED"
EVENT_HEALTH_CHANGED: Final = "HEALTH_CHANGED"

RegisteredDescriptor = ExecutorDescriptor | SessionProviderDescriptor


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """One registered build and the health posture it currently carries."""

    entry_kind: str
    descriptor: RegisteredDescriptor
    health: str
    revision: int

    @property
    def key(self) -> EntryKey:
        return self.descriptor.key

    @property
    def is_routable(self) -> bool:
        return self.health in ROUTABLE_HEALTH


@dataclass(frozen=True, slots=True)
class RegistryEvent:
    """One append-only history record.

    Carries the descriptor's *identity* -- content hash and build hash -- rather
    than the descriptor, so history stays a fixed-size record of which build was
    registered, and survives the replacement that made it history.
    """

    kind: str
    entry_kind: str
    key: EntryKey
    content_hash: str
    build_hash: str
    health: str
    revision: int


class RuntimeExecutionRegistry:
    """A static, source-qualified, exact-version, health-aware, fail-closed registry."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, EntryKey], RegistryEntry] = {}
        self._history: list[RegistryEvent] = []
        # Never cleared on removal, so a re-registration after a removal
        # continues the revision line instead of restarting it and looking like
        # a first registration in the history.
        self._revisions: dict[tuple[str, EntryKey], int] = {}

    @property
    def history(self) -> tuple[RegistryEvent, ...]:
        """Every registration, replacement, health change and removal, in order."""
        return tuple(self._history)

    def entries(self) -> tuple[RegistryEntry, ...]:
        """Every currently registered entry, in deterministic kind-then-key order."""
        return tuple(self._entries[slot] for slot in sorted(self._entries))

    def register_executor(
        self, descriptor: ExecutorDescriptor, *, health: str = HEALTH_CONFIGURED
    ) -> RegistryEntry:
        """Register an executor build, replacing any entry under the same exact key."""
        return self._register(ENTRY_EXECUTOR, descriptor, health)

    def register_session_provider(
        self, descriptor: SessionProviderDescriptor, *, health: str = HEALTH_CONFIGURED
    ) -> RegistryEntry:
        """Register a provider build, replacing any entry under the same exact key."""
        return self._register(ENTRY_SESSION_PROVIDER, descriptor, health)

    def set_health(self, entry_kind: str, key: EntryKey, health: str) -> RegistryEntry:
        """Move an entry's health posture, refusing to bring one back from quarantine."""
        self._require_health(health)
        entry = self._require_entry(entry_kind, key)
        if entry.health == HEALTH_QUARANTINED and health != HEALTH_QUARANTINED:
            raise ExecutionRefused(
                "quarantine_is_terminal",
                "a quarantined entry is only cleared by registering a replacement",
            )
        updated = replace(entry, health=health)
        self._entries[(entry_kind, key)] = updated
        self._record(EVENT_HEALTH_CHANGED, updated)
        return updated

    def remove(self, entry_kind: str, key: EntryKey) -> RegistryEntry:
        """Remove an entry from routing. Its history survives the removal."""
        entry = self._require_entry(entry_kind, key)
        del self._entries[(entry_kind, key)]
        self._record(EVENT_REMOVED, entry)
        return entry

    def resolve_executor(
        self,
        source_id: str,
        executor_id: str,
        version: str,
        *,
        capability: str,
        contract_version: str = CONTRACT_VERSION,
        required_reconciliation: Iterable[str] = (),
        minimum_isolation: int = ISOLATION_MIN,
        expect_content_hash: str | None = None,
    ) -> RegistryEntry:
        """Return the one executor entry that may serve ``capability``, or refuse."""
        require_identifier("capability", capability)
        require_version("contract_version", contract_version)
        reconciliation = tuple(required_reconciliation)
        require_collection("required_reconciliation", reconciliation, required=False)
        entry = self._resolvable(
            ENTRY_EXECUTOR,
            (source_id, executor_id, version),
            expect_content_hash,
            minimum_isolation,
        )
        descriptor = entry.descriptor
        if not isinstance(descriptor, ExecutorDescriptor):
            raise ExecutionRefused(
                "unknown_entry", f"{'/'.join(entry.key)} is not an executor"
            )
        if contract_version not in descriptor.supported_contract_versions:
            raise ExecutionRefused(
                "unsupported_contract_version",
                f"executor does not speak contract version {contract_version}",
            )
        if capability not in descriptor.capabilities:
            raise ExecutionRefused(
                "unsupported_capability",
                f"executor does not declare capability {capability!r}",
            )
        missing = [
            name
            for name in reconciliation
            if name not in descriptor.reconciliation_capabilities
        ]
        if missing:
            raise ExecutionRefused(
                "unsupported_reconciliation",
                f"executor does not declare reconciliation {missing[0]!r}",
            )
        return entry

    def resolve_session_provider(
        self,
        source_id: str,
        provider_id: str,
        version: str,
        *,
        session_kind: str,
        require_suspend_resume: bool = False,
        require_control_transfer: bool = False,
        minimum_isolation: int = ISOLATION_MIN,
        expect_content_hash: str | None = None,
    ) -> RegistryEntry:
        """Return the one provider entry that may serve ``session_kind``, or refuse."""
        require_vocabulary("session_kind", session_kind, SESSION_KINDS)
        entry = self._resolvable(
            ENTRY_SESSION_PROVIDER,
            (source_id, provider_id, version),
            expect_content_hash,
            minimum_isolation,
        )
        descriptor = entry.descriptor
        if not isinstance(descriptor, SessionProviderDescriptor):
            raise ExecutionRefused(
                "unknown_entry", f"{'/'.join(entry.key)} is not a session provider"
            )
        if session_kind not in descriptor.supported_kinds:
            raise ExecutionRefused(
                "unsupported_session_kind",
                f"provider does not serve session kind {session_kind!r}",
            )
        if require_suspend_resume and not descriptor.supports_suspend_resume:
            raise ExecutionRefused(
                "unsupported_session_feature",
                "provider does not support suspend and resume",
            )
        if require_control_transfer and not descriptor.supports_control_transfer:
            raise ExecutionRefused(
                "unsupported_session_feature",
                "provider does not support control transfer",
            )
        return entry

    def _register(
        self, entry_kind: str, descriptor: RegisteredDescriptor, health: str
    ) -> RegistryEntry:
        descriptor.verify_content_hash()
        self._require_health(health)
        slot = (entry_kind, descriptor.key)
        previous = self._entries.get(slot)
        if previous is not None:
            self._record(EVENT_REPLACED, previous)
        revision = self._revisions.get(slot, 0) + 1
        self._revisions[slot] = revision
        entry = RegistryEntry(entry_kind, descriptor, health, revision)
        self._entries[slot] = entry
        self._record(EVENT_REGISTERED, entry)
        return entry

    def _resolvable(
        self,
        entry_kind: str,
        key: EntryKey,
        expect_content_hash: str | None,
        minimum_isolation: int,
    ) -> RegistryEntry:
        """Return the entry under ``key`` if it is routable, approved and unmoved."""
        require_identifier("source_id", key[0])
        require_identifier(f"{entry_kind.lower()}_id", key[1])
        require_version("version", key[2])
        require_isolation("minimum_isolation", minimum_isolation)
        entry = self._entries.get((entry_kind, key))
        if entry is None:
            raise ExecutionRefused(
                "unknown_entry", f"no {entry_kind} registered as {'/'.join(key)}"
            )
        if (
            expect_content_hash is not None
            and entry.descriptor.content_hash != expect_content_hash
        ):
            # The pinned build is gone and a different one now answers to its
            # name. Refusing is the whole point: silently serving the successor
            # is how a caller ends up running a build it never resolved against.
            raise ExecutionRefused(
                "replaced_descriptor",
                f"{entry_kind} {'/'.join(key)} no longer carries the pinned identity",
            )
        if not entry.is_routable:
            raise ExecutionRefused(
                "unroutable_health", f"entry health is {entry.health!r}, not routable"
            )
        if entry.descriptor.trust_state != BUILD_TRUST_APPROVED:
            raise ExecutionRefused(
                "unsupported_trust",
                f"entry trust is {entry.descriptor.trust_state!r}, "
                f"not {BUILD_TRUST_APPROVED}",
            )
        if entry.descriptor.required_isolation < minimum_isolation:
            raise ExecutionRefused(
                "insufficient_isolation",
                f"entry runs at isolation {entry.descriptor.required_isolation}, "
                f"below the required {minimum_isolation}",
            )
        return entry

    def _require_entry(self, entry_kind: str, key: EntryKey) -> RegistryEntry:
        entry = self._entries.get((entry_kind, key))
        if entry is None:
            raise ExecutionRefused(
                "unknown_entry", f"no {entry_kind} registered as {'/'.join(key)}"
            )
        return entry

    def _require_health(self, health: str) -> None:
        if health not in HEALTH_STATES:
            raise ExecutionRefused(
                "unknown_vocabulary_member", f"{health!r} is not a health state"
            )

    def _record(self, kind: str, entry: RegistryEntry) -> None:
        self._history.append(
            RegistryEvent(
                kind,
                entry.entry_kind,
                entry.key,
                entry.descriptor.content_hash,
                entry.descriptor.build_hash,
                entry.health,
                entry.revision,
            )
        )


__all__ = [
    "ENTRY_EXECUTOR",
    "ENTRY_SESSION_PROVIDER",
    "EVENT_HEALTH_CHANGED",
    "EVENT_REGISTERED",
    "EVENT_REMOVED",
    "EVENT_REPLACED",
    "EntryKey",
    "RegisteredDescriptor",
    "RegistryEntry",
    "RegistryEvent",
    "RuntimeExecutionRegistry",
]
