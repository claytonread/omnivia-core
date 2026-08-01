"""Service startup coordination (T-0629G, ADR-037).

The startup sequence is:

```text
discover compatible service
→ if absent, acquire bootstrap mutex
→ repeat discovery
→ spawn Core Service
→ validate compatibility
→ acquire storage lock
→ establish the sole exclusive SQLite connection
→ atomically acquire the next generation
→ recover SQLite and durable jobs
→ publish readiness
→ release the bootstrap mutex
```

The second discovery is the part that is easy to omit and load-bearing. Between the
first discovery failing and the mutex being acquired, another launcher may have
started a service; without re-checking, both launchers spawn one.

The bootstrap mutex grants no write authority. Any client may hold it — that is the
whole point of separating it from the lifetime storage lock — so nothing in this
module treats holding it as ownership.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from omnivia_core.workspace.compatibility import CompatibilityOutcome
from omnivia_core_runtime.ownership.discovery import (
    ServiceDescriptor,
    discover,
    is_compatible,
)
from omnivia_core_runtime.ownership.identity import process_is_alive
from omnivia_core_runtime.ownership.locks import FileLock, LockRole, create_lock
from omnivia_core_runtime.service.transport import EndpointProbe, probe_endpoint


class StartupOutcome(str, Enum):
    """What a launcher should do after coordination."""

    USE_EXISTING = "use_existing"
    SPAWN = "spawn"
    ANOTHER_LAUNCHER_WON = "another_launcher_won"
    REFUSED_INCOMPATIBLE = "refused_incompatible"
    REFUSED_MUTEX_UNAVAILABLE = "refused_mutex_unavailable"


@dataclass(frozen=True)
class StartupDecision:
    """The coordination result, with the evidence behind it."""

    outcome: StartupOutcome
    reason: str
    existing: ServiceDescriptor | None = None

    @property
    def should_spawn(self) -> bool:
        return self.outcome is StartupOutcome.SPAWN


@contextmanager
def coordinated_startup(
    runtime_directory: Path,
    *,
    api_version: str,
    workspace_format_version: str,
) -> Iterator[StartupDecision]:
    """Coordinate startup, holding the bootstrap mutex for as long as the decision stands.

    A `spawn` decision is only true while this launcher still holds the mutex. Handing
    back a bare decision and releasing on the way out made the answer stale the moment
    it was given: the window the mutex exists to close is spawn-and-wait-for-readiness,
    which happens entirely in the caller, so a launcher that released first was telling
    the next launcher nothing and both spawned.

    The mutex is therefore released when the caller leaves this block -- after it has
    spawned and waited, or after it has failed -- and not before. It still grants no
    write authority; it serialises launching, nothing else.
    """
    decision, mutex = _decide(
        runtime_directory,
        api_version=api_version,
        workspace_format_version=workspace_format_version,
    )
    try:
        yield decision
    finally:
        if mutex is not None:
            mutex.release()


def _decide(
    runtime_directory: Path,
    *,
    api_version: str,
    workspace_format_version: str,
) -> tuple[StartupDecision, FileLock | None]:
    """Decide, returning the held mutex when this launcher won the right to spawn."""
    first = _live(discover(runtime_directory))
    if first is not None and first.ready:
        if is_compatible(
            first,
            api_version=api_version,
            workspace_format_version=workspace_format_version,
        ):
            return (
                StartupDecision(
                    outcome=StartupOutcome.USE_EXISTING,
                    reason="a compatible service is already ready",
                    existing=first,
                ),
                None,
            )
        return (
            StartupDecision(
                outcome=StartupOutcome.REFUSED_INCOMPATIBLE,
                reason=(
                    f"a service is ready but incompatible (api {first.api_version}, "
                    f"workspace format {first.workspace_format_version})"
                ),
                existing=first,
            ),
            None,
        )

    mutex = create_lock(
        runtime_directory / "bootstrap.lock", LockRole.BOOTSTRAP_MUTEX
    )
    if not mutex.acquire():
        # Another launcher holds the mutex, so it is spawning. Not an error.
        return (
            StartupDecision(
                outcome=StartupOutcome.REFUSED_MUTEX_UNAVAILABLE,
                reason=(
                    "another launcher holds the bootstrap mutex and is starting a service"
                ),
            ),
            None,
        )

    # Rediscovery: the window between the first discovery and the mutex is exactly
    # when another launcher may have finished starting a service.
    try:
        second = _live(discover(runtime_directory))
        if second is not None and second.ready:
            if is_compatible(
                second,
                api_version=api_version,
                workspace_format_version=workspace_format_version,
            ):
                decision = StartupDecision(
                    outcome=StartupOutcome.ANOTHER_LAUNCHER_WON,
                    reason="a compatible service appeared while acquiring the mutex",
                    existing=second,
                )
            else:
                decision = StartupDecision(
                    outcome=StartupOutcome.REFUSED_INCOMPATIBLE,
                    reason=(
                        "a service appeared while acquiring the mutex but is incompatible"
                    ),
                    existing=second,
                )
            # This launcher will not spawn, so it has no further use for the mutex.
            mutex.release()
            return decision, None
    except BaseException:
        mutex.release()
        raise

    # Won the right to spawn. The mutex stays held until the caller is done with it.
    return (
        StartupDecision(
            outcome=StartupOutcome.SPAWN,
            reason="no compatible service is running; this launcher will spawn one",
        ),
        mutex,
    )


def _live(descriptor: ServiceDescriptor | None) -> ServiceDescriptor | None:
    """The descriptor, unless the process it names is gone.

    A descriptor outlives its process whenever a service is killed before it can
    clean up -- SIGKILL, a power loss, a container stop. Trusting it then tells every
    launcher to use a service that is not running, and because they never reach the
    mutex, nobody spawns a replacement and nobody clears the file: the workspace
    stays stuck behind a dead endpoint until a human deletes it.

    `process_is_alive` was written for exactly this and, like the authorizer and the
    lease check before it, nothing outside the tests ever called it.

    A live pid is not enough on its own. Pids are recycled, so a descriptor left by a
    dead service can name a live process that has nothing to do with it, and the
    launcher is told to use a service that does not exist. The advertised endpoint is
    the claim that actually matters -- "you can talk to me here" -- so it is the one
    that gets tested. Only a descriptor whose endpoint answers is usable.

    Both checks are kept: the pid is cheap and rejects the common crash case without
    a connect, and the endpoint probe covers pid reuse. Neither can prove the service
    is healthy, only that the claim is not obviously dead, so this only ever
    downgrades a claim -- what survives still has to pass compatibility.
    """
    if descriptor is None:
        return None
    if not process_is_alive(descriptor.pid):
        return None
    if not _endpoint_answers(descriptor.endpoint):
        return None
    return descriptor


def _endpoint_answers(endpoint: str) -> bool:
    """Whether the advertised endpoint accepts a connection right now.

    Only `unix://` endpoints can be probed. An in-process endpoint has no socket to
    connect to and is not something another launcher could use anyway, so the pid
    check stands alone there.
    """
    if not endpoint.startswith("unix://"):
        return True
    return probe_endpoint(Path(endpoint[len("unix://") :])) is EndpointProbe.ANSWERING


def refuse_incompatible_workspace(outcome: CompatibilityOutcome) -> None:
    """Fail before any lock or lease when a manifest is incompatible.

    ADR-037 requires an incompatible manifest to fail before lease acquisition. This
    is called at the top of startup, before anything is acquired, so there is
    nothing to unwind when it refuses.
    """
    if not outcome.writable:
        raise RuntimeError(
            f"refusing to open this workspace for writing: {outcome.reason} "
            f"(status={outcome.status.value})"
        )


__all__ = [
    "StartupDecision",
    "StartupOutcome",
    "coordinated_startup",
    "refuse_incompatible_workspace",
]
