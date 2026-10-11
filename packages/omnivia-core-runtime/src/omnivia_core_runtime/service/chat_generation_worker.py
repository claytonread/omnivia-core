"""Core-owned background execution of committed Chat generation work.

`chat.command` commits a `SubmitMessage` or `RetryGeneration` and returns; this worker
then carries the generation on a thread of its own. A held provider stream therefore no
longer holds the serialized transport: the next `chat.snapshot`, `chat.events` or
Direction is answered while the stream is still open, and the executor takes that
Direction in at the iteration boundary as it always did.

**One connection, one gate.** The service connection is opened with
`locking_mode = EXCLUSIVE`, so no second connection can open the workspace at all, and
the workspace lease is not authority to hand to one. The worker uses the service
connection under the runner's `sqlite_gate`, as every other thread that touches it does,
and releases the gate only while it waits on the provider: between committed writes,
never inside a transaction.

**Durable rows are the queue.** A hint carries nothing the rows do not. On every wake,
and once at start, the worker drains `ChatGenerationExecutor.execute_next`, which selects
only freshly queued jobs -- a queued submission's, then a fork's (`RegenerateResponse`,
`CommitMessageAmendment`, which commit a job with no queue row) -- and claims each under
its compare-and-set, so work committed before a crash is picked up by the next service
and a claimed job is never claimed, or its provider invoked, a second time. A job that
was mid-stream when its process died is left running for its lease to expire; nothing
here re-invokes it. `RetryGeneration` opens its attempt in its own commit and is carried
by the hint.

**Stopping.** `stop` sets the flag and then takes the gate, so once it returns this
thread cannot touch the connection again: every reacquisition checks the flag first and
unwinds without writing. A provider stream still held then is abandoned exactly as a
crash abandons one, with its durable rows as they were.
"""

from __future__ import annotations

import functools
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

from omnivia_core.chat_contract.v1 import ProviderInvocationRequest
from omnivia_core_runtime.service.chat_generation_executor import ChatGenerationExecutor

__all__ = ["ChatGenerationWorker"]

_T = TypeVar("_T")


class _Stopped(BaseException):
    """Unwinds the worker once a stop was requested.

    A `BaseException` so the executor's `except Exception` boundaries, which terminalize
    a failed attempt, let it through: a stopping service writes nothing on its way out.
    """


@dataclass
class ChatGenerationWorker:
    executor: ChatGenerationExecutor
    gate: threading.RLock
    #: How long `stop` waits for the thread before relying on the gate alone.
    stop_timeout_seconds: float = 1.0
    _retries: deque[Mapping[str, Any]] = field(default_factory=deque)
    _wake: threading.Event = field(default_factory=threading.Event)
    _stopping: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def __post_init__(self) -> None:
        invoke = self.executor.invoke
        self.executor = replace(
            self.executor, invoke=functools.partial(self._stream_outside_gate, invoke)
        )

    def submit(self, **fields: Any) -> None:
        """The `ChatGenerationExecution` a handler calls after its commit. Never blocks."""
        if fields.get("queued_submission_id") is None:
            self._retries.append(fields)
        self._wake.set()

    def start(self) -> None:
        self._wake.set()  # pick up whatever an earlier process committed and never ran
        self._thread = threading.Thread(
            target=self._run, name="omnivia-chat-generation", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(self.stop_timeout_seconds)
        # Past this the thread is gone, or blocked on its provider without the gate and
        # bound to unwind the moment it takes the gate back.
        with self.gate:
            pass

    def _run(self) -> None:
        try:
            while True:
                self._wake.wait()
                self._wake.clear()
                while self._retries:
                    self._locked(functools.partial(self.executor.execute, **self._retries.popleft()))
                while self._locked(self.executor.execute_next) is not None:
                    pass
        except _Stopped:
            return

    def _locked(self, work: Callable[[], _T]) -> _T | None:
        """One unit of work under the gate. A failure ends the unit, never the worker.

        The rows record what the unit did. A drain that fails stops until the next wake
        rather than spinning on a row that keeps failing.
        """
        with self.gate:
            if self._stopping.is_set():
                raise _Stopped
            try:
                return work()
            except Exception:  # noqa: BLE001 - contained like a bad client on the socket
                return None

    def _outside_gate(self, step: Callable[[], _T]) -> _T:
        """Run a provider step with the gate released, then take it back."""
        if self.executor.connection.in_transaction:
            raise RuntimeError("a provider wait may not hold a SQLite transaction open")
        self.gate.release()
        try:
            return step()
        finally:
            self.gate.acquire()
            if self._stopping.is_set():
                raise _Stopped  # stopping overrides whatever the step did

    def _stream_outside_gate(
        self,
        invoke: Callable[[ProviderInvocationRequest], Any],
        request: ProviderInvocationRequest,
    ) -> Iterator[Mapping[str, Any]]:
        stream = self._outside_gate(lambda: iter(invoke(request)))
        while True:
            try:
                event = self._outside_gate(lambda: next(stream))
            except StopIteration:
                return
            yield event
