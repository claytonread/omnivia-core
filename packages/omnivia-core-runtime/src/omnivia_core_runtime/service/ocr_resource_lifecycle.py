"""Install, remove and reconcile the managed OCR resource bytes under one root.

:mod:`omnivia_core_runtime.service.ocr_resource_store` owns the state document --
which identities are installed, and which operations pinned one. This module owns
the *bytes* those identities name, and the capacity rule over them. Together they
are the managed resource cache §18.4 reserves against: 512 MiB of installed
resource sets, pinned entries untouchable, and a resource usable only after an
atomic publication.

**Nothing here reaches the network, and nothing installs a dependency.** The
payload is a file the caller already has, and the identity is the trusted
catalogue's -- a language, the engine and model revisions it was built for, its
exact byte size and its SHA-256. Both arrive from a verified manifest or an
explicit operator action, never from a captured document and never from a capture
that found a model missing. §14.5's "never download a dependency because an OCR
job silently requested one" is a property of this module having no way to.

**Verification precedes usability.** A payload is copied into private staging in
bounded blocks, counted and digested as it goes, and refused the moment it
exceeds its declared size -- so a payload that lies about its size cannot fill
the disk before it is caught. Only a copy whose length *and* digest match the
declared identity exactly is published, and publication is a single
:func:`os.replace` of that one verified file, so the payload path holds either the
whole resource or nothing. The enclosing directory is created first and is only
ever a container: nothing reads it, and the state document rather than its
existence is what says a resource is installed.

**Bytes lead, state follows.** Publication happens before the state document
records it, and removal records the absence before the bytes go. A crash
therefore leaves unreferenced bytes rather than a state entry claiming bytes that
are not there: the state document never overstates what is installed.
:meth:`ManagedOcrResources.reconcile` sweeps the other side.

**One operation at a time, across processes.** Every operation here spans two
things -- resource bytes and the state document -- and neither alone is a coherent
view of the cache. Two administrators left to run concurrently would each read the
same remaining capacity and each install against it, exceeding the 512 MiB cap
§18.4 says is never silently exceeded; each would write a state document derived
from a snapshot taken before the other's, losing an entry to the last writer; and
one's reconciliation would sweep the other's live staging. So install, remove,
reconcile and the inventory read are serialised by one exclusive
:data:`~omnivia_core_runtime.ownership.locks.LockRole.RESOURCE_COORDINATION` lock
at a stable owner-private path under the managed root, taken through the same
qualified cross-platform locking the service's own fencing uses rather than a
POSIX-only call of its own. A contender waits a bounded time and then refuses with
:data:`REFUSAL_BUSY`; it never waits forever. The lock is an OS lock on an open
descriptor, so a holder that crashes releases it on the spot and leaves its
abandoned staging for the next reconciliation -- which is why serialising the
operations is enough and no staging needs a separate owner record.

**And one operation at a time within this process.** Both lock implementations are
per-*descriptor*, so two threads sharing one
:class:`ManagedOcrResources` share the lock they are supposed to be excluded by:
the OS grants the second one the lock it already holds. A
:class:`threading.RLock` per instance is what actually serialises them -- held for
the whole of every coordinated block, re-entrant for the thread that owns it so
the administrative surface can still run an action and render the inventory as one
view, and bounded by the same wait, so a thread contending with another thread
refuses with :data:`REFUSAL_BUSY` exactly as it would against another process. The
two waits share one deadline: a contender's total wait is the wait it asked for,
not twice it.

**An absent cache is answered, not inspected.** The inventory read and the
reconciliation pass must not be the thing that brings a cache into existence, so
when the root is absent they take no lock -- and then return the empty answer
outright, computed from no read at all. That makes the unlocked decision a whole
snapshot of the moment it was taken rather than the beginning of one: an install
arriving immediately after creates the root and completes, and the empty answer
remains an accurate *before*. Reading the root after that decision is what would
be incoherent, because the read would land in the middle of the install.

**There is no eviction here, by choice.** §18.4 permits evicting *verified
unpinned* resources, but names no ordering over them, and the state document
records no recency or use that a deterministic policy could rank by. Inventing
one would mean deleting a resource the operator installed on a rule the design
never specified. So a full cache refuses the installation with
:data:`REFUSAL_CAPACITY` -- §18.4's "defer the requested installation/update" and
§14.5's **Language storage is full** -- and reports the capacity it needed.
:meth:`ManagedOcrResources.remove` is the operator's explicit lever instead.

That is also, exactly, why pinned resources never need defending from the cap:
the only thing that could evict one is an eviction policy, and there is none.
Capacity is spent by installing and reclaimed by removing, a pin occupies none of
it and never causes an install, and a cache whose remaining room is held by pinned
entries defers the next installation rather than taking those entries away. No
path here cancels a pin to make room, and none exceeds the 512 MiB cap to avoid
having to.

**One selection, one directory.** The managed layout keeps each identity's bytes
in their own directory so two model revisions of one language can coexist, which
is what §17.3's "including retained older managed model versions" requires. The
engine, though, takes a single ``--tessdata-dir``, so an ordered selection of two
or three languages installed as separate identities could not be handed to it at
all. :meth:`ManagedOcrResources.project_selection` publishes one owner-only
directory per selection whose members are *hard links* to the already-verified
payloads -- not copies, so nothing is duplicated and nothing is re-downloaded,
and not symlinks, which
:func:`~omnivia_core_runtime.service.ocr_resources._model_digest` refuses. The
links name the same inodes the digest check just passed, so the directory the
engine opens holds the exact verified bytes; the selection path digests them
again before invoking anything.

**A projection in use is held, not merely published.** Resolving a selection and
invoking the engine over it are two moments, and between them a removal, a
reconciliation or this module's own pruning would have discarded the directory
the invocation is about to open -- the engine would then be pointed at nothing
and the extraction would report a resource failure it had already resolved past.
:meth:`ManagedOcrResources.leased_selection` publishes the projection and holds
it for the length of the caller's block: while the hold is live the identities it
links may not be removed, reconciliation will not drop them, and neither the
pruning bound nor either sweep discards the directory.

The hold is an **OS lock on a file of its own** under ``leases/``, not an entry
in the state document, and it covers only the minutes one invocation runs. Both
platforms drop it with the holding descriptor, so a lease whose lock can be taken
is a lease nobody holds and the next operation to look discards it -- which makes
it the right instrument for a window a crash must not be able to extend, and the
wrong one for a window that outlives a process. What a live hold covers is read
from the projection's own hard links: a member *is* the payload's inode, which is
the exact byte identity rather than a second record of which identities were
named.

**A queued operation is held by a pin, which is the durable half.** An obligation
may wait hours between being admitted and being executed, and a lock cannot span
that: the service restarts and the lock is gone, while the claim must not be.
:meth:`ManagedOcrResources.pin` writes §18.4's pin -- a record in the state
document naming one operation and the exact identities it was admitted against --
and :meth:`ManagedOcrResources.release` takes it back once that operation has
reached a terminal disposition. The enforcement was here from the start and had no
writer: a pinned identity cannot be removed, and reconciliation keeps its entry
even when the bytes have gone, which is §18.4's unrecoverable case rather than a
repair.

Because the record survives a crash, the crash boundaries are where the care goes.
The pin is written *before* the operation it belongs to is durable and released
*after* that operation has settled -- the only order with no instant in which the
bytes may be removed while the operation still needs them, and therefore an order
whose two failure modes both leave a pin that no live operation holds.
:meth:`ManagedOcrResources.reclaim` is how the owner takes those back: it is told
the prefix its own pins carry and the exact set it still holds unresolved, and
drops the rest. Nothing here guesses which pins are stale -- a durable record is
precisely the thing this module cannot know the liveness of, so the component that
wrote it says.

**And a hold that cannot be read is not a hold that is absent.** Discovering
which selections are held is itself a read of the filesystem, and every one of
those reads can fail. An absent ``leases/`` means no lease, because nothing but
a hold creates it; anything else -- a ``leases/`` that will not enumerate, a
held projection that will not open, a member or a payload that will not stat --
leaves the covered bytes unknown, and this module never spends "unknown" as
"nothing is held". :meth:`ManagedOcrResources.remove` and
:meth:`ManagedOcrResources.reconcile` settle the whole lease question before
they mutate anything and refuse with :data:`REFUSAL_UNAVAILABLE` when they
cannot; the pruning bound, which runs only after a resolution has already
succeeded, preserves every projection instead, because cache tidiness is not
worth failing the resolution it was a tidy-up for.

Still absent, and not implied by anything here: downloading a resource, checking
a signature over one, and discovering that an update exists.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import threading
import time
from collections.abc import Container, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

from omnivia_core_runtime.ownership.discovery import restrict_to_owner
from omnivia_core_runtime.ownership.locks import (
    IS_WINDOWS,
    FileLock,
    LockError,
    LockRole,
    create_lock,
)
from omnivia_core_runtime.service.ocr_resource_store import (
    DEFAULT_OCR_RESOURCE_CAPACITY_BYTES,
    MAX_TOKEN_LENGTH,
    OcrResourceIdentityInvalid,
    OcrResourcePin,
    OcrResourceSetIdentity,
    OcrResourceStore,
    OcrResourceStoreError,
    OcrResourceStoreState,
    _validated_token,
)
from omnivia_core_runtime.workspace.filesystem import fsync_directory

#: The managed OCR cache's directory under an installation state root. Named
#: here beside the layout it describes; choosing the location stays the caller's,
#: exactly as it is for :class:`OcrResourceStore`.
OCR_RESOURCES_DIR: Final = "ocr-resources"

#: Published resource directories, one per installed identity.
MANAGED_RESOURCES_DIRNAME: Final = "resources"

#: Staging for an install in progress.
MANAGED_STAGING_DIRNAME: Final = "staging"

#: Where bytes that stopped matching their declared digest are moved.
MANAGED_QUARANTINE_DIRNAME: Final = "quarantine"

#: Published per-selection directories of hard links, one per ordered selection
#: that has been resolved. A sibling of ``resources/`` rather than a child,
#: because reconciliation discards every child of ``resources/`` no state entry
#: names and a projection is named by no state entry.
MANAGED_SELECTIONS_DIRNAME: Final = "selections"

#: One file per live hold on a projection, named ``<selection>-<hold>`` so the
#: projection a hold covers is the name itself and two concurrent holds of one
#: selection are two files. A sibling of ``selections/`` rather than a child,
#: because a rebuilt projection is replaced wholesale and would take the holds
#: inside it with it.
MANAGED_LEASES_DIRNAME: Final = "leases"

#: How many *unheld* projections are kept. A workspace resolves one selection at
#: a time, so this is generous; it exists because the set would otherwise grow by
#: one every time an operator changed the recognition order and never shrink.
#: A projection a live lease holds is never a candidate and does not count
#: towards the bound, so the published set is this many plus whatever is in use.
MAX_PUBLISHED_SELECTIONS: Final = 8

#: Models one projection may hold: the three recognition languages §14.5 allows,
#: plus the orientation/script data the engine loads alongside them.
MAX_PROJECTED_MODELS: Final = 4

#: The cross-process coordination lock, beside the state document it guards. One
#: stable name, because a lock at a per-call name would exclude nobody -- and at
#: the root rather than inside ``staging/`` or ``resources/``, which are the two
#: directories reconciliation sweeps by name.
MANAGED_LOCK_FILENAME: Final = "coordination.lock"

#: How long an operation waits for the coordination lock before refusing. Long
#: enough that it does not refuse merely because another administrator is copying
#: a large payload, bounded because §18.4's refusals are states an operator is
#: shown rather than a wait with no end. Overridable per cache so a caller with a
#: tighter responsiveness budget -- and a test proving the bound -- can say so.
DEFAULT_LOCK_WAIT_SECONDS: Final = 30.0

#: The model file's extension inside a published resource directory. Spelled
#: here rather than imported from
#: :mod:`omnivia_core_runtime.service.ocr_resources`, whose module-level
#: ``pytesseract`` import would otherwise become a dependency of copying a file.
#: The two must agree, and a test pins that they do.
MODEL_SUFFIX: Final = ".traineddata"

#: Copy block. Large enough that a 512 MiB payload is not a million calls, small
#: enough that an oversized payload is caught within a megabyte of the bound.
_COPY_BLOCK: Final = 1 << 20

#: Bytes of randomness in a staging directory's name.
_STAGING_NAME_BYTES: Final = 16

#: Bytes of randomness distinguishing two concurrent holds of one selection.
_LEASE_NAME_BYTES: Final = 16

#: Owner-only modes, spelled as :mod:`omnivia_core_runtime.ownership.discovery`
#: spells them. ``mkdir(mode=...)`` is masked by the umask and does nothing at
#: all to a directory that already exists, so every managed directory gets an
#: unconditional :func:`restrict_to_owner` besides.
_DIRECTORY_MODE: Final = 0o700

#: Open the payload itself and never a link standing in for it. ``O_NOFOLLOW``
#: closes the window between the check that classified the path and the read that
#: trusts it; ``O_RDONLY`` means this module cannot write to a caller's path
#: whatever it points at.
_SOURCE_FLAGS: Final = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
)

#: Open the coordination lock itself and never a link standing in for it. The
#: POSIX descriptor is created here rather than left to the lock implementation,
#: whose POSIX branch opens the path with ``open(..., "a+b")`` and would follow
#: one. ``O_CREAT`` without ``O_EXCL`` because the point is a *stable* name every
#: contender opens; ``O_NOFOLLOW`` is what makes opening that stable name safe.
#: See :meth:`ManagedOcrResources._open_lock_handle` for the Windows half.
_LOCK_FLAGS: Final = (
    os.O_RDWR
    | os.O_CREAT
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_BINARY", 0)
)

#: The payload is not a file this module may use.
REFUSAL_PAYLOAD: Final = "ocr_resource_payload_refused"

#: The payload's length or digest is not the declared identity's.
REFUSAL_VERIFICATION: Final = "ocr_resource_verification_failed"

#: Installing would exceed the managed cap. §14.5's **Language storage is full**.
REFUSAL_CAPACITY: Final = "language_storage_full"

#: An operation holds this exact identity, so its bytes may not be removed --
#: either a durable §18.4 pin in the state document, or a live selection lease
#: held by an extraction that is between resolving the resources and finishing
#: with them. One code for both, because the operator's recovery is the same:
#: wait for the operation to reach a terminal disposition and try again.
REFUSAL_PINNED: Final = "ocr_resource_pinned"

#: No installed resource has this exact identity.
REFUSAL_NOT_INSTALLED: Final = "ocr_resource_not_installed"

#: The state document claims this identity but its bytes are missing or wrong.
#: Repair is :meth:`ManagedOcrResources.reconcile`, which is explicit because the
#: alternative is an install silently destroying bytes an operator may want to
#: look at.
REFUSAL_DIVERGENT: Final = "ocr_resource_state_divergent"

#: Another process holds the coordination lock, so this operation would have had
#: to act on a view someone else is already changing. Retrying later is the whole
#: of the repair, which is why it is a distinct code from the two below.
REFUSAL_BUSY: Final = "ocr_resource_busy"

#: The coordination lock could not be established at all -- the path is a symlink
#: or not a regular file, the filesystem refused the lock, or the managed root is
#: not usable. Fail closed: without the lock an operation cannot be serialised,
#: and running it unserialised is the defect, not the fallback.
REFUSAL_UNAVAILABLE: Final = "ocr_resource_coordination_unavailable"


class OcrResourceLifecycleRefused(Exception):
    """A managed OCR resource operation was refused.

    ``code`` is one of the module's ``REFUSAL_*`` constants and is the stable
    thing a caller branches on; the message is for an operator. A capacity
    refusal also carries the exact byte figures, because §18.4 requires the
    required capacity to be visible rather than merely reported as full.

    A refusal raised over a *named* selection carries the identities it was
    raised for. The message names them too, but a message is not something a
    caller may parse, and the caller that turns this into a user-facing resource
    condition has to say which languages could not be served rather than
    implicating the whole selection.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        required_bytes: int | None = None,
        available_bytes: int | None = None,
        identities: Sequence[OcrResourceSetIdentity] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes
        self.identities = tuple(identities)


