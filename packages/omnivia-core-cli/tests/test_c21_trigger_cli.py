"""C21: the four trigger commands, as the CLI declares them and sends them.

Asserted from the CLI side only. A recording transport stands in for the service,
so nothing here starts a service or needs the runtime, and these hold whether or
not the trigger handlers have landed.
"""

from __future__ import annotations

import json

import pytest
from omnivia_core_cli.surface import APPLICATION_COMMANDS
from test_v06_6_dispatch import RecordingTransport, sent
from test_v06_6_main_execution import invoke

#: Path -> (operation, purpose), as the catalogue and the surface both name them.
TRIGGER_COMMANDS = {
    ("trigger", "declare"): ("trigger.declare", "trigger_configuration"),
    ("trigger", "lifecycle"): ("trigger.lifecycle", "trigger_configuration"),
    ("trigger", "ingest"): ("trigger.ingest", "trigger_ingestion"),
    ("trigger", "health"): ("trigger.health", "trigger_observation"),
}
MUTATIONS = (("trigger", "declare"), ("trigger", "lifecycle"), ("trigger", "ingest"))
HEALTH = ("trigger", "health")
KEY = "idem-c21-0001"

#: Sent exactly as written: the CLI does not interpret the fields it carries.
DOCUMENT = {
    "trigger_id": "trg-c21-0001",
    "limit": 10,
    "page": 2,
    "options": {"depth": 1},
}


def test_each_trigger_command_is_declared_with_its_operation_and_purpose() -> None:
    declared = {
        command.path: (command.operation, command.purpose)
        for command in APPLICATION_COMMANDS
    }
    assert {path: declared[path] for path in TRIGGER_COMMANDS} == TRIGGER_COMMANDS


@pytest.mark.parametrize("path", MUTATIONS, ids=lambda path: "/".join(path))
def test_a_trigger_mutation_needs_a_key_and_refuses_a_record_version(
    path: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    transport = RecordingTransport()
    assert invoke([*path], transport) == 2
    refused = [*path, "--idempotency-key", KEY, "--record-version", "rv-0001"]
    assert invoke(refused, transport) == 2
    assert transport.calls == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "path", list(TRIGGER_COMMANDS), ids=lambda path: "/".join(path)
)
def test_each_trigger_command_sends_its_input_document_unchanged(
    path: tuple[str, ...],
) -> None:
    transport = RecordingTransport()
    argv = [*path, "--input-json", json.dumps(DOCUMENT)]
    if path in MUTATIONS:
        argv += ["--idempotency-key", KEY]
    assert invoke(argv, transport) == 0

    operation, purpose = TRIGGER_COMMANDS[path]
    request = sent(transport)
    assert request.operation == operation
    assert request.metadata.purpose == purpose
    assert request.input == DOCUMENT
    assert request.metadata.idempotency_key == (KEY if path in MUTATIONS else None)
    assert request.metadata.mutation_precondition is None


def test_trigger_health_refuses_an_idempotency_key() -> None:
    transport = RecordingTransport()
    assert invoke([*HEALTH, "--idempotency-key", KEY], transport) == 2
    assert transport.calls == []
