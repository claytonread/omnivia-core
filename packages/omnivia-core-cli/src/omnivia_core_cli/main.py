"""The `omnivia` executable: parse one frozen command, call it, print the answer.

A thin shell over three modules that already own everything this one does.
:mod:`omnivia_core_cli.surface` says which commands exist and what each frozen
error code exits with, :mod:`omnivia_core_cli.dispatch` turns one command into
one call, and :class:`~omnivia_core_client.ServiceClient` finds the service and
carries the call. Nothing here discovers an endpoint, reads a descriptor,
chooses a transport or knows what a workspace is made of. Managed-local startup
is delegated whole to the shared client package.

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
    EndpointUnavailableError,
    InstallationServiceConfig,
    ManagedStartError,
    OperationCancelledError,
    ServiceClient,
    connect_managed_local,
    stop_managed_local,
)

from omnivia_core.contracts.v1 import (
    ContractSemanticError,
    CoreSafeStatusV1,
    CoreTargetV1,
    ResponseEnvelope,
    ServiceProbeResult,
    codec,
    encode_core_safe_status,
    encode_service_probe_result,
    get_operation_metadata,
    is_request_id,
)
from omnivia_core_cli.dispatch import (
    DispatchError,
    dispatch_application,
    dispatch_probe,
)
from omnivia_core_cli.safe_status import (
    degraded_status,
    incompatible_status,
    live_status,
    resolve_local_target,
    stopped_status,
    unreachable_status,
)
from omnivia_core_cli.surface import (
    APPLICATION_COMMANDS,
    LIFECYCLE_COMMANDS,
    PROBE_COMMANDS,
    ApplicationCommand,
    LifecycleCommand,
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
_MANAGED_START_FAILED: Final = "the managed service could not be started"
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

#: Version of the small lifecycle adapter document emitted by
#: ``start|stop|status --json``. This is intentionally not the managed-start
#: document: the latter is a service-owned launcher protocol, while this one is
#: the stable, least-privilege view adapters such as the Core status menu consume.
#:
#: **Version 2 removed `service` and `reason`.** Both were this installation's
#: internals -- a workspace id, a service instance id, the runtime's raw state
#: name and its raw `unmet` list; a free-form sentence naming directories,
#: endpoints, pids and the launcher's own words -- published to a surface that
#: has authenticated nothing. What replaced them is a required `code` from the
#: closed set below and an optional `safe_status`, which is `CoreSafeStatusV1`
#: and is rendered only by the contract encoder.
LIFECYCLE_ADAPTER_VERSION = 2

#: Every `code` this adapter may publish, and the whole of what a machine caller
#: learns about *why*. Closed and bounded on purpose: a caller can branch on
#: these, a UI can phrase them in its own words, and neither ever receives a
#: string this process assembled from a path, an endpoint, a launcher result or
#: an exception. `internal_error` is the fail-closed landing place for a code
#: this module did not declare -- publishing an undeclared one is the defect the
#: set exists to prevent.
LIFECYCLE_CODE_FRAMES: Final[
    Mapping[str, tuple[str | None, str, bool]]
] = MappingProxyType(
    {
        "start_started": ("start", "started", True),
        "start_attached": ("start", "attached", True),
        "start_workspace_missing": ("start", "failed", False),
        "start_incompatible_service": ("start", "failed", False),
        "start_timeout": ("start", "failed", False),
        "start_spawn_failed": ("start", "failed", False),
        "start_not_ready": ("start", "failed", False),
        "start_failed": ("start", "failed", False),
        "stop_stopped": ("stop", "stopped", True),
        "stop_not_running": ("stop", "not_running", True),
        "stop_service_unreachable": ("stop", "failed", False),
        "stop_no_process": ("stop", "failed", False),
        "stop_identity_mismatch": ("stop", "failed", False),
        "stop_timeout": ("stop", "failed", False),
        "stop_process_lingering": ("stop", "failed", False),
        "status_running": ("status", "running", True),
        "status_not_running": ("status", "not_running", False),
        "status_unreachable": ("status", "failed", False),
        "status_incompatible": ("status", "failed", False),
        "internal_error": (None, "failed", False),
    }
)
LIFECYCLE_CODES: Final = frozenset(LIFECYCLE_CODE_FRAMES)

def _write_lifecycle_document(
    action: str,
    *,
    ok: bool,
    outcome: str,
    code: str,
    safe_status: CoreSafeStatusV1 | None = None,
) -> None:
    """Write the one machine-readable lifecycle adapter document.

    The safe status goes through `encode_core_safe_status` and through nothing
    else, so a status that does not satisfy the contract's cross-field
    invariants -- an action offered for a target that may not be acted on, a
    version disagreement -- is omitted rather than published. Omission is the
    fail-closed answer: a caller that receives no `safe_status` offers no
    actions, while a caller that receives an invalid one might.
    """
    frame = LIFECYCLE_CODE_FRAMES.get(code)
    if frame is None or (
        (frame[0] is not None and frame[0] != action)
        or frame[1] != outcome
        or frame[2] != ok
    ):
        code = "internal_error"
        outcome = "failed"
        ok = False

    document: dict[str, Any] = {
        "lifecycle_adapter_version": LIFECYCLE_ADAPTER_VERSION,
        "action": action,
        "ok": ok,
        "outcome": outcome,
        "code": code,
    }
    if safe_status is not None:
        try:
            document["safe_status"] = encode_core_safe_status(safe_status)
        except ContractSemanticError:
            pass
    sys.stdout.write(json.dumps(document, sort_keys=True) + "\n")


def _finish_lifecycle(
    action: str,
    *,
    json_output: bool,
    returncode: int,
    outcome: str,
    code: str,
    safe_status: CoreSafeStatusV1 | None = None,
    human_stdout: str | None = None,
    human_stderr: str | None = None,
) -> int:
    """Render one lifecycle result without mixing JSON and human prose."""
    if json_output:
        _write_lifecycle_document(
            action,
            ok=returncode == 0,
            outcome=outcome,
            code=code,
            safe_status=safe_status,
        )
    else:
        if human_stdout:
            sys.stdout.write(human_stdout)
        if human_stderr:
            sys.stderr.write(human_stderr)
    return returncode


def _selected_target(
    installation_state: Path, workspace_id: str, *, json_output: bool
) -> CoreTargetV1 | None:
    """The one target this command addresses, or None if it cannot be formed.

    Resolved only when a machine document will be written: the human paths
    publish no target, and reading a manifest they will not use is work for
    nothing.
    """
    return (
        resolve_local_target(installation_state, workspace_id)
        if json_output
        else None
    )


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
            "--request-id",
            default=None,
            metavar="ID",
            type=_request_id,
            help=(
                "the request id to send, for an input document that must name "
                "this request (default: a fresh one)"
            ),
        )
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

    for lifecycle in LIFECYCLE_COMMANDS:
        group, leaf_name = lifecycle.path
        leaf = leaves(group).add_parser(
            leaf_name, allow_abbrev=False, help=f"{lifecycle.action} this service"
        )
        leaf.add_argument(
            "--json", action="store_true", help="emit the safe lifecycle document"
        )
        leaf.set_defaults(**{_COMMAND: lifecycle})

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
    no transport or launcher is constructed on this path.

    One :class:`~omnivia_core_client.Deadline` is built before the connection
    and reused for the call, because the budget the caller stated is for the
    whole invocation: a call that gets a fresh deadline after a slow connect
    would run for longer than was asked for.
    """
    parser = build_parser()
    client: ServiceClient | None
    try:
        arguments = parser.parse_args(argv)
        command = getattr(arguments, _COMMAND)
        if isinstance(command, ApplicationCommand):
            _check_mutation_metadata(parser, command, arguments)
    except SystemExit as requested:
        # argparse exits rather than returning, and this function is declared to
        # return a status. Both are the same number.
        return requested.code if isinstance(requested.code, int) else 0

    if isinstance(command, LifecycleCommand):
        return _run_lifecycle(arguments, command, connected_client=connected_client)

    try:
        deadline = Deadline.after_ms(arguments.timeout_ms)
        client = connected_client
        if client is None:
            managed = connect_managed_local(
                InstallationServiceConfig(
                    installation_state=arguments.installation_state,
                    workspace_id=arguments.workspace_id,
                ),
                deadline=deadline,
            )
            client = managed.client
        if isinstance(command, ApplicationCommand):
            return _report_application(
                dispatch_application(
                    client,
                    command,
                    payload=arguments.input_json,
                    deadline=deadline,
                    principal=arguments.principal,
                    request_id=arguments.request_id,
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
    except ManagedStartError:
        return _refuse(_MANAGED_START_FAILED, 1)
    except (ValueError, TypeError):
        # An argument or a request this build would not put on the wire,
        # including the contracts' own `ContractDecodeError`, which is a
        # `ValueError` and can quote the document that produced it.
        return _refuse(_REFUSED_LOCALLY, 2)
    except (ClientError, DispatchError, OSError):
        return _refuse(_NO_ANSWER, 1)


def _safe_live_status(
    target: CoreTargetV1 | None,
    client: ServiceClient,
    readiness: ServiceProbeResult,
) -> CoreSafeStatusV1 | None:
    """Project a live readiness answer through the accepted safe contract."""
    if target is None:
        return None
    return live_status(
        target,
        state=client.descriptor.lifecycle_state,
        ready=readiness.status == "pass",
        server_version=readiness.server_version,
        protocol_version=client.descriptor.protocol_version,
    )


def _readiness(client: ServiceClient, deadline: Deadline) -> ServiceProbeResult:
    command = next(
        probe for probe in PROBE_COMMANDS if probe.probe == "service.readiness"
    )
    return dispatch_probe(client, command, deadline=deadline)


def _run_lifecycle(
    arguments: argparse.Namespace,
    command: LifecycleCommand,
    *,
    connected_client: ServiceClient | None,
) -> int:
    """Run one explicit ``service`` administration command.

    Application and probe counts remain exactly 20 and 3.  These three commands
    are a separate administrative class and always address the explicit
    installation-state/workspace pair supplied to the root parser.
    """
    action = command.action
    json_output = bool(arguments.json)
    target = _selected_target(
        arguments.installation_state,
        arguments.workspace_id,
        json_output=json_output,
    )
    config = InstallationServiceConfig(
        installation_state=arguments.installation_state,
        workspace_id=arguments.workspace_id,
    )
    deadline = Deadline.after_ms(arguments.timeout_ms)

    if action == "stop":
        result = stop_managed_local(config, deadline=deadline)
        status = result.status
        returncode = 0 if status in {"stopped", "not_running"} else 1
        code = {
            "stopped": "stop_stopped",
            "not_running": "stop_not_running",
            "unreachable": "stop_service_unreachable",
            "no_process": "stop_no_process",
            "identity_mismatch": "stop_identity_mismatch",
            "timeout": "stop_timeout",
            "process_lingering": "stop_process_lingering",
        }.get(status, "internal_error")
        if target is None:
            safe = None
        elif status in {"stopped", "not_running"}:
            safe = stopped_status(target)
        elif status == "unreachable":
            safe = unreachable_status(target, may_start=True)
        elif status in {"timeout", "process_lingering"}:
            safe = degraded_status(target, lifecycle_state="stopping")
        else:
            safe = degraded_status(target, lifecycle_state="running")
        human = {
            "stopped": "stopped\n",
            "not_running": "not running\n",
        }.get(status)
        return _finish_lifecycle(
            action,
            json_output=json_output,
            returncode=returncode,
            outcome=status if returncode == 0 else "failed",
            code=code,
            safe_status=safe,
            human_stdout=human,
            human_stderr=None if human is not None else "the service was not stopped\n",
        )

    client: ServiceClient | None
    try:
        if action == "start":
            managed = connect_managed_local(config, deadline=deadline)
            client = managed.client
            outcome = managed.status
        else:
            client = connected_client
            if client is None:
                client = ServiceClient.connect(config, deadline=deadline)
            if client is None:
                return _finish_lifecycle(
                    action,
                    json_output=json_output,
                    returncode=1,
                    outcome="not_running",
                    code="status_not_running",
                    safe_status=stopped_status(target) if target is not None else None,
                    human_stdout="not running\n",
                )
            outcome = "running"
        assert client is not None
        readiness = _readiness(client, deadline)
    except CompatibilityError:
        code = "start_incompatible_service" if action == "start" else "status_incompatible"
        return _finish_lifecycle(
            action,
            json_output=json_output,
            returncode=1,
            outcome="failed",
            code=code,
            safe_status=incompatible_status(target) if target is not None else None,
            human_stderr="the service is incompatible\n",
        )
    except EndpointUnavailableError:
        code = "start_failed" if action == "start" else "status_unreachable"
        return _finish_lifecycle(
            action,
            json_output=json_output,
            returncode=1,
            outcome="failed",
            code=code,
            safe_status=(
                unreachable_status(target, may_start=True)
                if target is not None
                else None
            ),
            human_stderr="the service is unreachable\n",
        )
    except (DeadlineExceededError, OperationCancelledError):
        code = "start_timeout" if action == "start" else "status_unreachable"
        return _finish_lifecycle(
            action,
            json_output=json_output,
            returncode=1,
            outcome="failed",
            code=code,
            safe_status=(
                unreachable_status(target, may_start=True)
                if target is not None
                else None
            ),
            human_stderr=_OUT_OF_TIME + "\n",
        )
    except (ManagedStartError, ClientError, DispatchError, OSError):
        return _finish_lifecycle(
            action,
            json_output=json_output,
            returncode=1,
            outcome="failed",
            code="start_failed" if action == "start" else "status_unreachable",
            safe_status=(
                unreachable_status(target, may_start=True)
                if target is not None
                else None
            ),
            human_stderr=_NO_ANSWER + "\n",
        )

    return _finish_lifecycle(
        action,
        json_output=json_output,
        returncode=0,
        outcome=outcome,
        code=(
            "status_running"
            if action == "status"
            else "start_attached" if outcome == "attached" else "start_started"
        ),
        safe_status=_safe_live_status(target, client, readiness),
        human_stdout=(
            "running\n"
            if action == "status"
            else "already running\n" if outcome == "attached" else "started\n"
        ),
    )


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


def _request_id(value: str) -> str:
    """`value` as a contract `RequestId`, refusing anything the envelope would not carry.

    Checked here, with the contracts' own `is_request_id` rather than a pattern
    restated locally, so a malformed id is a usage error naming the flag instead
    of a `ValueError` from the middle of envelope construction. The value never
    appears in the refusal: it is the caller's, and a usage error is exactly what
    ends up in a shell history or a CI log.
    """
    if not is_request_id(value):
        raise argparse.ArgumentTypeError("must be a well-formed request id")
    return value


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