@dataclass(frozen=True, slots=True)
class ManagedOcrResource:
    """One installed resource set as the inventory reports it.

    ``bytes_present`` is the cheap check -- the payload exists, is a regular file
    and is exactly the declared length. It is deliberately not a digest: a status
    call must not read every installed byte. Digest verification is
    :meth:`ManagedOcrResources.reconcile`'s job.
    """

    identity: OcrResourceSetIdentity
    pinned_by: tuple[str, ...]
    bytes_present: bool

    @property
    def pinned(self) -> bool:
        """Whether an unresolved operation holds this exact identity."""
        return bool(self.pinned_by)


@dataclass(frozen=True, slots=True)
class ManagedOcrInventory:
    """What the managed cache holds, and what it has left.

    ``resources`` follows the state document's order, which is sorted by stable
    key, so the same cache always reports the same sequence.
    """

    capacity_bytes: int
    installed_bytes: int
    resources: tuple[ManagedOcrResource, ...]

    @property
    def available_bytes(self) -> int:
        """Bytes a further installation may still occupy."""
        return self.capacity_bytes - self.installed_bytes


@dataclass(frozen=True, slots=True)
class ManagedOcrInstallation:
    """The outcome of an accepted install.

    ``newly_installed`` is false when the exact identity was already installed
    with matching bytes: that call published nothing and changed no state, which
    is what makes repeating an install safe.
    """

    identity: OcrResourceSetIdentity
    payload_path: Path
    newly_installed: bool
    installed_bytes: int
    available_bytes: int


@dataclass(frozen=True, slots=True)
class ManagedOcrReconciliation:
    """What a reconciliation pass found and what it did about it.

    ``quarantined`` entries were dropped from the state document and their
    remaining bytes moved under :data:`MANAGED_QUARANTINE_DIRNAME`.
    ``unrecoverable_pinned`` entries were left exactly as they stand: a pinned
    identity may not be dropped, so §18.4's "pause with a specific
    compatibility/resource issue" is the honest outcome and the caller's to
    report.
    """

    discarded_staging: tuple[str, ...] = ()
    discarded_unreferenced: tuple[str, ...] = ()
    quarantined: tuple[OcrResourceSetIdentity, ...] = ()
    unrecoverable_pinned: tuple[OcrResourceSetIdentity, ...] = ()

    @property
    def changed(self) -> bool:
        """Whether the pass altered the managed tree or the state document."""
        return bool(
            self.discarded_staging or self.discarded_unreferenced or self.quarantined
        )


def _staging_name() -> str:
    """A staging directory name nobody could have pre-created."""
    return os.urandom(_STAGING_NAME_BYTES).hex()


