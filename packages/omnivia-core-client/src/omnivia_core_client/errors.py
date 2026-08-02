"""The typed failures the shared client protocol foundation raises.

One class per thing that can go wrong at this layer, all under a single
:class:`ClientError` base so a caller that only wants to know "the call did not
happen" needs one ``except``. Nothing here is a discovery, managed-startup, or
retry failure: none of those behaviours exists yet, so no error names them.
An error class is added when the code that raises it lands, not before.

**Diagnostics never carry payload content.** Every message in this package is
built from structural facts -- byte counts, byte offsets, JSON value kinds,
version strings, field names -- and never from the bytes or values that were in
flight. A frame may carry a bearer token, a principal claim, or a record's
contents, and an exception is exactly the value most likely to be logged, put
in a bug report, or rendered into a crash dump. So the rule is absolute rather
than best-effort, and it extends to the exception *chain*: where a standard
library parser's own exception would carry the payload in its arguments (as
``UnicodeDecodeError`` does), that exception is deliberately not chained, and
the diagnostic reports the category and offset instead.

Standard library only, and no import of any sibling distribution.
"""

from __future__ import annotations

__all__ = [
    "ClientError",
    "CompatibilityError",
    "DeadlineExceededError",
    "OperationCancelledError",
    "ProtocolError",
    "TransportError",
]


class ClientError(Exception):
    """Base class for every failure this package raises.

    Catching this catches a client-layer failure and nothing else: a
    ``ContractDecodeError`` from the public ``omnivia_core`` contracts is a
    statement about a *document*, and stays distinct on purpose.
    """


class ProtocolError(ClientError):
    """A frame violated the frozen OVC1 wire format.

    Raised for every framing fault: a wrong magic, a length outside the frozen
    bounds, a truncated or over-long frame, bytes that are not valid UTF-8 or
    not valid JSON, a root that is not a JSON object, and JSON that is well
    formed but not in the canonical byte form OVC1 requires.
    """


class TransportError(ClientError):
    """A transport could not carry a call to a peer and back.

    The declared failure mode of :class:`~omnivia_core_client.transport.ClientTransport`:
    an implementation raises this when the call did not complete for a reason
    that is about the connection rather than about the message -- refused,
    dropped mid-call, or answered with something that is not a frame. A frame
    that arrived and was malformed is a :class:`ProtocolError`; a peer that
    answered correctly with an application error is not an exception at all,
    but an error response envelope.
    """


class CompatibilityError(ClientError):
    """This client build and one endpoint could not agree on a version.

    Covers both directions of the fail-closed rule: a version window that is
    malformed or reversed, and windows that are each well formed but admit no
    version in common. Also raised for a protocol or descriptor major this
    build does not implement, because a major difference is breaking by
    definition and there is no safe way to proceed on a guess.
    """


class DeadlineExceededError(ClientError):
    """The overall call deadline ran out.

    The deadline covers the whole call, not one attempt within it, so this
    means no time is left to spend anywhere -- not that one step was slow.
    """


class OperationCancelledError(ClientError):
    """The caller cancelled the call.

    Distinct from :class:`DeadlineExceededError` on purpose: a cancelled call
    was abandoned by the caller and a timed-out one was abandoned by the clock,
    and reporting either as the other misattributes the cause.
    """
