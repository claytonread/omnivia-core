"""One document extraction in its own confined process (DOC-16, design 11.5).

The adapters in :mod:`document_extractors` are inert by construction, but inert
by construction is not a boundary. A hostile source still reaches lxml, PDFium,
Pillow, the OOXML readers and the pinned Pandoc and Tesseract binaries, and the
design is explicit that "parser settings alone are not the process boundary"
(11.5). This module *is* the boundary.

**What the child is handed.** A read-only copy of the admitted immutable bytes,
their media type, the capture id, the frozen extraction settings (including the
pinned engine/model identity) and one private writable scratch directory. It is
spawned with an environment built here from nothing -- the executable search
path included, which is a constant and not the service's own -- so it holds no
database handle, no credential, no native bookmark, no original path and no
inherited configuration. The reply is written into that same private directory,
under the one path the request named.

**What the child leaves behind is not trusted either.** For the length of the
parse a hostile document had write access to that directory, so the two things
the parent does with it afterwards -- read the reply, remove the tree -- follow
no link, block on nothing, and require what they open to be the kind of object
they expect. A tree that cannot be removed is reported as such; a successful
extraction whose copy of the admitted bytes is still on the host is not
returned as a success.

**The boundary fails closed.** The only confinement this build establishes is
the macOS seatbelt profile of :func:`confine_process`: deny-by-default, the
network denied outright, and the filesystem narrowed to the interpreter's own
import roots, the system and package directories the pinned readers load from,
the verified model directory and the private scratch. It is applied before a
single source byte is read. A host where it cannot be applied -- a macOS host
that refuses the profile, or any platform with no adapter at all -- does not
extract: the child returns :data:`STATUS_ISOLATION_REFUSED`, the parent raises
:class:`DocumentWorkerFailed` with :data:`REASON_ISOLATION`, and the caller
records that refusal. There is no unconfined production route, and nothing ever
extracts in the service process. The in-process adapters in
:mod:`document_extractors` remain importable for their own unit tests; they are
not reachable from a production call site.

**The protocol is bounded and deterministic.** One versioned JSON header on
stdin, the admitted bytes in a file beside the scratch, one versioned JSON reply
at the named path inside the scratch. Every field is validated on arrival, every
size has an explicit ceiling, the transported segments are re-hashed against the
digest the child reported, nothing is pickled and no shell is involved. A
malformed reply, a crash, a timeout, a resource-limit kill and a refused seal
are each their own typed failure: none can become a successful extraction, and
none is reported as an invalid source.

**The whole tree is accounted, not just the process that was spawned.** A
parser reaches Pandoc and Tesseract through processes of its own, so a ceiling
applied to the child alone bounds the cheap half of the work. 17.1 is explicit
that resident memory is "1 GiB per job, *including children*", and 11.13 that
the supervisor counts "cumulative unit time" and that "restarting a child does
not reset the document budget". :class:`_ProcessTreeSupervisor` is that
supervisor: the parent measures the child's whole session group -- orphans
included -- against the aggregate resident ceiling and against one cumulative
CPU budget per job, and a breach of either kills the session, reaps the child
and raises its own typed reason. It is a second, independent ceiling: the hard
wall deadline still bounds the attempt on its own.

The group is measured before the child is handed a source-bearing byte, and the
root's exit is read out of the root's own accounting rather than by reaping it.
Reaping is what frees the group id and what makes the root unreadable, so a
parent that reaps to learn the child has finished loses the last burst of CPU
the root spent and answers "finished" while the group may still hold live
descendants. Here the exit is observed while the root is still a zombie, which
is the one window in which its terminal usage is still chargeable and the group
id is still provably this job's.

**What is still not enforced here.** The resource ceilings applied *inside* the
child are only those the host actually binds, and they are named individually in
the reply; resident memory is not among them, because macOS does not enforce
``RLIMIT_AS``, and the aggregate ceiling above is what covers it instead. The
supervisor is macOS-only and refuses closed everywhere else, so no host both
extracts and goes unsupervised -- but the refusals are not interchangeable, and
a host missing either precondition says which one it was.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import hashlib
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, cast

from omnivia_core_runtime.service.document_extractors import (
    DocumentExtractionInvalid,
    DocumentExtractionUnsupported,
    _segments_digest,
    extract_document,
)
from omnivia_core_runtime.service.document_intake import (
    ExtractedSegment,
    ExtractionOutcome,
)
from omnivia_core_runtime.service.ocr_resources import (
    MODE_TEXT_AND_LOCAL_OCR,
    MODE_TEXT_ONLY,
    OCR_SECONDS_PER_DOCUMENT,
    ExtractionSettings,
    OcrResources,
)

#: The one wire protocol this build speaks. A header or reply naming anything
#: else is refused rather than read as a near-enough neighbour.
PROTOCOL_VERSION: Final = 1

#: Transport ceilings. The source bound sits above the 50 MiB per-file ceiling
#: of 17.1 so that a source the adapters would refuse is still refused *by them*,
#: with their own message, rather than by the transport. The reply bound is the
#: 128 MiB complete-structured-output ceiling of the same table.
MAX_SOURCE_BYTES: Final = 64 * 1024 * 1024
MAX_HEADER_BYTES: Final = 64 * 1024
MAX_REPLY_BYTES: Final = 128 * 1024 * 1024
#: Segments one reply may carry. The byte ceiling above is the binding bound;
#: this one exists so a reply that is small because its segments are empty
#: cannot still claim millions of them.
MAX_REPLY_SEGMENTS: Final = 2_000_000
#: Nesting one transported locator or structured value may reach: the 128-deep
#: structural bound of 17.1, applied to the reply.
MAX_REPLY_DEPTH: Final = 128
#: Characters of one reply's diagnostic detail. It never carries source content.
MAX_DETAIL_CHARS: Final = 200
#: The longest vocabulary word the protocol accepts. The OOXML media types are
#: the reason it is not 64.
MAX_NAME_CHARS: Final = 128

#: Wall-clock budgets from 17.1: 120 seconds of non-OCR extraction per source,
#: plus 1,800 seconds of OCR per document when the frozen profile may actually
#: run recognition.
EXTRACTION_SECONDS: Final = 120
OCR_EXTRACTION_SECONDS: Final = OCR_SECONDS_PER_DOCUMENT + EXTRACTION_SECONDS
#: The largest single file the child may write: the 2 GiB per-job worker
#: temporary storage of 17.1. It is a per-file bound and not an aggregate quota,
#: so it is reported as the limit it is.
MAX_SCRATCH_FILE_BYTES: Final = 2 * 1024 * 1024 * 1024
#: Entries the cleanup walk will visit before it gives up and says so. Nothing
#: bounds how many names a parser can create inside its own scratch, and an
#: unbounded walk over them is the one way cleanup could fail to terminate.
MAX_SCRATCH_ENTRIES: Final = 200_000

#: The resident-memory ceiling of 17.1: 1 GiB per job, *including children*. It
#: is enforced as the sum over the job's whole session group, so a tree of
#: processes each comfortably under it still breaches together.
WORKER_MEMORY_BYTES: Final = 1024 * 1024 * 1024
#: How often the live tree is measured.
#:
#: Resident memory is an instantaneous quantity and nothing but a sample can
#: read it, so this interval is the *resolution* of that ceiling and not a
#: kernel-hard instantaneous cap: a spike that rises and falls inside one
#: interval is never seen, and a tree that sits under the ceiling at every
#: sample is never refused however high it went in between. Cumulative CPU is
#: not sampled in that sense -- a process that lives and dies between two
#: samples is still charged in full, by the kernel, through the ``ri_child_*``
#: accounting below, and the root's own terminal burst is charged because its
#: exit is read from its accounting rather than by reaping it.
SUPERVISION_SECONDS: Final = 0.1
#: Processes one job's session group may hold before the supervisor refuses to
#: account it at all. Nothing bounds how many processes a parser can start, and
#: an unbounded enumeration is the one way a sample could fail to finish.
MAX_TREE_PROCESSES: Final = 4096
#: Passes one sample will make over the group before it refuses to account it.
#:
#: A member reaped between the enumeration and its own read leaves the pass
#: short by that member's whole charge whenever the process that reaped it was
#: read earlier in the same pass -- and the readings themselves do not say
#: which order it happened in. Such a pass is discarded and the group read
#: again: the reaped member is gone from the next enumeration and its time is
#: in its reaper's accounting, so one further pass is normally enough. A tree
#: that cannot be read coherently within this many is refused rather than
#: accounted short.
MAX_SAMPLE_ATTEMPTS: Final = 4

CONFINEMENT_SEATBELT: Final = "seatbelt"
CONFINEMENT_NONE: Final = "unconfined"
#: The confinement a reply must carry to be read as an extraction. There is one,
#: and a reply naming anything else -- ``unconfined`` included -- is refused
#: unless it is the isolation refusal itself.
REQUIRED_CONFINEMENT: Final = CONFINEMENT_SEATBELT

#: Reply statuses. The first three mirror what the in-process entry point
#: raises; the rest are the worker's own.
STATUS_EXTRACTED: Final = "extracted"
STATUS_UNSUPPORTED: Final = "unsupported"
STATUS_INVALID: Final = "invalid"
STATUS_ISOLATION_REFUSED: Final = "isolation_refused"
STATUS_WORKER_ERROR: Final = "worker_error"
STATUS_PROTOCOL_INVALID: Final = "protocol_invalid"
_STATUSES: Final = frozenset(
    {
        STATUS_EXTRACTED,
        STATUS_UNSUPPORTED,
        STATUS_INVALID,
        STATUS_ISOLATION_REFUSED,
        STATUS_WORKER_ERROR,
        STATUS_PROTOCOL_INVALID,
    }
)

#: Typed causes for a worker that produced no outcome. Each is a distinct
#: condition a caller can record truthfully; none of them means "the source is
#: invalid", which is an extraction result and not a worker failure.
REASON_SPAWN: Final = "worker_spawn_failed"
REASON_CRASHED: Final = "worker_crashed"
REASON_TIMEOUT: Final = "worker_timeout"
REASON_RESOURCE_LIMIT: Final = "worker_resource_limit"
#: The two aggregate ceilings, each named separately from the per-process
#: ceiling above and from each other. A caller records which resource the job
#: ran out of, and neither is a verdict on the document.
REASON_MEMORY_LIMIT: Final = "worker_memory_limit"
REASON_CPU_LIMIT: Final = "worker_cpu_limit"
#: The trusted monitor could not account the tree, so nothing was allowed to
#: run unaccounted. Distinct from a breach: no ceiling was reached.
REASON_SUPERVISION: Final = "worker_supervision_unavailable"
REASON_PROTOCOL: Final = "worker_protocol_invalid"
REASON_ISOLATION: Final = "worker_isolation_unavailable"
REASON_SOURCE_OVERSIZE: Final = "worker_source_oversize"
REASON_INTERNAL: Final = "worker_internal_error"
#: The extraction itself succeeded but the copy of the admitted bytes could not
#: be removed from the host. That is not an outcome this route returns.
REASON_SCRATCH_RESIDUE: Final = "worker_scratch_not_removed"
#: The root exited while the job's process group still held other processes. The
#: group is killed before the leader is reaped and the attempt fails under this
#: rather than returning the reply the residue outlived -- a reply written by a
#: job that was still running when it claimed to be finished is not an outcome
#: this route returns either.
REASON_TREE_RESIDUE: Final = "worker_tree_not_terminated"

_PRIVATE_DIR_MODE: Final = 0o700
_PRIVATE_FILE_MODE: Final = 0o600
_READ_ONLY_FILE_MODE: Final = 0o400

#: Whether the child can be given its own session, and so whether a timeout can
#: take the processes it started with it. Windows has no equivalent.
_OWN_SESSION: Final = os.name == "posix" and hasattr(os, "killpg")
#: How long a killed child is waited for before this gives up on reaping it.
_REAP_GRACE_SECONDS: Final = 5
#: ``O_NOFOLLOW`` where the platform has it. Windows has no symlink-following
#: open to refuse here, and 0 leaves the flag word unchanged there.
_NO_FOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
#: ``O_DIRECTORY``, so an open that is meant to reach a directory cannot reach
#: anything else, and ``O_NONBLOCK``, so an open that is meant to reach a file
#: cannot block on a FIFO or a device a parser left in a file's place.
_DIRECTORY_ONLY: Final = getattr(os, "O_DIRECTORY", 0)
_NON_BLOCKING: Final = getattr(os, "O_NONBLOCK", 0)

#: Signals that mean a resource ceiling stopped the child rather than the child
#: failing: the CPU limit's own signal and the file-size limit's own signal.
_RESOURCE_SIGNALS: Final = frozenset(
    number
    for number in (getattr(signal, "SIGXCPU", None), getattr(signal, "SIGXFSZ", None))
    if number is not None
)

#: System and package directories the interpreter, the pinned readers and their
#: native libraries load from. ``/`` itself is here because resolving any
#: absolute path reads the root directory, and dyld aborts the child without it.
_SYSTEM_READ_SUBPATHS: Final = (
    "/usr",
    "/System",
    "/Library",
    "/bin",
    "/sbin",
    "/opt",
    "/private/var/db",
)
#: The only directories the child may find an external engine in.
#:
#: It is a constant and not the service's ``PATH``. An inherited search path
#: lets whoever can set one variable on the service decide which binary the
#: RTF reader and the OCR engine actually are, and 11.5 is explicit that the
#: worker receives no arbitrary configuration paths. Nothing here widens the
#: confinement: ``/usr`` and ``/opt`` are already readable, because the
#: interpreter's own libraries load from under them.
#:
#: **This build packages neither Pandoc nor Tesseract**, so naming these
#: directories pins no verified binary -- it fixes the search scope, and the
#: engines' identity rests on the integrity of these directories rather than on
#: a packaged artefact this repository ships. A host where they hold neither
#: binary reads no RTF and runs no recognition, and the adapters report that
#: themselves. The gap is recorded in the DOC-16 ledger entry rather than
#: papered over here.
_TRUSTED_PATH_DIRS: Final = (
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
)

_SYSTEM_READ_LITERALS: Final = (
    "/",
    "/dev/urandom",
    "/dev/random",
    "/dev/null",
    "/dev/zero",
)

#: Exact key sets. A header or reply carrying an unknown key came from something
#: that is not this protocol, and is refused rather than partially understood.
_HEADER_KEYS: Final = frozenset(
    {
        "protocol",
        "mediaType",
        "captureId",
        "settings",
        "scratchRoot",
        "workDir",
        "sourcePath",
        "sourceLength",
        "sourceDigest",
        "replyPath",
        "wallSeconds",
    }
)
#: Every reply carries these four, and then exactly what its status calls for:
#: an outcome where there is one, a detail where there is a reason, and neither
#: anywhere else. A reply that is nearly the right shape for its status came
#: from something that is not this protocol.
_REPLY_KEYS: Final = frozenset({"protocol", "confinement", "limits", "status"})
_REPLY_STATUS_KEYS: Final = {
    STATUS_EXTRACTED: frozenset({"outcome"}),
    STATUS_UNSUPPORTED: frozenset(),
    STATUS_INVALID: frozenset({"detail"}),
    STATUS_ISOLATION_REFUSED: frozenset({"detail"}),
    STATUS_WORKER_ERROR: frozenset({"detail"}),
    STATUS_PROTOCOL_INVALID: frozenset({"detail"}),
}
#: The statuses that say something about the document. Those three are the ones
#: a sealed process alone may claim; the three failure statuses are each already
#: a terminal worker failure whatever the child managed to seal, and demanding
#: the seal of them would only replace an accurate reason with a vaguer one.
_DOCUMENT_STATUSES: Final = frozenset(
    {STATUS_EXTRACTED, STATUS_UNSUPPORTED, STATUS_INVALID}
)
_CONFINEMENTS: Final = frozenset({CONFINEMENT_SEATBELT, CONFINEMENT_NONE})
_OUTCOME_KEYS: Final = frozenset(
    {"state", "outputDigest", "coverage", "limitations", "workerBuild", "segments"}
)
_SETTINGS_KEYS: Final = frozenset({"processingMode", "embeddedImageOcr", "ocr"})
_OCR_KEYS: Final = frozenset(
    {
        "engine",
        "engineRevision",
        "languages",
        "modelHashes",
        "tessdataDir",
        "available",
        "reason",
        "unavailableModels",
        "resourceDetail",
    }
)
_SEGMENT_KEYS: Final = frozenset(
    {
        "semanticRole",
        "sourceLocator",
        "text",
        "structuredValue",
        "extractionMethod",
        "visibility",
        "revisionState",
        "language",
        "confidence",
    }
)

_DIGEST_PREFIX: Final = "sha256:"
_DIGEST_LENGTH: Final = len(_DIGEST_PREFIX) + 64
_HEX: Final = frozenset("0123456789abcdef")


class DocumentWorkerFailed(RuntimeError):
    """The isolated worker produced no outcome; ``reason`` is the typed cause.

    Never raised for a source the adapters refused: an unsupported media type
    and an undecodable source still arrive as
    :class:`DocumentExtractionUnsupported` and
    :class:`DocumentExtractionInvalid` through the boundary, exactly as the
    in-process entry point raises them.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class IsolationUnavailable(RuntimeError):
    """The platform's confinement adapter could not seal this process."""


