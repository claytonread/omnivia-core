"""The seam every client call crosses: what a transport must be, stated as a type.

:class:`ClientTransport` is a :class:`typing.Protocol`, not a base class. A
transport is *injected*, and structural typing is what makes that honest: a
fake in a test, a local-socket transport, and an HTTP transport are the same
type to everything above them without any of them importing the others or
inheriting from a shared parent that would end up owning behaviour none of them
share.

**No concrete transport lives in this module.** It defines the contract and the
one precondition every implementation must enforce, and stops there. The two
that ship are elsewhere in this package and neither is imported here:
:class:`~omnivia_core_client.local_ipc.LocalIpcTransport` over the
installation-local endpoint, and
:class:`~omnivia_core_client.http_transport.HttpTransport` over an authenticated
HTTP v1 endpoint. Connection pooling, multiplexing, retry, the Windows named
pipe, and managed startup are still absent; those arrive in their own packets,
and each will satisfy this protocol rather than change it.

Two calls, deliberately kept apart:

- :meth:`ClientTransport.call` carries an application ``RequestEnvelope`` and
  returns a ``ResponseEnvelope`` -- the success or error branch the peer chose.
  A peer that answers with an application error has answered; that is a
  response, not an exception.
- :meth:`ClientTransport.probe` carries a ``ServiceProbeRequest`` and returns a
  ``ServiceProbeResult``. A probe must be answerable before a workspace,
  authority, or version negotiation exists, which is exactly why the contract
  gives it its own request and result types rather than an operation on the
  application envelope. Sharing one method would force a probe through
  machinery that presumes all three.

Both take the whole-call :class:`~omnivia_core_client.deadline.Deadline` rather
than a relative timeout in seconds. An implementation derives the relative
timeout it needs from ``deadline.remaining_seconds()`` at the moment it is
about to wait -- so a reconnect, a second frame, or a retry inside the
implementation each get what is *left*, never a fresh budget.

Standard library plus the public ``omnivia_core`` contracts only.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnivia_core.contracts.v1 import (
    RequestEnvelope,
    ResponseEnvelope,
    ServiceProbeRequest,
    ServiceProbeResult,
)
from omnivia_core_client.deadline import CancellationToken, Deadline

__all__ = [
    "ClientTransport",
    "enforce_send_preconditions",
]


@runtime_checkable
class ClientTransport(Protocol):
    """One synchronous request/response transport to a single service endpoint.

    An implementation must, before writing the first byte of a call:

    1. call :func:`enforce_send_preconditions`, so a call that is already
       cancelled or already out of time is never put on the wire;
    2. bound every wait it then performs by the remaining time that call
       returned.

    It raises :class:`~omnivia_core_client.errors.TransportError` when the call
    could not be carried, :class:`~omnivia_core_client.errors.ProtocolError`
    when what came back was not a well-formed frame,
    :class:`~omnivia_core_client.errors.DeadlineExceededError` when the deadline
    passed, and :class:`~omnivia_core_client.errors.OperationCancelledError`
    when the caller cancelled. It returns normally for every answer the peer
    actually gave, including an error response.

    Nothing here implies a persistent connection. An implementation may open
    one connection per call or keep one open; either satisfies this protocol,
    and neither is promised to a caller. The protocol carries no pooling,
    multiplexing, streaming, or server-push semantics, so no caller may assume
    them.
    """

    def call(
        self,
        request: RequestEnvelope,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ResponseEnvelope:
        """Send one application request and return the peer's response envelope."""
        ...

    def probe(
        self,
        request: ServiceProbeRequest,
        *,
        deadline: Deadline,
        cancellation: CancellationToken | None = None,
    ) -> ServiceProbeResult:
        """Send one runtime probe and return the peer's probe result."""
        ...


def enforce_send_preconditions(
    *,
    deadline: Deadline,
    cancellation: CancellationToken | None = None,
    operation: str = "call",
) -> float:
    """Return the seconds still available, or refuse to send at all.

    The one check shared by every transport, here rather than in each of them
    so they cannot drift on it.

    Cancellation is tested first, and the order is not arbitrary: a call the
    caller has already abandoned is cancelled whether or not time also ran out,
    and reporting it as a deadline failure would tell the caller the peer was
    slow when in fact nobody ever asked it anything.

    Returns the remaining seconds so the caller waits on exactly the value that
    was just checked, rather than re-reading the clock and waiting on a
    slightly different one.
    """
    if cancellation is not None:
        cancellation.raise_if_cancelled(operation=operation)
    return deadline.assert_not_expired(operation=operation)
