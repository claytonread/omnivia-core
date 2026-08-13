"""V06-6: the parser is the surface, and the surface is all it is.

Three claims about `main.py`, each asserted over data rather than by example.

- **Every frozen command parses from its exact two segments.** The parser is
  built from `APPLICATION_COMMANDS` and `PROBE_COMMANDS`, so each is walked and
  the leaf's stored command compared *by identity*: the parser must hand back
  the frozen object, not an equal one re-derived from a path.
- **Nothing else parses.** The commands the CLI is documented not to have --
  `init`, `start`, `status`, `workspace show`, a `core.*` path -- and every
  prefix abbreviation of a real one exit 2 and print nothing to stdout.
- **A refused value is never echoed.** A secret can be passed to any flag, and
  the flags that refuse one locally are exactly the ones whose refusal reaches a
  shell history or a CI log. Each rejection is checked for the sentinel in both
  streams.

Every case here fails during the parse or the catalogue check that follows it,
so `main` returns before a client is connected. Nothing opens a socket.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_cli.main import build_parser, main
from omnivia_core_cli.surface import APPLICATION_COMMANDS, PROBE_COMMANDS

from omnivia_core.contracts.v1 import get_operation_metadata

#: The two flags every invocation needs, and a path that is absolute on every
#: platform the CLI runs on. Nothing is read from it.
BASE = [
    "--installation-state",
    str(Path.cwd() / "installation"),
    "--workspace-id",
    "workspace-1",
]

#: Distinctive enough that finding it anywhere in either stream is proof it came
#: from the argument and not from a fixed sentence.
SECRET = "s3cr3t-token-9f2a"

COMMANDS = (*APPLICATION_COMMANDS, *PROBE_COMMANDS)

#: Paths this CLI does not offer, and prefix abbreviations of paths it does.
#: `allow_abbrev=False` is set on every parser in the tree, so a unique prefix
#: is still not a command.
NOT_COMMANDS = (
    ["init"],
    ["start"],
    ["stop"],
    ["status"],
    ["health"],
    ["readiness"],
    ["list"],
    ["workspace"],
    ["workspace", "show"],
    ["workspace", "status"],
    ["core.memory", "create"],
    ["core", "health"],
    ["work", "list"],
    ["workspac", "list"],
    ["memory", "sea"],
    ["memory", "s"],
    ["service", "heal"],
    ["context", "build"],
    ["memory", "get", "extra"],
)


def _first_command(predicate: Callable[[Any], bool]) -> Any:
    """The first application command whose catalogue entry satisfies `predicate`."""
    for command in APPLICATION_COMMANDS:
        if predicate(get_operation_metadata(command.operation)):
            return command
    raise AssertionError("the catalogue declares no operation with this posture")


#: Commands that do not honour these flags at all: supplying one is a usage
#: error raised before the connection, and the value supplied is the caller's.
_NO_KEY = _first_command(lambda entry: not entry.idempotency.supports_idempotency_key)
_NO_VERSION = _first_command(
    lambda entry: not entry.precondition.supports_mutation_precondition
)

#: Every way a caller-supplied value is refused locally. The parametrisation is
#: named so a failure says which flag leaked.
SECRET_BEARING = {
    "malformed-input": ["memory", "create", "--input-json", f'{{"token": {SECRET}}}'],
    "duplicate-member": [
        "memory",
        "create",
        "--input-json",
        f'{{"token": "{SECRET}", "token": "other"}}',
    ],
    "not-an-object": ["memory", "create", "--input-json", f'["{SECRET}"]'],
    "non-finite-number": [
        "memory",
        "create",
        "--input-json",
        f'{{"token": "{SECRET}", "n": NaN}}',
    ],
    "unknown-flag": ["memory", "create", f"--not-a-flag={SECRET}"],
    "unsupported-idempotency-key": [*_NO_KEY.path, "--idempotency-key", SECRET],
    "unsupported-record-version": [*_NO_VERSION.path, "--record-version", SECRET],
}


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: ".".join(c.path))
def test_each_frozen_command_parses_from_its_exact_two_segments(command: Any) -> None:
    """The leaf hands back the frozen command object itself."""
    assert len(command.path) == 2
    arguments = build_parser().parse_args([*BASE, *command.path])
    assert arguments.command is command
    assert (arguments.group, arguments.leaf) == command.path


@pytest.mark.parametrize("argv", NOT_COMMANDS, ids=lambda a: " ".join(a) or "empty")
def test_a_legacy_or_abbreviated_command_is_a_usage_error(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*BASE, *argv]) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "argv", list(SECRET_BEARING.values()), ids=list(SECRET_BEARING)
)
def test_a_refused_value_never_reaches_either_stream(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*BASE, *argv]) == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert captured.out == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["--installation-state", f"relative/{SECRET}", "--workspace-id", "w"],
        [*BASE, "--timeout-ms", SECRET],
        [*BASE, "--timeout-ms", "86400001"],
    ],
    ids=["relative-path", "non-numeric-timeout", "timeout-out-of-domain"],
)
def test_a_refused_global_flag_never_reaches_either_stream(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*argv, "service", "health"]) == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert captured.out == ""