class _SupervisionUnavailable(RuntimeError):
    """The trusted monitor could not establish safe accounting for the tree.

    Raised rather than returning an unaccounted measurement, which is the one
    answer a supervisor must never give: a tree that cannot be measured is a
    tree with no ceiling, and the attempt is refused instead.
    """


class _ProtocolError(ValueError):
    """A header or reply left the bounded protocol this module speaks."""


# ---------------------------------------------------------------------------
# Parent: lay out the attempt, supervise the child, validate the reply
# ---------------------------------------------------------------------------


def extract_document_isolated(
    content: bytes,
    *,
    media_type: str,
    capture_id: str,
    settings: ExtractionSettings | None = None,
) -> ExtractionOutcome:
    """Extract one admitted source in a confined child and return its outcome.

    The signature, the return value and the two refusals are exactly those of
    :func:`document_extractors.extract_document`, so this is a drop-in
    production route for it. Everything else about the call is different: the
    parsers run in another process, sealed by :func:`confine_process`, over a
    read-only copy of the bytes in a private directory that is removed before
    this returns.

    Raises :class:`DocumentWorkerFailed` when the worker produced no outcome at
    all -- a host where the seal cannot be established included. A caller records
    that as its own terminal condition; it says nothing about the source and must
    never be settled as a success.
    """
    if not isinstance(content, bytes):
        raise DocumentWorkerFailed(REASON_PROTOCOL, "the admitted source is not bytes")
    if len(content) > MAX_SOURCE_BYTES:
        raise DocumentWorkerFailed(REASON_SOURCE_OVERSIZE, f"{len(content)} bytes")
    scratch = Path(tempfile.mkdtemp(prefix="omnivia-doc-worker-"))
    try:
        outcome = _extract_in(
            scratch,
            content,
            media_type=media_type,
            capture_id=capture_id,
            settings=settings,
        )
    except BaseException:
        # The attempt already has its own truthful cause and that is the one the
        # caller records. Cleanup still runs, and whatever it could not remove
        # is not allowed to replace that cause with a different one.
        _remove_scratch(scratch)
        raise
    residue = _remove_scratch(scratch)
    if residue:
        # The extraction worked, but a copy of the admitted bytes is still on
        # the host. Returning the outcome would report that as a clean success.
        raise DocumentWorkerFailed(REASON_SCRATCH_RESIDUE, residue)
    return outcome


