"""The overall call deadline, and the cancellation token that aborts a call early.

Two values every client call carries, and the two ways a call can end without
an answer from the peer: the clock ran out, or the caller gave up.

**The deadline is the whole call's, and it never resets.** A budget that is
re-derived per attempt is not a deadline: three attempts under a "5 second
timeout" that each reset the clock take fifteen seconds, and the caller was
told five. :class:`Deadline` therefore computes one absolute end at
construction and answers every later question against that same end. There is
deliberately no ``reset``, no ``extend``, and no per-operation constructor, and
the class is frozen so the end cannot be reassigned. A retry, a reconnect, or a
second frame on the same call all read a smaller remainder from the same
deadline; none of them gets a fresh one.

**The clock is injected.** The end is an instant on a *monotonic* timeline, not
a wall-clock time: a system clock adjustment mid-call must not shorten or
extend a timeout. The clock is a parameter so a test can advance time exactly
and assert the arithmetic, rather than sleeping and hoping.

**A budget is a wire value, and is bounded like one.** Every relative budget
this module accepts is a public contract ``DurationMs``: a whole number of
milliseconds in ``[0, 86400000]``. :data:`MAXIMUM_DURATION_MS` is that bound,
and :meth:`Deadline.remaining_ms` never reports past it, so the value a caller
puts in a request's ``deadline_ms`` is always one the contract admits. Refusing
a larger budget rather than clamping it is the fail-closed direction: a caller
asking for a week has misunderstood something, and silently giving them a day
would hide it.

**Every public failure is intentional.** A wrong *kind* of value raises
``TypeError`` and an impossible *amount* raises ``ValueError``, both from a
check written here. Neither is ever a ``math`` or arithmetic error escaping
from the middle of a calculation, because those name an operand rather than the
argument the caller passed and say nothing about which bound was missed.

Standard library only, and no import of any sibling distribution. Nothing here
performs I/O or knows what a transport is.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from omnivia_core_client.errors import DeadlineExceededError, OperationCancelledError

__all__ = [
    "MAXIMUM_DURATION_MS",
    "MAXIMUM_TIMEOUT_SECONDS",
    "CancellationToken",
    "Deadline",
    "MonotonicClock",
]

MonotonicClock = Callable[[], float]
"""A zero-argument source of monotonically non-decreasing seconds.

``time.monotonic`` is the production one. Its epoch is undefined and its values
are only meaningful as differences, which is exactly what a deadline needs.
"""

_MILLISECONDS_PER_SECOND: Final = 1000

MAXIMUM_DURATION_MS: Final = 86_400_000
"""The largest budget the public contract's ``DurationMs`` admits: 24 hours.

Restated here as the literal the contract's own JSON Schema states, because a
``DurationMs`` is a plain ``int`` in the generated types and carries its bounds
nowhere a caller could read them at runtime. It is the *wire* bound, so it is
what :meth:`Deadline.remaining_ms` must stay inside and what
:meth:`Deadline.after_ms` refuses to exceed."""

MAXIMUM_TIMEOUT_SECONDS: Final = MAXIMUM_DURATION_MS / _MILLISECONDS_PER_SECOND
"""The same bound in seconds, 86400.0, for :meth:`Deadline.after`. Derived, so
the two entry points cannot come to disagree about how long is too long."""


def _require_real(value: object, name: str) -> float:
    """Return ``value`` as a ``float``, refusing anything that is not a number.

    ``bool`` is refused even though Python makes it an ``int``: ``True`` is not
    "one second", it is a value that reached a numeric argument by mistake, and
    accepting it would turn that mistake into a working call with a nonsense
    budget. Everything outside ``int``/``float`` is refused here rather than
    left to raise from inside an arithmetic or ``math`` call further down,
    where the message would name an operand instead of this argument.

    The offending value is deliberately not put in the message. It is only
    described by type -- an ``int`` of a few thousand digits cannot be rendered
    at all under the interpreter's conversion limit, so formatting it would
    replace this diagnostic with an unrelated ``ValueError``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be an int or a float, got {type(value).__name__}")
    try:
        return float(value)
    except OverflowError:
        # An `int` above the binary64 range. `math.isfinite` would raise the same
        # `OverflowError` a moment later, so the conversion is done here where it
        # can be turned into a statement about this argument.
        raise ValueError(f"{name} is too large to convert to a finite number") from None


