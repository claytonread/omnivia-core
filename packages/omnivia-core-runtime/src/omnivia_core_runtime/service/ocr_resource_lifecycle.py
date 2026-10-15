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

**There is no eviction here, by choice.** §18.4 permits evicting *verified
unpinned* resources, but names no ordering over them, and the state document
records no recency or use that a deterministic policy could rank by. Inventing
one would mean deleting a resource the operator installed on a rule the design
never specified. So a full cache refuses the installation with
:data:`REFUSAL_CAPACITY` -- §18.4's "defer the requested installation/update" and
§14.5's **Language storage is full** -- and reports the capacity it needed.
:meth:`ManagedOcrResources.remove` is the operator's explicit lever instead.

Still absent, and not implied by anything here: downloading a resource, checking
a signature over one, discovering that an update exists, and presenting several
installed languages to the engine as one ``tessdata`` directory. The managed
layout keeps each identity's bytes in their own directory so two model revisions
of one language can coexist, which is what §17.3's "including retained older
managed model versions" requires; composing a multi-language selection out of
them is the selection path's problem and is not solved here.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from omnivia_core_runtime.ownership.discovery import restrict_to_owner
from omnivia_core_runtime.service.ocr_resource_store import (
    DEFAULT_OCR_RESOURCE_CAPACITY_BYTES,
    OcrResourceSetIdentity,
    OcrResourceStore,
    OcrResourceStoreError,
    OcrResourceStoreState,
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

#: The payload is not a file this module may use.
REFUSAL_PAYLOAD: Final = "ocr_resource_payload_refused"

#: The payload's length or digest is not the declared identity's.
REFUSAL_VERIFICATION: Final = "ocr_resource_verification_failed"

#: Installing would exceed the managed cap. §14.5's **Language storage is full**.
REFUSAL_CAPACITY: Final = "language_storage_full"

#: An operation pins this exact identity, so its bytes may not be removed.
REFUSAL_PINNED: Final = "ocr_resource_pinned"

#: No installed resource has this exact identity.
REFUSAL_NOT_INSTALLED: Final = "ocr_resource_not_installed"

#: The state document claims this identity but its bytes are missing or wrong.
#: Repair is :meth:`ManagedOcrResources.reconcile`, which is explicit because the
#: alternative is an install silently destroying bytes an operator may want to
#: look at.
REFUSAL_DIVERGENT: Final = "ocr_resource_state_divergent"


class OcrResourceLifecycleRefused(Exception):
    """A managed OCR resource operation was refused.

    ``code`` is one of the module's ``REFUSAL_*`` constants and is the stable
    thing a caller branches on; the message is for an operator. A capacity
    refusal also carries the exact byte figures, because §18.4 requires the
    required capacity to be visible rather than merely reported as full.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        required_bytes: int | None = None,
        available_bytes: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes


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
    ) -> None:
        if isinstance(capacity_bytes, bool) or not isinstance(capacity_bytes, int):
            raise OcrResourceStoreError("capacity_bytes must be an integer")
        if capacity_bytes <= 0:
            raise OcrResourceStoreError("capacity_bytes must be positive")
        root = Path(root)
        if not root.is_absolute():
            raise OcrResourceStoreError(f"managed OCR root must be absolute: {root}")
        if root.is_symlink():
            raise OcrResourceStoreError(f"managed OCR root must not be a symlink: {root}")
        self._root = root
        self._capacity_bytes = capacity_bytes
        self._store = OcrResourceStore(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def capacity_bytes(self) -> int:
        return self._capacity_bytes

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

    def resource_directory(self, identity: OcrResourceSetIdentity) -> Path:
        """Where this exact identity's bytes live when it is installed."""
        return self.resources_root / directory_name(identity)

    def payload_path(self, identity: OcrResourceSetIdentity) -> Path:
        """The model file inside this identity's resource directory."""
        return self.resource_directory(identity) / f"{identity.language}{MODEL_SUFFIX}"

    # -- inventory -----------------------------------------------------------

    def inventory(self) -> ManagedOcrInventory:
        """What is installed, what pins it, and how much capacity remains.

        Reads the state document and stats each payload. Writes nothing, creates
        nothing, and refuses a state document from a newer build rather than
        reporting an empty cache.
        """
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
        """
        if not isinstance(identity, OcrResourceSetIdentity):
            raise OcrResourceStoreError("identity must be an OcrResourceSetIdentity")
        source = self._validated_payload(payload)

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
        """
        if not isinstance(identity, OcrResourceSetIdentity):
            raise OcrResourceStoreError("identity must be an OcrResourceSetIdentity")
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
        self._store.save(
            OcrResourceStoreState(
                entries=tuple(entry for entry in state.entries if entry != identity),
                pins=state.pins,
            )
        )
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

        A state document from a newer build is refused by the load and nothing is
        touched, so an older build cannot reconcile away what a newer one
        recorded.

        ponytail: this sweeps all of ``staging/``, not only what one call left,
        which is safe because an install never outlives the call that started it
        and the service owns this root alone. A second concurrent installer would
        need per-call staging ownership, as
        :mod:`omnivia_core_runtime.distribution.shared_runtime` keeps.
        """
        state = self._store.load()
        discarded_staging = self._discard_children(self.staging_root)
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
            elif entry in pinned:
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
    "MANAGED_QUARANTINE_DIRNAME",
    "MANAGED_RESOURCES_DIRNAME",
    "MANAGED_STAGING_DIRNAME",
    "MODEL_SUFFIX",
    "OCR_RESOURCES_DIR",
    "REFUSAL_CAPACITY",
    "REFUSAL_DIVERGENT",
    "REFUSAL_NOT_INSTALLED",
    "REFUSAL_PAYLOAD",
    "REFUSAL_PINNED",
    "REFUSAL_VERIFICATION",
    "ManagedOcrInstallation",
    "ManagedOcrInventory",
    "ManagedOcrReconciliation",
    "ManagedOcrResource",
    "ManagedOcrResources",
    "OcrResourceLifecycleRefused",
    "directory_name",
]