def _extract_in(
    scratch: Path,
    content: bytes,
    *,
    media_type: str,
    capture_id: str,
    settings: ExtractionSettings | None,
) -> ExtractionOutcome:
    """Lay out one attempt's private directory, run the child, read the reply."""
    work = scratch / "work"
    work.mkdir(mode=_PRIVATE_DIR_MODE)
    source_path = scratch / "source.bin"
    # The admitted bytes land once, read-only. The child's write scope is `work`
    # alone, so nothing it runs can rewrite the source it was handed.
    with open(source_path, "wb", opener=_private_opener) as handle:
        handle.write(content)
    os.chmod(source_path, _READ_ONLY_FILE_MODE)
    wall = (
        OCR_EXTRACTION_SECONDS
        if settings is not None and settings.runs_ocr
        else EXTRACTION_SECONDS
    )
    reply_path = work / "reply.json"
    header = {
        "protocol": PROTOCOL_VERSION,
        "mediaType": media_type,
        "captureId": capture_id,
        "settings": _encode_settings(settings),
        "scratchRoot": str(scratch),
        "workDir": str(work),
        "sourcePath": str(source_path),
        "sourceLength": len(content),
        "sourceDigest": _DIGEST_PREFIX + hashlib.sha256(content).hexdigest(),
        "replyPath": str(reply_path),
        "wallSeconds": wall,
    }
    returncode = _run_child(header, work=work, wall=wall)
    if returncode != 0:
        # Whatever is in the reply file was written by a process that did not
        # finish, so it is never read as an extraction.
        raise DocumentWorkerFailed(*_exit_failure(returncode))
    return _outcome_from_reply(_read_reply(reply_path))


def _remove_scratch(scratch: Path) -> str:
    """Remove the attempt directory, and say so when anything is left behind.

    Everything here treats the tree as something a hostile document planted,
    because for the duration of the parse that is exactly what could have
    happened: the child held write access to ``work``.

    A directory the child created without write permission of its own would
    stop the removal, so each directory's mode is restored before the walk
    descends into it -- but through a descriptor opened with ``O_NOFOLLOW``,
    never by path. ``os.walk`` lists a symlink to a directory among its
    directory names, and ``os.chmod`` on that name follows it, so a child that
    dropped ``work/escape -> /some/host/directory`` would otherwise have *that*
    directory's mode changed here, outside the scratch entirely.

    :func:`shutil.rmtree` then removes the tree without ``ignore_errors``. It
    is itself link-safe where the platform supports directory descriptors, and
    a failure it reports is a fact the caller has to be told rather than one to
    swallow. Returns the empty string when the directory is gone, and a bounded
    description of what stopped the removal otherwise.
    """
    try:
        if not stat.S_ISDIR(os.lstat(scratch).st_mode):
            # `os.walk` resolves the directory it is given, so a link standing
            # where the attempt directory should be would have the walk --
            # and every mode it restores -- land in the directory it points at.
            return "the attempt directory is not a directory"
        if not _open_directories(scratch):
            return f"the attempt directory holds over {MAX_SCRATCH_ENTRIES} entries"
        shutil.rmtree(scratch)
    except (OSError, RecursionError) as error:
        return f"the attempt directory was not removed: {type(error).__name__}"
    if os.path.lexists(scratch):
        return "the attempt directory was not removed"
    return ""


def _open_directories(scratch: Path) -> bool:
    """Make every real directory under ``scratch`` removable, following no link.

    Bounded by :data:`MAX_SCRATCH_ENTRIES`, because nothing bounds how many
    names a parser can create inside its own scratch and an unbounded walk over
    them is the one way this could fail to finish. Returns ``False`` at the
    bound, and the caller reports a residue rather than claiming the bytes are
    gone.
    """
    visited = 0
    for parent, directories, files in os.walk(scratch):
        visited += len(directories) + len(files)
        if visited > MAX_SCRATCH_ENTRIES:
            return False
        for directory in directories:
            _make_removable(os.path.join(parent, directory))
    return True