@dataclass(frozen=True, slots=True)
class Deadline:
    """One absolute end on an injected monotonic timeline.

    Construct it with :meth:`after` or :meth:`after_ms` from a relative budget,
    or directly from an absolute ``end`` already read off the same ``clock``.
    Frozen: the end is fixed for the life of the call it describes.
    """

    clock: MonotonicClock
    """The monotonic clock ``end`` is expressed on, and the one every remaining
    calculation re-reads. Two deadlines built on different clocks are not
    comparable and must never be mixed within one call."""

    end: float
    """The absolute instant, on ``clock``'s timeline, at which the call is out
    of time. Set once."""

    def __post_init__(self) -> None:
        """Refuse an end that is not a real, finite instant.

        Two separate refusals, because they are two different mistakes. A
        non-number -- or a ``bool``, which Python would otherwise let through as
        an ``int`` -- is the wrong *kind* of value and raises ``TypeError``;
        checking it here is what keeps every later comparison from raising out
        of the middle of an arithmetic expression instead. ``inf`` and ``nan``
        are the right kind and not instants: ``inf`` would mean "no deadline",
        which is a decision to make by not having a deadline rather than by
        having an unreachable one, and ``nan`` would silently make every
        comparison below false.

        Deliberately *not* bounded by :data:`MAXIMUM_DURATION_MS`. That bounds a
        relative budget; ``end`` is an absolute reading of an injected clock
        whose epoch is undefined, so no particular magnitude is wrong for it.
        Only *unrepresentable* is: an ``int`` past the binary64 range would make
        ``math.isfinite`` itself raise ``OverflowError``, so the conversion runs
        through :func:`_require_real` where that becomes a ``ValueError`` naming
        this field.
        """
        end = _require_real(self.end, "deadline end")
        if not math.isfinite(end):
            raise ValueError(f"deadline end must be a finite instant, got {end!r}")

    @classmethod
    def after(
        cls, timeout_seconds: float, *, clock: MonotonicClock = time.monotonic
    ) -> Deadline:
        """A deadline ending ``timeout_seconds`` from now, measured on ``clock``.

        The clock is read exactly once, here. A zero timeout is accepted and
        yields an already-expired deadline: "no time at all" is a coherent
        instruction and is reported as a deadline failure rather than treated
        as "no deadline".

        The budget is bounded above by :data:`MAXIMUM_TIMEOUT_SECONDS`, the
        contract's ``DurationMs`` maximum expressed in seconds. A larger one is
        refused rather than clamped: it cannot be put on the wire, and a caller
        who asked for it is working from a misunderstanding that a silent
        adjustment would preserve.
        """
        seconds = _require_real(timeout_seconds, "timeout_seconds")
        if not math.isfinite(seconds) or not 0.0 <= seconds <= MAXIMUM_TIMEOUT_SECONDS:
            raise ValueError(
                "timeout_seconds must be a finite, non-negative number of seconds at "
                f"or below {MAXIMUM_TIMEOUT_SECONDS}, the contract's DurationMs maximum"
            )
        return cls(clock=clock, end=clock() + seconds)

    @classmethod
    def after_ms(
        cls, timeout_ms: int, *, clock: MonotonicClock = time.monotonic
    ) -> Deadline:
        """A deadline from a millisecond budget, the unit the contract's ``DurationMs`` uses.

        ``DurationMs`` is a whole number of milliseconds, so a fractional or
        otherwise non-integer argument is the wrong *kind* of value and raises
        ``TypeError``. One outside ``[0, MAXIMUM_DURATION_MS]`` is the right
        kind and an amount the contract has no way to carry, and raises
        ``ValueError`` -- the two failures stay distinct because they call for
        different fixes.

        The bounds are checked on the milliseconds rather than on the seconds
        :meth:`after` sees, so the diagnostic names the unit the caller used.
        """
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            raise TypeError(
                "timeout_ms must be a whole number of milliseconds, got "
                f"{type(timeout_ms).__name__}"
            )
        if not 0 <= timeout_ms <= MAXIMUM_DURATION_MS:
            raise ValueError(
                "timeout_ms must be a DurationMs: a whole number of milliseconds "
                f"in [0, {MAXIMUM_DURATION_MS}]"
            )
        return cls.after(timeout_ms / _MILLISECONDS_PER_SECOND, clock=clock)

    def remaining_seconds(self) -> float:
        """Seconds left before the end, clamped at zero.

        Never negative: "how long may I still wait" has no meaningful negative
        answer, and a negative timeout handed to a socket call means something
        different on every platform.
        """
        return max(0.0, self.end - self.clock())

    def remaining_ms(self) -> int:
        """:meth:`remaining_seconds` in whole milliseconds, as a valid ``DurationMs``.

        Rounded *down* rather than to nearest, because this value is what gets
        put in a request's ``deadline_ms`` for the peer to work to. Rounding up
        would promise a peer up to a millisecond of budget the caller does not
        actually have, and the peer would spend it.

        Capped at :data:`MAXIMUM_DURATION_MS` for the same reason and in the
        same direction. A ``Deadline`` built from an absolute ``end`` -- the one
        constructor that takes no relative budget, and so applies no bound -- may
        legitimately sit further out than the contract can express. Reporting
        the true remainder would produce a number no ``deadline_ms`` field
        accepts, and reporting the cap tells the peer only that it has less time
        than it really does, which is the safe error to make. Every value this
        returns is therefore a ``DurationMs``: a whole number in
        ``[0, MAXIMUM_DURATION_MS]``.
        """
        seconds = min(self.remaining_seconds(), MAXIMUM_TIMEOUT_SECONDS)
        return math.floor(seconds * _MILLISECONDS_PER_SECOND)

    @property
    def expired(self) -> bool:
        """Whether the end has been reached. Re-reads the clock on every access."""
        return self.remaining_seconds() <= 0.0

    def assert_not_expired(self, *, operation: str = "call") -> float:
        """Return the remaining seconds, or raise if there are none.

        The single check a transport makes before committing to anything that
        takes time. Returning the remainder rather than ``None`` means the
        caller reads the clock once, so the value it then waits on is exactly
        the value that was just checked.
        """
        remaining = self.remaining_seconds()
        if remaining <= 0.0:
            raise DeadlineExceededError(
                f"{operation}: the deadline for this call has passed, so no time is "
                "left to spend on it"
            )
        return remaining


