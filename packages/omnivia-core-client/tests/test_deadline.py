"""The whole-call deadline and the cancellation token.

Time is injected, never slept on: every deadline assertion here advances a fake
clock by an exact amount, so what is being tested is the arithmetic rather than
the scheduler.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable

import pytest
from omnivia_core_client.deadline import (
    MAXIMUM_DURATION_MS,
    MAXIMUM_TIMEOUT_SECONDS,
    CancellationToken,
    Deadline,
)
from omnivia_core_client.errors import (
    ClientError,
    DeadlineExceededError,
    OperationCancelledError,
)

#: An integer the interpreter will not convert to a decimal string, built by
#: arithmetic so the test itself does not hit that limit while constructing it.
UNRENDERABLE_INTEGER = 10**5000


class FakeClock:
    """A monotonic clock a test advances by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def test_after_reads_the_injected_clock_once_and_fixes_an_absolute_end() -> None:
    clock = FakeClock(now=100.0)
    deadline = Deadline.after(5.0, clock=clock)

    assert clock.reads == 1
    assert deadline.end == 105.0
    assert deadline.clock is clock


def test_after_ms_uses_the_contracts_millisecond_unit() -> None:
    clock = FakeClock(now=0.0)
    assert Deadline.after_ms(2500, clock=clock).end == 2.5


def test_the_default_clock_is_the_monotonic_one() -> None:
    """Not the wall clock: a system clock adjustment must not move a deadline."""
    assert Deadline.after(1.0).clock is time.monotonic


@pytest.mark.parametrize(
    "timeout",
    [
        -0.001,
        -1.0,
        float("nan"),
        float("inf"),
        float("-inf"),
        MAXIMUM_TIMEOUT_SECONDS + 1,
    ],
)
def test_a_timeout_that_is_not_a_finite_in_range_number_is_refused(
    timeout: float,
) -> None:
    with pytest.raises(ValueError, match="finite, non-negative"):
        Deadline.after(timeout, clock=FakeClock())


@pytest.mark.parametrize(
    "timeout",
    [True, False, "5", None, [1], complex(1, 0), object()],
)
def test_a_timeout_that_is_not_a_number_at_all_is_a_type_error(timeout: object) -> None:
    """The wrong *kind* of value, and never a `math` error naming an operand.

    `True` is here on purpose: Python makes a bool an int, so an unguarded
    check would read it as a one-second budget and the mistake would work.
    """
    with pytest.raises(TypeError, match="must be an int or a float"):
        Deadline.after(timeout, clock=FakeClock())  # type: ignore[arg-type]


def test_a_timeout_too_large_to_be_a_float_is_a_value_error_not_an_overflow() -> None:
    with pytest.raises(ValueError, match="too large"):
        Deadline.after(UNRENDERABLE_INTEGER, clock=FakeClock())


@pytest.mark.parametrize("timeout", [1.5, "5", None, True, False, [1]])
def test_after_ms_refuses_anything_but_a_whole_number_of_milliseconds(
    timeout: object,
) -> None:
    """The wrong kind of value, so `TypeError`; an out-of-range one is `ValueError`."""
    with pytest.raises(TypeError, match="whole number of milliseconds"):
        Deadline.after_ms(timeout, clock=FakeClock())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "timeout_ms",
    [
        pytest.param(-1, id="minus-one"),
        pytest.param(-1000, id="minus-one-second"),
        pytest.param(MAXIMUM_DURATION_MS + 1, id="one-past-the-maximum"),
        pytest.param(UNRENDERABLE_INTEGER, id="too-many-digits-to-render"),
    ],
)
def test_after_ms_refuses_a_budget_outside_the_duration_ms_bounds(
    timeout_ms: int,
) -> None:
    """Including one too large to render: the diagnostic must not try to print it.

    An integer of more than 4300 decimal digits cannot be converted to a string
    under the interpreter's own limit, so a message built with `{value!r}` would
    raise a `ValueError` about that limit in place of the bounds check.
    """
    with pytest.raises(ValueError, match=r"DurationMs.*\[0, 86400000\]"):
        Deadline.after_ms(timeout_ms, clock=FakeClock())


@pytest.mark.parametrize("end", [float("inf"), float("-inf"), float("nan")])
def test_an_end_that_is_not_a_finite_instant_is_refused(end: float) -> None:
    with pytest.raises(ValueError, match="finite instant"):
        Deadline(clock=FakeClock(), end=end)