def _make_removable(path: str) -> None:
    """Restore one real directory's mode through its own descriptor.

    A symlink, a vanished name and anything that is not a directory all fail the
    open and are left alone: none of them is a directory the walk will descend
    into, and ``rmtree`` unlinks each of them without following it.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | _NO_FOLLOW | _DIRECTORY_ONLY)
    except OSError:
        return
    try:
        # `AttributeError` is Windows, which has no `fchmod`. It also has no
        # directory open to get here through, so this is belt and braces.
        os.fchmod(descriptor, _PRIVATE_DIR_MODE)
    except (OSError, AttributeError):
        pass
    finally:
        os.close(descriptor)


def _private_opener(path: str, flags: int) -> int:
    return os.open(path, flags, _PRIVATE_FILE_MODE)


def _worker_argv() -> list[str]:
    """The child command: this interpreter, this module, no shell, no path search.

    ``-P`` keeps the working directory off ``sys.path``, so nothing a parser
    writes into the scratch can be imported afterwards; ``-s`` and ``-B`` keep
    the user site directory and bytecode writing out of it.
    """
    if not sys.executable:
        raise DocumentWorkerFailed(REASON_SPAWN, "this build has no interpreter path")
    return [
        sys.executable,
        "-P",
        "-s",
        "-B",
        "-m",
        "omnivia_core_runtime.service.document_worker",
    ]


def _child_environment(work: Path) -> dict[str, str]:
    """The child's whole environment, built here rather than inherited.

    Nothing is read out of :data:`os.environ`. ``PATH`` is the fixed
    :data:`_TRUSTED_PATH_DIRS` and ``PYTHONPATH`` the import roots this
    interpreter itself resolved; everything else the service process holds --
    tokens, workspace paths, provider configuration, an inherited search path --
    is simply absent, and ``HOME`` and ``TMPDIR`` point inside the private
    scratch so a library that insists on one writes there.
    """
    return {
        "PATH": os.pathsep.join(_TRUSTED_PATH_DIRS),
        "PYTHONPATH": os.pathsep.join(str(root) for root in _import_roots()),
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "HOME": str(work),
        "TMPDIR": str(work),
    }


def _run_child(header: Mapping[str, Any], *, work: Path, wall: int) -> int:
    """Run one child to completion under one deadline and return its status.

    ``wall`` bounds the whole attempt and is fixed before the child starts, so
    neither the request write nor anything the child spawns extends it. On
    expiry the child's entire session is killed -- it was given its own, so a
    Pandoc or Tesseract process it started goes with it -- and then reaped under
    a bounded grace, so this never blocks on a tree that will not die.

    The same session is what the supervisor accounts, and it is established
    before the child is handed the request: a host where the tree cannot be
    measured refuses the attempt before a single source byte has been read,
    exactly as a host where the process cannot be sealed does.

    A child that exits on its own is accounted and checked for residue *before*
    it is reaped, and its session is deliberately never signalled afterwards:
    the process group number is free once the leader is reaped, and signalling a
    freed group could reach something unrelated. A descendant still in the group
    when the root exits is therefore killed in the one window where that is
    still safe, and the attempt fails under :data:`REASON_TREE_RESIDUE` rather
    than returning a reply the job's own residue outlived.
    """
    request = json.dumps(header, allow_nan=False).encode()
    if len(request) > MAX_HEADER_BYTES:
        raise DocumentWorkerFailed(REASON_PROTOCOL, "the request header is oversize")
    argv = _worker_argv()
    try:
        # A fixed argv, no shell, and nothing in it derived from the document.
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            # Nothing the child or its native libraries print is read: a parser
            # diagnostic can quote document bytes, and this process never
            # carries those into a log.
            stderr=subprocess.DEVNULL,
            cwd=work,
            env=_child_environment(work),
            # Its own session, so a timeout can take the whole tree with it.
            # Windows has no equivalent and the timeout kills the child alone.
            start_new_session=_OWN_SESSION,
        )
    except (OSError, ValueError) as error:
        raise DocumentWorkerFailed(REASON_SPAWN, type(error).__name__) from error
    try:
        supervisor = _ProcessTreeSupervisor(
            process.pid, memory_bytes=WORKER_MEMORY_BYTES, cpu_seconds=wall
        )
    except _SupervisionUnavailable as refusal:
        # The child exists but is still blocked on a request it has not been
        # given, so it has opened nothing. A host that cannot be supervised
        # does not extract.
        _end_session(process)
        raise DocumentWorkerFailed(
            REASON_SUPERVISION, str(refusal)[:MAX_DETAIL_CHARS]
        ) from refusal
    try:
        return _supervise(process, supervisor, request=request, wall=wall)
    except OSError as error:
        # The request could not be handed over at all. The child is killed the
        # same way, because it may already have spawned something.
        _end_session(process)
        raise DocumentWorkerFailed(REASON_SPAWN, type(error).__name__) from error


def _supervise(
    process: subprocess.Popen[bytes],
    supervisor: _ProcessTreeSupervisor,
    *,
    request: bytes,
    wall: int,
) -> int:
    """Hand over the request, account the tree, and return the child's status.

    One thread does all three, and that is what makes the accounting safe
    rather than merely cheap. A sample asks the kernel about a process group
    that exists only while its leader is unreaped, and this loop is the only
    thing that ever reaps the leader. A monitor thread running beside it could
    therefore sample -- and signal -- a group id in the window after the reap,
    by which time the kernel may have given it to somebody else.

    **The tree is measured before the request is handed over.** The loop
    accounts first and offers second, so the job's initial process group has
    been enumerated and read in full before a single source-bearing byte
    reaches the child. Resolving the monitor's symbols in the supervisor's
    constructor establishes only that a monitor exists on this host; it does
    not establish that *this group* can be measured, and that is the
    precondition the route claims.

    **The root is not reaped until it has been accounted.**
    :meth:`subprocess.Popen.poll` is deliberately absent from this loop: it
    reaps, a reaped root is unreadable, and its charge then lives in this
    process's own ``ri_child_*`` where no member of the group carries it. So
    polling for the exit would discard exactly the CPU the root spent since the
    last sample, and would answer "finished" while the group may still hold
    live descendants. :func:`_account` reads the exit out of the root's own
    accounting instead.

    The deadline is absolute and taken before the first sample, so neither the
    handover nor a slow sample extends it. It still bounds the attempt on its
    own: the cumulative CPU budget is a second, independent ceiling, and a tree
    that sleeps for two minutes breaches neither.
    """
    deadline = time.monotonic() + wall
    if process.stdin is not None:
        # The write end alone; the child's inherited read end is a different
        # open file description and is unaffected.
        os.set_blocking(process.stdin.fileno(), False)
    pending: bytes | None = request
    while True:
        status = _account(process, supervisor)
        if status is not None:
            return status
        if pending is not None:
            pending = _offer(process, pending)
        if time.monotonic() >= deadline:
            _end_session(process)
            raise DocumentWorkerFailed(REASON_TIMEOUT, f"{wall}s")
        time.sleep(SUPERVISION_SECONDS)


def _account(
    process: subprocess.Popen[bytes], supervisor: _ProcessTreeSupervisor
) -> int | None:
    """Account the tree once; return the root's status only once the job is over.

    ``None`` means the job is still running. Everything terminal is raised from
    here, and the order it is decided in is the correctness of the whole
    control:

    * a breached ceiling is enforced first, exited or not, because the sample
      that observes the exit is also the first sample to charge the root's
      terminal usage -- a job that spent its last CPU on the way out is over
      budget, and a reply it managed to write is not a success;
    * a root that has exited is accepted as complete only when nothing else is
      left in the job's process group;
    * a group that still holds descendants is killed *before* the leader is
      reaped, which is the only moment at which the group id is still provably
      this job's, and the attempt then fails under its own typed reason;
    * and only then is the root reaped and its status returned.
    """
    try:
        breach, detail = supervisor.sample()
    except _SupervisionUnavailable as refusal:
        _end_session(process)
        raise DocumentWorkerFailed(
            REASON_SUPERVISION, str(refusal)[:MAX_DETAIL_CHARS]
        ) from refusal
    if breach:
        _end_session(process)
        raise DocumentWorkerFailed(breach, detail)
    if not supervisor.root_exited:
        return None
    # The group as the accepted sample read it, which is the enumeration that
    # also found the root exited: the leader is a zombie, so it cannot fork, and
    # a group whose only member is the leader has nothing left that could.
    survivors = tuple(pid for pid in supervisor.members if pid != process.pid)
    if survivors:
        _end_session(process)
        raise DocumentWorkerFailed(
            REASON_TREE_RESIDUE,
            f"{len(survivors)} processes in the job's group outlived the worker",
        )
    try:
        # This is the reap, and it cannot wait: the kernel has already reported
        # the root exited, and this process is its only possible reaper.
        return process.wait(timeout=_REAP_GRACE_SECONDS)
    except subprocess.TimeoutExpired as error:  # pragma: no cover - see above
        _end_session(process)
        raise DocumentWorkerFailed(
            REASON_INTERNAL, "the exited worker could not be reaped"
        ) from error


def _offer(process: subprocess.Popen[bytes], pending: bytes) -> bytes | None:
    """Hand over what the request pipe will take without blocking.

    Returns what is left of the request, or ``None`` once all of it is across
    and the pipe is closed. The write cannot be allowed to block: the header's
    ceiling is larger than a pipe buffer, so a child that stopped reading would
    hold this thread for as long as it liked -- past the deadline meant to bound
    the attempt, with no sample taken and no kill issued. That is the same
    hazard the reply's own non-blocking open refuses, one step earlier.
    """
    stdin = process.stdin
    assert stdin is not None
    if pending:
        try:
            pending = pending[os.write(stdin.fileno(), pending) :]
        except BlockingIOError:
            return pending
        except BrokenPipeError:
            # The child is already gone, which is its own answer: `poll` reports
            # the status, and a child that read no request writes no reply.
            pending = b""
    if pending:
        return pending
    try:
        stdin.close()
    except OSError:
        pass
    return None


def _end_session(process: subprocess.Popen[bytes]) -> None:
    """Kill an unreaped child's whole session and reap the child itself.

    Only ever called while ``process`` is unreaped, so its process group still
    exists and cannot have been reused -- and that is now checked rather than
    only documented, because a ``killpg`` of a leader that has already been
    reaped is a signal aimed at whatever the kernel has since given that number
    to. Everything else here is best-effort by nature: a group that has already
    gone is the ordinary case, and a platform without process groups falls back
    to the child alone.

    The killing still comes before the request pipe is closed, and the close
    still cannot block this thread in the one place it is trying to stop
    waiting -- but the reason is now the pipe rather than the order. The
    request is handed over through :func:`_offer`, which writes to the
    descriptor directly and never leaves a byte in the stream's own buffer, and
    writes to it only after it has been made non-blocking. There is therefore
    nothing left for this close to flush, and nothing it could wait on if there
    were.
    """
    if process.returncode is not None:
        # Already reaped, so the leader's pid -- and with it the group id -- is
        # free for the kernel to hand to anyone. There is nothing left of this
        # job to signal, and signalling anyway could reach a stranger.
        return
    if _OWN_SESSION:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass
    if process.stdin is not None and not process.stdin.closed:
        try:
            process.stdin.close()
        except OSError:
            pass
    try:
        # A SIGKILLed direct child is reaped at once; the grace exists so that a
        # host which somehow does not deliver it cannot wedge this thread.
        process.wait(timeout=_REAP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _exit_failure(returncode: int) -> tuple[str, str]:
    """The typed cause behind a non-zero child exit."""
    if returncode < 0 and -returncode in _RESOURCE_SIGNALS:
        return REASON_RESOURCE_LIMIT, f"signal {-returncode}"
    if returncode < 0:
        return REASON_CRASHED, f"signal {-returncode}"
    return REASON_CRASHED, f"exit {returncode}"


# ---------------------------------------------------------------------------
# The trusted supervisor: aggregate resident memory, cumulative tree CPU
# ---------------------------------------------------------------------------

#: ``RUSAGE_INFO_V3`` from ``<sys/resource.h>``. It is the earliest flavour
#: carrying the ``ri_child_*`` fields, which are what makes the CPU accounting
#: below exact rather than sampled.
RUSAGE_INFO_V3: Final = 3

#: ``struct rusage_info_v3``, in the order ``<sys/resource.h>`` declares it.
#:
#: Every field is declared although four are read, because ``proc_pid_rusage``
#: copies out the whole flavour it was asked for: a structure holding only the
#: interesting prefix would be written past its end. The layout is held against
#: the installed SDK header by a test rather than trusted from documentation.
_RUSAGE_V3_FIELDS: Final = (
    "ri_user_time",
    "ri_system_time",
    "ri_pkg_idle_wkups",
    "ri_interrupt_wkups",
    "ri_pageins",
    "ri_wired_size",
    "ri_resident_size",
    "ri_phys_footprint",
    "ri_proc_start_abstime",
    "ri_proc_exit_abstime",
    "ri_child_user_time",
    "ri_child_system_time",
    "ri_child_pkg_idle_wkups",
    "ri_child_interrupt_wkups",
    "ri_child_pageins",
    "ri_child_elapsed_abstime",
    "ri_diskio_bytesread",
    "ri_diskio_byteswritten",
    "ri_cpu_time_qos_default",
    "ri_cpu_time_qos_maintenance",
    "ri_cpu_time_qos_background",
    "ri_cpu_time_qos_utility",
    "ri_cpu_time_qos_legacy",
    "ri_cpu_time_qos_user_initiated",
    "ri_cpu_time_qos_user_interactive",
    "ri_billed_system_time",
    "ri_serviced_system_time",
)


class _RusageInfoV3(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        *((name, ctypes.c_uint64) for name in _RUSAGE_V3_FIELDS),
    ]


class _MachTimebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


def _load_libproc() -> ctypes.CDLL:
    """The Darwin process-accounting interface, or a refusal to supervise.

    ``libproc`` is what an unprivileged process is given on macOS to read
    another process's resident size and consumed CPU, and it is the whole basis
    of the accounting here. There is no portable fallback: reading these numbers
    out of ``/proc`` would be a different implementation of a different
    platform's interface, and a sampler that silently measured less would be
    worse than none at all. So this refuses, the attempt refuses with it, and
    the complete product target stays what it says it is.
    """
    if sys.platform != "darwin":
        raise _SupervisionUnavailable(
            f"no process-tree monitor for {sys.platform} in this build"
        )
    name = ctypes.util.find_library("System") or "libSystem.dylib"
    try:
        library = ctypes.CDLL(name, use_errno=True)
        # Resolved here, by name, so a host missing any of the three refuses
        # now rather than at the first sample.
        library.proc_listpgrppids.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        library.proc_listpgrppids.restype = ctypes.c_int
        library.proc_pid_rusage.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        library.proc_pid_rusage.restype = ctypes.c_int
        library.mach_timebase_info.argtypes = [ctypes.POINTER(_MachTimebase)]
        library.mach_timebase_info.restype = ctypes.c_int
    except (OSError, AttributeError) as error:
        raise _SupervisionUnavailable(
            "this host offers no process accounting interface"
        ) from error
    return library


def _seconds_per_tick(library: ctypes.CDLL) -> float:
    """Seconds in one unit of the times ``proc_pid_rusage`` reports.

    ``<sys/resource.h>`` documents ``ri_user_time`` and its neighbours as
    nanoseconds, and on Intel they are -- the Mach timebase is 1/1 there, so the
    two readings coincide and the documentation is never contradicted. On Apple
    silicon the timebase is 125/3: the fields are Mach absolute units, and read
    as nanoseconds they under-report consumed CPU by a factor of twenty-four,
    which would put the 1,800 second ceiling of 17.1 out of reach. The scale is
    therefore asked for rather than assumed, and it was measured against
    :func:`resource.getrusage` over a known burn before it was relied on.
    """
    timebase = _MachTimebase()
    if library.mach_timebase_info(ctypes.byref(timebase)) != 0 or not timebase.denom:
        raise _SupervisionUnavailable("the host clock scale is unavailable")
    return int(timebase.numer) / int(timebase.denom) / 1e9


class _ProcessTreeSupervisor:
    """Accounts one job's whole process tree against the ceilings of 17.1.

    **The unit is the session group, not the parent links.** The child is
    spawned into a session of its own, so every process the job starts carries
    its process group id. Enumerating that group rather than walking children
    from the root is both more complete and more sound, and both halves were
    measured on this host. Complete: a descendant whose own parent dies is
    reparented to ``launchd`` but *keeps its process group*, so a walk of the
    parent links loses it while this still finds it. Sound: joining a process
    group requires being in the same session, so nothing outside this job can
    appear, and the supervisor keeps no per-pid state for a reused pid to be
    mistaken for -- the group membership is the identity.

    **Memory is the sum across the group.** 17.1 says 1 GiB per job *including
    children*, so a per-process ceiling would not be it: four processes at
    300 MiB each breach a ceiling none of them reaches. Shared pages are
    counted once per process that maps them, which over-states the total by a
    few megabytes of interpreter and library text and so errs towards refusing;
    against a 1 GiB ceiling that is noise.

    **CPU is cumulative, and short-lived descendants do not escape it.** A
    sample sums, over every member, that member's own consumed time *and* the
    time of every descendant it has already reaped -- the kernel's own
    ``ri_child_*`` accounting. So a Tesseract pass that runs and exits entirely
    between two samples is still charged in full, because the process that
    reaped it carries its time, and the charge therefore does not depend on
    having observed the process alive. The time has to be collected from every
    member rather than from the root alone: a grandchild reaped by a *live*
    intermediate process is parked in that process's ``ri_child_*`` and does not
    reach the root's until the intermediate one is itself reaped. Measured.

    Nothing is counted twice. A member is either live, and contributes its own
    time, or reaped, and contributes through exactly one ancestor's
    ``ri_child_*``; a zombie is still readable and so is still counted as
    itself. The charge is additionally held to a high-water mark, because a job
    does not earn CPU back by letting the process that spent it disappear.

    **Nothing is counted zero either, which is the harder half.** A member
    reaped in the middle of a pass -- after the enumeration, before its own
    read -- has just moved its whole charge into the ``ri_child_*`` of its
    reaper, and if that reaper was read earlier in the same pass the reading it
    gave does not carry it. Taking the gone member for zero would then erase
    its entire consumed time from the sample, and no reading in the pass says
    that happened. So a pass that saw a member vanish is discarded and the
    group read again, up to :data:`MAX_SAMPLE_ATTEMPTS` times; see
    :meth:`_read_tree`.

    **The root's exit is read, not reaped for.** The root is the one member
    whose reaper is the supervising process rather than a member of the group,
    so reaping it moves its charge somewhere the group can never be asked
    about. Its exit is therefore taken from ``ri_proc_exit_abstime``, which is
    zero while it lives and non-zero once it has exited and not yet been
    reaped. That reading is stable rather than racy precisely because the
    supervising process is the root's parent: nothing else can reap it, so the
    zombie -- and its terminal usage, and the group id it still holds open --
    stays there until this code chooses to collect it.

    **The one residual.** A descendant orphaned to ``launchd`` and reaped there
    takes its unsampled tail with it: no member of the group carries it. That
    needs an intermediate process to die while its own child runs, which the
    pinned readers do not do -- they wait on what they start -- and which a
    hostile document cannot arrange, since it supplies data and not process
    topology. The high-water mark means whatever was already charged stays
    charged. Recorded in the DOC-16 ledger entry rather than papered over.
    """

    def __init__(
        self, root_pid: int, *, memory_bytes: int, cpu_seconds: float
    ) -> None:
        self._root = root_pid
        self._memory_bytes = memory_bytes
        self._cpu_seconds = cpu_seconds
        self._libproc = _load_libproc()
        self._tick = _seconds_per_tick(self._libproc)
        #: The job's charge so far, in seconds. Never allowed to fall.
        self.consumed_seconds = 0.0
        #: The group as the last accepted sample read it, and whether that
        #: sample found the root exited and not yet reaped. These are what the
        #: caller decides completion on, and neither carries a value until a
        #: sample has actually succeeded.
        self.members: tuple[int, ...] = ()
        self.root_exited = False

    def sample(self) -> tuple[str, str]:
        """Account the live tree once: a breach reason and detail, or two blanks.

        Raises :class:`_SupervisionUnavailable` rather than returning a
        measurement it cannot stand behind -- which includes a pass that could
        have been short, and not only one that failed outright.
        """
        for _ in range(MAX_SAMPLE_ATTEMPTS):
            reading = self._read_tree()
            if reading is not None:
                break
        else:
            raise _SupervisionUnavailable(
                "the job's process group could not be read coherently"
            )
        members, resident, consumed, exited = reading
        self.members = members
        self.root_exited = exited
        self.consumed_seconds = max(self.consumed_seconds, consumed)
        if resident > self._memory_bytes:
            return (
                REASON_MEMORY_LIMIT,
                f"{resident} bytes resident across {len(members)} processes",
            )
        if self.consumed_seconds > self._cpu_seconds:
            return (
                REASON_CPU_LIMIT,
                f"{self.consumed_seconds:.1f}s of {self._cpu_seconds}s",
            )
        return "", ""

    def _read_tree(self) -> tuple[tuple[int, ...], int, float, bool] | None:
        """One whole pass over the group, or ``None`` when it was not coherent.

        A pass is coherent when the root was among the members it enumerated
        and every one of those members was still readable when it came to be
        read. Both conditions guard the same hazard, which the readings
        themselves cannot tell apart from an honest measurement: a charge that
        has moved out of this pass's reach between two of its reads.

        A member reaped between the enumeration and its own read answers
        ``ESRCH``, and at that moment its whole charge has moved into the
        ``ri_child_*`` of whichever process reaped it. Read after that reaper,
        it is counted twice -- which over-states the total and errs towards
        refusing, so it is accepted. Read before it, it is counted not at all,
        and taking it for zero would erase every second it consumed. The
        readings do not record which order it was, so the pass is discarded.

        The root's absence is that erasure at its worst, because the root's
        reaper is this process and no member of the group would ever carry its
        time. It is readable for as long as this loop has not reaped it, so a
        pass that cannot find it is not measuring this job: either the group
        does not exist yet, or the leader has been reaped and the group id is
        no longer the job's. Neither may be accounted as a tree.
        """
        members = self._members()
        if self._root not in members:
            return None
        resident = 0
        consumed = 0.0
        exited = False
        for pid in members:
            usage = self._usage(pid)
            if usage is None:
                return None
            resident += usage.ri_resident_size
            consumed += self._tick * (
                usage.ri_user_time
                + usage.ri_system_time
                + usage.ri_child_user_time
                + usage.ri_child_system_time
            )
            if pid == self._root and usage.ri_proc_exit_abstime:
                # Exited and still a zombie: this reading is its terminal
                # usage, and the group id is still held open by its pid. Both
                # are true only until this job's loop reaps it.
                exited = True
        return members, resident, consumed, exited

    def _members(self) -> tuple[int, ...]:
        """Every process in the job's session group, the leader included.

        ``proc_listpgrppids`` returns a *count* of pids -- not a byte length,
        whatever the buffer-sized argument suggests -- and fills exactly what it
        was given room for, reporting no error when there were more. A buffer
        that came back full is therefore read as a possible truncation and the
        call repeated with a larger one, because an incomplete member list
        accounted as if it were the tree is the quiet failure this whole class
        exists to avoid.
        """
        capacity = 64
        while capacity <= MAX_TREE_PROCESSES:
            buffer = (ctypes.c_int32 * capacity)()
            ctypes.set_errno(0)
            count = self._libproc.proc_listpgrppids(
                self._root, ctypes.byref(buffer), ctypes.sizeof(buffer)
            )
            failure = ctypes.get_errno()
            if count < 0 or (count == 0 and failure):
                # The wrapper answers a failure with zero and an errno, which is
                # otherwise exactly what an emptied group looks like.
                raise _SupervisionUnavailable(
                    f"the job's process group could not be read: errno {failure}"
                )
            if count < capacity:
                return tuple(buffer[index] for index in range(count))
            capacity *= 2
        raise _SupervisionUnavailable(
            f"the job holds over {MAX_TREE_PROCESSES} processes"
        )

    def _usage(self, pid: int) -> _RusageInfoV3 | None:
        """One member's accounting, or ``None`` when it has already been reaped.

        A zombie is still readable, so this answers for a member right up to
        the moment its reaper collects it, and ``ESRCH``/``ENOENT`` is that
        moment having passed. That is the ordinary shape of a process ending
        and not a monitor failure -- but it is not a zero either, and
        :meth:`_read_tree` discards the pass rather than accounting the member
        as having consumed nothing. Any other refusal *is* a monitor failure: a
        member of this job's own session that cannot be measured leaves the
        tree with no ceiling.
        """
        usage = _RusageInfoV3()
        ctypes.set_errno(0)
        if (
            self._libproc.proc_pid_rusage(
                pid, RUSAGE_INFO_V3, ctypes.byref(usage)
            )
            != 0
        ):
            failure = ctypes.get_errno()
            if failure in (errno.ESRCH, errno.ENOENT):
                return None
            raise _SupervisionUnavailable(
                f"a member of the job could not be accounted: errno {failure}"
            )
        return usage


def _read_reply(reply_path: Path) -> Mapping[str, Any]:
    """Read and strictly validate the child's reply, bounded before it is read.

    The reply is opened once and measured and read through that one descriptor,
    so the file whose size was checked is the file whose bytes are decoded.

    The open refuses to follow a link and refuses to block, and the descriptor
    it returns is required to be a regular file before anything is read from it.
    All three are the same hazard: the child had write access to this directory
    while a hostile document was being parsed, so what sits at the reply's name
    is whatever it chose to put there. A symlink would aim this read anywhere on
    the host; a FIFO would hold this thread open indefinitely, long after the
    wall-clock budget that was supposed to bound the attempt has been settled; a
    device or a directory is not a reply at all.
    """
    try:
        descriptor = os.open(reply_path, os.O_RDONLY | _NO_FOLLOW | _NON_BLOCKING)
    except FileNotFoundError as error:
        raise DocumentWorkerFailed(
            REASON_PROTOCOL, "the worker wrote no reply"
        ) from error
    except OSError as error:
        raise DocumentWorkerFailed(REASON_PROTOCOL, type(error).__name__) from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            # Asked of the descriptor, not of the name, so this is the object
            # the bytes would have come from and not whatever the name meant a
            # moment ago.
            raise DocumentWorkerFailed(
                REASON_PROTOCOL, "the reply is not a regular file"
            )
        size = opened.st_size
        if size == 0:
            raise DocumentWorkerFailed(REASON_PROTOCOL, "the worker wrote no reply")
        if size > MAX_REPLY_BYTES:
            raise DocumentWorkerFailed(REASON_PROTOCOL, f"the reply is {size} bytes")
        # One byte past the measured size: a reply that grew after it was
        # measured is refused rather than truncated into something decodable.
        raw = _read_exactly(descriptor, size + 1)
    except OSError as error:
        raise DocumentWorkerFailed(REASON_PROTOCOL, type(error).__name__) from error
    finally:
        os.close(descriptor)
    if len(raw) != size:
        raise DocumentWorkerFailed(REASON_PROTOCOL, "the reply changed while it was read")
    try:
        return _decode_reply(raw)
    except _ProtocolError as error:
        raise DocumentWorkerFailed(REASON_PROTOCOL, str(error)) from error


def _read_exactly(descriptor: int, limit: int) -> bytes:
    """At most ``limit`` bytes from ``descriptor``, however short the reads are."""
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _decode_reply(raw: bytes) -> Mapping[str, Any]:
    try:
        reply = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        # `RecursionError` is what a reply nested thousands of levels deep
        # raises before the depth bound below ever sees it, and it has to leave
        # here as a protocol refusal rather than as an untyped failure.
        raise _ProtocolError("the reply is not the JSON this protocol speaks") from error
    if not isinstance(reply, dict):
        raise _ProtocolError("the reply is not a bounded reply object")
    if not _is_protocol(reply.get("protocol")):
        raise _ProtocolError("the reply names another protocol version")
    status = reply.get("status")
    if status not in _STATUSES:
        raise _ProtocolError("the reply status is outside the accepted vocabulary")
    # Exactly the fields this status is answered with. A reply that omits the
    # outcome it claims to carry, or carries one it does not claim, or arrives
    # with a field this protocol never sends, is not a near-enough message.
    if set(reply) != _REPLY_KEYS | _REPLY_STATUS_KEYS[status]:
        raise _ProtocolError("the reply does not carry the fields its status takes")
    confinement = reply.get("confinement")
    # The child says what it sealed, and there is one answer that admits a
    # statement about the document. A child that ran unconfined and extracted
    # anyway is refused here rather than believed. The three worker-failure
    # statuses are each already a terminal failure whatever was sealed, and
    # some of them are reached before the seal is attempted at all, so they are
    # held to naming a confinement this build knows rather than to naming this
    # one.
    accepted = (
        {REQUIRED_CONFINEMENT} if status in _DOCUMENT_STATUSES else _CONFINEMENTS
    )
    if confinement not in accepted:
        raise _ProtocolError("the reply does not carry the required confinement")
    limits = reply.get("limits")
    if not isinstance(limits, list) or not all(
        isinstance(name, str) and len(name) <= 32 for name in limits
    ):
        raise _ProtocolError("the reply does not name its applied resource limits")
    # Present exactly when the status takes one, so where it is present it is a
    # bounded string and not a null standing in for the reason it should carry.
    detail = reply.get("detail")
    if "detail" in reply and (
        not isinstance(detail, str) or len(detail) > MAX_DETAIL_CHARS
    ):
        raise _ProtocolError("the reply detail is not a bounded string")
    return cast("Mapping[str, Any]", reply)


def _outcome_from_reply(reply: Mapping[str, Any]) -> ExtractionOutcome:
    """Turn one validated reply into an outcome, or into its own typed failure."""
    status = reply["status"]
    detail = reply.get("detail") or ""
    if status == STATUS_UNSUPPORTED:
        raise DocumentExtractionUnsupported(
            "no qualified adapter for the admitted media type"
        )
    if status == STATUS_INVALID:
        raise DocumentExtractionInvalid(detail or "the admitted source was refused")
    if status == STATUS_ISOLATION_REFUSED:
        raise DocumentWorkerFailed(REASON_ISOLATION, detail)
    if status == STATUS_PROTOCOL_INVALID:
        raise DocumentWorkerFailed(REASON_PROTOCOL, detail)
    if status == STATUS_WORKER_ERROR:
        raise DocumentWorkerFailed(REASON_INTERNAL, detail)
    try:
        outcome = _decode_outcome(reply.get("outcome"))
    except _ProtocolError as error:
        raise DocumentWorkerFailed(REASON_PROTOCOL, str(error)) from error
    if (
        outcome.output_digest is not None
        and _segments_digest(outcome.segments) != outcome.output_digest
    ):
        raise DocumentWorkerFailed(
            REASON_PROTOCOL, "the transported segments are not the reported output"
        )
    return outcome


# ---------------------------------------------------------------------------
# The reply codec
# ---------------------------------------------------------------------------


def _encode_outcome(outcome: ExtractionOutcome) -> dict[str, Any]:
    """One outcome as the JSON the reply carries.

    A tuple anywhere inside a locator or a structured value becomes an array,
    which is exactly what ``to_canonical_json`` does when the output digest is
    computed, so the digest the child reports still identifies what the parent
    decodes.
    """
    return {
        "state": outcome.state,
        "outputDigest": outcome.output_digest,
        "coverage": dict(outcome.coverage),
        "limitations": dict(outcome.limitations),
        "workerBuild": outcome.worker_build,
        "segments": [
            {
                "semanticRole": segment.semantic_role,
                "sourceLocator": dict(segment.source_locator),
                "text": segment.text,
                "structuredValue": segment.structured_value,
                "extractionMethod": segment.extraction_method,
                "visibility": segment.visibility,
                "revisionState": segment.revision_state,
                "language": segment.language,
                "confidence": segment.confidence,
            }
            for segment in outcome.segments
        ],
    }


def _decode_outcome(encoded: object) -> ExtractionOutcome:
    """One outcome from a reply, with every field checked before it is used."""
    if not isinstance(encoded, dict) or set(encoded) != _OUTCOME_KEYS:
        raise _ProtocolError("the outcome is not a bounded outcome object")
    digest = encoded["outputDigest"]
    if digest is not None and not _is_digest(digest):
        raise _ProtocolError("the outcome digest is not a sha256 digest")
    segments = encoded["segments"]
    if not isinstance(segments, list) or len(segments) > MAX_REPLY_SEGMENTS:
        raise _ProtocolError("the outcome does not carry a bounded segment list")
    return ExtractionOutcome(
        state=_name(encoded["state"], "outcome state"),
        output_digest=None if digest is None else str(digest),
        coverage=_mapping(encoded["coverage"], "coverage"),
        limitations=_mapping(encoded["limitations"], "limitations"),
        worker_build=_name(encoded["workerBuild"], "worker build"),
        segments=tuple(_decode_segment(segment) for segment in segments),
    )


def _decode_segment(encoded: object) -> ExtractedSegment:
    if not isinstance(encoded, dict) or set(encoded) != _SEGMENT_KEYS:
        raise _ProtocolError("a segment is not a bounded segment object")
    return ExtractedSegment(
        semantic_role=_name(encoded["semanticRole"], "semantic role"),
        source_locator=_mapping(encoded["sourceLocator"], "source locator"),
        text=_text(encoded["text"], "segment text"),
        structured_value=_json_value(encoded["structuredValue"], "structured value"),
        extraction_method=_name(encoded["extractionMethod"], "extraction method"),
        visibility=_name(encoded["visibility"], "visibility"),
        revision_state=_name(encoded["revisionState"], "revision state"),
        language=_text(encoded["language"], "language"),
        confidence=_confidence(encoded["confidence"]),
    )


def _confidence(value: object) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise _ProtocolError("a segment confidence is not a finite number")
    return float(value)


def _is_protocol(value: object) -> bool:
    """This exact version, as an integer. ``True`` and ``1.0`` are not it."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value == PROTOCOL_VERSION
    )


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _DIGEST_LENGTH
        and value.startswith(_DIGEST_PREFIX)
        and set(value[len(_DIGEST_PREFIX) :]) <= _HEX
    )


