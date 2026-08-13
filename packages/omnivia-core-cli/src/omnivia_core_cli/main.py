"""The `omnivia` executable: parse one frozen command, call it, print the answer.

A thin shell over three modules that already own everything this one does.
:mod:`omnivia_core_cli.surface` says which commands exist and what each frozen
error code exits with, :mod:`omnivia_core_cli.dispatch` turns one command into
one call, and :class:`~omnivia_core_client.ServiceClient` finds the service and
carries the call. Nothing here discovers an endpoint, reads a descriptor,
chooses a transport, launches anything, or knows what a workspace is made of.

*The parser is the surface, and nothing else.* Every command path is built from
`APPLICATION_COMMANDS` and `PROBE_COMMANDS`, two segments each, in declared
order, with no alias, no abbreviation and no path written down here -- so there
is no `init`, `start`, `stop`, `status`, `health`, `readiness`, `workspace show`
or `core.*` command to reach, and none can be added without adding it to the
frozen surface first.

*Local refusals happen before the connection.* The catalogue states, per
operation, whether an idempotency key and a record version are required,
optional or not honoured at all. A call that would violate either posture is
refused as a usage error -- exit 2, before a socket is opened -- rather than
sent for the service to reject.

*Diagnostics carry nothing.* Every local failure prints one fixed sentence to
stderr and never the exception's text, the input document, the path, the
workspace, or anything a peer said. The service's own `code` and `message` are
printed for an application error, because those are the answer rather than a
diagnostic about it. Nothing local is ever written to stdout: a successful call
puts exactly one document and one newline there and no other byte.

Standard library, the public contracts, the shared client, and the two CLI
modules above. The runtime, the MCP adapter, storage, subprocesses, sockets and
concrete transports are all out of reach from here.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, NoReturn

from omnivia_core_client import (
    MAXIMUM_DURATION_MS,
    ClientError,
    CompatibilityError,
    CredentialError,
    Deadline,
    DeadlineExceededError,
    InstallationServiceConfig,
    OperationCancelledError,
    ServiceClient,
)

from omnivia_core.contracts.v1 import (
    ResponseEnvelope,
    ServiceProbeResult,
    codec,
    encode_service_probe_result,
    get_operation_metadata,
)
from omnivia_core_cli.dispatch import (
    DispatchError,
    dispatch_application,
    dispatch_probe,
)
from omnivia_core_cli.surface import (
    APPLICATION_COMMANDS,
    PROBE_COMMANDS,
    ApplicationCommand,
    exit_code_for,
)

__all__ = ["build_parser", "main"]

#: The whole-call budget when the caller states none: discovery's live probe and
#: the call itself. A local call that has not answered in ten seconds is not
#: about to.
DEFAULT_TIMEOUT_MS: Final = 10_000

#: The empty input document. Read-only so the one instance shared by every
#: invocation cannot be mutated by anything downstream.
_EMPTY_INPUT: Final[Mapping[str, Any]] = MappingProxyType({})

#: The `Namespace` attribute the leaf parser stores its frozen command on. The
#: command object itself, not its name: nothing here re-derives a path.
_COMMAND: Final = "command"

#: Every local diagnostic this module can print, one fixed sentence each. None
#: is built from an argument, a document, an exception or a peer's words.
_NO_SERVICE: Final = "no service is available for this workspace"
_OUT_OF_TIME: Final = "the call ran out of time, or was cancelled"
_NOT_AUTHENTICATED: Final = "the service did not accept this client's credential"
_INCOMPATIBLE: Final = "this client and the service could not agree on a version"
_REFUSED_LOCALLY: Final = "the call was refused here and never sent"
_NO_ANSWER: Final = "the call did not complete"

#: The one thing a malformed `--input-json` is told, and all it is told. The
#: document is never echoed: it is the caller's own payload and may hold
#: anything.
_BAD_INPUT: Final = (
    "must be exactly one JSON object, with unique member names and finite numbers"
)
_BAD_ARGUMENTS: Final = "the command arguments are not valid"


class _ArgumentParser(argparse.ArgumentParser):
    """An argparse parser whose usage refusals never quote caller input."""

    def error(self, message: str) -> NoReturn:
        """Print static usage and one fixed sentence, discarding `message`."""
        del message
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {_BAD_ARGUMENTS}\n")


def build_parser() -> argparse.ArgumentParser:
    """The whole public command surface, built from the frozen one.

    Deterministic: the groups appear in the order their commands are declared,
    each leaf is reached by exactly the two segments the surface names, and no
    alias or prefix abbreviation reaches any of them. Calling this twice
    produces the same parser, and it opens nothing and reads nothing.
    """
    parser = _ArgumentParser(
        prog="omnivia",
        description="Call one operation on a running OmniVia Core service.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--installation-state",
        required=True,
        metavar="ABSOLUTE_PATH",
        type=_absolute_path,
        help="the installation state root whose service is to be called",
    )
    parser.add_argument(
        "--workspace-id",
        required=True,
        metavar="ID",
        help="the workspace whose service is to be called",
    )
    parser.add_argument(
        "--timeout-ms",
        default=DEFAULT_TIMEOUT_MS,
        metavar="MILLISECONDS",
        type=_duration_ms,
        help=(
            "whole-call budget in milliseconds, covering the connection and the "
            f"call (default {DEFAULT_TIMEOUT_MS})"
        ),
    )

    groups = parser.add_subparsers(dest="group", required=True)
    built: dict[str, Any] = {}

    def leaves(group: str) -> Any:
        """The leaf subparsers of one group, creating the group on first use."""
        if group not in built:
            built[group] = groups.add_parser(group, allow_abbrev=False).add_subparsers(
                dest="leaf", required=True
            )
        return built[group]

    for application in APPLICATION_COMMANDS:
        group, leaf_name = application.path
        leaf = leaves(group).add_parser(
            leaf_name, allow_abbrev=False, help=application.operation
        )
        leaf.add_argument(
            "--input-json",
            default=_EMPTY_INPUT,
            metavar="JSON_OBJECT",
            type=_input_object,
            help="the operation's input document (default {})",
        )
        leaf.add_argument("--principal", default=None, help="the principal to claim")
        leaf.add_argument(
            "--idempotency-key", default=None, help="make this mutation replay-safe"
        )
        leaf.add_argument(
            "--record-version",
            default=None,
            help="guard this mutation with the record version it expects",
        )
        leaf.add_argument(
            "--json", action="store_true", help="emit the canonical response envelope"
        )
        leaf.set_defaults(**{_COMMAND: application})

    for probe in PROBE_COMMANDS:
        group, leaf_name = probe.path
        leaf = leaves(group).add_parser(leaf_name, allow_abbrev=False, help=probe.probe)
        leaf.add_argument(
            "--json", action="store_true", help="emit the canonical probe result"
        )
        leaf.set_defaults(**{_COMMAND: probe})

    return parser


def main(
    argv: list[str] | None = None, *, connected_client: ServiceClient | None = None
) -> int:
    """Run one command and return the process exit status.

    `connected_client` is an injection seam for tests and embedders that already
    hold a connected :class:`~omnivia_core_client.ServiceClient`. The console
    script passes none, and then the client is connected here from the
    installation state root and workspace the caller named -- through
    :meth:`ServiceClient.connect` and nothing else, so no descriptor is read and
    no transport is constructed on this path.

    One :class:`~omnivia_core_client.Deadline` is built before the connection
    and reused for the call, because the budget the caller stated is for the
    whole invocation: a call that gets a fresh deadline after a slow connect
    would run for longer than was asked for.
    """
    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
        command = getattr(arguments, _COMMAND)
        if isinstance(command, ApplicationCommand):
            _check_mutation_metadata(parser, command, arguments)
    except SystemExit as requested:
        # argparse exits rather than returning, and this function is declared to
        # return a status. Both are the same number.
        return requested.code if isinstance(requested.code, int) else 0

    try:
        deadline = Deadline.after_ms(arguments.timeout_ms)
        client = connected_client
        if client is None:
            client = ServiceClient.connect(
                InstallationServiceConfig(
                    installation_state=arguments.installation_state,
                    workspace_id=arguments.workspace_id,
                ),
                deadline=deadline,
            )
            if client is None:
                # An installation that has published no descriptor. Ordinary,
                # and still not an answer to the question that was asked.
                return _refuse(_NO_SERVICE, 1)
        if isinstance(command, ApplicationCommand):
            return _report_application(
                dispatch_application(
                    client,
                    command,
                    payload=arguments.input_json,
                    deadline=deadline,
                    principal=arguments.principal,
                    idempotency_key=arguments.idempotency_key,
                    record_version=arguments.record_version,
                ),
                as_json=arguments.json,
            )
        return _report_probe(
            dispatch_probe(client, command, deadline=deadline), as_json=arguments.json
        )
    except (DeadlineExceededError, OperationCancelledError):
        return _refuse(_OUT_OF_TIME, 6)
    except CredentialError:
        return _refuse(_NOT_AUTHENTICATED, 3)
    except CompatibilityError:
        return _refuse(_INCOMPATIBLE, 4)
    except (ValueError, TypeError):
        # An argument or a request this build would not put on the wire,
        # including the contracts' own `ContractDecodeError`, which is a
        # `ValueError` and can quote the document that produced it.
        return _refuse(_REFUSED_LOCALLY, 2)
    except (ClientError, DispatchError, OSError):
        return _refuse(_NO_ANSWER, 1)


def _check_mutation_metadata(
    parser: argparse.ArgumentParser,
    command: ApplicationCommand,
    arguments: argparse.Namespace,
) -> None:
    """Hold `--idempotency-key` and `--record-version` to the catalogue's postures.

    Read off the frozen entry for this operation, never transcribed, and checked
    before the connection: a mutation that requires a key and was given none is
    not replay-safe, and one given a key it does not honour is unsafe while
    looking guarded. Both are the caller's mistake, so both are usage errors --
    exit 2 -- rather than a request sent for the service to refuse.
    """
    entry = get_operation_metadata(command.operation)
    for flag, posture, supported, supplied in (
        (
            "--idempotency-key",
            entry.idempotency.required,
            entry.idempotency.supports_idempotency_key,
            arguments.idempotency_key,
        ),
        (
            "--record-version",
            entry.precondition.required,
            entry.precondition.supports_mutation_precondition,
            arguments.record_version,
        ),
    ):
        if posture and supplied is None:
            parser.error(f"{flag} is required for this command")
        if not supported and supplied is not None:
            parser.error(f"{flag} is not accepted by this command")


def _absolute_path(value: str) -> Path:
    """`value` as an absolute path, refusing a relative one.

    Relative is refused rather than resolved against the working directory: the
    installation state root is the trust anchor for everything discovery then
    checks, and one that means different directories from different shells is
    not an anchor.
    """
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("must be an absolute path")
    return path


def _duration_ms(value: str) -> int:
    """`value` as a contract `DurationMs`: whole milliseconds in `[0, 86400000]`.

    The same domain :meth:`Deadline.after_ms` accepts, checked here so the
    refusal is a usage error naming the flag rather than an exception from the
    middle of the call.
    """
    try:
        milliseconds = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "must be a whole number of milliseconds"
        ) from None
    if not 0 <= milliseconds <= MAXIMUM_DURATION_MS:
        raise argparse.ArgumentTypeError(
            f"must be a whole number of milliseconds in [0, {MAXIMUM_DURATION_MS}]"
        )
    return milliseconds


def _input_object(value: str) -> Mapping[str, Any]:
    """`value` as exactly one JSON object, or a usage error that quotes nothing.

    Four refusals, all of them things a later stage could not detect or could
    not report safely. A duplicated member name is gone once the parser has
    built a mapping, so it is refused during the parse. `NaN` and the
    infinities are not JSON, in either the literal form Python's parser accepts
    or the overflowing-decimal form it produces silently, and neither survives
    canonical encoding. An array or a scalar is well-formed JSON and not an
    operation input.

    The document never appears in the diagnostic, and the parser's own message
    is discarded rather than chained: an input document is the caller's payload
    and may hold a secret, a principal or a record's contents, and a usage error
    is exactly what ends up in a shell history or a CI log.
    """
    try:
        document = json.loads(
            value,
            object_pairs_hook=_unique_members,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (ValueError, RecursionError):
        raise argparse.ArgumentTypeError(_BAD_INPUT) from None
    if not isinstance(document, dict):
        raise argparse.ArgumentTypeError(_BAD_INPUT)
    return document


def _unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """The object `pairs` describe, refusing any name given twice."""
    members = dict(pairs)
    if len(members) != len(pairs):
        raise ValueError(_BAD_INPUT)
    return members


def _reject_constant(name: str) -> NoReturn:
    """Refuse the `NaN`, `Infinity` and `-Infinity` literals Python would accept."""
    raise ValueError(_BAD_INPUT)


def _finite_float(text: str) -> float:
    """`text` as a float, refusing one that overflows to an infinity."""
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(_BAD_INPUT)
    return number


def _report_application(response: ResponseEnvelope, *, as_json: bool) -> int:
    """Print one response and return the status its outcome maps to.

    The wire mapping is what gets printed in both modes, never the decoded
    envelope's attributes: those are read-only views `json` cannot serialise,
    and reaching past them field by field would be this CLI's second opinion
    about the shape of a contract result.
    """
    wire = codec.encode_response(response)
    error = codec.response_error(response)
    if as_json:
        # The whole envelope, error branch included: an error response is an
        # answer, and a caller reading JSON asked for the document rather than
        # for this CLI's summary of it.
        sys.stdout.write(codec.to_canonical_json(wire) + "\n")
        return 0 if error is None else exit_code_for(error.code)
    if error is not None:
        # The service's own code and message, and nothing of the request: a
        # refusal must not carry a caller-supplied value back out.
        sys.stderr.write(f"{error.code}: {error.message}\n")
        return exit_code_for(error.code)
    sys.stdout.write(json.dumps(wire["result"], indent=2, sort_keys=True) + "\n")
    return 0


def _report_probe(result: ServiceProbeResult, *, as_json: bool) -> int:
    """Print one probe result whole, and exit 0 because it answered.

    A `degraded` or `unhealthy` status is not this command's failure: the probe
    was answered, the answer is printed in full, and what it means is the
    caller's to decide.
    """
    wire = encode_service_probe_result(result)
    document = (
        codec.to_canonical_json(wire)
        if as_json
        else json.dumps(dict(wire), indent=2, sort_keys=True)
    )
    sys.stdout.write(document + "\n")
    return 0


def _refuse(diagnostic: str, status: int) -> int:
    """Write one fixed sentence to stderr and return `status`. Never stdout."""
    sys.stderr.write(diagnostic + "\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