@pytest.mark.parametrize("end", [True, False, "later", None, [1], complex(1, 0)])
def test_an_end_that_is_not_a_number_is_a_type_error(end: object) -> None:
    with pytest.raises(TypeError, match="must be an int or a float"):
        Deadline(clock=FakeClock(), end=end)  # type: ignore[arg-type]


def test_an_end_too_large_to_be_a_float_is_a_value_error_not_an_overflow() -> None:
    """`math.isfinite` raises `OverflowError` for an int past the binary64 range.

    An absolute end is deliberately unbounded in magnitude, so this is the one
    way it can still be unusable, and it must arrive as a `ValueError` naming
    the field rather than as an arithmetic error naming an operand.
    """
    with pytest.raises(ValueError, match="deadline end is too large"):
        Deadline(clock=FakeClock(), end=UNRENDERABLE_INTEGER)


@pytest.mark.parametrize("end", [0, 1, -1, 10**15])
def test_an_integer_end_is_a_perfectly_good_instant(end: int) -> None:
    """A clock may return whole seconds; that is a number, not a wrong kind."""
    assert Deadline(clock=FakeClock(now=0.0), end=end).end == end


# --------------------------------------------------------------------------
# The budget is a wire value, and is bounded like one
# --------------------------------------------------------------------------


def test_the_bounds_are_the_contracts_duration_ms_bounds() -> None:
    assert MAXIMUM_DURATION_MS == 86_400_000
    assert MAXIMUM_TIMEOUT_SECONDS == 86_400.0


@pytest.mark.parametrize("timeout_ms", [0, 1, MAXIMUM_DURATION_MS])
def test_the_duration_ms_bounds_themselves_are_accepted(timeout_ms: int) -> None:
    clock = FakeClock(now=0.0)
    deadline = Deadline.after_ms(timeout_ms, clock=clock)
    assert deadline.remaining_ms() == timeout_ms


@pytest.mark.parametrize("timeout", [0.0, MAXIMUM_TIMEOUT_SECONDS])
def test_the_second_bounds_themselves_are_accepted(timeout: float) -> None:
    clock = FakeClock(now=0.0)
    assert Deadline.after(timeout, clock=clock).remaining_seconds() == timeout


def test_remaining_ms_is_always_a_valid_duration_ms() -> None:
    """A `Deadline` built from an absolute end applies no relative bound, so this
    is the one place a wire-invalid duration could otherwise be produced."""
    clock = FakeClock(now=0.0)
    far_future = Deadline(clock=clock, end=1e17)

    assert far_future.remaining_ms() == MAXIMUM_DURATION_MS
    assert far_future.remaining_seconds() > MAXIMUM_TIMEOUT_SECONDS

    for seconds in (0.0, 0.5, 1.0, 3600.0, MAXIMUM_TIMEOUT_SECONDS, 1e6, 1e17):
        deadline = Deadline(clock=clock, end=seconds)
        assert 0 <= deadline.remaining_ms() <= MAXIMUM_DURATION_MS
        assert isinstance(deadline.remaining_ms(), int)


@pytest.mark.parametrize("reading", [float("inf"), float("-inf"), float("nan")])
def test_a_clock_that_misbehaves_still_yields_a_valid_duration_ms(
    reading: float,
) -> None:
    """`math.floor(inf)` is an `OverflowError`, and this is the one caller of it.

    The end is checked at construction, so a clock reading is the only way a
    non-finite value can reach the arithmetic afterwards.
    """
    deadline = Deadline(clock=lambda: reading, end=0.0)
    assert 0 <= deadline.remaining_ms() <= MAXIMUM_DURATION_MS


def test_capping_reports_less_time_than_there_is_never_more() -> None:
    """The safe direction: a peer told it has less time simply finishes sooner."""
    clock = FakeClock(now=0.0)
    deadline = Deadline(clock=clock, end=MAXIMUM_TIMEOUT_SECONDS * 3)
    assert deadline.remaining_ms() < deadline.remaining_seconds() * 1000


# --------------------------------------------------------------------------
# Remaining time
# --------------------------------------------------------------------------


def test_remaining_falls_as_the_clock_advances() -> None:
    clock = FakeClock(now=0.0)
    deadline = Deadline.after(10.0, clock=clock)

    assert deadline.remaining_seconds() == 10.0
    clock.advance(4.0)
    assert deadline.remaining_seconds() == 6.0
    clock.advance(6.0)
    assert deadline.remaining_seconds() == 0.0


def test_remaining_is_clamped_at_zero_and_never_negative() -> None:
    clock = FakeClock(now=0.0)
    deadline = Deadline.after(1.0, clock=clock)
    clock.advance(60.0)

    assert deadline.remaining_seconds() == 0.0
    assert deadline.remaining_ms() == 0


