"""The ceilings one document extraction is really held to (DOC-16, 11.13, 17.1).

The process boundary in ``test_document_worker_isolation.py`` asserts what the
worker can *reach*. This file asserts what it can *spend*, and the distinction
that matters throughout is between a per-process ceiling and a per-job one.
17.1 says resident memory is "1 GiB per job, including children" and 11.13 that
"the worker supervisor counts child processes and cumulative unit time" and that
"restarting a child does not reset the document budget". A ``setrlimit`` in the
child cannot say any of that: each process in the tree counts its own, and a
child inherits the ceiling but starts from zero.

Four claims, every one driven against real process trees rather than against a
patched measurement, because a sampler asserted only against its own stubs is
exactly the accounting this slice was asked not to pretend to have:

* **Aggregate.** Four processes each comfortably inside the ceiling breach it
  together, and the supervisor reports that -- measured per process first, so
  the ceiling under test is provably one no single member reaches.
* **Cumulative.** A descendant that runs and is reaped between two samples --
  never once observed alive -- is still charged in full, and the charge climbs
  across every restart until one document budget is exhausted.
* **Closed.** A tree that cannot be measured is refused, with its own typed
  reason; a member that merely vanished mid-sample is not, because that is the
  ordinary shape of a process exiting.
* **Terminal.** A breach kills the whole session, reaps the root child, removes
  the attempt directory and arrives as a resource failure -- never as a
  timeout, a crash, a verdict on the document or a success.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import itertools
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from omnivia_core_runtime.service import document_worker
from omnivia_core_runtime.service.document_worker import (
    REASON_CPU_LIMIT,
    REASON_MEMORY_LIMIT,
    REASON_SUPERVISION,
    DocumentWorkerFailed,
    extract_document_isolated,
)

CAPTURE: Final = "dcap-" + "s" * 64

#: What each process in a test tree allocates. Large enough that the resident
#: size is dominated by the allocation rather than by interpreter startup, so
#: the members are comparable; small enough that four of them are nothing.
BLOCK: Final = 48 * 1024 * 1024

#: The accounting this build supervises with is Darwin's, and there is no
#: portable fallback by design: the complete product target is macOS, and a
#: host that cannot account a tree refuses to extract rather than extracting
#: unaccounted. That refusal is asserted in ``test_document_worker_isolation``.
needs_accounting = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="only macOS ships the process accounting this build supervises with",
)

#: One process that allocates, reports, and waits to be killed.
_HOLDING: Final = (
    "import sys, time\n"
    "held = bytearray(int(sys.argv[1]))\n"
    "print(len(held), flush=True)\n"
    "time.sleep(120)\n"
)

#: A session leader that allocates and starts ``count`` descendants that each
#: allocate the same. It prints their pids once every one of them has finished
#: allocating, so a sample taken afterwards measures a settled tree.
_ALLOCATING_TREE: Final = f"""
import subprocess, sys, time

block, count = sys.argv[1], int(sys.argv[2])
held = [
    subprocess.Popen(
        [sys.executable, "-P", "-s", "-B", "-c", {_HOLDING!r}, block],
        stdout=subprocess.PIPE,
    )
    for _ in range(count)
]
mine = bytearray(int(block))
for descendant in held:
    descendant.stdout.readline()
print(" ".join(str(descendant.pid) for descendant in held), flush=True)
time.sleep(120)
"""

#: One process that burns a measured amount of CPU and exits.
_BURNING: Final = (
    "import sys, time\n"
    "deadline = time.process_time() + float(sys.argv[1])\n"
    "while time.process_time() < deadline:\n"
    "    pass\n"
)

#: A session leader that runs one CPU-burning descendant per line it is given,
#: waits for it, and reports the pid of the process it has just reaped. The
#: handshake is what makes the central assertion possible: when the pid
#: arrives, that descendant is gone and the next one has not been started.
_BURNING_TREE: Final = f"""
import subprocess, sys, time

for _ in sys.stdin:
    burner = subprocess.Popen(
        [sys.executable, "-P", "-s", "-B", "-c", {_BURNING!r}, sys.argv[1]]
    )
    burner.wait()
    print(burner.pid, flush=True)
