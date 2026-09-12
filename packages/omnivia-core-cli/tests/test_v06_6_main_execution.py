"""V06-6: what the executable prints, on which stream, and what it exits with.

`test_v06_6_main_parser.py` covers everything `main` refuses before a
connection. This file covers everything after one: the call is carried by the
same real :class:`~omnivia_core_client.ServiceClient` over the same
`RecordingTransport` the dispatch tests build -- imported rather than restated,
so production takes the object these tests hand it and nothing stands in for
`dispatch_application`, `dispatch_probe` or the client's own `call`.

Four claims, each asserted over data rather than by example.

- **A success is exactly one document, on stdout, with one newline after it.**
  Both modes, for an application call and for a probe. The expected bytes are
  re-derived from the request the transport actually recorded, so a test cannot
  agree with the CLI about an envelope neither of them built.
- **An application error is an answer, and every frozen code has one shape.**
  All twenty-six of `FROZEN_ERROR_CODES` are answered and rendered: the
  service's own `code: message` on stderr in human mode, the whole canonical
  envelope on stdout in JSON mode, and in both the status `exit_code_for` names.
- **The budget is one budget.** `ServiceClient.connect` is monkeypatched to
  record the deadline it was given, and the object the transport is then handed
  is compared *by identity*: a call that got a fresh deadline after a slow
  connect would run for longer than the caller asked for, and an equal
  re-derived one would satisfy a value check.
- **A local failure says one fixed sentence and nothing else.** Each known
  failure is raised with a distinctive string in it, and neither stream may
  contain that string. Nothing local ever reaches stdout.

Nothing here opens a socket, a file or a process: the client is injected, or
`connect` is patched out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from omnivia_core_cli.dispatch import DispatchError
from omnivia_core_cli.main import main
from omnivia_core_cli.surface import exit_code_for
from omnivia_core_client import (
    ClientError,
    CompatibilityError,
    CredentialError,
    Deadline,
    DeadlineExceededError,
    OperationCancelledError,
    ServiceClient,
)
from test_v06_6_dispatch import (
    WORKSPACE,
    RecordingTransport,
    client,
    echo,
    response_metadata,
    sent,
    success,
)

from omnivia_core.contracts.v1 import (
    FROZEN_ERROR_CODES,
    ApiError,
    ErrorResponseEnvelope,
    RequestEnvelope,
    ServiceProbeRequest,
    ServiceProbeResult,
    codec,
    encode_service_probe_result,
)

#: The two flags every invocation needs. The path is absolute on every platform
#: the CLI runs on, and nothing is read from it: either a connected client is
#: injected or `connect` is patched, so discovery never runs.
BASE = [
    "--installation-state",
    str(Path.cwd() / "installation"),
    "--workspace-id",
    WORKSPACE,
]

#: One application command and one probe command, named rather than swept. Every
#: command's parse is `test_v06_6_main_parser.py`'s claim and every command's
#: dispatch is `test_v06_6_dispatch.py`'s; what is left for this file is what the
#: two streams get, which does not vary by command. `memory get` requires
#: neither an idempotency key nor a record version, so it reaches the connection
#: with no flag beyond the two above.
APPLICATION = ["memory", "get"]
PROBE = ["service", "health"]

#: Every local diagnostic `main` can print, restated here rather than imported:
#: a test that read the module's own constants would pass unchanged if one were
#: replaced by an f-string quoting the endpoint, the workspace or the exception.
MANAGED_START_FAILED = "the managed service could not be started"
OUT_OF_TIME = "the call ran out of time, or was cancelled"
NOT_AUTHENTICATED = "the service did not accept this client's credential"
INCOMPATIBLE = "this client and the service could not agree on a version"
NO_ANSWER = "the call did not complete"

#: The message a service puts on an error answer. Printed, deliberately: the
#: peer's own `code` and `message` are the answer rather than a diagnostic about
#: it, and they are the only peer material either stream may carry.
MESSAGE = "the service's own sentence, which is the answer"

#: Distinctive enough that finding it in either stream is proof it came from an
#: exception or a peer's document rather than from a fixed sentence.
SECRET = "s3cr3t-endpoint-4b1c"


def invoke(argv: list[str], transport: Any) -> int:
    """Run one command against a real client holding `transport`."""
    return main([*BASE, *argv], connected_client=client(transport))


def only_document(text: str) -> str:
    """The one document `text` holds, refusing anything but one newline after it.

    A stream carrying a document and a blank line, or a document and no
    terminator, is not what a caller pipes into `jq`.
    """
    assert text.endswith("\n"), text
    assert not text.endswith("\n\n"), text
    return text[:-1]


def refusing(code: str) -> Any:
    """A transport answer: the error envelope a service returns for `code`.

    The retry class is the frozen one for the code rather than a chosen one, so
    the document is a response a conforming peer could actually have sent.
    """

    def answer(request: RequestEnvelope) -> ErrorResponseEnvelope:
        return ErrorResponseEnvelope(
            metadata=response_metadata(request),
            error=ApiError(
                code=code,
                message=MESSAGE,
                retry_class=codec.retry_class_for(code),
            ),
        )

    return answer


def probed(transport: RecordingTransport) -> ServiceProbeRequest:
    """The one probe that was carried, refusing to answer if there were others."""
    assert len(transport.probes) == 1
    return transport.probes[0][0]


# --------------------------------------------------------------------------
# 1. A success is one document, on stdout, with one newline after it
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "render"),
    [
        ([], lambda wire: json.dumps(wire["result"], indent=2, sort_keys=True)),
        (["--json"], codec.to_canonical_json),
    ],
    ids=["human", "json"],
)
def test_an_application_success_puts_one_document_on_stdout(
    flags: list[str], render: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Human mode prints the result; JSON mode prints the whole envelope."""
    transport = RecordingTransport()
    assert invoke([*APPLICATION, *flags], transport) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert only_document(captured.out) == render(
        codec.encode_response(success(sent(transport)))
    )