def _name(value: object, what: str) -> str:
    """A bounded vocabulary word.

    The closed vocabularies live in the store that writes them, so this bounds
    the shape and leaves the meaning to the writer that already checks it.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_NAME_CHARS:
        raise _ProtocolError(f"the {what} is not a bounded name")
    return value


def _text(value: object, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _ProtocolError(f"the {what} is not text")
    return value


def _mapping(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _ProtocolError(f"the {what} is not an object")
    _json_value(value, what)
    return cast("dict[str, Any]", value)


def _json_value(value: object, what: str, depth: int = 0) -> Any:
    """Refuse anything outside the JSON data model, at any depth."""
    if depth > MAX_REPLY_DEPTH:
        raise _ProtocolError(f"the {what} nests beyond the accepted bound")
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _ProtocolError(f"the {what} carries a non-finite number")
        return value
    if isinstance(value, list):
        for item in value:
            _json_value(item, what, depth + 1)
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _ProtocolError(f"the {what} carries a non-string field name")
            _json_value(item, what, depth + 1)
        return value
    raise _ProtocolError(f"the {what} is outside the JSON data model")


# ---------------------------------------------------------------------------
# The settings codec
# ---------------------------------------------------------------------------


def _encode_settings(settings: ExtractionSettings | None) -> dict[str, Any] | None:
    """The frozen trusted decisions, carried whole and derived from nothing."""
    if settings is None:
        return None
    ocr = settings.ocr
    return {
        "processingMode": settings.processing_mode,
        "embeddedImageOcr": settings.embedded_image_ocr,
        "ocr": None
        if ocr is None
        else {
            "engine": ocr.engine,
            "engineRevision": ocr.engine_revision,
            "languages": list(ocr.languages),
            "modelHashes": list(ocr.model_hashes),
            "tessdataDir": ocr.tessdata_dir,
            "available": ocr.available,
            "reason": ocr.reason,
            "unavailableModels": list(ocr.unavailable_models),
            "resourceDetail": ocr.resource_detail,
        },
    }


def _decode_settings(encoded: object) -> ExtractionSettings | None:
    if encoded is None:
        return None
    # Exactly these keys. The settings are the frozen trusted decisions, the
    # pinned model directory among them, so a settings object carrying a field
    # this protocol does not send did not come from this protocol and is not
    # read for the parts of it that happen to look familiar.
    if not isinstance(encoded, dict) or set(encoded) != _SETTINGS_KEYS:
        raise _ProtocolError("the settings are not a bounded settings object")
    mode = encoded["processingMode"]
    if mode not in {MODE_TEXT_ONLY, MODE_TEXT_AND_LOCAL_OCR}:
        raise _ProtocolError("the settings name an unknown processing mode")
    embedded = encoded["embeddedImageOcr"]
    if not isinstance(embedded, bool):
        raise _ProtocolError("the embedded-image decision is not a boolean")
    return ExtractionSettings(
        processing_mode=str(mode),
        ocr=_decode_ocr(encoded["ocr"]),
        embedded_image_ocr=embedded,
    )


def _decode_ocr(encoded: object) -> OcrResources | None:
    if encoded is None:
        return None
    if not isinstance(encoded, dict) or set(encoded) != _OCR_KEYS:
        raise _ProtocolError("the pinned OCR resources are not a bounded object")
    available = encoded["available"]
    if not isinstance(available, bool):
        raise _ProtocolError("the OCR availability is not a boolean")
    return OcrResources(
        engine=_name(encoded["engine"], "OCR engine"),
        engine_revision=_name(encoded["engineRevision"], "OCR engine revision"),
        languages=_words(encoded["languages"], "OCR languages"),
        model_hashes=_words(encoded["modelHashes"], "model hashes"),
        tessdata_dir=_text(encoded["tessdataDir"], "model directory"),
        available=available,
        reason=_text(encoded["reason"], "OCR reason"),
        unavailable_models=_words(encoded["unavailableModels"], "unavailable models"),
        resource_detail=_text(encoded["resourceDetail"], "resource detail"),
    )


def _words(value: object, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _ProtocolError(f"the {what} are not a list of names")
    return tuple(cast("list[str]", value))


# ---------------------------------------------------------------------------
# The platform confinement adapter
# ---------------------------------------------------------------------------


def confine_process(
    *, scratch_root: Path, work_dir: Path, model_dirs: Sequence[Path] = ()
) -> str:
    """Seal this process to its admitted scope and return what was established.

    ``scratch_root`` becomes readable, ``work_dir`` writable, and ``model_dirs``
    readable alongside the packaged code and the system libraries. Nothing else
    on the filesystem is reachable, and the network is denied outright.

    Raises :class:`IsolationUnavailable` when no seal was established, which is
    every case other than a seatbelt profile this host accepted: a platform with
    no adapter raises here rather than returning an unconfined process, because
    the caller's next act would be to read a hostile document.
    """
    if sys.platform != "darwin":
        raise IsolationUnavailable(
            f"no confinement adapter for {sys.platform} in this build"
        )
    _apply_seatbelt(
        _seatbelt_profile(
            scratch_root=scratch_root, work_dir=work_dir, model_dirs=model_dirs
        )
    )
    return CONFINEMENT_SEATBELT


def _seatbelt_profile(
    *, scratch_root: Path, work_dir: Path, model_dirs: Sequence[Path]
) -> str:
    """The deny-by-default profile for one extraction.

    Reads are the interpreter's own import roots, the system and package trees
    the pinned readers and their native libraries load from, the verified model
    directory and the private scratch. Writes are the scratch work directory
    alone. ``mach-lookup`` stays open because dyld and the notification daemon
    need it and no bounded service list for the pinned readers has been
    qualified; the explicit ``network*`` denial is what closes the path a looked
    up service could otherwise offer.

    There is deliberately no blanket ``file-read-metadata`` grant. The
    unrestricted form let the child stat the whole filesystem -- every home
    directory, every mounted volume -- which maps the host even though none of
    it can be opened, and 20.2 DOC-16 is about what the worker could reach at
    all. ``file-read*`` already carries metadata for everything in scope.

    One narrow exception is genuinely required and is encoded as such:
    :func:`_traversal_literals`, the named ancestors of the paths already in
    scope. macOS resolves ``/var`` to ``/private/var`` through a symlink, and
    reading that symlink is metadata access to ``/var`` itself, so without this
    a scratch directory under ``/var/folders/...`` -- which is every scratch
    directory :func:`tempfile.mkdtemp` makes here -- cannot be opened at all.
    It is a list of ancestors, not a prefix, so nothing beside them is reachable
    and the whole grant is exercised in ``test_document_worker_isolation.py``.
    """
    # `work_dir` is inside `scratch_root`, so the read scope and the ancestors
    # it needs are both already covered by the scratch.
    in_scope = (*_import_roots(), *model_dirs, scratch_root)
    readable = [
        *_SYSTEM_READ_SUBPATHS,
        *(_sbpl_path(path) for path in in_scope),
    ]
    literals = " ".join(f'(literal "{path}")' for path in _SYSTEM_READ_LITERALS)
    subpaths = " ".join(f'(subpath "{path}")' for path in readable)
    traversal = " ".join(
        f'(literal "{path}")' for path in _traversal_literals(in_scope)
    )
    return f"""(version 1)