def directory_name(identity: OcrResourceSetIdentity) -> str:
    """The managed directory name for one identity.

    A digest of the stable key rather than the fields themselves. The key already
    distinguishes every identity unambiguously, but it is up to 260 characters
    and carries a separator, while this is fixed-width and drawn from an alphabet
    that admits no separator, no ``..`` and no absolute path -- so a field value
    cannot reach the filesystem as anything but a name, which is the same
    technique :func:`omnivia_core_runtime.workspace.blob_publication.blob_path`
    uses for a digest.
    """
    return hashlib.sha256(identity.stable_key.encode("utf-8")).hexdigest()


def selection_name(identities: Sequence[OcrResourceSetIdentity]) -> str:
    """The managed directory name for one ordered selection.

    A digest over the identities *in order*, by the same reasoning
    :func:`directory_name` uses for one of them: fixed width, no separator, no
    ``..`` and no absolute path, so no field value reaches the filesystem as
    anything but a name. Order is part of the name because order is part of the
    selection -- ``deu+eng`` and ``eng+deu`` are different recognition requests
    and must not share a directory.
    """
    return hashlib.sha256(
        "\n".join(identity.stable_key for identity in identities).encode("utf-8")
    ).hexdigest()


def _discard(path: Path) -> None:
    """Remove a managed path, whatever kind of thing is sitting at it.

    A symlink is unlinked rather than followed, and never descended into: the
    managed tree is owner-only, but a sweep that followed a link out of it would
    be a sweep of somewhere else.
    """
    if path.is_symlink() or path.is_file():
        with suppress(FileNotFoundError):
            path.unlink()
        return
    with suppress(FileNotFoundError):
        shutil.rmtree(path)


def _installed_bytes(state: OcrResourceStoreState) -> int:
    """Exactly what the installed entries occupy.

    The sum of the declared sizes, which is exact because an entry only reaches
    the state document after a copy of exactly that length was verified, and
    because an entry whose bytes stop matching is dropped by reconciliation
    rather than left counting toward the cap.
    """
    return sum(entry.byte_size for entry in state.entries)