time.sleep(5)
"""

#: One process that burns a measured amount of CPU and then waits, so it is
#: still alive, and still chargeable, when the test comes to look at it.
_BURNING_THEN_WAITING: Final = (
    "import sys, time\n"
    "deadline = time.process_time() + float(sys.argv[1])\n"
    "while time.process_time() < deadline:\n"
    "    pass\n"
    "print('burned', flush=True)\n"
    "time.sleep(120)\n"
)

#: A middle process that starts one of those, waits for it to spend its CPU,
#: reports its pid and exits -- leaving it running with no parent of its own.
_ORPHANING_MIDDLE: Final = f"""
import subprocess, sys

burner = subprocess.Popen(
    [sys.executable, "-P", "-s", "-B", "-c", {_BURNING_THEN_WAITING!r}, sys.argv[1]],
    stdout=subprocess.PIPE,
)
burner.stdout.readline()
print(burner.pid, flush=True)
"""

#: A session leader that reaps that middle process, so the burner is reparented
#: away from the job's tree while staying in the job's process group.
_ORPHANING_TREE: Final = f"""
import subprocess, sys, time

middle = subprocess.Popen(
    [sys.executable, "-P", "-s", "-B", "-c", {_ORPHANING_MIDDLE!r}, sys.argv[1]],
    stdout=subprocess.PIPE,
)
orphan = middle.stdout.readline()
middle.wait()
sys.stdout.write(orphan.decode())
sys.stdout.flush()
time.sleep(120)
"""

#: A stand-in worker that allocates in itself *and* in a descendant, records
#: both pids outside its own scratch, and then waits. Neither process is near
#: the ceiling the test sets; together they are past it.
_ALLOCATING_WORKER: Final = f"""
import json, os, subprocess, sys, time

header = json.loads(sys.stdin.buffer.read())
block, marker = sys.argv[1], sys.argv[2]
descendant = subprocess.Popen(
    [sys.executable, "-P", "-s", "-B", "-c", {_HOLDING!r}, block],
    stdout=subprocess.PIPE,
)
descendant.stdout.readline()
mine = bytearray(int(block))
with open(marker, "w") as handle:
    handle.write("%d %d" % (os.getpid(), descendant.pid))