def _noop() -> None:
    """Do nothing. The unregister handle for a callback that has already run."""


class CancellationToken:
    """Thread-safe cancellation state shared between a caller and an in-flight call.

    A client call blocks on a transport, and the thread that wants to abort it
    is not the thread that is blocked. That is the whole reason this exists: a
    flag alone cannot wake a blocked read, so the token also carries
    *callbacks*, which is where a future transport registers the action that
    actually interrupts it -- shutting down its socket, closing its pipe handle,
    setting an event the read is also waiting on.

    Cancellation is one-way. There is no ``reset``: a cancelled call is
    finished, and the next call gets its own token.

    **Callbacks are deterministic.** They run in registration order, exactly
    once each, on whichever thread calls :meth:`cancel` -- or, for a callback
    registered *after* cancellation, inline on the registering thread, so
    "registered too late" never means "silently never runs". The pending list
    is snapshotted and cleared while the lock is held, so two concurrent
    :meth:`cancel` calls cannot run the same callback twice, and the callbacks
    themselves run with the lock released, so a callback may call back into the
    token without deadlocking.

    **Callback exceptions are contained, not swallowed.** An ``Exception`` from
    one callback never prevents the remaining callbacks from running and never
    propagates out of :meth:`cancel`: aborting a call must not itself fail, or
    a caller would be left unable to abandon a hung transport. Contained is not
    silent -- each one is kept and readable from :attr:`callback_failures`. A
    ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``) is deliberately
    *not* contained: those mean the process itself is going away, and holding
    one back to finish a callback list would be the wrong trade.
    """

    __slots__ = ("_callbacks", "_cancelled", "_failures", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._callbacks: list[Callable[[], None]] = []
        self._failures: tuple[Exception, ...] = ()

    @property
    def cancelled(self) -> bool:
        """Whether this token has been cancelled."""
        with self._lock:
            return self._cancelled

    @property
    def callback_failures(self) -> tuple[Exception, ...]:
        """Exceptions raised by cancellation callbacks, in the order they were raised.

        Empty unless a callback failed. Reading this is how a caller finds out
        that an abort action did not work, without :meth:`cancel` having been
        able to fail.
        """
        with self._lock:
            return self._failures

    def cancel(self) -> bool:
        """Cancel the call, running every registered callback.

        Returns ``True`` when this call performed the transition and ``False``
        when the token was already cancelled, so the caller can tell "I
        cancelled it" from "someone else already had" without a second read
        that could race.
        """
        with self._lock:
            if self._cancelled:
                return False
            self._cancelled = True
            pending = tuple(self._callbacks)
            self._callbacks.clear()
        self._record(self._run(pending))
        return True

    def register(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Run ``callback`` when this token is cancelled; return an unregister handle.

        If the token is already cancelled the callback runs immediately, inline,
        before this returns -- a transport that registers its abort action and
        then starts reading must never end up reading with the cancellation
        already past and nothing waiting to interrupt it.

        The returned handle removes the callback if it has not run yet, and
        does nothing if it has. Calling it more than once is safe. A transport
        calls it on the normal path, so a long-lived token accumulating one
        callback per completed call cannot grow without bound.
        """
        with self._lock:
            if not self._cancelled:
                self._callbacks.append(callback)
                cancelled = False
            else:
                cancelled = True
        if cancelled:
            self._record(self._run((callback,)))
            return _noop

        def unregister() -> None:
            with self._lock:
                try:
                    self._callbacks.remove(callback)
                except ValueError:
                    # Already run by `cancel`, or already unregistered. Both are
                    # ordinary outcomes of a race the caller cannot avoid, not errors.
                    pass

        return unregister

    def raise_if_cancelled(self, *, operation: str = "call") -> None:
        """Raise :class:`OperationCancelledError` if this token is already cancelled.

        The pre-send check: a transport calls this before writing the first
        byte, so a call cancelled before it started is never put on the wire at
        all. Sending it anyway would leave the peer doing work for an answer
        nobody is waiting for -- and, for a mutation, would apply an effect the
        caller had already withdrawn.
        """
        if self.cancelled:
            raise OperationCancelledError(
                f"{operation}: cancelled before it was sent, so nothing was sent"
            )

    def _record(self, failures: tuple[Exception, ...]) -> None:
        if not failures:
            return
        with self._lock:
            self._failures = self._failures + failures

    @staticmethod
    def _run(callbacks: tuple[Callable[[], None], ...]) -> tuple[Exception, ...]:
        """Run every callback in order, containing and collecting `Exception`s."""
        failures: list[Exception] = []
        for callback in callbacks:
            try:
                callback()
            # Blind by design, and the whole point of this method: aborting a
            # call must not itself fail, and one broken abort action must not
            # strand the others. Every exception is kept and surfaced on
            # `callback_failures`, so nothing is swallowed.
            except Exception as error:  # noqa: BLE001
                failures.append(error)
        return tuple(failures)