class ManagedOcrResources:
    """The managed OCR resource cache under one caller-provided root.

    The root is the caller's choice, as it is for :class:`OcrResourceStore`; this
    class never picks a location. It must be absolute and must not be a symlink,
    because a cache reachable through a link is not owner-private in any useful
    sense and a relative one would mean something different per working
    directory. It holds the state document the store owns plus three directories
    of its own, each created owner-only on first use.
    """

    def __init__(
        self,
        root: Path,
        *,
        capacity_bytes: int = DEFAULT_OCR_RESOURCE_CAPACITY_BYTES,
        lock_wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
    ) -> None:
        if isinstance(capacity_bytes, bool) or not isinstance(capacity_bytes, int):
            raise OcrResourceStoreError("capacity_bytes must be an integer")
        if capacity_bytes <= 0:
            raise OcrResourceStoreError("capacity_bytes must be positive")
        if isinstance(lock_wait_seconds, bool) or not isinstance(
            lock_wait_seconds, (int, float)
        ):
            raise OcrResourceStoreError("lock_wait_seconds must be a number")
        if lock_wait_seconds < 0:
            raise OcrResourceStoreError("lock_wait_seconds must not be negative")
        root = Path(root)
        if not root.is_absolute():
            raise OcrResourceStoreError(f"managed OCR root must be absolute: {root}")
        if root.is_symlink():
            raise OcrResourceStoreError(f"managed OCR root must not be a symlink: {root}")
        self._root = root
        self._capacity_bytes = capacity_bytes
        self._lock_wait_seconds = float(lock_wait_seconds)
        #: Serialises the threads of *this* process over this instance. The OS
        #: lock cannot: it is per-descriptor, and threads share one.
        self._reentry = threading.RLock()
        #: Only ever read or written by the thread holding ``_reentry``, which is
        #: what makes a plain integer safe here.
        self._lock_depth = 0
        self._store = OcrResourceStore(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def capacity_bytes(self) -> int:
        return self._capacity_bytes

    @property
    def lock_wait_seconds(self) -> float:
        return self._lock_wait_seconds

    @property
    def lock_path(self) -> Path:
        """The one coordination lock every operation on this cache contends for."""
        return self._root / MANAGED_LOCK_FILENAME

    @property
    def store(self) -> OcrResourceStore:
        """The state document this cache's bytes are described by."""
        return self._store

    @property
    def resources_root(self) -> Path:
        return self._root / MANAGED_RESOURCES_DIRNAME

    @property
    def staging_root(self) -> Path:
        return self._root / MANAGED_STAGING_DIRNAME

    @property
    def quarantine_root(self) -> Path:
        return self._root / MANAGED_QUARANTINE_DIRNAME

    @property
    def selections_root(self) -> Path:
        return self._root / MANAGED_SELECTIONS_DIRNAME

    @property
    def leases_root(self) -> Path:
        return self._root / MANAGED_LEASES_DIRNAME

    def selection_directory(self, identities: Sequence[OcrResourceSetIdentity]) -> Path:
        """Where this exact ordered selection is published when it is projected."""
        return self.selections_root / selection_name(identities)

    def resource_directory(self, identity: OcrResourceSetIdentity) -> Path:
        """Where this exact identity's bytes live when it is installed."""
        return self.resources_root / directory_name(identity)

    def payload_path(self, identity: OcrResourceSetIdentity) -> Path:
        """The model file inside this identity's resource directory."""
        return self.resource_directory(identity) / f"{identity.language}{MODEL_SUFFIX}"

    # -- coordination --------------------------------------------------------

    @contextmanager
    def coordinated(self, *, create: bool = True) -> Iterator[bool]:
        """Hold this cache's coordination for the length of the block.

        Yields ``True`` when the cache is coordinated and may therefore be read
        and written, and ``False`` only in the one case below, where nothing was
        locked and nothing under the root may be touched at all.

        Every operation whose coherent view spans both resource bytes and the
        state document runs inside one of these, so no two processes -- and no two
        threads of one process -- can read the same capacity and both spend it,
        derive a state document from a snapshot the other has already replaced, or
        sweep each other's live staging. The OS lock excludes the other processes;
        this instance's :class:`threading.RLock`, held for the whole block,
        excludes the other threads, which a per-descriptor OS lock cannot.

        Re-entrant within one instance *for the owning thread only*, counted
        rather than re-acquired: the administrative surface runs an action and then
        renders the inventory, and those two have to be one view of one moment
        rather than two views of two. An inner block must not release what an
        outer one still needs, and an OS lock has no depth of its own to count
        with. The depth is only ever touched under the ``RLock``, so no other
        thread can read a nonzero depth and take the re-entrant path past a lock
        it does not hold.

        ``create=False`` is for the operations that must not be the thing which
        brings the managed cache into existence -- the inventory read and the
        reconciliation pass, both of which do nothing at all to a cache that is
        not there. When the root is absent this yields ``False`` without locking,
        and the caller must then return its empty result **without reading the
        root**: the absence is the whole snapshot, so an install arriving next
        leaves that answer an accurate *before* rather than turning it into a view
        of a cache caught halfway into existence.
        """
        deadline = time.monotonic() + self._lock_wait_seconds
        # A zero wait is one attempt, exactly as it is for the OS lock below.
        if not self._reentry.acquire(timeout=self._lock_wait_seconds):
            raise OcrResourceLifecycleRefused(
                REFUSAL_BUSY,
                f"another thread of this process held the managed OCR resources "
                f"for longer than {self._lock_wait_seconds} seconds; nothing was "
                f"changed",
            )
        try:
            if self._lock_depth:
                self._lock_depth += 1
                try:
                    yield True
                finally:
                    self._lock_depth -= 1
                return
            if not create and self._absent():
                yield False
                return
            lock = self._acquire_coordination(deadline)
            self._lock_depth = 1
            try:
                yield True
            finally:
                self._lock_depth = 0
                lock.release()
        finally:
            self._reentry.release()

    def _absent(self) -> bool:
        """Whether there is no managed cache at this root at all.

        One decision, read once: what the caller returns for an absent cache is
        derived from this answer and from nothing it reads afterwards. A symlink
        at the root is deliberately not absent -- it is a root that fails closed
        when the lock is taken, not an empty cache to report as such.
        """
        return not self._root.exists() and not self._root.is_symlink()

    def _acquire_coordination(self, deadline: float) -> FileLock:
        """Take the one exclusive lock, or refuse; never wait without end.

        The lock is the repository's qualified one -- the same POSIX ``flock`` /
        Windows byte-range implementation behind
        :func:`~omnivia_core_runtime.ownership.locks.create_lock` -- under a role
        that grants no write authority. Both platforms release it when the holding
        process dies, which is what makes a crashed installation recoverable
        rather than a cache locked forever.

        The lock file is made safe to open by :meth:`_open_lock_handle`, inside a
        root this call has just confirmed is an owner-only real directory.

        ``deadline`` is the caller's whole wait, already started before the
        process-local mutex was taken, so waiting for both locks costs the wait
        that was asked for rather than twice it.
        """
        try:
            self._prepare(self._root)
        except OcrResourceStoreError as exc:
            raise OcrResourceLifecycleRefused(
                REFUSAL_UNAVAILABLE,
                f"the managed OCR resources could not be coordinated: {exc}",
            ) from exc

        handle = self._open_lock_handle(self.lock_path)
        lock = create_lock(
            self.lock_path,
            LockRole.RESOURCE_COORDINATION,
            {"component": OCR_RESOURCES_DIR},
            opened_handle=handle,
        )
        try:
            acquired = lock.acquire(
                blocking=False, timeout=max(0.0, deadline - time.monotonic())
            )
        except (LockError, OSError) as exc:
            lock.release()
            raise OcrResourceLifecycleRefused(
                REFUSAL_UNAVAILABLE,
                f"the managed OCR coordination lock could not be taken: {exc}",
            ) from exc
        if not acquired:
            raise OcrResourceLifecycleRefused(
                REFUSAL_BUSY,
                f"another managed OCR resource operation held the coordination "
                f"lock for longer than {self._lock_wait_seconds} seconds; "
                f"nothing was changed",
            )
        return lock

    def _open_lock_handle(self, path: Path) -> IO[bytes] | None:
        """Make one managed lock name safe to open, and open it where that is ours.

        Used for the coordination lock and for each selection lease, which are the
        same kind of thing at different names: a regular file inside the managed
        root whose only content is the holder's advisory payload.

        POSIX: this opens the descriptor itself, because the lock's own opener
        uses ``open(..., "a+b")`` and would follow a symbolic link sitting at the
        name. ``O_NOFOLLOW`` refuses one, and ``O_CREAT`` without ``O_EXCL``
        because the whole point is a stable name that every contender opens.

        Windows: the lock's opener already refuses a reparse point, pins every
        parent and applies an owner-only DACL, so it keeps the open -- but it
        secures a file *it* created behind a handle that shares nothing, and a
        contender arriving inside that window would be told the lock is
        unavailable when the truth is that it is busy. Creating the file here
        first means every contender, the first pair included, reaches it through
        the shared open and receives the ordinary busy result. A name already
        taken is left for that opener to validate rather than adopted here.
        """
        if IS_WINDOWS:  # pragma: no cover - exercised on the hosted Windows row
            try:
                descriptor = os.open(
                    path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
                )
            except FileExistsError:
                return None
            except OSError as exc:
                raise OcrResourceLifecycleRefused(
                    REFUSAL_UNAVAILABLE,
                    f"the managed OCR lock could not be created: {exc}",
                ) from exc
            os.close(descriptor)
            restrict_to_owner(path, directory=False)
            return None

        try:
            descriptor = os.open(path, _LOCK_FLAGS, 0o600)
        except OSError as exc:
            raise OcrResourceLifecycleRefused(
                REFUSAL_UNAVAILABLE,
                f"the managed OCR lock could not be opened: {exc}",
            ) from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                # A directory is already refused by `O_RDWR` itself; a FIFO or a
                # device at the name is not, and would be lockable while being
                # nothing this module should hold open. The same explicit test
                # `_stage` makes of a payload, for the same reason.
                raise OcrResourceLifecycleRefused(
                    REFUSAL_UNAVAILABLE,
                    f"a managed OCR lock must be a regular file: {path}",
                )
            restrict_to_owner(path, directory=False)
            return os.fdopen(descriptor, "r+b", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise

    # -- inventory -----------------------------------------------------------

    def inventory(self) -> ManagedOcrInventory:
        """What is installed, what pins it, and how much capacity remains.

        Reads the state document and stats each payload. Writes nothing beyond
        the coordination lock, never creates the cache, and refuses a state
        document from a newer build rather than reporting an empty cache.

        Coordinated, so the state document and the bytes it describes are read
        from one moment: an uncoordinated read could see an entry whose publication
        a concurrent install of the same identity was in the middle of replacing
        and report an installed resource as absent.

        A cache that is not there is answered rather than inspected: the whole
        empty inventory comes from :meth:`absent_inventory` and the root is not
        opened after the unlocked absence decision, so a concurrent install
        arriving at that moment cannot be observed half-made.
        """
        with self.coordinated(create=False) as coordinated:
            if not coordinated:
                return self.absent_inventory()
            return self._inventory()

    def absent_inventory(self) -> ManagedOcrInventory:
        """What a cache that does not exist holds: nothing, with all capacity free.

        Derived from no read at all, which is the point: it is the complete answer
        for the moment the root was found absent, so it cannot become a partial
        view of a cache that came into existence immediately afterwards.
        """
        return ManagedOcrInventory(
            capacity_bytes=self._capacity_bytes, installed_bytes=0, resources=()
        )

    def _inventory(self) -> ManagedOcrInventory:
        state = self._store.load()
        pins: dict[str, list[str]] = {}
        for pin in state.pins:
            pins.setdefault(pin.identity.stable_key, []).append(pin.operation_id)
        resources = tuple(
            ManagedOcrResource(
                identity=identity,
                pinned_by=tuple(sorted(pins.get(identity.stable_key, ()))),
                bytes_present=self._payload_length(identity) == identity.byte_size,
            )
            for identity in state.entries
        )
        return ManagedOcrInventory(
            capacity_bytes=self._capacity_bytes,
            installed_bytes=_installed_bytes(state),
            resources=resources,
        )

    # -- selection projection ------------------------------------------------

    def project_selection(
        self, identities: Sequence[OcrResourceSetIdentity]
    ) -> Path:
        """One directory the engine can be pointed at for this ordered selection.

        The engine takes a single ``--tessdata-dir``, and the managed layout keeps
        each identity's bytes in a directory of its own, so a selection of two or
        three languages has no directory to name. This publishes one: a hard link
        per model inside a directory named by the ordered selection's digest.

        Nothing is copied, nothing is downloaded and nothing is substituted. Every
        named identity must be installed *and* its bytes must still match their
        declared digest, which is read here before any link is made -- so the
        inodes the links name are inodes this call verified. A missing identity is
        :data:`REFUSAL_NOT_INSTALLED` and divergent bytes are
        :data:`REFUSAL_DIVERGENT`, the same two answers every other operation
        gives for them; neither is degraded into a smaller selection.

        Idempotent: a projection whose members are already links to exactly those
        inodes is returned untouched, so the common case costs one ``lstat`` per
        model and leaves the directory a concurrent engine invocation may be
        reading in place. Otherwise it is rebuilt in private staging and moved
        into place whole, so the published name holds either the complete
        selection or nothing.

        Coordinated, for the same reason the inventory read is: the state document
        and the bytes it describes have to be one view of one moment, and two
        processes projecting at once must not interleave a rebuild with a read of
        the directory being rebuilt.
        """
        selection = self._validated_selection(identities)
        with self.coordinated(create=False) as coordinated:
            if not coordinated:
                raise OcrResourceLifecycleRefused(
                    REFUSAL_NOT_INSTALLED,
                    "this installation has no managed OCR resources to project",
                )
            return self._project(selection)

    @contextmanager
    def leased_selection(
        self, identities: Sequence[OcrResourceSetIdentity]
    ) -> Iterator[Path]:
        """Project this ordered selection and hold it for the length of the block.

        :meth:`project_selection` publishes a directory; this one also keeps it.
        Resolving a selection and invoking the engine over it are two moments, and
        in between a removal, a reconciliation or this module's own pruning would
        discard the directory the invocation is about to open. While the hold is
        live, :meth:`remove` refuses the identities it links, :meth:`reconcile`
        will not drop them, and neither sweep nor the pruning bound touches the
        directory.

        The hold is an exclusive OS lock on a file of its own, taken under the
        coordination lock and then kept while the coordination lock is released --
        which is the point, because an extraction runs for minutes and the
        administrative operations must still be able to run and *preserve* rather
        than wait. Both platforms drop the lock with the holding descriptor, so a
        caller that crashes releases its hold on the spot and leaves a file the
        next operation discards.

        It refuses exactly what :meth:`project_selection` refuses, and for the
        same reasons: nothing is substituted and no smaller selection is served.
        A hold that cannot be taken at all is refused too, so a caller never
        proceeds believing it holds something it does not.
        """
        selection = self._validated_selection(identities)
        with self.coordinated(create=False) as coordinated:
            if not coordinated:
                raise OcrResourceLifecycleRefused(
                    REFUSAL_NOT_INSTALLED,
                    "this installation has no managed OCR resources to lease",
                )
            directory = self._project(selection)
            lock, path = self._hold(directory.name, selection)
        try:
            yield directory
        finally:
            lock.release()
            # The lock is already gone, so the file is a lease nobody holds and
            # the next operation would discard it anyway. Unlinking it here is
            # tidiness, never the release itself.
            with suppress(OSError):
                path.unlink()

    def _hold(
        self, projection: str, selection: tuple[OcrResourceSetIdentity, ...]
    ) -> tuple[FileLock, Path]:
        """Take one live hold on one published projection, or refuse.

        The name carries the projection, so what a hold covers needs no record of
        its own; the random half distinguishes two concurrent holds of the same
        selection, which must each keep their own lock. The lock's advisory
        payload names the identities for an operator reading the file, and is
        never what decides anything -- the lock is.
        """
        self._prepare(self.leases_root)
        path = self.leases_root / f"{projection}-{os.urandom(_LEASE_NAME_BYTES).hex()}"
        handle = self._open_lock_handle(path)
        lock = create_lock(
            path,
            LockRole.RESOURCE_COORDINATION,
            {
                "component": OCR_RESOURCES_DIR,
                "selection": [identity.stable_key for identity in selection],
            },
            opened_handle=handle,
        )
        try:
            acquired = lock.acquire(blocking=False, timeout=0.0)
        except (LockError, OSError) as exc:
            lock.release()
            raise OcrResourceLifecycleRefused(
                REFUSAL_UNAVAILABLE,
                f"a managed OCR selection could not be held: {exc}",
            ) from exc
        if not acquired:  # pragma: no cover - the name is freshly random
            raise OcrResourceLifecycleRefused(
                REFUSAL_UNAVAILABLE,
                f"a managed OCR selection lease was already held at {path}",
            )
        return lock, path

    def _live_leases(self) -> set[str]:
        """The projections a live hold covers, discarding the holds nobody has.

        Liveness is read from the lock and from nothing else: a lease file whose
        lock this call can take is a lease whose holder has gone, because both
        platforms drop the lock with the descriptor. Such a file is discarded on
        the spot, the way reconciliation discards abandoned staging.

        Fails closed, in both halves. A lease this call cannot even probe -- the
        name is not a regular file, the filesystem refused the lock -- is counted
        live, because the alternative is discarding a projection an extraction is
        reading on the strength of not having been able to look. And a
        ``leases/`` that cannot be enumerated at all refuses, for the same reason
        written larger: an empty answer there is indistinguishable from "no
        extraction holds anything", and every caller would then act on it.

        A *missing* ``leases/`` is the one safe absence. Nothing creates it but
        :meth:`_hold`, so a cache that has never leased a selection has no
        directory, and that genuinely means no lease exists.
        """
        try:
            names = sorted(child.name for child in self.leases_root.iterdir())
        except FileNotFoundError:
            return set()
        except OSError as exc:
            raise OcrResourceLifecycleRefused(
                REFUSAL_UNAVAILABLE,
                f"the managed OCR selection leases could not be enumerated, so "
                f"whether an extraction holds a selection is unknown: {exc}",
            ) from exc
        live: set[str] = set()
        for name in names:
            projection, _, hold = name.partition("-")
            if not hold:
                live.add(name)
                continue
            if self._abandoned(self.leases_root / name):
                continue
            live.add(projection)
        return live

    def _abandoned(self, path: Path) -> bool:
        """Whether this lease file's lock is free, discarding it when it is."""
        try:
            probe = create_lock(
                path,
                LockRole.RESOURCE_COORDINATION,
                {"component": OCR_RESOURCES_DIR},
                opened_handle=self._open_lock_handle(path),
            )
        except (OcrResourceLifecycleRefused, OSError):
            return False
        try:
            taken = probe.acquire(blocking=False, timeout=0.0)
        except (LockError, OSError):  # pragma: no cover - platform dependent
            taken = False
        probe.release()
        if not taken:
            return False
        with suppress(OSError):
            path.unlink()
        return True

    def _leased_inodes(self, live: set[str]) -> set[tuple[int, int]]:
        """The payload inodes the live holds cover.

        A projection member *is* the payload's inode -- that is what a hard link
        is -- so this is the exact byte identity a hold covers, read from the
        projection itself rather than from a second record of which identities
        were named and in which order.

        Fails closed on the same rule :meth:`_live_leases` follows: a projection
        a live hold names but this call cannot inspect, or a member of one it
        cannot stat, leaves the covered inodes unknown, and an unknown set read
        as an empty one is exactly how bytes under a running extraction get
        removed or quarantined. Only a projection directory that is *absent* is
        skipped -- it links nothing, so it covers nothing.
        """
        inodes: set[tuple[int, int]] = set()
        for projection in sorted(live):
            try:
                members = sorted((self.selections_root / projection).iterdir())
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise OcrResourceLifecycleRefused(
                    REFUSAL_UNAVAILABLE,
                    f"a live managed OCR selection lease covers {projection}, "
                    f"which could not be inspected: {exc}",
                ) from exc
            for member in members:
                try:
                    info = member.lstat()
                except OSError as exc:
                    raise OcrResourceLifecycleRefused(
                        REFUSAL_UNAVAILABLE,
                        f"a live managed OCR selection lease covers {member}, "
                        f"which could not be inspected: {exc}",
                    ) from exc
                if stat.S_ISREG(info.st_mode):
                    inodes.add((info.st_dev, info.st_ino))
        return inodes

    def _leased(
        self, identity: OcrResourceSetIdentity, inodes: set[tuple[int, int]]
    ) -> bool:
        """Whether a live hold covers exactly this identity's installed bytes.

        An absent payload is not leased -- there is no inode for a hold to have
        linked. Any other failure to stat it leaves the answer unknown, and
        "unknown" must not be spent as "no", so it refuses.
        """
        try:
            info = self.payload_path(identity).lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise OcrResourceLifecycleRefused(
                REFUSAL_UNAVAILABLE,
                f"whether a live managed OCR selection lease covers "
                f"{identity.stable_key} could not be established: {exc}",
            ) from exc
        return stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) in inodes

    def _validated_selection(
        self, identities: Sequence[OcrResourceSetIdentity]
    ) -> tuple[OcrResourceSetIdentity, ...]:
        """The selection as a tuple, or the caller's usage fault.

        Checked before anything is contended for, exactly as :meth:`install`
        validates its payload path first. A repeated language is a usage fault
        rather than a refusal because two identities of one language cannot both
        be published under one ``<language>.traineddata`` name -- the selection is
        not expressible, not merely unavailable.
        """
        selection = tuple(identities)
        if not selection:
            raise OcrResourceStoreError("a projected selection names at least one model")
        if len(selection) > MAX_PROJECTED_MODELS:
            raise OcrResourceStoreError(
                f"a projected selection names at most {MAX_PROJECTED_MODELS} models"
            )
        if not all(
            isinstance(identity, OcrResourceSetIdentity) for identity in selection
        ):
            raise OcrResourceStoreError(
                "a projected selection names OcrResourceSetIdentity values"
            )
        languages = [identity.language for identity in selection]
        if len(set(languages)) != len(languages):
            raise OcrResourceStoreError(
                "a projected selection names one identity per language"
            )
        return selection

    def _project(self, selection: tuple[OcrResourceSetIdentity, ...]) -> Path:
        state = self._store.load()
        for identity in selection:
            if identity not in state.entries:
                raise OcrResourceLifecycleRefused(
                    REFUSAL_NOT_INSTALLED,
                    f"no installed managed OCR resource has the identity "
                    f"{identity.stable_key}",
                    identities=(identity,),
                )
        target = self.selection_directory(selection)
        if self._projection_current(target, selection):
            return target
        for identity in selection:
            if not self._payload_matches(identity):
                raise OcrResourceLifecycleRefused(
                    REFUSAL_DIVERGENT,
                    f"the installed bytes for {identity.stable_key} are missing or no "
                    f"longer match their declared digest; reconcile the managed OCR "
                    f"resources",
                    identities=(identity,),
                )
        self._prepare(self._root)
        self._prepare(self.selections_root)
        self._prepare(self.staging_root)
        staging = self.staging_root / _staging_name()
        try:
            staging.mkdir(mode=_DIRECTORY_MODE)
            restrict_to_owner(staging, directory=True)
            for identity in selection:
                os.link(
                    self.payload_path(identity),
                    staging / f"{identity.language}{MODEL_SUFFIX}",
                )
            fsync_directory(staging)
        except OSError as exc:
            _discard(staging)
            raise OcrResourceStoreError(
                f"a managed OCR selection could not be projected: {exc}"
            ) from exc
        _discard(target)
        try:
            os.replace(staging, target)
        except OSError as exc:
            _discard(staging)
            raise OcrResourceStoreError(
                f"a managed OCR selection could not be published: {exc}"
            ) from exc
        fsync_directory(self.selections_root)
        self._bound_published_selections(keep=target.name)
        return target

    def _projection_current(
        self, target: Path, selection: tuple[OcrResourceSetIdentity, ...]
    ) -> bool:
        """Whether `target` already links exactly this selection's verified bytes.

        Identity by ``(st_dev, st_ino)``, which is what a hard link *is*: a member
        that names the payload's inode holds the payload's bytes by definition, so
        this is both cheaper and stricter than re-digesting a copy would be. A
        member that is a symlink, a directory or a regular file of its own is not
        current, and the projection is rebuilt.
        """
        for identity in selection:
            try:
                linked = (target / f"{identity.language}{MODEL_SUFFIX}").lstat()
                payload = self.payload_path(identity).lstat()
            except OSError:
                return False
            if not stat.S_ISREG(linked.st_mode) or not stat.S_ISREG(payload.st_mode):
                return False
            if (linked.st_dev, linked.st_ino) != (payload.st_dev, payload.st_ino):
                return False
        return True

    def _bound_published_selections(self, *, keep: str) -> None:
        """Keep at most :data:`MAX_PUBLISHED_SELECTIONS` unheld projections, oldest out.

        Oldest by modification time, which a projection only gets when it is
        built: the idempotent path does not touch it. The one just published is
        never a candidate, so this cannot evict the selection it was called for,
        and neither is one a live lease holds -- pruning the directory an
        extraction is about to open is exactly the window the lease exists to
        close. Failures are swallowed -- an unprunable projection is an untidy
        cache, not a reason to fail a resolution that has already succeeded.

        A lease view that cannot be established swallows the whole pass rather
        than pruning against the names it managed to read: every projection is
        preserved, which is the safe answer both ways round -- nothing an
        extraction holds is discarded, and the resolution this ran for is not
        failed because a cache cleanup could not be proven safe.
        """
        try:
            held = self._live_leases() | {keep}
        except OcrResourceLifecycleRefused:
            return
        try:
            published = [
                child
                for child in self.selections_root.iterdir()
                if child.name not in held
            ]
        except OSError:
            return
        if len(published) < MAX_PUBLISHED_SELECTIONS:
            return
        with suppress(OSError):
            published.sort(key=lambda child: child.lstat().st_mtime_ns)
            for child in published[: len(published) - MAX_PUBLISHED_SELECTIONS + 1]:
                _discard(child)
            fsync_directory(self.selections_root)

    # -- install -------------------------------------------------------------

    def install(
        self, payload: Path, identity: OcrResourceSetIdentity
    ) -> ManagedOcrInstallation:
        """Make one already-downloaded payload usable under its exact identity.

        ``payload`` is an absolute path to a regular file the caller already has,
        and ``identity`` is the trusted catalogue's description of it. The payload
        is verified against that description byte for byte before anything is
        published, and the declared size is reserved against the managed cap
        first, so a capacity refusal costs no copying.

        Nothing is substituted: the installation either serves this exact identity
        or refuses. Another model version of the same language, installed or not,
        is a different resource set and is neither consulted nor replaced.

        The whole of it -- the capacity check, the staging, the publication and
        the state write -- runs under the coordination lock, which is what makes
        the reservation a reservation. The arguments are validated first, so a
        caller's malformed payload path is refused without contending for
        anything and without creating a cache.
        """
        if not isinstance(identity, OcrResourceSetIdentity):
            raise OcrResourceStoreError("identity must be an OcrResourceSetIdentity")
        source = self._validated_payload(payload)
        with self.coordinated():
            return self._install(source, identity)

    def _install(
        self, source: Path, identity: OcrResourceSetIdentity
    ) -> ManagedOcrInstallation:
        state = self._store.load()
        if identity in state.entries:
            return self._already_installed(identity, state)

        installed_bytes = _installed_bytes(state)
        remaining = self._capacity_bytes - installed_bytes
        if identity.byte_size > remaining:
            raise OcrResourceLifecycleRefused(
                REFUSAL_CAPACITY,
                f"installing {identity.byte_size} bytes would exceed the managed "
                f"OCR resource cap of {self._capacity_bytes} bytes; "
                f"{remaining} bytes remain",
                required_bytes=identity.byte_size,
                available_bytes=remaining,
            )

        self._prepare(self._root)
        self._prepare(self.resources_root)
        self._prepare(self.staging_root)
        staging = self.staging_root / _staging_name()
        try:
            staging.mkdir(mode=_DIRECTORY_MODE)
        except OSError as exc:
            raise OcrResourceStoreError(
                f"managed OCR staging could not be created: {exc}"
            ) from exc
        restrict_to_owner(staging, directory=True)

        target = self.resource_directory(identity)
        try:
            staged = self._stage(source, identity, staging)
            # No state entry names this directory, so anything already at the
            # name is the remains of an install that did not finish and is not
            # usable. Discarding it keeps the publication a clean one.
            _discard(target)
            self._prepare(target)
            # Bytes before state: a crash from here on leaves an unreferenced
            # directory, which reconciliation sweeps, rather than a state entry
            # naming bytes that never arrived. One verified file into its final
            # name, so the payload path is never a partial resource.
            os.replace(staged, self.payload_path(identity))
            fsync_directory(target)
        except BaseException:
            with suppress(OSError):
                _discard(target)
            raise
        finally:
            with suppress(OSError):
                _discard(staging)

        try:
            self._store.save(
                OcrResourceStoreState(
                    entries=(*state.entries, identity), pins=state.pins
                )
            )
        except BaseException:
            # The publication is undone, so a failed install leaves no usable
            # resource, and the state document is replaced atomically or not at
            # all, so the previous state stands either way.
            with suppress(OSError):
                _discard(target)
            raise
        return ManagedOcrInstallation(
            identity=identity,
            payload_path=self.payload_path(identity),
            newly_installed=True,
            installed_bytes=installed_bytes + identity.byte_size,
            available_bytes=remaining - identity.byte_size,
        )

    def _already_installed(
        self, identity: OcrResourceSetIdentity, state: OcrResourceStoreState
    ) -> ManagedOcrInstallation:
        """Report an install that was already done, or refuse a divergent one.

        Repeating an install is safe and publishes nothing. An entry whose bytes
        are gone or the wrong length is a different matter: it is the crash or
        tampering case, and silently republishing over it would destroy exactly
        what reconciliation exists to preserve.
        """
        if self._payload_length(identity) != identity.byte_size:
            raise OcrResourceLifecycleRefused(
                REFUSAL_DIVERGENT,
                f"the state document already installs {identity.stable_key} but "
                f"its managed bytes are missing or the wrong length; reconcile "
                f"the managed OCR resources before installing it again",
            )
        installed_bytes = _installed_bytes(state)
        return ManagedOcrInstallation(
            identity=identity,
            payload_path=self.payload_path(identity),
            newly_installed=False,
            installed_bytes=installed_bytes,
            available_bytes=self._capacity_bytes - installed_bytes,
        )

    def _validated_payload(self, payload: Path) -> Path:
        """Refuse every caller-supplied path this module must not read.

        Absolute and traversal-free in its given form, so the path means one
        thing and does not depend on a working directory. Not inside the managed
        root, so an install cannot be sourced from the cache it installs into.
        The final component is checked for a symlink here and refused again by
        ``O_NOFOLLOW`` at the open; the ancestor chain is deliberately not walked,
        because whatever bytes a redirected directory yields still have to equal
        the declared length and digest exactly before anything is published.
        """
        if not isinstance(payload, Path):
            raise OcrResourceLifecycleRefused(
                REFUSAL_PAYLOAD, "the payload path must be a path"
            )
        if not payload.is_absolute():
            raise OcrResourceLifecycleRefused(
                REFUSAL_PAYLOAD, f"the payload path must be absolute: {payload}"
            )
        if any(part == ".." for part in payload.parts):
            raise OcrResourceLifecycleRefused(
                REFUSAL_PAYLOAD,
                f"the payload path must not contain a traversal: {payload}",
            )
        if payload.is_symlink():
            raise OcrResourceLifecycleRefused(
                REFUSAL_PAYLOAD, f"the payload must not be a symlink: {payload}"
            )
        root = self._root.resolve()
        resolved = payload.resolve()
        if resolved == root or root in resolved.parents:
            raise OcrResourceLifecycleRefused(
                REFUSAL_PAYLOAD,
                f"the payload must not be inside the managed root: {payload}",
            )
        return payload

    def _stage(
        self, source: Path, identity: OcrResourceSetIdentity, staging: Path
    ) -> Path:
        """Copy the payload into staging, verifying it as the bytes go past.

        The declared size bounds the copy rather than being checked after it: a
        payload that keeps producing bytes is refused as soon as it passes the
        bound, so a mis-declared or hostile payload cannot write 512 MiB into
        staging before anything notices. A short one is refused too, so the size
        cannot drift in either direction.
        """
        target = staging / f"{identity.language}{MODEL_SUFFIX}"
        digest = hashlib.sha256()
        copied = 0
        try:
            descriptor = os.open(source, _SOURCE_FLAGS)
        except OSError as exc:
            raise OcrResourceLifecycleRefused(
                REFUSAL_PAYLOAD, f"the payload could not be opened: {exc}"
            ) from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OcrResourceLifecycleRefused(
                    REFUSAL_PAYLOAD, f"the payload must be a regular file: {source}"
                )
            try:
                with (
                    open(descriptor, "rb", closefd=False) as reading,
                    open(target, "xb") as writing,
                ):
                    while True:
                        block = reading.read(_COPY_BLOCK)
                        if not block:
                            break
                        copied += len(block)
                        if copied > identity.byte_size:
                            raise OcrResourceLifecycleRefused(
                                REFUSAL_VERIFICATION,
                                f"the payload is larger than the declared "
                                f"{identity.byte_size} bytes",
                            )
                        digest.update(block)
                        writing.write(block)
                    writing.flush()
                    os.fsync(writing.fileno())
            except OSError as exc:
                raise OcrResourceStoreError(
                    f"the payload could not be staged: {exc}"
                ) from exc
        finally:
            os.close(descriptor)
        if copied != identity.byte_size:
            raise OcrResourceLifecycleRefused(
                REFUSAL_VERIFICATION,
                f"the payload is {copied} bytes, not the declared {identity.byte_size}",
            )
        actual = digest.hexdigest()
        if actual != identity.sha256:
            raise OcrResourceLifecycleRefused(
                REFUSAL_VERIFICATION,
                f"the payload digest is {actual}, not the declared {identity.sha256}",
            )
        restrict_to_owner(target, directory=False)
        return target

    # -- pins ----------------------------------------------------------------

    def pin(
        self, operation_id: str, identities: Sequence[OcrResourceSetIdentity]
    ) -> None:
        """Durably claim these exact identities for one unresolved operation.

        §18.4's pin, finally written by somebody. The state document has carried
        :class:`~omnivia_core_runtime.service.ocr_resource_store.OcrResourcePin`
        and both halves of its enforcement -- :meth:`remove` refuses a pinned
        identity, :meth:`reconcile` keeps its entry even when the bytes have gone
        -- since the store was written; what was missing was the writer, so
        nothing was ever pinned and the enforcement never fired.

        This is the claim a *queued* operation holds, which is what the selection
        lease deliberately is not. The lease is an OS lock and dies with the
        descriptor, so it covers the minutes an extraction is running and cannot
        survive the hours an obligation may wait; this is a durable record and
        survives a restart, which is the whole point, and which is why
        :meth:`release` and :meth:`reclaim` both exist to take it back.

        One operation's pins are exactly one selection, so a second call for the
        same ``operation_id`` *replaces* what the first claimed rather than adding
        to it. Re-pinning the same selection therefore writes the same document
        and is a no-op, which is what lets execution re-assert its own claim
        before it runs without having to know whether admission's claim survived.

        Refuses, rather than claiming what it cannot: an identity no state entry
        names is :data:`REFUSAL_NOT_INSTALLED`, and one whose payload is missing
        or the wrong length is :data:`REFUSAL_DIVERGENT`. The length is the same
        cheap check :attr:`ManagedOcrResource.bytes_present` makes and
        deliberately not a digest -- a pin is a claim on an identity the store
        says is installed, and proving the bytes belongs to the resolution that
        is about to hand them to the engine, which digests them twice. Pinning
        every capture's models at admission would otherwise read them all.

        Pins occupy no capacity and are never a reason to install anything, so
        this cannot exceed the managed cap. What it does is make the identities
        unevictable, which is why a full cache then defers an installation with
        :data:`REFUSAL_CAPACITY` rather than taking pinned bytes away to fit.
        """
        selection = self._validated_selection(identities)
        with self.coordinated(create=False) as coordinated:
            if not coordinated:
                raise OcrResourceLifecycleRefused(
                    REFUSAL_NOT_INSTALLED,
                    "this installation has no managed OCR resources to pin",
                )
            self._pin(operation_id, selection)

    def _pin(
        self, operation_id: str, selection: tuple[OcrResourceSetIdentity, ...]
    ) -> None:
        state = self._store.load()
        for identity in selection:
            if identity not in state.entries:
                raise OcrResourceLifecycleRefused(
                    REFUSAL_NOT_INSTALLED,
                    f"no installed managed OCR resource has the identity "
                    f"{identity.stable_key}",
                    identities=(identity,),
                )
            if self._payload_length(identity) != identity.byte_size:
                raise OcrResourceLifecycleRefused(
                    REFUSAL_DIVERGENT,
                    f"the state document installs {identity.stable_key} but its "
                    f"managed bytes are missing or the wrong length; reconcile the "
                    f"managed OCR resources before pinning it",
                    identities=(identity,),
                )
        try:
            # The pin's own validation of the operation identifier: a token no
            # longer than a stable key's field, so an identifier assembled from a
            # caller's own names cannot reach the document as anything else.
            claimed = tuple(
                OcrResourcePin(operation_id, identity) for identity in selection
            )
        except OcrResourceIdentityInvalid as exc:
            raise OcrResourceStoreError(f"a managed OCR pin is invalid: {exc}") from exc
        kept = tuple(pin for pin in state.pins if pin.operation_id != operation_id)
        self._store.save(
            OcrResourceStoreState(entries=state.entries, pins=(*kept, *claimed))
        )

    def release(self, operation_id: str) -> None:
        """Drop every pin one operation holds, once it has reached a terminal state.

        The counterpart of :meth:`pin`, and the *only* thing that ends a durable
        claim short of :meth:`reclaim`: §18.4's pin lasts until the operation
        holding it is resolved, and neither a removal nor a reconciliation may
        cancel one to make room.

        Idempotent and quiet about an operation that holds nothing, because the
        caller releasing after a terminal settlement cannot know whether it ever
        pinned -- a text-only capture pins nothing at all -- and must not have to
        branch on it. An absent cache has no pins to drop and is answered rather
        than created, so releasing against an installation whose resources were
        taken away is not an error either.
        """
        _validated_token("operation_id", operation_id)
        with self.coordinated(create=False) as coordinated:
            if not coordinated:
                return
            state = self._store.load()
            kept = tuple(pin for pin in state.pins if pin.operation_id != operation_id)
            if len(kept) != len(state.pins):
                self._store.save(
                    OcrResourceStoreState(entries=state.entries, pins=kept)
                )

    def reclaim(self, prefix: str, live: Container[str]) -> tuple[str, ...]:
        """Drop the pins under ``prefix`` that no live operation holds; which ones.

        What makes a durable pin recoverable rather than a leak. A pin is written
        before the operation that owns it is durable and released after that
        operation has settled, which is the only order with no window in which
        the bytes could be removed while the operation still needs them -- and it
        is therefore an order whose two crash boundaries both leave a pin behind:
        one taken for an admission that then rolled back, and one whose operation
        settled before the release landed. Neither is distinguishable from a live
        claim by looking at the state document, because a durable record is
        exactly the thing a crash cannot withdraw.

        So the owner says. ``prefix`` scopes this to the pins one caller writes --
        the pins of another workspace, or of some later component, are not under
        it and are not even considered -- and ``live`` is the set of operation
        identifiers that caller still holds unresolved, computed from its own
        durable state. A pin under the prefix that is not in ``live`` names an
        operation that no longer exists or has already settled, and is dropped.

        ``live`` must be *exact*, not a page of the first few: a truncated set
        would read as "these operations are no longer unresolved" and release the
        pins of work that is still queued. The caller computing it is the one
        thing this cannot check.

        The prefix is checked only for being a bounded non-empty string, and
        deliberately not for being a *token*: it is the beginning of one, so it
        may well end on the separator a token may not, and it reaches nothing but
        :meth:`str.startswith` -- no path, no document key, no lock name -- so
        there is nothing a narrower alphabet would protect.
        """
        if not isinstance(prefix, str) or not 0 < len(prefix) <= MAX_TOKEN_LENGTH:
            raise OcrResourceStoreError(
                f"a managed OCR pin prefix must be 1 to {MAX_TOKEN_LENGTH} "
                f"characters"
            )
        with self.coordinated(create=False) as coordinated:
            if not coordinated:
                return ()
            state = self._store.load()
            orphaned = {
                pin.operation_id
                for pin in state.pins
                if pin.operation_id.startswith(prefix) and pin.operation_id not in live
            }
            if not orphaned:
                return ()
            self._store.save(
                OcrResourceStoreState(
                    entries=state.entries,
                    pins=tuple(
                        pin for pin in state.pins if pin.operation_id not in orphaned
                    ),
                )
            )
            return tuple(sorted(orphaned))

    @contextmanager
    def pinned_selection(
        self, operation_id: str, identities: Sequence[OcrResourceSetIdentity]
    ) -> Iterator[Path]:
        """Re-assert one operation's claim, then project and hold what it claims.

        The execution half of a durable pin: an operation that is about to invoke
        the engine over the identities it was admitted against asserts the claim
        again and only then resolves them. Both steps refuse rather than
        substitute, so an identity that is no longer installed, or whose bytes no
        longer match, stops the invocation instead of being quietly replaced by
        whatever model of that language is installed now.

        Re-asserting is what closes the gap between a pin that should be there
        and one that is. :meth:`pin` replaces this operation's claim with exactly
        this selection, so a claim that survived is confirmed, a claim lost with
        an interrupted admission is restored, and either way the engine is never
        invoked over bytes nothing holds. It costs one state-document write per
        obligation, which is the price of not having to trust that the pin
        written hours earlier is still there.

        The two coordinated blocks are deliberately *not* nested, and the hold is
        taken in the second one: coordination is released while the caller's
        block runs, exactly as :meth:`leased_selection` arranges, because an
        extraction runs for minutes and the administrative operations must stay
        able to run and preserve rather than wait. Nothing can be removed in
        between, because by then the durable pin already refuses it.
        """
        selection = self._validated_selection(identities)
        self.pin(operation_id, selection)
        with self.leased_selection(selection) as directory:
            yield directory

    # -- remove --------------------------------------------------------------

    def remove(self, identity: OcrResourceSetIdentity) -> None:
        """Remove one installed, unpinned resource set by its exact identity.

        Fails closed on both halves of "which resource": an identity nothing
        installed is refused rather than treated as already gone, and a pinned
        one is refused outright, because §18.4's pin lasts until the operation
        holding it reaches a terminal disposition and an attempted removal cannot
        cancel it.

        The state document is updated before the bytes go, so an interrupted
        removal leaves unreferenced bytes for reconciliation rather than an entry
        claiming bytes that have already been deleted.

        Coordinated for the same reason an install is: read-decide-write over the
        state document, so a removal overlapping an installation would otherwise
        write back a document that never knew about the other's entry.
        """
        if not isinstance(identity, OcrResourceSetIdentity):
            raise OcrResourceStoreError("identity must be an OcrResourceSetIdentity")
        with self.coordinated():
            self._remove(identity)

    def _remove(self, identity: OcrResourceSetIdentity) -> None:
        state = self._store.load()
        if identity not in state.entries:
            raise OcrResourceLifecycleRefused(
                REFUSAL_NOT_INSTALLED,
                f"no managed OCR resource is installed as {identity.stable_key}",
            )
        holders = sorted(
            pin.operation_id for pin in state.pins if pin.identity == identity
        )
        if holders:
            raise OcrResourceLifecycleRefused(
                REFUSAL_PINNED,
                f"{identity.stable_key} is pinned by {', '.join(holders)} "
                f"and may not be removed",
            )
        # A live selection lease is the same answer as a pin and for the same
        # reason: an extraction is between resolving these exact bytes and
        # finishing with them, and §18.4's hold lasts until the operation
        # reaches a terminal disposition. Removing them would leave the engine
        # pointed at a language the operator took away mid-invocation.
        live = self._live_leases()
        if self._leased(identity, self._leased_inodes(live)):
            raise OcrResourceLifecycleRefused(
                REFUSAL_PINNED,
                f"{identity.stable_key} is leased by an extraction in progress "
                f"and may not be removed",
            )
        self._store.save(
            OcrResourceStoreState(
                entries=tuple(entry for entry in state.entries if entry != identity),
                pins=state.pins,
            )
        )
        # Every unheld projection goes first: a published selection holds a *hard
        # link* to these bytes, so one left behind would both keep the removed
        # inode alive on disk after its accounting entry is gone and let the
        # engine still be pointed at a language the operator has removed. They
        # are derived state and the next resolution rebuilds whichever are still
        # wanted, so discarding all of them is cheaper than working out which.
        # A held one is kept, and may be: every identity it links is leased, and
        # a leased identity is the one this call has just refused to remove.
        self._discard_projections(live)
        # The entry is gone, so these bytes are already unreferenced and a
        # failure here is work for reconciliation rather than a failed removal.
        with suppress(OSError):
            _discard(self.resource_directory(identity))

    # -- reconcile -----------------------------------------------------------

    def reconcile(self) -> ManagedOcrReconciliation:
        """Make the managed bytes and the state document describe each other.

        Three repairs, in the order that keeps the state document truthful
        throughout. Abandoned staging is discarded. Resource directories no state
        entry names are discarded: they are the crash window between publication
        and the state write, and they are not usable, because the state document
        is what says a resource is installed. Finally every entry's bytes are read
        and digested; one that is missing or no longer matches is dropped from the
        state document and its remaining bytes moved to quarantine, so the cache
        stops counting and offering a resource it does not have.

        Quarantine rather than deletion because bytes that stopped matching their
        digest are the evidence of whatever changed them, and quarantine rather
        than leaving them where
        :meth:`~omnivia_core_runtime.distribution.shared_runtime.SharedRuntimeInstallation.install_candidate`
        would -- it leaves a conflicting payload untouched -- because here the
        entry naming them is being dropped in the same pass, which would make
        them unreferenced and so the next pass's sweep would eat the evidence.

        A pinned entry is never dropped, even when its bytes are gone. That is
        §18.4's unrecoverable case -- the operation holding the pin must pause
        with a resource issue rather than run different bytes under its key --
        and it is reported for the caller to act on rather than repaired here.
        An entry a live selection lease still links is kept on the same rule, and
        so is the projection linking it, so a reconciliation running while an
        extraction is between resolving its resources and invoking the engine
        cannot take either away. An entry whose payload was *replaced* under it
        is quarantined even so: the hold covers the inode the projection links,
        which the replacement is not, and the running invocation keeps reading
        the verified bytes the hard link preserves.

        A state document from a newer build is refused by the load and nothing is
        touched, so an older build cannot reconcile away what a newer one
        recorded.

        This sweeps all of ``staging/`` rather than only what one call left, and
        the coordination lock is what makes that safe: an installation holds the
        lock for its whole length, so staging a reconciliation can see is staging
        no live operation owns. A holder that crashes releases the lock with its
        descriptor and leaves its staging exactly where the next pass will find
        it, which is why no staging directory needs an owner record of its own and
        why this cannot refuse to clean up after a dead process.

        A cache that is not there has nothing to repair, and that answer is
        returned without reading the root: sweeping a tree discovered after the
        unlocked absence decision would be sweeping a live install's staging.
        """
        with self.coordinated(create=False) as coordinated:
            if not coordinated:
                return ManagedOcrReconciliation()
            return self._reconcile()

    def _reconcile(self) -> ManagedOcrReconciliation:
        state = self._store.load()
        # The whole lease question is settled first, before anything is
        # discarded: every repair below is conditioned on what a live hold
        # covers, so a lease view that cannot be established has to refuse the
        # pass rather than let it sweep on the strength of not having been able
        # to look. That includes the per-entry probe, which is hoisted here for
        # the same reason -- an entry whose lease state is unknown must not be
        # reached after an earlier one has already been quarantined.
        live = self._live_leases()
        leased = self._leased_inodes(live)
        held = {entry for entry in state.entries if self._leased(entry, leased)}
        discarded_staging = self._discard_children(self.staging_root)
        # Projections are derived from the entries this pass may be about to drop,
        # and they hold hard links that would keep quarantined inodes alive. They
        # are not reported: the counts here are about what the state document
        # referenced, and a projection is referenced by no entry. The ones a live
        # lease holds are kept, and the entries they link are not dropped.
        self._discard_projections(live)
        referenced = {directory_name(entry) for entry in state.entries}
        discarded_unreferenced = self._discard_children(
            self.resources_root, keep=referenced
        )

        kept: list[OcrResourceSetIdentity] = []
        quarantined: list[OcrResourceSetIdentity] = []
        unrecoverable: list[OcrResourceSetIdentity] = []
        pinned = {pin.identity for pin in state.pins}
        for entry in state.entries:
            if self._payload_matches(entry):
                kept.append(entry)
            elif entry in pinned or entry in held:
                kept.append(entry)
                unrecoverable.append(entry)
            else:
                self._quarantine(entry)
                quarantined.append(entry)
        if quarantined:
            self._store.save(
                OcrResourceStoreState(entries=tuple(kept), pins=state.pins)
            )
        return ManagedOcrReconciliation(
            discarded_staging=discarded_staging,
            discarded_unreferenced=discarded_unreferenced,
            quarantined=tuple(quarantined),
            unrecoverable_pinned=tuple(unrecoverable),
        )

    def _discard_projections(self, live: set[str]) -> None:
        """Discard every unheld published selection; the next resolution rebuilds them.

        The projections a live lease holds are kept, because an extraction is
        between resolving one and invoking the engine over it and the hold is
        exactly the statement that it may not be taken away underneath.

        Tolerant of failure on purpose: an undiscardable projection must not fail
        the removal or reconciliation it was a tidy-up for, and the state document
        -- not a projection -- is what says a resource is installed.
        """
        with suppress(OcrResourceStoreError, OSError):
            self._discard_children(self.selections_root, keep=live)

    def _discard_children(
        self, directory: Path, *, keep: frozenset[str] | set[str] = frozenset()
    ) -> tuple[str, ...]:
        """Discard every child of one managed directory except the kept names."""
        try:
            names = sorted(child.name for child in directory.iterdir())
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise OcrResourceStoreError(
                f"managed OCR resources could not be scanned: {exc}"
            ) from exc
        discarded: list[str] = []
        for name in names:
            if name in keep:
                continue
            _discard(directory / name)
            discarded.append(name)
        if discarded:
            fsync_directory(directory)
        return tuple(discarded)

    def _quarantine(self, identity: OcrResourceSetIdentity) -> None:
        """Move one entry's remaining bytes out of the managed layout.

        A name already taken is left alone and the source discarded instead:
        quarantine is for inspection, and overwriting an earlier specimen would
        defeat it.
        """
        source = self.resource_directory(identity)
        if not source.exists() and not source.is_symlink():
            return
        self._prepare(self._root)
        self._prepare(self.quarantine_root)
        destination = self.quarantine_root / directory_name(identity)
        if destination.exists() or destination.is_symlink():
            with suppress(OSError):
                _discard(source)
            return
        try:
            os.replace(source, destination)
        except OSError:
            with suppress(OSError):
                _discard(source)
            return
        fsync_directory(self.quarantine_root)

    # -- shared --------------------------------------------------------------

    def _prepare(self, directory: Path) -> None:
        """Create one managed directory owner-only, refusing a redirected path.

        The ``chmod`` behind :func:`restrict_to_owner` is unconditional because
        ``mkdir``'s mode is masked by the umask and does nothing at all to a
        directory that already exists -- the reasoning
        :meth:`~omnivia_core_runtime.distribution.shared_runtime.SharedRuntimeInstallation.initialise`
        spells out.
        """
        if directory.is_symlink():
            raise OcrResourceStoreError(
                f"managed OCR path must not be a symlink: {directory}"
            )
        if directory.exists() and not directory.is_dir():
            raise OcrResourceStoreError(
                f"managed OCR path must be a directory: {directory}"
            )
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=_DIRECTORY_MODE)
        except OSError as exc:
            raise OcrResourceStoreError(
                f"managed OCR path could not be created: {exc}"
            ) from exc
        restrict_to_owner(directory, directory=True)

    def _payload_length(self, identity: OcrResourceSetIdentity) -> int | None:
        """The declared payload's length, or ``None`` when it is not usable.

        ``lstat`` and an explicit regular-file test, so a symlink standing where
        a payload belongs is unusable rather than silently measured through.
        """
        try:
            info = self.payload_path(identity).lstat()
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        return int(info.st_size)

    def _payload_matches(self, identity: OcrResourceSetIdentity) -> bool:
        """Whether the installed bytes are still exactly this identity's."""
        if self._payload_length(identity) != identity.byte_size:
            return False
        digest = hashlib.sha256()
        try:
            descriptor = os.open(self.payload_path(identity), _SOURCE_FLAGS)
        except OSError:
            return False
        try:
            with open(descriptor, "rb", closefd=False) as reading:
                for block in iter(lambda: reading.read(_COPY_BLOCK), b""):
                    digest.update(block)
        except OSError:
            return False
        finally:
            os.close(descriptor)
        return digest.hexdigest() == identity.sha256


__all__ = [
    "DEFAULT_LOCK_WAIT_SECONDS",
    "MANAGED_LEASES_DIRNAME",
    "MANAGED_LOCK_FILENAME",
    "MANAGED_QUARANTINE_DIRNAME",
    "MANAGED_RESOURCES_DIRNAME",
    "MANAGED_SELECTIONS_DIRNAME",
    "MANAGED_STAGING_DIRNAME",
    "MAX_PROJECTED_MODELS",
    "MAX_PUBLISHED_SELECTIONS",
    "MODEL_SUFFIX",
    "OCR_RESOURCES_DIR",
    "REFUSAL_BUSY",
    "REFUSAL_CAPACITY",
    "REFUSAL_DIVERGENT",
    "REFUSAL_NOT_INSTALLED",
    "REFUSAL_PAYLOAD",
    "REFUSAL_PINNED",
    "REFUSAL_UNAVAILABLE",
    "REFUSAL_VERIFICATION",
    "ManagedOcrInstallation",
    "ManagedOcrInventory",
    "ManagedOcrReconciliation",
    "ManagedOcrResource",
    "ManagedOcrResources",
    "OcrResourceLifecycleRefused",
    "directory_name",
    "selection_name",
]
