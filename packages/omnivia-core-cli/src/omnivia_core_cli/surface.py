"""The declarative `omnivia` command surface (V06-6 checkpoint 1).

Data, not behaviour. This module names every command path the CLI offers, the
application operation or runtime probe each one reaches, the purpose each
application command declares, and the process exit code each frozen error code
maps to. It dispatches nothing and calls nothing.

Three properties are held here rather than left to a reader:

*Bijection with the catalogue.* The twenty application commands map onto the
twenty operations of `OPERATION_CATALOGUE`, one to one, checked at import. A
command reaching an operation the contract does not publish -- or an operation
published with no command reaching it -- is an import-time refusal, not a
runtime surprise. That also closes the door on the legacy `core.*` operation names,
which no longer exist in the catalogue and therefore cannot appear here.

*Totality over the frozen error codes.* `EXIT_CODES` covers `FROZEN_ERROR_CODES`
exactly, also checked at import, so no error the service is allowed to return
can reach a caller without a defined exit code. `exit_code_for` still answers
for an unrecognised code -- 1, the same as an unrecoverable internal failure --
because a service one minor version ahead may name something this build has
never heard of, and exiting 0 on it would be a lie.

*Probes are not operations.* `service.health`, `service.readiness` and
`service.discover` are runtime probes, deliberately outside the catalogue, and
so they carry no purpose and are listed separately.

Imports reach the contract package only. Nothing here imports the runtime or
the MCP adapter: the surface must be readable by a client that has neither
installed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from omnivia_core.contracts.v1 import FROZEN_ERROR_CODES, OPERATION_CATALOGUE


@dataclass(frozen=True, slots=True)
class ApplicationCommand:
    """One command path, the operation it reaches and the purpose it declares."""

    path: tuple[str, ...]
    operation: str
    purpose: str


@dataclass(frozen=True, slots=True)
class ProbeCommand:
    """One command path and the runtime probe it reaches."""

    path: tuple[str, ...]
    probe: str


APPLICATION_COMMANDS: Final[tuple[ApplicationCommand, ...]] = (
    ApplicationCommand(("workspace", "list"), "workspace.list", "workspace_inspection"),
    ApplicationCommand(
        ("workspace", "create"), "workspace.create", "workspace_administration"
    ),
    ApplicationCommand(
        ("workspace", "inspect"), "workspace.inspect", "workspace_inspection"
    ),
    ApplicationCommand(("memory", "create"), "memory.create", "memory_authoring"),
    ApplicationCommand(("memory", "get"), "memory.get", "knowledge_retrieval"),
    ApplicationCommand(("memory", "list"), "memory.list", "knowledge_retrieval"),
    ApplicationCommand(("memory", "search"), "memory.search", "knowledge_retrieval"),
    ApplicationCommand(("import", "start"), "import.start", "content_ingestion"),
    ApplicationCommand(("job", "get"), "job.get", "job_observation"),
    ApplicationCommand(("job", "cancel"), "job.cancel", "job_control"),
    ApplicationCommand(("job", "retry"), "job.retry", "job_control"),
    ApplicationCommand(("job", "events"), "job.events", "job_observation"),
    ApplicationCommand(
        ("evidence", "search"), "evidence.search", "knowledge_retrieval"
    ),
    ApplicationCommand(
        ("knowledge", "search"), "knowledge.search", "knowledge_retrieval"
    ),
    ApplicationCommand(
        ("governance", "propose"), "knowledge.propose", "knowledge_governance"
    ),
    ApplicationCommand(
        ("governance", "approve"), "candidate.approve", "knowledge_governance"
    ),
    ApplicationCommand(
        ("governance", "reject"), "candidate.reject", "knowledge_governance"
    ),
    ApplicationCommand(
        ("governance", "supersede"), "record.supersede", "knowledge_governance"
    ),
    ApplicationCommand(("graph", "traverse"), "graph.traverse", "knowledge_retrieval"),
    ApplicationCommand(
        ("context-pack", "build"), "context_pack.build", "knowledge_retrieval"
    ),
)

PROBE_COMMANDS: Final[tuple[ProbeCommand, ...]] = (
    ProbeCommand(("service", "health"), "service.health"),
    ProbeCommand(("service", "readiness"), "service.readiness"),
    ProbeCommand(("service", "discover"), "service.discover"),
)

#: The exit code for an error code this build does not recognise, and for
#: `internal_non_recoverable`. A failure nothing here can classify is still a
#: failure.
EXIT_UNKNOWN_ERROR: Final = 1

EXIT_CODES: Final[Mapping[str, int]] = MappingProxyType(
    {
        "invalid_request": 2,
        "authentication_required": 3,
        "authorization_denied": 3,
        "workspace_not_granted": 3,
        "capability_not_granted": 3,
        "invalid_purpose": 3,
        "workspace_migration_required": 4,
        "incompatible_version": 4,
        "upgrade_required": 4,
        "conflict": 5,
        "mutation_precondition_failed": 5,
        "idempotency_conflict": 5,
        "workspace_busy": 5,
        "bootstrap_in_progress": 5,
        "workspace_lease_unavailable": 5,
        "deadline_exceeded": 6,
        "cancelled": 6,
        "projection_unavailable": 7,
        "stale_projection": 7,
        "rate_limited": 7,
        "dependency_unavailable": 7,
        "internal_recoverable": 7,
        "not_found": 8,
        "size_limit_exceeded": 8,
        "token_limit_exceeded": 8,
        "internal_non_recoverable": EXIT_UNKNOWN_ERROR,
    }
)


def exit_code_for(error_code: str) -> int:
    """Return the process exit code for `error_code`, 1 for anything unknown."""
    return EXIT_CODES.get(error_code, EXIT_UNKNOWN_ERROR)


def _validate() -> None:
    """Hold the surface against the contract, at import, or refuse to load."""
    operations = [command.operation for command in APPLICATION_COMMANDS]
    catalogue = {operation.name for operation in OPERATION_CATALOGUE}
    if len(operations) != len(set(operations)):
        raise RuntimeError("surface: an operation is reached by more than one command")
    if set(operations) != catalogue:
        raise RuntimeError(
            "surface: commands do not cover OPERATION_CATALOGUE exactly "
            f"(extra: {sorted(set(operations) - catalogue)}, "
            f"missing: {sorted(catalogue - set(operations))})"
        )
    # Implied by the bijection above -- the catalogue holds no `core.*` name --
    # but stated so the refusal names the reason rather than a set difference.
    legacy = sorted(
        operation for operation in operations if operation.startswith("core.")
    )
    if legacy:
        raise RuntimeError(f"surface: legacy core.* operations declared: {legacy}")
    paths = [command.path for command in APPLICATION_COMMANDS]
    paths.extend(probe.path for probe in PROBE_COMMANDS)
    if len(paths) != len(set(paths)):
        raise RuntimeError("surface: a command path is declared more than once")
    if set(EXIT_CODES) != set(FROZEN_ERROR_CODES):
        raise RuntimeError(
            "surface: EXIT_CODES does not cover FROZEN_ERROR_CODES exactly "
            f"(extra: {sorted(set(EXIT_CODES) - set(FROZEN_ERROR_CODES))}, "
            f"missing: {sorted(set(FROZEN_ERROR_CODES) - set(EXIT_CODES))})"
        )


_validate()