def test_remaining_ms_rounds_down_so_a_peer_is_never_promised_time_that_is_gone() -> (
    None
):
    clock = FakeClock(now=0.0)
    deadline = Deadline.after(1.9999, clock=clock)
    assert deadline.remaining_ms() == 1999


def test_expired_flips_exactly_at_the_end() -> None:
    clock = FakeClock(now=0.0)
    deadline = Deadline.after(1.0, clock=clock)

    assert not deadline.expired
    clock.advance(0.999)
    assert not deadline.expired
    clock.advance(0.001)
    assert deadline.expired


def test_a_zero_timeout_is_already_expired() -> None:
    deadline = Deadline.after(0.0, clock=FakeClock())
    assert deadline.expired
    assert deadline.remaining_seconds() == 0.0


def test_assert_not_expired_returns_the_remaining_seconds() -> None:
    clock = FakeClock(now=0.0)
    deadline = Deadline.after(3.0, clock=clock)
    clock.advance(1.0)
    assert deadline.assert_not_expired() == 2.0


def test_assert_not_expired_raises_once_the_end_has_passed() -> None:
    clock = FakeClock(now=0.0)
    deadline = Deadline.after(1.0, clock=clock)
    clock.advance(1.0)

    with pytest.raises(DeadlineExceededError, match="probe: the deadline"):
        deadline.assert_not_expired(operation="probe")


def test_deadline_failures_are_client_errors() -> None:
    assert issubclass(DeadlineExceededError, ClientError)


# --------------------------------------------------------------------------
# The deadline covers the call, and never resets
# --------------------------------------------------------------------------


def test_the_end_cannot_be_reassigned() -> None:
    deadline = Deadline.after(5.0, clock=FakeClock())
    with pytest.raises(dataclasses.FrozenInstanceError):
        deadline.end = 10_000.0  # type: ignore[misc]


def test_there_is_no_reset_or_extend_api() -> None:
    """A budget that can be renewed mid-call is not a deadline."""
    for name in ("reset", "extend", "renew", "restart", "refresh"):
        assert not hasattr(Deadline, name), name


def test_three_partial_operations_share_one_budget_rather_than_each_getting_it() -> (
    None
):
    """The point of the whole design, stated as a test.

    Three attempts under a five-second deadline take five seconds between them.
    A per-attempt budget would take fifteen, and the caller was told five.
    """
    clock = FakeClock(now=0.0)
    deadline = Deadline.after(5.0, clock=clock)

    budgets = []
    for _ in range(3):
        budgets.append(deadline.assert_not_expired())
        clock.advance(2.0)

    assert budgets == [5.0, 3.0, 1.0]
    assert deadline.expired
    with pytest.raises(DeadlineExceededError):
        deadline.assert_not_expired()


def test_remaining_never_increases_across_repeated_reads() -> None:
    clock = FakeClock(now=0.0)
    deadline = Deadline.after(10.0, clock=clock)

    previous = deadline.remaining_seconds()
    for _ in range(10):
        clock.advance(0.5)
        current = deadline.remaining_seconds()
        assert current <= previous
        previous = current


# --------------------------------------------------------------------------
# Cancellation state
# --------------------------------------------------------------------------


def test_a_new_token_is_not_cancelled() -> None:
    token = CancellationToken()
    assert token.cancelled is False
    assert token.callback_failures == ()
    token.raise_if_cancelled()


def test_cancel_reports_which_call_performed_the_transition() -> None:
    token = CancellationToken()
    assert token.cancel() is True
    assert token.cancel() is False
    assert token.cancelled is True


def test_cancellation_is_one_way() -> None:
    """A cancelled call is finished; the next call gets its own token."""
    for name in ("reset", "uncancel", "clear", "restore"):
        assert not hasattr(CancellationToken, name), name


def test_raise_if_cancelled_prevents_a_send_that_has_not_happened_yet() -> None:
    token = CancellationToken()
    token.cancel()
    with pytest.raises(OperationCancelledError, match="cancelled before it was sent"):
        token.raise_if_cancelled(operation="memory.get")


def test_cancellation_failures_are_client_errors() -> None:
    assert issubclass(OperationCancelledError, ClientError)


# --------------------------------------------------------------------------
# Cancellation callbacks
# --------------------------------------------------------------------------