(deny default)
(deny network*)
(allow process-exec*)
(allow process-fork)
(allow process-info* (target self))
(allow signal (target self))
(allow sysctl-read)
(allow mach-lookup)
(allow file-map-executable)
(allow file-read-metadata {traversal})
(allow file-read* {literals} {subpaths})
(allow file-write* (subpath "{_sbpl_path(work_dir)}") (literal "/dev/null"))
"""


def _traversal_literals(paths: Sequence[Path]) -> tuple[str, ...]:
    """The named ancestors an open of ``paths`` has to look at on the way in.

    Walking into a directory needs no grant of its own, but *resolving a
    symlink* in the path does, and ``/var`` is one: a scratch directory under
    ``/var/folders/...`` cannot be opened at all unless the child may read
    ``/var``'s own metadata. That is the one thing the unrestricted
    ``file-read-metadata`` grant was carrying, and this is the narrowest form of
    it -- the named ancestors of the paths that are already in scope, in both
    their given and their resolved spelling, and nothing else on the host.

    Deliberately not resolved through :func:`_sbpl_path`: resolving ``/var``
    would write ``/private/var`` into the profile, which is the directory the
    symlink points at rather than the symlink the open has to read.
    """
    literals: dict[str, None] = {}
    for path in paths:
        for spelling in (os.path.abspath(str(path)), os.path.realpath(str(path))):
            ancestor = Path(spelling)
            while ancestor != ancestor.parent:
                ancestor = ancestor.parent
                literals[_sbpl_literal(str(ancestor))] = None
    return tuple(literals)


def _sbpl_path(path: Path | str) -> str:
    """One resolved absolute path as an SBPL string literal.

    The profile is matched against resolved paths, so ``/var/folders/...`` has
    to become ``/private/var/folders/...`` here or the scratch would not be in
    scope.
    """
    return _sbpl_literal(os.path.realpath(str(path)))


def _sbpl_literal(text: str) -> str:
    """One absolute path as an SBPL string literal, exactly as it is spelled.

    A path carrying a control character cannot be written into the profile at
    all, and refusing is the only safe answer.
    """
    if not text.startswith("/") or any(character < " " for character in text):
        raise IsolationUnavailable("a confinement path is not a usable absolute path")
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _import_roots() -> tuple[Path, ...]:
    """The directories this interpreter imports from, resolved and deduplicated.

    These are packaged code, which DOC-16 admits alongside the admitted input
    and the models. The working directory is deliberately not among them: the
    child runs with ``-P``, so nothing a parser drops in the scratch is ever on
    the import path.

    The order is this interpreter's own ``sys.path`` order, because it becomes
    the child's ``PYTHONPATH`` and two trees on it can hold the same package.
    Sorting these would hand the child a different build of Core than the one
    that spawned it.
    """
    roots: dict[Path, None] = {}
    for entry in (*sys.path, sys.prefix, sys.base_prefix, _executable_dir()):
        if entry and os.path.isdir(entry):
            roots[Path(os.path.realpath(entry))] = None
    return tuple(roots)


def _executable_dir() -> str:
    return os.path.dirname(sys.executable) if sys.executable else ""


def _apply_seatbelt(profile: str) -> None:
    """Apply one seatbelt profile to this process, or refuse.

    ``sandbox_init`` is the only interface macOS offers an unprivileged process
    for this. It is applied here rather than by wrapping the command, so the
    seal lands *after* the packaged readers are imported and *before* any
    admitted byte is read.
    """
    name = ctypes.util.find_library("System") or "libSystem.dylib"
    try:
        sandbox_init = ctypes.CDLL(name, use_errno=True).sandbox_init
    except (OSError, AttributeError) as error:
        raise IsolationUnavailable("this host offers no sandbox interface") from error
    sandbox_init.argtypes = [
        ctypes.c_char_p,
        ctypes.c_uint64,
        ctypes.POINTER(ctypes.c_char_p),
    ]
    sandbox_init.restype = ctypes.c_int
    message = ctypes.c_char_p()
    if sandbox_init(profile.encode(), 0, ctypes.byref(message)) != 0:
        raise IsolationUnavailable("the sandbox profile was refused by the host")


def _apply_resource_limits(wall_seconds: int) -> tuple[str, ...]:
    """Apply the per-job ceilings this host enforces, and name those applied.

    Only limits the host actually binds are named, and each is named one by one,
    because the reply's ``limits`` list is read as a claim about enforcement.

    ``RLIMIT_AS`` is absent on purpose: macOS does not enforce it, and naming an
    unenforced control would make the record say more than the host does. So the
    1 GiB resident-memory ceiling of 17.1 is not enforced *here*; it is enforced
    across the whole tree by :class:`_ProcessTreeSupervisor` in the parent.

    ``RLIMIT_CPU`` is consumed CPU time and not wall clock, and each process in
    the tree counts its own: a child inherits the ceiling but starts from zero.
    It therefore bounds a spinning parser and does not bound the tree -- the
    supervisor's cumulative budget is what bounds that, and this remains the
    cheaper control that stops one runaway process first. Windows binds none of
    these at all, and says so by naming none.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - POSIX everywhere this suite runs
        return ()
    applied: list[str] = []
    for name, wanted_soft, wanted_hard in (
        ("RLIMIT_CPU", wall_seconds, wall_seconds + 5),
        ("RLIMIT_FSIZE", MAX_SCRATCH_FILE_BYTES, MAX_SCRATCH_FILE_BYTES),
        ("RLIMIT_CORE", 0, 0),
    ):
        number = getattr(resource, name, None)
        if number is None:
            continue
        try:
            _, hard = resource.getrlimit(number)
            ceiling = (
                wanted_hard
                if hard == resource.RLIM_INFINITY
                else min(wanted_hard, hard)
            )
            resource.setrlimit(number, (min(wanted_soft, ceiling), ceiling))
        except (OSError, ValueError):
            continue
        applied.append(name)
    return tuple(applied)


