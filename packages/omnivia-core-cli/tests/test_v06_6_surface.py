"""V06-6 checkpoint 1: the declared CLI surface, held against the contract.

`surface.py` validates itself at import, so most of what follows would already
have raised before a test ran. These assert it from the outside anyway, and
assert the parts import-time validation cannot: the *order* the commands are
declared in, the exact purpose each command declares, the exact probe set, and
every one of the twenty-six exit codes by value rather than by coverage.

The last test is the one that is about more than data. The surface must be
importable by a client that has installed neither the runtime nor the MCP
adapter, so it reads the module source and refuses either name.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from omnivia_core_cli import surface
from omnivia_core_cli.surface import (
    APPLICATION_COMMANDS,
    EXIT_CODES,
    LIFECYCLE_COMMANDS,
    PROBE_COMMANDS,
    exit_code_for,
)

from omnivia_core.contracts.v1 import FROZEN_ERROR_CODES, OPERATION_CATALOGUE

EXPECTED_COMMANDS = (
    (("workspace", "list"), "workspace.list", "workspace_inspection"),
    (("workspace", "create"), "workspace.create", "workspace_administration"),
    (("workspace", "inspect"), "workspace.inspect", "workspace_inspection"),
    (("memory", "create"), "memory.create", "memory_authoring"),
    (("memory", "get"), "memory.get", "knowledge_retrieval"),
    (("memory", "list"), "memory.list", "knowledge_retrieval"),
    (("memory", "search"), "memory.search", "knowledge_retrieval"),
    (("import", "start"), "import.start", "content_ingestion"),
    (("job", "get"), "job.get", "job_observation"),
    (("job", "cancel"), "job.cancel", "job_control"),
    (("job", "retry"), "job.retry", "job_control"),
    (("job", "events"), "job.events", "job_observation"),
    (("evidence", "search"), "evidence.search", "knowledge_retrieval"),
    (("knowledge", "search"), "knowledge.search", "knowledge_retrieval"),
    (("governance", "propose"), "knowledge.propose", "knowledge_governance"),
    (("governance", "approve"), "candidate.approve", "knowledge_governance"),
    (("governance", "reject"), "candidate.reject", "knowledge_governance"),
    (("governance", "supersede"), "record.supersede", "knowledge_governance"),
    (("graph", "traverse"), "graph.traverse", "knowledge_retrieval"),
    (("context-pack", "build"), "context_pack.build", "knowledge_retrieval"),
    (("chat", "command"), "chat.command", "chat_authoring"),
    (("chat", "events"), "chat.events", "chat_observation"),
)

EXPECTED_PROBES = (
    (("service", "health"), "service.health"),
    (("service", "readiness"), "service.readiness"),
    (("service", "discover"), "service.discover"),
)

EXPECTED_LIFECYCLE = (
    (("service", "start"), "start"),
    (("service", "stop"), "stop"),
    (("service", "status"), "status"),
)

EXPECTED_EXITS = {
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
    "internal_non_recoverable": 1,
}


def test_the_twenty_two_application_commands_are_declared_in_order() -> None:
    """Order is surface: it is the order help output and documentation follow."""
    declared = tuple(
        (command.path, command.operation, command.purpose)
        for command in APPLICATION_COMMANDS
    )
    assert declared == EXPECTED_COMMANDS
    assert len(APPLICATION_COMMANDS) == 22


def test_the_commands_are_a_bijection_with_the_operation_catalogue() -> None:
    """Every catalogue operation is reachable, by exactly one command path."""
    operations = [command.operation for command in APPLICATION_COMMANDS]
    assert len(operations) == len(set(operations))
    assert set(operations) == {operation.name for operation in OPERATION_CATALOGUE}
    assert not [name for name in operations if name.startswith("core.")]


def test_every_command_declares_its_exact_purpose() -> None:
    purposes = {command.operation: command.purpose for command in APPLICATION_COMMANDS}
    assert purposes == {
        operation: purpose for _, operation, purpose in EXPECTED_COMMANDS
    }


def test_the_probe_commands_are_exactly_the_three_runtime_probes() -> None:
    """Probes are not catalogue operations and carry no purpose."""
    declared = tuple((probe.path, probe.probe) for probe in PROBE_COMMANDS)
    assert declared == EXPECTED_PROBES
    catalogue = {operation.name for operation in OPERATION_CATALOGUE}
    assert not {probe.probe for probe in PROBE_COMMANDS} & catalogue
    assert not hasattr(PROBE_COMMANDS[0], "purpose")


def test_lifecycle_is_a_separate_three_command_administrative_surface() -> None:
    assert tuple((item.path, item.action) for item in LIFECYCLE_COMMANDS) == (
        EXPECTED_LIFECYCLE
    )
    assert not {item.path for item in LIFECYCLE_COMMANDS} & {
        item.path for item in (*APPLICATION_COMMANDS, *PROBE_COMMANDS)
    }


@pytest.mark.parametrize(("error_code", "expected"), sorted(EXPECTED_EXITS.items()))
def test_each_frozen_error_code_maps_to_its_exit_code(
    error_code: str, expected: int
) -> None:
    assert EXIT_CODES[error_code] == expected
    assert exit_code_for(error_code) == expected


def test_the_exit_map_covers_the_frozen_error_codes_exactly() -> None:
    assert set(EXIT_CODES) == set(FROZEN_ERROR_CODES)
    assert len(EXPECTED_EXITS) == 26


def test_an_unknown_error_code_exits_one() -> None:
    """A service a minor version ahead can name an error this build never heard
    of. That is a failure, and must not exit 0."""
    assert exit_code_for("not_an_error_code_this_build_knows") == 1
    assert exit_code_for("") == 1


def test_the_surface_is_immutable() -> None:
    assert isinstance(APPLICATION_COMMANDS, tuple)
    assert isinstance(PROBE_COMMANDS, tuple)
    assert isinstance(LIFECYCLE_COMMANDS, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        APPLICATION_COMMANDS[0].operation = "workspace.create"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        PROBE_COMMANDS[0].probe = "service.discover"  # type: ignore[misc]
    with pytest.raises(TypeError):
        EXIT_CODES["conflict"] = 0  # type: ignore[index]


def test_the_surface_imports_neither_the_runtime_nor_the_mcp_adapter() -> None:
    """It must be readable by a client that has installed neither."""
    source = Path(surface.__file__).read_text(encoding="utf-8")
    imports = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    ]
    assert not [line for line in imports if "omnivia_core_runtime" in line]
    assert not [line for line in imports if "omnivia_core_mcp" in line]


def test_the_production_package_contains_only_the_declared_cli_modules() -> None:
    """No legacy request-printing or ambient-home lifecycle module may return."""
    package = Path(surface.__file__).parent
    assert {path.name for path in package.iterdir() if path.is_file()} == {
        "__init__.py",
        "dispatch.py",
        "main.py",
        "py.typed",
        "safe_status.py",
        "surface.py",
    }
