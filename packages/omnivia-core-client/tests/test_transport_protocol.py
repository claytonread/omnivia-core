"""The transport contract: its typing, its runtime behaviour, and its preconditions.

The protocol is the whole deliverable of ``transport.py`` -- the concrete
transports live in their own modules and are tested there -- so these tests
assert what the protocol *is*: which methods it declares, with which parameters
and which public contract types, what ``isinstance`` does with it, and that a
call already cancelled or already out of time is refused before anything is
sent.

The request and response used below are decoded from the checked-in vectors, so
the fake transport moves real accepted contract documents rather than
hand-built approximations of them.
"""

from __future__ import annotations

import inspect
import json
import typing
from pathlib import Path
from typing import Any, get_type_hints

import pytest
from omnivia_core_client.deadline import CancellationToken, Deadline
from omnivia_core_client.errors import (
    DeadlineExceededError,
    OperationCancelledError,
    TransportError,
)
from omnivia_core_client.transport import ClientTransport, enforce_send_preconditions

from omnivia_core.contracts.v1 import (
    RequestEnvelope,
    ResponseEnvelope,
    ServiceProbeRequest,
    ServiceProbeResult,
    SuccessResponseEnvelope,
    codec,
)

MANIFEST: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "ovc1-v1.json").read_text(
        encoding="utf-8"
    )
)


def _vector(vector_id: str) -> Any:
    return next(
        vector["payload"] for vector in MANIFEST["vectors"] if vector["id"] == vector_id
    )


REQUEST = codec.decode_request(_vector("application.request"))
RESPONSE = codec.decode_response(_vector("application.success"))
PROBE = ServiceProbeRequest.from_wire(_vector("probe.health.request"))
PROBE_RESULT = ServiceProbeResult.from_wire(_vector("probe.health.result"))


