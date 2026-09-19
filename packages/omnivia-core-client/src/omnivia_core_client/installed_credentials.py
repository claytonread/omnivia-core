"""Where one installation keeps the protected state its own installed MCP server needs.

Two stores, one set of rules. :class:`InstalledCredentialStore` holds the bearers
the server presents; :class:`InstalledConfigStore` holds the
`omnivia.mcp-config.v1` document that tells it which bearer to ask for. They are
here together because the second is the first's layout with a different leaf: one
fixed directory under ``runtime/``, one file per key, no component any caller
supplies, and the same descriptor-anchored walk down to it. A second copy of that
walk would be a second copy to keep correct.

The prose below is written about the credential store; every paragraph of it is
true of the configuration store too, save for what the two hold and how each names
its leaf -- a digest of the reference there, a word from
:data:`CONFIGURATION_HOSTS` here.

The installed MCP server is handed a *name* -- a
:class:`~omnivia_core_client.CredentialReference` in its public configuration --
and has to turn it into the bearer it presents on every application call. That
is the one thing :mod:`~omnivia_core_client.credentials` deliberately does not
do: it defines the seam and refuses to invent a store behind it. This module is
that store, for exactly one host: a local installation, holding its own
credentials, for its own dedicated principals.

**The caller does not choose the path.** A store is rooted at an explicit
``installation_state`` -- the same trusted root discovery and the managed-local
convention are given -- and everything below that is fixed here: one private
directory at :data:`STORE_DIRECTORY`, and inside it one file per reference whose
name is a digest of that reference. There is no path argument, no filename
argument, no prefix, no suffix and no escape from the root, so a configuration
that could name a file could not name a file *here*. The digest is not secrecy --
a reference is a name and may be printed -- it is what keeps a valid reference
from spelling a reserved device name on Windows, and what keeps two references
differing only in case from becoming one file on a case-insensitive filesystem.

**The material is in the file and nowhere else.** It never reaches a process
argument, an environment variable, the public MCP configuration, an endpoint
descriptor, a log line, an exception or a ``repr``: it leaves this module only
inside a :class:`~omnivia_core_client.Credential`, whose ``repr`` and ``str`` are
a fixed redaction and whose one accessor is named :meth:`Credential.reveal`. This
module keeps no copy and no cache -- :meth:`InstalledCredentialStore.resolve`
reads the file every time it is called -- which is what makes a revocation or a
rotation take effect on the very next call rather than at some expiry.

**Nothing below the root is reached through a pathname.** Every operation --
read, store, remove, health -- first walks down to the store directory one
component at a time, opening each with ``O_DIRECTORY`` and ``O_NOFOLLOW`` and
proving it before descending, and then *holds those descriptors open for the
whole operation*. The leaf is opened, stat'ed, unlinked and renamed relative to
the held descriptor, never by a path this module composed. That is what a
pathname cannot give: a composed path is re-resolved by the kernel on every
call, so an attacker who can replace ``runtime/`` or ``.installed-credentials``
with a symlink between two of them redirects the next one, and a check that
passed a microsecond earlier proved nothing about the file that got written.
Against a held descriptor there is no component left to re-point -- the kernel
resolves nothing -- and each descriptor is compared once more against its own
no-follow directory entry before any operation is called a success, so a parent
that *was* replaced under the operation fails closed rather than reporting a
credential stored somewhere nobody chose.

**What each level has to prove.** The installation root and ``runtime/`` are
proved to be real directories, owned by this process's user, writable by nobody
else: they are shared, conventionally-moded locations -- the trusted root
discovery treats the root the same way -- and what makes a swap possible is
somebody else being able to write the name, not being able to read it. The store
directory itself is the owner-only proof in full, mode ``0o700`` from ``mkdir``
rather than from a later ``chmod``, because it is this module's own directory and
holds the bearers.

**Reading is the same proof the trusted configuration reader uses**,
:func:`~omnivia_core_client.owner_private.read_owner_private`, anchored to that
held descriptor: a regular file, not a symlink, owned by this process's user,
unreachable by group or world, read to a bound. Where the generic reader also
asks that the name still identify the same file afterward, this store asks for
less and is still exactly as safe: ``rotation_tolerant=True`` accepts a leaf
this store's own rotation has since moved on from, because the directory it is
held open on is reproved unchanged around the whole operation -- see
:data:`_READ_ATTEMPTS` -- and nothing else could have moved it. Anything else
is a refusal, and every refusal is a fixed sentence naming no path, no
reference and no bytes.

**Writing is atomic and owner-private from creation**, in that order of
importance. The material is written to a fresh private file in the same
directory -- created relative to the held descriptor with ``O_CREAT | O_EXCL``
and mode ``0o600``, written to completion, and fsynced -- and renamed over the
destination with both ends anchored to that same descriptor, so a reader sees the
old credential or the new one and never a half-written one, and no window exists
in which the bytes sit in a file anyone else could open.

**Windows keeps the pathname form**, because it has no ``O_DIRECTORY``, no
``O_NOFOLLOW`` and no ``dir_fd``. It is not thereby unproved. The whole chain --
the installation root, ``runtime/``, the store directory -- is walked by
:func:`~omnivia_core_client.owner_private.owner_private_chain` *before* every
operation and walked again *after* it, and the two are compared component by
component: each must be a real directory that is not a reparse point, owned by
this process's user, writable by nobody else, owner-only at the leaf, and still
the same directory afterwards. Nothing is created through a parent that has not
just been proved -- there is no ``mkdir(parents=True)`` here, only one component
at a time below a chain that proved out -- and nothing is renamed over or
unlinked when the chain cannot be proved, so a substituted component ends the
operation instead of redirecting it.

A component this module creates fresh is not merely proved afterwards, either.
There are no mode bits on Windows, so the mode ``mkdir`` and ``mkstemp`` are
given here is not a promise the filesystem keeps -- what a newly created
directory or file actually gets is whatever DACL its parent's inheritance and
the caller's token supply, which an elevated token can make owned by
``BUILTIN\\Administrators`` rather than this process's own user. Every
component this module creates, and the temporary file underneath the leaf, is
handed to :func:`~omnivia_core_client.owner_private.restrict_to_owner`
immediately after creation and before anything is written into it or created
below it, which sets its owner and DACL to this user alone rather than trusting
what creation happened to inherit -- and fails the operation closed, before the
proof below ever runs, if that could not be done. On top of that the leaf keeps
the owner-and-DACL proof taken from its *open handle* in
:func:`~omnivia_core_client.owner_private.owner_private_file`, which refuses a
reparse point for the same reason ``O_NOFOLLOW`` refuses a symlink -- what is
open is not what the attacker substituted, and the proof is taken on the handle
rather than on the name. Every part of it fails closed, including when the native
call does not complete.

A before-and-after pathname proof is weaker than a held descriptor and is not
claimed to be equal to one: an attacker who wins the whole race between the two
walks is refused rather than undetected, which is the strongest answer a platform
without ``*at`` calls admits.

Standard library plus this package's own parts.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final, NoReturn

from omnivia_core_client.credentials import (
    MAXIMUM_CREDENTIAL_CHARACTERS,
    Credential,
    CredentialReference,
)
from omnivia_core_client.errors import (
    CredentialInvalidError,
    CredentialMissingError,
    CredentialUnavailableError,
)
from omnivia_core_client.owner_private import (
    owner_private_chain,
    owner_private_directory_metadata,
    owner_private_file,
    owner_private_transaction,
    owner_writable_only,
    read_owner_private,
    restrict_to_owner,
    same_file,
)

__all__ = [
    "CONFIGURATION_HOSTS",
    "CONFIGURATION_STORE_DIRECTORY",
    "MAXIMUM_CONFIGURATION_BYTES",
    "STORE_DIRECTORY",
    "InstalledConfigStore",
    "InstalledCredentialStore",
]

#: The fixed location, relative to the installation state root, and the whole of
#: the layout this module knows.
#:
#: Under ``runtime/`` rather than beside it because ``runtime/`` is where this
#: installation's machine-local, non-portable, non-backed-up state already lives
#: -- the published service descriptors and the installation authority endpoint
#: are both there -- and because the four names an installation state root may
#: hold at its top level are fixed elsewhere, so a fifth would make every
#: already-configured installation unrecognisable to its own initialiser. The
#: leading dot is not concealment: it is what makes this name unable to collide
#: with a workspace directory, since a ``WorkspaceId`` must open with a letter or
#: a digit.
STORE_DIRECTORY: Final = ("runtime", ".installed-credentials")

#: Where the protected `omnivia.mcp-config.v1` documents live, on the same terms
#: and for the same reasons: under ``runtime/``, one fixed directory, one file per
#: host, and no component any caller supplies.
CONFIGURATION_STORE_DIRECTORY: Final = ("runtime", ".installed-mcp")

#: The whole vocabulary a protected configuration may be filed under.
#:
#: Closed, and closed *here* rather than at whichever caller happens to be
#: asking, because it is what makes a filename safe: a fixed word cannot traverse
#: out of the store, cannot differ only in case, and cannot spell a reserved
#: device name on Windows. :class:`InstalledConfigStore` takes no other key and
#: no path at all, so there is nothing to escape from.
CONFIGURATION_HOSTS: Final[tuple[str, ...]] = ("claude-code", "codex")

#: What a protected configuration may hold, inclusive. It is a fixed handful of
#: identifiers; the bound exists so a file somebody else grew cannot make a
#: reader allocate without limit.
MAXIMUM_CONFIGURATION_BYTES: Final = 65_536

#: Owner-only, and created that way. Applied at ``mkdir`` rather than by a later
#: ``chmod``, so there is no instant at which the directory is readable.
_DIRECTORY_MODE: Final = 0o700

#: What ``runtime/`` is created with when it is this module that first needs it.
#: Not ``0o700``: it is a shared installation directory holding published service
#: descriptors, and a store that narrowed it would narrow it for everything else
#: too. The umask narrows this further on most hosts; what matters is the proof
#: applied on the way down, not the mode this happened to ask for.
_PARENT_MODE: Final = 0o755

#: Owner-only, and created that way, for the same reason the directory is.
_FILE_MODE: Final = 0o600

#: What a stored file may hold, **inclusive**. The secret grammar's own bound:
#: this store holds credentials and a file longer than the longest admissible
#: credential is not one, whatever else it is.
_MAXIMUM_STORED_BYTES: Final = MAXIMUM_CREDENTIAL_CHARACTERS

#: How many times a read may be retried for whatever a read can still
#: transiently refuse.
#:
#: Rotation itself no longer spends this budget. :func:`_read_anchored` and
#: :func:`_read_by_path` pass ``rotation_tolerant=True`` to
#: :func:`~omnivia_core_client.owner_private.read_owner_private`: the directory
#: each holds open -- or reproves by chain -- around the whole operation is
#: nothing anybody but this process's own writer could have moved, so that
#: proof, not a retry, is what tells a legitimate rename apart from a
#: substitution. What is left for a retry to catch is smaller and rarer: this
#: process's own transient refusal of a leaf that is, in fact, there. Bounded
#: and small regardless, because a read that keeps losing is not transient any
#: more.
_READ_ATTEMPTS: Final = 3

#: How a reference becomes a filename. Sixteen bytes of BLAKE2s, hex: short
#: enough for any filesystem, fixed-length whatever the reference was, and
#: deterministic across interpreter runs in a way ``hash()`` is not.
_DIGEST_BYTES: Final = 16
_SUFFIX: Final = ".credential"
_PARTIAL_SUFFIX: Final = ".partial"

#: What a protected configuration file is called, after the host word. The
#: document is JSON and the path is public -- an MCP host names it on the server's
#: command line -- so it is spelled the way a human reading their own host
#: configuration would expect.
_CONFIGURATION_SUFFIX: Final = ".json"

#: The store's three health answers, and the only vocabulary it reports.
#:
#: Private until something outside needs to branch on them. A status command
#: renders them and nothing in this packet compares them, so publishing three
#: names now would be publishing a vocabulary before anyone has to agree on it.
_PRESENT: Final = "present"
_ABSENT: Final = "absent"
_UNUSABLE: Final = "unusable"

#: Whether this host can be walked by descriptor at all.
#:
#: Asked of the platform rather than assumed from ``os.name``: the walk needs
#: every one of these calls to accept ``dir_fd``, and a POSIX host whose kernel
#: does not offer the ``*at`` family would otherwise silently fall through to a
#: pathname form while this module's prose claimed otherwise. Windows has none of
#: them and takes the pathname form by design -- see the module docstring.
#: ``os.rename`` rather than ``os.replace`` in the probe: the two are one
#: ``renameat`` underneath and only the first is the name the interpreter
#: registers, so asking for ``os.replace`` would answer ``False`` on every host
#: that in fact supports it.
_ANCHORED: Final = (
    os.name != "nt"
    and {
        os.mkdir,
        os.open,
        os.rename,
        os.stat,
        os.unlink,
    }
    <= os.supports_dir_fd
)

_DIRECTORY_FLAGS: Final = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)

_CREATE_FLAGS: Final = (
    os.O_CREAT
    | os.O_EXCL
    | os.O_WRONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)


#: Raised from outside every handler, never inside -- the convention
#: ``credentials.py`` and ``local_ipc.py`` keep and
#: ``scripts/check-raise-discipline.py`` enforces. It matters more here than
#: almost anywhere: an ``OSError`` reachable through ``__context__`` quotes the
#: path of the file holding a bearer.


def _raise_missing() -> NoReturn:
    raise CredentialMissingError(
        "this installation holds no credential for that reference"
    )


def _raise_invalid() -> NoReturn:
    raise CredentialInvalidError("the stored installation credential is not usable")


def _raise_unavailable() -> NoReturn:
    raise CredentialUnavailableError(
        "the installation credential store could not be reached"
    )


def _raise_not_a_reference() -> NoReturn:
    raise CredentialInvalidError(
        "a stored credential is named by a credential reference"
    )


def _raise_not_a_credential() -> NoReturn:
    raise CredentialInvalidError("only a Credential may be stored")


class _Anchor:
    """The store directory held open, with the whole walk down to it still provable.

    Each :meth:`descend` opens one component relative to the last, never by a
    composed path, and refuses to go further unless what it opened proves out.
    The descriptors stay open until :meth:`close`, which is what leaves the
    caller operating on directories that cannot be re-pointed underneath it: the
    kernel resolves nothing for a ``dir_fd``.

    :meth:`unchanged` is the other half. Holding a descriptor makes the *open
    file* immune to substitution; it does not stop an attacker replacing the
    directory entry that named it, which would leave a successful store having
    written into a directory the installation no longer reaches. Comparing each
    descriptor against a fresh no-follow ``lstat`` of its own name, before the
    operation is called a success, turns that into a refusal.
    """

    __slots__ = ("_descriptors", "_entries")

    def __init__(self) -> None:
        self._descriptors: list[int] = []
        #: ``(parent descriptor or None, name, fstat at the moment it was opened)``
        self._entries: list[tuple[int | None, str, os.stat_result]] = []

    @property
    def descriptor(self) -> int:
        """The innermost directory opened so far."""
        return self._descriptors[-1]

    def descend(
        self, name: str, *, owner_only: bool, create: int | None = None
    ) -> bool:
        """Open `name` below the current directory and prove it, or answer ``False``.

        `create` is the mode to make the directory with when it is not there --
        ``None`` for an operation that must not bring the store into existence.
        The mode is given to ``mkdir`` rather than applied afterwards, so there is
        no instant at which a directory this module made is wider than it should
        be.
        """
        parent = self._descriptors[-1] if self._descriptors else None
        descriptor = _open_directory(name, parent)
        if descriptor < 0 and create is not None and parent is not None:
            try:
                os.mkdir(name, create, dir_fd=parent)
            except OSError:
                # Losing a race to create it is not a failure; the reopen below
                # decides, and it decides on what is actually there.
                pass
            descriptor = _open_directory(name, parent)
        if descriptor < 0:
            return False
        opened = _fstat(descriptor)
        # The same two policies the pathname walk applies, from the one place
        # that states them: a shared parent proved unswappable, the store's own
        # directory proved private. ``O_DIRECTORY`` has already decided this is a
        # directory, so the kind check inside them is a second opinion rather
        # than the only one.
        proved = opened is not None and (
            owner_private_directory_metadata(opened)
            if owner_only
            else owner_writable_only(opened)
        )
        if opened is None or not proved:
            _close(descriptor)
            return False
        self._descriptors.append(descriptor)
        self._entries.append((parent, name, opened))
        return True

    def unchanged(self) -> bool:
        """Whether every directory held open is still the one its name resolves to."""
        return all(
            (entry := _lstat(name, parent)) is not None and same_file(entry, opened)
            for parent, name, opened in self._entries
        )

    def close(self) -> None:
        for descriptor in self._descriptors:
            _close(descriptor)
        self._descriptors.clear()
        self._entries.clear()


#: The mode each component of a store's layout is created with, in order.
#:
#: ``runtime/`` shared, the store directory owner-only, and both applied at
#: ``mkdir`` rather than by a later ``chmod``. Beside the walk rather than inside
#: either store, because the two stores are the same layout with different leaves
#: and a second copy of this would be a second thing to keep in step.
_LAYOUT_MODES: Final = (_PARENT_MODE, _DIRECTORY_MODE)


def _anchor_to(root: Path, names: Sequence[str], *, create: bool) -> _Anchor | None:
    """Walk down to ``root/*names`` by descriptor, or answer ``None`` having gone nowhere.

    The root is never created: an installation state root this module had to bring
    into existence is not a trusted root, it is a directory this module invented.
    Everything below it is, when `create` says so, and each with its own mode.
    """
    anchor = _Anchor()
    descended = anchor.descend(str(root), owner_only=False)
    for index, name in enumerate(names):
        if not descended:
            break
        descended = anchor.descend(
            name,
            owner_only=index == len(names) - 1,
            create=_LAYOUT_MODES[index] if create else None,
        )
    if not descended:
        anchor.close()
        return None
    return anchor


def _read_anchored(
    anchor: _Anchor, leaf: str, maximum_bytes: int
) -> tuple[bytes | None, bool]:
    """The leaf's bytes, and whether anything stands where they belong.

    One byte more than the caller's bound is asked for and the extra byte is
    returned rather than trimmed, so a file *at* the bound is told apart from one
    *past* it. ``rotation_tolerant=True`` is what survives the store's own
    rename: this directory is held open and reproved unchanged by
    :meth:`_Anchor.unchanged` below, so a reader that opened the leaf a moment
    before a rotation landed keeps those bytes rather than losing them to a name
    that has since moved on.
    """
    content: bytes | None = None
    present = False
    for _ in range(_READ_ATTEMPTS):
        content = read_owner_private(
            Path(leaf),
            maximum_bytes=maximum_bytes,
            dir_fd=anchor.descriptor,
            rotation_tolerant=True,
        )
        if content is not None:
            present = True
            break
        if _lstat(leaf, anchor.descriptor) is None:
            # Absent rather than raced: there is nothing a retry could find, and a
            # rename lands the new file before the old name stops resolving.
            break
        present = True
    if not anchor.unchanged():
        content, present = None, False
    return content, present


def _write_anchored(anchor: _Anchor, leaf: str, material: bytes) -> bool:
    """Put `material` at `leaf`, atomically and owner-private from creation.

    Both ends of the rename are anchored to the one descriptor the directory is
    held open on -- a rename is only atomic within one filesystem, and a composed
    path would be re-resolved -- and the temporary is removed on every path that
    does not rename it.
    """
    temporary = _temporary_name()
    failed = not _publish(anchor.descriptor, temporary, material)
    if not failed:
        try:
            os.replace(
                temporary,
                leaf,
                src_dir_fd=anchor.descriptor,
                dst_dir_fd=anchor.descriptor,
            )
        except OSError:
            failed = True
    if not failed:
        _fsync(anchor.descriptor)
        # Last, and after everything else has succeeded: a parent replaced under
        # the operation means this file is not where the installation will look
        # for it, and saying so is the only honest answer left.
        failed = not anchor.unchanged()
    if failed:
        _unlink(temporary, anchor.descriptor)
    return not failed


def _remove_anchored(anchor: _Anchor, leaf: str) -> bool:
    return _unlink(leaf, anchor.descriptor) and anchor.unchanged()


# --- the pathname form, for a host with no ``dir_fd`` ------------------------
#
# Windows only, and the same three operations. There is no descriptor to hold and
# no ``dir_fd`` to anchor to, so what stands in place of the held walk is the
# same walk taken by name -- ``owner_private_chain``, which proves every
# component is a real directory, not a reparse point, owned by this user, not
# writable by anybody else, and owner-only at the leaf -- taken *before* the
# operation and again *after* it, with the two compared component by component.
# Nothing is created through a parent that has not just been proved, and nothing
# is renamed over or unlinked when the chain does not prove out.


def _proved_chain(
    root: Path, names: Sequence[str], *, create: bool
) -> tuple[os.stat_result, ...] | None:
    """Prove ``root/*names`` by pathname, bringing missing components into being.

    Each component is created only once the chain *above* it has proved out, one
    ``mkdir`` at a time and with its own mode. Never ``mkdir(parents=True)``: that
    call would create a component below a parent nothing has looked at, which is
    the whole of what an attacker who owns a name above the store wants.

    A ``mkdir`` that loses a race is not a failure; the proof at the end decides,
    and it decides on what is actually there. A ``mkdir`` that wins the race is
    handed to :func:`~omnivia_core_client.owner_private.restrict_to_owner` before
    this loop goes on to create anything beneath it, because the mode just given
    to ``mkdir`` is not a promise Windows keeps: a component this call could not
    restrict to this user alone ends the walk here rather than being created into
    further.
    """
    if create:
        for index in range(len(names)):
            # The prefix is proved as *parents*: the component about to be created
            # goes below them, and the installation root is a shared directory no
            # real installation has made owner-only.
            if (
                owner_private_chain(root, names[:index], owner_private_leaf=False)
                is None
            ):
                return None
            target = root.joinpath(*names[: index + 1])
            if _lstat(str(target), None) is None:
                created = True
                try:
                    target.mkdir(_LAYOUT_MODES[index])
                except OSError:
                    # Losing a race to create it is not a failure; the proof at
                    # the end decides, and it decides on what is actually there.
                    created = False
                if created and not restrict_to_owner(target, directory=True):
                    # A component this call just made could not be restricted to
                    # this user alone. Stopping here, rather than falling through
                    # to the proof below, is what keeps a child from ever being
                    # created beneath a directory whose security could not be
                    # pinned down.
                    return None
    return owner_private_chain(root, names)


def _same_chain(
    before: Sequence[os.stat_result], after: tuple[os.stat_result, ...] | None
) -> bool:
    """Whether the chain proved before an operation is the chain that is there now."""
    return (
        after is not None
        and len(before) == len(after)
        and all(same_file(first, second) for first, second in zip(before, after))
    )


def _read_by_path(
    root: Path, names: Sequence[str], leaf: str, maximum_bytes: int
) -> tuple[bytes | None, bool]:
    """The pathname form of :func:`_read_anchored`, proved by chain rather than a hold.

    ``rotation_tolerant=True`` for the same reason: the chain is reproved by
    name, before and after, against `before` -- nothing but this process's own
    writer could have moved a leaf inside a store directory proved unchanged --
    so a rename this store's own rotation made is not a substitution.
    """
    before = _proved_chain(root, names, create=False)
    if before is None:
        return None, _entry_exists(root.joinpath(*names))
    path = root.joinpath(*names, leaf)
    content: bytes | None = None
    present = False
    for _ in range(_READ_ATTEMPTS):
        content = read_owner_private(
            path, maximum_bytes=maximum_bytes, rotation_tolerant=True
        )
        if content is not None:
            present = True
            break
        if _lstat(str(path), None) is None:
            break
        present = True
    if not _same_chain(before, owner_private_chain(root, names)):
        content, present = None, False
    return content, present


def _write_by_path(
    root: Path, names: Sequence[str], leaf: str, material: bytes
) -> bool:
    """The pathname form of :func:`_write_anchored`, proved on both sides.

    ``mkstemp``, not a name this module composes: it creates with ``O_EXCL`` and
    mode ``0o600`` in one call, so there is no instant at which the file exists
    and is readable, and no name an attacker could have pre-created as a reparse
    point. On Windows that mode is not kept by the filesystem, so
    :func:`~omnivia_core_client.owner_private.restrict_to_owner` is called on the
    fresh name first, before a byte is written and before the descriptor's own
    proof runs; either way the proof is taken on the descriptor it returns, and
    the chain is proved again before the rename -- a chain that stopped proving
    between the two must not be published into -- and once more after it.
    """
    before = _proved_chain(root, names, create=True)
    if before is None:
        return False
    directory = root.joinpath(*names)
    descriptor, temporary = -1, ""
    failed = False
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=str(directory), suffix=_PARTIAL_SUFFIX
        )
        failed = not restrict_to_owner(Path(temporary), directory=False)
        if not failed:
            metadata = os.fstat(descriptor)
            failed = not owner_private_file(metadata, descriptor) or not _write_all(
                descriptor, material
            )
        if not failed:
            os.fsync(descriptor)
    except (OSError, ValueError):
        failed = True
    finally:
        _close(descriptor)
    if not failed and not _same_chain(before, owner_private_chain(root, names)):
        failed = True
    if not failed:
        try:
            os.replace(temporary, directory / leaf)
        except OSError:
            failed = True
    if not failed:
        failed = not _same_chain(before, owner_private_chain(root, names))
    if failed and temporary:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return not failed


def _remove_by_path(root: Path, names: Sequence[str], leaf: str) -> bool:
    """Remove one leaf by pathname, and never through a chain that did not prove.

    An unprovable chain with nothing standing where the store belongs is the
    idempotent case -- there is no file to remove and the caller's state is already
    the one it asked for. An unprovable chain with *something* there is not: an
    unlink down it would delete whatever that something points at.
    """
    before = _proved_chain(root, names, create=False)
    if before is None:
        return not _entry_exists(root.joinpath(*names))
    removed = True
    try:
        (root.joinpath(*names, leaf)).unlink()
    except FileNotFoundError:
        removed = True
    except OSError:
        removed = False
    return removed and _same_chain(before, owner_private_chain(root, names))


def _entry_exists(path: Path) -> bool:
    """Whether *something* stands at `path`, whatever kind of thing it is.

    Pathname-resolved and no-follow, and asked only once a walk has already
    refused: it tells the two refusals apart. Nothing there at all is ``absent``
    and an operation with nothing to do; something there this module would not
    descend into is ``unusable`` and an operation that must not claim to have run.
    """
    return _lstat(str(path), None) is not None


class InstalledCredentialStore:
    """One installation's private credentials, keyed by reference.

    Written as a plain slots class rather than a frozen dataclass for the reason
    :class:`~omnivia_core_client.CredentialReference` is: the validated form is
    the only form, and a generated ``repr`` would print the installation root
    into every log line and traceback frame that renders one.

    Holds no material and no descriptor. Every method that needs the bytes walks
    down to them, reads them, uses them and drops them, so nothing here outlives
    the call that asked -- and a credential this store handed out a second ago
    says nothing about whether the next :meth:`resolve` will succeed.
    """

    __slots__ = ("_directory", "_root")

    def __init__(self, installation_state: Path) -> None:
        if (
            not isinstance(installation_state, Path)
            or not installation_state.is_absolute()
        ):
            raise ValueError(
                "installation_state must be an absolute path to the trusted "
                "installation state root"
            )
        self._root = installation_state
        self._directory = installation_state.joinpath(*STORE_DIRECTORY)

    def resolve(self, reference: CredentialReference) -> Credential:
        """The credential stored under `reference`, read fresh from disk.

        No cache, by construction rather than by a TTL: there is no entry to
        expire and no state to clear, so a credential rotated or revoked between
        two calls is simply not what the second call reads.
        """
        if not isinstance(reference, CredentialReference):
            _raise_not_a_reference()
        content, _ = self._load(reference)
        if content is None:
            _raise_missing()
        credential = _admissible(content)
        if credential is None:
            _raise_invalid()
        return credential

    def health(self, reference: CredentialReference) -> str:
        """``present``, ``absent`` or ``unusable`` -- and nothing else, ever.

        A fixed vocabulary rather than a reason, because the caller for this is a
        status command a human reads and the reasons are all the same sentence:
        something about the stored file is wrong and the fix is to rotate it.
        Distinguishing "wrong owner" from "too long" here would publish what an
        attacker changed it to.
        """
        if not isinstance(reference, CredentialReference):
            _raise_not_a_reference()
        content, present = self._load(reference)
        if content is None:
            return _UNUSABLE if present else _ABSENT
        return _PRESENT if _admissible(content) is not None else _UNUSABLE

    def store(self, reference: CredentialReference, credential: Credential) -> None:
        """Put `credential` under `reference`, atomically and owner-private.

        Replacement is a rename over the destination, so a concurrent
        :meth:`resolve` reads the credential that was there or the one being put
        there and never a truncated file. The temporary lives in the same
        directory -- a rename is only atomic within one filesystem, and both ends
        of the rename are anchored to the one descriptor that directory is held
        open on -- and is removed on every path that does not rename it.
        """
        if not isinstance(reference, CredentialReference):
            _raise_not_a_reference()
        if not isinstance(credential, Credential):
            _raise_not_a_credential()
        material = credential.reveal().encode("ascii")
        leaf = self._leaf(reference)
        if not _ANCHORED:
            if not _write_by_path(self._root, STORE_DIRECTORY, leaf, material):
                _raise_unavailable()
            return
        anchor = _anchor_to(self._root, STORE_DIRECTORY, create=True)
        if anchor is None:
            _raise_unavailable()
        try:
            stored = _write_anchored(anchor, leaf, material)
        finally:
            anchor.close()
        if not stored:
            _raise_unavailable()

    def remove(self, reference: CredentialReference) -> None:
        """Delete the credential stored under `reference`. Idempotent.

        A reference that was never stored is already in the state this asks for,
        so an absent file is success rather than a refusal -- which is what lets
        a revoke be re-run after a partial failure without a special case. A
        store that cannot be proved is *not* that case: unlinking through an
        unproved parent would delete whatever it pointed at.
        """
        if not isinstance(reference, CredentialReference):
            _raise_not_a_reference()
        leaf = self._leaf(reference)
        if not _ANCHORED:
            if not _remove_by_path(self._root, STORE_DIRECTORY, leaf):
                _raise_unavailable()
            return
        anchor = _anchor_to(self._root, STORE_DIRECTORY, create=False)
        if anchor is None:
            if _entry_exists(self._directory):
                _raise_unavailable()
            return
        try:
            removed = _remove_anchored(anchor, leaf)
        finally:
            anchor.close()
        if not removed:
            _raise_unavailable()

    def __repr__(self) -> str:
        """Says what this is and not where it is.

        The installation root is not a secret, but it is a filesystem layout that
        ends up in whatever renders this, and the one thing a reader of a log
        should not be handed is the directory holding the bearers.
        """
        return "InstalledCredentialStore(<redacted>)"

    __str__ = __repr__

    def _leaf(self, reference: CredentialReference) -> str:
        """The one filename this reference may ever name, and never a path."""
        if not isinstance(reference, CredentialReference):
            _raise_not_a_reference()
        digest = hashlib.blake2s(
            reference.value.encode("ascii"), digest_size=_DIGEST_BYTES
        ).hexdigest()
        return f"{digest}{_SUFFIX}"

    def _load(self, reference: CredentialReference) -> tuple[bytes | None, bool]:
        """The stored bytes, and whether anything stands where they belong.

        ``None`` for every reason there might not be any, and the flag beside it
        is the only thing separating "nothing is stored" from "what is stored was
        refused" -- which is all :meth:`health` needs and more than
        :meth:`resolve` uses.

        One byte more than the bound is read, and the extra byte is returned
        rather than trimmed. That is what tells a file *at* the longest admissible
        credential apart from one *past* it: reading exactly the bound would hand
        back the first 4096 bytes of a much larger file as though they were the
        whole of a credential, and trimming here would do the same. The secret
        grammar refuses the oversized value, so there is one bound rather than
        two, and it is the one that defines what a credential is.
        """
        leaf = self._leaf(reference)
        bound = _MAXIMUM_STORED_BYTES + 1
        if not _ANCHORED:
            return _read_by_path(self._root, STORE_DIRECTORY, leaf, bound)
        anchor = _anchor_to(self._root, STORE_DIRECTORY, create=False)
        if anchor is None:
            return None, _entry_exists(self._directory)
        try:
            return _read_anchored(anchor, leaf, bound)
        finally:
            anchor.close()


class InstalledConfigStore:
    """One installation's protected `omnivia.mcp-config.v1` documents, keyed by host.

    The credential store's layout and the credential store's proofs, with two
    differences and no third. The key is a word from :data:`CONFIGURATION_HOSTS`
    rather than a reference, so there is no digest to take: a closed vocabulary
    word already cannot traverse out of the store, cannot differ only in case and
    cannot spell a reserved device name, and a filename a reader can recognise is
    worth more here than one they cannot, since the *path* is public -- it is what
    an MCP host's own configuration names on its command line. And the content is
    a document rather than a secret, bounded at
    :data:`MAXIMUM_CONFIGURATION_BYTES` and returned as bytes for the caller to
    parse.

    **Nothing here raises for a filesystem answer.** :meth:`write` and
    :meth:`remove` answer ``False``, :meth:`read` answers ``None`` and
    :meth:`health` answers one of three words, for the reason
    :mod:`~omnivia_core_client.owner_private` answers ``None``: the caller has its
    own fixed, payload-free sentence for every one of these, and a path is exactly
    what such a sentence must not carry. A host outside the vocabulary is the one
    exception, and it is not a filesystem answer -- it is a caller that has gone
    wrong before any file was touched.
    """

    __slots__ = ("_directory", "_root")

    def __init__(self, installation_state: Path) -> None:
        if (
            not isinstance(installation_state, Path)
            or not installation_state.is_absolute()
        ):
            raise ValueError(
                "installation_state must be an absolute path to the trusted "
                "installation state root"
            )
        self._root = installation_state
        self._directory = installation_state.joinpath(*CONFIGURATION_STORE_DIRECTORY)

    def path(self, host: str) -> Path:
        """The one configuration path this installation uses for `host`.

        Public on purpose, and the only thing here that is: an MCP host launches
        the server with this path on its command line, so it is printed in a
        snippet and passed to a child process. That is why it is derived rather
        than chosen -- a function of the trusted root and a closed vocabulary word,
        with no caller-supplied component -- and why a caller may name the path but
        never write, replace or delete through it.
        """
        return self._directory / _configuration_leaf(host)

    def read(self, host: str) -> bytes | None:
        """At most :data:`MAXIMUM_CONFIGURATION_BYTES` from `host`'s document.

        ``None`` for every reason there might not be one: absent, not a regular
        file, a symlink or reparse point, not owner-private, reached through a
        store this module would not descend into, longer than the bound, or an I/O
        failure at any point. A document *at* the bound is returned; one past it is
        not, which is what the extra byte read is for.
        """
        content, _ = self._load(host)
        return content

    def health(self, host: str) -> str:
        """``present``, ``absent`` or ``unusable`` -- and nothing else, ever.

        The credential store's vocabulary and the credential store's reason for
        it: the caller is a status command a human reads, and naming what is wrong
        with the file would publish what an attacker changed it to.
        """
        content, present = self._load(host)
        if content is not None:
            return _PRESENT
        return _UNUSABLE if present else _ABSENT

    def write(self, host: str, content: bytes) -> bool:
        """Put `content` at `host`'s document, atomically and owner-private.

        Bounded before anything is created: a document past
        :data:`MAXIMUM_CONFIGURATION_BYTES` is one this store's own reader would
        refuse, and writing a file that can never be read back is not a service to
        anybody.
        """
        leaf = _configuration_leaf(host)
        if not isinstance(content, bytes) or len(content) > MAXIMUM_CONFIGURATION_BYTES:
            return False
        if not self._prepare_directory():
            return False
        with owner_private_transaction(self.path(host)) as acquired:
            return acquired and self._write_unlocked(leaf, content)

    def _prepare_directory(self) -> bool:
        """Create and prove the protected store before its transaction starts."""
        if not _ANCHORED:
            return _proved_chain(
                self._root, CONFIGURATION_STORE_DIRECTORY, create=True
            ) is not None
        anchor = _anchor_to(self._root, CONFIGURATION_STORE_DIRECTORY, create=True)
        if anchor is None:
            return False
        anchor.close()
        return True

    def _write_unlocked(self, leaf: str, content: bytes) -> bool:
        """Write after :func:`owner_private_transaction` granted this path."""
        if not _ANCHORED:
            return _write_by_path(
                self._root, CONFIGURATION_STORE_DIRECTORY, leaf, content
            )
        anchor = _anchor_to(self._root, CONFIGURATION_STORE_DIRECTORY, create=False)
        if anchor is None:
            return False
        try:
            return _write_anchored(anchor, leaf, content)
        finally:
            anchor.close()

    def remove(self, host: str) -> bool:
        """Delete `host`'s document. Idempotent, and never through an unproved store.

        A host that was never configured is already in the state this asks for, so
        an absent file is success -- which is what lets a revoke be re-run after a
        partial failure without a special case. A store that cannot be proved is
        *not* that case: unlinking down it would delete whatever it pointed at.
        """
        leaf = _configuration_leaf(host)
        if not _entry_exists(self._directory):
            return True
        with owner_private_transaction(self.path(host)) as acquired:
            return acquired and self._remove_unlocked(leaf)

    def _remove_unlocked(self, leaf: str) -> bool:
        """Remove after :func:`owner_private_transaction` granted this path."""
        if not _ANCHORED:
            return _remove_by_path(self._root, CONFIGURATION_STORE_DIRECTORY, leaf)
        anchor = _anchor_to(self._root, CONFIGURATION_STORE_DIRECTORY, create=False)
        if anchor is None:
            return not _entry_exists(self._directory)
        try:
            return _remove_anchored(anchor, leaf)
        finally:
            anchor.close()

    def __repr__(self) -> str:
        """Says what this is and not where it is, for the sibling store's reason."""
        return "InstalledConfigStore(<redacted>)"

    __str__ = __repr__

    def _load(self, host: str) -> tuple[bytes | None, bool]:
        """The document's bytes, and whether anything stands where they belong."""
        leaf = _configuration_leaf(host)
        return self._load_unlocked(leaf)

    def _load_unlocked(self, leaf: str) -> tuple[bytes | None, bool]:
        """Read one validated leaf, optionally while its writer lock is held."""
        bound = MAXIMUM_CONFIGURATION_BYTES + 1
        if not _ANCHORED:
            content, present = _read_by_path(
                self._root, CONFIGURATION_STORE_DIRECTORY, leaf, bound
            )
        else:
            anchor = _anchor_to(self._root, CONFIGURATION_STORE_DIRECTORY, create=False)
            if anchor is None:
                return None, _entry_exists(self._directory)
            try:
                content, present = _read_anchored(anchor, leaf, bound)
            finally:
                anchor.close()
        if content is not None and len(content) > MAXIMUM_CONFIGURATION_BYTES:
            # Past the bound is not a document, and the caller is told the same
            # thing it would be told about any other unusable file.
            content = None
        return content, present


def _configuration_leaf(host: str) -> str:
    """The one filename `host` may ever name, and never a path.

    The vocabulary is closed *here* rather than at whichever caller happens to be
    asking, because that is what makes the filename safe. A ``ValueError`` rather
    than a refusal verdict: every other answer this store gives is about the
    filesystem, and a host outside :data:`CONFIGURATION_HOSTS` is a caller that
    went wrong before any file was reached.
    """
    if host not in CONFIGURATION_HOSTS:
        raise ValueError("host must be one of this installation's known MCP hosts")
    return f"{host}{_CONFIGURATION_SUFFIX}"


def _admissible(content: bytes) -> Credential | None:
    """The credential those bytes spell, or ``None`` if they do not spell one."""
    credential: Credential | None = None
    try:
        credential = Credential(content.decode("ascii"))
    except (UnicodeDecodeError, CredentialInvalidError):
        credential = None
    return credential


def _open_directory(name: str, dir_fd: int | None) -> int:
    """Open a directory without following a final symlink, or return ``-1``."""
    descriptor = -1
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=dir_fd)
    except (OSError, ValueError):
        descriptor = -1
    return descriptor


def _lstat(name: str, dir_fd: int | None) -> os.stat_result | None:
    value: os.stat_result | None = None
    try:
        value = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except (OSError, ValueError):
        value = None
    return value


def _fstat(descriptor: int) -> os.stat_result | None:
    value: os.stat_result | None = None
    try:
        value = os.fstat(descriptor)
    except OSError:
        value = None
    return value


def _temporary_name() -> str:
    """A name nobody could have pre-created, and no secret in it.

    Sixteen bytes from the operating system's generator rather than a counter or
    a pid: the file is created with ``O_EXCL``, so a guessed name loses the race
    rather than winning it, and an unguessable one means there is no race to run.
    """
    return f"{os.urandom(_DIGEST_BYTES).hex()}{_PARTIAL_SUFFIX}"


def _publish(dir_fd: int, name: str, material: bytes) -> bool:
    """Create `name` below `dir_fd`, owner-private, and put the whole of it there.

    ``O_CREAT | O_EXCL`` with mode ``0o600`` in one call, so there is no instant
    at which the file exists and is readable and no name an attacker could have
    pre-created as a symlink -- and ``O_NOFOLLOW`` besides, so a symlink that
    somehow *is* there is refused rather than followed. The owner-only proof is
    taken on the descriptor before a byte is written.
    """
    descriptor = -1
    written = False
    try:
        descriptor = os.open(name, _CREATE_FLAGS, _FILE_MODE, dir_fd=dir_fd)
        metadata = os.fstat(descriptor)
        if owner_private_file(metadata, descriptor) and _write_all(
            descriptor, material
        ):
            os.fsync(descriptor)
            written = True
    except (OSError, ValueError):
        written = False
    finally:
        _close(descriptor)
    return written


def _write_all(descriptor: int, material: bytes) -> bool:
    """Write the whole of `material` to `descriptor`, or say it could not be.

    ``os.write`` is the raw system call: it is allowed to accept less than it was
    handed and reports that only in its return value, which is not an error and
    raises nothing. Writing once and moving on would rename a *truncated*
    credential over the destination -- a file that is owner-private, atomic,
    fresh, and the wrong secret. So the whole of it is written or the caller is
    told it was not.

    Zero accepted ends it rather than spinning: a descriptor that took none of
    what it was given is not one a further attempt makes progress on, and the
    caller has a refusal for exactly this.
    """
    written = 0
    while written < len(material):
        progress = os.write(descriptor, material[written:])
        if progress <= 0:
            return False
        written += progress
    return True


def _unlink(name: str, dir_fd: int) -> bool:
    """Remove one name below `dir_fd`. Absent is success; anything else is not.

    ``os.unlink`` never follows a final symlink, so a leaf somebody replaced with
    a link to a file elsewhere loses the link and keeps the file.
    """
    removed = True
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        removed = True
    except OSError:
        removed = False
    return removed


def _close(descriptor: int) -> None:
    if descriptor < 0:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _fsync(descriptor: int) -> None:
    """Make the rename itself durable, where the platform can.

    Best effort on purpose: a platform that will not fsync a directory descriptor
    has still performed an atomic rename, which is the property this store
    promises. Failing the write because the durability barrier is unavailable
    would refuse a credential that is, in fact, correctly in place.
    """
    try:
        os.fsync(descriptor)
    except OSError:
        pass