# ---------------------------------------------------------------------------
# Child
# ---------------------------------------------------------------------------


def _decode_header(raw: bytes) -> Mapping[str, Any]:
    if len(raw) > MAX_HEADER_BYTES:
        raise _ProtocolError("the request header is oversize")
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _ProtocolError("the request header is not JSON") from error
    if not isinstance(header, dict) or set(header) != _HEADER_KEYS:
        raise _ProtocolError("the request header is not a bounded header object")
    if not _is_protocol(header["protocol"]):
        raise _ProtocolError("the request header names another protocol version")
    _name(header["mediaType"], "media type")
    _name(header["captureId"], "capture id")
    if not _is_digest(header["sourceDigest"]):
        raise _ProtocolError("the request header carries no source digest")
    for key in ("sourceLength", "wallSeconds"):
        value = header[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _ProtocolError(f"the request header's {key} is not a count")
    if header["sourceLength"] > MAX_SOURCE_BYTES:
        raise _ProtocolError("the admitted source is oversize")
    scratch = Path(str(header["scratchRoot"]))
    work = Path(str(header["workDir"]))
    for key, root in (
        ("workDir", scratch),
        ("sourcePath", scratch),
        # The reply is written into the one writable directory, so a header
        # that points it anywhere else is not this protocol's header.
        ("replyPath", work),
    ):
        path = Path(str(header[key]))
        if not path.is_absolute() or root not in path.parents:
            raise _ProtocolError(f"the request header's {key} is outside the scratch")
    return cast("Mapping[str, Any]", header)


def _run(
    header: Mapping[str, Any],
    *,
    limits: Sequence[str],
    confine: Callable[..., str] = confine_process,
) -> dict[str, Any]:
    """Seal, then extract, and report exactly what happened.

    The order is the whole point. :func:`main` has already applied the resource
    ceilings; the confinement is established here, and the admitted bytes are
    opened only afterwards. A refused seal returns before a single source byte
    has been read, and the extraction never happens anywhere but in this sealed
    process.
    """
    reply: dict[str, Any] = {
        "protocol": PROTOCOL_VERSION,
        "confinement": CONFINEMENT_NONE,
        "limits": list(limits),
        "status": STATUS_WORKER_ERROR,
    }
    try:
        settings = _decode_settings(header["settings"])
    except (_ProtocolError, ValueError) as error:
        reply["status"] = STATUS_PROTOCOL_INVALID
        reply["detail"] = f"the frozen settings were refused: {type(error).__name__}"
        return reply
    try:
        reply["confinement"] = confine(
            scratch_root=Path(str(header["scratchRoot"])),
            work_dir=Path(str(header["workDir"])),
            model_dirs=_model_dirs(settings),
        )
    except IsolationUnavailable as refusal:
        reply["status"] = STATUS_ISOLATION_REFUSED
        reply["detail"] = str(refusal)[:MAX_DETAIL_CHARS]
        return reply
    # Opened without following a link and read to one byte past the declared
    # length, so neither a substituted file nor a longer one can be parsed, and
    # the digest below settles that these are the admitted bytes.
    try:
        descriptor = os.open(str(header["sourcePath"]), os.O_RDONLY | _NO_FOLLOW)
    except OSError as error:
        reply["status"] = STATUS_PROTOCOL_INVALID
        reply["detail"] = f"the handed source could not be opened: {type(error).__name__}"
        return reply
    try:
        content = _read_exactly(descriptor, int(header["sourceLength"]) + 1)
    finally:
        os.close(descriptor)
    if (
        len(content) != header["sourceLength"]
        or _DIGEST_PREFIX + hashlib.sha256(content).hexdigest()
        != header["sourceDigest"]
    ):
        reply["status"] = STATUS_PROTOCOL_INVALID
        reply["detail"] = "the handed source is not the admitted source"
        return reply
    try:
        outcome = extract_document(
            content,
            media_type=str(header["mediaType"]),
            capture_id=str(header["captureId"]),
            settings=settings,
        )
    except DocumentExtractionUnsupported:
        reply["status"] = STATUS_UNSUPPORTED
        return reply
    except DocumentExtractionInvalid as invalid:
        reply["status"] = STATUS_INVALID
        reply["detail"] = str(invalid)[:MAX_DETAIL_CHARS]
        return reply
    except Exception as error:  # noqa: BLE001 - a parser defect is the worker's own failure, never a verdict on the source
        reply["status"] = STATUS_WORKER_ERROR
        reply["detail"] = _failure_location(error)
        return reply
    reply["status"] = STATUS_EXTRACTED
    reply["outcome"] = _encode_outcome(outcome)
    return reply


def _model_dirs(settings: ExtractionSettings | None) -> tuple[Path, ...]:
    """The verified model directory this extraction may read, if it pins one."""
    if settings is None or settings.ocr is None or not settings.ocr.tessdata_dir:
        return ()
    return (Path(settings.ocr.tessdata_dir),)


def _failure_location(error: BaseException) -> str:
    """Where an unexpected failure came from, with no document content in it.

    The exception's own message is deliberately dropped: a parser error can
    quote the bytes it choked on. A type and a code location are enough to find
    the defect and carry nothing out of the source.
    """
    frames = traceback.extract_tb(error.__traceback__)
    where = (
        f"{os.path.basename(frames[-1].filename)}:{frames[-1].lineno}"
        if frames
        else "?"
    )
    return f"{type(error).__name__}@{where}"[:MAX_DETAIL_CHARS]


def _bare_reply(reply: Mapping[str, Any], detail: str) -> bytes:
    """A reply that carries only what is certainly encodable, and why."""
    return json.dumps(
        {
            "protocol": PROTOCOL_VERSION,
            "confinement": reply.get("confinement", CONFINEMENT_NONE),
            "limits": list(reply.get("limits", ())),
            "status": STATUS_WORKER_ERROR,
            "detail": detail[:MAX_DETAIL_CHARS],
        }
    ).encode()


def _write_reply(reply_path: Path, reply: Mapping[str, Any]) -> None:
    """Write the one reply, refusing to emit more than the protocol admits.

    Created exclusively and without following a link: a parser that ran in this
    process already had write access to this directory, so the one thing the
    reply must not do is land wherever something already sitting at that name
    points. A name already taken means this is not the first write, and failing
    is the right answer -- the parent then reports a worker that produced no
    reply rather than reading one of unknown provenance.
    """
    try:
        raw = json.dumps(reply, allow_nan=False).encode()
    except (TypeError, ValueError):
        raw = _bare_reply(reply, "the extraction outcome is not encodable")
    if len(raw) > MAX_REPLY_BYTES:
        raw = _bare_reply(reply, f"the extraction output is {len(raw)} bytes")
    descriptor = os.open(
        reply_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NO_FOLLOW,
        _PRIVATE_FILE_MODE,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)


def main() -> int:
    """The child entry point: read one request, write one reply, exit."""
    try:
        header = _decode_header(sys.stdin.buffer.read(MAX_HEADER_BYTES + 1))
    except (_ProtocolError, OSError):
        # Without a valid header there is nowhere to answer, so the exit status
        # is the whole answer and the parent reports a crash.
        return 2
    try:
        reply = _run(header, limits=_apply_resource_limits(int(header["wallSeconds"])))
    except Exception as error:  # noqa: BLE001 - the worker answers for its own defects rather than dying silently
        reply = {
            "protocol": PROTOCOL_VERSION,
            "confinement": CONFINEMENT_NONE,
            "limits": [],
            "status": STATUS_WORKER_ERROR,
            "detail": _failure_location(error),
        }
    try:
        _write_reply(Path(str(header["replyPath"])), reply)
    except OSError:
        # The reply could not be put where the request asked for it. Exiting
        # non-zero is the only answer left, and the parent reads it as one.
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a real child process
    raise SystemExit(main())


__all__ = [
    "CONFINEMENT_NONE",
    "CONFINEMENT_SEATBELT",
    "EXTRACTION_SECONDS",
    "MAX_REPLY_BYTES",
    "MAX_SOURCE_BYTES",
    "OCR_EXTRACTION_SECONDS",
    "PROTOCOL_VERSION",
    "REASON_CPU_LIMIT",
    "REASON_CRASHED",
    "REASON_INTERNAL",
    "REASON_ISOLATION",
    "REASON_MEMORY_LIMIT",
    "REASON_PROTOCOL",
    "REASON_RESOURCE_LIMIT",
    "REASON_SCRATCH_RESIDUE",
    "REASON_SOURCE_OVERSIZE",
    "REASON_SPAWN",
    "REASON_SUPERVISION",
    "REASON_TIMEOUT",
    "REASON_TREE_RESIDUE",
    "REQUIRED_CONFINEMENT",
    "WORKER_MEMORY_BYTES",
    "DocumentWorkerFailed",
    "IsolationUnavailable",
    "confine_process",
    "extract_document_isolated",
    "main",
]