class FakeClock:
    """A monotonic clock a test advances by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingTransport:
    """A conforming transport that records what it was asked, and sends nothing."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, float]] = []

    def call(
        self,
        request: RequestEnvelope,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ResponseEnvelope:
        remaining = enforce_send_preconditions(
            deadline=deadline, cancellation=cancellation, operation=request.operation
        )
        self.sent.append((request.operation, remaining))
        return RESPONSE

    def probe(
        self,
        request: ServiceProbeRequest,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ServiceProbeResult:
        remaining = enforce_send_preconditions(
            deadline=deadline, cancellation=cancellation, operation=request.probe
        )
        self.sent.append((request.probe, remaining))
        return PROBE_RESULT


class CallOnlyTransport:
    """Half a transport: `call` without `probe`."""

    def call(
        self,
        request: RequestEnvelope,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ResponseEnvelope:
        raise TransportError("not connected")


# --------------------------------------------------------------------------
# It is a Protocol, not a base class
# --------------------------------------------------------------------------


def test_client_transport_is_a_protocol() -> None:
    assert getattr(ClientTransport, "_is_protocol", False) is True
    # Widened to `object` because `typing.Protocol` is a special form rather than
    # a class, and a type checker rules the membership test out statically
    # against the narrower element type it infers for an `__mro__`.
    mro: tuple[object, ...] = ClientTransport.__mro__
    assert typing.Protocol in mro


def test_a_protocol_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        ClientTransport()  # type: ignore[misc]


def test_conformance_is_structural_and_needs_no_inheritance() -> None:
    transport = RecordingTransport()
    assert ClientTransport not in type(transport).__mro__
    assert isinstance(transport, ClientTransport)


def test_a_partial_implementation_is_not_a_transport() -> None:
    assert not isinstance(CallOnlyTransport(), ClientTransport)
    assert not isinstance(object(), ClientTransport)


def test_the_protocol_declares_exactly_one_application_call_and_one_probe_call() -> (
    None
):
    declared = {
        name
        for name in vars(ClientTransport)
        if not name.startswith("_") and callable(getattr(ClientTransport, name))
    }
    assert declared == {"call", "probe"}


# --------------------------------------------------------------------------
# Its signatures
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["call", "probe"])
def test_each_method_takes_a_request_a_deadline_and_a_cancellation_token(
    method: str,
) -> None:
    parameters = list(
        inspect.signature(getattr(ClientTransport, method)).parameters.values()
    )

    assert [parameter.name for parameter in parameters] == [
        "self",
        "request",
        "deadline",
        "cancellation",
    ]
    assert parameters[1].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    # Keyword-only, so a call site can never transpose the deadline and the token.
    assert parameters[2].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters[3].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters[2].default is inspect.Parameter.empty
    assert parameters[3].default is None


def test_the_application_call_carries_the_public_envelopes() -> None:
    hints = get_type_hints(ClientTransport.call)
    assert hints["request"] is RequestEnvelope
    assert hints["return"] == ResponseEnvelope
    assert hints["deadline"] is Deadline
    assert hints["cancellation"] == CancellationToken | None


def test_the_probe_call_carries_the_public_probe_types() -> None:
    hints = get_type_hints(ClientTransport.probe)
    assert hints["request"] is ServiceProbeRequest
    assert hints["return"] is ServiceProbeResult
    assert hints["deadline"] is Deadline
    assert hints["cancellation"] == CancellationToken | None


def test_the_probe_call_is_not_the_application_call() -> None:
    """A probe must be answerable before a workspace, authority or version exists."""
    assert get_type_hints(ClientTransport.probe)["request"] is not RequestEnvelope
    assert get_type_hints(ClientTransport.call)["return"] is not ServiceProbeResult


# --------------------------------------------------------------------------
# Send preconditions
# --------------------------------------------------------------------------


def test_preconditions_return_the_remaining_seconds() -> None:
    clock = FakeClock(now=0.0)
    deadline = Deadline.after(5.0, clock=clock)
    clock.advance(1.5)
    assert enforce_send_preconditions(deadline=deadline) == 3.5


def test_a_pre_cancelled_call_is_never_sent() -> None:
    token = CancellationToken()
    token.cancel()
    transport = RecordingTransport()

    with pytest.raises(OperationCancelledError):
        transport.call(
            REQUEST,
            deadline=Deadline.after(30.0, clock=FakeClock()),
            cancellation=token,
        )
    assert transport.sent == []


def test_a_pre_cancelled_probe_is_never_sent() -> None:
    token = CancellationToken()
    token.cancel()
    transport = RecordingTransport()

    with pytest.raises(OperationCancelledError):
        transport.probe(
            PROBE, deadline=Deadline.after(30.0, clock=FakeClock()), cancellation=token
        )
    assert transport.sent == []


def test_a_call_with_no_time_left_is_never_sent() -> None:
    clock = FakeClock(now=0.0)
    deadline = Deadline.after(1.0, clock=clock)
    clock.advance(1.0)
    transport = RecordingTransport()

    with pytest.raises(DeadlineExceededError):
        transport.call(REQUEST, deadline=deadline)
    assert transport.sent == []


def test_cancellation_is_reported_ahead_of_an_expired_deadline() -> None:
    """Both are true; the caller abandoned the call, so that is the cause reported."""
    clock = FakeClock(now=0.0)
    deadline = Deadline.after(1.0, clock=clock)
    clock.advance(10.0)
    token = CancellationToken()
    token.cancel()

    with pytest.raises(OperationCancelledError):
        enforce_send_preconditions(deadline=deadline, cancellation=token)


def test_a_live_call_passes_the_preconditions_and_reaches_the_transport() -> None:
    clock = FakeClock(now=0.0)
    deadline = Deadline.after(30.0, clock=clock)
    token = CancellationToken()
    transport = RecordingTransport()

    response = transport.call(REQUEST, deadline=deadline, cancellation=token)
    probe_result = transport.probe(PROBE, deadline=deadline, cancellation=token)

    assert isinstance(response, SuccessResponseEnvelope)
    assert isinstance(probe_result, ServiceProbeResult)
    assert transport.sent == [("memory.get", 30.0), ("service.health", 30.0)]


def test_a_transport_may_be_called_without_a_cancellation_token() -> None:
    transport = RecordingTransport()
    transport.call(REQUEST, deadline=Deadline.after(5.0, clock=FakeClock()))
    assert transport.sent == [("memory.get", 5.0)]


def test_the_deadline_a_transport_sees_shrinks_across_calls_and_never_resets() -> None:
    clock = FakeClock(now=0.0)
    deadline = Deadline.after(10.0, clock=clock)
    transport = RecordingTransport()

    for _ in range(3):
        transport.probe(PROBE, deadline=deadline)
        clock.advance(3.0)

    assert [remaining for _, remaining in transport.sent] == [10.0, 7.0, 4.0]


def test_cancelling_mid_flight_reaches_a_registered_abort_action() -> None:
    """The reason the token carries callbacks: a blocked read cannot poll a flag."""
    token = CancellationToken()
    aborted: list[str] = []
    unregister = token.register(lambda: aborted.append("socket shut down"))

    assert aborted == []
    token.cancel()
    assert aborted == ["socket shut down"]
    unregister()


# --------------------------------------------------------------------------
# What this module, and this package, deliberately do not ship
# --------------------------------------------------------------------------


def test_the_contract_module_ships_no_transport_of_its_own() -> None:
    """The seam stays a seam. Both concrete transports live in their own modules.

    ``HttpTransport`` and ``HTTPTransport`` left the forbidden list below when
    the authenticated HTTP transport landed, exactly as ``HTTP transport`` left
    the README's "not implemented" section: a name is forbidden here because the
    surface is absent, and keeping it after the surface shipped would be
    asserting the package is missing something it has. What is still absent --
    the Windows named pipe, retry, managed startup, and the high-level client --
    is unchanged, and so is the rule that ``transport.py`` itself exports two
    names and no implementation.
    """
    import omnivia_core_client
    from omnivia_core_client import transport as transport_module

    assert transport_module.__all__ == ["ClientTransport", "enforce_send_preconditions"]
    forbidden = (
        "LocalTransport",
        "UnixSocketTransport",
        "NamedPipeTransport",
        "connect",
        "dial",
        "discover",
        "start_service",
        "ensure_service",
        "retry",
        "with_retry",
        "OmniviaClient",
        "CoreClient",
        "Client",
    )
    for name in forbidden:
        assert name not in omnivia_core_client.__all__, name
        assert not hasattr(omnivia_core_client, name), name

    # The counterweight: what did ship is exported, and from its own module.
    for shipped in ("LocalIpcTransport", "HttpTransport"):
        assert shipped in omnivia_core_client.__all__, shipped
        assert not hasattr(transport_module, shipped), shipped