@pytest.mark.parametrize(
    ("flags", "render"),
    [
        ([], lambda wire: json.dumps(dict(wire), indent=2, sort_keys=True)),
        (["--json"], codec.to_canonical_json),
    ],
    ids=["human", "json"],
)
def test_a_probe_success_puts_one_document_on_stdout(
    flags: list[str], render: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A probe answer is printed whole in both modes, and answering exits 0."""
    transport = RecordingTransport()
    assert invoke([*PROBE, *flags], transport) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert only_document(captured.out) == render(
        encode_service_probe_result(echo(probed(transport)))
    )


@pytest.mark.parametrize("status", ["pass", "degraded", "fail"])
def test_a_probe_that_answers_exits_zero_whatever_it_reports(
    status: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The probe was answered. What the answer means is the caller's to decide."""

    def answer(request: ServiceProbeRequest) -> ServiceProbeResult:
        return echo(request, status=status)

    transport = RecordingTransport(probe_answer=answer)
    assert invoke(PROBE, transport) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert status in only_document(captured.out)


# --------------------------------------------------------------------------
# 2. Every frozen error code is an answer, with the status the surface names
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code", FROZEN_ERROR_CODES)
def test_every_frozen_error_code_reports_its_own_code_and_message(
    code: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Human mode: the peer's `code: message` on stderr, and stdout untouched."""
    transport = RecordingTransport(answer=refusing(code))
    status = invoke(APPLICATION, transport)
    assert status == exit_code_for(code)
    assert status != 0, "an application error is never a success"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert only_document(captured.err) == f"{code}: {MESSAGE}"


@pytest.mark.parametrize("code", FROZEN_ERROR_CODES)
def test_every_frozen_error_code_is_a_canonical_envelope_in_json_mode(
    code: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON mode: the error branch whole, because it is the document asked for."""
    transport = RecordingTransport(answer=refusing(code))
    assert invoke([*APPLICATION, "--json"], transport) == exit_code_for(code)
    captured = capsys.readouterr()
    assert captured.err == ""
    expected = codec.encode_response(refusing(code)(sent(transport)))
    assert only_document(captured.out) == codec.to_canonical_json(expected)


# --------------------------------------------------------------------------
# 3. One deadline covers the connection and the call it precedes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "carried"),
    [
        (APPLICATION, lambda transport: transport.calls),
        (PROBE, lambda transport: transport.probes),
    ],
    ids=["application", "probe"],
)
def test_the_deadline_the_connection_got_is_the_one_the_call_gets(
    argv: list[str],
    carried: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Asserted by identity: a re-derived equal deadline would pass a value check.

    `connect` is the only seam the console script has, so patching it is what
    lets the deadline built before the connection be compared with the one that
    reached the wire after it.
    """
    transport = RecordingTransport()
    seen: list[Deadline] = []

    def connect(config: Any, *, deadline: Deadline, **_kwargs: Any) -> Any:
        seen.append(deadline)
        return client(transport)

    monkeypatch.setattr(ServiceClient, "connect", connect)
    assert main([*BASE, *argv]) == 0
    assert capsys.readouterr().err == ""
    assert len(seen) == 1
    assert isinstance(seen[0], Deadline)
    assert len(carried(transport)) == 1
    assert carried(transport)[0][1] is seen[0]


def test_a_workspace_that_cannot_be_started_says_so_and_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Managed-local startup failures use one payload-free diagnostic."""
    monkeypatch.setattr(ServiceClient, "connect", lambda config, **_kwargs: None)
    assert main([*BASE, *APPLICATION]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert only_document(captured.err) == MANAGED_START_FAILED


# --------------------------------------------------------------------------
# 3b. `--request-id` is the id the envelope carries
# --------------------------------------------------------------------------


def test_a_supplied_request_id_is_the_one_the_envelope_carries() -> None:
    """An operation whose input must name this request can be called at all.

    `chat.snapshot` requires the caller's query to repeat `metadata.request_id`,
    which is impossible unless the caller can state the id the CLI will send.
    Correlation and trace ids follow it, as they do for a minted one.
    """
    transport = RecordingTransport()
    assert invoke([*APPLICATION, "--request-id", "caller-chosen-id-05"], transport) == 0
    metadata = sent(transport).metadata
    assert metadata.request_id == "caller-chosen-id-05"
    assert metadata.correlation_id == metadata.trace_id == "caller-chosen-id-05"


def test_omitting_the_flag_still_mints_a_fresh_cli_request_id() -> None:
    """The default is unchanged: a CLI-prefixed id, and a different one each run."""
    minted = []
    for _ in range(2):
        transport = RecordingTransport()
        assert invoke(APPLICATION, transport) == 0
        minted.append(sent(transport).metadata.request_id)
    assert all(identifier.startswith("cli-") for identifier in minted)
    assert len(set(minted)) == len(minted)


# --------------------------------------------------------------------------
# 4. A local failure is one fixed sentence, and never on stdout
# --------------------------------------------------------------------------


class RaisingTransport:
    """A transport that fails the way one known local failure fails."""

    def __init__(self, failure: Any) -> None:
        self.failure = failure

    def call(self, request: RequestEnvelope, **_kwargs: Any) -> Any:
        raise self.failure()

    def probe(self, request: ServiceProbeRequest, **_kwargs: Any) -> Any:
        raise self.failure()


#: Every failure `main` names, the sentence it prints and the status it exits
#: with. The first four are `ClientError` subclasses caught ahead of it, so this
#: table is also the assertion that the handlers stay in that order: a
#: `CredentialError` reported as `the call did not complete` would exit 1.
LOCAL_FAILURES = {
    "credential": (lambda: CredentialError(SECRET), NOT_AUTHENTICATED, 3),
    "compatibility": (lambda: CompatibilityError(SECRET), INCOMPATIBLE, 4),
    "deadline-exceeded": (lambda: DeadlineExceededError(SECRET), OUT_OF_TIME, 6),
    "cancelled": (lambda: OperationCancelledError(SECRET), OUT_OF_TIME, 6),
    "client": (lambda: ClientError(SECRET), NO_ANSWER, 1),
    "dispatch": (DispatchError, NO_ANSWER, 1),
    "os": (lambda: OSError(SECRET), NO_ANSWER, 1),
}


@pytest.mark.parametrize("argv", [APPLICATION, PROBE], ids=["application", "probe"])
@pytest.mark.parametrize(
    ("failure", "diagnostic", "status"),
    list(LOCAL_FAILURES.values()),
    ids=list(LOCAL_FAILURES),
)
def test_a_known_local_failure_prints_one_fixed_sentence(
    argv: list[str],
    failure: Any,
    diagnostic: str,
    status: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The sentence, the status, and none of the exception's own words."""
    assert invoke(argv, RaisingTransport(failure)) == status
    captured = capsys.readouterr()
    assert captured.out == ""
    assert only_document(captured.err) == diagnostic
    assert SECRET not in captured.err


def test_an_answer_that_does_not_correlate_is_not_returned_to_the_caller(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A real `DispatchError`, from the refusal `dispatch.py` owns.

    The ids are the peer's, so finding one in either stream would mean a
    diagnostic built from what a peer said rather than from a fixed sentence.
    """

    def answer(request: RequestEnvelope) -> Any:
        return success(request, request_id=SECRET, correlation_id=SECRET)

    transport = RecordingTransport(answer=answer)
    assert invoke(APPLICATION, transport) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert only_document(captured.err) == NO_ANSWER
    assert SECRET not in captured.err


def test_a_probe_answer_that_does_not_correlate_is_not_returned_either(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def answer(request: ServiceProbeRequest) -> ServiceProbeResult:
        return echo(request, details={"request_id": SECRET})

    transport = RecordingTransport(probe_answer=answer)
    assert invoke(PROBE, transport) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert only_document(captured.err) == NO_ANSWER
    assert SECRET not in captured.err