time.sleep(120)
"""


@contextlib.contextmanager
def _tree(program: str, *arguments: str) -> Iterator[subprocess.Popen[bytes]]:
    """One process tree in a session of its own, the way the worker gets one.

    Killed by session on the way out whatever the test did, because a leaked
    descendant of a failed assertion would hold its allocation for as long as
    the suite runs.
    """
    process = subprocess.Popen(
        [sys.executable, "-P", "-s", "-B", "-c", program, *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        yield process
    finally:
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
        process.kill()
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        process.wait(timeout=30)


def _supervisor(
    pid: int, *, memory_bytes: int = 1 << 62, cpu_seconds: float = 1e9
) -> document_worker._ProcessTreeSupervisor:
    """A supervisor whose unexercised ceiling cannot be the one that fires."""
    return document_worker._ProcessTreeSupervisor(
        pid, memory_bytes=memory_bytes, cpu_seconds=cpu_seconds
    )


def _resident(
    supervisor: document_worker._ProcessTreeSupervisor, members: tuple[int, ...]
) -> dict[int, int]:
    sizes: dict[int, int] = {}
    for pid in members:
        usage = supervisor._usage(pid)
        assert usage is not None, pid
        sizes[pid] = usage.ri_resident_size
    return sizes


def _line(process: subprocess.Popen[bytes]) -> str:
    assert process.stdout is not None
    return process.stdout.readline().decode().strip()


def _ask(process: subprocess.Popen[bytes]) -> None:
    assert process.stdin is not None
    process.stdin.write(b"go\n")
    process.stdin.flush()


def _wait_for_exit(pid: int, *, within: float = 15) -> None:
    """Block until ``pid`` is gone, or say what outlived the job.

    ``kill(pid, 0)`` answers for a zombie as readily as for a running process,
    so a :class:`ProcessLookupError` here means the entry has left the process
    table entirely -- the process was killed *and* reaped, which is the claim.
    """
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} outlived the job")


def _attempt_directories() -> set[str]:
    return {
        path.name
        for path in Path(tempfile.gettempdir()).glob("omnivia-doc-worker-*")
    }


# -- the Darwin accounting interface -----------------------------------------


@needs_accounting
def test_the_accounting_structure_is_the_one_the_installed_sdk_declares() -> None:
    """The layout is held against the header rather than against documentation.

    ``proc_pid_rusage`` copies out the whole flavour it is asked for, so a
    structure holding only the fields this module reads would be written past
    its end -- an overflow in the supervising process, caused by the very call
    meant to keep the job inside its ceilings. The fields are therefore
    declared in full and in order, and this is what keeps them that way when
    the SDK moves.
    """
    sdk = subprocess.run(
        ["/usr/bin/xcrun", "--show-sdk-path"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    header = Path(sdk.stdout.strip() or "/") / "usr/include/sys/resource.h"
    if sdk.returncode != 0 or not header.is_file():
        pytest.skip("no installed SDK header to hold the layout against")
    declared = re.search(
        r"struct rusage_info_v3\s*\{(.*?)\n\};", header.read_text(), re.DOTALL
    )
    assert declared is not None
    names = tuple(re.findall(r"uint64_t\s+(\w+);", declared.group(1)))
    assert names == document_worker._RUSAGE_V3_FIELDS
    assert ctypes.sizeof(document_worker._RusageInfoV3) == 16 + 8 * len(names)


@needs_accounting
def test_consumed_cpu_is_read_on_this_hosts_clock_scale_and_not_as_nanoseconds(
) -> None:
    """``<sys/resource.h>`` documents these fields in nanoseconds. They are not.

    They are Mach absolute units. On Intel the timebase is 1/1, so the two
    readings coincide and the documentation is never contradicted; on Apple
    silicon it is 125/3 and reading them as nanoseconds under-reports consumed
    CPU by a factor of twenty-four. A supervisor that made that mistake would
    hold a 120 second budget that cannot be reached inside a 120 second wall
    deadline -- an accounting control that silently never fires. The scale is
    therefore asked of the host, and this is the measurement that pins it.
    """
    burn = 0.5
    scale = document_worker._seconds_per_tick(document_worker._load_libproc())
    with _tree(_BURNING_TREE, str(burn)) as leader:
        supervisor = _supervisor(leader.pid)
        _ask(leader)
        _line(leader)
        supervisor.sample()
        charged = supervisor.consumed_seconds
    assert burn * 0.5 < charged < burn * 4, charged
    if scale * 1e9 != 1.0:
        # And the misreading really would land outside that band here, rather
        # than being a distinction this host cannot tell apart.
        assert charged / (scale * 1e9) < burn * 0.5


@needs_accounting
def test_a_full_member_buffer_is_read_as_a_truncation_and_not_as_the_tree() -> None:
    """``proc_listpgrppids`` answers with a count, and truncates in silence.

    Two things about that call are easy to get wrong and neither announces
    itself. It returns the *number of pids* and not a byte length, despite
    taking a buffer size; and when the group holds more processes than the
    buffer has room for it fills what it can, returns that, and reports no
    error at all. Read as bytes, a group of sixty-four would be accounted as
    sixteen processes; read without the truncation check, a large tree would be
    accounted as whichever members happened to fit. Both are a ceiling quietly
    applied to a fraction of the job.
    """
    expected = tuple(range(1000, 1100))

    class _Truncating:
        """A ``libproc`` stand-in that fills what it is given and says no more."""

        def __init__(self) -> None:
            self.rooms: list[int] = []

        def proc_listpgrppids(self, _pgid: int, buffer: Any, size: int) -> int:
            room = size // ctypes.sizeof(ctypes.c_int32)
            self.rooms.append(room)
            written = expected[:room]
            ctypes.memmove(
                buffer,
                (ctypes.c_int32 * len(written))(*written),
                len(written) * ctypes.sizeof(ctypes.c_int32),
            )
            return len(written)

    supervisor = _supervisor(os.getpid())
    truncating = _Truncating()
    supervisor._libproc = truncating  # type: ignore[assignment]
    assert supervisor._members() == expected
    # The first buffer came back full, so it was asked again with a larger one
    # rather than believed.
    assert truncating.rooms[0] < len(expected) <= truncating.rooms[-1]


@needs_accounting
def test_a_tree_larger_than_the_bound_is_refused_rather_than_part_accounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing bounds how many processes a parser can start. At the bound the
    # supervisor refuses to account the job at all, which fails the attempt
    # closed; it does not enforce a ceiling against the members that fitted.
    monkeypatch.setattr(document_worker, "MAX_TREE_PROCESSES", 2)
    with _tree(_ALLOCATING_TREE, "1024", "3") as leader:
        _line(leader)
        with pytest.raises(document_worker._SupervisionUnavailable):
            _supervisor(leader.pid).sample()


# -- aggregate resident memory -----------------------------------------------


@needs_accounting
def test_the_resident_ceiling_is_the_whole_tree_and_not_one_process() -> None:
    """17.1's 1 GiB is "per job, including children", and this is that claim.

    The ceiling is derived from the measurement rather than guessed, so the
    assertion is exactly the one that matters: *every* member is individually
    inside it, and the tree is past it anyway. A per-process ``RLIMIT`` -- the
    only kind the child itself can be given, and the only kind the reply's
    ``limits`` list ever claimed -- does not see this breach at all.
    """
    with _tree(_ALLOCATING_TREE, str(BLOCK), "3") as leader:
        _line(leader)
        measuring = _supervisor(leader.pid)
        members = measuring._members()
        # The leader and all three descendants, discovered rather than told.
        assert len(members) == 4, members
        resident = _resident(measuring, members)
        ceiling = 2 * max(resident.values())
        assert all(size < ceiling for size in resident.values()), resident
        assert sum(resident.values()) > ceiling, resident

        breach, detail = _supervisor(leader.pid, memory_bytes=ceiling).sample()
        assert breach == REASON_MEMORY_LIMIT
        assert "across 4 processes" in detail, detail


# -- cumulative CPU ----------------------------------------------------------


@needs_accounting
def test_cumulative_cpu_outlives_every_descendant_that_exits_and_restarts() -> None:
    """11.13: restarting a child does not reset the document budget.

    The strong form of that claim, and the reason this is not merely sampling.
    Each descendant here is started, burns its CPU, and is *reaped* before the
    supervisor is allowed to look -- so not one of them is ever observed alive,
    and nothing a sampler saw could account for them. They are charged in full
    regardless, because the process that reaped them carries their time in the
    kernel's own ``ri_child_*`` accounting, which the supervisor collects from
    every member. The charge climbs over every restart until one document
    budget is exhausted, and then the job is out of CPU.
    """
    burn = 0.3
    budget = 1.0
    with _tree(_BURNING_TREE, str(burn)) as leader:
        supervisor = _supervisor(leader.pid, cpu_seconds=budget)
        charged = [0.0]
        restarted: list[int] = []
        ever_seen: set[int] = set()
        breach = ""
        for _ in range(8):
            _ask(leader)
            restarted.append(int(_line(leader)))
            ever_seen.update(supervisor._members())
            breach, detail = supervisor.sample()
            charged.append(supervisor.consumed_seconds)
            if breach:
                break

    # Not one of the descendants was ever a member of the group the supervisor
    # enumerated: every one of them had already been reaped when it looked.
    assert len(restarted) >= 2
    assert not ever_seen.intersection(restarted), sorted(ever_seen)
    # The charge rose over every restart, and by about what each one burned.
    assert all(later > earlier for earlier, later in itertools.pairwise(charged))
    assert charged[-1] >= burn * len(restarted) * 0.7, charged
    # And it ran out, under its own reason, on the budget the job was given.
    assert breach == REASON_CPU_LIMIT
    assert f"of {budget}s" in detail, detail
    assert supervisor.consumed_seconds > budget


@needs_accounting
def test_cpu_already_charged_is_retained_when_a_descendant_goes_for_good() -> None:
    """The one case the kernel does not retain for us, and the high-water mark.

    Everywhere else a departed descendant's time is kept by the process that
    reaped it. A descendant whose own parent died first is reparented to
    ``launchd``, and when it exits *that* is what reaps it, so no member of the
    job's group carries its time afterwards and the measured sum genuinely
    falls. The job does not get that CPU back: the charge is a high-water mark.

    The fall is asserted directly rather than assumed, so this is a test of the
    mark and not of a drop that might not happen on this host.
    """
    burn = 0.4
    with _tree(_ORPHANING_TREE, str(burn)) as leader:
        orphan = int(_line(leader))
        supervisor = _supervisor(leader.pid)
        supervisor.sample()
        charged = supervisor.consumed_seconds
        assert charged > burn * 0.7, charged
        # Its parent is gone, yet it is still in scope: the process group is
        # what keeps a reparented descendant inside the job's accounting.
        assert orphan in supervisor._members()
        assert os.getpgid(orphan) == leader.pid

        os.kill(orphan, signal.SIGKILL)
        _wait_for_exit(orphan)

        raw = 0.0
        for pid in supervisor._members():
            usage = supervisor._usage(pid)
            if usage is None:
                continue
            raw += supervisor._tick * (
                usage.ri_user_time
                + usage.ri_system_time
                + usage.ri_child_user_time
                + usage.ri_child_system_time
            )
        # Nothing in the group accounts for it any more...
        assert raw < charged * 0.5, (raw, charged)
        # ... and the job is still charged for every second of it.
        assert supervisor.sample() == ("", "")
        assert supervisor.consumed_seconds >= charged


@needs_accounting
def test_a_tree_that_only_waits_exhausts_no_cpu_budget() -> None:
    # The complement, and what keeps the CPU ceiling from being a second wall
    # clock: a job that blocks on a slow disk or a large read spends no CPU and
    # is charged none. The wall deadline is what bounds that, separately.
    with _tree(_ALLOCATING_TREE, "1024", "2") as leader:
        _line(leader)
        supervisor = _supervisor(leader.pid, cpu_seconds=0.5)
        for _ in range(5):
            assert supervisor.sample() == ("", "")
            time.sleep(0.1)
        assert supervisor.consumed_seconds < 0.5


# -- refusing closed ---------------------------------------------------------


@needs_accounting
def test_a_member_that_cannot_be_read_refuses_the_job_rather_than_estimating(
) -> None:
    """A member of our own session that will not answer is a monitor failure.

    The distinction this draws is the one the whole control rests on. A process
    that has *gone* is the ordinary case: it exited between the enumeration and
    the read, and its time is already in the ``ri_child_*`` of whichever
    process reaped it, so the sample is complete without it. Any other refusal
    means a member of this job is running and unmeasured, and a ceiling applied
    to the rest of the tree would be a ceiling on nothing in particular.
    """

    class _Denying:
        """A ``libproc`` stand-in whose per-process accounting fails."""

        def __init__(self, real: Any, failure: int) -> None:
            self._real = real
            self._failure = failure

        def proc_listpgrppids(self, pgid: int, buffer: Any, size: int) -> int:
            return int(self._real.proc_listpgrppids(pgid, buffer, size))

        def proc_pid_rusage(self, _pid: int, _flavour: int, _buffer: Any) -> int:
            ctypes.set_errno(self._failure)
            return -1

    with _tree(_ALLOCATING_TREE, "1024", "1") as leader:
        _line(leader)
        denied = _supervisor(leader.pid)
        real = denied._libproc
        denied._libproc = _Denying(real, errno.EPERM)  # type: ignore[assignment]
        with pytest.raises(document_worker._SupervisionUnavailable):
            denied.sample()

        # Whereas a member that has simply been reaped since the enumeration is
        # not a failure at all, and leaves a sample that reports no breach.
        vanished = _supervisor(leader.pid, memory_bytes=1, cpu_seconds=0.0)
        vanished._libproc = _Denying(real, errno.ESRCH)  # type: ignore[assignment]
        assert vanished.sample() == ("", "")


@needs_accounting
def test_a_host_that_cannot_account_a_tree_extracts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The production half of failing closed. Supervision is established before
    # the child is handed its request, so a host without it refuses before a
    # single source byte has been read -- and refuses under its own reason,
    # which is neither a ceiling that was reached nor a verdict on the source.
    def unavailable() -> ctypes.CDLL:
        raise document_worker._SupervisionUnavailable("no accounting interface")

    monkeypatch.setattr(document_worker, "_load_libproc", unavailable)
    before = _attempt_directories()
    with pytest.raises(DocumentWorkerFailed) as failure:
        extract_document_isolated(
            b"the admitted bytes", media_type="text/plain", capture_id=CAPTURE
        )
    assert failure.value.reason == REASON_SUPERVISION
    # And the copy of the admitted bytes did not stay behind because of it.
    assert _attempt_directories() <= before


# -- a breach is terminal ----------------------------------------------------


@needs_accounting
def test_a_resource_breach_kills_the_tree_reaps_the_child_and_says_which(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole control, driven end to end through the production route.

    The stand-in worker breaches nothing on its own: it is the pair of it and
    its descendant that passes the ceiling. What has to come back is the
    aggregate memory reason -- not a timeout, because the deadline is untouched
    and nowhere near; not a crash, although the session is killed with a signal
    and the child's exit status says exactly that; and not an extraction,
    although a reply would have been perfectly readable had one been written.
    """
    marker = tmp_path / "pids"
    monkeypatch.setattr(document_worker, "WORKER_MEMORY_BYTES", 100 * 1024 * 1024)
    monkeypatch.setattr(
        document_worker,
        "_worker_argv",
        lambda: [
            sys.executable,
            "-P",
            "-s",
            "-B",
            "-c",
            _ALLOCATING_WORKER,
            str(BLOCK),
            str(marker),
        ],
    )
    before = _attempt_directories()
    started = time.monotonic()
    with pytest.raises(DocumentWorkerFailed) as failure:
        extract_document_isolated(
            b"the admitted bytes", media_type="text/plain", capture_id=CAPTURE
        )
    elapsed = time.monotonic() - started

    assert failure.value.reason == REASON_MEMORY_LIMIT
    assert "across 2 processes" in failure.value.detail, failure.value.detail
    # Nowhere near the wall deadline, which is still 120 seconds and was never
    # the thing that stopped this.
    assert elapsed < document_worker.EXTRACTION_SECONDS / 2, elapsed

    root, descendant = (int(pid) for pid in marker.read_text().split())
    # The root child is killed *and* reaped -- `kill(pid, 0)` would still
    # answer for a zombie -- and the descendant goes with the session rather
    # than surviving as somebody else's orphan.
    _wait_for_exit(root)
    _wait_for_exit(descendant)
    # And the copy of the admitted bytes is off the host.
    assert _attempt_directories() <= before


@needs_accounting
def test_a_breach_is_never_a_verdict_on_the_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The two refusals that mean something about the source are raised by the
    # adapters, and a resource failure is neither of them, so a call site's
    # `except` clause for either one cannot catch this. What the production
    # call sites record is the worker's own typed reason.
    from omnivia_core_runtime.service.document_extractors import (
        DocumentExtractionInvalid,
        DocumentExtractionUnsupported,
    )

    for reason in (REASON_MEMORY_LIMIT, REASON_CPU_LIMIT, REASON_SUPERVISION):
        raised = DocumentWorkerFailed(reason, "a ceiling, not a document")
        assert not isinstance(
            raised, (DocumentExtractionInvalid, DocumentExtractionUnsupported)
        )
        assert raised.reason == reason
    # Each one is its own reason rather than a single blurred resource outcome,
    # because what a job ran out of is what a caller has to record.
    assert len(
        {
            REASON_MEMORY_LIMIT,
            REASON_CPU_LIMIT,
            REASON_SUPERVISION,
            document_worker.REASON_RESOURCE_LIMIT,
            document_worker.REASON_TIMEOUT,
        }
    ) == 5