def test_callbacks_run_in_registration_order_on_cancel() -> None:
    token = CancellationToken()
    order: list[str] = []
    for name in ("first", "second", "third"):
        token.register(lambda name=name: order.append(name))  # type: ignore[misc]

    assert order == []
    token.cancel()
    assert order == ["first", "second", "third"]


def test_a_callback_runs_exactly_once_however_often_cancel_is_called() -> None:
    token = CancellationToken()
    runs: list[int] = []
    token.register(lambda: runs.append(1))

    token.cancel()
    token.cancel()
    token.cancel()
    assert runs == [1]


def test_registering_after_cancellation_runs_the_callback_immediately() -> None:
    """ "Registered too late" must never mean "silently never runs"."""
    token = CancellationToken()
    token.cancel()

    runs: list[int] = []
    token.register(lambda: runs.append(1))
    assert runs == [1]


def test_unregistering_removes_a_callback_that_has_not_run() -> None:
    token = CancellationToken()
    runs: list[str] = []
    unregister = token.register(lambda: runs.append("gone"))
    token.register(lambda: runs.append("kept"))

    unregister()
    unregister()  # idempotent
    token.cancel()
    assert runs == ["kept"]


def test_unregistering_after_the_callback_ran_is_a_no_op() -> None:
    token = CancellationToken()
    unregister = token.register(lambda: None)
    token.cancel()
    unregister()


def test_a_callback_may_call_back_into_the_token_without_deadlocking() -> None:
    """Callbacks run with the lock released, which is what makes this safe."""
    token = CancellationToken()
    observed: list[bool] = []
    token.register(lambda: observed.append(token.cancelled))

    token.cancel()
    assert observed == [True]


def test_a_failing_callback_does_not_stop_the_others_or_escape_cancel() -> None:
    token = CancellationToken()
    runs: list[str] = []

    def boom() -> None:
        raise RuntimeError("closing the socket failed")

    token.register(lambda: runs.append("before"))
    token.register(boom)
    token.register(lambda: runs.append("after"))

    assert token.cancel() is True
    assert runs == ["before", "after"]


def test_a_failing_callback_is_contained_but_not_swallowed() -> None:
    token = CancellationToken()

    def boom() -> None:
        raise RuntimeError("closing the socket failed")

    token.register(boom)
    token.cancel()

    failures = token.callback_failures
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert str(failures[0]) == "closing the socket failed"


def test_a_late_registered_callback_that_fails_is_contained_too() -> None:
    token = CancellationToken()
    token.cancel()

    def boom() -> None:
        raise RuntimeError("too late, and broken")

    token.register(boom)
    assert [str(error) for error in token.callback_failures] == ["too late, and broken"]


def test_a_base_exception_from_a_callback_is_deliberately_not_contained() -> None:
    """`KeyboardInterrupt` means the process is going away; a callback list does not outrank that."""
    token = CancellationToken()

    def interrupt() -> None:
        raise KeyboardInterrupt

    token.register(interrupt)
    with pytest.raises(KeyboardInterrupt):
        token.cancel()


# --------------------------------------------------------------------------
# Thread safety
# --------------------------------------------------------------------------


def _run_concurrently(target: Callable[[], None], *, threads: int) -> None:
    ready = threading.Barrier(threads)

    def worker() -> None:
        ready.wait()
        target()

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join(timeout=10)
        assert not worker_thread.is_alive()


def test_only_one_of_many_concurrent_cancels_performs_the_transition() -> None:
    token = CancellationToken()
    runs: list[int] = []
    winners: list[bool] = []
    lock = threading.Lock()
    token.register(lambda: runs.append(1))

    def cancel() -> None:
        won = token.cancel()
        with lock:
            winners.append(won)

    _run_concurrently(cancel, threads=16)

    assert winners.count(True) == 1
    assert winners.count(False) == 15
    assert runs == [1]


def _race_one_register_against_one_cancel() -> tuple[CancellationToken, list[int]]:
    """Start a register and a cancel at the same instant; return what happened."""
    token = CancellationToken()
    runs: list[int] = []
    started = threading.Barrier(2)

    def register() -> None:
        started.wait()
        token.register(lambda: runs.append(1))

    def cancel() -> None:
        started.wait()
        token.cancel()

    threads = [threading.Thread(target=register), threading.Thread(target=cancel)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    return token, runs


def test_a_callback_registered_while_another_thread_cancels_still_runs_exactly_once() -> (
    None
):
    """Either order is a race the caller cannot avoid; neither may drop or double-run."""
    for _ in range(200):
        token, runs = _race_one_register_against_one_cancel()
        assert runs == [1]
        assert token.cancelled is True
