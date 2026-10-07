"""C17: the eight managed Skills commands, as the CLI declares them and sends them.

Asserted from the CLI side only, with a recording transport standing in for the service, so
nothing here starts a service or needs the runtime. The purposes are restated literally, and the
runtime's own tests pin the same strings to `MUTATION_PURPOSES` and `SKILL_RESOLUTION_PURPOSE`.
"""

from __future__ import annotations

import json

import pytest
from omnivia_core_cli.surface import APPLICATION_COMMANDS
from test_v06_6_dispatch import RecordingTransport, sent
from test_v06_6_main_execution import invoke

#: Path -> (operation, purpose), as the catalogue and the surface both name them.
SKILL_COMMANDS = {
    ("skills", "draft-create"): ("skills.draft.create", "skill_authoring"),
    ("skills", "draft-update"): ("skills.draft.update", "skill_authoring"),
    ("skills", "propose"): ("skills.proposal.submit", "skill_authoring"),
    ("skills", "publish"): ("skills.version.publish", "skill_publication"),
    ("skills", "deprecate"): ("skills.version.deprecate", "skill_publication"),
    ("skills", "install"): ("skills.install", "skill_installation"),
    ("skills", "remove"): ("skills.remove", "skill_installation"),
    ("skills", "resolve"): ("skills.resolve", "skill_resolution"),
}
MUTATIONS = tuple(path for path in SKILL_COMMANDS if path != ("skills", "resolve"))
RESOLVE = ("skills", "resolve")
KEY = "idem-c17-skills-0001"

#: Sent exactly as written: the CLI does not interpret the fields it carries.
DOCUMENT = {
    "manifest_id": "skm-c17-0001",
    "draft_id": "skdraft-c17-0001",
    "expected_revision": 2,
    "role_id": "reviewer",
    "selections": [{"skill_name": "triage"}],
}


def test_each_skill_command_is_declared_with_its_operation_and_purpose() -> None:
    declared = {
        command.path: (command.operation, command.purpose)
        for command in APPLICATION_COMMANDS
    }
    assert {path: declared[path] for path in SKILL_COMMANDS} == SKILL_COMMANDS


@pytest.mark.parametrize("path", MUTATIONS, ids=lambda path: "/".join(path))
def test_a_skill_mutation_needs_a_key_and_refuses_a_record_version(
    path: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    transport = RecordingTransport()
    assert invoke([*path], transport) == 2
    refused = [*path, "--idempotency-key", KEY, "--record-version", "rv-0001"]
    assert invoke(refused, transport) == 2
    assert transport.calls == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("path", list(SKILL_COMMANDS), ids=lambda path: "/".join(path))
def test_each_skill_command_sends_its_input_document_unchanged(
    path: tuple[str, ...],
) -> None:
    transport = RecordingTransport()
    argv = [*path, "--input-json", json.dumps(DOCUMENT)]
    if path in MUTATIONS:
        argv += ["--idempotency-key", KEY]
    assert invoke(argv, transport) == 0

    operation, purpose = SKILL_COMMANDS[path]
    request = sent(transport)
    assert request.operation == operation
    assert request.metadata.purpose == purpose
    assert request.input == DOCUMENT
    assert request.metadata.idempotency_key == (KEY if path in MUTATIONS else None)


def test_skills_resolve_refuses_an_idempotency_key() -> None:
    transport = RecordingTransport()
    assert invoke([*RESOLVE, "--idempotency-key", KEY], transport) == 2
    assert transport.calls == []
